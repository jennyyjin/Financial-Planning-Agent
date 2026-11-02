"""Budget Restructurer Agent with scripted models (TDD §11.2, §14.2, §14.4; no network)."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from finagent.output import policy
from finagent.output.agents.restructurer import load_prompt, make_agent_restructurer, restructure_with_agent
from finagent.output.agents.runtime import AssistantTurn, Message, ScriptedModel, ToolSpec, call, turn
from finagent.output.budget import build_budgets
from finagent.output.models import AdjustmentAction, BudgetId, DecidedBy, GeneratedBy, Tier
from conftest import ALL_PROFILES
from invariants import check_budget_invariants

R50 = BudgetId.rule_50_30_20
G53 = BudgetId.goals_5_3
DINING = "Dining out is this user's largest flexible want; cutting it to $60 keeps a little social spending."
SUMMARY = "Cut dining out to fit the wants allowance; rent and other protected needs keep this budget infeasible."


class GreedyAgent:
    """A scripted ChatModel that reads get_budget_state and cuts the largest flexible wants first."""

    name = "greedy"

    def chat(self, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn:
        tool_msgs = [m for m in messages if m.role == "tool"]
        if not tool_msgs:
            return turn(call("get_budget_state"))
        plan = self._plan(json.loads(tool_msgs[0].content))
        issued = sum(1 for m in messages if m.role == "assistant" and m.tool_calls) - 1
        if issued < len(plan):
            return turn(plan[issued])
        return turn(call("finalize", summary_rationale="Cut the largest flexible wants first until they fit."))

    @staticmethod
    def _plan(state: dict) -> list:
        excess = state["wants_over_allowance"]
        wants = [l for l in state["lines"] if l["bucket"] == "want"]
        calls = []
        for line in sorted((l for l in wants if l["tier"] == "scalable"), key=lambda l: -l["amount"]):
            if excess <= 0.01:
                break
            cut = min(excess, line["amount"] - line["min_allowed"])
            excess -= cut
            calls.append(call("adjust_item", line_id=line["id"], new_monthly_amount=line["amount"] - cut,
                              rationale=f"{line['name']} is a flexible want, trimmed to fit the wants allowance."))
        for line in sorted((l for l in wants if l["tier"] == "drop_only"), key=lambda l: -l["amount"]):
            if excess <= 0.01:
                break
            excess -= line["amount"]
            calls.append(call("adjust_item", line_id=line["id"], new_monthly_amount=0,
                              rationale=f"{line['name']} is all-or-nothing, dropped to fit the wants allowance."))
        return calls


def test_load_prompt():
    prompt = load_prompt()
    for phrase in ("get_budget_state", "finalize", "mark_one_time_discretionary", "drop_only", "medical"):
        assert phrase in prompt


def test_happy_path_rent_rejected_then_dining_cut(normalized_named, config):
    norm = normalized_named("overspender")
    model = ScriptedModel([
        turn(call("get_budget_state")),
        turn(call("adjust_item", line_id="rec:rent", new_monthly_amount=1500,
                  rationale="Rent is the largest expense in this budget.")),
        turn(call("adjust_item", line_id="rec:dining-out", new_monthly_amount=60, rationale=DINING)),
        turn(call("finalize", summary_rationale=SUMMARY)),
    ])
    budget, result = restructure_with_agent(norm, G53, config, model)

    assert result.completed and result.steps == 4
    rent_step = next(s for s in result.trace if s.tool == "adjust_item" and s.arguments["line_id"] == "rec:rent")
    assert "protected" in rent_step.result["error"]
    assert all(s.agent == "restructurer:goals_5_3" for s in result.trace)

    assert budget.generated_by is GeneratedBy.agent
    assert budget.summary_rationale == SUMMARY
    rent = next(l for l in budget.lines if l.id == "rec:rent")
    assert rent.monthly_amount == pytest.approx(2200)
    [cut] = [a for a in budget.adjustments if a.decided_by is DecidedBy.agent]
    assert cut.line_id == "rec:dining-out" and cut.rationale == DINING
    assert cut.after_monthly == pytest.approx(60)
    assert budget.totals.want == pytest.approx(120)
    assert not budget.feasible and budget.infeasibility.gaps == {"need": pytest.approx(830)}

    # The user message describes this rule's task.
    user = model.calls[0][1].content
    assert "5:3" in user and "Disneyland trip" in user and "Emergency fund" in user
    assert "$120.00" in user  # wants allowance
    assert "$400.00/mo kept in cash" in user  # the one-time set-aside comes before wants


def test_finalize_too_early_then_corrected(normalized_named, config):
    model = ScriptedModel([
        turn(call("finalize", summary_rationale=SUMMARY)),
        turn(call("adjust_item", line_id="rec:dining-out", new_monthly_amount=60, rationale=DINING)),
        turn(call("finalize", summary_rationale=SUMMARY)),
    ])
    budget, result = restructure_with_agent(normalized_named("overspender"), G53, config, model)
    assert result.completed
    assert "error" in result.trace[0].result
    assert budget.generated_by is GeneratedBy.agent


def test_script_exhausted_falls_back(normalized_named, config):
    model = ScriptedModel([turn(call("get_budget_state"))])
    budget, result = restructure_with_agent(normalized_named("overspender"), G53, config, model, max_steps=4)
    assert not result.completed and result.error == "max_steps" and result.steps == 4
    assert budget.generated_by is GeneratedBy.fallback
    assert all(a.decided_by is not DecidedBy.agent for a in budget.adjustments)
    assert budget.totals.want == pytest.approx(120)


def test_max_steps_defaults_to_config(normalized_named, config):
    cfg = config.model_copy(update={"agent_max_steps": 2})
    model = ScriptedModel([turn(call("get_budget_state"))] * 5)
    budget, result = restructure_with_agent(normalized_named("complete"), G53, cfg, model)
    assert result.steps == 2 and not result.completed
    assert budget.generated_by is GeneratedBy.fallback


def test_model_failure_falls_back(normalized_named, config):
    class Broken:
        name = "broken"

        def chat(self, messages, tools):
            raise RuntimeError("provider down")

    budget, result = restructure_with_agent(normalized_named("complete"), R50, config, Broken())
    assert not result.completed and "provider down" in result.error
    assert budget.generated_by is GeneratedBy.fallback


def test_no_cuts_needed_complete(normalized_named, config):
    model = ScriptedModel([
        turn(call("get_budget_state")),
        turn(call("finalize", summary_rationale="Wants are already within the allowance, so no cuts were made.")),
    ])
    budget, result = restructure_with_agent(normalized_named("complete"), R50, config, model)
    assert result.completed and budget.generated_by is GeneratedBy.agent and budget.feasible
    assert not any(a.decided_by is DecidedBy.agent for a in budget.adjustments)


def test_make_agent_restructurer_with_build_budgets(normalized_named, config):
    results = []
    budgets = build_budgets(normalized_named("overspender"), config, restructure=make_agent_restructurer(GreedyAgent(), results))
    assert [r.completed for r in results] == [True, True]
    assert {r.trace[0].agent for r in results} == {"restructurer:50_30_20", "restructurer:goals_5_3"}
    assert budgets[R50].generated_by is GeneratedBy.agent and budgets[G53].generated_by is GeneratedBy.agent
    assert budgets[R50].totals.want == pytest.approx(0)  # the trip set-aside leaves no wants allowance
    assert budgets[G53].totals.want == pytest.approx(120)
    assert budgets[BudgetId.current].generated_by is GeneratedBy.as_stated


def test_make_agent_restructurer_without_results_list(normalized_named, config):
    restructure = make_agent_restructurer(GreedyAgent())
    assert restructure(normalized_named("complete"), R50, config).generated_by is GeneratedBy.agent


# --- invariants of agent-finalized budgets (TDD §14.2) ----------------------------------


def _latest_state(messages: list[Message]) -> dict:
    for m in reversed(messages):
        if m.role == "tool" and m.name == "get_budget_state":
            return json.loads(m.content)
    raise AssertionError("no get_budget_state result yet")


class ReclassifyAndDropAgent:
    """Reclassifies every one-time item it may (essential ones are refused), drops each one it can, then
    cuts greedily from a fresh state."""

    name = "reclassify-drop"

    def __init__(self) -> None:
        self.queue: list = []
        self.phase = 0

    def chat(self, messages, tools):
        if self.queue:
            return turn(self.queue.pop(0))
        self.phase += 1
        if self.phase == 1:
            return turn(call("get_budget_state"))
        if self.phase == 2:
            ids = [i["id"] for i in _latest_state(messages)["one_time_items"]]
            reason = "This one-time item is optional spending the person can postpone."
            self.queue = [call("mark_one_time_discretionary", line_id=i, rationale=reason) for i in ids]
            self.queue += [call("adjust_item", line_id=i, new_monthly_amount=0, rationale=reason) for i in ids]
            return turn(self.queue.pop(0)) if self.queue else turn(call("get_budget_state"))
        if self.phase == 3:
            return turn(call("get_budget_state"))
        if self.phase == 4:
            self.queue = GreedyAgent._plan(_latest_state(messages))
            if self.queue:
                return turn(self.queue.pop(0))
        return turn(call("finalize", summary_rationale="Dropped optional one-time items, then cut flexible wants."))


class OverCutter:
    """Zeroes every adjustable want, then insists on finalizing (rejected whenever the cut was not needed)."""

    name = "over-cutter"

    def __init__(self) -> None:
        self.queue: list = []
        self.started = False

    def chat(self, messages, tools):
        if self.queue:
            return turn(self.queue.pop(0))
        if not self.started:
            self.started = True
            return turn(call("get_budget_state"))
        state = _latest_state(messages)
        if not any(m.role == "tool" and m.name == "adjust_item" for m in messages):
            self.queue = [call("adjust_item", line_id=l["id"], new_monthly_amount=0,
                               rationale="Every want is cut to the bone to maximise savings.")
                          for l in state["lines"] if l["bucket"] == "want" and l["tier"] != "protected"]
            if self.queue:
                return turn(self.queue.pop(0))
        return turn(call("finalize", summary_rationale="Cut every want to maximise savings."))


class RevertThenRecut(GreedyAgent):
    """Cuts every want to 0, reverts them all, then cuts greedily."""

    name = "revert-recut"

    def __init__(self) -> None:
        self.queue: list = []
        self.started = False

    def chat(self, messages, tools):
        if self.queue:
            return turn(self.queue.pop(0))
        if not self.started:
            self.started = True
            state = None
            return turn(call("get_budget_state"))
        state = _latest_state(messages)
        wants = [l for l in state["lines"] if l["bucket"] == "want" and l["tier"] != "protected"]
        self.queue = [call("adjust_item", line_id=l["id"], new_monthly_amount=0,
                           rationale="Trying the deepest possible cut first, to compare.") for l in wants]
        self.queue += [call("revert_item", line_id=l["id"]) for l in wants]
        self.queue += self._plan(state)
        self.queue.append(call("finalize", summary_rationale="Cut the largest flexible wants first until they fit."))
        return turn(self.queue.pop(0))


AGENTS = {"greedy": GreedyAgent, "reclassify-drop": ReclassifyAndDropAgent, "over-cutter": OverCutter,
          "revert-recut": RevertThenRecut}


@pytest.mark.parametrize("agent", list(AGENTS))
@pytest.mark.parametrize("budget_id", [R50, G53], ids=lambda b: b.value)
@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_agent_budget_invariants(normalized_named, config, profile, budget_id, agent):
    """The strict §14.2 invariants hold for whatever budget comes out, agent-finalized or fallback."""
    norm = normalized_named(profile)
    budget, result = restructure_with_agent(norm, budget_id, config, AGENTS[agent](), max_steps=40)
    check_budget_invariants(norm, budget, config)
    if agent in ("greedy", "revert-recut", "reclassify-drop"):
        assert result.completed and budget.generated_by is GeneratedBy.agent, result.error
    reclassified = [a for a in budget.adjustments if a.action is AdjustmentAction.reclassify]
    assert not any("dental" in a.line_id for a in reclassified)


def test_over_cutter_is_refused_when_cuts_were_not_needed(normalized_named, config):
    budget, result = restructure_with_agent(normalized_named("complete"), R50, config, OverCutter(), max_steps=10)
    assert not result.completed and budget.generated_by is GeneratedBy.fallback
    assert any("Too many cuts" in (s.result or {}).get("error", "") for s in result.trace if s.tool == "finalize")
    assert not any(a.decided_by is DecidedBy.agent for a in budget.adjustments)


def test_reclassify_agent_drops_the_trip_but_not_dental_surgery(normalized_named, config):
    budget, result = restructure_with_agent(normalized_named("mislabeled"), R50, config, ReclassifyAndDropAgent(),
                                            max_steps=40)
    assert result.completed
    one = {l.id: l for l in budget.one_time}
    assert one["one:dental-surgery"].monthly_amount == 1200 and not one["one:dental-surgery"].discretionary
    errors = [s.result["error"] for s in result.trace if s.tool == "mark_one_time_discretionary" and "error" in s.result]
    assert any("essential" in e for e in errors)
