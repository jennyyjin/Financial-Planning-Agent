"""Tests for the advisor tools and the Advisor Agent (TDD §10, §11.3, §14.4)."""

from __future__ import annotations

import json

import pytest

from conftest import load_profile
from finagent.output import budget as budget_ops
from finagent.output import forecast as forecast_ops
from finagent.output import retirement as retirement_ops
from finagent.output.advice import PlanContext, fallback_recommendation, find_ungrounded
from finagent.output.agents.advisor import PROMPT_PATH, advise_with_agent, build_user_message, load_prompt
from finagent.output.agents.runtime import ScriptedModel, ToolCall, call, turn
from finagent.output.config import PlanConfig
from finagent.output.metrics import compute_metrics
from finagent.output.models import BudgetId, GeneratedBy, OverallStatus
from finagent.output.normalize import normalize
from finagent.output.tools.advisor_tools import AdvisorSession, build_advisor_tools


def build_context(name: str, config: PlanConfig | None = None) -> PlanContext:
    config = config or PlanConfig()
    norm = normalize(load_profile(name), config)
    budgets = budget_ops.build_budgets(norm, config)
    forecasts = {bid: forecast_ops.forecast_budget(b, norm, config) for bid, b in budgets.items()}
    retirement = {bid: retirement_ops.project_retirement(b, forecasts[bid], norm, config) for bid, b in budgets.items()}
    metrics = compute_metrics(norm, budgets, forecasts, retirement, config)
    return PlanContext(norm, config, budgets, forecasts, retirement, metrics)


@pytest.fixture
def complete() -> PlanContext:
    return build_context("complete")


@pytest.fixture
def session(complete) -> AdvisorSession:
    return AdvisorSession(complete)


def tool_call(tool: str, **arguments) -> ToolCall:
    """Like runtime.call, but allows an argument called 'name' (get_section's section name)."""
    return ToolCall(id=call(tool).id, name=tool, arguments=arguments)


def run(registry, tool: str, **arguments):
    out, done = registry.dispatch(tool_call(tool, **arguments))
    json.dumps(out)  # every tool result is strict JSON
    return out, done


def change(title: str, detail: str, impact: str, *refs: str) -> dict:
    return {"title": title, "detail": detail, "impact": impact, "evidence_refs": list(refs)}


JAPAN = change(
    "Raise Japan trip savings to $571/mo",
    "At $500/mo the Japan trip reaches $3,500 of its $4,000 target by June 2027, $500 short.",
    "The 50/30/20 budget funds it in time.",
    "forecasts.current.goals.goal:japan-trip", "forecasts.50_30_20.goals.goal:japan-trip",
)
SWITCH = change(
    "Switch to the 50/30/20 budget",
    "It assigns the $2,287/mo your current budget leaves unallocated.",
    "Retirement moves from about age 52 to about 46, against a target of 60.",
    "budgets.50_30_20.totals", "retirement.50_30_20.projected_retirement_age",
)


EMERGENCY = change(
    "Build a 3-month emergency fund",
    "Your $6,500 of savings covers 2.7 months of needs; the guideline is 3 months.",
    "A cash cushion keeps an unexpected bill from turning into debt.",
    "metrics.emergency_fund_months",
)


def submission(*changes: dict, summary: str = "You are in reasonable shape; the Japan trip is $500 short.") -> dict:
    """A valid submission for the complete profile; the given changes are padded to the minimum of 3."""
    major = list(changes) or [JAPAN, SWITCH]
    for pad in (EMERGENCY, SWITCH, JAPAN):
        if len(major) < 3 and pad not in major:
            major.append(pad)
    return {
        "overall_status": "needs_attention",
        "summary": summary,
        "recommended_budget": "50_30_20",
        "major_changes": major,
        "strengths": ["No months with negative cash."],
        "risks": ["Savings cover 2.7 months of needs, under the 3-month guideline."],
    }


# --- read-only tools ----------------------------------------------------------------------------


