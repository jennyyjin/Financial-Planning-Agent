"""Budget variants (TDD §7).

Shared helpers and the current budget. The restructured variants (50/30/20
and goals + 5:3) are built on these.
"""

from __future__ import annotations

from finagent.output.config import PlanConfig
from finagent.output.models import (
    Bucket,
    Budget,
    BudgetId,
    BudgetLine,
    BucketTotals,
    GeneratedBy,
    NormalizedProfile,
)

DESCRIPTIONS = {
    BudgetId.current: "Your budget as you described it.",
    BudgetId.rule_50_30_20: "Restructured to the 50/30/20 rule: at most 50% needs, at most 30% wants, at least 20% savings.",
    BudgetId.goals_5_3: "Restructured to fund your saving goals first, with the rest split 5:3 between needs and wants.",
}


def compute_totals(lines: list[BudgetLine], income: float) -> BucketTotals:
    totals = {bucket: 0.0 for bucket in Bucket}
    for line in lines:
        if line.bucket is not None:
            totals[line.bucket] += line.monthly_amount
    spent = sum(totals.values())
    return BucketTotals(
        need=totals[Bucket.need], want=totals[Bucket.want], savings=totals[Bucket.savings],
        unallocated=income - spent,
    )


def compute_shares(totals: BucketTotals, income: float) -> BucketTotals | None:
    if income <= 0:
        return None
    return BucketTotals(
        need=totals.need / income, want=totals.want / income,
        savings=totals.savings / income, unallocated=totals.unallocated / income,
    )


def refresh(budget: Budget) -> Budget:
    """Recompute totals and shares after lines changed (in place; returns the budget)."""
    budget.totals = compute_totals(budget.lines, budget.income_monthly)
    budget.shares = compute_shares(budget.totals, budget.income_monthly)
    return budget


def copy_lines(lines: list[BudgetLine]) -> list[BudgetLine]:
    return [line.model_copy(deep=True) for line in lines]


def build_current_budget(norm: NormalizedProfile, config: PlanConfig | None = None) -> Budget:
    """The budget exactly as stated. Any surplus stays unallocated (it goes to cash)."""
    budget = Budget(
        id=BudgetId.current, description=DESCRIPTIONS[BudgetId.current],
        income_monthly=norm.net_monthly_income, lines=copy_lines(norm.lines), one_time=copy_lines(norm.one_time),
        totals=BucketTotals(), generated_by=GeneratedBy.as_stated,
    )
    return refresh(budget)
