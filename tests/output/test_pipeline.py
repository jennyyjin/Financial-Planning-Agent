"""Tests for the end-to-end pipeline and the CLI (TDD §4, §11.4, §14.4)."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime

import pytest

from conftest import ALL_PROFILES, PROFILE_PATHS, ROOT, load_profile
from finagent.output import PlanConfig, PlanOutput, generate_plan, render_text
from finagent.output import pipeline
from finagent.output.__main__ import main
from finagent.output.agents.advisor import load_prompt as advisor_prompt
from finagent.output.agents.restructurer import load_prompt as restructurer_prompt
from finagent.output.agents.runtime import AssistantTurn, Message, ToolSpec, call, turn
from finagent.output.models import BudgetId, GeneratedBy, Tier

FIXED = datetime(2026, 10, 28, 15, 0, 0)
ALL_IDS = {BudgetId.current, BudgetId.rule_50_30_20, BudgetId.goals_5_3}


@pytest.fixture(scope="module")
def plans() -> dict[str, PlanOutput]:
    return {name: generate_plan(load_profile(name), generated_at=FIXED) for name in ALL_PROFILES}


# --- deterministic run (model=None) --------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_deterministic_plan_is_complete(plans, name):
    plan = plans[name]
    assert set(plan.budgets) == set(plan.forecasts) == set(plan.retirement) == ALL_IDS
    assert set(plan.metrics.per_budget) == {bid.value for bid in ALL_IDS}
    for forecast in plan.forecasts.values():
        assert len(forecast.months) == 12
    assert plan.budgets[BudgetId.current].generated_by is GeneratedBy.as_stated
    for bid in (BudgetId.rule_50_30_20, BudgetId.goals_5_3):
        assert plan.budgets[bid].generated_by is GeneratedBy.fallback
    assert plan.recommendation.generated_by is GeneratedBy.fallback
    assert plan.meta.model is None and plan.meta.generated_at == FIXED
    assert plan.trace == []


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_json_round_trip_is_lossless(plans, name):
    plan = plans[name]
    assert PlanOutput.model_validate_json(plan.model_dump_json()) == plan


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_deterministic_runs_are_identical(plans, name):
    again = generate_plan(load_profile(name), generated_at=FIXED)
    assert again.model_dump_json() == plans[name].model_dump_json()


def test_meta_carries_profile_config_and_normalization_warnings(plans):
    from finagent.output.normalize import normalize

    for name in ALL_PROFILES:
        plan = plans[name]
        norm = normalize(load_profile(name))
        assert plan.meta.user_id == norm.user_id and plan.meta.start_month == norm.start_month
        assert plan.meta.warnings == norm.warnings
        assert plan.meta.config == PlanConfig()


def test_custom_config_is_recorded_and_used():
    config = PlanConfig(real_return=0.03)
    plan = generate_plan(load_profile("complete"), config, generated_at=FIXED)
    base = generate_plan(load_profile("complete"), generated_at=FIXED)
    assert plan.meta.config.real_return == pytest.approx(0.03)
    assert (plan.retirement[BudgetId.current].projected_retirement_age
            > base.retirement[BudgetId.current].projected_retirement_age)


def test_generated_at_defaults_to_now():
    before = datetime.now().astimezone()
    plan = generate_plan(load_profile("complete"))
    assert plan.meta.generated_at.tzinfo is not None
    assert before <= plan.meta.generated_at <= datetime.now().astimezone()


def test_plan_from_json_accepts_path_dict_and_string():
    path = PROFILE_PATHS["complete"]
    expected = generate_plan(load_profile("complete"), generated_at=FIXED)
    for source in (path, str(path), json.loads(path.read_text()), path.read_text()):
        assert pipeline.plan_from_json(source, generated_at=FIXED) == expected


# --- agent path (scripted models, no network) ------------------------------------------------------


class PlanAgents:
    """A ChatModel that plays both agents, choosing its role from the system prompt.

    Restructurer: reads the state, cuts the largest flexible wants to fit, finalizes.
    Advisor: submits `recommendation` (a dict of submit_recommendation arguments).
    """

    name = "scripted-plan-agents"

    def __init__(self, recommendation: dict | None = None) -> None:
        self.recommendation = recommendation
        self.roles: list[str] = []

    def chat(self, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn:
        system = messages[0].content
        if system == restructurer_prompt():
            self.roles.append("restructurer")
            return self._restructure(messages)
        assert system == advisor_prompt()
        self.roles.append("advisor")
        if not any(m.role == "tool" for m in messages):
            return turn(call("compare_budgets"))
        if self.recommendation is None:
            return turn(text="I have nothing to add.")
        return turn(call("submit_recommendation", **self.recommendation))

    @staticmethod
    def _restructure(messages: list[Message]) -> AssistantTurn:
        tool_msgs = [m for m in messages if m.role == "tool"]
        if not tool_msgs:
            return turn(call("get_budget_state"))
        state = json.loads(tool_msgs[0].content)
        excess, calls = state["wants_over_allowance"], []
        for line in sorted((l for l in state["lines"] if l["bucket"] == "want" and l["tier"] == "scalable"),
                           key=lambda l: -l["amount"]):
            if excess <= 0.01:
                break
            cut = min(excess, line["amount"] - line["min_allowed"])
            excess -= cut
            calls.append(call("adjust_item", line_id=line["id"], new_monthly_amount=line["amount"] - cut,
                              rationale=f"{line['name']} is the largest flexible want, trimmed to fit the allowance."))
        for line in (l for l in state["lines"] if l["bucket"] == "want" and l["tier"] == "drop_only"):
            if excess <= 0.01:
                break
            excess -= line["amount"]
            calls.append(call("adjust_item", line_id=line["id"], new_monthly_amount=0,
                              rationale=f"{line['name']} is an all-or-nothing want, dropped to fit the allowance."))
        issued = sum(1 for m in messages if m.role == "assistant" and m.tool_calls) - 1
        if issued < len(calls):
            return turn(calls[issued])
        return turn(call("finalize", summary_rationale="Cut the largest flexible wants first, only as far as needed."))


def _submission(plan: PlanOutput) -> dict:
    """A deterministic plan's recommendation as submit_recommendation arguments (grounded by construction)."""
    rec = plan.recommendation.model_dump(mode="json")
    rec.pop("generated_by")
    return rec


