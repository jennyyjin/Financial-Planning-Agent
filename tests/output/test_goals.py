"""Tests for savings-goal timing (TDD §7.3, §14.1)."""

from __future__ import annotations

from datetime import date

import pytest

from finagent.output.goals import contribution_months, goal_priority_key, months_between, required_monthly
from finagent.output.models import GoalInfo
from finagent.schema import Priority

NOV = date(2026, 11, 1)


def goal(name: str = "G", target: float | None = 1200, deadline: date | None = date(2027, 11, 1),
         priority: Priority | None = None) -> GoalInfo:
    return GoalInfo(id=f"goal:{name.lower()}", name=name, target_amount=target, deadline=deadline,
                    priority=priority, source_ref="/savings_goals/0")


@pytest.mark.parametrize("start, end, n", [
    (NOV, date(2027, 6, 1), 7),
    (NOV, date(2026, 11, 20), 0),
    (NOV, date(2026, 10, 1), -1),
    (date(2026, 1, 1), date(2026, 12, 31), 11),
])
def test_months_between(start, end, n):
    assert months_between(start, end) == n


@pytest.mark.parametrize("deadline, n", [
    (date(2027, 6, 1), 7),     # Nov-May
    (date(2027, 6, 15), 8),    # June starts before the deadline, so it counts
    (date(2027, 6, 30), 8),
    (date(2026, 12, 1), 1),
    (date(2026, 11, 15), 1),   # deadline inside the first forecast month
    (date(2026, 11, 1), 0),    # deadline on the first day of the forecast
    (date(2026, 10, 15), 0),   # in the past
    (date(2025, 1, 1), 0),
    (date(2027, 11, 1), 12),
])
def test_contribution_months(deadline, n):
    assert contribution_months(NOV, deadline) == n


def test_japan_trip_required_monthly(normalized_named):
    norm = normalized_named("complete")
    japan = norm.goals[0]
    assert contribution_months(norm.start_month, japan.deadline) == 7
    assert required_monthly(japan, norm.start_month) == pytest.approx(571.43, abs=0.005)
    assert required_monthly(japan, norm.start_month) == pytest.approx(4000 / 7)


def test_funded_reduces_requirement():
    g = goal(target=4000, deadline=date(2027, 6, 1))
    assert required_monthly(g, NOV, funded=500) == pytest.approx(500)
    assert required_monthly(g, NOV, funded=4000) == 0
    assert required_monthly(g, NOV, funded=5000) == 0  # never negative


@pytest.mark.parametrize("target, deadline", [(None, date(2027, 6, 1)), (4000, None), (None, None)])
def test_required_monthly_none_without_target_or_deadline(target, deadline):
    assert required_monthly(goal(target=target, deadline=deadline), NOV) is None


@pytest.mark.parametrize("deadline", [date(2026, 11, 1), date(2026, 10, 15), date(2025, 1, 1)])
def test_deadline_at_or_before_start_needs_whole_remainder(deadline):
    assert required_monthly(goal(target=3000, deadline=deadline), NOV) == pytest.approx(3000)
    assert required_monthly(goal(target=3000, deadline=deadline), NOV, funded=1000) == pytest.approx(2000)


def test_big_goals_requirements(normalized_named):
    norm = normalized_named("big_goals")
    by_name = {g.name: g for g in norm.goals}
    assert required_monthly(by_name["House down payment"], norm.start_month) == pytest.approx(30000 / 36)
    assert required_monthly(by_name["Wedding"], norm.start_month) == pytest.approx(1000)


def test_priority_order():
    goals = [
        goal("Unset", priority=None),
        goal("Low", priority=Priority.low),
        goal("High", priority=Priority.high),
        goal("Medium", priority=Priority.medium),
    ]
    assert [g.name for g in sorted(goals, key=goal_priority_key)] == ["High", "Medium", "Low", "Unset"]


def test_same_priority_earliest_deadline_first_then_no_deadline():
    goals = [
        goal("Later", deadline=date(2028, 1, 1), priority=Priority.high),
        goal("Open", deadline=None, priority=Priority.high),
        goal("Sooner", deadline=date(2027, 3, 1), priority=Priority.high),
    ]
    assert [g.name for g in sorted(goals, key=goal_priority_key)] == ["Sooner", "Later", "Open"]


def test_priority_beats_deadline():
    goals = [goal("Urgent low", deadline=date(2026, 12, 1), priority=Priority.low),
             goal("Distant high", deadline=date(2030, 1, 1), priority=Priority.high)]
    assert [g.name for g in sorted(goals, key=goal_priority_key)] == ["Distant high", "Urgent low"]


def test_ties_broken_by_name_deterministically():
    goals = [goal("B", priority=Priority.medium), goal("A", priority=Priority.medium)]
    assert [g.name for g in sorted(goals, key=goal_priority_key)] == ["A", "B"]
