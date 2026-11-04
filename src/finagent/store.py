"""Per-user persistence as JSON files.

    {root}/{user_id}/{memory_method}/
        transcript.jsonl      every user and assistant turn
        memory_state.json     the memory strategy's state
        session.json          pipeline bookkeeping (the last version that was complete)
        profiles/v{n}.json    every extracted profile version

Each memory method has its own directory, so the methods can be run on the same
user without affecting each other.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional, Union

from finagent.memory.base import Turn
from finagent.schema import FinancialProfile, MemoryMethod

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "data" / "users"
USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def default_root() -> Path:
    return Path(os.environ.get("FINAGENT_DATA_DIR") or DEFAULT_ROOT)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


class Store:
    def __init__(self, root: Union[str, Path, None] = None):
        self.root = Path(root) if root is not None else default_root()

    def _dir(self, user_id: str, method: MemoryMethod) -> Path:
        # user_id may come from a web request, so it must not be able to escape the root.
        if not USER_ID_RE.match(user_id):
            raise ValueError(f"Invalid user_id {user_id!r}: use 1-64 letters, digits, '_' or '-'.")
        return self.root / user_id / method.value

    def load_memory_state(self, user_id: str, method: MemoryMethod) -> Optional[dict[str, Any]]:
        path = self._dir(user_id, method) / "memory_state.json"
        return json.loads(path.read_text()) if path.exists() else None

    def save_memory_state(self, user_id: str, method: MemoryMethod, state: dict[str, Any]) -> None:
        _write_atomic(self._dir(user_id, method) / "memory_state.json", json.dumps(state, indent=2))

    def load_session(self, user_id: str, method: MemoryMethod) -> dict[str, Any]:
        path = self._dir(user_id, method) / "session.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def save_session(self, user_id: str, method: MemoryMethod, session: dict[str, Any]) -> None:
        _write_atomic(self._dir(user_id, method) / "session.json", json.dumps(session, indent=2))

    def append_transcript(self, user_id: str, method: MemoryMethod, turns: list[Turn]) -> None:
        path = self._dir(user_id, method) / "transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            for turn in turns:
                f.write(json.dumps(turn.to_dict()) + "\n")

    def transcript(self, user_id: str, method: MemoryMethod) -> list[Turn]:
        path = self._dir(user_id, method) / "transcript.jsonl"
        if not path.exists():
            return []
        return [Turn.from_dict(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]

    def save_profile(self, profile: FinancialProfile) -> Path:
        path = self._dir(profile.meta.user_id, profile.meta.memory_method) / "profiles" / f"v{profile.meta.profile_version}.json"
        _write_atomic(path, profile.model_dump_json(indent=2))
        return path

    def profile_versions(self, user_id: str, method: MemoryMethod) -> list[int]:
        folder = self._dir(user_id, method) / "profiles"
        if not folder.exists():
            return []
        return sorted(int(p.stem[1:]) for p in folder.glob("v*.json") if p.stem[1:].isdigit())

    def load_profile(self, user_id: str, method: MemoryMethod, version: int) -> FinancialProfile:
        path = self._dir(user_id, method) / "profiles" / f"v{version}.json"
        return FinancialProfile.model_validate_json(path.read_text())

    def latest_profile(self, user_id: str, method: MemoryMethod) -> Optional[FinancialProfile]:
        versions = self.profile_versions(user_id, method)
        return self.load_profile(user_id, method, versions[-1]) if versions else None