def test_agent_path_uses_agents_and_records_trace(plans):
    model = PlanAgents(_submission(plans["complete"]))
    plan = generate_plan(load_profile("complete"), model=model, generated_at=FIXED)

    assert plan.meta.model == "scripted-plan-agents"
    for bid in (BudgetId.rule_50_30_20, BudgetId.goals_5_3):
        assert plan.budgets[bid].generated_by is GeneratedBy.agent
        assert plan.budgets[bid].summary_rationale.startswith("Cut the largest")
    assert plan.recommendation.generated_by is GeneratedBy.agent
    assert not [w for w in plan.meta.warnings if w.code.startswith("agent_")]

    agents = [s.agent for s in plan.trace]
    assert agents[0] == "restructurer:50_30_20" and agents[-1] == "advisor"
    order = list(dict.fromkeys(agents))
    assert order == ["restructurer:50_30_20", "restructurer:goals_5_3", "advisor"]
    assert plan.trace[-1].tool == "submit_recommendation" and plan.trace[-1].result["accepted"] is True
    assert PlanOutput.model_validate_json(plan.model_dump_json()) == plan
    assert "trace" not in render_text(plan).lower()


def test_agent_cuts_respect_protections_on_overspender():
    plan = generate_plan(load_profile("overspender"), model=PlanAgents(), generated_at=FIXED)
    for bid in (BudgetId.rule_50_30_20, BudgetId.goals_5_3):
        budget = plan.budgets[bid]
        assert budget.generated_by is GeneratedBy.agent
        for line in budget.lines:
            if line.tier is Tier.protected:
                assert line.monthly_amount == pytest.approx(line.original_amount)
        cuts = [a for a in budget.adjustments if a.decided_by.value == "agent"]
        assert cuts and all(a.rationale for a in cuts)
    # The advisor never submitted: the templated recommendation is used and flagged.
    assert plan.recommendation.generated_by is GeneratedBy.fallback
    assert [w.ref for w in plan.meta.warnings if w.code == "agent_fallback"] == ["advisor"]


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_agent_path_completes_on_every_profile(name):
    plan = generate_plan(load_profile(name), model=PlanAgents(), generated_at=FIXED)
    assert set(plan.budgets) == ALL_IDS
    assert all(len(f.months) == 12 for f in plan.forecasts.values())
    assert PlanOutput.model_validate_json(plan.model_dump_json()) == plan


class Crashing:
    name = "crashing"

    def chat(self, messages, tools):
        raise RuntimeError("provider down")


def test_crashing_model_falls_back_everywhere_with_warnings(plans):
    plan = generate_plan(load_profile("complete"), model=Crashing(), generated_at=FIXED)
    fallback_refs = [w.ref for w in plan.meta.warnings if w.code == "agent_fallback"]
    assert fallback_refs == ["restructurer:50_30_20", "restructurer:goals_5_3", "advisor"]
    assert all("provider down" in w.message for w in plan.meta.warnings if w.code == "agent_fallback")
    assert plan.meta.model == "crashing"
    # Numbers and advice equal the deterministic run; only meta differs.
    base = plans["complete"]
    assert plan.budgets == base.budgets and plan.forecasts == base.forecasts
    assert plan.recommendation == base.recommendation


