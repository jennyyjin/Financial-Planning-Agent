"""Budget variants: targets, adjustments, surplus policy, fallback restructurer (TDD §7, §14.1-14.2)."""

from __future__ import annotations

from collections import Counter

import pytest

from finagent.output.budget import (
    AdjustmentError,
    apply_adjustment,
    best_achievable,
    build_budgets,
    build_current_budget,
    fallback_restructure,
    finalize_budget,
    gaps,
    mark_one_time_discretionary,
    remaining_cuts_possible,
    revert_adjustment,
    start_restructure,
    targets_for,
    want_allowance,
)
from finagent.output.config import PlanConfig
from finagent.output.models import (
    AdjustmentAction,
    BudgetId,
    DecidedBy,
    GeneratedBy,
    Tier,
)
from conftest import ALL_PROFILES

R50 = BudgetId.rule_50_30_20
G53 = BudgetId.goals_5_3
VARIANTS = [R50, G53]
RATIONALE = "Dining out is the largest flexible want, so trimming it protects the savings floor."


def line(budget, line_id):
    return next(l for l in budget.lines + budget.one_time if l.id == line_id)


def adjustments_for(budget, line_id):
    return [a for a in budget.adjustments if a.line_id == line_id]


# --- targets -------------------------------------------------------------------


def test_targets_current_is_none(normalized_named, config):
    assert targets_for(BudgetId.current, normalized_named("complete"), config) is None


def test_targets_50_30_20_complete(normalized_named, config):
    t = targets_for(R50, normalized_named("complete"), config)
    assert t.need_max == pytest.approx(3100)
    assert t.want_max == pytest.approx(1860)
    assert t.savings_min == pytest.approx(1240)


def test_targets_goals_5_3_complete_reduces_to_50_30_20(normalized_named, config):
    norm = normalized_named("complete")
    t53, t50 = targets_for(G53, norm, config), targets_for(R50, norm, config)
    assert t53.savings_min == pytest.approx(1240)  # max(1240, 571.43 + 400)
    assert t53.need_max == pytest.approx(t50.need_max)
    assert t53.want_max == pytest.approx(t50.want_max)


def test_targets_big_goals(normalized_named, config):
    norm = normalized_named("big_goals")
    t50 = targets_for(R50, norm, config)
    assert (t50.need_max, t50.want_max, t50.savings_min) == pytest.approx((2500, 1500, 1000))
    t = targets_for(G53, norm, config)
    s_req = 30000 / 36 + 12000 / 12 + 300
    assert t.savings_min == pytest.approx(s_req)
    assert s_req == pytest.approx(2133.33, abs=0.01)
    assert t.need_max == pytest.approx(5 / 8 * (5000 - s_req))
    assert t.want_max == pytest.approx(3 / 8 * (5000 - s_req))


def test_targets_goals_5_3_remainder_never_negative(normalized_named, config):
    norm = normalized_named("big_goals").model_copy(update={"net_monthly_income": 1000})
    t = targets_for(G53, norm, config)
    assert t.need_max == 0 and t.want_max == 0


def test_want_allowance_savings_floor_beats_wants(normalized_named, config):
    norm = normalized_named("overspender")
    budget = start_restructure(norm, R50, config)
    assert want_allowance(budget, budget.targets) == pytest.approx(4000 - 3080 - 800)
    norm = normalized_named("complete")
    budget = start_restructure(norm, R50, config)
    assert want_allowance(budget, budget.targets) == pytest.approx(1860)


def test_want_allowance_never_negative(normalized_named, config):
    norm = normalized_named("overspender").model_copy(update={"net_monthly_income": 3000})
    budget = start_restructure(norm, R50, config)
    assert want_allowance(budget, budget.targets) == 0


# --- start_restructure ---------------------------------------------------------


def test_start_restructure_copies_lines(normalized_named, config):
    norm = normalized_named("complete")
    budget = start_restructure(norm, R50, config)
    assert budget.id is R50 and budget.targets is not None
    assert budget.generated_by is GeneratedBy.agent
    assert [l.id for l in budget.lines] == [l.id for l in norm.lines]
    budget.lines[0].monthly_amount = 0
    assert norm.lines[0].monthly_amount > 0  # deep copy
    assert budget.totals.need == pytest.approx(2443.33, abs=0.01)


def test_start_restructure_rejects_current(normalized_named, config):
    with pytest.raises(ValueError):
        start_restructure(normalized_named("complete"), BudgetId.current, config)