def test_compare_budgets_one_row_per_budget(session):
    reg = build_advisor_tools(session)
    out, done = run(reg, "compare_budgets")
    assert not done
    rows = {row["budget_id"]: row for row in out["budgets"]}
    assert set(rows) == {"current", "50_30_20", "goals_5_3"}
    cur = rows["current"]
    assert cur["needs"] == pytest.approx(2443.33)
    assert cur["need_pct"] == pytest.approx(39.4)
    assert cur["goals_off_track"] == ["goal:japan-trip"]
    assert rows["50_30_20"]["goals_met"] == ["goal:japan-trip"]
    assert cur["cash_shortfall_months"] == 0
    assert cur["matches_target"] is True
    assert out["target_retirement_age"] == 60


def test_get_section_forecasts_summarized_unless_full(session):
    reg = build_advisor_tools(session)
    out, _ = run(reg, "get_section", name="forecasts", budget_id="current")
    data = out["data"]
    assert set(data) == {"current"}
    assert "months" not in data["current"]
    summary = data["current"]["months_summary"]
    assert summary["count"] == 12 and summary["first"]["month"] == "2026-11-01"
    assert data["current"]["goals"][0]["goal_id"] == "goal:japan-trip"

    full, _ = run(reg, "get_section", name="forecasts", budget_id="current", full=True)
    assert len(full["data"]["current"]["months"]) == 12


def test_get_section_profile_and_metrics(session):
    reg = build_advisor_tools(session)
    profile, _ = run(reg, "get_section", name="profile")
    assert profile["data"]["goals"][0]["id"] == "goal:japan-trip"
    assert profile["data"]["retirement"]["target_retirement_age"] == 60
    assert profile["data"]["assumptions"]["real_return"] == pytest.approx(0.05)
    metrics, _ = run(reg, "get_section", name="metrics", budget_id="goals_5_3")
    assert set(metrics["data"]["per_budget"]) == {"goals_5_3"}
    budgets, _ = run(reg, "get_section", name="budgets", budget_id="50_30_20")
    assert {a["line_id"] for a in budgets["data"]["50_30_20"]["adjustments"]} >= {"ret:contribution"}


def test_get_section_rejects_unknown_section(session):
    out, _ = run(build_advisor_tools(session), "get_section", name="secrets")
    assert "error" in out


def test_simulate_retirement_extends_allowed_values(session, complete):
    reg = build_advisor_tools(session)
    base = complete.retirement[BudgetId.current].projected_retirement_age
    out, done = run(reg, "simulate_retirement", budget_id="current", extra_monthly=300)
    assert not done
    new_age = out["projection"]["projected_retirement_age"]
    assert new_age < base
    assert out["years_earlier"] == pytest.approx(base - new_age, abs=0.01)
    assert 300 in session.extra_allowed
    assert any(v == pytest.approx(new_age) for v in session.extra_allowed)


def test_simulate_retirement_validates_and_handles_missing_data():
    s = AdvisorSession(build_context("no_retirement"))
    reg = build_advisor_tools(s)
    out, _ = run(reg, "simulate_retirement", budget_id="current", extra_monthly=-5)
    assert "error" in out
    out, _ = run(reg, "simulate_retirement", budget_id="current", extra_monthly=100)
    assert out["projection"]["status"] == "insufficient_data"


# --- submit_recommendation ------------------------------------------------------------------------


def test_submit_rejects_bad_refs(session):
    bad = change("Raise Japan trip savings", "Save more.", "Meets the deadline.",
                 "forecast.current.goals.japan-trip", "forecasts.current.goals.goal:japan-trip")
    out, done = run(build_advisor_tools(session), "submit_recommendation", **submission(bad))
    assert not done and session.submitted is None
    assert "forecast.current.goals.japan-trip" in out["error"]
    assert "forecasts.current.goals.goal:japan-trip," not in out["error"]  # only the bad ref is listed


def test_submit_requires_a_major_change(session):
    args = submission()
    args["major_changes"] = []
    out, done = run(build_advisor_tools(session), "submit_recommendation", **args)
    assert not done and "Give 3 to 5 major changes" in out["error"]


def test_submit_rejects_too_many_changes(session):
    args = submission()
    args["major_changes"] = [EMERGENCY] * 6
    out, done = run(build_advisor_tools(session), "submit_recommendation", **args)
    assert not done and "you gave 6" in out["error"]


