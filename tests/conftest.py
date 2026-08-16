"""Shared fixtures: mock-app servers (per chaos mode) and a browser session.

Servers are real subprocesses serving the real Flask app; the browser is a
real Chromium. Only the LLM is substituted (scripted FakeDecider) — the
point of the suite is to exercise everything around the model for real.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# In some sandboxes Playwright's default browser path is absent but a
# system Chromium exists; mirror the CLI's env-var escape hatch.
_CHROMIUM_CANDIDATES = ["/opt/pw-browsers/chromium"]
if "CUA_CHROMIUM_PATH" not in os.environ:
    for candidate in _CHROMIUM_CANDIDATES:
        if Path(candidate).exists():
            os.environ["CUA_CHROMIUM_PATH"] = candidate
            break


def _start_app(port: int, chaos: str) -> subprocess.Popen:
    env = {**os.environ, "MOCK_PORT": str(port), "MOCK_CHAOS": chaos}
    proc = subprocess.Popen(
        [sys.executable, "-m", "mock_app.app"],
        cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 15
    url = f"http://127.0.0.1:{port}/"
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError(f"mock app on :{port} exited early")
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError(f"mock app on :{port} did not come up")


def _server_fixture(port: int, chaos: str):
    @pytest.fixture(scope="session")
    def server():
        proc = _start_app(port, chaos)
        yield f"http://127.0.0.1:{port}"
        proc.terminate()
        proc.wait(timeout=5)

    return server


app_clean = _server_fixture(5101, "none")


def set_chaos(context, origin: str, mode: str) -> None:
    """Inject a chaos mode for this browser context via the app's cookie hook."""
    context.add_cookies([{"name": "chaos", "value": mode, "url": origin + "/"}])


@pytest.fixture(scope="session")
def browser_session():
    from cua.browser import BrowserSession

    with BrowserSession() as session:
        yield session


@pytest.fixture()
def fresh_page(browser_session):
    """A fresh context/page per test (isolated cookies -> chaos state)."""
    context = browser_session.browser.new_context()
    page = context.new_page()

    class _Sess:
        pass

    sess = _Sess()
    sess.browser = browser_session.browser
    sess.context = context
    sess.page = page
    yield sess
    context.close()


def make_policy(*origins: str):
    from cua.safety.policy import Policy

    return Policy(
        allowed_origins=list(origins),
        allowed_path_prefixes=["/"],
        blocked_path_prefixes=["/admin"],
    )


def load_spec(entry_origin: str):
    from cua.discovery.spec import GoalSpec

    spec = GoalSpec.load(REPO / "specs" / "open_sub_account.json")
    spec.entry_url = f"{entry_origin}/"
    return spec