# --- apply / revert / reclassify -------------------------------------------------


@pytest.mark.parametrize(
    "profile, line_id, amount",
    [
        ("complete", "rec:rent", 1500),
        ("overspender", "rec:medication", 0),
        ("mislabeled", "rec:medication", 0),  # labelled want, category healthcare
        ("mislabeled", "rec:mystery-charge", 0),  # null label
        ("overspender", "debt:credit-card:min", 0),
        ("complete", "ret:contribution", 100),  # savings line
        ("complete", "goal:japan-trip:contribution", 0),
        ("complete", "rec:eating-out", 500),  # increase
        ("complete", "rec:yoga-membership", 60),  # partial drop_only
        ("complete", "one:laptop", 0),  # one-time not yet discretionary
        ("complete", "rec:eating-out", -5),
        ("complete", "rec:nope", 0),  # unknown id
    ],
)
def test_apply_adjustment_rejects(normalized_named, config, profile, line_id, amount):
    budget = start_restructure(normalized_named(profile), R50, config)
    before = budget.model_dump()
    with pytest.raises(AdjustmentError):
        apply_adjustment(budget, line_id, amount, RATIONALE, config)
    assert budget.model_dump() == before


def test_apply_adjustment_rejects_short_rationale(normalized_named, config):
    budget = start_restructure(normalized_named("complete"), R50, config)
    with pytest.raises(AdjustmentError, match="rationale"):
        apply_adjustment(budget, "rec:eating-out", 200, "too short", config)


def test_apply_reduce_and_drop(normalized_named, config):
    budget = start_restructure(normalized_named("complete"), R50, config)
    adj = apply_adjustment(budget, "rec:eating-out", 150, RATIONALE, config)
    assert adj.action is AdjustmentAction.reduce
    assert (adj.before_monthly, adj.after_monthly) == pytest.approx((450, 150))
    assert adj.rule == "50_30_20.wants_ceiling" and adj.decided_by is DecidedBy.agent
    assert budget.totals.want == pytest.approx(270)
    drop = apply_adjustment(budget, "rec:yoga-membership", 0, "Yoga is an all-or-nothing membership we can drop.", config)
    assert drop.action is AdjustmentAction.drop
    assert budget.totals.want == pytest.approx(150)
    assert len(budget.adjustments) == 2


def test_apply_adjustment_replaces_existing_record(normalized_named, config):
    budget = start_restructure(normalized_named("complete"), R50, config)
    apply_adjustment(budget, "rec:eating-out", 150, RATIONALE, config)
    adj = apply_adjustment(budget, "rec:eating-out", 300, RATIONALE + " A smaller cut is enough.", config)
    records = adjustments_for(budget, "rec:eating-out")
    assert records == [adj]
    assert adj.before_monthly == pytest.approx(450) and adj.after_monthly == pytest.approx(300)
    assert budget.totals.want == pytest.approx(420)


def test_apply_back_to_original_removes_record(normalized_named, config):
    budget = start_restructure(normalized_named("complete"), R50, config)
    apply_adjustment(budget, "rec:eating-out", 150, RATIONALE, config)
    assert apply_adjustment(budget, "rec:eating-out", 450, RATIONALE, config) is None
    assert budget.adjustments == []
    assert line(budget, "rec:eating-out").monthly_amount == pytest.approx(450)


def test_revert_adjustment(normalized_named, config):
    budget = start_restructure(normalized_named("complete"), R50, config)
    apply_adjustment(budget, "rec:eating-out", 150, RATIONALE, config)
    revert_adjustment(budget, "rec:eating-out")
    assert budget.adjustments == []
    assert budget.totals.want == pytest.approx(570)
    with pytest.raises(AdjustmentError):
        revert_adjustment(budget, "rec:eating-out")
    with pytest.raises(AdjustmentError):
        revert_adjustment(budget, "rec:nope")


