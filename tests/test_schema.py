"""Artifact schema contract tests: round-trip + cross-reference validation."""

import json

import pytest
from pydantic import ValidationError

from cua.schema import (
    ActionType,
    Capability,
    DiscoveryProvenance,
    ElementTarget,
    InputParam,
    Locator,
    LocatorStrategy,
    OutputField,
    StateProbe,
    Step,
    utc_now,
)


def _target() -> ElementTarget:
    return ElementTarget(
        description="the Search button",
        locators=[
            Locator(strategy=LocatorStrategy.ROLE, value="button", name="Search"),
            Locator(strategy=LocatorStrategy.CSS, value='input[type="submit"]'),
        ],
    )


def _step(step_id="s01", **overrides) -> Step:
    base = dict(
        id=step_id,
        description="click search",
        action=ActionType.CLICK,
        target=_target(),
        checkpoint=StateProbe(text="Member Profile", description="profile shown"),
    )
    base.update(overrides)
    return Step(**base)


def _capability(**overrides) -> Capability:
    base = dict(
        capability_id="cap_test",
        name="Test capability",
        description="does a thing",
        provenance=DiscoveryProvenance(
            goal="g", model="m", run_id="r", discovered_at=utc_now()
        ),
        entry_url_template="http://127.0.0.1:5000/",
        inputs=[InputParam(name="member_id", description="member number")],
        steps=[_step()],
    )
    base.update(overrides)
    return Capability(**base)


def test_round_trip_preserves_everything():
    cap = _capability(
        steps=[
            _step(),
            _step("s02", action=ActionType.FILL, value_template="{member_id}"),
        ]
    )
    loaded = Capability.model_validate_json(cap.model_dump_json())
    assert loaded == cap
    # And it is genuinely plain JSON on disk.
    parsed = json.loads(cap.model_dump_json())
    assert parsed["schema_version"] == "1.0"


def test_duplicate_step_ids_rejected():
    with pytest.raises(ValidationError, match="unique"):
        _capability(steps=[_step("dup"), _step("dup")])


def test_undeclared_template_reference_rejected():
    with pytest.raises(ValidationError, match="undeclared input"):
        _capability(
            steps=[_step(action=ActionType.FILL, value_template="{no_such_param}")]
        )


def test_output_must_reference_existing_step():
    with pytest.raises(ValidationError, match="unknown step"):
        _capability(
            outputs=[
                OutputField(
                    name="x", description="d", source_step_id="missing_step"
                )
            ]
        )


def test_signature_reads_like_a_function():
    cap = _capability(
        outputs=[OutputField(name="ref", description="d", source_step_id="s01")]
    )
    assert cap.signature() == "cap_test(member_id: string) -> {ref: string}"


def test_locator_chain_requires_at_least_one():
    with pytest.raises(ValidationError):
        ElementTarget(description="empty", locators=[])
