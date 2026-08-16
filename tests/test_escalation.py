"""Escalation & handoff: pause -> human operates the SAME session -> resume.

The 'human' here is a scripted command stream into the operator console —
exactly what a person would type — driving the same live Playwright page
the agent was using. We assert the control-transfer contract: a structured
intervention request is raised and persisted, human actions are recorded
and policy-checked, and automation resumes on the state the human left.
"""

from __future__ import annotations

import io
import json

from cua.discovery.agent import DiscoveryAgent
from cua.discovery.decider import Decision
from cua.escalation.console import OperatorConsole
from cua.runlog import RunLogger, new_run_id
from cua.safety.policy import PolicyGate
from cua.safety.redact import Redactor
from tests.conftest import load_spec, make_policy
from tests.fake_decider import FakeDecider, open_sub_account_script


def test_blocked_agent_hands_off_and_resumes_after_human_fixes_state(
    app_clean, fresh_page, tmp_path
):
    spec = load_spec(app_clean)
    gate = PolicyGate(make_policy(app_clean))
    logger = RunLogger(tmp_path, new_run_id("discovery"), Redactor())

    # The agent gives up immediately; the human performs the lookup by hand
    # (fill member number, click Search), tries one off-allowlist command
    # (must be blocked), then returns control. The remaining script picks up
    # from the member page.
    script = [Decision(tool="blocked", args={"reason": "cannot find the form"})]
    script += open_sub_account_script()[2:]  # resume after lookup steps

    human = io.StringIO(
        "look\n"
        "fill e1 10023\n"
        "click e2\n"
        "goto https://evil.example.com/\n"
        "resume\n"
    )
    console_out = io.StringIO()
    console = OperatorConsole(
        fresh_page.page, gate, logger,
        input_stream=human, output_stream=console_out,
    )
    agent = DiscoveryAgent(
        fresh_page, spec, FakeDecider(script), gate, logger,
        approve_risky=True, console=console,
    )
    outcome = agent.run()

    assert outcome.success, outcome.summary
    assert outcome.outputs["confirmation_ref"].startswith("SA-2026-")

    # The human's off-allowlist command was refused by the same policy gate.
    assert "POLICY BLOCKED" in console_out.getvalue()

    events = [json.loads(l) for l in logger.events_path.read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert "intervention_raised" in kinds
    assert "handoff_started" in kinds
    assert kinds.count("human_action") >= 4
    assert "handoff_ended" in kinds
    # Control-transfer ordering: no automation actions while human held control.
    start, end = kinds.index("handoff_started"), kinds.index("handoff_ended")
    for event in events[start:end]:
        assert event.get("actor") != "automation" or event["event"] in (
            "intervention_raised",
        )

    # The intervention request was persisted with the human's actions.
    iv_files = list(logger.dir.glob("iv-*.json"))
    assert iv_files, "intervention request not persisted"
    iv = json.loads(iv_files[0].read_text())
    assert iv["reason"] == "agent_blocked"
    assert iv["resolution"] == "resumed"
    assert [a["command"] for a in iv["human_actions"]][:3] == ["look", "fill", "click"]
    assert iv["screenshot_path"], "intervention must carry a screenshot"


def test_intervention_screenshot_and_log_excerpt_captured(
    app_clean, fresh_page, tmp_path
):
    from cua.escalation.intervention import InterventionReason, build_intervention

    logger = RunLogger(tmp_path, new_run_id("replay"), Redactor())
    logger.event("step_ok", actor="automation", step="s01")
    fresh_page.page.goto(app_clean + "/")
    request = build_intervention(
        run_logger=logger,
        page=fresh_page.page,
        reason=InterventionReason.REPLAY_HARD_FAILURE,
        capability_or_goal="open_sub_account",
        current_step="s02",
        message="unexpected state",
    )
    assert request.screenshot_path and request.log_excerpt
    assert any("step_ok" in line for line in request.log_excerpt)
