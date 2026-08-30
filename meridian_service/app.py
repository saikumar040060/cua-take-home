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

RUNS_DIR = PROJECT_DIR / "runs"


@dataclass(frozen=True)
class BackendSystem:
    """One target this service can route capabilities against. Each system
    has its own capability directory, its own policy (own allowed origin),
    and its own set of credential inputs that are server-bound rather than
    ever accepted from a client -- mock_app has none at all (it has no
    sign-on), MERIDIAN CORE requires operator_id/password."""

    key: str
    artifacts_dir: Path
    policy_path: Path
    credential_bound_inputs: frozenset[str]


SYSTEMS: dict[str, BackendSystem] = {
    "meridian_core": BackendSystem(
        key="meridian_core",
        artifacts_dir=PROJECT_DIR / "artifacts" / "meridian_core",
        policy_path=PROJECT_DIR / "policy_meridian.json",
        credential_bound_inputs=frozenset({"operator_id", "password"}),
    ),
    "mock_app": BackendSystem(
        key="mock_app",
        artifacts_dir=PROJECT_DIR / "artifacts" / "mock_app",
        policy_path=PROJECT_DIR / "policy.json",
        credential_bound_inputs=frozenset(),
    ),
}

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
CAPABILITY_CACHE: TTLCache[dict[str, list[Capability]]] = TTLCache(ttl_seconds=5)
ROUTER_BREAKER = CircuitBreaker(failure_threshold=3, reset_seconds=30)
IDEMPOTENCY: IdempotencyStore["RunState"] = IdempotencyStore(ttl_seconds=3600)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# --------------------------------------------------------------------- #
# Customer identity -- session-scoped, bound to one of the known members
# across BOTH backend systems. Neither target has a "create a member"
# action (nothing was ever recorded for either, because no such control
# exists on either site), so customer "signup" can never be real account
# creation -- it can only bind a browser session to one of these known
# member numbers. Password is the same fixed demo convention already used
# for the operator/supervisor logins (see MERIDIAN_OPERATOR_ID etc. below)
# -- this is a demo login, not real authentication, and is labeled as such
# in the UI.
#
# `accounts` is a small, fixed set of real account/share IDs per member
# used only to populate the home page's account cards. Neither system has
# a capability that enumerates *all* of a member's accounts (each
# capability call is a full fresh sign-on, not a cheap query -- reading
# every one of a member's many accounts on every page load would mean
# that many full browser sessions per view), so the home page shows a
# couple of real, live-fetched accounts rather than a complete dynamic
# listing.
CUSTOMER_DEMO_PASSWORD = "password"
KNOWN_CUSTOMERS: dict[str, dict[str, Any]] = {
    "100987": {"system": "meridian_core", "accounts": ["100987-MMKT-11", "100987-S0001-9"]},
    "100234": {"system": "meridian_core", "accounts": ["100234-S0001-6", "100234-MMKT-16"]},
    "101555": {"system": "meridian_core", "accounts": ["101555-CERT-4", "101555-S0001-5"]},
    "102777": {"system": "meridian_core", "accounts": ["102777-MMKT-3", "102777-MMKT-4"]},
    "103001": {"system": "meridian_core", "accounts": ["103001-MMKT-4", "103001-MMKT-7"]},
    "20001": {"system": "mock_app", "accounts": ["712280-S00", "712280-S01"]},
    "20002": {"system": "mock_app", "accounts": ["751856-S00", "751856-S01"]},
    "20003": {"system": "mock_app", "accounts": ["707592-S00", "707592-S01"]},
}


def customer_system(customer_id: str) -> BackendSystem:
    return SYSTEMS[_customer_profile(customer_id)["system"]]


# --------------------------------------------------------------------- #
# Employee identity -- session-scoped, same demo convention as customer
# login (fixed password, known-identity list; a demo of the authorization
# property, not real authentication). The two identities are the ones the
# system already has: the operator and supervisor accounts the replay
# engine itself signs into MERIDIAN CORE with. Anonymous visitors can
# still *view* the console (catalog, run history, evidence) -- read-only
# -- but approve/deny/console commands require an employee session.
EMPLOYEE_DEMO_PASSWORD = "password"
KNOWN_EMPLOYEES: dict[str, dict[str, str]] = {
    "teller1": {"role": "TELLER"},
    "super1": {"role": "SUPERVISOR"},
}


