"""Customer login, session scoping, and multi-backend registry tests.

These pin down the security properties the customer surface was built
around, all verified live at build time but previously untested:

* chat requires a logged-in customer session;
* a logged-in customer's member_id is server-bound from the session --
  never offered to the routing model, never accepted from the client;
* each customer's chat tool catalog is scoped to their own backend
  system only;
* login only accepts known members with the demo password;
* the multi-backend registry keeps the two systems' capabilities,
  policies, and credential-bound inputs separate.

No browser, no LLM, no network: everything here goes through the Flask
test client and the module's own functions.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

service_app = importlib.import_module("meridian_service.app")

from meridian_service.app import (
    KNOWN_CUSTOMERS,
    SYSTEMS,
    _bind_system_credentials,
    _catalog_tools,
    _customer_profile,
    _load_customer_home,
    _public_demo_route,
    _rank_capability_artifacts,
    _route_customer_message,
    app,
    customer_system,
    find_capability_system,
    get_capability,
    list_capabilities,
)
from cua.replay.engine import ParamError


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    return app.test_client()


def _login(client, member_id: str):
    return client.post(
        "/customer/login",
        data={"member_id": member_id, "password": "password"},
    )


# ------------------------------------------------------------------ login


def test_login_rejects_unknown_member(client):
    response = _login_with(client, "999999", "password")
    assert response.status_code == 401


def test_login_rejects_wrong_password(client):
    response = _login_with(client, "100987", "wrong")
    assert response.status_code == 401


def _login_with(client, member_id: str, password: str):
    return client.post(
        "/customer/login",
        data={"member_id": member_id, "password": password},
    )


def test_login_accepts_known_member_and_sets_session(client):
    response = _login(client, "20001")
    assert response.status_code == 302
    assert "/customer/home" in response.headers["Location"]


def test_logout_clears_session(client):
    _login(client, "20001")
    client.get("/customer/logout")
    response = client.get("/customer/home")
    assert response.status_code == 302
    assert "/customer/login" in response.headers["Location"]


def test_home_redirects_to_login_when_anonymous(client):
    response = client.get("/customer/home")
    assert response.status_code == 302
    assert "/customer/login" in response.headers["Location"]


# ------------------------------------------------------------------- chat


def test_chat_requires_login(client):
    response = client.post("/api/chat", json={"message": "check my balance"})
    assert response.status_code == 401
    assert response.get_json()["login_required"] is True


def test_public_demo_routes_meridian_login_to_synthetic_backend(monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO_READ_ONLY", "true")
    monkeypatch.setenv("PUBLIC_DEMO_SYNTHETIC", "true")
    profile = _customer_profile("100987")
    assert profile["system"] == "mock_app"
    assert profile["member_id"] == "100987"
    assert profile["accounts"] == KNOWN_CUSTOMERS["100987"]["accounts"]


def test_public_demo_home_uses_fast_synthetic_overview(monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO_READ_ONLY", "true")
    monkeypatch.setenv("PUBLIC_DEMO_SYNTHETIC", "true")
    overview = _load_customer_home("100987")
    assert overview["member_name"] == "Lee, Jordan"
    assert [account["balance"] for account in overview["accounts"]] == [
        "$8,420.17",
        "$2,195.44",
    ]
    assert all(account["ok"] for account in overview["accounts"])


def test_customer_home_embeds_only_session_scoped_capability_options(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO_READ_ONLY", "true")
    monkeypatch.setenv("PUBLIC_DEMO_SYNTHETIC", "true")
    _login(client, "100987")
    response = client.get("/customer/home")
    assert response.status_code == 200
    assert b"Check the balance of a specific member account" in response.data
    assert b"Place a restricted hold on a member account" in response.data
    assert b"Check the balance of a specific member share" not in response.data
    assert b"Show ${Math.min(CAPABILITY_PAGE_SIZE, remaining)} more options" in response.data


def test_public_demo_router_handles_safe_catalog_and_balance_requests():
    profile = {
        "member_id": "100987",
        "system": "mock_app",
        "accounts": ["100987-MMKT-11"],
    }
    catalog = _public_demo_route("Show available banking capabilities", "100987", profile)
    assert catalog["status"] == 200
    assert "read-only" in catalog["reply"]

    balance = _public_demo_route(
        "Check the balance for account 100987-MMKT-11", "100987", profile
    )
    assert balance["capability_id"] == "mock_member_balance"
    assert balance["input"] == {"account_no": "100987-MMKT-11"}


def test_artifact_ranker_understands_natural_account_name_request():
    profile = {
        "member_id": "100987",
        "system": "mock_app",
        "accounts": ["100987-MMKT-11", "100987-S0001-9"],
    }
    routed = _public_demo_route("What is my account name?", "100987", profile)
    assert routed["capability_id"] == "mock_member_inquiry"
    assert routed["input"] == {}

    ranked = _rank_capability_artifacts("What is my account name?", "mock_app")
    assert ranked[0][0].capability_id == "mock_member_inquiry"
    assert ranked[0][1] > ranked[1][1]


def test_artifact_ranker_asks_specific_question_when_information_is_missing():
    profile = {
        "member_id": "100987",
        "system": "mock_app",
        "accounts": ["100987-MMKT-11", "100987-S0001-9"],
    }
    routed = _public_demo_route("Check my balance", "100987", profile)
    assert routed["clarification_required"] is True
    assert routed["pending_capability_id"] == "mock_member_balance"
    assert "Which account" in routed["reply"]
    assert "100987-MMKT-11" in routed["reply"]


def test_artifact_ranker_does_not_guess_an_unclear_request():
    profile = {
        "member_id": "100987",
        "system": "mock_app",
        "accounts": ["100987-MMKT-11", "100987-S0001-9"],
    }
    routed = _public_demo_route("Can you handle this for me?", "100987", profile)
    assert routed["clarification_required"] is True
    assert "not certain" in routed["reply"]


def test_router_prefers_llm_when_key_is_configured(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-sent")
    monkeypatch.setenv("PUBLIC_DEMO_SYNTHETIC", "true")
    profile = {
        "member_id": "100987",
        "system": "mock_app",
        "accounts": ["100987-MMKT-11"],
    }
    monkeypatch.setattr(
        service_app,
        "_llm_chat_route",
        lambda message, system_key: {
            "capability_id": "mock_member_inquiry",
            "input": {},
        },
    )
    routed = _route_customer_message("What is my account name?", "100987", profile)
    assert routed["capability_id"] == "mock_member_inquiry"


def test_router_falls_back_to_artifact_ranking_when_llm_fails(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "invalid-test-key")
    monkeypatch.setenv("PUBLIC_DEMO_SYNTHETIC", "true")
    profile = {
        "member_id": "100987",
        "system": "mock_app",
        "accounts": ["100987-MMKT-11"],
    }

    def fail_router(message, system_key):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(service_app, "_llm_chat_route", fail_router)
    routed = _route_customer_message("What is my account name?", "100987", profile)
    assert routed["capability_id"] == "mock_member_inquiry"


def test_public_demo_router_rejects_another_member():
    profile = {
        "member_id": "100987",
        "system": "mock_app",
        "accounts": ["100987-MMKT-11"],
    }
    routed = _public_demo_route("Look up member 100234", "100987", profile)
    assert routed["status"] == 403
    assert "current session" in routed["reply"]


def test_public_demo_chat_uses_session_bound_synthetic_member(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO_READ_ONLY", "true")
    monkeypatch.setenv("PUBLIC_DEMO_SYNTHETIC", "true")
    captured = {}

    def fake_start(system_key, capability, params, **kwargs):
        captured.update(system=system_key, capability=capability.capability_id, params=params)
        return SimpleNamespace(run_id="replay-test")

    monkeypatch.setattr(service_app, "start_replay", fake_start)
    monkeypatch.setattr(
        service_app,
        "_await_result",
        lambda run_id: {
            "status": "done",
            "result": {
                "status": "success",
                "outputs": {"account_balance": "$8,420.17", "account_status": "OPEN"},
            },
        },
    )
    _login(client, "100987")
    response = client.post(
        "/api/chat",
        json={"message": "Check the balance for account 100987-MMKT-11"},
    )
    assert response.status_code == 200
    assert captured == {
        "system": "mock_app",
        "capability": "mock_member_balance",
        "params": {"account_no": "100987-MMKT-11", "member_id": "100987"},
    }
    assert "$8,420.17" in response.get_json()["reply"]


def test_public_demo_blocks_hold_before_requesting_account(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO_READ_ONLY", "true")
    monkeypatch.setenv("PUBLIC_DEMO_SYNTHETIC", "true")

    def replay_must_not_start(*args, **kwargs):
        raise AssertionError("public write request reached browser replay")

    monkeypatch.setattr(service_app, "start_replay", replay_must_not_start)
    _login(client, "100987")
    response = client.post(
        "/api/chat",
        json={"message": "Place a hold on my account"},
    )
    body = response.get_json()
    assert response.status_code == 403
    assert body["capability_id"] == "mock_place_hold"
    assert "Writes are disabled" in body["reply"]
    assert "Which account" not in body["reply"]
    run = client.get("/api/runs").get_json()[0]
    assert run["capability_id"] == "mock_place_hold"
    assert run["status"] == "policy_blocked"
    assert run["phase"] == "policy"


def test_account_only_reply_continues_pending_balance_request(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO_READ_ONLY", "true")
    monkeypatch.setenv("PUBLIC_DEMO_SYNTHETIC", "true")
    captured = {}

    def fake_start(system_key, capability, params, **kwargs):
        captured.update(capability=capability.capability_id, params=params)
        return SimpleNamespace(run_id="replay-pending-account")

    monkeypatch.setattr(service_app, "start_replay", fake_start)
    monkeypatch.setattr(
        service_app,
        "_await_result",
        lambda run_id: {
            "status": "done",
            "result": {
                "status": "success",
                "outputs": {"account_balance": "$2,195.44", "account_status": "OPEN"},
            },
        },
    )
    _login(client, "100987")

    clarification = client.post(
        "/api/chat", json={"message": "Check my balance"}
    )
    assert clarification.status_code == 200
    assert clarification.get_json()["clarification_required"] is True
    pending_run = client.get("/api/runs").get_json()[0]
    assert pending_run["capability_id"] == "mock_member_balance"
    assert pending_run["status"] == "awaiting_customer"

    response = client.post(
        "/api/chat", json={"message": "100987-S0001-9"}
    )
    assert response.status_code == 200
    assert captured == {
        "capability": "mock_member_balance",
        "params": {"account_no": "100987-S0001-9", "member_id": "100987"},
    }
    assert "$2,195.44" in response.get_json()["reply"]
    resumed_run = client.get("/api/runs").get_json()[0]
    assert resumed_run["run_id"] == pending_run["run_id"]


def test_chat_suggests_exact_owned_account_for_typo_without_replay(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO_READ_ONLY", "true")
    monkeypatch.setenv("PUBLIC_DEMO_SYNTHETIC", "true")

    def replay_must_not_start(*args, **kwargs):
        raise AssertionError("invalid account input reached browser replay")

    monkeypatch.setattr(service_app, "start_replay", replay_must_not_start)
    _login(client, "100987")
    response = client.post(
        "/api/chat",
        json={"message": "Check the balance for account 100987-S00019"},
    )
    body = response.get_json()
    assert response.status_code == 200
    assert body["clarification_required"] is True
    assert "Did you mean 100987-S0001-9?" in body["reply"]
    assert "Nothing was executed" in body["reply"]


def test_chat_can_route_the_second_owned_account(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO_READ_ONLY", "true")
    monkeypatch.setenv("PUBLIC_DEMO_SYNTHETIC", "true")
    captured = {}

    def fake_start(system_key, capability, params, **kwargs):
        captured.update(params=params)
        return SimpleNamespace(run_id="replay-second-account")

    monkeypatch.setattr(service_app, "start_replay", fake_start)
    monkeypatch.setattr(
        service_app,
        "_await_result",
        lambda run_id: {
            "status": "done",
            "result": {
                "status": "success",
                "outputs": {"account_balance": "$2,195.44", "account_status": "OPEN"},
            },
        },
    )
    _login(client, "100987")
    response = client.post(
        "/api/chat",
        json={"message": "Check the balance for account 100987-S0001-9"},
    )
    assert response.status_code == 200
    assert captured["params"]["account_no"] == "100987-S0001-9"
    assert "$2,195.44" in response.get_json()["reply"]


def test_chat_tool_schema_never_offers_member_id():
    """The routing model must not even see member_id as a parameter --
    identity comes from the session, not the conversation."""
    for system_key in SYSTEMS:
        for tool in _catalog_tools(system_key, hide_member_id=True):
            assert "member_id" not in tool["input_schema"]["properties"], (
                f"{tool['name']} exposes member_id to the routing model"
            )


def test_chat_tool_schema_never_offers_credentials():
    for system_key, system in SYSTEMS.items():
        for tool in _catalog_tools(system_key, hide_member_id=True):
            offered = set(tool["input_schema"]["properties"])
            leaked = offered & set(system.credential_bound_inputs)
            assert not leaked, f"{tool['name']} exposes {leaked}"


def test_catalog_tools_scoped_to_customer_system():
    """A mock_app customer's chat must never see MERIDIAN capabilities,
    and vice versa."""
    mock_tools = {t["name"] for t in _catalog_tools("mock_app", hide_member_id=True)}
    meridian_tools = {t["name"] for t in _catalog_tools("meridian_core", hide_member_id=True)}
    assert mock_tools and meridian_tools
    assert not mock_tools & meridian_tools
    assert all(name.startswith("mock_") for name in mock_tools)
    assert all(name.startswith("meridian_") for name in meridian_tools)


# ------------------------------------------------- multi-backend registry


def test_every_known_customer_maps_to_a_registered_system():
    for member_id, info in KNOWN_CUSTOMERS.items():
        assert info["system"] in SYSTEMS, f"{member_id} names unknown system"
        assert info["accounts"], f"{member_id} has no home-page accounts"
        assert customer_system(member_id) is SYSTEMS[info["system"]]


def test_capability_ids_are_namespaced_and_unique_across_systems():
    all_ids = [c.capability_id for c in list_capabilities()]
    assert len(all_ids) == len(set(all_ids)), "duplicate capability_id across systems"


def test_find_capability_system_resolves_both_backends():
    assert find_capability_system("meridian_member_balance") == "meridian_core"
    assert find_capability_system("mock_member_balance") == "mock_app"
    assert find_capability_system("nonexistent_capability") is None


def test_get_capability_respects_system_scope():
    assert get_capability("mock_member_balance", "meridian_core") is None
    assert get_capability("meridian_member_balance", "mock_app") is None
    assert get_capability("mock_member_balance", "mock_app") is not None


def test_mock_app_has_no_credential_bound_inputs():
    """mock_app has no sign-on at all; binding credentials for it must be
    a no-op, and clients may not smuggle 'credentials' in anyway."""
    cap = get_capability("mock_member_balance", "mock_app")
    params = _bind_system_credentials(
        "mock_app", cap, {"member_id": "20001", "account_no": "712280-S00"}
    )
    assert params == {"member_id": "20001", "account_no": "712280-S00"}


def test_meridian_rejects_client_supplied_credentials():
    cap = get_capability("meridian_member_balance", "meridian_core")
    with pytest.raises(ParamError, match="infrastructure-owned"):
        _bind_system_credentials(
            "meridian_core", cap, {"member_id": "100987", "password": "hacked"}
        )


def test_api_capabilities_labels_each_system(client):
    response = client.get("/api/capabilities")
    assert response.status_code == 200
    listed = response.get_json()
    systems = {c["system"] for c in listed}
    assert systems == set(SYSTEMS)
    for c in listed:
        names = {item["name"] for item in c["inputs"]}
        assert not names & set(SYSTEMS[c["system"]].credential_bound_inputs)


# --------------------------------------------------------------- employee


def test_employee_login_rejects_unknown_or_wrong_password(client):
    assert client.post("/employee/login", data={"employee_id": "nobody", "password": "password"}).status_code == 401
    assert client.post("/employee/login", data={"employee_id": "teller1", "password": "wrong"}).status_code == 401


def test_employee_login_accepts_known_employee(client):
    response = client.post("/employee/login", data={"employee_id": "teller1", "password": "password"})
    assert response.status_code == 302


def test_run_command_requires_employee_login(client, monkeypatch):
    monkeypatch.delenv("PUBLIC_DEMO_READ_ONLY", raising=False)
    response = client.post("/api/runs/some-run/command", json={"command": "approve"})
    assert response.status_code == 401
    assert response.get_json()["error"] == "employee_login_required"


def test_run_command_allows_logged_in_employee_past_auth(client, monkeypatch):
    """Auth passes and the request proceeds to run lookup (404 for an
    unknown run) rather than being rejected at the login gate."""
    monkeypatch.delenv("PUBLIC_DEMO_READ_ONLY", raising=False)
    client.post("/employee/login", data={"employee_id": "super1", "password": "password"})
    response = client.post("/api/runs/some-run/command", json={"command": "approve"})
    assert response.status_code == 404


def test_customer_login_preserves_employee_session(client):
    client.post("/employee/login", data={"employee_id": "teller1", "password": "password"})
    client.post("/customer/login", data={"member_id": "20001", "password": "password"})
    dashboard = client.get("/")
    assert b"teller1" in dashboard.data


def test_mock_mutations_have_only_commit_actions_marked_risky():
    """Same invariant already enforced for meridian_core artifacts: each
    mutating mock_app capability marks exactly one step RISKY and it is
    the actual commit action, not a navigation link."""
    from pathlib import Path

    from cua.schema import Capability

    root = Path(__file__).resolve().parents[1]
    expected = {
        "transfer_funds.json": "click_post_transfer",
        "update_contact_info.json": "click_save_changes",
        "close_account.json": "click_close_account",
        "loan_application.json": "click_submit_application",
        "bill_pay.json": "click_submit_payment",
        "place_hold.json": "click_place_hold",
        "card_lock.json": "click_lock_card",
    }
    for filename, expected_suffix in expected.items():
        path = root / "artifacts" / "mock_app" / filename
        capability = Capability.model_validate_json(path.read_text())
        risky = [s for s in capability.steps if s.risk.value == "risky"]
        assert len(risky) == 1, f"{filename}: expected exactly one risky step, got {[s.id for s in risky]}"
        assert risky[0].id.endswith(expected_suffix), (
            f"{filename}: risky step {risky[0].id!r} != expected {expected_suffix!r}"
        )
