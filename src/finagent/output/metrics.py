"""Deterministic plan metrics (TDD §10)."""

from __future__ import annotations

from typing import Optional

from finagent.output.config import PlanConfig
from finagent.output.models import (
    Budget,
    BudgetId,
    BudgetMetrics,
    Forecast,
    GoalStatus,
    Metrics,
    NormalizedProfile,
    RetirementProjection,
)


def _ratio(numerator: float, denominator: float) -> Optional[float]:
    return numerator / denominator if denominator > 0 else None


def _budget_metrics(budget: Budget, forecast: Forecast, projection: Optional[RetirementProjection]) -> BudgetMetrics:
    income = budget.income_monthly
    totals = budget.totals
    return BudgetMetrics(
        savings_rate=_ratio(totals.savings + max(0.0, totals.unallocated), income),
        need_share=_ratio(totals.need, income),
        want_share=_ratio(totals.want, income),
        end_cash_balance=forecast.end_state.cash_balance,
        goals_off_track=[v.goal_id for v in forecast.goals if v.status == GoalStatus.off_track],
        cash_shortfall_months=len(forecast.cash_shortfall_months),
        retirement_delta_years=projection.delta_years if projection is not None else None,
    )


def compute_metrics(
    norm: NormalizedProfile,
    budgets: dict[BudgetId, Budget],
    forecasts: dict[BudgetId, Forecast],
    retirement: dict[BudgetId, RetirementProjection],
    config: PlanConfig,
) -> Metrics:
    income = norm.net_monthly_income
    current = budgets.get(BudgetId.current)
    return Metrics(
        net_monthly_income=income,
        emergency_fund_months=_ratio(norm.current_savings_balance, current.totals.need) if current else None,
        debt_total=sum(d.balance for d in norm.debts),
        debt_to_income=_ratio(sum(d.minimum_payment or 0.0 for d in norm.debts), income),
        high_apr_debts=[d.id for d in norm.debts if d.apr is not None and d.apr >= config.high_apr_threshold],
        per_budget={
            bid.value: _budget_metrics(budget, forecasts[bid], retirement.get(bid))
            for bid, budget in budgets.items()
        },
    )
