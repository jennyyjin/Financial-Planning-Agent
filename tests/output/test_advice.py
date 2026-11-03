"""Tests for evidence refs, numeric grounding and the fallback recommendation (TDD §10, D4)."""

from __future__ import annotations

import pytest

from conftest import ALL_PROFILES, load_profile
from finagent.output import budget as budget_ops
from finagent.output import forecast as forecast_ops
from finagent.output import retirement as retirement_ops
from finagent.output.advice import (
    PlanContext,
    allowed_values,
    choose_budget,
    extract_numbers,
    fallback_recommendation,
    find_ungrounded,
    invalid_refs,
    overall_status,
    recommendation_texts,
    resolve_ref,
)
from finagent.output.config import PlanConfig
from finagent.output.metrics import compute_metrics
from finagent.output.models import BudgetId, GeneratedBy, OverallStatus, Recommendation
from finagent.output.normalize import normalize


def build_context(name: str, config: PlanConfig | None = None) -> PlanContext:
    config = config or PlanConfig()
    norm = normalize(load_profile(name), config)
    budgets = budget_ops.build_budgets(norm, config)
    forecasts = {bid: forecast_ops.forecast_budget(b, norm, config) for bid, b in budgets.items()}
    retirement = {bid: retirement_ops.project_retirement(b, forecasts[bid], norm, config) for bid, b in budgets.items()}
    metrics = compute_metrics(norm, budgets, forecasts, retirement, config)
    return PlanContext(norm, config, budgets, forecasts, retirement, metrics)


@pytest.fixture(scope="module")
def complete() -> PlanContext:
    return build_context("complete")


# --- PlanContext.data ----------------------------------------------------------------------


def test_data_has_plan_sections_in_json_shape(complete):
    data = complete.data()
    assert set(data) == {"budgets", "forecasts", "retirement", "metrics"}
    assert set(data["budgets"]) == {"current", "50_30_20", "goals_5_3"}
    assert data["budgets"]["current"]["income_monthly"] == pytest.approx(6200)
    assert isinstance(data["forecasts"]["current"]["months"][0]["month"], str)  # JSON mode: dates as strings


# --- resolve_ref ---------------------------------------------------------------------------


def test_resolve_ref_by_goal_id(complete):
    verdict = resolve_ref(complete.data(), "forecasts.current.goals.goal:japan-trip")
    assert verdict["status"] == "off_track"
    assert verdict["shortfall"] == pytest.approx(500)


def test_resolve_ref_by_line_id_and_adjustment(complete):
    data = complete.data()
    assert resolve_ref(data, "budgets.current.lines.rec:eating-out")["monthly_amount"] == pytest.approx(450)
    adj = resolve_ref(data, "budgets.50_30_20.adjustments.goal:japan-trip:contribution")
    assert adj["action"] == "allocate"


def test_resolve_ref_fields_index_month_and_dict_keys(complete):
    data = complete.data()
    assert resolve_ref(data, "retirement.goals_5_3.projected_retirement_age") == pytest.approx(
        complete.retirement[BudgetId.goals_5_3].projected_retirement_age
    )
    assert resolve_ref(data, "forecasts.current.months.0") == data["forecasts"]["current"]["months"][0]
    assert resolve_ref(data, "forecasts.current.months.2027-01-01")["month"] == "2027-01-01"
    assert resolve_ref(data, "forecasts.current.end_state.debt_balances.debt:federal-student-loan") > 0
    assert resolve_ref(data, "budgets.50_30_20.totals.want") == pytest.approx(570)


def test_resolve_ref_list_of_ids():
    data = build_context("overspender").data()
    assert resolve_ref(data, "metrics.high_apr_debts.debt:credit-card") == "debt:credit-card"
    assert resolve_ref(data, "metrics.high_apr_debts.0") == "debt:credit-card"


@pytest.mark.parametrize("ref", [
    "", "forecast.current.goals.goal:japan-trip", "forecasts.current.goals.goal:nope",
    "budgets.current.totals.need.extra", "budgets.unknown", "forecasts.current.months.99",
    "budgets..current", "recommendation.summary",
])
def test_resolve_ref_raises_key_error(complete, ref):
    with pytest.raises(KeyError):
        resolve_ref(complete.data(), ref)


def test_invalid_refs_lists_only_bad(complete):
    refs = ["metrics.emergency_fund_months", "metrics.nope", "budgets.current.totals"]
    assert invalid_refs(complete.data(), refs) == ["metrics.nope"]


# --- extract_numbers -----------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("Save $1,234.56 a month", [1234.56]),
    ("about $517k by 60", [517_000, 60]),
    ("1.5k", [1500]),
    ("a $1.2M nest egg", [1_200_000]),
    ("$2 million", [2_000_000]),
    ("wants are 45% of income", [45]),
    ("by June 2027", [2027]),
    ("3.5 years early", [3.5]),
    ("cut from $600 to $60/mo.", [600, 60]),
    ("Use your 401(k) match", []),
    ("see forecasts.current.goals.goal:japan-trip and budgets.50_30_20.totals.want", []),
    ("version v2 and Q4", []),
    ("5 kids", [5]),
])
def test_extract_numbers(text, expected):
    assert extract_numbers(text) == pytest.approx(expected)


