"""Tests for the provider-agnostic tool-calling runtime (TDD §11.1, §14.4)."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from finagent.output.agents.runtime import (
    AgentResult,
    AssistantTurn,
    Message,
    OpenAICompatibleModel,
    ScriptedModel,
    Tool,
    ToolCall,
    ToolError,
    ToolRegistry,
    ToolSpec,
    call,
    run_agent,
    turn,
)
from finagent.output.models import TraceStep


# --- Fixtures ---------------------------------------------------------------


class AddArgs(BaseModel):
    a: float
    b: float


class CutArgs(BaseModel):
    line_id: str
    amount: float = Field(ge=0)


class FinalizeArgs(BaseModel):
    summary: str


class Answer(BaseModel):
    value: float


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    state = {"total": 0.0}

    @reg.tool("add", "Add two numbers.", AddArgs)
    def add(args: AddArgs) -> dict:
        state["total"] = args.a + args.b
        return {"sum": state["total"]}

    @reg.tool("cut", "Cut a line.", CutArgs)
    def cut(args: CutArgs) -> dict:
        if args.line_id == "rent":
            raise ToolError("rent is fixed and cannot be cut")
        return {"cut": args.line_id, "amount": args.amount}

    @reg.tool("answer", "Return a model.", AddArgs)
    def answer(args: AddArgs) -> Answer:
        return Answer(value=args.a * args.b)

    @reg.tool("scalar", "Return a scalar.", AddArgs)
    def scalar(args: AddArgs) -> float:
        return args.a - args.b

    @reg.tool("finalize", "Finish.", FinalizeArgs, terminal=True)
    def finalize(args: FinalizeArgs) -> dict:
        if args.summary == "too early":
            raise ToolError("targets not met")
        return {"final": True, "summary": args.summary, "total": state["total"]}

    return reg


# --- Tool / registry --------------------------------------------------------


def test_tool_spec_uses_pydantic_schema(registry: ToolRegistry) -> None:
    specs = {s.name: s for s in registry.specs()}
    assert set(specs) == {"add", "cut", "answer", "scalar", "finalize"}
    add = specs["add"]
    assert isinstance(add, ToolSpec)
    assert add.description == "Add two numbers."
    assert add.parameters == AddArgs.model_json_schema()
    assert set(add.parameters["properties"]) == {"a", "b"}


def test_tool_dataclass_spec_and_add() -> None:
    reg = ToolRegistry()
    tool = Tool("noop", "Do nothing.", FinalizeArgs, lambda args: {"ok": True})
    reg.add(tool)
    assert tool.terminal is False
    assert reg.specs() == [tool.spec()]
    with pytest.raises(ValueError):
        reg.add(tool)


def test_dispatch_success(registry: ToolRegistry) -> None:
    result, done = registry.dispatch(ToolCall(id="1", name="add", arguments={"a": 1.5, "b": 2}))
    assert result == {"sum": pytest.approx(3.5)}
    assert done is False


def test_dispatch_serializes_models_and_scalars(registry: ToolRegistry) -> None:
    result, _ = registry.dispatch(ToolCall(id="1", name="answer", arguments={"a": 2, "b": 3}))
    assert result == {"value": pytest.approx(6.0)}
    result, _ = registry.dispatch(ToolCall(id="2", name="scalar", arguments={"a": 5, "b": 3}))
    assert result == {"result": pytest.approx(2.0)}


def test_dispatch_unknown_tool(registry: ToolRegistry) -> None:
    result, done = registry.dispatch(ToolCall(id="1", name="nope", arguments={}))
    assert "error" in result and "nope" in result["error"]
    assert done is False


def test_dispatch_invalid_arguments(registry: ToolRegistry) -> None:
    result, done = registry.dispatch(ToolCall(id="1", name="cut", arguments={"line_id": "dining", "amount": -5}))
    assert result["error"].startswith("Invalid arguments:")
    assert "amount" in result["error"]
    assert done is False


def test_dispatch_tool_error(registry: ToolRegistry) -> None:
    result, done = registry.dispatch(ToolCall(id="1", name="cut", arguments={"line_id": "rent", "amount": 0}))
    assert result == {"error": "rent is fixed and cannot be cut"}
    assert done is False


def test_dispatch_terminal(registry: ToolRegistry) -> None:
    result, done = registry.dispatch(ToolCall(id="1", name="finalize", arguments={"summary": "ok"}))
    assert done is True and result["final"] is True
    result, done = registry.dispatch(ToolCall(id="2", name="finalize", arguments={"summary": "too early"}))
    assert done is False and result == {"error": "targets not met"}


def test_dispatch_rejects_non_json_result() -> None:
    reg = ToolRegistry()
    reg.add(Tool("bad", "Returns a set.", FinalizeArgs, lambda args: {"x": {1, 2}}))
    result, done = reg.dispatch(ToolCall(id="1", name="bad", arguments={"summary": "s"}))
    assert "error" in result and done is False


# --- ScriptedModel helpers --------------------------------------------------


def test_call_and_turn_helpers() -> None:
    c1 = call("add", a=1, b=2)
    c2 = call("add", a=3, b=4)
    assert c1.name == "add" and c1.arguments == {"a": 1, "b": 2}
    assert c1.id != c2.id
    t = turn(c1, c2, text="thinking")
    assert t.text == "thinking" and t.tool_calls == [c1, c2]


def test_scripted_model_exhausted() -> None:
    model = ScriptedModel([])
    out = model.chat([Message(role="user", content="hi")], [])
    assert out.text == "script exhausted" and out.tool_calls == []
    assert len(model.calls) == 1


# --- run_agent ---------------------------------------------------------------


def test_run_agent_multi_step(registry: ToolRegistry) -> None:
    model = ScriptedModel([
        turn(call("add", a=1, b=2)),
        turn(call("cut", line_id="dining", amount=100)),
        turn(call("finalize", summary="done")),
    ])
    res = run_agent(model, "SYS", registry, "go", agent_name="tester")
    assert isinstance(res, AgentResult)
    assert res.completed is True and res.error is None
    assert res.steps == 3
    assert res.final_result == {"final": True, "summary": "done", "total": pytest.approx(3.0)}
    roles = [m.role for m in res.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool", "assistant", "tool"]
    assert res.messages[0].content == "SYS" and res.messages[1].content == "go"
    tool_msg = res.messages[3]
    assert tool_msg.tool_call_id == res.messages[2].tool_calls[0].id
    assert tool_msg.name == "add"
    assert json.loads(tool_msg.content) == {"sum": 3.0}


def test_run_agent_error_fed_back_then_corrected(registry: ToolRegistry) -> None:
    def after_error(messages: list[Message]) -> AssistantTurn:
        last = json.loads(messages[-1].content)
        assert last == {"error": "rent is fixed and cannot be cut"}
        return turn(call("cut", line_id="dining", amount=50))

    model = ScriptedModel([
        turn(call("cut", line_id="rent", amount=0)),
        after_error,
        turn(call("finalize", summary="fixed")),
    ])
    res = run_agent(model, "SYS", registry, "go", agent_name="tester")
    assert res.completed is True
    # the model saw the error in its second call
    assert json.loads(model.calls[1][-1].content)["error"]
    assert res.trace[0].result == {"error": "rent is fixed and cannot be cut"}
    assert res.trace[1].result == {"cut": "dining", "amount": 50}


def test_run_agent_invalid_args_come_back(registry: ToolRegistry) -> None:
    model = ScriptedModel([
        turn(call("add", a="x", b=2)),
        turn(call("finalize", summary="ok")),
    ])
    res = run_agent(model, "SYS", registry, "go", agent_name="t")
    assert res.completed is True
    assert res.trace[0].result["error"].startswith("Invalid arguments:")


def test_run_agent_text_only_nudge(registry: ToolRegistry) -> None:
    model = ScriptedModel([
        turn(text="I think we are done."),
        turn(call("finalize", summary="ok")),
    ])
    res = run_agent(model, "SYS", registry, "go", agent_name="t")
    assert res.completed is True and res.steps == 2
    nudge = model.calls[1][-1]
    assert nudge.role == "user"
    assert "finalize" in nudge.content
    assert res.messages[2].role == "assistant" and res.messages[2].content == "I think we are done."
    assert res.trace[0].text == "I think we are done." and res.trace[0].tool is None


def test_run_agent_terminal_stops_and_ignores_later_calls(registry: ToolRegistry) -> None:
    model = ScriptedModel([
        turn(call("finalize", summary="early but valid"), call("add", a=9, b=9)),
        turn(call("add", a=1, b=1)),
    ])
    res = run_agent(model, "SYS", registry, "go", agent_name="t")
    assert res.completed is True and res.steps == 1
    assert len(model.calls) == 1
    assert [s.tool for s in res.trace] == ["finalize"]
    assert res.final_result["total"] == pytest.approx(0.0)


def test_run_agent_failed_terminal_continues(registry: ToolRegistry) -> None:
    model = ScriptedModel([
        turn(call("finalize", summary="too early")),
        turn(call("finalize", summary="now")),
    ])
    res = run_agent(model, "SYS", registry, "go", agent_name="t")
    assert res.completed is True and res.steps == 2
    assert res.final_result["summary"] == "now"


def test_run_agent_max_steps(registry: ToolRegistry) -> None:
    model = ScriptedModel([turn(call("add", a=1, b=1)) for _ in range(10)])
    res = run_agent(model, "SYS", registry, "go", agent_name="t", max_steps=3)
    assert res.completed is False
    assert res.error == "max_steps"
    assert res.final_result is None
    assert res.steps == 3 and len(model.calls) == 3


def test_run_agent_model_exception(registry: ToolRegistry) -> None:
    def boom(messages: list[Message]) -> AssistantTurn:
        raise RuntimeError("provider down")

    model = ScriptedModel([turn(call("add", a=1, b=1)), boom])
    res = run_agent(model, "SYS", registry, "go", agent_name="t")
    assert res.completed is False
    assert "provider down" in res.error and "RuntimeError" in res.error
    assert res.steps == 2
    assert len(res.trace) == 1


def test_run_agent_trace_and_usage(registry: ToolRegistry) -> None:
    model = ScriptedModel([
        AssistantTurn(
            text="plan",
            tool_calls=[call("add", a=1, b=2), call("cut", line_id="dining", amount=5)],
            usage={"prompt_tokens": 10, "completion_tokens": 4},
        ),
        AssistantTurn(
            tool_calls=[call("finalize", summary="ok")],
            usage={"prompt_tokens": 20, "completion_tokens": 3},
        ),
    ])
    res = run_agent(model, "SYS", registry, "go", agent_name="restructurer")
    assert all(isinstance(s, TraceStep) for s in res.trace)
    assert [(s.agent, s.step, s.tool) for s in res.trace] == [
        ("restructurer", 1, "add"),
        ("restructurer", 1, "cut"),
        ("restructurer", 2, "finalize"),
    ]
    assert res.trace[0].arguments == {"a": 1, "b": 2}
    assert res.trace[0].result == {"sum": 3.0}
    assert res.trace[0].text == "plan"
    assert res.trace[0].usage["prompt_tokens"] == 10
    assert "latency_ms" in res.trace[0].usage
    assert res.usage["prompt_tokens"] == 30
    assert res.usage["completion_tokens"] == 7
    # trace is JSON-serializable for PlanOutput.trace
    json.dumps([s.model_dump(mode="json") for s in res.trace])


# --- OpenAICompatibleModel ----------------------------------------------------


class FakeTransport:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.requests: list[tuple[str, dict, dict]] = []

    def __call__(self, url: str, headers: dict, payload: dict) -> dict:
        self.requests.append((url, headers, payload))
        return self.response


def _response(message: dict, usage: dict | None = None) -> dict:
    return {
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


def test_openai_request_payload_shape() -> None:
    transport = FakeTransport(_response({"role": "assistant", "content": "hi"}))
    model = OpenAICompatibleModel(
        "gpt-test", base_url="https://example.com/v1/", api_key="sk-1", temperature=0.2, transport=transport
    )
    tc = ToolCall(id="c1", name="add", arguments={"a": 1, "b": 2})
    messages = [
        Message(role="system", content="SYS"),
        Message(role="user", content="go"),
        Message(role="assistant", content=None, tool_calls=[tc]),
        Message(role="tool", content='{"sum": 3}', tool_call_id="c1", name="add"),
    ]
    specs = [ToolSpec(name="add", description="Add.", parameters=AddArgs.model_json_schema())]
    model.chat(messages, specs)

    url, headers, payload = transport.requests[0]
    assert url == "https://example.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-1"
    assert payload["model"] == "gpt-test"
    assert payload["temperature"] == 0.2
    assert payload["tool_choice"] == "auto"
    assert payload["tools"] == [
        {"type": "function", "function": {"name": "add", "description": "Add.", "parameters": AddArgs.model_json_schema()}}
    ]
    msgs = payload["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1] == {"role": "user", "content": "go"}
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["tool_calls"] == [
        {"id": "c1", "type": "function", "function": {"name": "add", "arguments": json.dumps({"a": 1, "b": 2})}}
    ]
    assert msgs[3] == {"role": "tool", "content": '{"sum": 3}', "tool_call_id": "c1"}
    json.dumps(payload)


def test_openai_no_tools_omits_tool_fields() -> None:
    transport = FakeTransport(_response({"role": "assistant", "content": "hi"}))
    model = OpenAICompatibleModel("m", transport=transport)
    model.chat([Message(role="user", content="hi")], [])
    _, headers, payload = transport.requests[0]
    assert "tools" not in payload and "tool_choice" not in payload
    assert "Authorization" not in headers
    assert transport.requests[0][0] == "https://api.openai.com/v1/chat/completions"


def test_openai_parses_text_response() -> None:
    transport = FakeTransport(_response({"role": "assistant", "content": "hello"}))
    out = OpenAICompatibleModel("m", transport=transport).chat([Message(role="user", content="hi")], [])
    assert out.text == "hello" and out.tool_calls == []
    assert out.usage["prompt_tokens"] == 5 and out.usage["total_tokens"] == 7


def test_openai_parses_tool_calls_and_bad_json() -> None:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_a", "type": "function", "function": {"name": "add", "arguments": '{"a": 1, "b": 2}'}},
            {"id": "call_b", "type": "function", "function": {"name": "cut", "arguments": "{not json"}},
            {"id": "call_c", "type": "function", "function": {"name": "finalize", "arguments": ""}},
        ],
    }
    out = OpenAICompatibleModel("m", transport=FakeTransport(_response(message))).chat(
        [Message(role="user", content="hi")], []
    )
    assert out.text is None
    assert out.tool_calls[0] == ToolCall(id="call_a", name="add", arguments={"a": 1, "b": 2})
    assert out.tool_calls[1].arguments == {"_raw": "{not json"}
    assert out.tool_calls[2].arguments == {}


def test_openai_bad_raw_args_reach_model_as_error(registry: ToolRegistry) -> None:
    result, _ = registry.dispatch(ToolCall(id="x", name="add", arguments={"_raw": "{oops"}))
    assert result["error"].startswith("Invalid arguments:")


def test_openai_missing_choices_raises() -> None:
    model = OpenAICompatibleModel("m", transport=FakeTransport({"error": {"message": "bad key"}}))
    with pytest.raises(RuntimeError, match="bad key"):
        model.chat([Message(role="user", content="hi")], [])


def test_openai_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("FINAGENT_LLM_MODEL", "FINAGENT_LLM_BASE_URL", "FINAGENT_LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="FINAGENT_LLM_MODEL"):
        OpenAICompatibleModel.from_env()

    monkeypatch.setenv("FINAGENT_LLM_MODEL", "gemini-2.0-flash")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    m = OpenAICompatibleModel.from_env()
    assert m.name == "gemini-2.0-flash"
    assert m.base_url == "https://api.openai.com/v1"
    assert m.api_key == "sk-openai"

    monkeypatch.setenv("FINAGENT_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("FINAGENT_LLM_API_KEY", "sk-router")
    m = OpenAICompatibleModel.from_env(transport=FakeTransport(_response({"content": "x"})))
    assert m.base_url == "https://openrouter.ai/api/v1"
    assert m.api_key == "sk-router"


def test_dispatch_rejects_nan_result() -> None:
    reg = ToolRegistry()
    reg.add(Tool("nan", "Returns NaN.", FinalizeArgs, lambda args: {"x": float("nan")}))
    result, done = reg.dispatch(ToolCall(id="1", name="nan", arguments={"summary": "s"}))
    assert "error" in result and done is False
    json.dumps(result, allow_nan=False)


def test_dispatch_non_object_json_args_are_invalid(registry: ToolRegistry) -> None:
    message = {"content": None, "tool_calls": [{"id": "c", "function": {"name": "add", "arguments": "[1, 2]"}}]}
    out = OpenAICompatibleModel("m", transport=FakeTransport(_response(message))).chat([], [])
    assert out.tool_calls[0].arguments == {"_raw": "[1, 2]"}
    result, _ = registry.dispatch(out.tool_calls[0])
    assert result["error"].startswith("Invalid arguments:")


def test_openai_empty_assistant_text_serialized_as_empty_string() -> None:
    transport = FakeTransport(_response({"content": "ok"}))
    OpenAICompatibleModel("m", transport=transport).chat([Message(role="assistant", content=None)], [])
    assert transport.requests[0][2]["messages"][0] == {"role": "assistant", "content": ""}


def test_openai_missing_tool_call_ids_are_unique() -> None:
    message = {"content": None, "tool_calls": [
        {"function": {"name": "add", "arguments": "{}"}},
        {"function": {"name": "add", "arguments": "{}"}},
    ]}
    model = OpenAICompatibleModel("m", transport=FakeTransport(_response(message)))
    ids = [c.id for _ in range(2) for c in model.chat([], []).tool_calls]
    assert len(set(ids)) == 4 and all(ids)
