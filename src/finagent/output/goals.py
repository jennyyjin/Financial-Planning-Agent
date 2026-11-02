"""Savings-goal timing: contribution months and required contributions (TDD §7.3)."""

from __future__ import annotations

from datetime import date
from typing import Optional

from finagent.output.models import GoalInfo
from finagent.schema import Priority

_PRIORITY_RANK = {Priority.high: 0, Priority.medium: 1, Priority.low: 2, None: 3}


def add_months(d: date, n: int) -> date:
    """First day of the month n months after d's month (n may be negative)."""
    total = d.year * 12 + (d.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


def months_between(start: date, end: date) -> int:
    """Whole calendar months from start's month to end's month (may be negative)."""
    return (end.year - start.year) * 12 + (end.month - start.month)


def contribution_months(start_month: date, deadline: date) -> int:
    """Number of forecast months whose first day falls before the deadline.

    A contribution made in month m counts toward a goal if the month starts
    before the deadline: Nov 2026 to a 2027-06-01 deadline is 7 months (Nov-May).
    """
    n = months_between(start_month, deadline)
    if deadline.day > 1:
        n += 1  # the deadline's own month starts before the deadline
    return max(0, n)


def required_monthly(goal: GoalInfo, start_month: date, funded: float = 0.0) -> Optional[float]:
    """Monthly contribution needed to reach the target by the deadline.

    None if the goal has no target or no deadline, or if the deadline has
    already passed (no contribution month is left before it). A passed
    deadline is treated like no deadline: the goal keeps its stated
    contribution, and normalize warns `goal_deadline_passed`. (Spreading the
    whole remainder over one month would otherwise become a permanent
    monthly line the size of the target.)
    """
    if goal.target_amount is None or goal.deadline is None or deadline_passed(goal, start_month):
        return None
    remaining = max(0.0, goal.target_amount - funded)
    return remaining / contribution_months(start_month, goal.deadline)


def deadline_passed(goal: GoalInfo, start_month: date) -> bool:
    """True if the goal has a deadline and no forecast month starts before it."""
    return goal.deadline is not None and contribution_months(start_month, goal.deadline) == 0


def goal_priority_key(goal: GoalInfo) -> tuple:
    """Sort key: high > medium > low > unset, then earliest deadline, then name."""
    return (_PRIORITY_RANK[goal.priority], goal.deadline or date.max, goal.name)