@pytest.mark.parametrize("ref", ["budgets", "metrics", "forecasts.current", "metrics.per_budget"])
def test_submit_rejects_refs_to_a_whole_section(session, ref):
    vague = change("Build a 3-month emergency fund", "Savings cover 2.7 months.", "A cushion.", ref)
    out, done = run(build_advisor_tools(session), "submit_recommendation", **submission(vague, JAPAN, SWITCH))
    assert not done and "whole section" in out["error"] and ref in out["error"]


def test_submit_rejects_overall_status_against_the_rules(session):
    args = submission()
    args["overall_status"] = "on_track"
    out, done = run(build_advisor_tools(session), "submit_recommendation", **args)
    assert not done and "needs_attention" in out["error"]


def test_submit_rejects_dominated_recommended_budget(session):
    args = submission()
    args["recommended_budget"] = "current"  # the Japan trip is off track in the current budget
    out, done = run(build_advisor_tools(session), "submit_recommendation", **args)
    assert not done and "recommended_budget" in out["error"] and "50_30_20" in out["error"]


def test_submit_rejects_false_claims_on_overspender():
    """Reviewer repro: grounded numbers, false verdicts ('every goal met', 'retire at 65 as planned')."""
    ctx = build_context("overspender")
    s = AdvisorSession(ctx)
    reg = build_advisor_tools(s)
    status = fallback_recommendation(ctx).overall_status.value
    rid = fallback_recommendation(ctx).recommended_budget.value
    changes = [change("Keep your budget", "Nothing to change.", "Emergency fund reaches its target.",
                      f"forecasts.current.goals.goal:emergency-fund")] * 3
    args = {"overall_status": status, "recommended_budget": rid, "major_changes": changes,
            "summary": "Every saving goal is met in the current budget and you retire at 65 as planned.",
            "strengths": [], "risks": []}
    out, done = run(reg, "submit_recommendation", **args)
    assert not done and "contradict" in out["error"]
    assert "off track in the current budget" in out["error"] and "65" in out["error"]
    # Hedged use of the target age is fine.
    args["summary"] = "Your target is to retire at 65; the current budget leaves the Emergency fund off track."
    out, done = run(reg, "submit_recommendation", **args)
    assert done and out["accepted"], out


def test_submit_requires_evidence_refs_per_change(session):
    out, done = run(build_advisor_tools(session), "submit_recommendation",
                    **submission(change("Save more", "Save more.", "Better.")))
    assert not done and "error" in out


def test_ungrounded_rejected_then_simulation_makes_it_grounded(session):
    reg = build_advisor_tools(session)
    what_if = change(
        "Invest $300/mo more", "Investing $300/mo more would move retirement earlier.",
        "You could retire around age {age}.", "retirement.current.projected_retirement_age",
    )
    sim_age = retirement_ops.project_retirement(
        session.context.budgets[BudgetId.current], session.context.forecasts[BudgetId.current],
        session.context.norm, session.context.config, extra_monthly=300,
    ).projected_retirement_age
    what_if["impact"] = what_if["impact"].format(age=f"{sim_age:.0f}")

    out, done = run(reg, "submit_recommendation", **submission(JAPAN, what_if))
    assert not done and session.submitted is None
    assert "300" in out["error"] and "tool" in out["error"]

    run(reg, "simulate_retirement", budget_id="current", extra_monthly=300)
    out, done = run(reg, "submit_recommendation", **submission(JAPAN, what_if))
    assert done and out["accepted"] is True
    assert session.submitted.generated_by is GeneratedBy.agent
    assert session.submitted.major_changes[1].title == "Invest $300/mo more"


def test_simulate_retirement_cannot_launder_invented_numbers(session):
    """Reviewer repro: simulate_retirement(extra_monthly=73219) must not make $73,219 citable."""
    reg = build_advisor_tools(session)
    out, _ = run(reg, "simulate_retirement", budget_id="current", extra_monthly=73219)
    assert "not citable" in out["note"]
    assert find_ungrounded(["You will have $73,219 by 2030."], session.allowed()) == [73219]
    mi = out["projection"]["monthly_investment"]
    assert find_ungrounded([f"${mi:,.0f}"], session.allowed()) == [round(mi)]
    # A round what-if amount stays citable, with its results.
    out, _ = run(reg, "simulate_retirement", budget_id="current", extra_monthly=250)
    assert "note" not in out and 250 in session.extra_allowed


