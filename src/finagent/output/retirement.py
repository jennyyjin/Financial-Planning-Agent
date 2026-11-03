"""Retirement projection with the 4% rule (TDD §9).

The long-run projection starts from the forecast's end state and steps month
by month until investable assets reach the retirement number or the user
reaches `config.max_age`. Investable assets are two pools: the retirement
portfolio and cash above the emergency reserve (optional). Goal funds are
never investable.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Literal, Optional

from finagent.output.config import PlanConfig, monthly_rate
from finagent.output.format import CASH_SAVINGS_KINDS
from finagent.output.goals import add_months
from finagent.output.models import (
    Budget,
    Forecast,
    LineKind,
    NormalizedProfile,
    RetirementProjection,
)

NumberSource = Literal["desired_annual_spending", "target", "budget_spending"]

_CASH_KINDS = CASH_SAVINGS_KINDS
_DEBT_KINDS = (LineKind.debt_min, LineKind.debt_extra)


def fv(present: float, monthly: float, months: int, monthly_rate: float) -> float:
    """Future value of `present` plus `monthly` deposited at the end of each month."""
    if monthly_rate == 0:
        return present + monthly * months
    growth = (1 + monthly_rate) ** months
    return present * growth + monthly * (growth - 1) / monthly_rate


def retirement_number(budget: Budget, norm: NormalizedProfile, config: PlanConfig) -> tuple[float, NumberSource, float]:
    """(number, source, implied annual spending), from the first available source."""
    multiple = config.retirement_multiple
    ret = norm.retirement
    if ret is not None and ret.desired_annual_spending is not None:
        return ret.desired_annual_spending * multiple, "desired_annual_spending", ret.desired_annual_spending
    if ret is not None and ret.target is not None:
        return ret.target, "target", ret.target / multiple
    spending = 12 * (budget.totals.need + budget.totals.want)
    return spending * multiple, "budget_spending", spending


def emergency_reserve(budget: Budget, config: PlanConfig) -> float:
    return config.emergency_months * budget.totals.need


@dataclass
class _Goal:
    fund: float
    target: Optional[float]
    deadline: Optional[date]
    contribution: float


@dataclass
class _Debt:
    balance: float
    monthly_interest: float
    payment: float


@dataclass
class _Simulation:
    """Post-forecast state. `cash` is the full cash balance; only the part above the reserve is investable."""

    portfolio: float
    cash: float
    reserve: float
    portfolio_rate: float
    cash_rate: float
    count_cash: bool
    redirect: bool
    portfolio_inflow: float                      # retirement line (+ one-time set-aside if redirected) + extra
    cash_inflow: float                           # surplus + emergency/general savings
    goals: list[_Goal] = field(default_factory=list)
    debts: list[_Debt] = field(default_factory=list)
    last_to_portfolio: float = 0.0
    last_to_cash: float = 0.0

    def investable(self) -> float:
        cash_pool = max(0.0, self.cash - self.reserve) if self.count_cash else 0.0
        return self.portfolio + cash_pool

    def step(self, month: date) -> float:
        """Simulate one month; returns the amount invested this month.

        Mirrors the forecast: a goal fund stops at its target and the rest of
        its contribution flows to cash; a paid-off debt's payment flows back to
        cash. With redirect on, both go to the portfolio instead (a goal's from
        the first month starting on or after its deadline).
        """
        to_portfolio = self.portfolio_inflow
        to_cash = self.cash_inflow
        for goal in self.goals:
            if self.redirect and goal.deadline is not None and month >= goal.deadline:
                to_portfolio += goal.contribution
                continue
            paid = goal.contribution
            if goal.target is not None:
                paid = min(paid, max(0.0, goal.target - goal.fund))
            goal.fund += paid
            to_cash += goal.contribution - paid
        for debt in self.debts:
            paid = 0.0
            if debt.balance > 0:
                debt.balance += debt.balance * debt.monthly_interest
                paid = min(debt.payment, debt.balance)
                debt.balance -= paid
            if self.redirect:
                to_portfolio += debt.payment - paid
            else:
                to_cash += debt.payment - paid
        self.portfolio = self.portfolio * (1 + self.portfolio_rate) + to_portfolio
        self.last_to_portfolio = to_portfolio
        if not self.count_cash:
            self.last_to_cash = 0.0
            return to_portfolio
        self.cash = self.cash * (1 + self.cash_rate) + to_cash
        self.last_to_cash = to_cash
        return to_portfolio + to_cash

    def reaches(self, threshold: float, start_month: date, elapsed: int, limit: int) -> tuple[bool, int]:
        """Step until investable assets reach `threshold` or `limit` months have elapsed (mutates the state)."""
        reached = self.investable() >= threshold and elapsed <= limit
        while not reached and elapsed < limit:
            self.step(add_months(start_month, elapsed))
            elapsed += 1
            reached = self.investable() >= threshold
        return reached, elapsed


def _build_simulation(budget: Budget, forecast: Forecast, norm: NormalizedProfile, config: PlanConfig,
                      extra_monthly: float) -> _Simulation:
    end = forecast.end_state
    lines = budget.lines
    infos = {goal.id: goal for goal in norm.goals}
    goals = [
        _Goal(
            fund=end.goal_balances.get(l.target_ref, 0.0),
            target=infos[l.target_ref].target_amount if l.target_ref in infos else None,
            deadline=infos[l.target_ref].deadline if l.target_ref in infos else None,
            contribution=l.monthly_amount,
        )
        for l in lines
        if l.kind == LineKind.goal_contribution
    ]
    # After the window the one-time costs are paid, so the set-aside is freed like a goal past its deadline.
    set_aside = sum(l.monthly_amount for l in lines if l.kind == LineKind.one_time_fund)
    portfolio_inflow = sum(l.monthly_amount for l in lines if l.kind == LineKind.retirement_contribution)
    cash_inflow = max(0.0, budget.totals.unallocated) + sum(l.monthly_amount for l in lines if l.kind in _CASH_KINDS)
    if config.redirect_freed_payments_to_retirement:
        portfolio_inflow += set_aside
    else:
        cash_inflow += set_aside
    if extra_monthly >= 0:
        portfolio_inflow += extra_monthly
    else:  # a smaller total: take it from 0%-return cash first, then from the portfolio
        cut = -extra_monthly
        from_cash = min(cash_inflow, cut) if config.count_cash_as_investable else 0.0
        cash_inflow -= from_cash
        portfolio_inflow -= cut - from_cash
    debts = [
        _Debt(
            balance=end.debt_balances.get(debt.id, debt.balance),
            monthly_interest=(debt.apr or 0.0) / 100 / 12,
            payment=sum(l.monthly_amount for l in lines if l.kind in _DEBT_KINDS and l.target_ref == debt.id),
        )
        for debt in norm.debts
    ]
    return _Simulation(
        portfolio=end.retirement_balance,
        cash=end.cash_balance,
        reserve=emergency_reserve(budget, config),
        portfolio_rate=monthly_rate(config.real_return),
        cash_rate=monthly_rate(config.cash_real_return),
        count_cash=config.count_cash_as_investable,
        redirect=config.redirect_freed_payments_to_retirement,
        portfolio_inflow=portfolio_inflow,
        cash_inflow=cash_inflow,
        goals=goals,
        debts=debts,
    )


def required_monthly(number: float, investable_now: float, months: int, monthly_rate: float) -> Optional[float]:
    """Closed-form annuity: constant monthly contribution that grows investable_now to the number in `months`
    at one rate (None if months <= 0). Exact only when every dollar earns that rate (no cash pool); the
    projection itself uses `_solve_required_extra`, which runs the same two-pool simulation."""
    if months <= 0:
        return None
    if monthly_rate == 0:
        return max(0.0, (number - investable_now) / months)
    growth = (1 + monthly_rate) ** months
    return max(0.0, (number - investable_now * growth) * monthly_rate / (growth - 1))


def _solve_required_extra(build: Callable[[float], _Simulation], threshold: float, start_month: date,
                          horizon: int, limit: int, lower: float, upper: float, tol: float) -> float:
    """Smallest extra monthly portfolio contribution (>= lower) whose projection reaches the number within
    `limit` months, by bisection over the projector itself (so feeding it back always matches)."""

    def ok(extra: float) -> bool:
        return build(extra).reaches(threshold, start_month, horizon, limit)[0]

    if ok(0.0):
        lo, hi = lower, 0.0
        if ok(lo):
            return lo
    else:
        lo, hi = 0.0, upper
        for _ in range(60):  # defensive: `upper` (the whole number in one month) always reaches
            if ok(hi):
                break
            lo, hi = hi, hi * 2
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return hi


def project_retirement(budget: Budget, forecast: Forecast, norm: NormalizedProfile, config: PlanConfig,
                       extra_monthly: float = 0.0) -> RetirementProjection:
    ret = norm.retirement
    if ret is None:
        return RetirementProjection(budget_id=budget.id, status="insufficient_data", missing=["retirement"])
    if ret.current_age is None:
        return RetirementProjection(
            budget_id=budget.id, status="insufficient_data", missing=["retirement.current_age"],
            target_retirement_age=ret.target_retirement_age,
        )

    number, source, spending = retirement_number(budget, norm, config)

    def build(extra: float) -> _Simulation:
        return _build_simulation(budget, forecast, norm, config, extra_monthly + extra)

    sim = build(0.0)
    investable_now = sim.investable()
    horizon = config.horizon_months
    first = copy.deepcopy(sim)
    monthly_investment = first.step(add_months(norm.start_month, horizon))

    threshold = number - config.money_tolerance
    max_elapsed = (config.max_age - ret.current_age) * 12
    reached, elapsed = sim.reaches(threshold, norm.start_month, horizon, max_elapsed)

    age = ret.current_age + elapsed / 12 if reached else None
    year = add_months(norm.start_month, elapsed).year if reached else None
    target_age = ret.target_retirement_age
    delta = age - target_age if age is not None and target_age is not None else None
    matches = None
    if target_age is not None:
        # Compare in whole months: the float ages can miss the 1-month tolerance by an ulp.
        matches = reached and elapsed <= (target_age - ret.current_age) * 12 + 1
    required = None
    if target_age is not None:
        limit = (target_age - ret.current_age) * 12
        if limit - horizon > 0:
            base = build(0.0)
            lower = -(base.portfolio_inflow + (base.cash_inflow if config.count_cash_as_investable else 0.0))
            extra = _solve_required_extra(build, threshold, norm.start_month, horizon, limit,
                                          lower=lower, upper=max(number, 1.0), tol=config.money_tolerance / 2)
            required = max(0.0, monthly_investment + extra)

    return RetirementProjection(
        budget_id=budget.id, status="ok",
        retirement_number=number, number_source=source, implied_annual_spending=spending,
        investable_now=investable_now, monthly_investment=monthly_investment,
        monthly_to_portfolio=first.last_to_portfolio, monthly_to_cash=first.last_to_cash,
        reachable=reached, projected_retirement_age=age, projected_retirement_year=year,
        target_retirement_age=target_age, delta_years=delta, matches_target=matches,
        required_monthly_to_hit_target=required,
    )