def test_agent_exceptions_are_caught_and_reported(monkeypatch, plans):
    def broken_restructurer(model, results=None):
        def restructure(norm, budget_id, config):
            raise ValueError("tool bug")
        return restructure

    def broken_advisor(context, model, **kwargs):
        raise KeyError("advisor bug")

    monkeypatch.setattr(pipeline, "make_agent_restructurer", broken_restructurer)
    monkeypatch.setattr(pipeline, "advise_with_agent", broken_advisor)
    plan = generate_plan(load_profile("complete"), model=PlanAgents(), generated_at=FIXED)

    errors = [w for w in plan.meta.warnings if w.code == "agent_error"]
    assert [w.ref for w in errors] == ["restructurer:50_30_20", "restructurer:goals_5_3", "advisor"]
    assert "tool bug" in errors[0].message and "advisor bug" in errors[2].message
    assert plan.budgets == plans["complete"].budgets
    assert plan.recommendation == plans["complete"].recommendation


# --- CLI -----------------------------------------------------------------------------------------------


def test_cli_json_no_llm(capsys):
    assert main([str(PROFILE_PATHS["complete"]), "--format", "json", "--no-llm",
                 "--generated-at", FIXED.isoformat()]) == 0
    out = capsys.readouterr()
    plan = PlanOutput.model_validate_json(out.out)
    assert plan == generate_plan(load_profile("complete"), generated_at=FIXED)
    assert "FINAGENT_LLM_MODEL" not in out.err


def test_cli_text_without_model_env_notes_deterministic(capsys, monkeypatch):
    monkeypatch.delenv("FINAGENT_LLM_MODEL", raising=False)
    assert main([str(PROFILE_PATHS["overspender"])]) == 0
    out = capsys.readouterr()
    assert out.out.startswith("# Financial plan")
    assert "## Overall recommendation" in out.out
    assert "FINAGENT_LLM_MODEL is not set" in out.err


def test_cli_uses_llm_from_env(capsys, monkeypatch):
    seen = {}

    class FakeModel(Crashing):
        name = "fake-llm"
        base_url = "http://localhost"

    def from_env(cls=None, **kw):
        seen["called"] = True
        return FakeModel()

    import finagent.output.__main__ as cli

    monkeypatch.setenv("FINAGENT_LLM_MODEL", "fake-llm")
    monkeypatch.setattr(cli.OpenAICompatibleModel, "from_env", classmethod(lambda cls, **kw: from_env()))
    assert main([str(PROFILE_PATHS["complete"]), "--json"]) == 0
    out = capsys.readouterr()
    plan = PlanOutput.model_validate_json(out.out)
    assert seen["called"] and plan.meta.model == "fake-llm"
    assert "agent did not finish" in out.err  # the fallbacks are surfaced on stderr


def test_cli_writes_out_file(tmp_path, capsys):
    out_path = tmp_path / "plans" / "plan.md"
    assert main([str(PROFILE_PATHS["complete"]), "--no-llm", "--out", str(out_path)]) == 0
    assert out_path.read_text().startswith("# Financial plan")
    assert capsys.readouterr().out == ""


