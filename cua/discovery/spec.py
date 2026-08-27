"""Goal specification — the operator-declared contract for a discovery run.

The LLM discovers *how* to do the task; it does not get to define *what*
the task's contract is. The inputs, outputs, business outcomes, and known
interstitials of a capability are part of its interface, declared up front
by whoever commissions it (in production: the team onboarding the app).
This split is deliberate:

* inputs must be declared so the recorder can parameterize the artifact
  (concrete values -> ``{member_id}`` templates) and so sensitivity/redaction
  is known *before* the first byte is logged;
* business outcomes are contract, not observation — a successful discovery
  run never sees "member not found", yet replay must recognize it;
* known interstitials become recovery rules even when the discovery run
  happened not to trigger them.

A GoalSpec is a small JSON file; see ``specs/`` for examples.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from cua.schema import ParamKind, Sensitivity


class SpecInput(BaseModel):
    name: str
    value: str = Field(description="Concrete value used for the discovery run.")
    description: str
    kind: ParamKind = ParamKind.STRING
    sensitivity: Sensitivity = Sensitivity.PUBLIC


class SpecOutput(BaseModel):
    name: str
    description: str
    kind: ParamKind = ParamKind.STRING
    sensitivity: Sensitivity = Sensitivity.PUBLIC


class SpecBusinessOutcome(BaseModel):
    code: str
    text: str = Field(description="Visible text that identifies this outcome.")
    description: str
    http_status: int | None = Field(
        default=None,
        description=(
            "Optional HTTP status this outcome also/instead signals via "
            "(e.g. 404 for member-not-found), for targets that use status "
            "codes rather than distinct copy alone."
        ),
    )


class SpecInterstitial(BaseModel):
    id: str
    text: str = Field(description="Visible text identifying the interstitial.")
    dismiss_role: str = "button"
    dismiss_name: str = Field(description="Accessible name of the dismiss control.")
    http_status: int | None = Field(
        default=None,
        description="Optional HTTP status this interstitial also signals via.",
    )
    description: str
    max_applications: int = 2


class GoalSpec(BaseModel):
    capability_id: str
    name: str
    goal: str = Field(description="Natural-language goal for the LLM.")
    entry_url: str
    inputs: list[SpecInput] = Field(default_factory=list)
    outputs: list[SpecOutput] = Field(default_factory=list)
    business_outcomes: list[SpecBusinessOutcome] = Field(default_factory=list)
    known_interstitials: list[SpecInterstitial] = Field(default_factory=list)
    max_steps: int = 20

    @classmethod
    def load(cls, path: str | Path) -> "GoalSpec":
        return cls.model_validate(json.loads(Path(path).read_text()))

    def goal_with_values(self) -> str:
        lines = [self.goal, "", "Concrete values for this run:"]
        for p in self.inputs:
            lines.append(f"  {p.name} = {p.value}")
        if self.outputs:
            lines.append("")
            lines.append(
                "Before finishing you MUST use the read tool to extract: "
                + ", ".join(f"{o.name} ({o.description})" for o in self.outputs)
            )
        return "\n".join(lines)
