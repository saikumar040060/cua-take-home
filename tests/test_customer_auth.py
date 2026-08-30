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

import pytest

from meridian_service.app import (
    KNOWN_CUSTOMERS,
    SYSTEMS,
    _bind_system_credentials,
    _catalog_tools,
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
