"""Submission-service cross-cutting and safety regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.schema import Capability
from meridian_service.app import app
from meridian_service.platform import (
    CircuitBreaker,
    CircuitOpen,
    IdempotencyStore,
    SlidingWindowRateLimiter,
    TTLCache,
)

ROOT = Path(__file__).resolve().parents[1]


def test_rate_limiter_rejects_above_window_budget():
    limiter = SlidingWindowRateLimiter()
    assert limiter.check("customer", limit=2, window_seconds=60).allowed
    assert limiter.check("customer", limit=2, window_seconds=60).allowed
    denied = limiter.check("customer", limit=2, window_seconds=60)
    assert not denied.allowed and denied.retry_after_seconds > 0


def test_ttl_cache_loads_once_before_expiry():
    cache: TTLCache[list[str]] = TTLCache(ttl_seconds=60)
    calls = 0

    def load():
        nonlocal calls
        calls += 1
        return ["capability"]

    assert cache.get_or_load(load) == ["capability"]
    assert cache.get_or_load(load) == ["capability"]
    assert calls == 1


def test_circuit_breaker_opens_and_recovers_after_reset():
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=0)

    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert breaker.state == "half_open"
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.state == "closed"


def test_circuit_breaker_fails_fast_while_open():
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=60)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(CircuitOpen):
        breaker.call(lambda: "must not run")


def test_idempotency_store_reuses_same_request_and_rejects_conflict():
    store: IdempotencyStore[str] = IdempotencyStore()
    store.put("key", "fingerprint-a", "run-1")
    assert store.get("key", "fingerprint-a") == "run-1"
    with pytest.raises(ValueError, match="different request"):
        store.get("key", "fingerprint-b")


def test_catalog_never_exposes_legacy_credentials():
    client = app.test_client()
    response = client.get("/api/capabilities")
    assert response.status_code == 200
    for capability in response.get_json():
        names = {item["name"] for item in capability["inputs"]}
        assert "operator_id" not in names
        assert "password" not in names


def test_client_cannot_supply_infrastructure_credentials():
    client = app.test_client()
    response = client.post(
        "/api/capabilities/meridian_member_balance/invoke",
        json={"params": {"operator_id": "attacker", "password": "secret"}},
    )
    assert response.status_code == 400
    assert "infrastructure-owned" in response.get_json()["error"]


def test_mutation_requires_idempotency_key(monkeypatch):
    monkeypatch.delenv("PUBLIC_DEMO_READ_ONLY", raising=False)
    monkeypatch.setenv("MERIDIAN_OPERATOR_ID", "server-operator")
    monkeypatch.setenv("MERIDIAN_OPERATOR_PASSWORD", "server-secret")
    client = app.test_client()
    response = client.post(
        "/api/capabilities/meridian_update_member_info/invoke",
        json={
            "params": {
                "member_id": "100001",
                "email": "member@example.com",
                "phone": "555-0100",
                "mailing_address": "1 Main St",
            },
            "confirm_risky": False,
        },
    )
    assert response.status_code == 400
    assert response.get_json()["error"].startswith("Idempotency-Key")


def test_public_demo_blocks_mutations_before_execution(monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO_READ_ONLY", "true")
    client = app.test_client()
    response = client.post(
        "/api/capabilities/meridian_update_member_info/invoke",
        json={
            "params": {
                "member_id": "100001",
                "email": "member@example.com",
                "phone": "555-0100",
                "mailing_address": "1 Main St",
            }
        },
    )
    assert response.status_code == 403
    assert response.get_json()["error"] == "public_demo_read_only"


def test_meridian_mutations_have_only_commit_actions_marked_risky():
    """Each mutating capability must mark exactly one step RISKY, and it
    must be the actual commit/post click -- not a navigation link into the
    flow. Step id *numbers* legitimately shift across re-recordings (they
    depend on how many actions a given discovery run kept), so this checks
    the stable part: there is exactly one risky step, it's a click, and its
    id names the real commit action."""
    expected = {
        "funds_transfer.json": "click_post_transfer",
        "place_account_hold.json": "click_apply_hold",
        "open_new_share.json": "click_open_share",
        "update_member_info.json": "click_save_changes",
    }
    for filename, expected_suffix in expected.items():
        path = ROOT / "artifacts" / "meridian_core" / filename
        capability = Capability.model_validate_json(path.read_text())
        risky = [step for step in capability.steps if step.risk.value == "risky"]
        assert len(risky) == 1, f"{filename}: expected exactly one risky step, got {risky}"
        assert risky[0].id.endswith(expected_suffix), (
            f"{filename}: risky step {risky[0].id!r} does not match the "
            f"expected commit action {expected_suffix!r}"
        )