def test_one_time_reclassify_then_drop(normalized_named, config):
    budget = start_restructure(normalized_named("overspender"), R50, config)
    totals_before = budget.totals.model_copy()
    with pytest.raises(AdjustmentError):
        mark_one_time_discretionary(budget, "one:disneyland-trip", "fun", config)
    with pytest.raises(AdjustmentError):
        mark_one_time_discretionary(budget, "rec:dining-out", RATIONALE, config)
    reason = "A Disneyland trip is a vacation, not an essential cost, given the budget deficit."
    rec = mark_one_time_discretionary(budget, "one:disneyland-trip", reason, config)
    assert rec.action is AdjustmentAction.reclassify
    assert rec.before_monthly == rec.after_monthly == pytest.approx(2500)
    trip = line(budget, "one:disneyland-trip")
    assert trip.discretionary and trip.tier is Tier.scalable
    drop = apply_adjustment(budget, "one:disneyland-trip", 0, reason, config)
    assert drop.action is AdjustmentAction.drop and trip.monthly_amount == 0
    assert [a.action for a in adjustments_for(budget, trip.id)] == [AdjustmentAction.reclassify, AdjustmentAction.drop]
    assert budget.totals == totals_before  # one-time items stay outside monthly totals


# --- best achievable / gaps --------------------------------------------------------


def test_best_achievable_and_remaining_cuts(normalized_named, config):
    budget = start_restructure(normalized_named("overspender"), R50, config)
    best = best_achievable(budget, config)
    assert best.need == pytest.approx(3080) and best.want == 0
    assert remaining_cuts_possible(budget, config) == pytest.approx(660)
    apply_adjustment(budget, "rec:dining-out", 100, RATIONALE, config)
    assert remaining_cuts_possible(budget, config) == pytest.approx(160)


def test_gaps_only_positive_entries(normalized_named, config):
    budget = start_restructure(normalized_named("overspender"), R50, config)
    g = gaps(budget, config)
    assert g["need"] == pytest.approx(1080)
    assert g["savings"] == pytest.approx(800)
    assert "want" not in g  # 660 <= 1200
    assert gaps(start_restructure(normalized_named("complete"), R50, config), config) == {
        "savings": pytest.approx(340)
    }


# --- worked examples -------------------------------------------------------------


def test_complete_50_30_20_surplus_policy(normalized_named, config):
    budget = fallback_restructure(normalized_named("complete"), R50, config)
    assert budget.generated_by is GeneratedBy.fallback
    cuts = [a for a in budget.adjustments if a.action in (AdjustmentAction.reduce, AdjustmentAction.drop)]
    assert cuts == []
    rules = [a.rule for a in budget.adjustments]
    assert rules == ["surplus.goal_shortfall", "surplus.emergency_fund", "surplus.retirement"]
    assert all(a.decided_by is DecidedBy.policy and a.action is AdjustmentAction.allocate for a in budget.adjustments)
    japan, emergency, ret = budget.adjustments
    assert japan.after_monthly - japan.before_monthly == pytest.approx(4000 / 7 - 500)
    assert japan.after_monthly - japan.before_monthly == pytest.approx(71.43, abs=0.01)
    assert emergency.line_id == "savings:emergency-fund"
    assert emergency.after_monthly == pytest.approx((3 * (1800 + 100 * 52 / 12 + 210) - 6500) / 12)
    assert ret.line_id == "ret:contribution" and ret.before_monthly == pytest.approx(400)
    assert budget.totals.unallocated == pytest.approx(0)
    assert budget.totals.savings == pytest.approx(6200 - 2443.33 - 570, abs=0.01)
    assert budget.feasible and budget.infeasibility is None and budget.unfunded_goals == []
    assert budget.headroom["want"] == pytest.approx(1860 - 570)
    assert budget.headroom["need"] == pytest.approx(3100 - (1800 + 100 * 52 / 12 + 210))
    assert not any(l.id.endswith(":extra") for l in budget.lines)  # 5.5% APR < 8%


def test_complete_goals_5_3_matches_50_30_20(normalized_named, config):
    budgets = build_budgets(normalized_named("complete"), config)
    a, b = budgets[R50], budgets[G53]
    assert a.targets == b.targets
    assert a.totals == b.totals
    assert [l.model_dump() for l in a.lines] == [l.model_dump() for l in b.lines]


