"""Capability API + chatbot + dashboard for MERIDIAN CORE.

Adapter/extension layer only. This module does not reimplement discovery,
replay, the artifact schema, the safety gate, redaction, or escalation --
it imports and drives the existing `cua` package exactly as the CLI does
(`cua/cli.py`), just from HTTP instead of argv. Everything that decides
"is this a business outcome / recoverable condition / hard failure",
"is this action within the allowlist", "which locator resolves" etc.
still runs entirely inside the original, unmodified `cua` engine.

Three thin surfaces on top of that unmodified core:

  * Capability API   -- POST /api/capabilities/<id>/invoke runs the real
                         ReplayEngine (no LLM) and returns a structured
                         result. GET /api/capabilities lists the catalog.
  * Chatbot          -- POST /api/chat takes one plain-language sentence,
                         makes exactly ONE LLM call to pick which
                         capability + typed args to invoke (tool-calling
                         over the same capability catalog), then calls the
                         capability API above. The LLM never decides what
                         happens in the browser -- it only routes intent.
                         Execution underneath is 100% deterministic replay.
  * Dashboard        -- GET / renders the catalog, run history, and the
                         evidence the engine already captures (steps,
                         screenshots, DOM snapshots, logs) -- nothing new
                         is recorded here, this just displays what
                         RunLogger already writes to `runs/`.

Run:  python -m meridian_service.app   (from the cua-take-home repo root)
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_DIR / ".env")

from flask import Flask, jsonify, render_template, request  # noqa: E402

from cua.browser import BrowserSession  # noqa: E402
from cua.escalation.console import OperatorConsole  # noqa: E402
from cua.replay.engine import ParamError, ReplayEngine  # noqa: E402
from cua.runlog import RunLogger, new_run_id  # noqa: E402
from cua.safety.policy import Policy, PolicyGate  # noqa: E402
from cua.safety.redact import Redactor  # noqa: E402
from cua.schema import Capability  # noqa: E402

ARTIFACTS_DIR = PROJECT_DIR / "artifacts" / "meridian_core"
RUNS_DIR = PROJECT_DIR / "runs"
POLICY_PATH = PROJECT_DIR / "policy_meridian.json"

app = Flask(__name__)


def new_gate() -> PolicyGate:
    return PolicyGate(Policy.load(POLICY_PATH))


# --------------------------------------------------------------------- #
# Console I/O stand-ins -- same pattern as cua-dashboard's bridge: these
# implement just readline()/write() so the *unmodified* OperatorConsole
# can be driven over HTTP instead of a terminal. The command handling
# itself (look/click/fill/select/approve/deny/resume/abort, each
# policy-gated) is entirely the original console's code.
# --------------------------------------------------------------------- #


class QueueInputStream:
    def __init__(self) -> None:
        self._q: "queue.Queue[str]" = queue.Queue()

    def push(self, line: str) -> None:
        self._q.put(line if line.endswith("\n") else line + "\n")

    def readline(self) -> str:
        return self._q.get()


class BroadcastOutputStream:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        with self._lock:
            self.lines.append(s)
        return len(s)

    def flush(self) -> None:
        pass

    def tail(self, since: int) -> tuple[list[str], int]:
        with self._lock:
            return list(self.lines[since:]), len(self.lines)


@dataclass
class RunState:
    run_id: str
    capability_id: str
    status: str = "running"  # running | awaiting_human | done | error
    console_in: QueueInputStream = field(default_factory=QueueInputStream)
    console_out: BroadcastOutputStream = field(default_factory=BroadcastOutputStream)
    intervention: dict | None = None
    result: dict | None = None
    error: str | None = None
    logger: RunLogger | None = None
    started_at: float = field(default_factory=time.time)


RUNS: dict[str, RunState] = {}
RUNS_LOCK = threading.Lock()


class TrackedConsole(OperatorConsole):
    """Unmodified OperatorConsole; only tracks status + redacts the
    InterventionRequest before it's exposed via the API (the same gap
    fixed in the dashboard: `build_intervention()` puts the live,
    unredacted page URL on the in-memory object -- only the persisted
    copy is redacted by RunLogger.save_json(). Same Redactor, same
    scrub_obj() call the logger already uses.)"""

    def __init__(self, *args: Any, run_state: RunState, redactor: Redactor, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._run_state = run_state
        self._redactor = redactor

    def handle(self, req):
        self._run_state.status = "awaiting_human"
        self._run_state.intervention = self._redactor.scrub_obj(req.model_dump(mode="json"))
        try:
            result = super().handle(req)
        finally:
            self._run_state.status = "running"
            self._run_state.intervention = None
        return result


# --------------------------------------------------------------------- #
# Capability catalog
# --------------------------------------------------------------------- #


def list_capabilities() -> list[Capability]:
    caps = []
    if ARTIFACTS_DIR.exists():
        for p in sorted(ARTIFACTS_DIR.glob("*.json")):
            try:
                caps.append(Capability.model_validate_json(p.read_text()))
            except Exception:
                continue
    return caps


def get_capability(capability_id: str) -> Capability | None:
    for cap in list_capabilities():
        if cap.capability_id == capability_id:
            return cap
    return None


# --------------------------------------------------------------------- #
# Capability API -- runs the real, unmodified ReplayEngine. No LLM.
# --------------------------------------------------------------------- #


def start_replay(capability: Capability, params: dict[str, str], *, confirm_risky: bool, headed: bool) -> RunState:
    run_id = new_run_id("replay")
    state = RunState(run_id=run_id, capability_id=capability.capability_id)
    with RUNS_LOCK:
        RUNS[run_id] = state

    def worker() -> None:
        redactor = Redactor()
        logger = RunLogger(RUNS_DIR, run_id, redactor)
        state.logger = logger
        try:
            with BrowserSession(headed=headed) as session:
                gate = new_gate()
                console = TrackedConsole(
                    session.page, gate, logger, run_state=state, redactor=redactor,
                    input_stream=state.console_in, output_stream=state.console_out,
                )
                engine = ReplayEngine(
                    session, capability, gate, logger,
                    confirm_risky=confirm_risky,
                    escalate_on_failure=True,
                    console=console,
                )
                result = engine.run(params)
            # Same redactor the engine registered identifier/secret inputs
            # against -- the API response gets the same treatment as the
            # persisted result.json, not the raw in-memory object.
            state.result = redactor.scrub_obj(json.loads(result.model_dump_json()))
            state.status = "done"
        except ParamError as exc:
            state.error = str(exc)
            state.status = "error"
        except Exception as exc:  # noqa: BLE001
            state.error = f"{exc.__class__.__name__}: {exc}"
            state.status = "error"

    threading.Thread(target=worker, daemon=True).start()
    return state


def run_summary(state: RunState) -> dict:
    return {
        "run_id": state.run_id,
        "capability_id": state.capability_id,
        "status": state.status,
        "intervention": state.intervention,
        "result": state.result,
        "error": state.error,
        "evidence_dir": str(state.logger.dir) if state.logger else None,
    }


@app.get("/api/capabilities")
def api_capabilities():
    return jsonify([
        {
            "capability_id": c.capability_id,
            "name": c.name,
            "signature": c.signature(),
            "description": c.description,
            "inputs": [p.model_dump(mode="json") for p in c.inputs],
            "outputs": [o.model_dump(mode="json") for o in c.outputs],
            "business_outcomes": [b.code for b in c.business_outcomes],
        }
        for c in list_capabilities()
    ])


@app.post("/api/capabilities/<capability_id>/invoke")
def api_invoke(capability_id: str):
    cap = get_capability(capability_id)
    if cap is None:
        return jsonify({"error": f"unknown capability '{capability_id}'"}), 404
    body = request.get_json(force=True, silent=True) or {}
    params = {k: str(v) for k, v in (body.get("params") or {}).items()}
    confirm_risky = bool(body.get("confirm_risky", False))
    headed = bool(body.get("headed", False))
    state = start_replay(cap, params, confirm_risky=confirm_risky, headed=headed)
    return jsonify(run_summary(state)), 202


@app.get("/api/runs")
def api_runs():
    with RUNS_LOCK:
        states = list(RUNS.values())
    states.sort(key=lambda s: s.started_at, reverse=True)
    return jsonify([run_summary(s) for s in states[:50]])


@app.get("/api/runs/<run_id>")
def api_run(run_id: str):
    state = RUNS.get(run_id)
    if state is None:
        return jsonify({"error": "unknown run"}), 404
    return jsonify(run_summary(state))


@app.post("/api/runs/<run_id>/command")
def api_run_command(run_id: str):
    state = RUNS.get(run_id)
    if state is None:
        return jsonify({"error": "unknown run"}), 404
    body = request.get_json(force=True, silent=True) or {}
    line = str(body.get("command", "")).strip()
    if not line:
        return jsonify({"error": "empty command"}), 400
    state.console_in.push(line)
    return jsonify({"queued": line})


# --------------------------------------------------------------------- #
# Chatbot -- ONE LLM call routes plain language to a capability + typed
# args (tool-calling over the same catalog above). Execution is always
# the deterministic replay path above; the model never touches the page.
# --------------------------------------------------------------------- #


def _catalog_tools() -> list[dict]:
    tools = []
    for cap in list_capabilities():
        props = {p.name: {"type": "string", "description": p.description} for p in cap.inputs}
        required = [p.name for p in cap.inputs if p.required]
        tools.append({
            "name": cap.capability_id,
            "description": (
                f"{cap.description}\nSignature: {cap.signature()}\n"
                f"Possible business outcomes: {', '.join(b.code for b in cap.business_outcomes) or 'none'}."
            ),
            "input_schema": {"type": "object", "properties": props, "required": required},
        })
    return tools


def _await_result(run_id: str, timeout_s: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = RUNS.get(run_id)
        if state is None:
            return {"status": "error", "error": "run vanished"}
        if state.status in ("done", "error", "awaiting_human"):
            return run_summary(state)
        time.sleep(0.5)
    return {"status": "timeout"}


def _plain_language_reply(capability_id: str, outcome: dict) -> str:
    status = outcome.get("status")
    result = outcome.get("result") or {}
    if status == "awaiting_human":
        iv = outcome.get("intervention") or {}
        return (
            f"I started '{capability_id}' but it needs a human decision before continuing: "
            f"{iv.get('message', 'a risky or unrecognized step was reached')}. "
            f"Open the dashboard to look at the live session and approve, deny, or act on it."
        )
    if status == "error":
        return f"'{capability_id}' could not run: {outcome.get('error')}"
    if status == "timeout":
        return f"'{capability_id}' is still running -- check the dashboard for its result."
    if not result:
        return f"'{capability_id}' finished but returned no result."
    r_status = result.get("status")
    if r_status == "success":
        outs = result.get("outputs") or {}
        return f"Done. {capability_id} succeeded: " + ", ".join(f"{k}={v}" for k, v in outs.items())
    if r_status == "business_outcome":
        bo = result.get("business_outcome") or {}
        return f"'{capability_id}' completed and reported: {bo.get('code')} -- {bo.get('description')}"
    if r_status == "hard_failure":
        f = result.get("failure") or {}
        return (
            f"'{capability_id}' hit an unexpected problem at step {f.get('step_id')}: "
            f"expected {f.get('expected')!r}, saw {f.get('observed')!r}. Evidence saved for review."
        )
    return f"'{capability_id}' finished with status: {r_status}"


@app.post("/api/chat")
def api_chat():
    body = request.get_json(force=True, silent=True) or {}
    message = str(body.get("message", "")).strip()
    if not message:
        return jsonify({"error": "empty message"}), 400

    import anthropic

    client = anthropic.Anthropic()
    tools = _catalog_tools()
    if not tools:
        return jsonify({"reply": "No capabilities are recorded yet."})

    resp = client.messages.create(
        model=os.environ.get("CUA_MODEL", "claude-sonnet-4-5"),
        max_tokens=1024,
        tools=tools,
        tool_choice={"type": "any"},
        messages=[{
            "role": "user",
            "content": (
                "You are a routing layer over a fixed set of deterministic "
                "capabilities against MERIDIAN CORE. Pick exactly one capability "
                "that matches this request and call it with the right typed "
                "arguments extracted from the request. Do not invent values you "
                "were not given -- ask for the missing ones instead by explaining "
                "what's missing, if truly required arguments are absent.\n\n"
                f"Request: {message}"
            ),
        }],
    )

    tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
    if tool_use is None:
        text = "".join(b.text for b in resp.content if b.type == "text")
        return jsonify({"reply": text or "I couldn't map that to a known capability."})

    capability_id = tool_use.name
    params = {k: str(v) for k, v in (tool_use.input or {}).items()}
    cap = get_capability(capability_id)
    if cap is None:
        return jsonify({"reply": f"Routed to unknown capability '{capability_id}'."})

    state = start_replay(cap, params, confirm_risky=False, headed=False)
    outcome = _await_result(state.run_id)
    reply = _plain_language_reply(capability_id, outcome)
    return jsonify({"reply": reply, "capability_id": capability_id, "params": params, "run_id": state.run_id, "detail": outcome})


# --------------------------------------------------------------------- #
# Dashboard -- read-only view over the catalog + run history + evidence
# already written by RunLogger. Nothing here records anything new.
# --------------------------------------------------------------------- #


@app.get("/")
def dashboard():
    return render_template("dashboard.html")


@app.get("/api/evidence/<run_id>/events")
def api_evidence_events(run_id: str):
    p = safe_join(RUNS_DIR, run_id, "events.jsonl")
    if not p.exists():
        return jsonify([])
    events = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return jsonify(events)


@app.get("/api/evidence/<run_id>/media")
def api_evidence_media(run_id: str):
    run_dir = safe_join(RUNS_DIR, run_id)
    if not run_dir.exists():
        return jsonify({"screenshots": [], "dom_snapshots": []})
    shots_dir = run_dir / "screenshots"
    return jsonify({
        "screenshots": sorted(p.name for p in shots_dir.glob("*.png")) if shots_dir.exists() else [],
        "dom_snapshots": sorted(p.name for p in run_dir.glob("*.html")),
    })


@app.get("/api/evidence/<run_id>/file/<kind>/<path:filename>")
def api_evidence_file(run_id: str, kind: str, filename: str):
    from flask import send_file
    if kind == "screenshot":
        p = safe_join(RUNS_DIR, run_id, "screenshots", filename)
    else:
        p = safe_join(RUNS_DIR, run_id, filename)
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(p)


def safe_join(root: Path, *parts: str) -> Path:
    p = (root / Path(*parts)).resolve()
    root_resolved = root.resolve()
    if root_resolved not in p.parents and p != root_resolved:
        raise ValueError("path traversal blocked")
    return p


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5077, debug=False, threaded=True)
