"""Expense flexibility rules (TDD §6).

These rules are enforced in code: the budget tools call `validate_adjustment`
before applying any change an agent proposes, so a prompt can never cause a
protected expense (rent, medication, debt minimums...) to be cut.
"""

from __future__ import annotations

from typing import Optional

from finagent.output.config import PlanConfig
from finagent.output.models import Bucket, BudgetLine, LineKind, Tier
from finagent.schema import ExpenseCategory


def classify_tier(
    bucket: Bucket,
    category: Optional[ExpenseCategory],
    is_fixed: Optional[bool],
    config: PlanConfig,
) -> Tier:
    """Tier of a labelled recurring expense.

    Unlabelled lines never reach here: normalize marks them protected directly,
    because with need_trim_cap > 0 a variable "need" would otherwise be scalable.
    """
    if category in config.protected_categories:
        return Tier.protected
    if bucket is Bucket.need:
        if config.need_trim_cap > 0 and is_fixed is False:
            return Tier.scalable
        return Tier.protected
    if bucket is Bucket.want:
        return Tier.drop_only if is_fixed else Tier.scalable
    return Tier.savings


def one_time_tier(discretionary: bool) -> Tier:
    return Tier.scalable if discretionary else Tier.protected


def is_adjustable(line: BudgetLine) -> bool:
    return line.tier in (Tier.drop_only, Tier.scalable)


def min_allowed(line: BudgetLine, config: PlanConfig) -> float:
    """Lowest amount restructuring may set this line to."""
    if not is_adjustable(line):
        return line.original_amount
    if line.kind is LineKind.recurring and line.bucket is Bucket.need:
        return line.original_amount * (1 - config.need_trim_cap)
    return 0.0


def validate_adjustment(
    line: BudgetLine, new_amount: float, rationale: str, config: PlanConfig
) -> Optional[str]:
    """Return an error message if the change breaks a rule, else None."""
    tol = config.money_tolerance
    if not is_adjustable(line):
        if line.kind is LineKind.one_time:
            return (
                f"'{line.name}' is a one-time expense treated as essential. Call "
                "mark_one_time_discretionary first if it is clearly non-essential."
            )
        if line.tier is Tier.savings:
            return f"'{line.name}' is a savings line; restructuring only cuts expenses."
        return f"'{line.name}' is protected (essential expense) and cannot be reduced or dropped."
    if new_amount < -tol:
        return "Amounts cannot be negative."
    if new_amount > line.original_amount + tol:
        return f"Restructuring never increases expenses: '{line.name}' is at most {line.original_amount:.2f}."
    if line.tier is Tier.drop_only and tol < new_amount < line.original_amount - tol:
        return f"'{line.name}' is all-or-nothing (a fixed want): keep it at {line.original_amount:.2f} or drop it to 0."
    floor = min_allowed(line, config)
    if new_amount < floor - tol:
        return f"'{line.name}' cannot go below {floor:.2f}."
    if len(rationale.strip()) < config.min_rationale_chars:
        return f"Every change needs a specific rationale of at least {config.min_rationale_chars} characters."
    return None
