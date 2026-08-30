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
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_DIR / ".env")

from flask import (  # noqa: E402
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from cua.browser import BrowserSession  # noqa: E402
from cua.escalation.console import OperatorConsole  # noqa: E402
from cua.replay.engine import ParamError, ReplayEngine  # noqa: E402
from cua.runlog import RunLogger, new_run_id  # noqa: E402
from cua.safety.policy import Policy, PolicyGate  # noqa: E402
from cua.safety.redact import Redactor  # noqa: E402
from cua.schema import Capability, RiskLevel, Sensitivity  # noqa: E402
from meridian_service.platform import (  # noqa: E402
    CircuitBreaker,
    CircuitOpen,
    IdempotencyStore,
    SlidingWindowRateLimiter,
    TTLCache,
)

ARTIFACTS_DIR = PROJECT_DIR / "artifacts" / "meridian_core"
RUNS_DIR = PROJECT_DIR / "runs"
POLICY_PATH = PROJECT_DIR / "policy_meridian.json"

app = Flask(__name__)
# Session cookie signing key. A random per-process key is fine for this demo
# (it only means existing sessions drop on restart, same as the in-memory
# RUNS/rate-limit state already does) -- production would pull a stable key
# from a secret manager instead. Set SESSION_SECRET_KEY to pin one.
app.secret_key = os.environ.get("SESSION_SECRET_KEY") or os.urandom(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

APP_LOG = logging.getLogger("meridian_service")
if not APP_LOG.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    APP_LOG.addHandler(_handler)
APP_LOG.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

RATE_LIMITER = SlidingWindowRateLimiter()
CAPABILITY_CACHE: TTLCache[list[Capability]] = TTLCache(ttl_seconds=5)
ROUTER_BREAKER = CircuitBreaker(failure_threshold=3, reset_seconds=30)
IDEMPOTENCY: IdempotencyStore["RunState"] = IdempotencyStore(ttl_seconds=3600)

# These values are infrastructure-owned. They are never exposed to the routing
# model and never accepted from a customer request.
SYSTEM_BOUND_INPUTS = {"operator_id", "password"}
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# --------------------------------------------------------------------- #
# Customer identity -- session-scoped, bound to the live target's 5 real
# members. The target has no "create a member" action at all (nothing was
# ever recorded for it because no such control exists on the site), so
# customer "signup" can never be real account creation -- it can only bind
# a browser session to one of these known member numbers. Password is the
# same fixed demo convention already used for the operator/supervisor
# logins (see MERIDIAN_OPERATOR_ID etc.) -- this is a demo login, not real
# authentication, and is labeled as such in the UI.
#
# `shares` is a small, fixed set of real share IDs per member used only to
# populate the home page's account cards. There is no capability that
# enumerates *all* of a member's shares (each capability call is a full
# fresh sign-on against the live site, not a cheap query -- reading every
# one of a member's 12-18 shares on every page load would mean that many
# full browser sessions per view), so the home page shows a couple of real,
# live-fetched accounts rather than a complete dynamic listing.
CUSTOMER_DEMO_PASSWORD = "password"
KNOWN_CUSTOMERS: dict[str, dict[str, Any]] = {
    "100987": {"shares": ["100987-MMKT-11", "100987-S0001-9"]},
    "100234": {"shares": ["100234-S0001-6", "100234-MMKT-16"]},
    "101555": {"shares": ["101555-CERT-4", "101555-S0001-5"]},
    "102777": {"shares": ["102777-MMKT-3", "102777-MMKT-4"]},
    "103001": {"shares": ["103001-MMKT-4", "103001-MMKT-7"]},
}


def current_customer_id() -> str | None:
    return session.get("customer_member_id")


def _json_log(event: str, **fields: Any) -> None:
    APP_LOG.info(json.dumps({"event": event, **fields}, default=str))


def _public_demo_read_only() -> bool:
    """Fail closed for public demos that do not yet have bank SSO/JWT."""
    return os.environ.get("PUBLIC_DEMO_READ_ONLY", "false").lower() == "true"


def _rate_limit_for_path(path: str) -> tuple[int, int]:
    if path == "/api/chat":
        return 10, 60
    if path.endswith("/invoke"):
        return 20, 60
    if path.endswith("/command"):
        return 60, 60
    return 120, 60


@app.before_request
def _cross_cutting_before_request():
    supplied = request.headers.get("X-Request-ID", "")
    g.request_id = supplied if _REQUEST_ID_RE.fullmatch(supplied) else uuid.uuid4().hex
    g.request_started = time.monotonic()
    if not request.path.startswith("/api/"):
        return None
    limit, window = _rate_limit_for_path(request.path)
    identity = request.remote_addr or "unknown"
    decision = RATE_LIMITER.check(
        f"{identity}:{request.method}:{request.path}",
        limit=limit,
        window_seconds=window,
    )
    if decision.allowed:
        return None
    response = jsonify(
        {
            "error": "rate_limited",
            "message": "Too many requests. Please retry later.",
            "request_id": g.request_id,
        }
    )
    response.status_code = 429
    response.headers["Retry-After"] = str(decision.retry_after_seconds)
    return response


@app.after_request
def _cross_cutting_after_request(response):
    request_id = getattr(g, "request_id", uuid.uuid4().hex)
    started = getattr(g, "request_started", time.monotonic())
    duration_ms = int((time.monotonic() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:"
    )
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    _json_log(
        "http_request",
        request_id=request_id,
        method=request.method,
        path=request.path,
        status=response.status_code,
        duration_ms=duration_ms,
    )
    return response


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/readyz")
def readyz():
    try:
        count = len(list_capabilities())
        Policy.load(POLICY_PATH)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "not_ready", "error": str(exc)}), 503
    return jsonify({"status": "ready", "capabilities": count})


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


def _load_capabilities() -> list[Capability]:
    caps = []
    if ARTIFACTS_DIR.exists():
        for p in sorted(ARTIFACTS_DIR.glob("*.json")):
            try:
                caps.append(Capability.model_validate_json(p.read_text()))
            except Exception:
                continue
    return caps


def list_capabilities() -> list[Capability]:
    """Return the frequently-read catalog through a short-lived TTL cache."""
    return CAPABILITY_CACHE.get_or_load(_load_capabilities)


def get_capability(capability_id: str) -> Capability | None:
    for cap in list_capabilities():
        if cap.capability_id == capability_id:
            return cap
    return None


def _is_mutating(capability: Capability) -> bool:
    return any(step.risk == RiskLevel.RISKY for step in capability.steps)


def _client_inputs(capability: Capability, *, exclude: frozenset[str] = frozenset()):
    hidden = SYSTEM_BOUND_INPUTS | exclude
    return [p for p in capability.inputs if p.name not in hidden]


def _client_signature(capability: Capability, *, exclude: frozenset[str] = frozenset()) -> str:
    params = ", ".join(
        f"{p.name}: {p.kind.value}" for p in _client_inputs(capability, exclude=exclude)
    )
    outputs = ", ".join(
        f"{output.name}: {output.kind.value}" for output in capability.outputs
    )
    return f"{capability.capability_id}({params}) -> {{{outputs}}}"


def _bind_system_credentials(
    capability: Capability, supplied: dict[str, str]
) -> dict[str, str]:
    forbidden = SYSTEM_BOUND_INPUTS.intersection(supplied)
    if forbidden:
        raise ParamError(
            "infrastructure-owned parameter(s) may not be supplied by a client: "
            f"{sorted(forbidden)}"
        )

    params = dict(supplied)
    needs = {p.name for p in capability.inputs}.intersection(SYSTEM_BOUND_INPUTS)
    if not needs:
        return params

    supervisor = capability.capability_id == "meridian_place_account_hold"
    operator_key = (
        "MERIDIAN_SUPERVISOR_ID" if supervisor else "MERIDIAN_OPERATOR_ID"
    )
    password_key = (
        "MERIDIAN_SUPERVISOR_PASSWORD"
        if supervisor
        else "MERIDIAN_OPERATOR_PASSWORD"
    )
    values = {
        "operator_id": os.environ.get(operator_key),
        "password": os.environ.get(password_key),
    }
    missing = [name for name in needs if not values.get(name)]
    if missing:
        raise RuntimeError(
            "server-side legacy credentials are not configured for this capability"
        )
    params.update({name: str(values[name]) for name in needs})
    return params


def _idempotency_fingerprint(
    capability: Capability, params: dict[str, str]
) -> str:
    public = {
        k: v for k, v in params.items() if k not in SYSTEM_BOUND_INPUTS
    }
    return json.dumps(
        {"capability": capability.capability_id, "params": public},
        sort_keys=True,
        separators=(",", ":"),
    )


# --------------------------------------------------------------------- #
# Capability API -- runs the real, unmodified ReplayEngine. No LLM.
# --------------------------------------------------------------------- #


def start_replay(
    capability: Capability,
    params: dict[str, str],
    *,
    confirm_risky: bool,
    headed: bool,
    idempotency_key: str | None = None,
) -> RunState:
    fingerprint = _idempotency_fingerprint(capability, params)
    if idempotency_key:
        existing = IDEMPOTENCY.get(idempotency_key, fingerprint)
        if existing is not None:
            return existing

    run_id = new_run_id("replay")
    state = RunState(run_id=run_id, capability_id=capability.capability_id)
    with RUNS_LOCK:
        RUNS[run_id] = state
    if idempotency_key:
        IDEMPOTENCY.put(idempotency_key, fingerprint, state)

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
            _json_log(
                "replay_worker_error",
                run_id=run_id,
                capability_id=capability.capability_id,
                error_type=exc.__class__.__name__,
            )

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
        "evidence_available": state.logger is not None,
    }


