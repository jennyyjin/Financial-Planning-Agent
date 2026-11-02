"""Budget Restructurer tools: state, rule enforcement, finalize gate (TDD §11.2, §6, D2)."""

from __future__ import annotations

import json

import pytest

from finagent.output.agents.runtime import ToolRegistry, call
from finagent.output.models import AdjustmentAction, BudgetId, DecidedBy, GeneratedBy, Tier
from finagent.output.tools.budget_tools import BudgetSession, build_budget_tools

R50 = BudgetId.rule_50_30_20
G53 = BudgetId.goals_5_3
RATIONALE = "Dining out is the largest flexible want, so trimming it protects the savings floor."
SUMMARY = "Cut dining out to fit the wants allowance; protected needs remain above the ceiling."


def make(normalized_named, config, profile="overspender", budget_id=R50):
    session = BudgetSession.start(normalized_named(profile), budget_id, config)
    return session, build_budget_tools(session)


def run(registry: ToolRegistry, name: str, **args):
    """Dispatch one tool call as the agent would; returns (result, done)."""
    out, done = registry.dispatch(call(name, **args))
    json.dumps(out)  # every result must be strict JSON
    return out, done


def by_id(items, item_id):
    return next(i for i in items if i["id"] == item_id)


# --- registry ----------------------------------------------------------------------


def test_registry_tools_and_terminal(normalized_named, config):
    _, reg = make(normalized_named, config)
    assert set(reg.names) == {
        "get_budget_state", "list_adjustable", "adjust_item", "revert_item",
        "mark_one_time_discretionary", "finalize",
    }
    assert reg.terminal_names() == ["finalize"]
    adjust = next(s for s in reg.specs() if s.name == "adjust_item")
    assert set(adjust.parameters["required"]) == {"line_id", "new_monthly_amount", "rationale"}


def test_session_start(normalized_named, config):
    session, _ = make(normalized_named, config)
    assert session.budget.id is R50 and session.budget.targets is not None
    assert session.finalized is None
    with pytest.raises(ValueError):
        BudgetSession.start(normalized_named("complete"), BudgetId.current, config)


# --- get_budget_state ----------------------------------------------------------------


def test_get_budget_state_overspender_50_30_20(normalized_named, config):
    _, reg = make(normalized_named, config)
    state, _ = run(reg, "get_budget_state")
    assert state["budget_id"] == "50_30_20"
    assert state["targets"] == pytest.approx({"need_max": 2000, "want_max": 1200, "savings_min": 800})
    assert state["targets_pct_of_income"] == pytest.approx({"need_max": 50, "want_max": 30, "savings_min": 20})
    # Needs 3,080 + the $400/mo trip set-aside + the 800 savings floor leave nothing for wants.
    assert state["want_allowance"] == 0
    assert state["wants_over_allowance"] == pytest.approx(660)
    assert state["gaps"] == pytest.approx({"need": 1080, "savings": 800})