def current_employee_id() -> str | None:
    return session.get("employee_id")


def current_customer_id() -> str | None:
    return session.get("customer_member_id")


def _json_log(event: str, **fields: Any) -> None:
    APP_LOG.info(json.dumps({"event": event, **fields}, default=str))


def _public_demo_read_only() -> bool:
    """Fail closed for public demos that do not yet have bank SSO/JWT."""
    return os.environ.get("PUBLIC_DEMO_READ_ONLY", "false").lower() == "true"


def _public_demo_synthetic() -> bool:
    """Use the bundled synthetic bank for a self-contained hosted demo.

    Read-only public deployments default to this mode so an existing Render
    service becomes functional on the next code deploy even before its
    Blueprint environment is re-synced.  Private deployments can explicitly
    set PUBLIC_DEMO_SYNTHETIC=false and configure real test credentials.
    """
    configured = os.environ.get("PUBLIC_DEMO_SYNTHETIC")
    if configured is None:
        return _public_demo_read_only()
    return configured.lower() == "true"


def _customer_profile(customer_id: str) -> dict[str, Any]:
    profile = {"member_id": customer_id, **KNOWN_CUSTOMERS[customer_id]}
    if _public_demo_synthetic():
        profile["system"] = "mock_app"
    return profile


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
        for system in SYSTEMS.values():
            Policy.load(system.policy_path)
        if _public_demo_synthetic():
            from urllib.request import urlopen

            with urlopen("http://127.0.0.1:5000/healthz", timeout=2) as response:
                if response.status != 200:
                    raise RuntimeError("synthetic bank is not healthy")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "not_ready", "error": str(exc)}), 503
    return jsonify({
        "status": "ready",
        "capabilities": count,
        "public_demo_read_only": _public_demo_read_only(),
        "synthetic_backend": _public_demo_synthetic(),
        "revision": os.environ.get("RENDER_GIT_COMMIT", "local")[:12],
    })


def new_gate(system_key: str) -> PolicyGate:
    return PolicyGate(Policy.load(SYSTEMS[system_key].policy_path))


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


def _load_capabilities_for(system: BackendSystem) -> list[Capability]:
    caps = []
    if system.artifacts_dir.exists():
        for p in sorted(system.artifacts_dir.glob("*.json")):
            try:
                caps.append(Capability.model_validate_json(p.read_text()))
            except Exception:
                continue
    return caps


def _load_all_capabilities() -> dict[str, list[Capability]]:
    return {key: _load_capabilities_for(system) for key, system in SYSTEMS.items()}


def list_capabilities(system_key: str | None = None) -> list[Capability]:
    """Return the frequently-read catalog through a short-lived TTL cache.

    With no ``system_key``, returns every capability across every backend
    system (capability IDs are namespaced per system -- meridian_* /
    mock_* -- so they never collide); with one, returns just that system's."""
    all_caps = CAPABILITY_CACHE.get_or_load(_load_all_capabilities)
    if system_key is None:
        return [c for caps in all_caps.values() for c in caps]
    return all_caps.get(system_key, [])


def get_capability(capability_id: str, system_key: str | None = None) -> Capability | None:
    for cap in list_capabilities(system_key):
        if cap.capability_id == capability_id:
            return cap
    return None


def find_capability_system(capability_id: str) -> str | None:
    """Which backend system a capability_id belongs to, for callers (the
    employee console, the direct API) that address a capability without
    already knowing -- unlike customer chat, which always has a logged-in
    member's own system."""
    for key in SYSTEMS:
        if get_capability(capability_id, key) is not None:
            return key
    return None


def _is_mutating(capability: Capability) -> bool:
    return any(step.risk == RiskLevel.RISKY for step in capability.steps)


def _client_inputs(
    capability: Capability, *, bound: frozenset[str] = frozenset(), exclude: frozenset[str] = frozenset()
):
    hidden = bound | exclude
    return [p for p in capability.inputs if p.name not in hidden]


