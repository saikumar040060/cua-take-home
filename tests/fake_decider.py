"""Scripted Decider — TEST DOUBLE ONLY.

Used by the test suite to exercise the agent loop, executor, recorder,
policy gate and escalation deterministically, without the network. It is
never wired into the CLI: real discovery runs (`python -m cua discover`)
always use ``AnthropicDecider``, and the /evidence/ discovery run is a
genuine LLM run as the brief requires.
"""

from __future__ import annotations

import re

from cua.discovery.decider import Decision


class FakeDecider:
    def __init__(self, script: list[Decision]):
        self.script = list(script)
        self.calls: list[list[dict]] = []

    @property
    def model_name(self) -> str:
        return "fake-scripted-decider"

    def decide(self, messages: list[dict]) -> Decision:
        self.calls.append(messages)
        if not self.script:
            return Decision(tool="blocked", args={"reason": "script exhausted"})
        decision = self.script.pop(0)
        # Placeholder refs of the form '@cell:<Label>' are resolved against
        # the latest observation, mimicking how the real model picks refs.
        ref = decision.args.get("ref", "")
        if ref.startswith("@cell:"):
            label = ref.split(":", 1)[1]
            observation = self._latest_text(messages)
            m = re.search(rf'\[(e\d+)\] cell "{re.escape(label)}"', observation)
            if m:
                decision.args["ref"] = m.group(1)
        return decision

    @staticmethod
    def _latest_text(messages: list[dict]) -> str:
        for message in reversed(messages):
            content = message.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "content" in block:
                        return str(block["content"])
        return ""


def open_sub_account_script(member_id: str = "10023") -> list[Decision]:
    """The happy-path action sequence for the open_sub_account spec.

    Refs (e1, e2, ...) follow the deterministic observation order of the
    mock app's pages. If the mock app's markup changes, update here.
    """
    return [
        # Home: e1 = Member No input, e2 = Search button
        Decision(tool="fill", args={
            "ref": "e1", "value": member_id,
            "reason": "enter the member number in the lookup field",
        }),
        Decision(tool="click", args={
            "ref": "e2", "reason": "run the member search",
            "success_text": "Member Profile",
        }),
        # Member page: e1/e2 = per-account History links (member 10023 has 2
        # accounts), e3 = Open Sub-Account link, followed by the other
        # account-action links, then New Lookup.
        Decision(tool="click", args={
            "ref": "e3", "reason": "start the sub-account flow",
            "success_text": "Open Sub-Account",
        }),
        # Form: e1 = product select, e2 = nickname, e3 = deposit, e4 = submit
        Decision(tool="select", args={
            "ref": "e1", "value": "Holiday Club",
            "reason": "choose the product type",
        }),
        Decision(tool="fill", args={
            "ref": "e2", "value": "Winter savings",
            "reason": "enter the nickname",
        }),
        Decision(tool="fill", args={
            "ref": "e3", "value": "25.00",
            "reason": "enter the initial deposit",
        }),
        Decision(tool="click", args={
            "ref": "e4", "reason": "submit the sub-account form",
            "success_text": "Sub-Account Created",
        }),
        # Confirmation: read the ref cell then finish
        Decision(tool="read", args={
            "ref": "@cell:Confirmation Ref", "output_name": "confirmation_ref",
            "reason": "capture the confirmation reference",
        }),
        Decision(tool="done", args={
            "summary": "sub-account created and confirmation reference read",
        }),
    ]
