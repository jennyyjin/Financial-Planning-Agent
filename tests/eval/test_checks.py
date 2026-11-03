"""Scenarios, the oracle, and the code checks.

The mutation tests break a correct plan in one specific way and check that the matching check fails.
"""

from datetime import date
from pathlib import Path

import pytest

from finagent.eval.checks import (
    compare_plans, compare_profiles, grade_plan, name_similarity, plan_facts, run_probe, safety_flags,
)
from finagent.eval.data import ARCHETYPES, Probe, generate, load_scenarios, save_jsonl
from finagent.eval.oracle import PROTECTED_CATEGORIES, OneTime, compute_facts, contribution_months, set_aside_for
from finagent.output import generate_plan
from finagent.output.config import DEFAULT_PROTECTED_CATEGORIES
from finagent.output.models import BudgetId, GoalStatus, LineKind
from finagent.output.pipeline import load_profile

ROOT = Path(__file__).resolve().parents[2]
GOLD = ROOT / "eval" / "scenarios" / "gold"
FIXTURES = [ROOT / "examples/profiles/complete_profile.json"] + sorted((ROOT / "tests/output/fixtures").glob("*.json"))


@pytest.fixture(scope="module")
def scenarios():
    return generate(3, seed=1) + load_scenarios(GOLD)


@pytest.fixture
def over():
    profile = load_profile(ROOT / "tests/output/fixtures/overspender.json")
    return profile, generate_plan(profile)


def failed(plan, profile, prefix=""):
    return {c.id for c in grade_plan(plan, profile).failures() if c.id.startswith(prefix)}


def line(plan, bid, name):
    return next(l for l in plan.budgets[BudgetId(bid)].lines if l.name == name)


# scenarios 

def test_generator_is_reproducible_and_covers_every_archetype():
    a, b = generate(2, seed=3), generate(2, seed=3)
    assert [s.model_dump_json() for s in a] == [s.model_dump_json() for s in b]
    assert {s.archetype for s in a} == set(ARCHETYPES)


def test_probes_pass_on_their_turn_and_fail_on_the_turn_before(scenarios):
    count = 0
    for s in scenarios:
        assert s.final_turn().expected_status == "ready"
        for i, t in enumerate(s.turns):
            for p in t.probes:
                count += 1
                assert run_probe(t.profile(), p).passed, (s.id, i)
                if i > 0:
                    assert not run_probe(s.turns[i - 1].profile(), p).passed, (s.id, i)
    assert count > 30


def test_missing_info_is_followed_by_a_clarification(scenarios):
    for s in scenarios:
        if s.turns[0].expected_status == "needs_clarification":
            assert s.turns[0].expected_missing and s.turns[1].event == "clarification"


def test_jsonl_roundtrip(tmp_path, scenarios):
    assert [s.id for s in load_scenarios(save_jsonl(scenarios, tmp_path / "s.jsonl"))] == sorted(s.id for s in scenarios)


# oracle

def test_protected_categories_match_the_output_phase():
    assert PROTECTED_CATEGORIES == {c.value for c in DEFAULT_PROTECTED_CATEGORIES}


def test_contribution_months():
    assert contribution_months(date(2026, 11, 1), date(2027, 6, 1)) == 7
    assert contribution_months(date(2026, 11, 1), date(2027, 6, 15)) == 8
    assert contribution_months(date(2026, 11, 1), date(2026, 10, 1)) == 0


def test_oracle_matches_the_worked_example_in_the_spec():
    f = compute_facts(load_profile(ROOT / "examples/profiles/complete_profile.json"))
    assert f.income == 6200 and f.wants == 570 and f.savings_stated == 900
    assert f.needs == pytest.approx(2443.33, abs=0.01)
    assert f.s_req == pytest.approx(1240)
    assert f.goals[0].required_monthly == pytest.approx(571.43, abs=0.01)
    assert f.goals[0].status_current == "off_track"


def test_set_aside_uses_the_largest_running_requirement():
    items = [OneTime("a", 1250, date(2026, 11, 1)), OneTime("b", 1050, date(2027, 2, 1)), OneTime("c", 2850, date(2027, 4, 1))]
    assert set_aside_for(items, 1000, date(2026, 11, 1)) == pytest.approx((5150 - 1000) / 6)


def test_medication_labelled_want_is_a_protected_need():
    f = compute_facts(load_profile(ROOT / "tests/output/fixtures/mislabeled.json"))
    med = next(i for i in f.items if i.name == "Medication")
    assert med.protected and med.bucket == "need"


# plan checks

@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_deterministic_plans_pass(path):
    profile = load_profile(path)
    assert {c.id for c in grade_plan(generate_plan(profile), profile).failures() if c.group != "advice"} == set()


def test_cutting_rent(over):
    profile, plan = over
    line(plan, "50_30_20", "Rent").monthly_amount -= 300
    plan.budgets[BudgetId.rule_50_30_20].totals.need -= 300
    assert {"50_30_20.protected_unchanged", "50_30_20.needs_unchanged", "50_30_20.sums_to_income"} <= failed(plan, profile)


