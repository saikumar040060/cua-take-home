"""Redaction — applied at every serialization boundary.

Two complementary mechanisms:

1. **Value-aware redaction.** Runtime parameter values whose declared
   ``Sensitivity`` is ``IDENTIFIER`` are masked (first + last char kept)
   wherever they appear in logged text; ``SECRET`` values are removed
   entirely and are additionally *rejected* from ever being persisted in
   artifacts (values in artifacts are templates, never literals).

2. **Pattern-based scrubbing.** Regardless of declared sensitivity, text
   that flows into logs/evidence (page text excerpts, DOM snapshots, LLM
   observations) is scrubbed for shapes that look like regulated data:
   SSN fragments, account numbers, phone numbers, card-like digit runs.

The redactor is injected into the run logger, so nothing reaches disk
without passing through it. This is deliberately a choke point: one code
path to audit, not N call sites to remember.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # SSN forms: 123-45-6789 or masked fragments ***-**-1234
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\*{3}-\*{2}-\d{4}"), "[SSN]"),
    # Account numbers like 724401-S01
    (re.compile(r"\b\d{6}-[A-Z]\d{2}\b"), "[ACCT]"),
    # Card-like digit runs (13-19 digits, allowing separators)
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[NUM]"),
    # US phone numbers
    (re.compile(r"\(\d{3}\)\s?\d{3}-\d{4}"), "[PHONE]"),
]


def mask_identifier(value: str) -> str:
    """Mask a business identifier, keeping just enough to correlate runs.

    '10023' -> '1***3'; short values are fully masked.
    """
    if len(value) <= 2:
        return "*" * len(value)
    return value[0] + "*" * (len(value) - 2) + value[-1]


class Redactor:
    """Scrubs sensitive values and sensitive-looking patterns from text."""

    def __init__(self) -> None:
        self._identifier_values: dict[str, str] = {}
        self._secret_values: set[str] = set()

    def register_identifier(self, value: str) -> None:
        if value:
            self._identifier_values[value] = mask_identifier(value)

    def register_secret(self, value: str) -> None:
        if value:
            self._secret_values.add(value)

    def scrub(self, text: str) -> str:
        if not text:
            return text
        for secret in self._secret_values:
            text = text.replace(secret, "[REDACTED]")
        for value, masked in self._identifier_values.items():
            text = text.replace(value, masked)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def scrub_obj(self, obj):
        """Recursively scrub strings inside dicts/lists (for JSONL events)."""
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, dict):
            return {k: self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.scrub_obj(v) for v in obj]
        return obj
