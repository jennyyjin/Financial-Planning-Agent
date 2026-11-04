from datetime import datetime

from finagent.memory import FullHistory
from finagent.schema import MemoryMethod

T1 = datetime(2026, 11, 2, 9, 0)
T2 = datetime(2026, 11, 5, 18, 30)


def test_full_history_context_has_every_turn_in_order():
    memory = FullHistory()
    memory.add_user_turn("I take home $5,000 a month.", T1)
    memory.add_assistant_turn("Is your student loan balance known?", T1)
    memory.add_user_turn("It's $18,000.", T2)
    [message] = memory.extraction_context()
    assert message.role == "user"
    text = message.content
    assert text.startswith("Current date: 2026-11-05")
    assert text.index("[1] 2026-11-02 09:00 User: I take home") < text.index("[2] 2026-11-02 09:00 Assistant: Is your") \
        < text.index("[3] 2026-11-05 18:30 User: It's $18,000.")
    assert not memory.truncated


def test_full_history_drops_oldest_turns_past_the_limit_but_keeps_latest():
    memory = FullHistory(max_context_tokens=60)
    for i in range(10):
        memory.add_user_turn(f"message number {i} " + "x" * 40, T1)
    text = memory.extraction_context()[0].content
    assert memory.truncated
    assert "message number 9" in text and "message number 0" not in text
    assert memory.context_tokens > 0


def test_full_history_keeps_latest_turn_even_if_it_alone_is_too_long():
    memory = FullHistory(max_context_tokens=10)
    memory.add_user_turn("first message", T1)
    memory.add_user_turn("y" * 500, T2)
    text = memory.extraction_context()[0].content
    assert "y" * 500 in text and "first message" not in text


def test_full_history_state_round_trip():
    memory = FullHistory(max_context_tokens=5000)
    memory.add_user_turn("hello", T1)
    memory.add_assistant_turn("hi", T2)
    restored = FullHistory.load(memory.state())
    assert memory.state()["method"] == MemoryMethod.full_history.value
    assert restored.state() == memory.state()
    assert restored.extraction_context() == memory.extraction_context()
