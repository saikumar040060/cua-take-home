"""Deterministic replay — the production execution path. No model in the loop.

Execution discipline per step:

1. **Resolve** the target through the artifact's locator fallback chain
   (semantic first; resolving via a fallback is logged as degraded — an
   early drift signal, not a silent pass).
2. **Gate** the action: allowlist check on every action, and the risky-step
   gate — a RISKY step executes only with the caller's explicit
   ``confirm_risky`` sign-off, otherwise the run escalates to a human
   instead of submitting.
3. **Act**, then **verify** the step's checkpoint with a condition-based
   wait (poll-until-true within the step's budget — never a blind sleep;
   transient slowness is absorbed here).

When a step cannot proceed (locator unresolvable or checkpoint unmet), the
engine classifies the live state in a strict order that implements the
three-bucket contract:

  a. **Declared business outcomes** — terminal, reported with their code.
     The system worked; the answer was negative. Checked first because a
     legitimate "no" must never be misread as breakage.
  b. **Recovery rules** — known interruptions with a recorded deterministic
     fix (dismiss the session interstitial, reload). Bounded by
     ``max_applications``; applied, then the step is re-verified.
  c. **Hard failure** — unknown state. Stop, capture screenshot + DOM
     snapshot, report step / expected / observed. Optionally escalate to a
     human who can repair state on the same live session and resume.
"""

from __future__ import annotations

import time

from cua.browser import (
    BrowserSession,
    ResolutionError,
    probe_holds,
    resolve_target,
)
from cua.escalation.console import OperatorConsole
from cua.escalation.intervention import (
    InterventionReason,
    InterventionResolution,
    build_intervention,
)
from cua.replay.result import (
    BusinessOutcomeResult,
    FailureDetail,
    RecoveryEvent,
    ReplayResult,
    ReplayStatus,
    StepReport,
)
from cua.runlog import RunLogger
from cua.safety.policy import PolicyGate, PolicyViolation
from cua.schema import (
    ActionType,
    Capability,
    RecoveryActionKind,
    RiskLevel,
    Sensitivity,
    Step,
    utc_now,
)


class ParamError(ValueError):
    """Invalid invocation parameters — rejected before the browser moves."""