# --- grounding -----------------------------------------------------------------------------


def test_grounding_accepts_rounded_tool_values(complete):
    allowed = allowed_values(complete)
    texts = [
        "Raise Japan trip savings to $571/mo; it is $500 short of $4,000 by June 2027.",  # 571.43 rounded
        "Needs are 39% of your $6,200 income.",                                           # share x100
        "Retire around age 46 instead of 52, against a target of 60.",                    # rounded ages
        "Your current budget leaves $2,287/mo unassigned, or $27,440 a year.",            # x12 annual
        "Use the 50/30/20 rule and a 3-month emergency fund ($7,330); assume 5% returns.",
        "Retiring 6 years earlier with a $1.5M target.",
    ]
    assert find_ungrounded(texts, allowed) == []


def test_grounding_rejects_invented_numbers(complete):
    allowed = allowed_values(complete)
    assert find_ungrounded(["You should save $777/mo and retire at 42 with $3.3M or $1,357."], allowed) == [777, 42, 3_300_000, 1357]


def test_grounding_small_integers_always_allowed():
    assert find_ungrounded(["You have 2 goals, 7 months and 12 payments; 13 is not."], []) == [13]


def test_grounding_tolerance_follows_written_precision():
    # "$517k" and "$100,000" are rounded figures (precision 500 and 50,000, capped at 5% of the value);
    # "$100,400" is written to the dollar, so it must be within 0.51 of an allowed value.
    assert find_ungrounded(["$100,000", "$517k", "$517,000"], [100_400, 516_600]) == []
    assert find_ungrounded(["$100,400"], [100_000]) == [100_400]
    assert find_ungrounded(["$101,000"], [100_000]) == [101_000]


def test_grounding_suffix_tolerance_is_bounded_relatively():
    """Reviewer repro: '$1 million' / '$2 million' against 1.5M and '$2k' against 2,443 were accepted."""
    assert find_ungrounded(["about $1 million", "about $2 million", "$2k"], [1_500_000, 2443.33]) == [
        1_000_000, 2_000_000, 2_000]
    assert find_ungrounded(["about $1.5 million", "$2.4k", "$1.2M"], [1_500_000, 2443.33, 1_152_089]) == []


def test_grounding_accepts_decades_of_allowed_values():
    assert find_ungrounded(["You could retire in your mid-40s."], [45.7]) == []
    assert find_ungrounded(["You could retire in your 30s."], [45.7]) == [30]


def test_extra_allowed_values_are_accepted(complete):
    assert find_ungrounded(["Investing $300 more"], allowed_values(complete)) == [300]
    assert find_ungrounded(["Investing $300 more"], allowed_values(complete, extra=[300.0])) == []


# --- fallback recommendation ----------------------------------------------------------------


@pytest.fixture(scope="module", params=ALL_PROFILES)
def fallback(request) -> tuple[PlanContext, Recommendation]:
    ctx = build_context(request.param)
    return ctx, fallback_recommendation(ctx)


def test_fallback_is_valid_and_marked(fallback):
    _, rec = fallback
    assert rec.generated_by is GeneratedBy.fallback
    assert 3 <= len(rec.major_changes) <= 5
    assert rec.summary
    for change in rec.major_changes:
        assert change.title and change.detail and change.impact and change.evidence_refs


def test_fallback_evidence_refs_resolve(fallback):
    ctx, rec = fallback
    data = ctx.data()
    assert invalid_refs(data, [r for c in rec.major_changes for r in c.evidence_refs]) == []


def test_fallback_numbers_are_grounded(fallback):
    ctx, rec = fallback
    assert find_ungrounded(recommendation_texts(rec), allowed_values(ctx)) == []


def test_fallback_is_deterministic():
    assert fallback_recommendation(build_context("big_goals")) == fallback_recommendation(build_context("big_goals"))


def test_complete_needs_attention_because_japan_off_track(complete):
    rec = fallback_recommendation(complete)
    assert rec.overall_status is OverallStatus.needs_attention
    assert rec.recommended_budget is BudgetId.rule_50_30_20  # tie with goals_5_3 goes to 50/30/20
    titles = " ".join(c.title for c in rec.major_changes)
    assert "Japan trip" in titles and "$571" in titles
    japan = next(c for c in rec.major_changes if "Japan" in c.title)
    assert "forecasts.current.goals.goal:japan-trip" in japan.evidence_refs