def _client_signature(
    capability: Capability, *, bound: frozenset[str] = frozenset(), exclude: frozenset[str] = frozenset()
) -> str:
    params = ", ".join(
        f"{p.name}: {p.kind.value}" for p in _client_inputs(capability, bound=bound, exclude=exclude)
    )
    outputs = ", ".join(
        f"{output.name}: {output.kind.value}" for output in capability.outputs
    )
    return f"{capability.capability_id}({params}) -> {{{outputs}}}"


def _bind_system_credentials(
    system_key: str, capability: Capability, supplied: dict[str, str]
) -> dict[str, str]:
    bound = SYSTEMS[system_key].credential_bound_inputs
    forbidden = bound.intersection(supplied)
    if forbidden:
        raise ParamError(
            "infrastructure-owned parameter(s) may not be supplied by a client: "
            f"{sorted(forbidden)}"
        )

    params = dict(supplied)
    needs = {p.name for p in capability.inputs}.intersection(bound)
    if not needs:
        return params

    # Only meridian_core declares credential-bound inputs today (mock_app's
    # credential_bound_inputs is empty, so this branch is never reached for
    # it) -- the supervisor-vs-operator split below is specific to that
    # system's two privilege levels.
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
    system_key: str, capability: Capability, params: dict[str, str]
) -> str:
    bound = SYSTEMS[system_key].credential_bound_inputs
    public = {k: v for k, v in params.items() if k not in bound}
    return json.dumps(
        {"capability": capability.capability_id, "params": public},
        sort_keys=True,
        separators=(",", ":"),
    )


# --------------------------------------------------------------------- #
# Capability API -- runs the real, unmodified ReplayEngine. No LLM.
# --------------------------------------------------------------------- #