def test_get_budget_state_overspender(normalized_named, config):
    _, reg = make(normalized_named, config, budget_id=G53)
    state, done = run(reg, "get_budget_state")
    assert not done and "error" not in state
    assert state["budget_id"] == "goals_5_3"
    assert state["income_monthly"] == pytest.approx(4000)
    assert state["targets"] == pytest.approx({"need_max": 2250, "want_max": 1350, "savings_min": 400})
    assert state["want_allowance"] == pytest.approx(120)  # 4000 - 3080 - 400 (trip) - 400 (S_req)
    assert state["wants_over_allowance"] == pytest.approx(540)
    assert state["totals"] == pytest.approx(
        {"need": 3080, "want": 660, "savings": 0, "one_time_set_aside": 0, "unallocated": 260})
    assert state["shares_pct_of_income"]["need"] == pytest.approx(77.0)
    assert state["gaps"] == pytest.approx({"need": 830, "savings": 400})
    assert state["best_achievable"]["totals"]["want"] == pytest.approx(0)
    assert state["best_achievable"]["totals"]["need"] == pytest.approx(3080)
    assert state["remaining_cuts_possible"] == pytest.approx(660)
    assert state["finalized"] is False

    rent = by_id(state["lines"], "rec:rent")
    assert rent == {
        "id": "rec:rent", "name": "Rent", "kind": "recurring", "bucket": "need", "tier": "protected",
        "amount": pytest.approx(2200), "original_amount": pytest.approx(2200),
        "min_allowed": pytest.approx(2200), "category": "housing",
    }
    dining = by_id(state["lines"], "rec:dining-out")
    assert dining["tier"] == "scalable" and dining["min_allowed"] == 0
    assert by_id(state["lines"], "rec:streaming")["tier"] == "drop_only"
    assert len(state["lines"]) == 7

    trip = by_id(state["one_time_items"], "one:disneyland-trip")
    assert trip == {
        "id": "one:disneyland-trip", "name": "Disneyland trip", "amount": pytest.approx(2500),
        "original_amount": pytest.approx(2500), "expected_date": "2027-03-15",
        "discretionary": False, "tier": "protected",
    }
    goal = by_id(state["goals"], "goal:emergency-fund")
    assert goal["target"] == pytest.approx(3000) and goal["deadline"] == "2027-10-01"
    assert goal["required_monthly"] == pytest.approx(3000 / 11, abs=0.01)
    assert goal["stated_monthly"] is None
    assert state["debts"] == [{
        "id": "debt:credit-card", "name": "Credit card", "apr": 24.0,
        "balance": pytest.approx(5000), "minimum_payment": pytest.approx(150),
    }]
    assert state["adjustments"] == []

    outlook = state["savings_outlook"]
    assert outlook["available_for_savings"] == pytest.approx(260)
    assert outlook["one_time_set_aside_monthly"] == pytest.approx((2500 - 500) / 5)
    assert outlook["one_time_set_aside_unfunded"] == pytest.approx(400 - 260)
    assert outlook["goal_required_monthly_total"] == pytest.approx(3000 / 11, abs=0.01)


def test_get_budget_state_complete(normalized_named, config):
    _, reg = make(normalized_named, config, "complete")
    state, _ = run(reg, "get_budget_state")
    assert state["want_allowance"] == pytest.approx(1860)
    assert state["wants_over_allowance"] == 0
    assert state["gaps"] == pytest.approx({"savings": 340})
    japan = by_id(state["goals"], "goal:japan-trip")
    assert japan["required_monthly"] == pytest.approx(4000 / 7, abs=0.01)
    assert japan["stated_monthly"] == pytest.approx(500)
    assert by_id(state["lines"], "ret:contribution")["tier"] == "savings"


# --- list_adjustable -----------------------------------------------------------------


def test_list_adjustable(normalized_named, config):
    _, reg = make(normalized_named, config)
    out, _ = run(reg, "list_adjustable")
    items = out["items"]
    assert {i["id"] for i in items} == {"rec:dining-out", "rec:streaming", "one:disneyland-trip"}
    dining = by_id(items, "rec:dining-out")
    assert dining["tier"] == "scalable" and dining["allowed_range"] == pytest.approx({"min": 0, "max": 600})
    assert "allowed_values" not in dining
    streaming = by_id(items, "rec:streaming")
    assert streaming["tier"] == "drop_only" and streaming["allowed_values"] == pytest.approx([0, 60])
    trip = by_id(items, "one:disneyland-trip")
    assert trip["status"] == "reclassifiable_with_rationale"
    assert "allowed_range" not in trip
    # protected lines are never listed
    assert not any(i["id"] in ("rec:rent", "rec:medication", "debt:credit-card:min") for i in items)


def test_list_adjustable_after_reclassify(normalized_named, config):
    _, reg = make(normalized_named, config)
    run(reg, "mark_one_time_discretionary", line_id="one:disneyland-trip",
        rationale="A Disneyland trip is a vacation, not an essential cost.")
    trip = by_id(run(reg, "list_adjustable")[0]["items"], "one:disneyland-trip")
    assert trip["status"] == "adjustable"
    assert trip["allowed_range"] == pytest.approx({"min": 0, "max": 2500})


