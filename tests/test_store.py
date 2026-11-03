from datetime import datetime
from pathlib import Path

import pytest

from finagent.memory import Turn
from finagent.schema import FinancialProfile, MemoryMethod
from finagent.store import Store

PROFILES = Path(__file__).resolve().parents[1] / "examples" / "profiles"
FH = MemoryMethod.full_history


def profile(version: int, user_id: str = "u_001") -> FinancialProfile:
    p = FinancialProfile.model_validate_json((PROFILES / "complete_profile.json").read_text())
    return p.model_copy(update={"meta": p.meta.model_copy(update={
        "profile_version": version, "user_id": user_id, "memory_method": FH})})


def test_profiles_are_versioned(tmp_path):
    store = Store(tmp_path)
    assert store.latest_profile("u_001", FH) is None
    store.save_profile(profile(1))
    path = store.save_profile(profile(2))
    assert path == tmp_path / "u_001" / "full_history" / "profiles" / "v2.json"
    assert store.profile_versions("u_001", FH) == [1, 2]
    assert store.latest_profile("u_001", FH).meta.profile_version == 2
    assert store.load_profile("u_001", FH, 1) == profile(1)


def test_methods_are_kept_apart(tmp_path):
    store = Store(tmp_path)
    store.save_memory_state("u_001", FH, {"turns": []})
    assert store.load_memory_state("u_001", MemoryMethod.rolling_summary) is None
    assert store.load_memory_state("u_001", FH) == {"turns": []}


def test_transcript_appends(tmp_path):
    store = Store(tmp_path)
    t = datetime(2026, 11, 3, 9, 0)
    store.append_transcript("u_001", FH, [Turn(1, "user", "hi", t), Turn(2, "assistant", "hello", t)])
    store.append_transcript("u_001", FH, [Turn(3, "user", "bye", t)])
    assert [x.text for x in store.transcript("u_001", FH)] == ["hi", "hello", "bye"]


def test_session_round_trip(tmp_path):
    store = Store(tmp_path)
    assert store.load_session("u_001", FH) == {}
    store.save_session("u_001", FH, {"last_ready_version": 3})
    assert store.load_session("u_001", FH) == {"last_ready_version": 3}


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "x" * 65, "me@example.com"])
def test_rejects_unsafe_user_ids(tmp_path, bad):
    with pytest.raises(ValueError):
        Store(tmp_path).load_session(bad, FH)


def test_root_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FINAGENT_DATA_DIR", str(tmp_path))
    assert Store().root == tmp_path
