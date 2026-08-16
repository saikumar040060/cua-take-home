"""Human escalation — intervention requests and the control-transfer model.

The core seam: automation (discovery agent or replay engine) and the human
operator share **one live browser session**. Control transfer is explicit
and single-holder:

    AUTOMATION -> (intervention raised, run paused) -> HUMAN
    HUMAN      -> (resume/approve/deny/abort)       -> AUTOMATION

While the human holds control, automation issues no actions; while
automation holds control, the console accepts no commands. Who holds
control is recorded on every event, so the evidence trail shows exactly
which actor did what.

An ``InterventionRequest`` is a structured, serialized object (written to
the run directory) carrying everything an operator needs to act: the
capability/goal, the current step, why the run stopped, a screenshot, and
the tail of the event log. The bundled operator surface is a minimal CLI
console (see console.py) — deliberately mocked-thin per the brief; the
mechanism (pause / cede / act / resume on the same session) is real.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from cua.runlog import RunLogger
from cua.schema import utc_now


class InterventionReason(str, Enum):
    RISKY_ACTION_CONFIRMATION = "risky_action_confirmation"
    AGENT_BLOCKED = "agent_blocked"
    REPLAY_HARD_FAILURE = "replay_hard_failure"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"


class HumanAction(BaseModel):
    """One command the human issued while holding control."""

    ts: datetime
    command: str
    detail: str = ""


class InterventionResolution(str, Enum):
    APPROVED = "approved"    # risky action may proceed
    DENIED = "denied"        # risky action must not proceed
    RESUMED = "resumed"      # human fixed state; automation continues
    ABORTED = "aborted"      # human stopped the run


class InterventionRequest(BaseModel):
    id: str
    run_id: str
    capability_or_goal: str
    reason: InterventionReason
    message: str = Field(description="Human-readable explanation of why the run stopped.")
    current_step: str = Field(description="Step id / turn the run was on.")
    current_url: str
    screenshot_path: str | None = None
    log_excerpt: list[str] = Field(
        default_factory=list, description="Tail of the structured event log."
    )
    created_at: datetime = Field(default_factory=utc_now)
    resolution: InterventionResolution | None = None
    human_actions: list[HumanAction] = Field(default_factory=list)
    resolved_at: datetime | None = None


def build_intervention(
    *,
    run_logger: RunLogger,
    page,
    reason: InterventionReason,
    capability_or_goal: str,
    current_step: str,
    message: str,
) -> InterventionRequest:
    """Assemble the structured request, capturing screenshot + log tail."""
    screenshot = run_logger.save_screenshot(page, f"intervention_{reason.value}")
    excerpt: list[str] = []
    try:
        lines = run_logger.events_path.read_text().splitlines()
        excerpt = lines[-10:]
    except FileNotFoundError:
        pass
    req = InterventionRequest(
        id=f"iv-{run_logger.run_id}-{reason.value}",
        run_id=run_logger.run_id,
        capability_or_goal=capability_or_goal,
        reason=reason,
        message=message,
        current_step=current_step,
        current_url=page.url,
        screenshot_path=screenshot or None,
        log_excerpt=excerpt,
    )
    run_logger.event(
        "intervention_raised",
        actor="automation",
        intervention_id=req.id,
        reason=reason.value,
        message=message,
        step=current_step,
        screenshot=screenshot,
    )
    run_logger.save_json(f"{req.id}.json", req.model_dump(mode="json"))
    return req
