"""End-to-end: scripted discovery -> artifact -> deterministic replays.

The discovery loop here runs with the scripted FakeDecider (the LLM seam's
test double) so the suite is hermetic; the recorded artifact then drives
real replays against the live mock app under every runtime condition:
success, business outcomes, recoverable interstitial, transient slowness,
and a hard failure with a debug bundle.
"""

from __future__ import annotations

import io
import json

import pytest

from cua.discovery.agent import DiscoveryAgent
from cua.escalation.console import OperatorConsole
from cua.replay.engine import ParamError, ReplayEngine
from cua.replay.result import ReplayStatus
from cua.runlog import RunLogger, new_run_id
from cua.safety.policy import PolicyGate
from cua.safety.redact import Redactor
from cua.schema import Capability, RiskLevel
from tests.conftest import load_spec, make_policy, set_chaos
from tests.fake_decider import FakeDecider, open_sub_account_script

GOOD_PARAMS = {
    "member_id": "10456",
    "product_type": "Money Market",
    "nickname": "Emergency fund",
    "initial_deposit": "50.00",
}


@pytest.fixture(scope="session")
def capability(app_clean, browser_session, tmp_path_factory) -> Capability:
    """Run scripted discovery once; all replay tests share the artifact."""
    spec = load_spec(app_clean)
    gate = PolicyGate(make_policy(app_clean))
    logger = RunLogger(
        tmp_path_factory.mktemp("runs"), new_run_id("discovery"), Redactor()
    )
    context = browser_session.browser.new_context()

    class _Sess:
        browser = browser_session.browser

    sess = _Sess()
    sess.context = context
    sess.page = context.new_page()

    agent = DiscoveryAgent(
        sess, spec, FakeDecider(open_sub_account_script()), gate, logger,
        approve_risky=True,
    )
    outcome = agent.run()
    context.close()
    assert outcome.success, outcome.summary
    return outcome.capability


def _replay(page_sess, origin, capability, params, tmp_path, **kwargs):
    gate = PolicyGate(make_policy(origin))
    logger = RunLogger(tmp_path, new_run_id("replay"), Redactor())
    engine = ReplayEngine(page_sess, capability, gate, logger, **kwargs)
    return engine.run(params), logger


# ---------------------------------------------------------------- artifact


class TestRecordedArtifact:
    def test_values_are_parameterized_never_literal(self, capability):
        # Identifier-sensitivity values must not appear anywhere in the
        # artifact; run values of any sensitivity must not appear in the
        # executable steps (only templates). Public values MAY appear once,
        # as a documented `example` on their input declaration.
        raw = capability.model_dump_json()
        assert "10023" not in raw, "identifier value leaked into artifact"
        steps_raw = json.dumps(
            [s.model_dump(mode="json") for s in capability.steps]
        )
        for literal in ("10023", "Winter savings", "25.00", "Holiday Club"):
            assert literal not in steps_raw, (
                f"run literal {literal!r} leaked into steps"
            )
        assert "{member_id}" in steps_raw
        assert "{initial_deposit}" in steps_raw

    def test_submit_step_classified_risky(self, capability):
        risky = [s for s in capability.steps if s.risk == RiskLevel.RISKY]
        assert len(risky) == 1
        assert "sub-account" in risky[0].description.lower() or "submit" in risky[0].description.lower()

    def test_every_step_has_checkpoint_and_fallback_locators(self, capability):
        for step in capability.steps:
            assert step.checkpoint.description
            if step.target:
                assert len(step.target.locators) >= 2, (
                    f"{step.id} has no fallback locator"
                )

    def test_outputs_wired_to_read_step(self, capability):
        assert [o.name for o in capability.outputs] == ["confirmation_ref"]
        source = capability.outputs[0].source_step_id
        assert any(s.id == source and s.action.value == "read"
                   for s in capability.steps)

    def test_declared_outcomes_and_recoveries_present(self, capability):
        codes = {o.code for o in capability.business_outcomes}
        assert codes == {"member_not_found", "validation_rejected"}
        assert [r.id for r in capability.recovery_rules] == ["session_expiry_notice"]

    def test_json_round_trip(self, capability, tmp_path):
        path = tmp_path / "cap.json"
        path.write_text(capability.model_dump_json(indent=2))
        assert Capability.model_validate_json(path.read_text()) == capability


# ----------------------------------------------------------------- replays