# --- adjust_item ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "profile, line_id, amount, match",
    [
        ("overspender", "rec:rent", 1500, "protected"),
        ("overspender", "rec:medication", 0, "protected"),
        ("mislabeled", "rec:medication", 0, "protected"),  # labelled want, category healthcare
        ("overspender", "debt:credit-card:min", 0, "protected"),
        ("complete", "ret:contribution", 100, "savings"),
        ("complete", "goal:japan-trip:contribution", 0, "savings"),
        ("overspender", "rec:dining-out", 700, "never increases"),
        ("overspender", "rec:streaming", 30, "all-or-nothing"),
        ("overspender", "one:disneyland-trip", 0, "mark_one_time_discretionary"),
        ("overspender", "rec:nope", 0, "Unknown line"),
    ],
)
def test_adjust_item_rejects(normalized_named, config, profile, line_id, amount, match):
    session, reg = make(normalized_named, config, profile)
    before = session.budget.model_dump()
    out, done = run(reg, "adjust_item", line_id=line_id, new_monthly_amount=amount, rationale=RATIONALE)
    assert not done
    assert match in out["error"]
    assert session.budget.model_dump() == before


def test_adjust_item_rejects_short_rationale(normalized_named, config):
    session, reg = make(normalized_named, config)
    out, _ = run(reg, "adjust_item", line_id="rec:dining-out", new_monthly_amount=100, rationale="too short")
    assert "rationale" in out["error"]
    assert session.budget.adjustments == []


def test_adjust_item_reduce_returns_summary(normalized_named, config):
    session, reg = make(normalized_named, config, budget_id=G53)
    out, done = run(reg, "adjust_item", line_id="rec:dining-out", new_monthly_amount=200, rationale=RATIONALE)
    assert not done and "error" not in out
    assert out["adjustment"]["action"] == "reduce"
    assert out["adjustment"]["before_monthly"] == pytest.approx(600)
    assert out["adjustment"]["after_monthly"] == pytest.approx(200)
    assert out["totals"]["want"] == pytest.approx(260)
    assert out["want_allowance"] == pytest.approx(120)
    assert out["wants_over_allowance"] == pytest.approx(140)
    assert out["remaining_cuts_possible"] == pytest.approx(260)
    assert out["gaps"]["need"] == pytest.approx(830)
    [adj] = session.budget.adjustments
    assert adj.decided_by is DecidedBy.agent and adj.rationale == RATIONALE


def test_adjust_item_drop_only_full_drop(normalized_named, config):
    session, reg = make(normalized_named, config)
    out, _ = run(reg, "adjust_item", line_id="rec:streaming", new_monthly_amount=0,
                 rationale="Streaming is an all-or-nothing subscription the user can pause for now.")
    assert out["adjustment"]["action"] == "drop"
    assert out["totals"]["want"] == pytest.approx(600)


def test_adjust_item_back_to_original(normalized_named, config):
    session, reg = make(normalized_named, config)
    run(reg, "adjust_item", line_id="rec:dining-out", new_monthly_amount=200, rationale=RATIONALE)
    out, _ = run(reg, "adjust_item", line_id="rec:dining-out", new_monthly_amount=600, rationale=RATIONALE)
    assert "error" not in out and out["adjustment"] is None
    assert "original" in out["note"]
    assert session.budget.adjustments == []
    assert out["totals"]["want"] == pytest.approx(660)


# --- revert / reclassify ---------------------------------------------------------------


def test_revert_item(normalized_named, config):
    session, reg = make(normalized_named, config)
    run(reg, "adjust_item", line_id="rec:dining-out", new_monthly_amount=0, rationale=RATIONALE)
    out, _ = run(reg, "revert_item", line_id="rec:dining-out")
    assert "error" not in out and out["totals"]["want"] == pytest.approx(660)
    assert session.budget.adjustments == []
    out, _ = run(reg, "revert_item", line_id="rec:dining-out")
    assert "no adjustment" in out["error"]
    assert "Unknown" in run(reg, "revert_item", line_id="rec:nope")[0]["error"]