def test_fallback_recommendation_passes_the_submit_checks():
    """The deterministic recommendation must satisfy every check the agent's submission faces."""
    from conftest import ALL_PROFILES

    for name in ALL_PROFILES:
        ctx = build_context(name)
        rec = fallback_recommendation(ctx)
        args = rec.model_dump(mode="json", exclude={"generated_by"})
        s = AdvisorSession(ctx)
        out, done = run(build_advisor_tools(s), "submit_recommendation", **args)
        assert done and out["accepted"], (name, out)


def test_grounding_failures_beyond_retry_end_the_session(session):
    reg = build_advisor_tools(session)
    invented = submission(summary="Save $777 a month.")
    out, done = run(reg, "submit_recommendation", **invented)
    assert not done and "777" in out["error"]
    out, done = run(reg, "submit_recommendation", **invented)
    assert done and out["accepted"] is False
    assert session.submitted is None and "777" in session.failed


def test_submit_accepts_grounded_recommendation(session):
    out, done = run(build_advisor_tools(session), "submit_recommendation", **submission())
    assert done and out["accepted"]
    rec = session.submitted
    assert rec.overall_status is OverallStatus.needs_attention
    assert rec.recommended_budget is BudgetId.rule_50_30_20


# --- Advisor Agent ------------------------------------------------------------------------------


def test_prompt_loads_from_repo():
    assert PROMPT_PATH.exists()
    prompt = load_prompt()
    for needle in ("compare_budgets", "submit_recommendation", "simulate_retirement", "evidence_refs",
                   "not a licensed financial adviser"):
        assert needle in prompt


def test_user_message_has_no_numbers(complete):
    msg = build_user_message(complete)
    assert "compare_budgets" in msg and "goals_5_3" in msg
    assert not any(ch.isdigit() for ch in msg.replace("50_30_20", "").replace("goals_5_3", "")
                   .replace("50/30/20", "").replace("5:3", ""))


def test_advise_with_agent_happy_path(complete):
    model = ScriptedModel([
        turn(call("compare_budgets")),
        turn(tool_call("get_section", name="forecasts", budget_id="current"), tool_call("get_section", name="metrics")),
        turn(call("submit_recommendation", **submission())),
    ])
    rec, result = advise_with_agent(complete, model)
    assert result.completed and result.error is None
    assert rec.generated_by is GeneratedBy.agent
    assert rec.major_changes[0].title == JAPAN["title"]
    assert [s.tool for s in result.trace] == ["compare_budgets", "get_section", "get_section", "submit_recommendation"]
    assert all(s.agent == "advisor" for s in result.trace)
    assert model.calls[0][0].role == "system" and "Advisor" in model.calls[0][0].content


def test_advise_with_agent_retries_after_ungrounded(complete):
    model = ScriptedModel([
        turn(call("submit_recommendation", **submission(summary="Save $777 a month."))),
        turn(call("submit_recommendation", **submission())),
    ])
    rec, result = advise_with_agent(complete, model)
    assert rec.generated_by is GeneratedBy.agent
    assert "777" in result.trace[0].result["error"]


def test_advise_with_agent_falls_back_at_max_steps(complete):
    model = ScriptedModel([turn(call("compare_budgets"))] * 3)
    rec, result = advise_with_agent(complete, model, max_steps=3)
    assert not result.completed and result.error == "max_steps"
    assert rec == fallback_recommendation(complete)
    assert rec.generated_by is GeneratedBy.fallback


def test_advise_with_agent_falls_back_after_repeated_ungrounded(complete):
    invented = submission(summary="Save $777 a month.")
    model = ScriptedModel([turn(call("submit_recommendation", **invented))] * 2)
    rec, result = advise_with_agent(complete, model)
    assert rec.generated_by is GeneratedBy.fallback
    assert result.completed and "777" in result.error


def test_advise_with_agent_falls_back_on_model_error(complete):
    class Broken:
        name = "broken"

        def chat(self, messages, tools):
            raise RuntimeError("network down")

    rec, result = advise_with_agent(complete, Broken())
    assert rec.generated_by is GeneratedBy.fallback
    assert "network down" in result.error
