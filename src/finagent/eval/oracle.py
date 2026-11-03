"""Expected plan numbers, worked out from the output-phase spec without using its code."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from finagent.schema import FinancialProfile

PROTECTED_CATEGORIES = frozenset({"housing", "utilities", "healthcare", "insurance", "childcare", "education"})
FACTOR = {"weekly": 52 / 12, "biweekly": 26 / 12, "semimonthly": 2.0, "monthly": 1.0, "quarterly": 1 / 3,
          "annual": 1 / 12}


def add_months(d: date, n: int) -> date:
    total = d.year * 12 + d.month - 1 + n
    return date(total // 12, total % 12 + 1, 1)


def month_diff(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def contribution_months(start: date, deadline: date) -> int:
    """Months that start before the deadline (Nov 2026 to 2027-06-01 is 7)."""
    return max(0, month_diff(start, deadline) + (1 if deadline.day > 1 else 0))


def _active(effective: Optional[date], month: date) -> bool:
    return effective is None or effective < add_months(month, 1)


def _value(x):
    return getattr(x, "value", x)


@dataclass
class Item:
    name: str
    kind: str  # recurring | debt_min | goal | retirement
    bucket: str  # need | want | savings
    monthly: float
    protected: bool
    source_ref: Optional[str] = None


@dataclass
class OneTime:
    name: str
    amount: float
    month: date


@dataclass
class Goal:
    name: str
    target: Optional[float]
    deadline: Optional[date]
    stated_monthly: float
    effective_date: Optional[date]
    required_monthly: Optional[float]
    status_current: str


@dataclass
class Targets:
    need_max: float
    want_max: float
    savings_min: float


@dataclass
class Facts:
    start_month: date
    income: float
    income_maybe_gross: bool
    items: list[Item]
    one_time: list[OneTime]
    needs: float
    wants: float
    savings_stated: float
    unallocated: float
    set_aside: float
    s_req: float
    targets: dict[str, Targets]
    goals: list[Goal]
    high_apr_debts: list[str]
    emergency_fund_months: Optional[float]
    savings_balance: float
    has_retirement_age: bool
    retirement_target: Optional[float]
    retirement_ages: tuple[Optional[int], Optional[int]]
    notes: list[str] = field(default_factory=list)

    def allowance(self, budget_id: str, set_aside: Optional[float] = None) -> float:
        """Most the wants can total. Needs, one-time costs and the savings floor come first (spec 7.2)."""
        t = self.targets[budget_id]
        o = self.set_aside if set_aside is None else set_aside
        return max(0.0, min(t.want_max, self.income - self.needs - o - t.savings_min))

    def expected_want_cut(self, budget_id: str) -> float:
        return max(0.0, self.wants - self.allowance(budget_id))

    def need_feasible(self, budget_id: str, tol: float = 0.01) -> bool:
        return self.needs <= self.targets[budget_id].need_max + tol

    def savings_feasible(self, budget_id: str, set_aside: Optional[float] = None, tol: float = 0.01) -> bool:
        """Can savings reach the floor if every want is cut to zero?"""
        o = self.set_aside if set_aside is None else set_aside
        return self.income - self.needs - o >= self.targets[budget_id].savings_min - tol

    def retirement_number(self, need: float, want: float, withdrawal_rate: float = 0.04) -> float:
        """The stated target, or 25x this budget's yearly needs + wants (spec 9)."""
        if self.retirement_target is not None:
            return self.retirement_target
        return 12 * (need + want) / withdrawal_rate


def monthly(amount, freq) -> Optional[float]:
    if amount is None or freq is None:
        return None
    return amount * FACTOR[_value(freq)]


def set_aside_for(one_time: list[OneTime], savings_balance: float, start: date, horizon: int = 12) -> float:
    """Monthly cash to keep so one-time costs never overdraw savings (spec 7.4 step 0)."""
    end = add_months(start, horizon)
    cumulative, need = 0.0, 0.0
    for o in sorted((o for o in one_time if o.amount > 0 and o.month < end), key=lambda o: o.month):
        cumulative += o.amount
        need = max(need, (cumulative - savings_balance) / (month_diff(start, o.month) + 1))
    return max(0.0, need)


def goal_status(target, deadline, monthly_amt, start, horizon=12, effective=None, tol=0.01) -> str:
    """Goal verdict for a fixed monthly contribution (spec 8)."""
    if target is None or deadline is None:
        return "no_deadline"
    cm = contribution_months(start, deadline)
    if cm == 0:
        return "off_track"
    end = add_months(start, horizon)
    months = [add_months(start, i) for i in range(cm)]
    by_deadline = monthly_amt * sum(_active(effective, m) for m in months)
    in_window = monthly_amt * sum(_active(effective, add_months(start, i)) for i in range(horizon))
    if deadline <= end:
        before = monthly_amt * sum(_active(effective, m) for m in months if m < end)
        return "met" if before >= target - tol else "off_track"
    if in_window >= target - tol:
        return "met"
    return "on_track" if by_deadline >= target - tol else "off_track"