def test_overspender_is_at_risk_and_cuts_dining():
    ctx = build_context("overspender")
    rec = fallback_recommendation(ctx)
    assert rec.overall_status in (OverallStatus.at_risk, OverallStatus.needs_attention)
    assert overall_status(ctx) is OverallStatus.at_risk  # cash runs negative in the current budget
    assert rec.recommended_budget is BudgetId.rule_50_30_20
    assert any("Dining out" in c.title for c in rec.major_changes)
    assert any("Credit card" in c.title for c in rec.major_changes)
    assert any("24% APR" in r for r in rec.risks)


def test_big_goals_recommends_goals_budget():
    assert choose_budget(build_context("big_goals")) is BudgetId.goals_5_3


def test_no_retirement_mentions_missing_data():
    rec = fallback_recommendation(build_context("no_retirement"))
    assert any("Retirement could not be projected" in r for r in rec.risks)
    assert any(c.title == "Add your retirement details" for c in rec.major_changes)


def test_on_track_when_nothing_is_wrong(complete):
    # A 2-month guideline clears the emergency-fund flag and the Japan goal is the only issue left.
    ctx = build_context("complete", PlanConfig(emergency_months=2))
    assert overall_status(ctx) is OverallStatus.needs_attention
    ctx.forecasts[BudgetId.current] = ctx.forecasts[BudgetId.rule_50_30_20].model_copy(
        update={"budget_id": BudgetId.current}
    )
    assert overall_status(ctx) is OverallStatus.on_track


def test_current_recommended_when_it_dominates(complete):
    ctx = build_context("complete")
    better = ctx.forecasts[BudgetId.rule_50_30_20].model_copy(update={"budget_id": BudgetId.current})
    ctx.forecasts[BudgetId.current] = better
    faster = ctx.retirement[BudgetId.rule_50_30_20].model_copy(
        update={"budget_id": BudgetId.current, "projected_retirement_age": 40.0}
    )
    ctx.retirement[BudgetId.current] = faster
    assert choose_budget(ctx) is BudgetId.current
    rec = fallback_recommendation(ctx)
    assert rec.major_changes[0].title == "Keep following your current budget"


# --- review regressions: advice wording matches the plan -------------------------------------------


def _all_text(rec: Recommendation) -> str:
    return " ".join(recommendation_texts(rec))


def test_debt_advice_does_not_claim_a_prepayment_the_budget_lacks():
    """Overspender: no budget has room to prepay the 24% card, so the advice must say so."""
    ctx = build_context("overspender")
    rec = fallback_recommendation(ctx)
    debt = next(c for c in rec.major_changes if "Credit card" in c.title)
    assert debt.title == "Pay down Credit card as soon as there is room"
    assert "only the minimum is paid" in debt.detail
    with_room = build_context("overspender", PlanConfig(emergency_months=0))
    debt = next(c for c in fallback_recommendation(with_room).major_changes if "Credit card" in c.title)
    assert debt.title == "Pay down Credit card first" and "on top of the minimum" in debt.detail


def test_savings_rate_and_unassigned_cash_are_reported_apart():
    ctx = build_context("no_retirement")
    rec = fallback_recommendation(ctx)
    m = ctx.metrics.per_budget["current"]
    assert m.unallocated_share > 0
    assert f"leave {m.unallocated_share * 100:.0f}% unassigned" in rec.summary


def test_savings_target_is_judged():
    rec = fallback_recommendation(build_context("mislabeled"))  # target $300/mo, current saves $100
    assert "less than your target of $300/mo" in rec.summary


def test_unknown_apr_and_missing_payment_are_risks_not_strengths():
    rec = fallback_recommendation(build_context("incomplete"))
    assert not any("APR or more" in s for s in rec.strengths)
    assert any("no payment" in r and "$2,400" in r for r in rec.risks)
    assert any("APR of Credit card is unknown" in r for r in rec.risks)
    assert "not off track" not in rec.summary and "no deadline" in rec.summary


def test_no_goal_count_when_there_are_no_goals():
    for name in ("mislabeled", "no_retirement"):
        assert "goals off track" not in fallback_recommendation(build_context(name)).summary


def test_retirement_advice_shows_the_cash_split_and_holds():
    ctx = build_context("mislabeled")
    rec = fallback_recommendation(ctx)
    change = next(c for c in rec.major_changes if "retire by" in c.title)
    assert "kept as cash" in change.detail and "invested" in change.impact
    rid = rec.recommended_budget
    proj = ctx.retirement[rid]
    hit = retirement_ops.project_retirement(ctx.budgets[rid], ctx.forecasts[rid], ctx.norm, ctx.config,
                                            extra_monthly=proj.required_monthly_to_hit_target - proj.monthly_investment)
    assert hit.matches_target is True


def test_fallback_makes_no_false_claims():
    from finagent.output.advice import false_claims

    for name in ALL_PROFILES:
        ctx = build_context(name)
        assert false_claims(ctx, fallback_recommendation(ctx)) == [], name
