import json
from datetime import datetime
from pathlib import Path

import pytest

import finagent.input_pipeline as pipeline
from finagent.extraction import ExtractionError
from finagent.input_pipeline import InputPhaseUnavailable, process_message, run_episode
from finagent.llm import AssistantTurn
from finagent.schema import MemoryMethod
from finagent.store import Store
from finagent.web import extract as web_extract

PROFILES = Path(__file__).resolve().parents[1] / "examples" / "profiles"
COMPLETE = {k: v for k, v in json.loads((PROFILES / "complete_profile.json").read_text()).items() if k != "meta"}
NARRATIVE = {k: v for k, v in json.loads((PROFILES / "narrative_profile.json").read_text()).items() if k != "meta"}
T1, T2, T3 = datetime(2026, 11, 3, 9, 0), datetime(2026, 11, 4, 9, 0), datetime(2026, 11, 5, 9, 0)


class RoutedModel:
    """Fake model: answers each kind of call (by its system prompt) from its own queue."""

    name = "routed"
    ROUTES = {"You extract": "extract", "You write follow-up": "follow_up", "You maintain a running summary": "summary"}

    def __init__(self, extract=(), follow_up=(), summary=()):
        self.queues = {"extract": list(extract), "follow_up": list(follow_up), "summary": list(summary)}
        self.calls: list[tuple[str, list]] = []

    def chat(self, messages, tools):
        kind = next(v for k, v in self.ROUTES.items() if messages[0].content.startswith(k))
        self.calls.append((kind, messages))
        reply = self.queues[kind].pop(0)
        return AssistantTurn(text=reply if isinstance(reply, str) else json.dumps(reply), usage={"prompt_tokens": 100, "completion_tokens": 10})

    def sent(self, kind):
        return [m for k, m in self.calls if k == kind]


def with_rent(body, amount):
    body = json.loads(json.dumps(body))
    body["recurring_expenses"][0]["amount"] = amount
    return body


def test_complete_first_message_is_ready(tmp_path):
    store = Store(tmp_path)
    model = RoutedModel(extract=[COMPLETE])
    result = process_message("u1", "my finances...", MemoryMethod.full_history, T1, model=model, store=store)
    assert result.status == "ready"
    assert result.diff is None and result.follow_up_questions == []
    assert result.confirmation_message.startswith("Got it. I've recorded income of $6,200/month")
    assert (result.profile.meta.user_id, result.profile.meta.profile_version) == ("u1", 1)
    assert [u["tag"] for u in result.usage] == ["extract"]
    assert result.usage_totals["prompt_tokens"] == 100
    assert result.memory_stats["truncated"] is False
    # Everything was saved.
    fh = MemoryMethod.full_history
    assert store.profile_versions("u1", fh) == [1]
    assert store.load_session("u1", fh) == {"last_ready_version": 1}
    assert [t.role for t in store.transcript("u1", fh)] == ["user", "assistant"]


def test_missing_info_asks_then_answer_completes(tmp_path):
    store = Store(tmp_path)
    questions = {"questions": ["What's left on your student loan, and its APR?", "How old are you, and when do you want to retire?"]}
    answered = json.loads(json.dumps(NARRATIVE))
    answered["debts"][0].update(balance=18000, apr=5.5)
    answered["retirement"].update(current_age=27, target_retirement_age=62)
    model = RoutedModel(extract=[NARRATIVE, answered], follow_up=[questions])

    first = process_message("u1", "story", "full_history", T1, model=model, store=store)
    assert first.status == "needs_clarification"
    assert first.follow_up_questions == questions["questions"] and first.follow_up_generated_by == "llm"
    assert [m.path for m in first.missing] == ["/debts/0", "/retirement"]
    assert store.load_session("u1", MemoryMethod.full_history) == {}

    second = process_message("u1", "$18k at 5.5%; I'm 27 and want to retire at 62.", "full_history", T2,
                             model=model, store=store)
    assert second.status == "ready" and second.profile.meta.profile_version == 2
    # No plan existed yet, so the confirmation summarizes everything rather than a diff.
    assert second.diff is None and "I've recorded income" in second.confirmation_message
    # The second extraction saw the follow-up questions the user was answering.
    context = model.sent("extract")[1][1].content
    assert "Assistant: Thanks! I need a few more details" in context
    assert "- What's left on your student loan, and its APR?" in context


def test_update_after_ready_reports_changes_since_last_plan(tmp_path):
    store = Store(tmp_path)
    model = RoutedModel(extract=[COMPLETE, with_rent(COMPLETE, 1950)])
    process_message("u1", "start", "full_history", T1, model=model, store=store)
    result = process_message("u1", "Rent went up to $1,950.", "full_history", T2, model=model, store=store)
    assert result.status == "ready"
    assert result.diff.describe() == ["updated Rent (amount $1,800 → $1,950)"]
    assert result.confirmation_message == "Got it. I've updated Rent (amount $1,800 → $1,950). Your plan is being updated."
    assert store.load_session("u1", MemoryMethod.full_history) == {"last_ready_version": 2}