def test_reclassify_then_drop_disneyland(normalized_named, config):
    session, reg = make(normalized_named, config)
    out, _ = run(reg, "mark_one_time_discretionary", line_id="one:disneyland-trip", rationale="fun")
    assert "rationale" in out["error"]
    out, _ = run(reg, "mark_one_time_discretionary", line_id="rec:dining-out", rationale=RATIONALE)
    assert "not a one-time" in out["error"]

    reason = "A Disneyland trip is a vacation; dropping it avoids overdrawing the user's $500 of savings."
    out, _ = run(reg, "mark_one_time_discretionary", line_id="one:disneyland-trip", rationale=reason)
    assert "error" not in out
    assert out["adjustment"]["action"] == "reclassify"
    assert out["item"]["discretionary"] is True and out["item"]["tier"] == "scalable"

    out, _ = run(reg, "adjust_item", line_id="one:disneyland-trip", new_monthly_amount=0, rationale=reason)
    assert out["adjustment"]["action"] == "drop"
    assert out["savings_outlook"]["one_time_set_aside_monthly"] == 0
    trip = next(l for l in session.budget.one_time if l.id == "one:disneyland-trip")
    assert trip.monthly_amount == 0 and trip.tier is Tier.scalable
    actions = [a.action for a in session.budget.adjustments]
    assert actions == [AdjustmentAction.reclassify, AdjustmentAction.drop]

    # Reverting twice undoes the drop, then the reclassification.
    run(reg, "revert_item", line_id="one:disneyland-trip")
    run(reg, "revert_item", line_id="one:disneyland-trip")
    assert session.budget.adjustments == []
    assert trip.monthly_amount == pytest.approx(2500) and trip.tier is Tier.protected


# --- finalize ------------------------------------------------------------------------


def test_finalize_too_early_rejected_then_accepted(normalized_named, config):
    session, reg = make(normalized_named, config, budget_id=G53)
    out, done = run(reg, "finalize", summary_rationale=SUMMARY)
    assert not done and session.finalized is None
    assert "allowance" in out["error"] and "540" in out["error"]

    run(reg, "adjust_item", line_id="rec:dining-out", new_monthly_amount=200, rationale=RATIONALE)
    out, done = run(reg, "finalize", summary_rationale=SUMMARY)
    assert not done and "140" in out["error"]  # still over the allowance

    run(reg, "adjust_item", line_id="rec:dining-out", new_monthly_amount=60, rationale=RATIONALE)
    out, done = run(reg, "finalize", summary_rationale="short")
    assert not done and "summary_rationale" in out["error"]

    out, done = run(reg, "finalize", summary_rationale=SUMMARY)
    assert done and "error" not in out
    final = session.finalized
    assert final is session.budget and final.generated_by is GeneratedBy.agent
    assert final.summary_rationale == SUMMARY
    assert final.totals.want == pytest.approx(120)
    assert not final.feasible and final.infeasibility.gaps == {"need": pytest.approx(830)}
    assert final.unfunded_goals == []
    assert out["feasible"] is False
    assert out["gaps"] == pytest.approx({"need": 830})
    assert out["totals"]["want"] == pytest.approx(120)
    assert out["totals"]["unallocated"] == pytest.approx(0)
    assert out["adjustments_count"] == len(final.adjustments)
    assert out["agent_adjustments_count"] == 1
    assert out["infeasibility_reason"]


def test_finalize_infeasible_when_nothing_left_to_cut(normalized_named, config):
    norm = normalized_named("overspender").model_copy(update={"net_monthly_income": 3000})
    session = BudgetSession.start(norm, R50, config)
    reg = build_budget_tools(session)
    assert run(reg, "get_budget_state")[0]["want_allowance"] == 0
    run(reg, "adjust_item", line_id="rec:dining-out", new_monthly_amount=0, rationale=RATIONALE)
    assert "error" in run(reg, "finalize", summary_rationale=SUMMARY)[0]  # streaming still cuttable
    run(reg, "adjust_item", line_id="rec:streaming", new_monthly_amount=0,
        rationale="Streaming is an all-or-nothing subscription the user can pause for now.")
    out, done = run(reg, "finalize", summary_rationale=SUMMARY)
    assert done and out["feasible"] is False


def test_finalize_no_cuts_needed_complete(normalized_named, config):
    session, reg = make(normalized_named, config, "complete")
    out, done = run(reg, "finalize", summary_rationale="Wants are already within the allowance, so no cuts were needed.")
    assert done and out["feasible"] is True
    assert out["agent_adjustments_count"] == 0
    assert session.finalized.feasible
    assert session.finalized.totals.unallocated == pytest.approx(0)
    assert all(a.decided_by is DecidedBy.policy for a in session.finalized.adjustments)