def test_wants_over_allowance(over):
    profile, plan = over
    line(plan, "goals_5_3", "Dining out").monthly_amount += 200
    plan.budgets[BudgetId.goals_5_3].totals.want += 200
    plan.budgets[BudgetId.goals_5_3].totals.savings -= 200
    assert "goals_5_3.wants_within_allowance" in failed(plan, profile)


def test_cutting_more_than_needed(over):
    profile, plan = over
    dining = line(plan, "goals_5_3", "Dining out")
    plan.budgets[BudgetId.goals_5_3].totals.want -= dining.monthly_amount
    dining.monthly_amount = 0.0
    assert "goals_5_3.no_overcut" in failed(plan, profile)


def test_changing_the_current_budget(over):
    profile, plan = over
    line(plan, "current", "Dining out").monthly_amount = 100
    plan.budgets[BudgetId.current].totals.want -= 500
    assert {"current.untouched", "current.want_total"} <= failed(plan, profile)


def test_wrong_feasibility_and_targets(over):
    profile, plan = over
    plan.budgets[BudgetId.rule_50_30_20].feasible = True
    plan.budgets[BudgetId.goals_5_3].targets.savings_min = 0.0
    assert {"50_30_20.feasible_flag", "goals_5_3.targets"} <= failed(plan, profile)


def test_wrong_goal_verdicts(over):
    profile, plan = over
    plan.forecasts[BudgetId.current].goals[0].status = GoalStatus.met
    next(l for l in plan.budgets[BudgetId.rule_50_30_20].lines if l.kind is LineKind.goal_contribution).monthly_amount = 10
    f = failed(plan, profile, "goals.")
    assert any(x.startswith("goals.current.verdict[") for x in f)
    assert any(x.startswith("goals.50_30_20.verdict[") for x in f)


def test_wrong_retirement_numbers(over):
    profile, plan = over
    plan.retirement[BudgetId.rule_50_30_20].retirement_number *= 2
    plan.retirement[BudgetId.goals_5_3].matches_target = not plan.retirement[BudgetId.goals_5_3].matches_target
    assert {"retirement.50_30_20.number", "retirement.goals_5_3.matches_target"} <= failed(plan, profile)


def test_retirement_age_too_late():
    profile = load_profile(ROOT / "examples/profiles/complete_profile.json")
    plan = generate_plan(profile)
    plan.retirement[BudgetId.rule_50_30_20].projected_retirement_age += 15
    assert "retirement.50_30_20.age_not_too_late" in failed(plan, profile)


def test_advice_breaking_the_rules(over):
    profile, plan = over
    rec = plan.recommendation
    rec.recommended_budget = BudgetId.current
    rec.overall_status = type(rec.overall_status).on_track
    rec.summary += " Index funds are guaranteed to return 10% a year, so buy VOO."
    assert {"advice.budget_rule", "advice.status_rule", "advice.safety[guarantee]", "advice.safety[products]"} <= failed(plan, profile)


def test_advice_missing_key_issues(over):
    profile, plan = over
    for c in plan.recommendation.major_changes:
        c.title = c.detail = c.impact = "Keep going."
        c.evidence_refs = []
    plan.recommendation.summary = "Keep going."
    assert {"advice.covers[high_apr_debt:Credit card]", "advice.covers[emergency_fund]"} <= failed(plan, profile)


def test_guarantee_lint():
    assert not safety_flags("Paying more than the minimum is a guaranteed saving on interest.")["guarantee"]
    assert safety_flags("Stocks have guaranteed returns.")["guarantee"]


def test_extraction_errors_change_plan_decisions():
    profile = load_profile(ROOT / "tests/output/fixtures/overspender.json")
    truth = plan_facts(generate_plan(profile))
    assert not compare_plans(truth, truth).failures()
    wrong = profile.model_copy(deep=True)
    wrong.income[0].amount = 6000
    f = {c.id for c in compare_plans(plan_facts(generate_plan(wrong)), truth).failures()}
    assert "e2e.income" in f and any(x.startswith(("e2e.50_30_20.", "e2e.goals_5_3.")) for x in f)


# profile and memory checks

def test_profile_comparison():
    truth = load_profile(ROOT / "examples/profiles/complete_profile.json")
    assert compare_profiles(truth, truth)["exact"]
    pred = truth.model_copy(deep=True)
    pred.recurring_expenses[2].name = "Dining out"  # truth says "Eating out"
    pred.recurring_expenses[3].need_or_want = "need"  # yoga labelled wrong
    del pred.recurring_expenses[1]  # groceries missing
    pred.current_savings_balance = 5000
    m = compare_profiles(pred, truth)
    assert m["label_acc"] == pytest.approx(2 / 3)
    assert m["item_recall"] < 1 and m["scalar_acc"] < 1 and not m["exact"]
    assert name_similarity("Uber rides", "Rideshares") > 0


def test_probe_flags_stale_values():
    truth = load_profile(ROOT / "examples/profiles/complete_profile.json")
    r = run_probe(truth, Probe(kind="scalar", path="derived.income_monthly", expected=1900, stale=6200))
    assert not r.passed and r.stale
    assert run_probe(truth, Probe(kind="item_absent", in_list="recurring_expenses", match=["yoga"])).stale
