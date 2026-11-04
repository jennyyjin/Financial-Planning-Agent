"""Memory strategies: what the extractor sees of the conversation each turn.

Every strategy records the same turns; they differ only in `extraction_context()`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from finagent.llm import Message
from finagent.schema import MemoryMethod

Role = Literal["user", "assistant"]


@dataclass
class Turn:
    index: int  # 1-based position in the conversation
    role: Role
    text: str
    timestamp: datetime

    def render(self) -> str:
        speaker = "User" if self.role == "user" else "Assistant"
        return f"[{self.index}] {self.timestamp:%Y-%m-%d %H:%M} {speaker}: {self.text}"

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "role": self.role, "text": self.text, "timestamp": self.timestamp.isoformat()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Turn:
        return cls(data["index"], data["role"], data["text"], datetime.fromisoformat(data["timestamp"]))


def estimate_tokens(text: str) -> int:
    # Rough provider-independent estimate (~4 characters per token).
    return len(text) // 4 + 1


def current_date_line(timestamp: datetime) -> str:
    # Lets the extractor resolve relative dates ("next June") against the latest message.
    return f"Current date: {timestamp:%Y-%m-%d}"


class MemoryStrategy(ABC):
    method: MemoryMethod

    @abstractmethod
    def add_user_turn(self, text: str, timestamp: datetime) -> Turn:
        """Record a user message; returns the recorded turn."""

    @abstractmethod
    def add_assistant_turn(self, text: str, timestamp: datetime) -> Turn:
        """Record what the agent said (follow-up questions, confirmations), so answers have context."""

    @abstractmethod
    def extraction_context(self) -> list[Message]:
        """The messages, after the system prompt, that the extractor rebuilds the profile from."""

    @abstractmethod
    def state(self) -> dict[str, Any]:
        """JSON-serializable state for persistence; `load(state)` restores it."""

    def stats(self) -> dict[str, Any]:
        """Facts about the last extraction context, reported to evaluation (e.g. its size)."""
        return {}
