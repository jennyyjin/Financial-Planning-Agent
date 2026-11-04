import json
from datetime import datetime
from pathlib import Path

import pytest

from finagent.extraction import ExtractionError, body_schema, extract_profile, system_prompt
from finagent.llm import Message, UsageLog, scripted_model
from finagent.schema import FinancialProfile, MemoryMethod, Meta

PROFILES = Path(__file__).resolve().parents[1] / "examples" / "profiles"
NARRATIVE = json.loads((PROFILES / "narrative_profile.json").read_text())
BODY = {k: v for k, v in NARRATIVE.items() if k != "meta"}
META = Meta(user_id="u_demo", as_of=datetime(2026, 11, 2, 9, 0), profile_version=1,
            memory_method=MemoryMethod.full_history)
CONTEXT = [Message(role="user", content="Current date: 2026-11-02\n\n[1] ... User: I take home about $5,000.")]


def test_body_schema_excludes_meta():
    schema = body_schema()
    assert "meta" not in schema["properties"]
    assert "Meta" not in schema["$defs"] and "MemoryMethod" not in schema["$defs"]
    assert '"#/$defs/Meta"' not in json.dumps(schema)


def test_system_prompt_includes_schema():
    prompt = system_prompt()
    assert "{{SCHEMA}}" not in prompt
    assert '"recurring_expenses"' in prompt and '"dining_out"' in prompt


def test_extracts_valid_profile_and_sets_meta():
    model = scripted_model(json.dumps(BODY))
    log = UsageLog()
    profile = extract_profile(model, CONTEXT, META, log=log)
    assert profile == FinancialProfile.model_validate(NARRATIVE)
    assert [u.tag for u in log.records] == ["extract"]
    sent = model.calls[0]
    assert sent[0].role == "system" and sent[1:] == CONTEXT


def test_meta_from_model_is_ignored():
    reply = {**BODY, "meta": {"user_id": "someone_else", "profile_version": 99}}
    profile = extract_profile(scripted_model(json.dumps(reply)), CONTEXT, META)
    assert profile.meta == META


def test_retries_after_invalid_json():
    model = scripted_model("Sorry, here you go: {not json", f"```json\n{json.dumps(BODY)}\n```")
    log = UsageLog()
    profile = extract_profile(model, CONTEXT, META, log=log)
    assert profile.income[0].amount == 5000
    assert len(log.records) == 2
    retry = model.calls[1]
    assert retry[-2].role == "assistant" and retry[-1].role == "user"
    assert "not valid" in retry[-1].content


def test_retry_message_lists_schema_errors():
    bad = json.loads(json.dumps(BODY))
    bad["recurring_expenses"][0]["category"] = "rent"
    bad["debts"][0]["bonus"] = 1
    model = scripted_model(json.dumps(bad), json.dumps(BODY))
    extract_profile(model, CONTEXT, META)
    feedback = model.calls[1][-1].content
    assert "recurring_expenses/0/category" in feedback
    assert "debts/0/bonus" in feedback


def test_retries_after_empty_reply():
    model = scripted_model("", json.dumps(BODY))
    assert extract_profile(model, CONTEXT, META).current_savings_balance == 5000


def test_gives_up_after_max_retries():
    model = scripted_model("nope", "still nope", "nope again", json.dumps(BODY))
    log = UsageLog()
    with pytest.raises(ExtractionError, match="3 attempts"):
        extract_profile(model, CONTEXT, META, log=log, max_retries=2)
    assert len(log.records) == 3


def test_missing_fields_default_rather_than_fail():
    # Missing optional keys are filled with schema defaults; the completeness check (step 4) catches gaps.
    profile = extract_profile(scripted_model(json.dumps({"income": [{"source": "Job"}]})), CONTEXT, META)
    assert profile.income[0].amount is None and profile.recurring_expenses == []
