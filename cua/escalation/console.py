"""Minimal operator console — the mocked-thin surface over a real handoff.

When an intervention is raised, the automation pauses and this console takes
over **the same live Playwright page**. The human can inspect state and act
(their commands run through the same PolicyGate as automation — a human
operator does not get to take the session off-allowlist), then hand control
back with ``resume``/``approve``/``deny``/``abort``.

The console reads commands from a file object (stdin by default), which is
what keeps the mechanism real *and* scriptable: interactive use for a human
at a terminal, a piped script for reproducible evidence runs. A full
operator UI (queue, co-browsing view) is explicitly out of scope per the
brief; this is the seam it would plug into — anything that can deliver
commands and read output can drive a handoff.

Every human command is appended to the intervention's ``human_actions`` and
mirrored to the run's JSONL log with ``actor="human"``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from cua.browser import take_snapshot
from cua.escalation.intervention import (
    HumanAction,
    InterventionRequest,
    InterventionResolution,
)
from cua.runlog import RunLogger
from cua.safety.policy import PolicyGate, PolicyViolation

HELP = """Operator console — you hold control of the live session.
Commands:
  status                 show intervention summary and current URL
  look                   show interactive elements (refs) and page text
  screenshot             capture a screenshot into the run directory
  goto <url>             navigate (policy-checked)
  click <ref>            click an element by ref from 'look'
  fill <ref> <value...>  fill a field
  select <ref> <value>   choose a dropdown option
  approve                approve the pending risky action and return control
  deny                   deny the pending risky action and return control
  resume                 return control to automation (state fixed)
  abort                  stop the run
"""


class OperatorConsole:
    def __init__(
        self,
        page,
        gate: PolicyGate,
        run_logger: RunLogger,
        input_stream=None,
        output_stream=None,
    ):
        self.page = page
        self.gate = gate
        self.log = run_logger
        self.stdin = input_stream or sys.stdin
        self.stdout = output_stream or sys.stdout

    def _say(self, text: str) -> None:
        print(text, file=self.stdout, flush=True)

    def handle(self, request: InterventionRequest) -> InterventionRequest:
        """Run the console loop until the human returns control."""
        self._say("\n" + "=" * 62)
        self._say("HUMAN INTERVENTION REQUIRED")
        self._say(f"  reason : {request.reason.value}")
        self._say(f"  goal   : {request.capability_or_goal}")
        self._say(f"  step   : {request.current_step}")
        self._say(f"  where  : {request.current_url}")
        self._say(f"  why    : {request.message}")
        if request.screenshot_path:
            self._say(f"  shot   : {request.screenshot_path}")
        self._say("=" * 62)
        self._say(HELP)
        self.log.event("handoff_started", actor="system", holder="human",
                       intervention_id=request.id)

        snapshot = None
        while True:
            self._say("operator> ")
            line = self.stdin.readline()
            if not line:  # EOF -> treat as abort (never hang unattended)
                line = "abort"
            line = line.strip()
            if not line:
                continue
            cmd, _, rest = line.partition(" ")
            self._record(request, cmd, rest)
            try:
                if cmd == "status":
                    self._say(f"reason={request.reason.value} url={self.page.url}")
                elif cmd == "look":
                    snapshot = take_snapshot(self.page)
                    self._say(snapshot.to_prompt_text()[:3000])
                elif cmd == "screenshot":
                    path = self.log.save_screenshot(self.page, "operator")
                    self._say(f"saved {path}")
                elif cmd == "goto":
                    self.gate.check_action("navigate", rest)
                    self.page.goto(rest)
                    self._say(f"now at {self.page.url}")
                elif cmd in ("click", "fill", "select"):
                    snapshot = snapshot or take_snapshot(self.page)
                    ref, _, value = rest.partition(" ")
                    el = snapshot.element(ref)
                    if el is None:
                        self._say(f"unknown ref {ref!r} — run 'look' first")
                        continue
                    self.gate.check_action(
                        "click" if cmd == "click" else cmd, self.page.url
                    )
                    handle = self.page.locator(el.css_path)
                    if cmd == "click":
                        handle.click()
                    elif cmd == "fill":
                        handle.fill(value)
                    else:
                        handle.select_option(value)
                    snapshot = None  # page likely changed
                    self._say(f"ok — now at {self.page.url}")
                elif cmd in ("approve", "deny", "resume", "abort"):
                    request.resolution = {
                        "approve": InterventionResolution.APPROVED,
                        "deny": InterventionResolution.DENIED,
                        "resume": InterventionResolution.RESUMED,
                        "abort": InterventionResolution.ABORTED,
                    }[cmd]
                    request.resolved_at = datetime.now(timezone.utc)
                    self.log.event(
                        "handoff_ended", actor="human",
                        resolution=request.resolution.value,
                        intervention_id=request.id,
                    )
                    self.log.save_json(f"{request.id}.json",
                                       request.model_dump(mode="json"))
                    self._say(f"control returned to automation ({cmd})")
                    return request
                elif cmd == "help":
                    self._say(HELP)
                else:
                    self._say(f"unknown command {cmd!r} — 'help' for commands")
            except PolicyViolation as exc:
                self._say(f"POLICY BLOCKED: {exc}")
            except Exception as exc:  # console must never crash the run
                self._say(f"error: {exc}")

    def _record(self, request: InterventionRequest, cmd: str, rest: str) -> None:
        action = HumanAction(
            ts=datetime.now(timezone.utc), command=cmd, detail=rest
        )
        request.human_actions.append(action)
        self.log.event("human_action", actor="human", command=cmd, detail=rest)
