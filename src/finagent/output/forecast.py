"""Twelve-month cash-flow forecast and goal verdicts (TDD §8).

Budgets carry every line at its go-forward amount; the forecast additionally
respects `effective_date`, so an item only starts in the month it takes effect.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Optional

from finagent.output import goals as goal_math
from finagent.output.config import PlanConfig, monthly_rate
from finagent.output.format import CASH_SAVINGS_KINDS
from finagent.output.goals import add_months
from finagent.output.models import (
    Budget,
    BudgetLine,
    Bucket,
    EndState,
    Forecast,
    ForecastMonth,
    GoalInfo,
    GoalStatus,
    GoalVerdict,
    LineKind,
    NormalizedProfile,
)

_CASH_SAVINGS = CASH_SAVINGS_KINDS


def month_starts(start_month: date, n: int) -> list[date]:
    return [add_months(start_month, i) for i in range(n)]


def is_active(effective_date: Optional[date], month: date) -> bool:
    """An item is active in a month if it takes effect on or before the month's last day."""
    return effective_date is None or effective_date < add_months(month, 1)


def _sum_active(lines: list[BudgetLine], month: date, kind: LineKind, bucket: Bucket | None = None) -> float:
    return sum(
        line.monthly_amount for line in lines
        if line.kind is kind and (bucket is None or line.bucket is bucket) and is_active(line.effective_date, month)
    )


def _same_month(a: date, b: date) -> bool:
    return a.year == b.year and a.month == b.month


def forecast_budget(budget: Budget, norm: NormalizedProfile, config: PlanConfig | None = None) -> Forecast:
    config = config or PlanConfig()
    months = month_starts(norm.start_month, config.horizon_months)
    r_m = monthly_rate(config.real_return)
    c_m = monthly_rate(config.cash_real_return)

    cash = norm.current_savings_balance
    retirement = norm.retirement.current_balance if norm.retirement else 0.0
    goal_funds = {g.id: 0.0 for g in norm.goals}
    debt_balances = {d.id: d.balance for d in norm.debts}
    targets = {g.id: g.target_amount for g in norm.goals}

    rows: list[ForecastMonth] = []
    shortfalls: list[date] = []
    for month in months:
        active = [line for line in budget.lines if is_active(line.effective_date, month)]
        income = sum(inc.monthly_amount for inc in norm.income if is_active(inc.effective_date, month))
        needs = _sum_active(active, month, LineKind.recurring, Bucket.need)
        wants = _sum_active(active, month, LineKind.recurring, Bucket.want)
        one_time = sum(
            line.monthly_amount for line in budget.one_time
            if line.expected_date is not None and _same_month(line.expected_date, month)
        )

        debt_paid = 0.0
        for debt in norm.debts:
            balance = debt_balances[debt.id]
            balance += balance * (debt.apr or 0.0) / 100 / 12
            scheduled = sum(
                line.monthly_amount for line in active
                if line.kind in (LineKind.debt_min, LineKind.debt_extra) and line.target_ref == debt.id
            )
            payment = min(balance, scheduled)
            debt_balances[debt.id] = balance - payment
            debt_paid += payment

        goal_paid = 0.0
        for line in active:
            if line.kind is not LineKind.goal_contribution or line.target_ref is None:
                continue
            fund = goal_funds.setdefault(line.target_ref, 0.0)
            target = targets.get(line.target_ref)
            contribution = line.monthly_amount if target is None else min(line.monthly_amount, max(0.0, target - fund))
            goal_funds[line.target_ref] = fund + contribution
            goal_paid += contribution

        ret_contribution = _sum_active(active, month, LineKind.retirement_contribution)
        retirement = retirement * (1 + r_m) + ret_contribution
        cash_savings = sum(line.monthly_amount for line in active if line.kind in _CASH_SAVINGS)

        net = income - needs - wants - one_time - debt_paid - goal_paid - ret_contribution
        cash = cash * (1 + c_m) + net
        if cash < -config.money_tolerance:
            shortfalls.append(month)

        rows.append(ForecastMonth(
            month=month, income=income, needs=needs, wants=wants, one_time=one_time,
            debt_payments=debt_paid, goal_contributions=goal_paid, retirement_contribution=ret_contribution,
            emergency_and_general_savings=cash_savings, net_cash_flow=net, cash_balance=cash,
            goal_balances=dict(goal_funds), debt_balances=dict(debt_balances), retirement_balance=retirement,
        ))

    last = rows[-1]
    return Forecast(
        budget_id=budget.id,
        months=rows,
        goals=goal_verdicts(norm, rows, config),
        cash_shortfall_months=shortfalls,
        end_state=EndState(
            month=last.month, cash_balance=last.cash_balance, goal_balances=dict(last.goal_balances),
            debt_balances=dict(last.debt_balances), retirement_balance=last.retirement_balance,
        ),
    )