def test_diff_is_against_last_ready_profile_not_an_incomplete_one(tmp_path):
    store = Store(tmp_path)
    incomplete = json.loads(json.dumps(COMPLETE))
    incomplete["debts"][0]["apr"] = None
    model = RoutedModel(extract=[COMPLETE, incomplete, with_rent(COMPLETE, 1950)],
                        follow_up=[{"questions": ["What's the APR?"]}])
    process_message("u1", "start", "full_history", T1, model=model, store=store)
    assert process_message("u1", "new loan terms", "full_history", T2, model=model, store=store).status == "needs_clarification"
    result = process_message("u1", "5.5%, and rent is $1,950", "full_history", T3, model=model, store=store)
    assert result.diff.describe() == ["updated Rent (amount $1,800 → $1,950)"]  # compared with v1
    assert result.profile.meta.profile_version == 3


def test_rolling_summary_pipeline(tmp_path):
    store = Store(tmp_path)
    model = RoutedModel(extract=[COMPLETE, with_rent(COMPLETE, 1950)], summary=["Income: $6,200/month after tax..."])
    process_message("u1", "start", "rolling_summary", T1, model=model, store=store)
    result = process_message("u1", "Rent went up to $1,950.", "rolling_summary", T2, model=model, store=store)
    assert [u["tag"] for u in result.usage] == ["summary_update", "extract"]
    context = model.sent("extract")[1][1].content
    assert "Income: $6,200/month after tax..." in context and "User: start" not in context
    assert result.memory_stats["summary_words"] > 0
    # Methods keep separate state.
    assert store.profile_versions("u1", MemoryMethod.full_history) == []


def test_failed_extraction_saves_nothing(tmp_path):
    store = Store(tmp_path)
    model = RoutedModel(extract=["nope", "nope", "nope"])
    with pytest.raises(ExtractionError):
        process_message("u1", "hello", "full_history", T1, model=model, store=store)
    assert store.profile_versions("u1", MemoryMethod.full_history) == []
    assert store.transcript("u1", MemoryMethod.full_history) == []
    assert store.load_memory_state("u1", MemoryMethod.full_history) is None


def test_no_model_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("FINAGENT_LLM_MODEL", raising=False)
    with pytest.raises(InputPhaseUnavailable):
        process_message("u1", "hello", store=Store(tmp_path))


def test_run_episode_uses_a_fresh_store(monkeypatch, tmp_path):
    monkeypatch.setenv("FINAGENT_DATA_DIR", str(tmp_path / "app_data"))
    model = RoutedModel(extract=[COMPLETE, with_rent(COMPLETE, 1950)])
    results = run_episode(["start", {"text": "rent up", "timestamp": "2026-11-04T09:00:00"}],
                          "full_history", model=model)
    assert [r.status for r in results] == ["ready", "ready"]
    assert results[1].profile.meta.as_of == T2
    assert not (tmp_path / "app_data").exists()
    json.dumps(results[1].to_dict())  # serializable for evaluation logs


# --- Dashboard hook (web/extract.py) -----------------------------------------------


def test_dashboard_uses_the_pipeline(monkeypatch, tmp_path):
    monkeypatch.setenv("FINAGENT_DATA_DIR", str(tmp_path))
    model = RoutedModel(extract=[NARRATIVE], follow_up=[{"questions": ["q1", "q2"]}])
    monkeypatch.setattr(pipeline, "resolve_model", lambda m: model)
    found = web_extract.extract("anything", user_id="web_abc123")
    assert found.status == "needs_clarification" and found.questions == ["q1", "q2"]
    assert (tmp_path / "web_abc123" / "full_history" / "profiles" / "v1.json").exists()


def test_dashboard_falls_back_to_examples_without_a_model(monkeypatch):
    monkeypatch.delenv("FINAGENT_LLM_MODEL", raising=False)
    assert web_extract.extract(web_extract.example_text()).status == "ready"
    found = web_extract.extract("I earn $3,000 a month")
    assert found.status == "unavailable" and "input phase isn" in found.message


def test_dashboard_reports_pipeline_errors(monkeypatch, capsys):
    def boom(m):
        raise ExtractionError("bad")
    monkeypatch.setattr(pipeline, "resolve_model", boom)
    found = web_extract.extract("I earn $3,000 a month")
    assert found.status == "unavailable" and "couldn't read that" in found.message
    assert "input phase failed" in capsys.readouterr().err
