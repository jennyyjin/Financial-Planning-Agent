from __future__ import annotations

from datetime import date

import pytest

from finagent.output.budget import build_current_budget, refresh
from finagent.output.models import (
    BudgetId,
    EndState,
    Forecast,
    GoalStatus,
    GoalVerdict,
    RetirementProjection,
)
from finagent.output.metrics import compute_metrics

END = date(2027, 10, 1)


def make_forecast(budget_id: BudgetId, cash: float, verdicts=(), shortfalls=()) -> Forecast:
    return Forecast(
        budget_id=budget_id, months=[], goals=list(verdicts), cash_shortfall_months=list(shortfalls),
        end_state=EndState(month=END, cash_balance=cash, goal_balances={}, debt_balances={}, retirement_balance=0),
    )


def verdict(goal_id: str, status: GoalStatus) -> GoalVerdict:
    return GoalVerdict(goal_id=goal_id, name=goal_id, status=status)


def test_complete_profile_metrics(normalized_named, config):
    norm = normalized_named("complete")
    current = build_current_budget(norm)
    forecasts = {BudgetId.current: make_forecast(
        BudgetId.current, 21_000, verdicts=[verdict("goal:japan-trip", GoalStatus.off_track)])}
    retirement = {BudgetId.current: RetirementProjection(budget_id=BudgetId.current, status="ok", delta_years=4.5)}

    m = compute_metrics(norm, {BudgetId.current: current}, forecasts, retirement, config)

    assert m.net_monthly_income == pytest.approx(6200)
    assert m.emergency_fund_months == pytest.approx(6500 / 2443.333, rel=1e-4)
    assert m.debt_total == pytest.approx(18_000)
    assert m.debt_to_income == pytest.approx(210 / 6200)
    assert m.high_apr_debts == []

    bm = m.per_budget["current"]
    assert bm.savings_rate == pytest.approx((900 + 2286.667) / 6200, rel=1e-4)
    assert bm.need_share == pytest.approx(2443.333 / 6200, rel=1e-4)
    assert bm.want_share == pytest.approx(570 / 6200)
    assert bm.end_cash_balance == pytest.approx(21_000)
    assert bm.goals_off_track == ["goal:japan-trip"]
    assert bm.cash_shortfall_months == 0
    assert bm.retirement_delta_years == pytest.approx(4.5)


def test_high_apr_and_shortfalls(normalized_named, config):
    norm = normalized_named("overspender")
    current = build_current_budget(norm)
    forecasts = {BudgetId.current: make_forecast(
        BudgetId.current, -1200, verdicts=[verdict("goal:emergency-fund", GoalStatus.on_track)],
        shortfalls=[date(2027, 3, 1), date(2027, 4, 1)])}
    m = compute_metrics(norm, {BudgetId.current: current}, forecasts, {}, config)

    assert m.high_apr_debts == [norm.debts[0].id]
    assert m.debt_to_income == pytest.approx(150 / 4000)
    bm = m.per_budget["current"]
    assert bm.savings_rate == pytest.approx(260 / 4000)  # no savings lines; the surplus counts
    assert bm.goals_off_track == []
    assert bm.cash_shortfall_months == 2
    assert bm.retirement_delta_years is None


def test_negative_unallocated_does_not_lower_savings_rate(normalized_named, config):
    norm = normalized_named("complete").model_copy(update={"net_monthly_income": 3000.0})
    current = build_current_budget(norm)
    assert current.totals.unallocated < 0
    m = compute_metrics(norm, {BudgetId.current: current}, {BudgetId.current: make_forecast(BudgetId.current, 0)},
                        {}, config)
    assert m.per_budget["current"].savings_rate == pytest.approx(900 / 3000)


def test_high_apr_threshold_inclusive(normalized_named, config):
    norm = normalized_named("overspender")
    norm.debts[0].apr = config.high_apr_threshold
    m = compute_metrics(norm, {}, {}, {}, config)
    assert m.high_apr_debts == [norm.debts[0].id]


def test_zero_income_and_no_needs(normalized_named, config):
    norm = normalized_named("complete").model_copy(update={"net_monthly_income": 0.0, "lines": []})
    current = build_current_budget(norm)
    m = compute_metrics(norm, {BudgetId.current: current}, {BudgetId.current: make_forecast(BudgetId.current, 0)},
                        {}, config)
    assert m.debt_to_income is None
    assert m.emergency_fund_months is None
    bm = m.per_budget["current"]
    assert bm.savings_rate is None and bm.need_share is None and bm.want_share is None


def test_per_budget_keys_for_every_budget(normalized_named, config):
    norm = normalized_named("complete")
    current = build_current_budget(norm)
    other = refresh(current.model_copy(deep=True, update={"id": BudgetId.rule_50_30_20}))
    budgets = {BudgetId.current: current, BudgetId.rule_50_30_20: other}
    forecasts = {bid: make_forecast(bid, 1000) for bid in budgets}
    m = compute_metrics(norm, budgets, forecasts, {}, config)
    assert set(m.per_budget) == {"current", "50_30_20"}