def test_big_goals_5_3_infeasible_on_needs(normalized_named, config):
    budget = fallback_restructure(normalized_named("big_goals"), G53, config)
    s_req = 30000 / 36 + 12000 / 12 + 300
    need_max = 5 / 8 * (5000 - s_req)
    assert budget.totals.want == pytest.approx(5000 - 2050 - s_req)
    assert budget.totals.want == pytest.approx(816.67, abs=0.01)
    assert not budget.feasible
    assert budget.infeasibility.gaps == {"need": pytest.approx(2050 - need_max)}
    assert "needs" in budget.infeasibility.reason.lower() and "protected" in budget.infeasibility.reason.lower()
    gym = line(budget, "rec:gym")
    assert gym.monthly_amount == 60  # scaling was enough; drop_only want kept
    dining, shopping = line(budget, "rec:dining-out"), line(budget, "rec:shopping")
    assert dining.monthly_amount / 500 == pytest.approx(shopping.monthly_amount / 400)  # proportional
    assert line(budget, "goal:house-down-payment:contribution").monthly_amount == pytest.approx(30000 / 36)
    assert line(budget, "goal:wedding:contribution").monthly_amount == pytest.approx(1000)
    assert budget.totals.savings == pytest.approx(s_req)
    assert budget.unfunded_goals == []
    assert budget.totals.unallocated == pytest.approx(0)


def test_big_goals_variants_differ(normalized_named, config):
    budgets = build_budgets(normalized_named("big_goals"), config)
    assert budgets[R50].targets != budgets[G53].targets
    assert budgets[R50].totals.want > budgets[G53].totals.want


def test_overspender_50_30_20(normalized_named, config):
    budget = fallback_restructure(normalized_named("overspender"), R50, config)
    assert budget.totals.need == pytest.approx(3080)
    assert budget.totals.want == pytest.approx(120)
    assert line(budget, "rec:streaming").monthly_amount == 60
    assert line(budget, "rec:dining-out").monthly_amount == pytest.approx(60)
    assert not budget.feasible
    assert budget.infeasibility.gaps == {"need": pytest.approx(1080)}
    assert budget.totals.savings == pytest.approx(800)
    assert line(budget, "one:disneyland-trip").monthly_amount == 2500  # fallback never touches one-time items
    cut = adjustments_for(budget, "rec:dining-out")[0]
    assert cut.decided_by is DecidedBy.fallback and "50_30_20.wants_ceiling" in cut.rule


def test_overspender_high_apr_debt_gets_extra_when_surplus(normalized_named):
    config = PlanConfig(emergency_months=0)
    norm = normalized_named("overspender").model_copy(update={"one_time": []})  # no one-time set-aside
    budget = fallback_restructure(norm, G53, config)
    extra = line(budget, "debt:credit-card:extra")
    assert extra.target_ref == "debt:credit-card" and extra.tier is Tier.savings
    rec = adjustments_for(budget, extra.id)[0]
    assert rec.rule == "surplus.high_apr_debt" and "24" in rec.rationale
    assert extra.monthly_amount == pytest.approx(4000 - 3080 - 520 - 3000 / 11)


def test_one_time_set_aside_prevents_overdrawing_cash(normalized_named, config):
    from finagent.output.budget import ONE_TIME_FUND_ID, one_time_set_aside
    from finagent.output.forecast import forecast_budget

    norm = normalized_named("overspender")
    budget = fallback_restructure(norm, R50, config)
    # Disneyland $2,500 in March 2027 (5th month) with $500 saved: (2500 - 500) / 5 = 400/mo.
    assert one_time_set_aside(budget, norm, config) == pytest.approx(400)
    rec = adjustments_for(budget, ONE_TIME_FUND_ID)[0]
    assert rec.rule == "surplus.one_time_costs" and rec.after_monthly == pytest.approx(400)
    assert forecast_budget(budget, norm, config).cash_shortfall_months == []


def test_one_time_set_aside_zero_when_savings_cover_costs(normalized_named, config):
    from finagent.output.budget import one_time_set_aside

    norm = normalized_named("complete")  # $1,500 laptop, $6,500 saved
    assert one_time_set_aside(fallback_restructure(norm, R50, config), norm, config) == 0


def test_dropping_drop_only_when_scaling_is_not_enough(normalized_named, config):
    norm = normalized_named("overspender").model_copy(update={"net_monthly_income": 3900})
    budget = fallback_restructure(norm, R50, config)  # allowance = 3900 - 3080 - 780 = 40
    assert line(budget, "rec:dining-out").monthly_amount == 0
    assert line(budget, "rec:streaming").monthly_amount == 0
    assert budget.totals.want == 0