def test_no_changes_after_finalize(normalized_named, config):
    session, reg = make(normalized_named, config, "complete")
    run(reg, "finalize", summary_rationale="Wants are already within the allowance, so no cuts were needed.")
    before = session.finalized.model_dump()
    for name, args in [
        ("adjust_item", {"line_id": "rec:eating-out", "new_monthly_amount": 100, "rationale": RATIONALE}),
        ("revert_item", {"line_id": "rec:eating-out"}),
        ("mark_one_time_discretionary", {"line_id": "one:laptop", "rationale": RATIONALE}),
        ("finalize", {"summary_rationale": SUMMARY}),
    ]:
        out, done = run(reg, name, **args)
        assert "finalized" in out["error"] and not done
    assert session.finalized.model_dump() == before
    assert run(reg, "get_budget_state")[0]["finalized"] is True


def test_goals_5_3_allowance_without_one_time_costs(normalized_named, config):
    norm = normalized_named("overspender").model_copy(update={"one_time": []})
    session = BudgetSession.start(norm, G53, config)
    reg = build_budget_tools(session)
    state, _ = run(reg, "get_budget_state")
    assert state["want_allowance"] == pytest.approx(520)
    run(reg, "adjust_item", line_id="rec:dining-out", new_monthly_amount=460, rationale=RATIONALE)
    out, done = run(reg, "finalize", summary_rationale=SUMMARY)
    assert done and out["totals"]["want"] == pytest.approx(520)


# --- review regressions: guardrails in code ----------------------------------------------------


def test_nan_amount_is_rejected_before_the_handler_runs(normalized_named, config):
    """Reviewer repro: NaN passed validation, corrupted the budget and let finalize through."""
    from finagent.output.agents.runtime import _parse_arguments

    session, reg = make(normalized_named, config)
    before = session.budget.model_dump()
    for amount in (float("nan"), float("inf")):
        out, done = run(reg, "adjust_item", line_id="rec:dining-out", new_monthly_amount=amount, rationale=RATIONALE)
        assert not done and "finite" in out["error"]
    assert session.budget.model_dump() == before
    # The NaN / Infinity JSON tokens are refused at parse time too.
    assert set(_parse_arguments('{"line_id": "rec:dining-out", "new_monthly_amount": NaN}')) == {"_raw"}


def test_nan_is_rejected_by_policy_as_defense_in_depth(normalized_named, config):
    from finagent.output.policy import validate_adjustment

    session, _ = make(normalized_named, config)
    dining = next(l for l in session.budget.lines if l.id == "rec:dining-out")
    assert "finite" in validate_adjustment(dining, float("nan"), RATIONALE, config)


def test_finalize_rejects_cuts_below_the_allowance(normalized_named, config):
    """Reviewer repro: complete profile, wants 570 against an allowance of 1,860, both dropped."""
    session, reg = make(normalized_named, config, "complete")
    run(reg, "adjust_item", line_id="rec:eating-out", new_monthly_amount=0, rationale=RATIONALE)
    run(reg, "adjust_item", line_id="rec:yoga-membership", new_monthly_amount=0,
        rationale="Yoga is an all-or-nothing membership, dropped to free money for savings.")
    out, done = run(reg, "finalize", summary_rationale=SUMMARY)
    assert not done and "Too many cuts" in out["error"] and session.finalized is None
    run(reg, "revert_item", line_id="rec:eating-out")
    run(reg, "revert_item", line_id="rec:yoga-membership")
    out, done = run(reg, "finalize", summary_rationale="Wants already fit the allowance, so nothing was cut.")
    assert done and out["agent_adjustments_count"] == 0


