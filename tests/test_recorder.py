"""Recorder unit tests: step inclusion rule + parameterization guards.

These target the exact failure modes observed while validating against the
real LLM: a slightly-misphrased expectation must not drop a state-advancing
step, a wrong expectation must not become a permanent checkpoint, and model
reasoning that mentions concrete values must be parameterized.
"""

from __future__ import annotations

from cua.browser import ElementInfo
from cua.discovery.recorder import ExecutedAction, Recording, build_capability
from cua.discovery.spec import GoalSpec
from cua.safety.policy import Policy, PolicyGate
from tests.conftest import REPO


def _spec() -> GoalSpec:
    return GoalSpec.load(REPO / "specs" / "open_sub_account.json")


def _gate() -> PolicyGate:
    return PolicyGate(Policy(allowed_origins=["http://127.0.0.1:5000"]))


def _el(name="Open Sub-Account", role="link", real=True) -> ElementInfo:
    return ElementInfo(
        ref="e1", role=role, name=name, tag="a", name_attr=None,
        id_attr=None, css_path="div > a:nth-of-type(1)", text=name,
        name_is_real=real,
    )


def _click(url_before, url_after, *, verified, success_text="X", reason=""):
    return ExecutedAction(
        tool="click", args={"reason": reason}, element=_el(),
        url_before=url_before, url_after=url_after,
        success_text=success_text, verified=verified, reason=reason,
    )


def test_state_advancing_click_with_wrong_expectation_is_kept():
    rec = Recording()
    rec.add(_click(
        "http://127.0.0.1:5000/member/10023",
        "http://127.0.0.1:5000/member/10023/subacct/new",
        verified=False, success_text="Open New Sub-Account",  # model misphrased
    ))
    cap = build_capability(_spec(), rec, model_name="m", run_id="r", gate=_gate())
    assert len(cap.steps) == 1
    # The wrong expectation must NOT become a recorded checkpoint...
    assert cap.steps[0].checkpoint.text is None
    # ...but the observed URL transition (parameterized) must.
    assert "subacct/new" in cap.steps[0].checkpoint.url_pattern
    assert "[^/?#]+" in cap.steps[0].checkpoint.url_pattern


def test_noop_click_is_excluded():
    # A capability needs >=1 step (schema contract), so pair the no-op with
    # one real action and assert only the real one survives recording.
    rec = Recording()
    same = "http://127.0.0.1:5000/member/10023"
    rec.add(_click(same, same, verified=False))  # no-op: dropped
    rec.add(_click(
        same, "http://127.0.0.1:5000/member/10023/subacct/new",
        verified=True, success_text="Open Sub-Account",
    ))
    cap = build_capability(_spec(), rec, model_name="m", run_id="r", gate=_gate())
    assert len(cap.steps) == 1
    assert "subacct/new" in cap.steps[0].checkpoint.url_pattern


def test_model_reasoning_is_parameterized_in_descriptions():
    rec = Recording()
    rec.add(_click(
        "http://127.0.0.1:5000/",
        "http://127.0.0.1:5000/member/10023",
        verified=True, success_text="Member Profile",
        reason="Click Search to look up member 10023",
    ))
    cap = build_capability(_spec(), rec, model_name="m", run_id="r", gate=_gate())
    assert "10023" not in cap.model_dump_json()
    assert "{member_id}" in cap.steps[0].description


def test_run_specific_expectation_text_is_stripped_even_if_verified():
    rec = Recording()
    rec.add(_click(
        "http://127.0.0.1:5000/",
        "http://127.0.0.1:5000/member/10023",
        verified=True, success_text="Profile of member 10023",
    ))
    cap = build_capability(_spec(), rec, model_name="m", run_id="r", gate=_gate())
    assert cap.steps[0].checkpoint.text is None
