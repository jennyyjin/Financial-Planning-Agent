import pytest

from finagent.llm import (
    LLMError,
    Message,
    UsageLog,
    complete,
    complete_json,
    model_from_env,
    parse_json,
    scripted_model,
)
from finagent.output.agents.runtime import OpenAICompatibleModel

MESSAGES = [Message(role="system", content="Reply in JSON."), Message(role="user", content="hi")]


def test_complete_returns_text_and_records_usage():
    model = scripted_model("hello", usage={"prompt_tokens": 12, "completion_tokens": 3})
    log = UsageLog()
    response = complete(model, MESSAGES, tag="extract", log=log)
    assert response.text == "hello"
    assert [(u.tag, u.model, u.prompt_tokens, u.completion_tokens) for u in log.records] == [
        ("extract", "scripted", 12, 3)
    ]
    assert log.records[0].latency_s >= 0


def test_complete_sends_messages_without_tools():
    model = scripted_model("ok")
    complete(model, MESSAGES, tag="extract")
    assert model.calls == [MESSAGES]


def test_complete_missing_usage_counts_as_zero():
    log = UsageLog()
    complete(scripted_model("ok"), MESSAGES, tag="x", log=log)
    assert (log.records[0].prompt_tokens, log.records[0].completion_tokens) == (0, 0)


def test_complete_empty_reply_raises_but_still_logs_usage():
    log = UsageLog()
    with pytest.raises(LLMError):
        complete(scripted_model("  "), MESSAGES, tag="extract", log=log)
    assert len(log.records) == 1


def test_usage_log_totals():
    model = scripted_model("a", "b", usage={"prompt_tokens": 10, "completion_tokens": 2})
    log = UsageLog()
    complete(model, MESSAGES, tag="summary_update", log=log)
    complete(model, MESSAGES, tag="extract", log=log)
    totals = log.totals()
    assert (totals["calls"], totals["prompt_tokens"], totals["completion_tokens"]) == (2, 20, 4)
    assert [r["tag"] for r in log.to_list()] == ["summary_update", "extract"]


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        'Here is the profile:\n{"a": 1}\nLet me know!',
    ],
)
def test_parse_json_accepts_common_formats(text):
    assert parse_json(text) == {"a": 1}


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '{"a": }'])
def test_parse_json_rejects_non_objects(text):
    with pytest.raises(LLMError):
        parse_json(text)


def test_complete_json():
    data, response = complete_json(scripted_model('```json\n{"income": []}\n```'), MESSAGES, tag="extract")
    assert data == {"income": []}
    assert response.usage.tag == "extract"


def test_model_from_env_uses_shared_config(monkeypatch):
    monkeypatch.setenv("FINAGENT_LLM_MODEL", "test-model")
    monkeypatch.setenv("FINAGENT_LLM_API_KEY", "key")
    monkeypatch.setenv("FINAGENT_LLM_BASE_URL", "https://example.com/v1/")
    model = model_from_env()
    assert isinstance(model, OpenAICompatibleModel)
    assert (model.name, model.api_key, model.base_url) == ("test-model", "key", "https://example.com/v1")


def test_model_from_env_requires_model(monkeypatch):
    monkeypatch.delenv("FINAGENT_LLM_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="FINAGENT_LLM_MODEL"):
        model_from_env()
