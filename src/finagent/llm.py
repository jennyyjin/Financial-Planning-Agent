"""LLM helper for the input phase, built on the output phase's runtime.

Uses the same `ChatModel` interface and provider config as the output agents
(`FINAGENT_LLM_MODEL`, `FINAGENT_LLM_API_KEY`, `FINAGENT_LLM_BASE_URL`), so the
project has one way to pick a model. This module adds what the input phase
needs on top: plain (tool-free) completions, JSON parsing, and a per-call
usage record (tokens + latency, tagged by purpose) for the memory comparison.

JSON is requested in the prompt and parsed here; schema validation and
validation retries happen in the caller (extraction).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from finagent.output.agents.runtime import (
    AssistantTurn,
    ChatModel,
    Message,
    OpenAICompatibleModel,
    ScriptedModel,
)

__all__ = [
    "ChatModel",
    "LLMError",
    "LLMResponse",
    "Message",
    "Usage",
    "UsageLog",
    "complete",
    "complete_json",
    "model_from_env",
    "parse_json",
    "scripted_model",
]

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    """The model returned no text or text that is not a JSON object."""


@dataclass
class Usage:
    tag: str  # what the call was for, e.g. "extract", "summary_update", "follow_up"
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float


@dataclass
class LLMResponse:
    text: str
    usage: Usage


@dataclass
class UsageLog:
    """Collects the usage of every call made while handling one message or episode."""

    records: list[Usage] = field(default_factory=list)

    def add(self, usage: Usage) -> None:
        self.records.append(usage)

    def totals(self) -> dict[str, Any]:
        return {
            "calls": len(self.records),
            "prompt_tokens": sum(u.prompt_tokens for u in self.records),
            "completion_tokens": sum(u.completion_tokens for u in self.records),
            "latency_s": sum(u.latency_s for u in self.records),
        }

    def to_list(self) -> list[dict[str, Any]]:
        return [asdict(u) for u in self.records]


def model_from_env(**overrides: Any) -> ChatModel:
    """The configured OpenAI-compatible model (raises if FINAGENT_LLM_MODEL is unset)."""
    return OpenAICompatibleModel.from_env(**overrides)


def complete(model: ChatModel, messages: list[Message], *, tag: str, log: UsageLog | None = None) -> LLMResponse:
    """One tool-free chat call. Records tokens and latency in `log` if given."""
    start = time.perf_counter()
    turn = model.chat(messages, tools=[])
    latency = time.perf_counter() - start
    usage = Usage(
        tag=tag,
        model=getattr(model, "name", "unknown"),
        prompt_tokens=int(turn.usage.get("prompt_tokens") or 0),
        completion_tokens=int(turn.usage.get("completion_tokens") or 0),
        latency_s=latency,
    )
    if log is not None:
        log.add(usage)
    if not turn.text or not turn.text.strip():
        raise LLMError(f"Model returned no text for '{tag}'.")
    return LLMResponse(text=turn.text, usage=usage)


def parse_json(text: str) -> dict[str, Any]:
    """Parse a JSON object from model output, tolerating ```json fences or text around it."""
    match = _FENCE_RE.match(text)
    candidate = match.group(1) if match else text.strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        # Fall back to the outermost {...} span, e.g. "Here is the JSON: {...}".
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"Model output is not JSON: {text[:200]!r}") from None
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model output is not valid JSON ({exc}): {text[:200]!r}") from None
    if not isinstance(value, dict):
        raise LLMError(f"Expected a JSON object, got {type(value).__name__}.")
    return value


def complete_json(
    model: ChatModel, messages: list[Message], *, tag: str, log: UsageLog | None = None
) -> tuple[dict[str, Any], LLMResponse]:
    """`complete()` and parse the reply as a JSON object. Raises LLMError if it isn't one."""
    response = complete(model, messages, tag=tag, log=log)
    return parse_json(response.text), response


def scripted_model(*replies: str, usage: dict[str, int] | None = None) -> ScriptedModel:
    """A fake model for tests that returns `replies` in order, with optional fixed usage."""
    return ScriptedModel([AssistantTurn(text=r, usage=dict(usage or {})) for r in replies])
