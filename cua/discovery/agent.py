"""Discovery agent — the LLM-in-the-loop observe -> decide -> act cycle.

Each turn:

1. **Observe**: structured snapshot of the live page (roles, names, refs,
   visible text) — see cua/browser.py for why this beats raw screenshots on
   legacy surfaces.
2. **Decide**: the Decider (Anthropic tool-calling) returns exactly one
   structured action.
3. **Act**: the action passes the PolicyGate, runs via Playwright, and is
   verified against the model's own declared expectation (``success_text``)
   — an unmet expectation is fed back to the model as a failed observation,
   not papered over.

Every cycle is logged (observation digest, decision + reasoning, action
result) to the run's JSONL file. On ``done``, the executed actions — never
the transcript — are handed to the recorder to build the artifact.

Escalation points: the model calling ``blocked``, a risky action without
operator pre-approval, or hitting max steps. All three raise a structured
intervention and hand the *same live session* to the operator console.
"""

from __future__ import annotations

from dataclasses import dataclass

from cua.browser import BrowserSession, probe_holds, take_snapshot
from cua.discovery.decider import Decider, Decision
from cua.discovery.recorder import ExecutedAction, Recording, build_capability
from cua.discovery.spec import GoalSpec
from cua.escalation.console import OperatorConsole
from cua.escalation.intervention import (
    InterventionReason,
    InterventionResolution,
    build_intervention,
)
from cua.runlog import RunLogger
from cua.safety.policy import PolicyGate, PolicyViolation
from cua.schema import Capability, RiskLevel, StateProbe

EXPECTATION_TIMEOUT_MS = 5000


@dataclass
class DiscoveryOutcome:
    success: bool
    capability: Capability | None
    summary: str
    outputs: dict[str, str]
    turns: int


