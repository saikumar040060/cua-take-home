"""Policy allowlist, risk classification, and redaction tests."""

import json

import pytest

from cua.safety.policy import Policy, PolicyGate, PolicyViolation
from cua.safety.redact import Redactor, mask_identifier
from cua.schema import RiskLevel


@pytest.fixture()
def gate() -> PolicyGate:
    return PolicyGate(
        Policy(
            allowed_origins=["http://127.0.0.1:5000"],
            allowed_path_prefixes=["/"],
            blocked_path_prefixes=["/admin"],
        )
    )


class TestAllowlist:
    def test_allowed_url_passes(self, gate):
        gate.check_action("click", "http://127.0.0.1:5000/member/1")

    def test_foreign_origin_blocked(self, gate):
        with pytest.raises(PolicyViolation, match="origin"):
            gate.check_action("navigate", "https://evil.example.com/")

    def test_same_host_wrong_port_blocked(self, gate):
        with pytest.raises(PolicyViolation, match="origin"):
            gate.check_action("navigate", "http://127.0.0.1:9999/")

    def test_blocked_path_prefix_wins(self, gate):
        with pytest.raises(PolicyViolation, match="blocked"):
            gate.check_action("click", "http://127.0.0.1:5000/admin/panel")

    def test_disallowed_action_type_blocked(self, gate):
        with pytest.raises(PolicyViolation, match="action type"):
            gate.check_action("upload", "http://127.0.0.1:5000/")


class TestRiskClassification:
    def test_submit_like_click_is_risky(self, gate):
        assert gate.classify("click", "Create Sub-Account") == RiskLevel.RISKY

    def test_search_click_is_safe(self, gate):
        assert gate.classify("click", "Search") == RiskLevel.SAFE

    def test_fill_is_safe_even_with_scary_name(self, gate):
        assert gate.classify("fill", "Create Sub-Account") == RiskLevel.SAFE


class TestRedaction:
    def test_mask_identifier_keeps_ends(self):
        assert mask_identifier("10023") == "1***3"
        assert mask_identifier("ab") == "**"

    def test_registered_identifier_masked_everywhere(self):
        r = Redactor()
        r.register_identifier("10023")
        assert "10023" not in r.scrub("member 10023 was viewed")
        assert "1***3" in r.scrub("member 10023 was viewed")

    def test_secret_fully_removed(self):
        r = Redactor()
        r.register_secret("hunter2")
        assert r.scrub("password is hunter2") == "password is [REDACTED]"

    def test_pattern_scrubbing_without_registration(self):
        r = Redactor()
        text = "SSN ***-**-4417 acct 724401-S01 phone (313) 555-0164"
        scrubbed = r.scrub(text)
        assert "4417" not in scrubbed
        assert "724401" not in scrubbed
        assert "555-0164" not in scrubbed

    def test_scrub_obj_reaches_nested_structures(self):
        r = Redactor()
        r.register_identifier("10023")
        obj = {"a": ["member 10023", {"b": "10023"}]}
        scrubbed = json.dumps(r.scrub_obj(obj))
        assert "10023" not in scrubbed