def compute_facts(profile: FinancialProfile, horizon: int = 12, emergency_months: float = 3.0,
                  high_apr_threshold: float = 8.0, protected_categories=PROTECTED_CATEGORIES,
                  tol: float = 0.01) -> Facts:
    protected_categories = {_value(c) for c in protected_categories}
    start = add_months(profile.meta.as_of.date(), 1)
    notes = []

    income, maybe_gross = 0.0, False
    for i in profile.income:
        m = monthly(i.amount, i.frequency)
        if m is None:
            notes.append(f"income '{i.source}' has no amount or frequency")
            continue
        income += m
        maybe_gross |= i.is_net is not True

    items = []
    for n, e in enumerate(profile.recurring_expenses):
        m = monthly(e.amount, e.frequency)
        if m is None:
            notes.append(f"expense '{e.name}' has no amount or frequency")
            continue
        label, cat = _value(e.need_or_want), _value(e.category)
        # Unlabelled items count as needs. A protected category wins over a "want" label (e.g. medication),
        # and the item moves to the needs bucket, matching the output phase's label_conflict handling.
        bucket = "need" if (label is None or cat in protected_categories) else label
        items.append(Item(e.name, "recurring", bucket, m, bucket == "need", f"/recurring_expenses/{n}"))
    for n, d in enumerate(profile.debts):
        if d.minimum_payment:
            items.append(Item(f"{d.name} minimum", "debt_min", "need", d.minimum_payment, True, f"/debts/{n}"))

    def savings_amount(s) -> float:
        if s is None:
            return 0.0
        if _value(s.type) == "percent_of_income":
            return s.value / 100 * income
        return s.value * FACTOR[_value(s.frequency)]

    goals = []
    for n, g in enumerate(profile.savings_goals):
        stated = savings_amount(g.contribution)
        if stated > 0:
            items.append(Item(f"{g.name} contribution", "goal", "savings", stated, False, f"/savings_goals/{n}"))
        cm = contribution_months(start, g.deadline) if g.deadline else None
        required = g.target_amount / cm if (g.target_amount is not None and cm) else None
        goals.append(Goal(g.name, g.target_amount, g.deadline, stated, g.effective_date, required,
                          goal_status(g.target_amount, g.deadline, stated, start, horizon, g.effective_date, tol)))
    ret = profile.retirement
    ret_contribution = (ret.monthly_contribution or 0.0) if ret else 0.0
    if ret_contribution:
        items.append(Item("Retirement contribution", "retirement", "savings", ret_contribution, False))

    one_time = [OneTime(o.name, o.amount, max(add_months(o.expected_date, 0) if o.expected_date else start, start))
                for o in profile.one_time_expenses if o.amount is not None]
    balance = profile.current_savings_balance or 0.0
    needs = sum(i.monthly for i in items if i.bucket == "need")
    wants = sum(i.monthly for i in items if i.bucket == "want")
    savings = sum(i.monthly for i in items if i.bucket == "savings")

    # Goals + 5:3: the floor is the larger of the overall target and the goals plus retirement
    overall = savings_amount(profile.overall_savings_target) if profile.overall_savings_target else 0.0
    goal_need = sum(g.required_monthly if g.required_monthly is not None else g.stated_monthly for g in goals)
    s_req = max(overall, goal_need + ret_contribution)
    rest = max(0.0, income - s_req)
    targets = {"50_30_20": Targets(0.5 * income, 0.3 * income, 0.2 * income),
               "goals_5_3": Targets(5 / 8 * rest, 3 / 8 * rest, s_req)}

    return Facts(
        start_month=start, income=income, income_maybe_gross=maybe_gross, items=items, one_time=one_time,
        needs=needs, wants=wants, savings_stated=savings, unallocated=income - needs - wants - savings,
        set_aside=set_aside_for(one_time, balance, start, horizon), s_req=s_req, targets=targets, goals=goals,
        high_apr_debts=[d.name for d in profile.debts if d.apr is not None and d.apr >= high_apr_threshold],
        emergency_fund_months=balance / needs if needs > 0 else None, savings_balance=balance,
        has_retirement_age=ret is not None and ret.current_age is not None,
        retirement_target=ret.target if ret else None,
        retirement_ages=(ret.current_age, ret.target_retirement_age) if ret else (None, None), notes=notes,
    )


def latest_retirement_age(current_age, horizon, investable_now, to_portfolio, number, real_return, max_age=100):
    """Latest retirement age a correct projection could give, from a path with less money than the real one."""
    rate = (1 + real_return) ** (1 / 12) - 1
    portfolio, months = 0.0, horizon
    while months <= round((max_age - current_age) * 12):
        if investable_now + portfolio >= number:
            return current_age + months / 12
        portfolio = portfolio * (1 + rate) + to_portfolio
        months += 1
    return None
