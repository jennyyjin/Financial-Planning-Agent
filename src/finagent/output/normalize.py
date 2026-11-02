"""FinancialProfile -> NormalizedProfile (TDD §5).

Every money item becomes a line with a stable id, a monthly amount, a bucket
(need / want / savings) and a flexibility tier. Budgets are built from these
lines; the profile itself is never modified.

Budgets use every item in the profile (the profile describes the go-forward
situation). The forecast additionally respects `effective_date`.
"""

from __future__ import annotations

import re
from datetime import date

from finagent.output.config import PlanConfig
from finagent.output.goals import contribution_months
from finagent.output.models import (
    Bucket,
    BudgetLine,
    DebtInfo,
    GoalInfo,
    IncomeLine,
    LineKind,
    NormalizedProfile,
    PlanWarning,
    RetirementInfo,
    Tier,
)
from finagent.output.policy import classify_tier, one_time_tier
from finagent.schema import FinancialProfile, NeedOrWant, SavingsAmount, SavingsAmountType, monthly_amount


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "item"


class _Ids:
    """Hands out unique ids per prefix: 'rec:rent', 'rec:rent-2', ..."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def make(self, prefix: str, name: str) -> str:
        base = f"{prefix}:{slugify(name)}"
        candidate, n = base, 2
        while candidate in self._seen:
            candidate, n = f"{base}-{n}", n + 1
        self._seen.add(candidate)
        return candidate


def first_of_next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def savings_monthly(amount: SavingsAmount, net_monthly_income: float) -> float:
    if amount.type is SavingsAmountType.percent_of_income:
        return net_monthly_income * amount.value / 100
    return monthly_amount(amount) or 0.0


def normalize(profile: FinancialProfile, config: PlanConfig | None = None) -> NormalizedProfile:
    config = config or PlanConfig()
    ids = _Ids()
    warnings: list[PlanWarning] = []
    start_month = first_of_next_month(profile.meta.as_of.date())

    income: list[IncomeLine] = []
    for i, item in enumerate(profile.income):
        ref = f"/income/{i}"
        amount = monthly_amount(item)
        if amount is None:
            warnings.append(PlanWarning(code="income_unknown", message=f"Income '{item.source}' has no amount or frequency and is ignored.", ref=ref))
            continue
        if item.is_net is not True:
            warnings.append(PlanWarning(
                code="income_may_be_gross",
                message=f"Income '{item.source}' may be gross pay; budgets assume it is take-home pay.",
                ref=ref,
            ))
        income.append(IncomeLine(
            id=ids.make("inc", item.source), name=item.source, monthly_amount=amount,
            is_net=item.is_net, effective_date=item.effective_date, source_ref=ref,
        ))
    net_income = sum(line.monthly_amount for line in income)
    if net_income <= 0:
        warnings.append(PlanWarning(code="no_income", message="No usable income; budget shares cannot be computed."))

    lines: list[BudgetLine] = []
    for i, exp in enumerate(profile.recurring_expenses):
        ref = f"/recurring_expenses/{i}"
        line_id = ids.make("rec", exp.name)
        amount = monthly_amount(exp)
        if amount is None:
            warnings.append(PlanWarning(code="expense_amount_unknown", message=f"Expense '{exp.name}' has no amount or frequency; counted as 0.", ref=line_id))
            amount = 0.0
        if exp.need_or_want is None:
            warnings.append(PlanWarning(code="need_or_want_unknown", message=f"Expense '{exp.name}' is unlabelled; treated as a protected need.", ref=line_id))
            bucket = Bucket.need
        else:
            bucket = Bucket.need if exp.need_or_want is NeedOrWant.need else Bucket.want
        if bucket is Bucket.want and exp.category in config.protected_categories:
            warnings.append(PlanWarning(
                code="label_conflict",
                message=f"Expense '{exp.name}' is labelled a want but its category '{exp.category.value}' is protected; it is counted as a need and will not be cut.",
                ref=line_id,
            ))
            bucket = Bucket.need
        # TDD §6: a line whose label is null is protected whatever need_trim_cap says.
        tier = Tier.protected if exp.need_or_want is None else classify_tier(bucket, exp.category, exp.is_fixed, config)
        lines.append(BudgetLine(
            id=line_id, name=exp.name, kind=LineKind.recurring, bucket=bucket,
            monthly_amount=amount, original_amount=amount,
            tier=tier,
            category=exp.category, is_fixed=exp.is_fixed, effective_date=exp.effective_date, source_ref=ref,
        ))

    debts: list[DebtInfo] = []
    for i, debt in enumerate(profile.debts):
        ref = f"/debts/{i}"
        debt_id = ids.make("debt", debt.name)
        if debt.balance is None:
            warnings.append(PlanWarning(code="debt_balance_unknown", message=f"Debt '{debt.name}' has no balance; treated as 0.", ref=debt_id))
        if debt.apr is None:
            warnings.append(PlanWarning(code="debt_apr_unknown", message=f"Debt '{debt.name}' has no APR; interest is not modelled.", ref=debt_id))
        if debt.minimum_payment is None:
            warnings.append(PlanWarning(code="debt_minimum_unknown", message=f"Debt '{debt.name}' has no minimum payment; counted as 0.", ref=debt_id))
        debts.append(DebtInfo(
            id=debt_id, name=debt.name, type=debt.type, balance=debt.balance or 0.0,
            apr=debt.apr, minimum_payment=debt.minimum_payment, source_ref=ref,
        ))
        minimum = debt.minimum_payment or 0.0
        lines.append(BudgetLine(
            id=f"{debt_id}:min", name=f"{debt.name} (minimum payment)", kind=LineKind.debt_min,
            bucket=Bucket.need, monthly_amount=minimum, original_amount=minimum, tier=Tier.protected,
            effective_date=debt.effective_date, target_ref=debt_id, source_ref=ref,
        ))

    goals: list[GoalInfo] = []
    for i, goal in enumerate(profile.savings_goals):
        ref = f"/savings_goals/{i}"
        goal_id = ids.make("goal", goal.name)
        stated = savings_monthly(goal.contribution, net_income) if goal.contribution else None
        if goal.target_amount is None:
            warnings.append(PlanWarning(code="goal_target_unknown", message=f"Goal '{goal.name}' has no target amount.", ref=goal_id))
        if goal.deadline is None and stated is None:
            warnings.append(PlanWarning(code="goal_unscheduled", message=f"Goal '{goal.name}' has neither a deadline nor a contribution.", ref=goal_id))
        if goal.deadline is not None and contribution_months(start_month, goal.deadline) == 0:
            warnings.append(PlanWarning(
                code="goal_deadline_passed",
                message=f"Goal '{goal.name}' has a deadline before the forecast starts; it is planned like a goal without a deadline"
                + (" (its stated contribution is kept)." if stated else " and gets no required contribution."),
                ref=goal_id,
            ))
        goals.append(GoalInfo(
            id=goal_id, name=goal.name, target_amount=goal.target_amount, deadline=goal.deadline,
            stated_monthly=stated, priority=goal.priority, source_ref=ref,
        ))
        if stated:
            lines.append(BudgetLine(
                id=f"{goal_id}:contribution", name=f"{goal.name} savings", kind=LineKind.goal_contribution,
                bucket=Bucket.savings, monthly_amount=stated, original_amount=stated, tier=Tier.savings,
                effective_date=goal.effective_date, target_ref=goal_id, source_ref=ref,
            ))

    retirement = None
    if profile.retirement is not None:
        r = profile.retirement
        retirement = RetirementInfo(
            current_age=r.current_age, target_retirement_age=r.target_retirement_age,
            current_balance=r.current_balance or 0.0, monthly_contribution=r.monthly_contribution or 0.0,
            target=r.target,
        )
        if retirement.monthly_contribution > 0:
            lines.append(BudgetLine(
                id="ret:contribution", name="Retirement contribution", kind=LineKind.retirement_contribution,
                bucket=Bucket.savings, monthly_amount=retirement.monthly_contribution,
                original_amount=retirement.monthly_contribution, tier=Tier.savings, source_ref="/retirement",
            ))

    one_time: list[BudgetLine] = []
    for i, item in enumerate(profile.one_time_expenses):
        ref = f"/one_time_expenses/{i}"
        line_id = ids.make("one", item.name)
        amount = item.amount
        if amount is None:
            warnings.append(PlanWarning(code="expense_amount_unknown", message=f"One-time expense '{item.name}' has no amount; counted as 0.", ref=line_id))
            amount = 0.0
        expected = item.expected_date
        if expected is None:
            warnings.append(PlanWarning(code="one_time_undated", message=f"One-time expense '{item.name}' has no date; assumed in the first forecast month.", ref=line_id))
            expected = start_month
        elif expected < start_month:
            warnings.append(PlanWarning(code="one_time_in_past", message=f"One-time expense '{item.name}' is dated before the forecast; assumed in the first forecast month.", ref=line_id))
            expected = start_month
        one_time.append(BudgetLine(
            id=line_id, name=item.name, kind=LineKind.one_time, bucket=None,
            monthly_amount=amount, original_amount=amount, tier=one_time_tier(False),
            expected_date=expected, effective_date=item.effective_date, source_ref=ref,
        ))

    overall = None
    if profile.overall_savings_target is not None:
        overall = savings_monthly(profile.overall_savings_target, net_income)

    return NormalizedProfile(
        user_id=profile.meta.user_id, profile_version=profile.meta.profile_version, as_of=profile.meta.as_of,
        start_month=start_month, net_monthly_income=net_income, income=income, lines=lines, one_time=one_time,
        goals=goals, debts=debts, retirement=retirement, overall_savings_target_monthly=overall,
        current_savings_balance=profile.current_savings_balance or 0.0, warnings=warnings,
    )