def test_finalize_rejects_over_cutting_mislabeled_goals_5_3(normalized_named, config):
    session, reg = make(normalized_named, config, "mislabeled", G53)
    state, _ = run(reg, "get_budget_state")
    assert state["wants_over_allowance"] == pytest.approx(60)  # 850 against 790 (after the $300/mo set-aside)
    for line_id in ("rec:concerts", "rec:shopping"):
        run(reg, "adjust_item", line_id=line_id, new_monthly_amount=0, rationale=RATIONALE)
    out, done = run(reg, "finalize", summary_rationale=SUMMARY)
    assert not done and "Too many cuts" in out["error"]
    run(reg, "adjust_item", line_id="rec:concerts", new_monthly_amount=290, rationale=RATIONALE)
    run(reg, "revert_item", line_id="rec:shopping")
    out, done = run(reg, "finalize", summary_rationale=SUMMARY)
    assert done, out


def test_finalize_accepts_an_unavoidable_all_or_nothing_overshoot(normalized_named, config):
    # Only an all-or-nothing $200 want against a $120 allowance: dropping it undershoots by $120, but
    # restoring it would not fit, so the cut is the smallest possible.
    norm = normalized_named("overspender")
    streaming = next(l for l in norm.lines if l.id == "rec:streaming").model_copy(
        update={"monthly_amount": 200, "original_amount": 200})
    lines = [l for l in norm.lines if l.id not in ("rec:dining-out", "rec:streaming")] + [streaming]
    session = BudgetSession.start(norm.model_copy(update={"lines": lines}), G53, config)
    reg = build_budget_tools(session)
    assert session.allowance() == pytest.approx(120)
    run(reg, "adjust_item", line_id="rec:streaming", new_monthly_amount=0,
        rationale="Streaming is an all-or-nothing subscription the user can pause for now.")
    out, done = run(reg, "finalize", summary_rationale=SUMMARY)
    assert done, out


def test_one_time_item_can_be_cut_only_to_fix_a_shortfall(normalized_named, config):
    session, reg = make(normalized_named, config, "complete")
    out, _ = run(reg, "mark_one_time_discretionary", line_id="one:laptop",
                 rationale="A new laptop is an upgrade the user could postpone.")
    assert "error" not in out
    out, _ = run(reg, "adjust_item", line_id="one:laptop", new_monthly_amount=0,
                 rationale="A new laptop is an upgrade the user could postpone.")
    assert "No shortfall" in out["error"]
    assert next(l for l in session.budget.one_time if l.id == "one:laptop").monthly_amount == 1500


def test_essential_one_time_item_reclassification_is_rejected(normalized_named, config):
    session, reg = make(normalized_named, config, "mislabeled")
    out, _ = run(reg, "mark_one_time_discretionary", line_id="one:dental-surgery",
                 rationale="Dental surgery could be postponed to next year.")
    assert "essential" in out["error"]
    assert all(a.action is not AdjustmentAction.reclassify for a in session.budget.adjustments)


def test_rationale_with_invented_numbers_is_rejected_and_not_applied(normalized_named, config):
    """Reviewer repro: 'Cutting dining saves you $9,999 a year and lets you retire at 41.' was accepted."""
    session, reg = make(normalized_named, config, budget_id=G53)
    before = session.budget.model_dump()
    out, _ = run(reg, "adjust_item", line_id="rec:dining-out", new_monthly_amount=60,
                 rationale="Cutting dining saves you $9,999 a year and lets you retire at 41.")
    assert "9999" in out["error"] and "41" in out["error"]
    assert session.budget.model_dump() == before
    # Tool numbers (the new amount, the cut, its annual size, the allowance) are fine.
    out, _ = run(reg, "adjust_item", line_id="rec:dining-out", new_monthly_amount=60,
                 rationale="Dining out goes from $600 to $60, a $540 cut ($6,480 a year), so wants fit the $120 allowance.")
    assert "error" not in out, out


def test_summary_with_invented_numbers_is_rejected(normalized_named, config):
    session, reg = make(normalized_named, config, budget_id=G53)
    run(reg, "adjust_item", line_id="rec:dining-out", new_monthly_amount=60, rationale=RATIONALE)
    out, done = run(reg, "finalize", summary_rationale="This budget now saves 47% of income and meets every target.")
    assert not done and "47" in out["error"] and session.finalized is None
    out, done = run(reg, "finalize", summary_rationale="Dining out is cut to $60 so wants fit the $120 allowance; "
                    "needs stay $830 above their ceiling.")
    assert done, out