class DiscoveryAgent:
    def __init__(
        self,
        session: BrowserSession,
        spec: GoalSpec,
        decider: Decider,
        gate: PolicyGate,
        run_logger: RunLogger,
        *,
        approve_risky: bool = False,
        console: OperatorConsole | None = None,
    ):
        self.session = session
        self.page = session.page
        self.spec = spec
        self.decider = decider
        self.gate = gate
        self.log = run_logger
        self.approve_risky = approve_risky
        self.console = console or OperatorConsole(self.page, gate, run_logger)
        self.recording = Recording()
        self.collected_outputs: dict[str, str] = {}

    # ------------------------------------------------------------------ run

    def run(self) -> DiscoveryOutcome:
        self.gate.check_url(self.spec.entry_url)
        self.page.goto(self.spec.entry_url)
        self.log.event("run_started", actor="automation", phase="discovery",
                       goal=self.spec.goal, entry_url=self.spec.entry_url,
                       model=self.decider.model_name)

        snapshot = take_snapshot(self.page)
        messages: list[dict] = [
            {
                "role": "user",
                "content": (
                    "GOAL:\n" + self.spec.goal_with_values()
                    + "\n\nCURRENT PAGE OBSERVATION:\n"
                    + snapshot.to_prompt_text()
                ),
            }
        ]

        for turn in range(1, self.spec.max_steps + 1):
            self.log.event(
                "observe", actor="automation", turn=turn, url=snapshot.url,
                title=snapshot.title, elements=len(snapshot.elements),
            )
            decision = self.decider.decide(messages)
            self.log.event(
                "decide", actor="llm", turn=turn, tool=decision.tool,
                args=decision.args, reasoning=decision.args.get("reason", ""),
            )

            if decision.tool == "done":
                return self._finish(decision, turn)

            if decision.tool == "blocked":
                resolution = self._escalate(
                    InterventionReason.AGENT_BLOCKED,
                    f"turn {turn}",
                    decision.args.get("reason", "agent reported blocked"),
                )
                if resolution == InterventionResolution.ABORTED:
                    return DiscoveryOutcome(
                        False, None, "aborted by operator", {}, turn
                    )
                snapshot = take_snapshot(self.page)
                self._append_exchange(
                    messages, decision,
                    "A human operator intervened on the live session and "
                    "returned control. Continue toward the goal.\n\n"
                    "CURRENT PAGE OBSERVATION:\n" + snapshot.to_prompt_text(),
                )
                continue

            result_text, snapshot = self._execute(decision, turn)
            self._append_exchange(messages, decision, result_text)

        # Max steps exhausted -> escalate rather than silently fail.
        self._escalate(
            InterventionReason.MAX_STEPS_EXCEEDED,
            f"turn {self.spec.max_steps}",
            "discovery did not converge within the step budget",
        )
        return DiscoveryOutcome(
            False, None, "max steps exceeded", self.collected_outputs,
            self.spec.max_steps,
        )

    # ------------------------------------------------------------ execution

    def _execute(self, decision: Decision, turn: int) -> tuple[str, object]:
        args = decision.args
        snapshot = take_snapshot(self.page)
        url_before = self.page.url

        # Interstitial context: is the model currently clearing a known one?
        on_interstitial = any(
            i.text in snapshot.visible_text
            for i in self.spec.known_interstitials
        )

        element = None
        if decision.tool in ("click", "fill", "select", "read"):
            element = snapshot.element(args.get("ref", ""))
            if element is None:
                return (
                    f"ERROR: ref {args.get('ref')!r} does not exist in the "
                    "current observation. Re-read the observation and use a "
                    "valid ref.\n\nCURRENT PAGE OBSERVATION:\n"
                    + snapshot.to_prompt_text(),
                    snapshot,
                )

        try:
            self.gate.check_action(
                decision.tool if decision.tool != "read" else "read",
                args["url"] if decision.tool == "navigate" else url_before,
            )
        except PolicyViolation as exc:
            self.log.event("policy_blocked", actor="policy", turn=turn,
                           tool=decision.tool, detail=str(exc))
            return (
                f"POLICY VIOLATION — action blocked: {exc}. "
                "Choose a different action within the permitted application.",
                snapshot,
            )

        # Risky-action gate: never auto-submit destructive actions blindly.
        risk = self.gate.classify(
            decision.tool, element.name if element else None
        )
        if risk == RiskLevel.RISKY and not self.approve_risky:
            resolution = self._escalate(
                InterventionReason.RISKY_ACTION_CONFIRMATION,
                f"turn {turn}",
                f"the agent wants to perform a risky action: "
                f"{decision.tool} on \"{element.name if element else '?'}\" "
                f"({args.get('reason', '')})",
            )
            if resolution != InterventionResolution.APPROVED:
                self.log.event("risky_denied", actor="human", turn=turn)
                return (
                    "The human operator DENIED the risky action. Do not retry "
                    "it. If the goal cannot be completed without it, call "
                    "blocked.",
                    snapshot,
                )
            self.log.event("risky_approved", actor="human", turn=turn)

        success_text = args.get("success_text")
        try:
            if decision.tool == "navigate":
                self.page.goto(args["url"])
            elif decision.tool == "click":
                self.page.locator(element.css_path).click()
            elif decision.tool == "fill":
                handle = self.page.locator(element.css_path)
                handle.fill(args["value"])
                if handle.input_value() != args["value"]:
                    raise RuntimeError("read-back after fill did not match")
            elif decision.tool == "select":
                self.page.locator(element.css_path).select_option(args["value"])
            elif decision.tool == "read":
                value = self.page.locator(element.css_path).inner_text().strip()
                self.collected_outputs[args["output_name"]] = value
        except Exception as exc:
            self.log.event("act_failed", actor="automation", turn=turn,
                           tool=decision.tool, error=str(exc))
            self.log.save_screenshot(self.page, f"act_failed_t{turn}")
            snapshot = take_snapshot(self.page)
            return (
                f"ACTION FAILED: {exc}. The page may have changed.\n\n"
                "CURRENT PAGE OBSERVATION:\n" + snapshot.to_prompt_text(),
                snapshot,
            )

        verified = True
        if decision.tool in ("navigate", "click") and success_text:
            verified = probe_holds(
                self.page,
                StateProbe(text=success_text, description="model expectation"),
                timeout_ms=EXPECTATION_TIMEOUT_MS,
            )

        action = ExecutedAction(
            tool=decision.tool,
            args=args,
            element=element,
            url_before=url_before,
            url_after=self.page.url,
            success_text=success_text,
            verified=verified,
            was_interstitial_dismissal=on_interstitial,
            read_value=self.collected_outputs.get(args.get("output_name", "")),
            reason=args.get("reason", ""),
        )
        self.recording.add(action)
        self.log.event(
            "act", actor="automation", turn=turn, tool=decision.tool,
            element=(element.name if element else None),
            url_before=url_before, url_after=self.page.url,
            expectation=success_text, expectation_held=verified,
            interstitial_context=on_interstitial,
        )

        snapshot = take_snapshot(self.page)
        status = (
            "OK — expectation held."
            if verified
            else f"WARNING — expected text {success_text!r} is NOT visible. "
            "Re-check the state before proceeding."
        )
        if decision.tool == "read":
            status = f"OK — recorded output {args['output_name']!r}."
        return (
            f"{status}\n\nCURRENT PAGE OBSERVATION:\n"
            + snapshot.to_prompt_text(),
            snapshot,
        )

    # ------------------------------------------------------------- plumbing

    def _append_exchange(
        self, messages: list[dict], decision: Decision, result_text: str
    ) -> None:
        if decision.raw_assistant_content is not None:
            messages.append(
                {"role": "assistant", "content": decision.raw_assistant_content}
            )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": decision.tool_use_id,
                            "content": result_text,
                        }
                    ],
                }
            )
        else:  # scripted decider in tests — plain-text exchange
            messages.append(
                {"role": "assistant", "content": f"[{decision.tool}] {decision.args}"}
            )
            messages.append({"role": "user", "content": result_text})

    def _escalate(
        self, reason: InterventionReason, step: str, message: str
    ) -> InterventionResolution:
        request = build_intervention(
            run_logger=self.log,
            page=self.page,
            reason=reason,
            capability_or_goal=self.spec.goal,
            current_step=step,
            message=message,
        )
        request = self.console.handle(request)
        return request.resolution or InterventionResolution.ABORTED

    def _finish(self, decision: Decision, turn: int) -> DiscoveryOutcome:
        missing = [
            o.name for o in self.spec.outputs
            if o.name not in self.collected_outputs
        ]
        if missing:
            self.log.event("done_rejected", actor="automation",
                           missing_outputs=missing)
            # The contract is unmet — treat as not converged.
            return DiscoveryOutcome(
                False, None,
                f"agent called done but outputs missing: {missing}",
                self.collected_outputs, turn,
            )
        capability = build_capability(
            self.spec,
            self.recording,
            model_name=self.decider.model_name,
            run_id=self.log.run_id,
            gate=self.gate,
        )
        self.log.event(
            "run_succeeded", actor="automation", turns=turn,
            summary=decision.args.get("summary", ""),
            steps_recorded=len(capability.steps),
        )
        return DiscoveryOutcome(
            True, capability, decision.args.get("summary", ""),
            self.collected_outputs, turn,
        )
