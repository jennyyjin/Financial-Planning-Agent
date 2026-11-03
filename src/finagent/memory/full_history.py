"""Full history: the extractor sees the whole transcript, oldest first."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from finagent.llm import Message
from finagent.memory.base import MemoryStrategy, Role, Turn, current_date_line, estimate_tokens
from finagent.schema import MemoryMethod

DEFAULT_MAX_CONTEXT_TOKENS = 100_000
HEADER = "Conversation so far, oldest first. Later messages override earlier ones."


class FullHistory(MemoryStrategy):
    method = MemoryMethod.full_history

    def __init__(self, turns: Optional[list[Turn]] = None, max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS):
        self.turns: list[Turn] = list(turns or [])
        self.max_context_tokens = max_context_tokens
        # Set by extraction_context(): whether the oldest turns had to be dropped, and the context size.
        self.truncated = False
        self.context_tokens = 0

    def _add(self, role: Role, text: str, timestamp: datetime) -> Turn:
        turn = Turn(len(self.turns) + 1, role, text, timestamp)
        self.turns.append(turn)
        return turn

    def add_user_turn(self, text: str, timestamp: datetime) -> Turn:
        return self._add("user", text, timestamp)

    def add_assistant_turn(self, text: str, timestamp: datetime) -> Turn:
        return self._add("assistant", text, timestamp)

    def extraction_context(self) -> list[Message]:
        if not self.turns:
            raise ValueError("No turns recorded yet.")
        head = f"{current_date_line(self.turns[-1].timestamp)}\n\n{HEADER}\n"
        lines = [t.render() for t in self.turns]
        # Drop the oldest turns (never the latest) until the transcript fits the limit.
        start = 0
        while start < len(lines) - 1 and estimate_tokens(head + "\n".join(lines[start:])) > self.max_context_tokens:
            start += 1
        content = head + "\n".join(lines[start:])
        self.truncated = start > 0
        self.context_tokens = estimate_tokens(content)
        return [Message(role="user", content=content)]

    def stats(self) -> dict[str, Any]:
        return {"context_tokens": self.context_tokens, "truncated": self.truncated}

    def state(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "max_context_tokens": self.max_context_tokens,
            "turns": [t.to_dict() for t in self.turns],
        }

    @classmethod
    def load(cls, state: dict[str, Any]) -> FullHistory:
        return cls([Turn.from_dict(t) for t in state["turns"]], state.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS))