def start_replay(
    system_key: str,
    capability: Capability,
    params: dict[str, str],
    *,
    confirm_risky: bool,
    headed: bool,
    idempotency_key: str | None = None,
) -> RunState:
    fingerprint = _idempotency_fingerprint(system_key, capability, params)
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
                gate = new_gate(system_key)
                console = TrackedConsole(
                    session.page, gate, logger, run_state=state, redactor=redactor,
                    input_stream=state.console_in, output_stream=state.console_out,
                )
                engine = ReplayEngine(
                    session, capability, gate, logger,
                    confirm_risky=confirm_risky,
                    escalate_on_failure=True,
                    console=console,
                    step_screenshots=True,
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
    out = []
    for system_key, caps in _load_all_capabilities().items():
        bound = SYSTEMS[system_key].credential_bound_inputs
        for c in caps:
            out.append({
                "capability_id": c.capability_id,
                "system": system_key,
                "name": c.name,
                "signature": _client_signature(c, bound=bound),
                "description": c.description,
                "inputs": [
                    p.model_dump(mode="json", exclude={"example"})
                    for p in _client_inputs(c, bound=bound)
                ],
                "outputs": [o.model_dump(mode="json") for o in c.outputs],
                "business_outcomes": [b.code for b in c.business_outcomes],
                "requires_confirmation": _is_mutating(c),
            })
    return jsonify(out)


@app.post("/api/capabilities/<capability_id>/invoke")
def api_invoke(capability_id: str):
    system_key = find_capability_system(capability_id)
    if system_key is None:
        return jsonify({"error": f"unknown capability '{capability_id}'"}), 404
    cap = get_capability(capability_id, system_key)
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
        params = _bind_system_credentials(system_key, cap, supplied)
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
            system_key,
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
    employee_id = current_employee_id()
    if employee_id is None:
        return jsonify(
            {
                "error": "employee_login_required",
                "message": "Log in as an employee to act on runs.",
            }
        ), 401
    _json_log("employee_command", employee=employee_id, run_id=run_id)
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


def _catalog_tools(system_key: str, *, hide_member_id: bool = False) -> list[dict]:
    exclude = frozenset({"member_id"}) if hide_member_id else frozenset()
    bound = SYSTEMS[system_key].credential_bound_inputs
    tools = []
    for cap in list_capabilities(system_key):
        client_inputs = _client_inputs(cap, bound=bound, exclude=exclude)
        props = {
            p.name: {"type": "string", "description": p.description}
            for p in client_inputs
        }
        required = [p.name for p in client_inputs if p.required]
        client_signature = _client_signature(cap, bound=bound, exclude=exclude)
        member_note = (
            "\nThe customer's member number is already known from their login "
            "session -- it is not a parameter of this tool and must never be "
            "asked for."
            if hide_member_id and any(p.name == "member_id" for p in cap.inputs)
            else ""
        )
        tools.append({
            "name": cap.capability_id,
            "description": (
                f"{cap.description}\nCustomer-safe signature: {client_signature}\n"
                f"Possible business outcomes: {', '.join(b.code for b in cap.business_outcomes) or 'none'}."
                f"{member_note}"
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


# Per-system capability names/param shapes for the home dashboard -- the
# two systems' equivalent capabilities were recorded independently and
# don't share names or output field names (meridian_member_balance takes
# share_id and returns share_balance/share_status; mock_member_balance
# takes account_no and returns account_balance/account_status).
_HOME_CAPABILITIES: dict[str, dict[str, str]] = {
    "meridian_core": {
        "name_capability_id": "meridian_member_inquiry",
        "balance_capability_id": "meridian_member_balance",
        "account_param": "share_id",
        "balance_output": "share_balance",
        "status_output": "share_status",
    },
    "mock_app": {
        "name_capability_id": "mock_member_inquiry",
        "balance_capability_id": "mock_member_balance",
        "account_param": "account_no",
        "balance_output": "account_balance",
        "status_output": "account_status",
    },
}


def _load_customer_home(customer_id: str) -> dict[str, Any]:
    """Build the logged-in member's account overview.

    The public synthetic overview comes from the bundled in-memory data set,
    which is already trusted application state and avoids launching three
    browsers during page navigation. Customer assistant requests still use
    recorded deterministic browser replay and create employee-visible runs.
    Private live-bank deployments retain the replay-backed overview.
    """
    profile = _customer_profile(customer_id)
    system_key = profile["system"]
    backend_member_id = profile["member_id"]
    cfg = _HOME_CAPABILITIES[system_key]

    if _public_demo_synthetic():
        from mock_app.data import MEMBERS

        member = MEMBERS.get(str(backend_member_id))
        if member is not None:
            account_by_id = {account.number: account for account in member.accounts}
            return {
                "member_name": member.name,
                "accounts": [
                    {
                        "share_id": account_id,
                        "balance": account_by_id[account_id].balance,
                        "status": account_by_id[account_id].status,
                        "ok": True,
                    }
                    if account_id in account_by_id
                    else {
                        "share_id": account_id,
                        "balance": None,
                        "status": None,
                        "ok": False,
                    }
                    for account_id in profile.get("accounts", [])
                ],
            }

    specifications: list[tuple[str, Capability | None, dict[str, str]]] = []

    name_cap = get_capability(cfg["name_capability_id"], system_key)
    specifications.append(("name", name_cap, {"member_id": backend_member_id}))

    balance_cap = get_capability(cfg["balance_capability_id"], system_key)
    account_ids = profile.get("accounts", [])
    for account_id in account_ids:
        specifications.append((
            account_id,
            balance_cap,
            {"member_id": backend_member_id, cfg["account_param"]: account_id},
        ))

    def launch(capability: Capability | None, raw_params: dict[str, str]):
        if capability is None:
            return None
        try:
            params = _bind_system_credentials(system_key, capability, raw_params)
            return start_replay(
                system_key, capability, params, confirm_risky=False, headed=False
            )
        except (ParamError, RuntimeError):
            return None

    jobs = [
        (key, launch(capability, raw_params))
        for key, capability, raw_params in specifications
    ]
    outcomes = {
        key: (
            _await_result(state.run_id, timeout_s=60.0)
            if state else {"status": "error"}
        )
        for key, state in jobs
    }

    member_name = None
    name_result = (outcomes.get("name") or {}).get("result") or {}
    if name_result.get("status") == "success":
        member_name = name_result["outputs"].get("member_name")

    accounts = []
    for account_id in account_ids:
        result = (outcomes.get(account_id) or {}).get("result") or {}
        if result.get("status") == "success":
            outs = result["outputs"]
            accounts.append({
                "share_id": account_id,
                "balance": outs.get(cfg["balance_output"]),
                "status": outs.get(cfg["status_output"]),
                "ok": True,
            })
        else:
            accounts.append({"share_id": account_id, "balance": None, "status": None, "ok": False})

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
        if capability_id == "mock_member_balance":
            return (
                f"Your account balance is {outs.get('account_balance', 'unavailable')} "
                f"and its status is {outs.get('account_status', 'unavailable')}."
            )
        if capability_id == "mock_member_inquiry":
            return f"The name on your account is {outs.get('member_name', 'unavailable')}."
        if capability_id == "mock_transaction_history":
            return (
                "Your most recent transaction is "
                f"{outs.get('txn_date', 'date unavailable')}: "
                f"{outs.get('txn_description', 'description unavailable')} "
                f"({outs.get('txn_amount', 'amount unavailable')})."
            )
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


def _public_demo_route(
    message: str, customer_id: str, profile: dict[str, Any]
) -> dict[str, Any]:
    """Small, auditable router for the public demo's read-only surface.

    It deliberately recognizes only the handful of safe demo intents. The
    private product path still uses the LLM tool router over the full approved
    catalog; browser execution remains deterministic in both modes.
    """
    normalized = " ".join(message.lower().split())
    accounts = [str(value) for value in profile.get("accounts", [])]

    referenced_members = set(re.findall(r"\b\d{5,6}\b", normalized))
    other_members = {
        member_id for member_id in referenced_members
        if member_id in KNOWN_CUSTOMERS and member_id != customer_id
    }
    if other_members:
        return {
            "reply": (
                "For your security, I can only access the member account in "
                "your current session. Nothing was executed."
            ),
            "status": 403,
        }

    if any(word in normalized for word in ("capabilities", "capability", "what can you", "services")):
        return {
            "reply": (
                "In this read-only public demo I can:\n"
                "• check the balance and status of your accounts\n"
                "• show your most recent transaction\n"
                "• look up the name on your member record\n\n"
                "Transfers, contact updates, card controls, bill pay, loans, "
                "holds, and account closure are approved catalog capabilities, "
                "but writes are disabled on the public site."
            ),
            "status": 200,
        }

    if any(phrase in normalized for phrase in ("talk to a person", "human", "employee", "representative")):
        return {
            "reply": (
                "A bank employee can inspect the complete run history and "
                "evidence in the Operations Console. This public demo does not "
                "create a real support ticket; no account action was taken."
            ),
            "status": 200,
        }

    requested_account = next(
        (account for account in accounts if account.lower() in normalized),
        None,
    )
    typed_account = re.search(r"\b\d{6}-(?:[a-z0-9]+)-?\d*\b", normalized)
    if typed_account is not None:
        requested_account = typed_account.group(0).upper()
    selected_account = requested_account or (accounts[0] if accounts else None)

    if any(phrase in normalized for phrase in ("transaction", "recent activity", "history")):
        if selected_account is None:
            return {"reply": "Please provide an account ID.", "status": 400}
        return {
            "capability_id": "mock_transaction_history",
            "input": {"account_no": selected_account},
        }

    if "balance" in normalized or "account status" in normalized:
        if selected_account is None:
            return {"reply": "Please provide an account ID.", "status": 400}
        return {
            "capability_id": "mock_member_balance",
            "input": {"account_no": selected_account},
        }

    if any(phrase in normalized for phrase in ("my name", "name on", "who am i", "look up member", "member information")):
        return {"capability_id": "mock_member_inquiry", "input": {}}

    write_intents = (
        (("transfer", "send money", "move money"), "mock_transfer_funds"),
        (("update", "change address", "change phone", "contact info"), "mock_update_contact_info"),
        (("lock card", "unlock card", "card control"), "mock_lock_card"),
        (("close account",), "mock_close_account"),
        (("loan",), "mock_loan_application"),
        (("bill pay", "pay bill"), "mock_bill_pay"),
        (("place hold", "account hold"), "mock_place_hold"),
    )
    for phrases, capability_id in write_intents:
        if any(phrase in normalized for phrase in phrases):
            return {"capability_id": capability_id, "input": {}}

    if "open" in normalized and ("share" in normalized or "account" in normalized):
        return {
            "reply": (
                "Opening an account is a write action and is disabled on this "
                "public demo. Nothing was executed."
            ),
            "status": 403,
        }

    return {
        "reply": (
            "I can safely help with balances, recent transactions, the name on "
            "your member record, or the approved capability list. Nothing was executed."
        ),
        "status": 200,
    }


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

    profile = _customer_profile(customer_id)
    system_key = profile["system"]

    # member_id is never offered to the routing model and never accepted
    # from the request below -- it is always the logged-in session's own
    # member number, so a customer's chat can only ever act on their own
    # account, never another member's, regardless of what they type. The
    # tool list is also scoped to just this customer's own backend system
    # -- a mock_app member's chat never even sees MERIDIAN CORE capabilities.
    if _public_demo_synthetic():
        routed = _public_demo_route(message, customer_id, profile)
        if "reply" in routed:
            return jsonify({"reply": routed["reply"]}), int(routed.get("status", 200))
        capability_id = str(routed["capability_id"])
        supplied = {k: str(v) for k, v in routed.get("input", {}).items()}
    else:
        import anthropic

        tools = _catalog_tools(system_key, hide_member_id=True)
        if not tools:
            return jsonify({"reply": "No capabilities are recorded yet."})

        # Defense in depth: legacy credentials are server-bound and excluded
        # from tool schemas; scrub password-shaped text typed by a customer.
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
                        "capabilities for the customer's own account. Select one "
                        "capability only when all of its required customer inputs "
                        "are present. Never invent values. If information is "
                        "missing, explain what the customer must provide without "
                        "making a tool call.\n\n"
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
    cap = get_capability(capability_id, system_key)
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
        supplied["member_id"] = str(profile["member_id"])

    try:
        params = _bind_system_credentials(system_key, cap, supplied)
    except (ParamError, RuntimeError) as exc:
        return jsonify({"reply": str(exc)}), 503

    idempotency_key = request.headers.get("Idempotency-Key") or g.request_id
    try:
        state = start_replay(
            system_key,
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
        for p in _client_inputs(cap, bound=SYSTEMS[system_key].credential_bound_inputs)
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
    employee_id = current_employee_id()
    return render_template(
        "dashboard.html",
        public_demo_read_only=_public_demo_read_only(),
        employee_id=employee_id,
        employee_role=KNOWN_EMPLOYEES.get(employee_id, {}).get("role") if employee_id else None,
    )


@app.get("/employee/login")
def employee_login():
    if current_employee_id() is not None:
        return redirect(url_for("dashboard"))
    return render_template("employee_login.html", error=None, employee_id="")


@app.post("/employee/login")
def employee_login_submit():
    employee_id = str(request.form.get("employee_id", "")).strip()
    password = str(request.form.get("password", ""))
    if employee_id not in KNOWN_EMPLOYEES or password != EMPLOYEE_DEMO_PASSWORD:
        return render_template(
            "employee_login.html",
            error="Employee ID or password not recognized.",
            employee_id=employee_id,
        ), 401
    # Keep any customer session in the same browser intact -- demoing both
    # surfaces side by side shouldn't log one out to log the other in.
    customer = session.get("customer_member_id")
    session.clear()
    if customer:
        session["customer_member_id"] = customer
    session["employee_id"] = employee_id
    return redirect(url_for("dashboard"))


@app.get("/employee/logout")
def employee_logout():
    session.pop("employee_id", None)
    return redirect(url_for("dashboard"))


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
    # Symmetric with employee login: keep any employee session in the same
    # browser intact rather than logging it out.
    employee = session.get("employee_id")
    session.clear()
    if employee:
        session["employee_id"] = employee
    session["customer_member_id"] = member_id
    return redirect(url_for("customer_home"))


@app.get("/customer/logout")
def customer_logout():
    session.pop("customer_member_id", None)
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
        public_demo_synthetic=_public_demo_synthetic(),
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
