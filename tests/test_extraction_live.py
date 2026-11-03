"""Live acceptance test: extract Daisy's example narrative with a real model.

    FINAGENT_LLM_MODEL=gpt-4o-mini FINAGENT_LLM_API_KEY=... pytest -m live tests/test_extraction_live.py

Items are matched by amount rather than name, since the model may name them differently.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from finagent.extraction import extract_profile
from finagent.llm import UsageLog, model_from_env
from finagent.memory import FullHistory
from finagent.schema import FinancialProfile, MemoryMethod, Meta

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("FINAGENT_LLM_MODEL"), reason="FINAGENT_LLM_MODEL is not set"),
]

PROFILES = Path(__file__).resolve().parents[1] / "examples" / "profiles"
AS_OF = datetime(2026, 11, 2, 9, 0)


def _by_amount(items):
    return {(i.amount, i.frequency): i for i in items}


def test_live_extracts_narrative_example():
    expected = FinancialProfile.model_validate_json((PROFILES / "narrative_profile.json").read_text())
    memory = FullHistory()
    memory.add_user_turn((PROFILES / "narrative_profile.txt").read_text().strip(), AS_OF)
    meta = Meta(user_id="u_demo", as_of=AS_OF, profile_version=1, memory_method=MemoryMethod.full_history)
    log = UsageLog()
    got = extract_profile(model_from_env(), memory.extraction_context(), meta, log=log)

    out = os.environ.get("FINAGENT_LIVE_OUT")
    if out:
        os.makedirs(out, exist_ok=True)
        Path(out, "narrative_extracted.json").write_text(got.model_dump_json(indent=2))

    assert [(i.amount, i.frequency, i.is_net) for i in got.income] == [(5000, "monthly", True)]

    exp_rec, got_rec = _by_amount(expected.recurring_expenses), _by_amount(got.recurring_expenses)
    assert set(got_rec) == set(exp_rec)
    for key, item in exp_rec.items():
        assert (got_rec[key].category, got_rec[key].need_or_want) == (item.category, item.need_or_want), item.name

    assert got.one_time_expenses == []
    assert sorted((g.target_amount, g.deadline) for g in got.savings_goals) == sorted(
        (g.target_amount, g.deadline) for g in expected.savings_goals
    )
    assert sorted((d.type, d.balance, d.apr, d.minimum_payment) for d in got.debts) == sorted(
        (d.type, d.balance, d.apr, d.minimum_payment) for d in expected.debts
    )
    assert got.retirement is not None and got.retirement.monthly_contribution == 250
    assert got.current_savings_balance == 5000
    assert got.overall_savings_target is None
    print(json.dumps(log.totals()))