def test_deficit_reduces_lowest_priority_savings_first(normalized_named, config):
    norm = normalized_named("big_goals").model_copy(update={"net_monthly_income": 3000})
    budget = fallback_restructure(norm, R50, config)
    # N 2050, wants cut to 350, stated savings 800 -> 200 over income
    deficit = [a for a in budget.adjustments if a.rule == "deficit.reduce_savings"]
    assert [a.line_id for a in deficit] == ["goal:wedding:contribution"]
    assert deficit[0].after_monthly == pytest.approx(300)
    assert budget.totals.unallocated == pytest.approx(0)
    assert budget.unfunded_goals == ["goal:house-down-payment", "goal:wedding"]


def _big_goals_with_house_line(normalized_named, house_monthly, income):
    norm = normalized_named("big_goals")
    wedding = next(l for l in norm.lines if l.id == "goal:wedding:contribution")
    house = wedding.model_copy(update={
        "id": "goal:house-down-payment:contribution", "name": "House down payment savings",
        "target_ref": "goal:house-down-payment", "monthly_amount": house_monthly, "original_amount": house_monthly,
    })
    return norm.model_copy(update={"lines": norm.lines + [house], "net_monthly_income": income})


def test_deficit_trims_goal_excess_above_requirement_first(normalized_named, config):
    # Stated: house 1000 (needs 833.33), wedding 500, retirement 300; N 2050, W 960 (no cut) -> 100 over income.
    norm = _big_goals_with_house_line(normalized_named, 1000, 4710)
    budget = fallback_restructure(norm, R50, config)
    deficit = [a for a in budget.adjustments if a.rule == "deficit.reduce_savings"]
    assert [a.line_id for a in deficit] == ["goal:house-down-payment:contribution"]
    assert (deficit[0].before_monthly, deficit[0].after_monthly) == pytest.approx((1000, 900))
    assert line(budget, "goal:wedding:contribution").monthly_amount == pytest.approx(500)
    assert budget.totals.unallocated == pytest.approx(0)


def test_deficit_cuts_same_line_twice_keeps_one_record(normalized_named, config):
    # 1000 over: house trimmed to 833.33 first, then wedding dropped, then house cut again.
    norm = _big_goals_with_house_line(normalized_named, 1000, 3810)
    budget = fallback_restructure(norm, R50, config)
    house = adjustments_for(budget, "goal:house-down-payment:contribution")
    assert len(house) == 1
    assert house[0].before_monthly == pytest.approx(1000)
    assert house[0].after_monthly == line(budget, "goal:house-down-payment:contribution").monthly_amount
    assert line(budget, "goal:wedding:contribution").monthly_amount == 0
    assert budget.totals.unallocated == pytest.approx(0)


def test_big_goals_50_30_20_reports_unfunded_goal(normalized_named, config):
    # 50/30/20 needs no cuts; the 1190 surplus funds the house (833.33) first, the wedding only partly.
    budget = fallback_restructure(normalized_named("big_goals"), R50, config)
    assert budget.feasible
    assert line(budget, "goal:house-down-payment:contribution").monthly_amount == pytest.approx(30000 / 36)
    assert line(budget, "goal:wedding:contribution").monthly_amount == pytest.approx(500 + 1190 - 30000 / 36)
    assert budget.unfunded_goals == ["goal:wedding"]


def test_infeasibility_reason_mentions_remaining_cuts_for_agent_budget(normalized_named, config):
    norm = normalized_named("overspender")
    budget = start_restructure(norm, R50, config)  # agent made no cuts
    finalize_budget(budget, norm, config, GeneratedBy.agent)
    assert set(budget.infeasibility.gaps) == {"need", "savings"}
    assert "$540.00" in budget.infeasibility.reason  # 660 - 120 still cuttable
    assert "no further allowed cut" not in budget.infeasibility.reason


def test_zero_income_does_not_crash(normalized_named, config):
    norm = normalized_named("complete").model_copy(update={"net_monthly_income": 0})
    for budget_id in VARIANTS:
        budget = fallback_restructure(norm, budget_id, config)
        assert budget.shares is None and not budget.feasible
        assert budget.totals.savings == 0 and budget.totals.want == 0


def test_revert_drop_then_reclassification(normalized_named, config):
    budget = start_restructure(normalized_named("overspender"), R50, config)
    reason = "A Disneyland trip is a vacation, not an essential cost, given the budget deficit."
    mark_one_time_discretionary(budget, "one:disneyland-trip", reason, config)
    apply_adjustment(budget, "one:disneyland-trip", 0, reason, config)
    revert_adjustment(budget, "one:disneyland-trip")  # undoes the drop
    trip = line(budget, "one:disneyland-trip")
    assert trip.monthly_amount == 2500 and trip.tier is Tier.scalable
    revert_adjustment(budget, "one:disneyland-trip")  # then the reclassification
    assert trip.tier is Tier.protected and not trip.discretionary and budget.adjustments == []
    with pytest.raises(AdjustmentError):
        apply_adjustment(budget, "one:disneyland-trip", 0, reason, config)


