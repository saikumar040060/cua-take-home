"""Replay result contract — what a calling agent gets back.

Every replay terminates in exactly one of three buckets:

* ``SUCCESS``          — flow completed, all checkpoints held; ``outputs``
                         carries the declared extracted values.
* ``BUSINESS_OUTCOME`` — the target system worked correctly and returned a
                         legitimate negative answer (e.g. member not found,
                         validation rejected). This is *not* a failure of the
                         automation; the caller needs the outcome code.
* ``HARD_FAILURE``     — the flow reached a state the artifact does not
                         describe. Replay stops and returns a debug bundle:
                         which step, what was expected, what was observed,
                         plus screenshot/DOM snapshot paths.

Recoverable conditions (known interstitials, transient slowness) never
appear as a terminal status — by definition they were recovered from and
the run continued. They are reported in ``recoveries`` for observability.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    HARD_FAILURE = "hard_failure"


class RecoveryEvent(BaseModel):
    """A recoverable condition that was detected and handled mid-run."""

    rule_id: str
    at_step_id: str
    description: str
    application_count: int


class BusinessOutcomeResult(BaseModel):
    code: str
    description: str
    detected_at_step_id: str


class FailureDetail(BaseModel):
    """Everything needed to debug a hard failure without re-running it."""

    step_id: str
    step_description: str
    action: str
    expected: str = Field(description="The checkpoint/probe that did not hold.")
    observed: str = Field(description="Observed URL/title/visible-state summary.")
    screenshot_path: str | None = None
    dom_snapshot_path: str | None = None


class StepReport(BaseModel):
    """Per-step execution trace (also mirrored to the JSONL event log)."""

    step_id: str
    status: str  # ok | degraded_locator | recovered | failed | skipped
    locator_used: str | None = None
    locator_rank: int | None = Field(
        default=None,
        description="0 = primary locator; >0 means a fallback resolved (drift signal).",
    )
    duration_ms: int


class ReplayResult(BaseModel):
    run_id: str
    capability_id: str
    capability_version: int
    status: ReplayStatus
    started_at: datetime
    finished_at: datetime
    outputs: dict[str, str] = Field(default_factory=dict)
    business_outcome: BusinessOutcomeResult | None = None
    failure: FailureDetail | None = None
    recoveries: list[RecoveryEvent] = Field(default_factory=list)
    steps: list[StepReport] = Field(default_factory=list)
    events_log_path: str | None = None
    intervention_id: str | None = Field(
        default=None,
        description="Set when the run raised a human intervention request.",
    )
