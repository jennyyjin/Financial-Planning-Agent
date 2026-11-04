"""Memory strategies for the input phase."""

from __future__ import annotations

from typing import Any, Optional

from finagent.llm import ChatModel, UsageLog
from finagent.memory.base import MemoryStrategy, Turn
from finagent.memory.full_history import FullHistory
from finagent.memory.rolling_summary import RollingSummary
from finagent.schema import MemoryMethod

__all__ = ["FullHistory", "MemoryStrategy", "RollingSummary", "Turn", "make_memory"]


def make_memory(
    method: MemoryMethod, model: ChatModel, state: Optional[dict[str, Any]] = None, log: Optional[UsageLog] = None
) -> MemoryStrategy:
    """A new memory strategy, or one restored from `state`."""
    if method is MemoryMethod.full_history:
        return FullHistory.load(state) if state else FullHistory()
    if method is MemoryMethod.rolling_summary:
        return RollingSummary.load(state, model, log) if state else RollingSummary(model, log=log)
    raise ValueError(f"Unknown memory method: {method}")