@app.get("/api/capabilities")
def api_capabilities():
    return jsonify([
        {
            "capability_id": c.capability_id,
            "name": c.name,
            "signature": _client_signature(c),
            "description": c.description,
            "inputs": [
                p.model_dump(mode="json", exclude={"example"})
                for p in _client_inputs(c)
            ],
            "outputs": [o.model_dump(mode="json") for o in c.outputs],
            "business_outcomes": [b.code for b in c.business_outcomes],
            "requires_confirmation": _is_mutating(c),
        }
        for c in list_capabilities()
    ])


@app.post("/api/capabilities/<capability_id>/invoke")
def api_invoke(capability_id: str):
    cap = get_capability(capability_id)
    if cap is None:
        return jsonify({"error": f"unknown capability '{capability_id}'"}), 404
    body = request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict) or not isinstance(body.get("params", {}), dict):
        return jsonify({"error": "params must be a JSON object"}), 400
    if "confirm_risky" in body and not isinstance(body["confirm_risky"], bool):
        return jsonify({"error": "confirm_risky must be a boolean"}), 400
    if _public_demo_read_only() and _is_mutating(cap):
        return jsonify(
            {
                "error": "public_demo_read_only",
                "message": (
                    "Write capabilities are disabled on the public demo until "
                    "bank SSO/JWT and employee authorization are connected."
                ),
            }
        ), 403
    supplied = {k: str(v) for k, v in body.get("params", {}).items()}
    try:
        params = _bind_system_credentials(cap, supplied)
    except ParamError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503

    idempotency_key = request.headers.get("Idempotency-Key")
    if _is_mutating(cap) and not idempotency_key:
        return jsonify(
            {"error": "Idempotency-Key is required for mutating capabilities"}
        ), 400
    if idempotency_key and not _REQUEST_ID_RE.fullmatch(idempotency_key):
        return jsonify({"error": "invalid Idempotency-Key"}), 400

    confirm_risky = body.get("confirm_risky", False)
    try:
        state = start_replay(
            cap,
            params,
            confirm_risky=confirm_risky,
            headed=os.environ.get("CUA_HEADED", "false").lower() == "true",
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
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
    if _public_demo_read_only():
        return jsonify(
            {
                "error": "public_demo_read_only",
                "message": "Employee commands are disabled on the public demo.",
            }
        ), 403
    state = RUNS.get(run_id)
    if state is None:
        return jsonify({"error": "unknown run"}), 404
    if state.status != "awaiting_human":
        return jsonify({"error": "run is not awaiting human action"}), 409
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


def _catalog_tools(*, hide_member_id: bool = False) -> list[dict]:
    exclude = frozenset({"member_id"}) if hide_member_id else frozenset()
    tools = []
    for cap in list_capabilities():
        client_inputs = _client_inputs(cap, exclude=exclude)
        props = {
            p.name: {"type": "string", "description": p.description}
            for p in client_inputs
        }
        required = [p.name for p in client_inputs if p.required]
        client_signature = _client_signature(cap, exclude=exclude)
        tools.append({
            "name": cap.capability_id,
            "description": (
                f"{cap.description}\nCustomer-safe signature: {client_signature}\n"
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


def _load_customer_home(customer_id: str) -> dict[str, Any]:
    """Fetch the logged-in member's name and a few real account cards via
    real deterministic replay (not a shortcut/mock) -- each capability call
    is launched concurrently (own browser session, own thread) since they
    are independent, then awaited together, so the page takes roughly as
    long as the slowest single call rather than the sum of all of them."""
    jobs: list[tuple[str, Any]] = []

    name_cap = get_capability("meridian_member_inquiry")
    if name_cap is not None:
        try:
            params = _bind_system_credentials(name_cap, {"member_id": customer_id})
            jobs.append(("name", start_replay(name_cap, params, confirm_risky=False, headed=False)))
        except (ParamError, RuntimeError):
            jobs.append(("name", None))

    balance_cap = get_capability("meridian_member_balance")
    share_ids = KNOWN_CUSTOMERS.get(customer_id, {}).get("shares", [])
    for share_id in share_ids:
        if balance_cap is None:
            jobs.append((share_id, None))
            continue
        try:
            params = _bind_system_credentials(
                balance_cap, {"member_id": customer_id, "share_id": share_id}
            )
            jobs.append((share_id, start_replay(balance_cap, params, confirm_risky=False, headed=False)))
        except (ParamError, RuntimeError):
            jobs.append((share_id, None))

    outcomes = {
        key: (_await_result(state.run_id, timeout_s=60.0) if state else {"status": "error"})
        for key, state in jobs
    }

    member_name = None
    name_result = (outcomes.get("name") or {}).get("result") or {}
    if name_result.get("status") == "success":
        member_name = name_result["outputs"].get("member_name")

    accounts = []
    for share_id in share_ids:
        result = (outcomes.get(share_id) or {}).get("result") or {}
        if result.get("status") == "success":
            outs = result["outputs"]
            accounts.append({
                "share_id": share_id,
                "balance": outs.get("share_balance"),
                "status": outs.get("share_status"),
                "ok": True,
            })
        else:
            accounts.append({"share_id": share_id, "balance": None, "status": None, "ok": False})

    return {"member_name": member_name, "accounts": accounts}


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
    customer_id = current_customer_id()
    if customer_id is None:
        return jsonify(
            {
                "reply": "Please log in to chat about your account.",
                "login_required": True,
            }
        ), 401

    body = request.get_json(force=True, silent=True) or {}
    message = str(body.get("message", "")).strip()
    if not message:
        return jsonify({"error": "empty message"}), 400

    import anthropic

    # member_id is never offered to the routing model and never accepted
    # from the request below -- it is always the logged-in session's own
    # member number, so a customer's chat can only ever act on their own
    # account, never another member's, regardless of what they type.
    tools = _catalog_tools(hide_member_id=True)
    if not tools:
        return jsonify({"reply": "No capabilities are recorded yet."})

    # Defense in depth: legacy credentials are server-bound and excluded from
    # tool schemas; also scrub password-shaped text a customer typed anyway.
    routed_message = re.sub(
        r"(?i)(password\s*(?:is|=|:)?\s*)\S+",
        r"\1[REDACTED]",
        message,
    )

    def _route():
        client = anthropic.Anthropic()
        return client.messages.create(
            model=os.environ.get("CUA_MODEL", "claude-sonnet-4-5"),
            max_tokens=1024,
            tools=tools,
            messages=[{
                "role": "user",
                "content": (
                    "You are a routing layer over a fixed set of deterministic "
                    "capabilities against MERIDIAN CORE. Select one capability "
                    "only when all of its required customer inputs are present. "
                    "Never invent values. If information is missing, explain what "
                    "the customer must provide without making a tool call.\n\n"
                    f"Request: {routed_message}"
                ),
            }],
        )

    try:
        resp = ROUTER_BREAKER.call(_route)
    except CircuitOpen:
        return jsonify(
            {
                "reply": (
                    "The assistant is temporarily unavailable. Your request was "
                    "not executed; please try again or contact a bank employee."
                )
            }
        ), 503
    except Exception as exc:  # noqa: BLE001
        _json_log("router_error", error_type=exc.__class__.__name__)
        return jsonify(
            {
                "reply": (
                    "I could not safely understand that request. Nothing was "
                    "executed; please try again or contact a bank employee."
                )
            }
        ), 503

    tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
    if tool_use is None:
        text = "".join(b.text for b in resp.content if b.type == "text")
        return jsonify({"reply": text or "I couldn't map that to a known capability."})

    capability_id = tool_use.name
    supplied = {k: str(v) for k, v in (tool_use.input or {}).items()}
    cap = get_capability(capability_id)
    if cap is None:
        return jsonify({"reply": f"Routed to unknown capability '{capability_id}'."})

    if _public_demo_read_only() and _is_mutating(cap):
        return jsonify(
            {
                "reply": (
                    "That request maps to a write capability. Writes are disabled "
                    "on this public demo until bank SSO/JWT and employee "
                    "authorization are connected. Nothing was executed."
                ),
                "capability_id": capability_id,
            }
        ), 403

    # Force member_id to the logged-in customer's own, regardless of
    # whether the model supplied one -- the tool schema never offered it as
    # an option, and this overwrites/ignores anything supplied anyway.
    supplied.pop("member_id", None)
    if any(p.name == "member_id" for p in cap.inputs):
        supplied["member_id"] = customer_id

    try:
        params = _bind_system_credentials(cap, supplied)
    except (ParamError, RuntimeError) as exc:
        return jsonify({"reply": str(exc)}), 503

    idempotency_key = request.headers.get("Idempotency-Key") or g.request_id
    try:
        state = start_replay(
            cap,
            params,
            confirm_risky=False,
            headed=False,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        return jsonify({"reply": str(exc)}), 409
    outcome = _await_result(state.run_id)
    reply = _plain_language_reply(capability_id, outcome)
    safe_params = {
        p.name: supplied[p.name]
        for p in _client_inputs(cap)
        if p.sensitivity == Sensitivity.PUBLIC and p.name in supplied
    }
    return jsonify({
        "reply": reply,
        "capability_id": capability_id,
        "params": safe_params,
        "run_id": state.run_id,
        "detail": outcome,
    })


# --------------------------------------------------------------------- #
# Dashboard -- read-only view over the catalog + run history + evidence
# already written by RunLogger. Nothing here records anything new.
# --------------------------------------------------------------------- #


@app.get("/")
def dashboard():
    return render_template(
        "dashboard.html", public_demo_read_only=_public_demo_read_only()
    )


@app.get("/customer")
def customer_landing():
    return render_template(
        "customer_landing.html",
        public_demo_read_only=_public_demo_read_only(),
        logged_in=current_customer_id() is not None,
    )


@app.get("/customer/signup")
def customer_signup():
    return render_template(
        "customer_signup.html",
        known_member_ids=sorted(KNOWN_CUSTOMERS),
    )


@app.get("/customer/login")
def customer_login():
    if current_customer_id() is not None:
        return redirect(url_for("customer_home"))
    return render_template("customer_login.html", error=None, member_id="")


@app.post("/customer/login")
def customer_login_submit():
    member_id = str(request.form.get("member_id", "")).strip()
    password = str(request.form.get("password", ""))
    if member_id not in KNOWN_CUSTOMERS or password != CUSTOMER_DEMO_PASSWORD:
        return render_template(
            "customer_login.html",
            error="Member ID or password not recognized.",
            member_id=member_id,
        ), 401
    session.clear()
    session["customer_member_id"] = member_id
    return redirect(url_for("customer_home"))


@app.get("/customer/logout")
def customer_logout():
    session.clear()
    return redirect(url_for("customer_landing"))


@app.get("/customer/home")
def customer_home():
    customer_id = current_customer_id()
    if customer_id is None:
        return redirect(url_for("customer_login"))
    profile = _load_customer_home(customer_id)
    return render_template(
        "customer_home.html",
        public_demo_read_only=_public_demo_read_only(),
        member_id=customer_id,
        member_name=profile["member_name"],
        accounts=profile["accounts"],
    )


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