def test_deficit_when_needs_exceed_income(normalized_named, config):
    norm = normalized_named("complete").model_copy(update={"net_monthly_income": 2000})
    budget = fallback_restructure(norm, R50, config)
    assert budget.totals.savings == 0 and budget.totals.want == 0
    assert budget.totals.unallocated == pytest.approx(2000 - 2443.33, abs=0.01)
    assert set(budget.infeasibility.gaps) == {"need", "savings"}


def test_finalize_agent_budget(normalized_named, config):
    norm = normalized_named("overspender")
    budget = start_restructure(norm, R50, config)
    apply_adjustment(budget, "rec:dining-out", 60, RATIONALE, config)
    out = finalize_budget(budget, norm, config, GeneratedBy.agent, "Cut dining to protect savings.")
    assert out.generated_by is GeneratedBy.agent and out.summary_rationale == "Cut dining to protect savings."
    assert out.totals.unallocated == pytest.approx(0)


# --- invariants (TDD §14.2) ----------------------------------------------------------


@pytest.mark.parametrize("budget_id", VARIANTS, ids=lambda b: b.value)
@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_invariants(normalized_named, config, profile, budget_id):
    norm = normalized_named(profile)
    budget = fallback_restructure(norm, budget_id, config)
    tol = config.money_tolerance
    t = budget.totals
    assert t.need + t.want + t.savings + t.unallocated == pytest.approx(norm.net_monthly_income)
    if t.need <= norm.net_monthly_income:
        assert t.unallocated == pytest.approx(0, abs=tol)

    originals = {l.id: l for l in norm.lines + norm.one_time}
    for l in budget.lines + budget.one_time:
        orig = originals.get(l.id)
        if orig is not None and orig.tier is Tier.protected:
            assert l.monthly_amount == orig.monthly_amount, l.id
        if l.monthly_amount > l.original_amount + tol:
            assert l.tier is Tier.savings
            assert any(a.action is AdjustmentAction.allocate and a.decided_by is DecidedBy.policy
                       for a in adjustments_for(budget, l.id)), l.id
        baseline = orig.monthly_amount if orig is not None else 0.0
        changed = abs(l.monthly_amount - baseline) > tol
        records = [a for a in adjustments_for(budget, l.id) if a.action is not AdjustmentAction.reclassify]
        if changed:
            assert len(records) == 1, l.id
            assert records[0].rationale.strip()
        else:
            assert records == [], l.id
    assert len(Counter(l.id for l in budget.lines)) == len(budget.lines)

    g = gaps(budget, config)
    assert budget.feasible == (not g)
    if budget.feasible:
        assert budget.infeasibility is None
    else:
        assert budget.infeasibility.gaps == pytest.approx(g)
        assert budget.infeasibility.reason
        if "want" in g or "savings" in g:
            assert remaining_cuts_possible(budget, config) <= tol
        # A needs or wants gap equals the best achievable one (TDD §14.2).
        best = best_achievable(budget, config)
        assert best.need == pytest.approx(t.need)
        if "want" in g:
            assert best.want == pytest.approx(t.want)


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_fallback_is_deterministic(normalized_named, config, profile):
    norm = normalized_named(profile)
    first = build_budgets(norm, config)
    second = build_budgets(norm, config)
    assert {k: v.model_dump() for k, v in first.items()} == {k: v.model_dump() for k, v in second.items()}


def test_build_budgets_keys_and_current(normalized_named, config):
    norm = normalized_named("complete")
    budgets = build_budgets(norm, config)
    assert list(budgets) == [BudgetId.current, R50, G53]
    assert budgets[BudgetId.current] == build_current_budget(norm, config)
    assert budgets[BudgetId.current].targets is None


def test_build_budgets_custom_restructure(normalized_named, config):
    calls = []

    def restructure(norm, budget_id, cfg):
        calls.append(budget_id)
        return fallback_restructure(norm, budget_id, cfg)

    build_budgets(normalized_named("complete"), config, restructure=restructure)
    assert calls == [R50, G53]