# --- Goal verdicts ------------------------------------------------------------------------


def _completion_date(
    goal: GoalInfo, rows: list[ForecastMonth], rate: float, window_end: date, tol: float
) -> Optional[date]:
    """First month in which the fund reaches its target: simulated in the window, then extrapolated at `rate`."""
    target = goal.target_amount
    if target is None:
        return None
    if target - tol <= 0:
        return rows[0].month
    for row in rows:
        if row.goal_balances.get(goal.id, 0.0) >= target - tol:
            return row.month
    remaining = target - rows[-1].goal_balances.get(goal.id, 0.0)
    if rate <= 0:
        return None
    return add_months(window_end, max(1, math.ceil((remaining - tol) / rate)) - 1)


def goal_verdict(goal: GoalInfo, rows: list[ForecastMonth], start_month: date, config: PlanConfig) -> GoalVerdict:
    tol = config.money_tolerance
    window_end = add_months(rows[-1].month, 1)  # first day after the forecast
    end_fund = rows[-1].goal_balances.get(goal.id, 0.0)
    prev_fund = rows[-2].goal_balances.get(goal.id, 0.0) if len(rows) > 1 else 0.0
    # The goal's contribution in the last month; snap float noise from the balance difference.
    rate = round(end_fund - prev_fund, 9)
    verdict = GoalVerdict(
        goal_id=goal.id, name=goal.name, status=GoalStatus.no_deadline,
        target_amount=goal.target_amount, deadline=goal.deadline, current_monthly=rate,
    )
    target = goal.target_amount
    if target is None:
        return verdict
    verdict.projected_completion_date = _completion_date(goal, rows, rate, window_end, tol)
    if goal.deadline is None:
        return verdict

    if goal.deadline <= window_end:
        before = [row for row in rows if row.month < goal.deadline]
        balance = before[-1].goal_balances.get(goal.id, 0.0) if before else 0.0
        status = GoalStatus.met if balance >= target - tol else GoalStatus.off_track
    elif end_fund >= target - tol:
        balance, status = end_fund, GoalStatus.met  # fully funded inside the window, ahead of the deadline
    else:
        extra = rate * goal_math.contribution_months(window_end, goal.deadline)
        balance = min(target, end_fund + extra)  # contributions stop at the target
        status = GoalStatus.on_track if balance >= target - tol else GoalStatus.off_track

    verdict.status = status
    verdict.balance_at_deadline = balance
    if status is GoalStatus.off_track:
        verdict.shortfall = target - balance
    if status is not GoalStatus.met:
        verdict.required_monthly = goal_math.required_monthly(goal, start_month)
    return verdict


def goal_verdicts(norm: NormalizedProfile, rows: list[ForecastMonth], config: PlanConfig | None = None) -> list[GoalVerdict]:
    config = config or PlanConfig()
    return [goal_verdict(goal, rows, norm.start_month, config) for goal in norm.goals]