class ReplayEngine:
    def __init__(
        self,
        session: BrowserSession,
        capability: Capability,
        gate: PolicyGate,
        run_logger: RunLogger,
        *,
        confirm_risky: bool = False,
        escalate_on_failure: bool = False,
        console: OperatorConsole | None = None,
        step_screenshots: bool = False,
    ):
        self.session = session
        self.page = session.page
        self.cap = capability
        self.gate = gate
        self.log = run_logger
        self.confirm_risky = confirm_risky
        self.escalate_on_failure = escalate_on_failure
        # When set, capture a screenshot after every verified step -- a full
        # visual trail so a reviewer can see what the operator's screen
        # showed at each action, not just at the failure point. Off by
        # default: CLI replays keep the lean evidence set (failure and
        # intervention shots only); the service turns it on so an employee
        # investigating an escalated run can walk the whole flow visually.
        self.step_screenshots = step_screenshots
        self.console = console or OperatorConsole(self.page, gate, run_logger)
        self._recovery_counts: dict[str, int] = {}
        self._recoveries: list[RecoveryEvent] = []
        self._step_reports: list[StepReport] = []
        self._outputs: dict[str, str] = {}
        self._intervention_id: str | None = None

    # ------------------------------------------------------------------ api

    def run(self, params: dict[str, str]) -> ReplayResult:
        started = utc_now()
        self._validate_params(params)
        self.log.event(
            "run_started", actor="automation", phase="replay",
            capability=self.cap.capability_id, version=self.cap.version,
            params={k: v for k, v in params.items()},  # redactor masks these
        )

        entry_url = self._substitute(self.cap.entry_url_template, params)
        try:
            self.gate.check_url(entry_url)
            self.page.goto(entry_url)
        except (PolicyViolation, Exception) as exc:
            if isinstance(exc, PolicyViolation):
                raise
            return self._hard_failure(
                started, self.cap.steps[0],
                expected=f"entry page loads at {self.cap.entry_url_template}",
                observed=f"navigation error: {exc}",
            )

        for step in self.cap.steps:
            outcome = self._run_step(step, params)
            if outcome is not None:  # terminal (business outcome or failure)
                return self._finalize(started, outcome)

        result = ReplayResult(
            run_id=self.log.run_id,
            capability_id=self.cap.capability_id,
            capability_version=self.cap.version,
            status=ReplayStatus.SUCCESS,
            started_at=started,
            finished_at=utc_now(),
            outputs=self._outputs,
            recoveries=self._recoveries,
            steps=self._step_reports,
            events_log_path=str(self.log.events_path),
            intervention_id=self._intervention_id,
        )
        self.log.event("run_succeeded", actor="automation",
                       outputs=list(self._outputs))
        return self._finalize(started, result)

    # ----------------------------------------------------------- step logic

    def _run_step(self, step: Step, params: dict[str, str]):
        """Execute one step. Returns None to continue, or a terminal result."""
        t0 = time.monotonic()
        report = StepReport(step_id=step.id, status="ok", duration_ms=0)
        self._step_reports.append(report)

        # -- resolve -------------------------------------------------------
        handle = None
        if step.target is not None:
            resolved = self._resolve_with_classification(step, report, params)
            if isinstance(resolved, (ReplayResult,)):
                report.duration_ms = int((time.monotonic() - t0) * 1000)
                return resolved
            handle = resolved

        # -- gate ----------------------------------------------------------
        action_url = (
            self._substitute(step.url_template, params)
            if step.action == ActionType.NAVIGATE
            else self.page.url
        )
        self.gate.check_action(step.action.value, action_url)

        if step.risk == RiskLevel.RISKY and not self.confirm_risky:
            resolution = self._escalate(
                InterventionReason.RISKY_ACTION_CONFIRMATION, step,
                f"step {step.id!r} is classified RISKY "
                f"({step.description}) and the caller did not pre-confirm "
                "(confirm_risky=false). A human must approve or deny.",
            )
            if resolution != InterventionResolution.APPROVED:
                self.log.event("risky_denied", actor="human", step=step.id)
                return self._hard_failure(
                    None, step,
                    expected="risky step approved (confirm_risky or operator sign-off)",
                    observed=f"operator resolution: {resolution.value if resolution else 'none'}",
                )
            self.log.event("risky_approved", actor="human", step=step.id)

        # -- act -----------------------------------------------------------
        try:
            if step.action == ActionType.NAVIGATE:
                self.page.goto(action_url)
            elif step.action == ActionType.CLICK:
                handle.click()
            elif step.action == ActionType.FILL:
                value = self._substitute(step.value_template, params)
                handle.fill(value)
                if handle.input_value() != value:
                    return self._hard_failure(
                        None, step,
                        expected="field read-back equals the filled value",
                        observed="read-back mismatch after fill",
                    )
            elif step.action == ActionType.SELECT:
                handle.select_option(self._substitute(step.value_template, params))
            elif step.action == ActionType.PRESS:
                handle.press(step.value_template or "Enter")
            elif step.action == ActionType.READ:
                value = handle.inner_text().strip()
                if step.output_name:
                    self._outputs[step.output_name] = value
        except Exception as exc:
            return self._classify_or_fail(
                step, report,
                expected=f"action {step.action.value} executes on {step.target.description if step.target else action_url}",
                observed=f"action raised: {exc}",
            )

        # -- verify --------------------------------------------------------
        checkpoint = self._substitute_probe(step.checkpoint, params)
        if not probe_holds(self.page, checkpoint, step.wait_timeout_ms):
            terminal = self._classify_or_fail(
                step, report,
                expected=checkpoint.description,
                observed=self._observed_summary(),
            )
            report.duration_ms = int((time.monotonic() - t0) * 1000)
            return terminal

        report.duration_ms = int((time.monotonic() - t0) * 1000)
        if self.step_screenshots:
            self.log.save_screenshot(self.page, f"step_{step.id}")
        self.log.event(
            "step_ok", actor="automation", step=step.id,
            action=step.action.value, locator=report.locator_used,
            locator_rank=report.locator_rank, duration_ms=report.duration_ms,
        )
        return None

    def _resolve_with_classification(
        self, step: Step, report: StepReport, params: dict[str, str]
    ):
        """Resolve the step target; on failure run the three-bucket triage."""
        target = self._substitute_target(step.target, params)
        for attempt in (1, 2):
            try:
                handle, rank, describe = resolve_target(
                    self.page, target, timeout_ms=step.wait_timeout_ms
                )
                report.locator_used = describe
                report.locator_rank = rank
                if rank > 0:
                    report.status = "degraded_locator"
                    self.log.event(
                        "locator_degraded", actor="automation", step=step.id,
                        used=describe, rank=rank,
                        detail="primary locator failed; UI drift suspected",
                    )
                return handle
            except ResolutionError as exc:
                terminal = self._classify_or_fail(
                    step, report,
                    expected=f"exactly one visible match for {target.description}",
                    observed="; ".join(exc.attempts) or "no locator matched",
                    allow_retry=(attempt == 1),
                )
                if terminal is None:
                    continue  # a recovery rule fired — retry resolution once
                return terminal
        return terminal  # pragma: no cover

    def _substitute_target(
        self, target: "ElementTarget | None", params: dict[str, str]
    ) -> "ElementTarget | None":
        """Fill '{param}' placeholders recorded into locator text (e.g. a
        data-table row anchored on a declared input's value) with the
        current invocation's concrete params — the same templating
        ``value_template``/``url_template`` already get, extended to
        locators now that a locator can legitimately reference an input."""
        if target is None:
            return None
        substituted = target.model_copy(deep=True)
        substituted.description = self._substitute(substituted.description, params)
        for locator in substituted.locators:
            locator.value = self._substitute(locator.value, params)
            if locator.name:
                locator.name = self._substitute(locator.name, params)
        return substituted

    def _substitute_probe(self, probe, params: dict[str, str]):
        """Same substitution, applied to a StateProbe's optional target
        (used by fill/select/read checkpoints that assert the control is
        still present) — a no-op copy when the probe has no target."""
        if probe.target is None:
            return probe
        substituted = probe.model_copy(deep=True)
        substituted.target = self._substitute_target(substituted.target, params)
        return substituted

    # ------------------------------------------------- three-bucket triage

    def _classify_or_fail(
        self, step: Step, report: StepReport, *, expected: str, observed: str,
        allow_retry: bool = False,
    ):
        """Bucket a — declared business outcome?"""
        for outcome in self.cap.business_outcomes:
            if probe_holds(self.page, outcome.detector, timeout_ms=0):
                self.log.event(
                    "business_outcome", actor="automation", step=step.id,
                    code=outcome.code,
                )
                report.status = "business_outcome"
                return ReplayResult(
                    run_id=self.log.run_id,
                    capability_id=self.cap.capability_id,
                    capability_version=self.cap.version,
                    status=ReplayStatus.BUSINESS_OUTCOME,
                    started_at=utc_now(),
                    finished_at=utc_now(),
                    business_outcome=BusinessOutcomeResult(
                        code=outcome.code,
                        description=outcome.description,
                        detected_at_step_id=step.id,
                    ),
                    recoveries=self._recoveries,
                    steps=self._step_reports,
                    events_log_path=str(self.log.events_path),
                )

        # Bucket b — known recoverable condition?
        for rule in self.cap.recovery_rules:
            if not probe_holds(self.page, rule.detector, timeout_ms=0):
                continue
            count = self._recovery_counts.get(rule.id, 0)
            if count >= rule.max_applications:
                return self._hard_failure(
                    None, step,
                    expected=f"recovery rule {rule.id!r} within its budget "
                    f"({rule.max_applications})",
                    observed=f"rule matched again after {count} applications — "
                    "promoting to hard failure",
                )
            self._recovery_counts[rule.id] = count + 1
            self._apply_recovery(rule)
            event = RecoveryEvent(
                rule_id=rule.id, at_step_id=step.id,
                description=rule.description, application_count=count + 1,
            )
            self._recoveries.append(event)
            report.status = "recovered"
            self.log.event("recovered", actor="automation", step=step.id,
                           rule=rule.id, application=count + 1)
            if allow_retry:
                return None  # caller retries resolution
            # Re-verify the step checkpoint after recovery.
            if probe_holds(self.page, step.checkpoint, step.wait_timeout_ms):
                return None
            return self._hard_failure(
                None, step,
                expected=step.checkpoint.description + " (after recovery)",
                observed=self._observed_summary(),
            )

        # Bucket c — hard failure (with optional human escalation).
        if self.escalate_on_failure:
            resolution = self._escalate(
                InterventionReason.REPLAY_HARD_FAILURE, step,
                f"unexpected state at step {step.id!r}: expected {expected}; "
                f"observed {observed}",
            )
            if resolution == InterventionResolution.RESUMED:
                # Human repaired the live session — re-verify and continue.
                if probe_holds(self.page, step.checkpoint, step.wait_timeout_ms):
                    report.status = "recovered"
                    self.log.event("resumed_after_intervention",
                                   actor="automation", step=step.id)
                    return None
                observed += " (still unmet after human intervention)"
        return self._hard_failure(None, step, expected=expected, observed=observed)

    def _apply_recovery(self, rule) -> None:
        # Recovery actions pass the same gate as everything else.
        self.gate.check_action(
            "click" if rule.action_kind == RecoveryActionKind.CLICK else "navigate",
            self.page.url,
        )
        if rule.action_kind == RecoveryActionKind.CLICK and rule.action_target:
            handle, _, _ = resolve_target(self.page, rule.action_target)
            handle.click()
        elif rule.action_kind == RecoveryActionKind.RELOAD:
            self.page.reload()
        elif rule.action_kind == RecoveryActionKind.WAIT_RETRY:
            self.page.wait_for_timeout(1000)

    # ------------------------------------------------------------- plumbing

    def _validate_params(self, params: dict[str, str]) -> None:
        for p in self.cap.inputs:
            if p.required and p.name not in params:
                raise ParamError(f"missing required parameter {p.name!r}")
            value = params.get(p.name)
            if value is None:
                continue
            if p.sensitivity == Sensitivity.IDENTIFIER:
                self.log.redactor.register_identifier(value)
            elif p.sensitivity == Sensitivity.SECRET:
                self.log.redactor.register_secret(value)
        unknown = set(params) - {p.name for p in self.cap.inputs}
        if unknown:
            raise ParamError(f"unknown parameter(s): {sorted(unknown)}")

    @staticmethod
    def _substitute(template: str | None, params: dict[str, str]) -> str:
        if template is None:
            return ""
        out = template
        for name, value in params.items():
            out = out.replace("{" + name + "}", value)
        return out

    def _observed_summary(self) -> str:
        try:
            body = self.page.inner_text("body", timeout=1000)
        except Exception:
            body = "<body unreadable>"
        first_line = next(
            (ln.strip() for ln in body.splitlines() if ln.strip()), ""
        )
        return f"url={self.page.url} title={self.page.title()!r} first_visible_line={first_line!r}"

    def _escalate(self, reason: InterventionReason, step: Step, message: str):
        request = build_intervention(
            run_logger=self.log,
            page=self.page,
            reason=reason,
            capability_or_goal=self.cap.capability_id,
            current_step=step.id,
            message=message,
        )
        self._intervention_id = request.id
        request = self.console.handle(request)
        return request.resolution

    def _hard_failure(
        self, started, step: Step, *, expected: str, observed: str
    ) -> ReplayResult:
        screenshot = self.log.save_screenshot(self.page, f"failure_{step.id}")
        dom = self.log.save_dom(self.page, f"failure_{step.id}")
        self.log.event(
            "hard_failure", actor="automation", step=step.id,
            expected=expected, observed=observed,
            screenshot=screenshot, dom_snapshot=dom,
        )
        for r in self._step_reports:
            if r.step_id == step.id:
                r.status = "failed"
        return ReplayResult(
            run_id=self.log.run_id,
            capability_id=self.cap.capability_id,
            capability_version=self.cap.version,
            status=ReplayStatus.HARD_FAILURE,
            started_at=started or utc_now(),
            finished_at=utc_now(),
            failure=FailureDetail(
                step_id=step.id,
                step_description=step.description,
                action=step.action.value,
                expected=expected,
                observed=observed,
                screenshot_path=screenshot or None,
                dom_snapshot_path=dom or None,
            ),
            recoveries=self._recoveries,
            steps=self._step_reports,
            events_log_path=str(self.log.events_path),
            intervention_id=self._intervention_id,
        )

    def _finalize(self, started, result: ReplayResult) -> ReplayResult:
        if result.started_at is None:  # defensive; should not happen
            result.started_at = started
        self.log.save_json("result.json", result.model_dump(mode="json"))
        return result
