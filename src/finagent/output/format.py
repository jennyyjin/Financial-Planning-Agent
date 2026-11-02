"""Shared formatting helpers and labels (money, percents, budget names, line-kind groups)."""

from __future__ import annotations

from typing import Optional

from finagent.output.models import BudgetId, LineKind

# Display names of the three budgets: lower-case for prose ("the 50/30/20 budget"), title for headings.
BUDGET_LABELS: dict[BudgetId, str] = {
    BudgetId.current: "current budget",
    BudgetId.rule_50_30_20: "50/30/20 budget",
    BudgetId.goals_5_3: "Goals + 5:3 budget",
}
BUDGET_TITLES: dict[BudgetId, str] = {
    BudgetId.current: "Current",
    BudgetId.rule_50_30_20: "50/30/20",
    BudgetId.goals_5_3: "Goals + 5:3",
}

# Savings lines whose money stays in cash (shared by the forecast and the retirement projection).
CASH_SAVINGS_KINDS: tuple[LineKind, ...] = (LineKind.emergency_fund, LineKind.general_savings)


def cents(x: float) -> str:
    """$1,234.56 (rationales and tool messages)."""
    return f"${x:,.2f}"


def dollars(x: Optional[float]) -> str:
    """$1,235 / -$12 (reports and advice); n/a for None; never "-$0"."""
    if x is None:
        return "n/a"
    rounded = round(x)
    if rounded == 0:
        rounded = 0
    return f"-${-rounded:,}" if rounded < 0 else f"${rounded:,}"


def percent(fraction: Optional[float]) -> str:
    """A fraction as a whole percent (0.394 -> "39%"); n/a for None."""
    return "n/a" if fraction is None else f"{fraction * 100:.0f}%"