def test_cli_bad_profile_exits_2(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text('{"meta": {}}')
    assert main([str(bad), "--no-llm"]) == 2
    assert "cannot read profile" in capsys.readouterr().err


def test_cli_runs_as_module():
    proc = subprocess.run(
        [sys.executable, "-m", "finagent.output", str(PROFILE_PATHS["complete"]), "--format", "json", "--no-llm"],
        capture_output=True, text=True, cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src"), "PATH": ""}, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert PlanOutput.model_validate_json(proc.stdout).meta.user_id


# --- review regressions ---------------------------------------------------------------------------


def test_agent_usage_is_carried_into_meta():
    class Counting(PlanAgents):
        def chat(self, messages, tools):
            reply = super().chat(messages, tools)
            return reply.model_copy(update={"usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    plan = generate_plan(load_profile("overspender"), model=Counting(), generated_at=FIXED)
    usage = plan.meta.agent_usage
    assert set(usage) == {"restructurer:50_30_20", "restructurer:goals_5_3", "advisor"}
    for name, totals in usage.items():
        steps = [s for s in plan.trace if s.agent == name and s.usage]
        assert totals["prompt_tokens"] == 10 * len(steps)
    assert generate_plan(load_profile("overspender"), generated_at=FIXED).meta.agent_usage == {}


def test_one_time_reclassification_is_reported_as_a_warning():
    class Reclassifier(PlanAgents):
        @staticmethod
        def _restructure(messages):
            tools = [m for m in messages if m.role == "tool"]
            if not tools:
                return turn(call("mark_one_time_discretionary", line_id="one:disneyland-trip",
                                 rationale="A Disneyland trip is a vacation the person can postpone."))
            return PlanAgents._restructure(messages[:2] + messages[4:]) if len(tools) > 1 else turn(call("get_budget_state"))

    plan = generate_plan(load_profile("overspender"), model=Reclassifier(), generated_at=FIXED)
    warned = [w for w in plan.meta.warnings if w.code == "one_time_reclassified"]
    assert warned and all(w.ref == "one:disneyland-trip" and "vacation" in w.message for w in warned)


def test_advisor_tool_crash_keeps_its_trace(monkeypatch):
    from finagent.output.tools import advisor_tools

    def broken(context):
        raise RuntimeError("compare bug")

    monkeypatch.setattr(advisor_tools, "comparison", broken)
    plan = generate_plan(load_profile("complete"), model=PlanAgents(), generated_at=FIXED)
    steps = [s for s in plan.trace if s.agent == "advisor"]
    assert steps and steps[0].tool == "compare_budgets" and "compare bug" in steps[0].result["error"]
    assert any(w.code == "agent_tool_error" and w.ref == "advisor" for w in plan.meta.warnings)
    assert plan.recommendation.generated_by is GeneratedBy.fallback


def test_current_budget_has_no_feasibility_verdict(plans):
    for plan in plans.values():
        assert plan.budgets[BudgetId.current].feasible is None
        assert plan.budgets[BudgetId.rule_50_30_20].feasible in (True, False)


EDGE_PROFILES = {
    "no_income": {"income": []},
    "zero_income": {"income": [{"source": "Job", "amount": 0, "frequency": "monthly", "is_net": True,
                                "effective_date": None}]},
    "no_expenses": {"recurring_expenses": [], "one_time_expenses": []},
    "all_protected_deficit": {
        "income": [{"source": "Job", "amount": 2500, "frequency": "monthly", "is_net": True, "effective_date": None}],
        "recurring_expenses": [{"name": "Rent", "amount": 2600, "frequency": "monthly", "category": "housing",
                                "need_or_want": "need", "is_fixed": True, "effective_date": None}],
    },
    "no_age": {"retirement": {"current_age": None, "target_retirement_age": 60, "current_balance": 1000,
                              "monthly_contribution": 100, "target": None}},
    "age_past_target": {"retirement": {"current_age": 70, "target_retirement_age": 60, "current_balance": 1000,
                                       "monthly_contribution": 100, "target": None}},
    "past_deadline": {"savings_goals": [{"name": "Japan trip", "target_amount": 4000, "deadline": "2026-09-01",
                                         "contribution": {"type": "amount", "value": 500, "frequency": "monthly"},
                                         "priority": "high", "effective_date": None}]},
}


@pytest.mark.parametrize("trim_cap", [0.0, 0.1])
@pytest.mark.parametrize("edge", list(EDGE_PROFILES))
def test_edge_profiles_run_end_to_end(edge, trim_cap):
    from invariants import check_budget_invariants
    from finagent.output.normalize import normalize

    data = json.loads(PROFILE_PATHS["complete"].read_text())
    data.update(EDGE_PROFILES[edge])
    profile = load_profile_dict(data)
    config = PlanConfig(need_trim_cap=trim_cap)
    plan = generate_plan(profile, config, generated_at=FIXED)
    text = render_text(plan)
    for heading in ("## Overall recommendation", "## Budget comparison", "## 12-month forecast",
                    "## Retirement (4% rule)", "## Key metrics", "## Warnings", "## Assumptions"):
        assert heading in text
    norm = normalize(profile, config)
    for bid in (BudgetId.rule_50_30_20, BudgetId.goals_5_3):
        check_budget_invariants(norm, plan.budgets[bid], config)
    # The fallback recommendation satisfies the advisor's own checks on every edge profile.
    from finagent.output.advice import false_claims

    rec = plan.recommendation
    assert 3 <= len(rec.major_changes) <= 5
    context = pipeline.PlanContext(norm, config, plan.budgets, plan.forecasts, plan.retirement, plan.metrics)
    assert false_claims(context, rec) == []


def load_profile_dict(data):
    from finagent.schema import FinancialProfile

    return FinancialProfile.model_validate(data)
