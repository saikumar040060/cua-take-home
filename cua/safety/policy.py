"""Safety policy — allowlist + action risk model, enforced at one choke point.

Both the discovery agent and the replay engine execute browser actions
through a single ``PolicyGate.check(...)`` call before anything touches the
page. There is deliberately no second code path: if the gate doesn't pass
it, it doesn't happen — whether the actor is an LLM, a replayed artifact,
or (during handoff) a human-issued console command.

The policy is configuration, not code: ``policy.json`` at the repo root
(overridable with ``--policy``), so a reviewer can see and change exactly
what the agent is permitted to do without reading the engine.

Risk model: an action is RISKY if it is a click/press on a control whose
accessible name matches one of ``risky_name_patterns`` (e.g. Create/Submit/
Delete/Transfer). Risky actions are never blocked outright — that would
make the system useless — but they never run unattended either:

* discovery: the agent's risky action pauses for confirmation (auto-approved
  only with an explicit ``--approve-risky`` operator flag, which is itself
  logged);
* replay: the caller must pass ``confirm_risky=True`` (the calling agent's
  explicit sign-off for this invocation), otherwise the run escalates to a
  human instead of submitting.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from cua.schema import RiskLevel


class PolicyViolation(Exception):
    """Raised when an action falls outside the allowlist. Always fatal."""


class Policy(BaseModel):
    allowed_origins: list[str] = Field(
        description="Exact scheme://host[:port] origins the agent may touch."
    )
    allowed_path_prefixes: list[str] = Field(
        default=["/"],
        description="URL path prefixes permitted within allowed origins.",
    )
    blocked_path_prefixes: list[str] = Field(
        default_factory=list,
        description="Explicit deny-list evaluated before the allow-list "
        "(e.g. an admin console living on the same origin).",
    )
    allowed_actions: list[str] = Field(
        default=["navigate", "click", "fill", "select", "press", "read"],
    )
    risky_name_patterns: list[str] = Field(
        default=[
            r"create", r"submit", r"delete", r"remove", r"transfer",
            r"post", r"approve", r"close account",
        ],
        description="Case-insensitive regexes over a control's accessible "
        "name; a click/press match classifies the action as RISKY.",
    )

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        return cls.model_validate(json.loads(Path(path).read_text()))


class PolicyGate:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._risky = [
            re.compile(p, re.IGNORECASE) for p in policy.risky_name_patterns
        ]

    def check_url(self, url: str) -> None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.policy.allowed_origins:
            raise PolicyViolation(
                f"origin '{origin}' is not in the allowlist "
                f"{self.policy.allowed_origins}"
            )
        path = parsed.path or "/"
        for prefix in self.policy.blocked_path_prefixes:
            if path.startswith(prefix):
                raise PolicyViolation(f"path '{path}' is explicitly blocked")
        if not any(
            path.startswith(p) for p in self.policy.allowed_path_prefixes
        ):
            raise PolicyViolation(f"path '{path}' matches no allowed prefix")

    def check_action(self, action: str, current_url: str) -> None:
        """The pre-action gate: action type + the URL the action runs on."""
        if action not in self.policy.allowed_actions:
            raise PolicyViolation(f"action type '{action}' is not permitted")
        self.check_url(current_url)

    def classify(self, action: str, accessible_name: str | None) -> RiskLevel:
        if action in ("click", "press") and accessible_name:
            if any(rx.search(accessible_name) for rx in self._risky):
                return RiskLevel.RISKY
        return RiskLevel.SAFE


DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "policy.json"
