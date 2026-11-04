from datetime import datetime

from finagent.llm import UsageLog, scripted_model
from finagent.memory import RollingSummary, make_memory
from finagent.schema import MemoryMethod

T1 = datetime(2026, 11, 2, 9, 0)
T2 = datetime(2026, 11, 3, 10, 0)
T3 = datetime(2026, 11, 4, 11, 0)


def test_first_message_needs_no_summary_call():
    model = scripted_model()
    memory = RollingSummary(model)
    memory.add_user_turn("I take home $5,000 a month.", T1)
    assert model.calls == []
    text = memory.extraction_context()[0].content
    assert "(No earlier messages.)" in text
    assert "[1] 2026-11-02 09:00 User: I take home $5,000 a month." in text


def test_previous_exchange_is_merged_and_only_latest_kept_raw():
    model = scripted_model("Income: $5,000/month after tax.")
    log = UsageLog()
    memory = RollingSummary(model, log=log)
    memory.add_user_turn("I take home $5,000 a month.", T1)
    memory.add_assistant_turn("Got it. Your plan is being updated.", T1)
    memory.add_user_turn("Rent is $1,500.", T2)

    # The summary call saw the old summary and the previous user message.
    [sent] = model.calls
    assert "(No earlier messages.)" in sent[1].content
    assert "[1] 2026-11-02 09:00 User: I take home" in sent[1].content
    assert "Current date: 2026-11-03" in sent[1].content
    assert "400 words" in sent[0].content
    assert [u.tag for u in log.records] == ["summary_update"]

    # The assistant reply leads into the new message, so it stays raw until the next turn.
    text = memory.extraction_context()[0].content
    assert "Income: $5,000/month after tax." in text
    assert "[2] 2026-11-02 09:00 Assistant: Got it." in text
    assert "[3] 2026-11-03 10:00 User: Rent is $1,500." in text
    assert "I take home" not in text


def test_follow_up_question_stays_raw_with_the_answer():
    model = scripted_model("Income: $5,000/month, tax status unknown.")
    memory = RollingSummary(model)
    memory.add_user_turn("I make $5,000 a month.", T1)
    memory.add_assistant_turn("Is that before or after tax?", T1)
    memory.add_user_turn("After tax.", T2)
    [sent] = model.calls
    assert "Is that before or after tax?" not in sent[1].content  # not merged yet
    text = memory.extraction_context()[0].content
    assert text.index("Assistant: Is that before or after tax?") < text.index("User: After tax.")


def test_summary_updates_accumulate():
    model = scripted_model("summary v1", "summary v2")
    memory = RollingSummary(model)
    for text, ts in [("one", T1), ("two", T2), ("three", T3)]:
        memory.add_user_turn(text, ts)
    assert "Current summary:\nsummary v1" in model.calls[1][1].content
    assert memory.summary == "summary v2"
    assert memory.stats()["summary_words"] == 2


def test_state_round_trip_continues_numbering():
    model = scripted_model("s1", "s2")
    memory = RollingSummary(model, max_summary_words=250)
    memory.add_user_turn("one", T1)
    memory.add_assistant_turn("ok", T1)
    memory.add_user_turn("two", T2)
    restored = make_memory(MemoryMethod.rolling_summary, model, memory.state())
    assert isinstance(restored, RollingSummary)
    assert restored.state() == memory.state()
    turn = restored.add_user_turn("three", T3)
    assert turn.index == 4
    assert "250 words" in model.calls[1][0].content
