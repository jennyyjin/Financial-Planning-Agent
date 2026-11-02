"""Live smoke test (TDD §14.6): the real model on every fixture. Skipped unless FINAGENT_LLM_MODEL is set.

    FINAGENT_LLM_MODEL=gpt-4o-mini FINAGENT_LLM_API_KEY=... pytest -m live tests/output/test_live.py
"""

from __future__ import annotations

import os

import pytest

from conftest import ALL_PROFILES, load_profile
from invariants import check_budget_invariants

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("FINAGENT_LLM_MODEL"), reason="FINAGENT_LLM_MODEL is not set"),
]


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_live_plan_holds_the_invariants(name, tmp_path):
    from finagent.output import PlanConfig, generate_plan
    from finagent.output.agents.runtime import OpenAICompatibleModel
    from finagent.output.models import BudgetId
    from finagent.output.normalize import normalize

    config = PlanConfig()
    profile = load_profile(name)
    plan = generate_plan(profile, config, model=OpenAICompatibleModel.from_env())
    norm = normalize(profile, config)
    for bid in (BudgetId.rule_50_30_20, BudgetId.goals_5_3):
        check_budget_invariants(norm, plan.budgets[bid], config)
    out = os.environ.get("FINAGENT_LIVE_OUT")
    if out:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, f"{name}_plan.json"), "w") as f:
            f.write(plan.model_dump_json(indent=2))
