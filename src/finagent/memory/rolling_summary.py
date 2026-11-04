"""Rolling summary: the extractor sees a running summary plus the latest exchange.

The latest exchange is the newest user message, together with any assistant
messages directly before it (e.g. the follow-up question it answers). When the
next user message arrives, the previous exchange is merged into the summary
with one LLM call, and the raw text of it is no longer shown to the extractor.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from finagent.llm import ChatModel, Message, UsageLog, complete
from finagent.memory.base import MemoryStrategy, Role, Turn, current_date_line, estimate_tokens
from finagent.schema import MemoryMethod

PROMPT_PATH = Path(__file__).resolve().parents[3] / "prompts" / "input" / "update_summary.md"
DEFAULT_MAX_SUMMARY_WORDS = 400
EMPTY_SUMMARY = "(No earlier messages.)"


@lru_cache(maxsize=1)
def _prompt_template() -> str:
    try:
        return PROMPT_PATH.read_text()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Summary prompt not found at {PROMPT_PATH}. Run from a source checkout or an editable install."
        ) from exc


class RollingSummary(MemoryStrategy):
    method = MemoryMethod.rolling_summary

    def __init__(
        self,
        model: ChatModel,
        *,
        summary: str = "",
        pending: Optional[list[Turn]] = None,
        next_index: int = 1,
        max_summary_words: int = DEFAULT_MAX_SUMMARY_WORDS,
        log: Optional[UsageLog] = None,
    ):
        self.model = model
        self.summary = summary
        self.pending: list[Turn] = list(pending or [])  # turns not yet merged into the summary
        self.next_index = next_index
        self.max_summary_words = max_summary_words
        self.log = log  # set by the pipeline for each message so summary calls are counted
        self.context_tokens = 0

    def _add(self, role: Role, text: str, timestamp: datetime) -> Turn:
        turn = Turn(self.next_index, role, text, timestamp)
        self.pending.append(turn)
        self.next_index += 1
        return turn

    def add_user_turn(self, text: str, timestamp: datetime) -> Turn:
        # Merge everything up to the previous user message; keep the assistant turns
        # that came after it, since they lead into this new message.
        last_user = max((i for i, t in enumerate(self.pending) if t.role == "user"), default=-1)
        if last_user >= 0:
            self.summary = self._merge(self.pending[: last_user + 1], timestamp)
            self.pending = self.pending[last_user + 1 :]
        return self._add("user", text, timestamp)

    def add_assistant_turn(self, text: str, timestamp: datetime) -> Turn:
        return self._add("assistant", text, timestamp)

    def _merge(self, turns: list[Turn], now: datetime) -> str:
        system = _prompt_template().replace("{{MAX_WORDS}}", str(self.max_summary_words))
        user = (
            f"{current_date_line(now)}\n\n"
            f"Current summary:\n{self.summary or EMPTY_SUMMARY}\n\n"
            "New messages:\n" + "\n".join(t.render() for t in turns)
        )
        messages = [Message(role="system", content=system), Message(role="user", content=user)]
        return complete(self.model, messages, tag="summary_update", log=self.log).text.strip()

    def extraction_context(self) -> list[Message]:
        if not self.pending:
            raise ValueError("No turns recorded yet.")
        content = (
            f"{current_date_line(self.pending[-1].timestamp)}\n\n"
            f"Summary of the earlier conversation:\n{self.summary or EMPTY_SUMMARY}\n\n"
            "Latest messages (these override the summary):\n" + "\n".join(t.render() for t in self.pending)
        )
        self.context_tokens = estimate_tokens(content)
        return [Message(role="user", content=content)]

    def stats(self) -> dict[str, Any]:
        return {"context_tokens": self.context_tokens, "summary_words": len(self.summary.split())}

    def state(self) -> dict[str, Any]:
        return {
            "method": self.method.value,
            "summary": self.summary,
            "pending": [t.to_dict() for t in self.pending],
            "next_index": self.next_index,
            "max_summary_words": self.max_summary_words,
        }

    @classmethod
    def load(cls, state: dict[str, Any], model: ChatModel, log: Optional[UsageLog] = None) -> RollingSummary:
        return cls(
            model,
            summary=state["summary"],
            pending=[Turn.from_dict(t) for t in state["pending"]],
            next_index=state["next_index"],
            max_summary_words=state.get("max_summary_words", DEFAULT_MAX_SUMMARY_WORDS),
            log=log,
        )
