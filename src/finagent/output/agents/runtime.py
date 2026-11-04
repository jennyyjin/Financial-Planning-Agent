"""Provider-agnostic tool-calling agent runtime (TDD §11.1, D7).

Agents talk to any OpenAI-compatible chat endpoint through the `ChatModel`
protocol. Tools are Python functions with a Pydantic argument model; tool
errors go back to the model as `{"error": ...}` results so it can correct
itself. `ScriptedModel` replays fixed turns for tests (no network).
"""

from __future__ import annotations

import itertools
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional, Protocol, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from finagent.output.config import PlanConfig
from finagent.output.models import TraceStep

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MAX_STEPS: int = PlanConfig.model_fields["agent_max_steps"].default
RAW_ARGS_KEY = "_raw"  # holds tool-call arguments that were not valid JSON


# --- Messages ----------------------------------------------------------------


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AssistantTurn(BaseModel):
    text: Optional[str] = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Optional[str] = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ChatModel(Protocol):
    name: str

    def chat(self, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn: ...


# --- Tools -------------------------------------------------------------------


class ToolError(Exception):
    """Raised by a tool handler for a rule violation; the message goes back to the model."""


class ToolArgs(BaseModel):
    """Base of every tool argument model: NaN and infinity are rejected before a handler runs."""

    model_config = ConfigDict(allow_inf_nan=False)


class NoArgs(ToolArgs):
    """Arguments of a tool that takes none."""


def _non_finite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_non_finite(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_non_finite(v) for v in value)
    return False


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[BaseModel], Any]
    terminal: bool = False

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.args_model.model_json_schema(),
        )


