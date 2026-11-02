"""Tests for the Markdown report (TDD §14.5). Assertions are snapshot-free."""

from __future__ import annotations

from datetime import datetime

import pytest

from conftest import ALL_PROFILES, load_profile
from finagent.output import generate_plan, render_text
from finagent.output.models import BudgetId, PlanWarning, TraceStep
from finagent.output.render import BUDGET_TITLES, GOAL_LABELS, money, pct

FIXED = datetime(2026, 10, 28, 15, 0, 0)
SECTIONS = [
    "# Financial plan", "## Overall recommendation", "### Major changes", "### Strengths", "### Risks",
    "## Budget comparison", "## Budget changes", "## 12-month forecast", "## Retirement (4% rule)",
    "## Key metrics", "## Warnings", "## Assumptions",
]


@pytest.fixture(scope="module")
def plans():
    return {name: generate_plan(load_profile(name), generated_at=FIXED) for name in ALL_PROFILES}


def test_money_and_pct_formatting():
    assert money(1234.4) == "$1,234"
    assert money(1234.5) == "$1,234"  # banker's rounding is fine for display
    assert money(-700) == "-$700"
    assert money(-0.2) == "$0"
    assert money(None) == "n/a"
    assert pct(0.45) == "45%"
    assert pct(0.19999999999999996) == "20%"
    assert pct(None) == "n/a"


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_every_section_in_order(plans, name):
    text = render_text(plans[name])
    positions = [text.index(heading) for heading in SECTIONS]
    assert positions == sorted(positions)


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_every_adjustment_rationale_and_infeasibility_shown(plans, name):
    plan, text = plans[name], render_text(plans[name])
    for budget in plan.budgets.values():
        for adj in budget.adjustments:
            assert adj.rationale in text
        if budget.infeasibility:
            assert budget.infeasibility.reason in text


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_every_goal_verdict_shown_per_budget(plans, name):
    plan, text = plans[name], render_text(plans[name])
    forecast_part = text[text.index("## 12-month forecast"):text.index("## Retirement")]
    for bid, forecast in plan.forecasts.items():
        block = forecast_part.split(f"### {BUDGET_TITLES[bid]} budget")[1].split("###")[0]
        for verdict in forecast.goals:
            row = next(l for l in block.splitlines() if l.startswith(f"| {verdict.name} |"))
            assert GOAL_LABELS[verdict.status] in row
            if verdict.status.value == "off_track":
                assert money(verdict.shortfall) in row and money(verdict.required_monthly) in row


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_recommendation_fully_rendered(plans, name):
    rec, text = plans[name].recommendation, render_text(plans[name])
    assert rec.summary in text
    for change in rec.major_changes:
        assert change.title in text and change.detail in text and change.impact in text
    for item in rec.strengths + rec.risks:
        assert item in text


def test_comparison_table_has_three_budgets_with_shares(plans):
    text = render_text(plans["overspender"])
    table = text[text.index("## Budget comparison"):text.index("## Budget changes")]
    assert "| Current | 50/30/20 | Goals + 5:3 |" in table
    assert "| Needs | $3,080 (77%) | $3,080 (77%) | $3,080 (77%) |" in table
    assert "| Savings target | — | ≥ $800 |" in table
    assert "| Meets targets | — | no | no |" in table


def test_overspender_details(plans):
    text = render_text(plans["overspender"])
    assert "$600 → $60/mo" in text  # dining out cut, before -> after
    assert "Targets not met" in text and "Headroom" in text
    assert "Months with negative cash: 3 (Mar 2027, Apr 2027, May 2027)" in text
    assert "Disneyland trip $2,500 in Mar 2027" in text
    assert "Credit card $4,329" in text  # debt names, not ids
    # The trip set-aside is shown as its own row, not as savings, and no longer crowds out the goal.
    assert "| One-time set-aside | — " not in text and "| One-time set-aside |" in text
    assert "Unfunded goals" not in text
    assert "kept as cash)" in text  # retirement inflow split between the portfolio and cash


def test_passed_deadline_goal_is_explained(profile_named):
    from datetime import date

    from finagent.output import generate_plan

    profile = profile_named("complete")
    profile.savings_goals[0].deadline = date(2026, 9, 1)
    text = render_text(generate_plan(profile))
    assert "the deadline has passed" in text and "goal_deadline_passed" in text
    assert "needs n/a/mo" not in text


def test_complete_forecast_and_retirement(plans):
    plan, text = plans["complete"], render_text(plans["complete"])
    assert "Cash: $6,500 at the start" in text
    assert "Laptop $1,500 in Dec 2026" in text
    assert "| Japan trip | Off track | $4,000 | 2027-06-01 | $3,500 | short $500; needs $571/mo |" in text
    proj = plan.retirement[BudgetId.current]
    assert f"Retirement number: {money(proj.retirement_number)}" in text
    assert "from your retirement savings target" in text
    assert "age 52.1 (2054)" in text and "Target age: 60; on target: yes" in text


def test_insufficient_retirement_data(plans):
    text = render_text(plans["no_retirement"])
    assert text.count("insufficient data (missing: retirement)") == 3


def test_warnings_and_assumptions(plans):
    plan = plans["complete"].model_copy(deep=True)
    plan.meta.warnings.append(PlanWarning(code="agent_fallback", message="The advisor agent did not finish.", ref="advisor"))
    text = render_text(plan)
    assert "`agent_fallback`: The advisor agent did not finish. (advisor)" in text
    assert "real return: 5.0%" in text and "25 × annual spending" in text and "High-APR threshold: 8% APR" in text
    assert "## Warnings\n\nNone." in render_text(plans["complete"])


def test_render_is_pure_and_excludes_trace(plans):
    plan = plans["complete"].model_copy(deep=True)
    before = plan.model_dump_json()
    plan.trace.append(TraceStep(agent="advisor", step=1, tool="compare_budgets", text="SECRET-TRACE-TEXT"))
    text = render_text(plan)
    assert "SECRET-TRACE-TEXT" not in text
    assert render_text(plan) == text
    plan.trace.clear()
    assert plan.model_dump_json() == before


def test_header_names_engine(plans):
    text = render_text(plans["complete"])
    assert "deterministic (no LLM)" in text and "2026-10-28T15:00" in text
    plan = plans["complete"].model_copy(update={"meta": plans["complete"].meta.model_copy(update={"model": "gpt-x"})})
    assert "model `gpt-x`" in render_text(plan)
