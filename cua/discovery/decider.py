"""The "decide" of observe -> decide -> act.

``Decider`` is a narrow interface so the agent loop is testable without the
network: ``AnthropicDecider`` is the real implementation (tool-calling
against the Anthropic API — structured actions, never free-text parsing);
tests use a scripted stand-in. The genuine discovery evidence run always
uses ``AnthropicDecider`` — that run is required to be real.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

TOOLS: list[dict] = [
    {
        "name": "navigate",
        "description": "Navigate the browser to a URL within the permitted application.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "reason": {"type": "string", "description": "Why this action progresses the goal."},
                "success_text": {"type": "string", "description": "Short text you expect to be visible after the action succeeds (used as a checkpoint)."},
            },
            "required": ["url", "reason", "success_text"],
        },
    },
    {
        "name": "click",
        "description": "Click an interactive element identified by its ref from the observation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Element ref, e.g. 'e3'."},
                "reason": {"type": "string"},
                "success_text": {"type": "string", "description": "Short text expected to be visible after the click succeeds."},
            },
            "required": ["ref", "reason", "success_text"],
        },
    },
    {
        "name": "fill",
        "description": "Clear a text field and type a value into it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "value": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["ref", "value", "reason"],
        },
    },
    {
        "name": "select",
        "description": "Select an option (by value) in a dropdown.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "value": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["ref", "value", "reason"],
        },
    },
    {
        "name": "read",
        "description": "Read the visible text of an element and record it as a named output of the capability.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "output_name": {"type": "string", "description": "Which declared output this populates."},
                "reason": {"type": "string"},
            },
            "required": ["ref", "output_name", "reason"],
        },
    },
    {
        "name": "done",
        "description": "Declare the goal complete (only after all required outputs were read).",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "What was accomplished."},
            },
            "required": ["summary"],
        },
    },
    {
        "name": "blocked",
        "description": "Declare that you cannot safely proceed and need a human operator.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
]

SYSTEM_PROMPT = """You are a computer-use agent operating a bank back-office web application on behalf of an operations team. You will be given a goal, the concrete input values for this run, and after every action a fresh observation of the page (URL, interactive elements with refs, visible text).

Rules:
- Work strictly toward the stated goal with the minimum number of actions.
- Interact only through the provided tools, referencing elements by their ref.
- The application is legacy-style; identify controls by their visible labels and roles.
- For click/navigate provide success_text: a short, distinctive text you expect on the resulting page if the action worked. It becomes a permanent checkpoint in a recorded automation, so choose stable wording from the UI, never a value specific to this run (no member numbers, names, or dollar amounts).
- If an unexpected page interrupts the flow (e.g. a session notice), deal with it, then continue.
- If a required output is declared, use read on the element containing it before calling done.
- If you cannot proceed safely, or the page is in an unrecognizable/error state, call blocked instead of guessing.
- Never navigate outside the application's origin."""


@dataclass
class Decision:
    tool: str
    args: dict
    raw_assistant_content: list | None = None  # Anthropic content blocks
    tool_use_id: str | None = None
    text: str = ""  # any thinking-out-loud text the model emitted


class Decider(Protocol):
    def decide(self, messages: list[dict]) -> Decision: ...

    @property
    def model_name(self) -> str: ...


class AnthropicDecider:
    """Real LLM decider. Requires ANTHROPIC_API_KEY (see .env.example)."""

    def __init__(self, model: str | None = None):
        import anthropic  # imported here so tests never need the package config

        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self._model = model or os.environ.get("CUA_MODEL", "claude-sonnet-4-5")

    @property
    def model_name(self) -> str:
        return self._model

    def decide(self, messages: list[dict]) -> Decision:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            tool_choice={"type": "any"},  # a structured action, every turn
            messages=messages,
        )
        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text
            if block.type == "tool_use":
                return Decision(
                    tool=block.name,
                    args=dict(block.input),
                    raw_assistant_content=[b.model_dump() for b in response.content],
                    tool_use_id=block.id,
                    text=text,
                )
        # tool_choice="any" makes this unreachable in practice; treat as blocked.
        return Decision(tool="blocked", args={"reason": "model returned no tool call"})