def _to_json_result(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        out: Any = value.model_dump(mode="json")
    elif isinstance(value, dict):
        out = value
    else:
        out = {"result": value}
    json.dumps(out, allow_nan=False)  # raises TypeError/ValueError if not strict JSON (NaN/inf too)
    return out


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def tool(
        self, name: str, description: str, args_model: type[BaseModel], terminal: bool = False
    ) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
        """Decorator registering a handler as a tool; returns the handler unchanged."""

        def register(handler: Callable[[Any], Any]) -> Callable[[Any], Any]:
            self.add(Tool(name, description, args_model, handler, terminal))
            return handler

        return register

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def terminal_names(self) -> list[str]:
        return [t.name for t in self._tools.values() if t.terminal]

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def dispatch(self, call: ToolCall) -> tuple[dict[str, Any], bool]:
        """Run one tool call. Returns (JSON-serializable result, done)."""
        tool = self._tools.get(call.name)
        if tool is None:
            return {"error": f"Unknown tool '{call.name}'. Available tools: {', '.join(self._tools)}"}, False
        if set(call.arguments) == {RAW_ARGS_KEY}:
            return {"error": f"Invalid arguments: not valid JSON: {call.arguments[RAW_ARGS_KEY]!r}"}, False
        if _non_finite(call.arguments):
            return {"error": "Invalid arguments: numbers must be finite (no NaN or Infinity)."}, False
        try:
            args = tool.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            return {"error": f"Invalid arguments: {exc}"}, False
        try:
            value = tool.handler(args)
        except ToolError as exc:
            return {"error": str(exc)}, False
        except Exception as exc:  # a bug in a tool must not end the run or lose the trace (D4)
            return {"error": f"Internal tool error in '{call.name}': {exc!r}", "internal_error": True}, False
        try:
            result = _to_json_result(value)
        except (TypeError, ValueError) as exc:
            return {"error": f"Tool '{call.name}' returned a non-JSON result: {exc}"}, False
        return result, tool.terminal


# --- Agent loop --------------------------------------------------------------


@dataclass
class AgentResult:
    completed: bool
    final_result: Optional[dict[str, Any]]
    steps: int
    trace: list[TraceStep] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    error: Optional[str] = None
    usage: dict[str, Any] = field(default_factory=dict)


def _add_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
    for key, value in usage.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value


def _tokens_used(usage_total: dict[str, Any]) -> int:
    """Tokens spent so far, from the provider's usage (total_tokens, else prompt + completion)."""
    total = usage_total.get("total_tokens")
    if total is None:
        total = usage_total.get("prompt_tokens", 0) + usage_total.get("completion_tokens", 0)
    return int(total)


def _nudge(registry: ToolRegistry) -> str:
    terminal = registry.terminal_names() or registry.names
    return (
        "Please continue by calling the available tools. "
        f"When you are finished, call {' or '.join(terminal)} to complete the task."
    )


def run_agent(
    model: ChatModel,
    system_prompt: str,
    registry: ToolRegistry,
    user_message: str,
    *,
    agent_name: str,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_tokens: Optional[int] = None,
) -> AgentResult:
    """Run the tool-calling loop until a terminal tool succeeds, max_steps, the token budget, or a model error.

    `max_tokens` bounds the tokens the provider reports for this run. It is checked after each model call, so
    the run can overshoot by one call; the over-budget turn's tool calls are not executed, and the result has
    `error="token_budget_exceeded: ..."`. A provider that reports no usage cannot be limited.
    """
    messages = [Message(role="system", content=system_prompt), Message(role="user", content=user_message)]
    trace: list[TraceStep] = []
    usage_total: dict[str, Any] = {}
    specs = registry.specs()

    def result(completed: bool, steps: int, final: Optional[dict] = None, error: Optional[str] = None) -> AgentResult:
        return AgentResult(completed, final, steps, trace, messages, error, usage_total)

    for step in range(1, max_steps + 1):
        started = time.perf_counter()
        try:
            reply = model.chat(list(messages), specs)
        except Exception as exc:  # provider/network failure ends the run; caller falls back
            return result(False, step, error=repr(exc))
        usage = {**reply.usage, "latency_ms": (time.perf_counter() - started) * 1000}
        _add_usage(usage_total, usage)

        if max_tokens is not None and _tokens_used(usage_total) > max_tokens:
            trace.append(TraceStep(agent=agent_name, step=step, text=reply.text, usage=usage))
            return result(False, step, error=f"token_budget_exceeded: {_tokens_used(usage_total)} of {max_tokens} tokens")

        if not reply.tool_calls:
            messages.append(Message(role="assistant", content=reply.text))
            trace.append(TraceStep(agent=agent_name, step=step, text=reply.text, usage=usage))
            messages.append(Message(role="user", content=_nudge(registry)))
            continue

        executed: list[tuple[ToolCall, dict[str, Any]]] = []
        final: Optional[dict[str, Any]] = None
        for i, call in enumerate(reply.tool_calls):
            out, done = registry.dispatch(call)
            executed.append((call, out))
            trace.append(
                TraceStep(
                    agent=agent_name, step=step, tool=call.name, arguments=call.arguments,
                    # The turn's usage is recorded once, on its first tool call, so trace sums are correct.
                    result=out, text=reply.text, usage=usage if i == 0 else None,
                )
            )
            if done:
                final = out
                break  # later calls in the same turn are ignored

        # Only record calls that ran, so every assistant tool_call has a matching tool message.
        messages.append(Message(role="assistant", content=reply.text, tool_calls=[c for c, _ in executed]))
        for call, out in executed:
            messages.append(Message(role="tool", content=json.dumps(out), tool_call_id=call.id, name=call.name))
        if final is not None:
            return result(True, step, final=final)

    return result(False, max_steps, error="max_steps")


# --- Scripted model (tests) ----------------------------------------------------

_call_ids = itertools.count(1)


def call(name: str, **arguments: Any) -> ToolCall:
    """Build a ToolCall with an auto-generated id (for scripted tests)."""
    return ToolCall(id=f"call_{next(_call_ids)}", name=name, arguments=arguments)


def turn(*calls: ToolCall, text: Optional[str] = None) -> AssistantTurn:
    return AssistantTurn(text=text, tool_calls=list(calls))


ScriptStep = Union[AssistantTurn, Callable[[list[Message]], AssistantTurn]]


class ScriptedModel:
    """Replays a fixed list of turns; a callable turn receives the messages so far."""

    def __init__(self, turns: list[ScriptStep], name: str = "scripted") -> None:
        self.name = name
        self._turns = list(turns)
        self._index = 0
        self.calls: list[list[Message]] = []

    def chat(self, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn:
        self.calls.append(list(messages))
        if self._index >= len(self._turns):
            return AssistantTurn(text="script exhausted")
        step = self._turns[self._index]
        self._index += 1
        return step(messages) if callable(step) else step


# --- OpenAI-compatible model -----------------------------------------------------

Transport = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]


def _urllib_transport(timeout: float) -> Transport:
    def send(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(f"LLM request failed ({exc.code}): {body}") from exc

    return send


def _message_to_openai(msg: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": msg.role, "content": msg.content}
    if msg.role == "assistant" and not msg.tool_calls and msg.content is None:
        out["content"] = ""  # OpenAI rejects a null-content assistant message without tool_calls
    if msg.role == "assistant" and msg.tool_calls:
        out["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in msg.tool_calls
        ]
    if msg.role == "tool":
        out["tool_call_id"] = msg.tool_call_id
    return out


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite number {token} in tool arguments")


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    try:
        parsed = json.loads(raw, parse_constant=_reject_constant)
    except (TypeError, ValueError):  # JSONDecodeError is a ValueError; so are NaN/Infinity tokens
        return {RAW_ARGS_KEY: str(raw)}
    return parsed if isinstance(parsed, dict) else {RAW_ARGS_KEY: str(raw)}


class OpenAICompatibleModel:
    """Chat Completions client for OpenAI, OpenRouter, or Gemini's OpenAI-compatible endpoint."""

    def __init__(
        self,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        api_key: Optional[str] = None,
        temperature: float = 0.0,
        timeout: float = 60,
        transport: Optional[Transport] = None,
    ) -> None:
        self.name = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.transport = transport or _urllib_transport(timeout)

    @classmethod
    def from_env(cls, **overrides: Any) -> OpenAICompatibleModel:
        model = os.environ.get("FINAGENT_LLM_MODEL")
        if not model:
            raise RuntimeError("FINAGENT_LLM_MODEL is not set; set it to the model name to use (e.g. gpt-4o-mini).")
        kwargs: dict[str, Any] = {
            "base_url": os.environ.get("FINAGENT_LLM_BASE_URL") or DEFAULT_BASE_URL,
            "api_key": os.environ.get("FINAGENT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"),
        }
        kwargs.update(overrides)
        return cls(model, **kwargs)

    def build_payload(self, messages: list[Message], tools: list[ToolSpec]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.name,
            "messages": [_message_to_openai(m) for m in messages],
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
            payload["tool_choice"] = "auto"
        return payload

    def chat(self, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = self.transport(f"{self.base_url}/chat/completions", headers, self.build_payload(messages, tools))

        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            err = data.get("error") if isinstance(data, dict) else None
            detail = err.get("message") if isinstance(err, dict) else err
            raise RuntimeError(f"LLM response has no choices: {detail or data}")
        msg = choices[0].get("message") or {}
        tool_calls = [
            ToolCall(
                id=tc.get("id") or f"call_{next(_call_ids)}",  # unique even across turns
                name=(tc.get("function") or {}).get("name", ""),
                arguments=_parse_arguments((tc.get("function") or {}).get("arguments")),
            )
            for tc in msg.get("tool_calls") or []
        ]
        return AssistantTurn(text=msg.get("content"), tool_calls=tool_calls, usage=dict(data.get("usage") or {}))
