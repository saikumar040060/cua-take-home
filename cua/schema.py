"""Capability artifact schema — the contract between discovery and replay.

This is the central data model of the system. A ``Capability`` is what the
LLM produces once (at discovery time) and what an AI agent invokes forever
after (at replay time, with **no model in the loop**). It is designed to be
read three ways:

1. **By the replay engine** — every step carries enough targeting, waiting,
   and verification information to execute deterministically.
2. **By a calling agent** — ``name``, ``description``, ``inputs`` and
   ``outputs`` form a function signature: what it does, what it needs,
   what it returns, and which *business outcomes* (not errors) it can
   legitimately report.
3. **By a human reviewer** — every element target and checkpoint carries a
   human-readable description, so the artifact can be audited before it is
   approved for unattended replay.

Versioning: ``schema_version`` versions this *format* (migrations),
``version`` versions the *recorded flow itself* (bumped on re-record).

Design choices worth defending
------------------------------
**Locator chains, semantic-first.** Legacy surfaces have no test IDs and
non-semantic class names, so every ``ElementTarget`` is an *ordered* list of
independent locator strategies. The order encodes a robustness argument:

  * ``role`` (ARIA role + accessible name) survives markup refactors and is
    the representation closest to what a human operator perceives — it also
    exists on desktop apps via OS accessibility APIs, which matters for the
    heterogeneity story. It is primary whenever the control has a stable
    accessible name.
  * ``label``/``text`` rely on visible wording — stable in enterprise apps
    where copy changes require vendor releases, but sensitive to i18n and
    re-wording, so they come second.
  * ``css``/``dom_path`` are exact but brittle (nth-child positions shatter
    when a row is added); they are recorded as a last resort *and* as a
    cross-check: if only the dom_path matches, replay still proceeds but the
    resolution is logged as degraded — an early-warning signal of UI drift.

**Values are templates, never literals.** A discovery run types a concrete
member number, but the artifact stores ``"{member_id}"``. This is what makes
the artifact a *parameterized capability* instead of a macro replay, and it
doubles as a privacy property: run-time PII never persists inside artifacts.

**Checkpoints are mandatory.** A click that "worked" proves nothing; each
step asserts the state it was supposed to produce. On checkpoint failure the
replay engine consults, in order: declared business outcomes, then recovery
rules, then hard failure — the three-bucket contract lives here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Element targeting
# ---------------------------------------------------------------------------

class LocatorStrategy(str, Enum):
    """How to find a control, ordered roughly by robustness on legacy UIs.

    ``ROLE``     ARIA role + accessible name (Playwright ``get_by_role``).
    ``LABEL``    Association with a visible label (``get_by_label``).
    ``TEXT``     Visible text content (``get_by_text``); for links/buttons.
    ``CSS``      CSS selector, e.g. ``input[name="mno"]`` — attribute-based
                 CSS is moderately stable; class-based CSS on legacy apps
                 (``.c1 > td``) is not, and is avoided by the recorder.
    ``DOM_PATH`` Absolute structural path — brittle, last resort + drift
                 canary.
    """

    ROLE = "role"
    LABEL = "label"
    TEXT = "text"
    CSS = "css"
    DOM_PATH = "dom_path"


class Locator(BaseModel):
    """One concrete way to resolve a control on the page."""

    strategy: LocatorStrategy
    value: str = Field(description="Strategy-specific selector value. For ROLE this is the role name (e.g. 'button').")
    name: str | None = Field(
        default=None,
        description="Accessible name (ROLE strategy only), e.g. 'Search'.",
    )
    note: str | None = Field(
        default=None,
        description="Why this locator was chosen / expected robustness.",
    )


class ElementTarget(BaseModel):
    """An ordered fallback chain of locators plus a human description.

    Replay tries each locator in order; the first that resolves to exactly
    one visible element wins. Resolution below index 0 is logged as a
    degraded match (drift signal). Ambiguous (>1 match) locators are
    skipped rather than guessed at — determinism beats cleverness.
    """

    description: str = Field(description="Human-readable identity of the control, e.g. \"the 'Search' button on the Member Lookup form\".")
    locators: list[Locator] = Field(min_length=1)


# ---------------------------------------------------------------------------
# State probes (shared by checkpoints, outcome detectors, recovery rules)
# ---------------------------------------------------------------------------

class StateProbe(BaseModel):
    """A verifiable assertion about the current page state.

    All specified fields must hold simultaneously. Kept deliberately simple
    (URL pattern + visible text + optional element) because these are the
    signals that exist on *every* surface — including a desktop app observed
    through an accessibility API, where there is no DOM to query.
    """

    url_pattern: str | None = Field(
        default=None, description="Regex the current URL must match."
    )
    text: str | None = Field(
        default=None, description="Text that must be visible on the page."
    )
    target: ElementTarget | None = Field(
        default=None, description="An element that must be present/visible."
    )
    description: str = Field(description="What this probe verifies, in words.")


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    PRESS = "press"
    READ = "read"


class RiskLevel(str, Enum):
    """Safety classification, enforced by the policy layer at execution time.

    ``SAFE``  read-only or trivially reversible (search, view, navigate).
    ``RISKY`` creates/mutates state in the target system (e.g. submitting
              the sub-account creation form). Risky steps never execute
              unattended: replay requires an explicit caller confirmation
              (``confirm_risky=True``) or escalates to a human.
    """

    SAFE = "safe"
    RISKY = "risky"


class Step(BaseModel):
    """One deterministic action plus the checkpoint that proves it worked."""

    id: str = Field(description="Stable step id, e.g. 's3_click_open_subacct'.")
    description: str
    action: ActionType
    target: ElementTarget | None = Field(
        default=None, description="Required for click/fill/select/press/read."
    )
    value_template: str | None = Field(
        default=None,
        description=(
            "Value for fill/select/press. May reference declared inputs as "
            "'{param_name}' — concrete discovery-time values are never stored."
        ),
    )
    url_template: str | None = Field(
        default=None, description="For navigate; may reference '{param_name}'."
    )
    output_name: str | None = Field(
        default=None,
        description="For read: which declared output this step populates.",
    )
    risk: RiskLevel = RiskLevel.SAFE
    checkpoint: StateProbe = Field(
        description="Post-condition that must hold after the action."
    )
    wait_timeout_ms: int = Field(
        default=5000,
        description=(
            "Budget for the checkpoint to become true. Waiting is always "
            "condition-based (wait for the asserted state), never sleep()."
        ),
    )


# ---------------------------------------------------------------------------
# Inputs / outputs — the capability's function signature
# ---------------------------------------------------------------------------

class ParamKind(str, Enum):
    STRING = "string"
    NUMBER = "number"


class Sensitivity(str, Enum):
    """Drives redaction at every serialization boundary (logs, evidence).

    ``PUBLIC``       safe to log verbatim (e.g. a product type).
    ``IDENTIFIER``   business identifier: logged masked (e.g. '100**').
    ``SECRET``       never logged or persisted in any form.
    """

    PUBLIC = "public"
    IDENTIFIER = "identifier"
    SECRET = "secret"


class InputParam(BaseModel):
    name: str
    kind: ParamKind = ParamKind.STRING
    description: str
    required: bool = True
    example: str | None = None
    sensitivity: Sensitivity = Sensitivity.PUBLIC


class OutputField(BaseModel):
    name: str
    kind: ParamKind = ParamKind.STRING
    description: str
    source_step_id: str = Field(
        description="The read-step that extracts this value."
    )
    sensitivity: Sensitivity = Sensitivity.PUBLIC


# ---------------------------------------------------------------------------
# Business outcomes & recovery rules — the error taxonomy, recorded
# ---------------------------------------------------------------------------

class BusinessOutcome(BaseModel):
    """A legitimate negative result the *caller* needs to know about.

    'No such member' is an answer, not a crash. Recording these in the
    artifact is what lets replay distinguish 'the system worked and said no'
    from 'the system is in a state I don't recognize'.
    """

    code: str = Field(description="Machine code, e.g. 'member_not_found'.")
    description: str
    detector: StateProbe


class RecoveryActionKind(str, Enum):
    CLICK = "click"
    RELOAD = "reload"
    WAIT_RETRY = "wait_retry"


class RecoveryRule(BaseModel):
    """A *known* interruption and the deterministic move that clears it.

    Example: the 'session expiring' interstitial → click Continue. Bounded
    by ``max_applications`` so a recovery loop can never spin forever; when
    the budget is exhausted the condition is promoted to a hard failure.
    """

    id: str
    description: str
    detector: StateProbe
    action_kind: RecoveryActionKind
    action_target: ElementTarget | None = None
    max_applications: int = 2


# ---------------------------------------------------------------------------
# The capability itself
# ---------------------------------------------------------------------------

class DiscoveryProvenance(BaseModel):
    """Where this artifact came from — audit trail for reviewers."""

    goal: str = Field(description="The natural-language goal given to the discovery agent.")
    model: str = Field(description="LLM used at discovery time.")
    run_id: str
    discovered_at: datetime


class Capability(BaseModel):
    """A recorded, parameterized, replayable UI flow — an agent-invocable tool.

    Serialized as JSON (``model_dump_json``); loaded with
    ``Capability.model_validate_json`` which enforces the full contract
    including cross-references (outputs → read steps, templates → declared
    inputs).
    """

    schema_version: str = Field(default=SCHEMA_VERSION)
    capability_id: str = Field(description="Stable slug, e.g. 'open_sub_account'.")
    version: int = Field(
        default=1,
        description="Version of this recorded flow; bumped on re-record.",
    )
    name: str
    description: str = Field(
        description=(
            "What the capability does, for both a reviewer and a calling "
            "agent — including what success returns and which business "
            "outcomes are possible."
        )
    )
    provenance: DiscoveryProvenance
    entry_url_template: str = Field(
        description="Starting URL; may reference '{param_name}'."
    )
    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    business_outcomes: list[BusinessOutcome] = Field(default_factory=list)
    recovery_rules: list[RecoveryRule] = Field(default_factory=list)

    @field_validator("steps")
    @classmethod
    def _step_ids_unique(cls, v: list[Step]) -> list[Step]:
        ids = [s.id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        return v

    def model_post_init(self, __context) -> None:  # noqa: D105
        step_ids = {s.id for s in self.steps}
        for out in self.outputs:
            if out.source_step_id not in step_ids:
                raise ValueError(
                    f"output '{out.name}' references unknown step "
                    f"'{out.source_step_id}'"
                )
        declared = {p.name for p in self.inputs}
        for s in self.steps:
            for tmpl in (s.value_template, s.url_template):
                if tmpl:
                    for ref in _template_refs(tmpl):
                        if ref not in declared:
                            raise ValueError(
                                f"step '{s.id}' references undeclared input "
                                f"'{{{ref}}}'"
                            )

    # Convenience -----------------------------------------------------------

    def input_by_name(self, name: str) -> InputParam | None:
        return next((p for p in self.inputs if p.name == name), None)

    def signature(self) -> str:
        """One-line function signature for an agent-facing catalog."""
        params = ", ".join(f"{p.name}: {p.kind.value}" for p in self.inputs)
        outs = ", ".join(f"{o.name}: {o.kind.value}" for o in self.outputs)
        return f"{self.capability_id}({params}) -> {{{outs}}}"


def _template_refs(template: str) -> list[str]:
    """Extract '{name}' references from a value/url template."""
    import re

    return re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", template)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