class TestReplayOutcomes:
    def test_success_with_different_params(
        self, app_clean, fresh_page, capability, tmp_path
    ):
        result, _ = _replay(
            fresh_page, app_clean, capability, GOOD_PARAMS, tmp_path,
            confirm_risky=True,
        )
        assert result.status == ReplayStatus.SUCCESS
        assert result.outputs["confirmation_ref"].startswith("SA-2026-")
        assert result.recoveries == []

    def test_member_not_found_is_business_outcome(
        self, app_clean, fresh_page, capability, tmp_path
    ):
        result, _ = _replay(
            fresh_page, app_clean, capability,
            {**GOOD_PARAMS, "member_id": "99999"}, tmp_path,
            confirm_risky=True,
        )
        assert result.status == ReplayStatus.BUSINESS_OUTCOME
        assert result.business_outcome.code == "member_not_found"
        assert result.failure is None

    def test_validation_rejection_is_business_outcome(
        self, app_clean, fresh_page, capability, tmp_path
    ):
        result, _ = _replay(
            fresh_page, app_clean, capability,
            {**GOOD_PARAMS, "initial_deposit": "1.00"}, tmp_path,
            confirm_risky=True,
        )
        assert result.status == ReplayStatus.BUSINESS_OUTCOME
        assert result.business_outcome.code == "validation_rejected"

    def test_interstitial_is_recovered_and_run_completes(
        self, app_clean, fresh_page, capability, tmp_path
    ):
        set_chaos(fresh_page.context, app_clean, "interstitial")
        result, _ = _replay(
            fresh_page, app_clean, capability, GOOD_PARAMS, tmp_path,
            confirm_risky=True,
        )
        assert result.status == ReplayStatus.SUCCESS
        assert [r.rule_id for r in result.recoveries] == ["session_expiry_notice"]

    def test_transient_slowness_absorbed_by_checkpoint_wait(
        self, app_clean, fresh_page, capability, tmp_path
    ):
        set_chaos(fresh_page.context, app_clean, "slow")
        result, _ = _replay(
            fresh_page, app_clean, capability, GOOD_PARAMS, tmp_path,
            confirm_risky=True,
        )
        assert result.status == ReplayStatus.SUCCESS

    def test_broken_app_is_hard_failure_with_debug_bundle(
        self, app_clean, fresh_page, capability, tmp_path
    ):
        set_chaos(fresh_page.context, app_clean, "broken")
        result, logger = _replay(
            fresh_page, app_clean, capability, GOOD_PARAMS, tmp_path,
            confirm_risky=True,
        )
        assert result.status == ReplayStatus.HARD_FAILURE
        f = result.failure
        assert f.step_id and f.expected and f.observed
        assert f.screenshot_path and f.dom_snapshot_path
        # The structured log recorded the failure event too.
        events = [
            json.loads(line)
            for line in logger.events_path.read_text().splitlines()
        ]
        assert any(e["event"] == "hard_failure" for e in events)

    def test_missing_required_param_rejected_before_browser_moves(
        self, app_clean, fresh_page, capability, tmp_path
    ):
        with pytest.raises(ParamError, match="member_id"):
            _replay(
                fresh_page, app_clean, capability,
                {"product_type": "Money Market"}, tmp_path,
            )


class TestRiskyGate:
    def test_unconfirmed_risky_step_escalates_and_deny_stops_run(
        self, app_clean, fresh_page, capability, tmp_path
    ):
        gate = PolicyGate(make_policy(app_clean))
        logger = RunLogger(tmp_path, new_run_id("replay"), Redactor())
        console = OperatorConsole(
            fresh_page.page, gate, logger,
            input_stream=io.StringIO("deny\n"), output_stream=io.StringIO(),
        )
        engine = ReplayEngine(
            fresh_page, capability, gate, logger,
            confirm_risky=False, console=console,
        )
        result = engine.run(GOOD_PARAMS)
        assert result.status == ReplayStatus.HARD_FAILURE
        assert "risky" in result.failure.expected.lower()
        assert result.intervention_id is not None

    def test_operator_approval_lets_risky_step_proceed(
        self, app_clean, fresh_page, capability, tmp_path
    ):
        gate = PolicyGate(make_policy(app_clean))
        logger = RunLogger(tmp_path, new_run_id("replay"), Redactor())
        console = OperatorConsole(
            fresh_page.page, gate, logger,
            input_stream=io.StringIO("approve\n"), output_stream=io.StringIO(),
        )
        engine = ReplayEngine(
            fresh_page, capability, gate, logger,
            confirm_risky=False, console=console,
        )
        result = engine.run(GOOD_PARAMS)
        assert result.status == ReplayStatus.SUCCESS


class TestLocatorFallback:
    def test_broken_primary_locator_falls_back_and_flags_drift(
        self, app_clean, fresh_page, capability, tmp_path
    ):
        # Sabotage the primary locator of the first click step.
        mutated = Capability.model_validate_json(capability.model_dump_json())
        click_step = next(s for s in mutated.steps if s.action.value == "click")
        assert len(click_step.target.locators) >= 2
        click_step.target.locators[0].name = "Nonexistent Button Label"
        result, logger = _replay(
            fresh_page, app_clean, mutated, GOOD_PARAMS, tmp_path,
            confirm_risky=True,
        )
        assert result.status == ReplayStatus.SUCCESS
        report = next(s for s in result.steps if s.step_id == click_step.id)
        assert report.locator_rank > 0
        assert report.status == "degraded_locator"
        events = [
            json.loads(line)
            for line in logger.events_path.read_text().splitlines()
        ]
        assert any(e["event"] == "locator_degraded" for e in events)
