"""Input phase entry points: one user message in, a FinancialProfile (or follow-up questions) out.

    process_message(user_id, text, memory_method) -> InputResult
    run_episode(turns, memory_method) -> list[InputResult]

For each message: load the user's memory -> record the message -> rebuild the
profile from the memory context -> check required fields -> either ask follow-up
questions (status "needs_clarification") or confirm (status "ready"). The reply
is recorded in memory too, so the next message is read with it as context.
Nothing is saved unless the whole message is handled successfully.

Interactive use:  python -m finagent.input_pipeline --user me [--memory rolling_summary]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional, Sequence, Union

from finagent.completeness import (
    CLARIFICATION_MESSAGE,
    DEFAULT_REQUIRED,
    MissingItem,
    RequiredFields,
    confirmation_message,
    find_missing,
    follow_up_questions,
)
from finagent.diff import ProfileDiff, diff_profiles
from finagent.env import load_dotenv
from finagent.extraction import extract_profile
from finagent.llm import ChatModel, UsageLog, model_from_env
from finagent.memory import make_memory
from finagent.schema import FinancialProfile, MemoryMethod, Meta
from finagent.store import Store

Status = Literal["ready", "needs_clarification"]

# One lock per (user, method): the dashboard server handles requests on threads.
_locks: defaultdict[tuple[str, str], threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


class InputPhaseUnavailable(RuntimeError):
    """No LLM is configured, so free text can't be read."""


@dataclass
class InputResult:
    status: Status
    profile: FinancialProfile
    # Changes since the last complete profile (the one the current plan is based on); None if there is none.
    diff: Optional[ProfileDiff]
    confirmation_message: str
    follow_up_questions: list[str] = field(default_factory=list)
    follow_up_generated_by: Optional[Literal["llm", "template"]] = None
    missing: list[MissingItem] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)  # one record per LLM call
    usage_totals: dict[str, Any] = field(default_factory=dict)
    memory_stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "profile": self.profile.model_dump(mode="json"),
            "diff": self.diff.model_dump(mode="json") if self.diff is not None else None,
            "confirmation_message": self.confirmation_message,
            "follow_up_questions": self.follow_up_questions,
            "follow_up_generated_by": self.follow_up_generated_by,
            "missing": [m.__dict__ for m in self.missing],
            "usage": self.usage,
            "usage_totals": self.usage_totals,
            "memory_stats": self.memory_stats,
        }


def resolve_model(model: Optional[ChatModel]) -> ChatModel:
    if model is not None:
        return model
    if not os.environ.get("FINAGENT_LLM_MODEL"):
        raise InputPhaseUnavailable("FINAGENT_LLM_MODEL is not set, so the input phase can't read free text.")
    return model_from_env()


def _lock(user_id: str, method: MemoryMethod) -> threading.Lock:
    with _locks_guard:
        return _locks[(user_id, method.value)]


def _assistant_text(message: str, questions: list[str]) -> str:
    return "\n".join([message, *(f"- {q}" for q in questions)])


def process_message(
    user_id: str,
    text: str,
    memory_method: Union[MemoryMethod, str] = MemoryMethod.full_history,
    timestamp: Optional[datetime] = None,
    *,
    model: Optional[ChatModel] = None,
    store: Optional[Store] = None,
    required: RequiredFields = DEFAULT_REQUIRED,
) -> InputResult:
    """Handle one user message. Uses the FINAGENT_LLM_* model if `model` is not given."""
    method = MemoryMethod(memory_method)
    model = resolve_model(model)
    store = store or Store()
    timestamp = timestamp or datetime.now().replace(microsecond=0)
    text = text.strip()
    if not text:
        raise ValueError("Empty message.")

    with _lock(user_id, method):
        log = UsageLog()
        memory = make_memory(method, model, store.load_memory_state(user_id, method), log)
        session = store.load_session(user_id, method)
        versions = store.profile_versions(user_id, method)
        last_ready = session.get("last_ready_version")
        base = store.load_profile(user_id, method, last_ready) if last_ready else None

        user_turn = memory.add_user_turn(text, timestamp)
        meta = Meta(user_id=user_id, as_of=timestamp, profile_version=(versions[-1] + 1 if versions else 1),
                    memory_method=method)
        profile = extract_profile(model, memory.extraction_context(), meta, log=log)
        memory_stats = memory.stats()
        diff = diff_profiles(base, profile) if base is not None else None

        missing = find_missing(profile, required)
        if missing:
            follow_up = follow_up_questions(missing, model, log=log)
            status: Status = "needs_clarification"
            message, questions, generated_by = CLARIFICATION_MESSAGE, follow_up.questions, follow_up.generated_by
        else:
            status, message, questions, generated_by = "ready", confirmation_message(profile, diff), [], None
            session["last_ready_version"] = profile.meta.profile_version
        assistant_turn = memory.add_assistant_turn(_assistant_text(message, questions), timestamp)

        store.save_profile(profile)
        store.append_transcript(user_id, method, [user_turn, assistant_turn])
        store.save_memory_state(user_id, method, memory.state())
        store.save_session(user_id, method, session)

    return InputResult(
        status=status, profile=profile, diff=diff, confirmation_message=message,
        follow_up_questions=questions, follow_up_generated_by=generated_by, missing=missing,
        usage=log.to_list(), usage_totals=log.totals(), memory_stats=memory_stats,
    )


EpisodeTurn = Union[str, dict[str, Any]]  # text, or {"text": ..., "timestamp": "ISO"?}


def run_episode(
    turns: Sequence[EpisodeTurn],
    memory_method: Union[MemoryMethod, str] = MemoryMethod.full_history,
    *,
    model: Optional[ChatModel] = None,
    user_id: str = "episode",
    store: Optional[Store] = None,
    required: RequiredFields = DEFAULT_REQUIRED,
) -> list[InputResult]:
    """Replay a scripted conversation, one InputResult per user message.

    Uses a fresh temporary store unless one is given, so eval runs start clean and
    don't touch the app's data.
    """
    model = resolve_model(model)
    with tempfile.TemporaryDirectory(prefix="finagent_episode_") as tmp:
        store = store or Store(tmp)
        results = []
        for t in turns:
            text, ts = (t, None) if isinstance(t, str) else (t["text"], t.get("timestamp"))
            ts = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
            results.append(process_message(user_id, text, memory_method, ts, model=model, store=store,
                                           required=required))
        return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m finagent.input_pipeline", description="Chat with the input phase.")
    p.add_argument("--user", default="cli")
    p.add_argument("--memory", choices=[m.value for m in MemoryMethod], default=MemoryMethod.full_history.value)
    p.add_argument("--show-profile", action="store_true", help="Print the extracted profile JSON after each message.")
    args = p.parse_args(argv)
    load_dotenv()
    try:
        model = resolve_model(None)
    except InputPhaseUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Model {model.name}; memory {args.memory}; user {args.user}. Empty line to quit.", file=sys.stderr)
    while True:
        try:
            text = input("> ").strip()
        except EOFError:
            break
        if not text:
            break
        result = process_message(args.user, text, args.memory, model=model)
        print(result.confirmation_message)
        for q in result.follow_up_questions:
            print(f"  - {q}")
        if args.show_profile:
            print(json.dumps(result.profile.model_dump(mode="json"), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
