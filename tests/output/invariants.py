"""Invariants of every restructured budget (TDD §14.2), shared by the fallback and agent tests."""

from __future__ import annotations

from collections import Counter

import pytest

from finagent.output import policy
from finagent.output.budget import allowance_for, best_achievable, gaps, remaining_cuts_possible
from finagent.output.config import PlanConfig
from finagent.output.models import AdjustmentAction, Budget, DecidedBy, LineKind, NormalizedProfile, Tier


def check_budget_invariants(norm: NormalizedProfile, budget: Budget, config: PlanConfig) -> None:
    tol = config.money_tolerance
    t = budget.totals
    # 1. Allocations sum to income: N + W + S + one-time set-aside + U = I.
    assert t.need + t.want + t.savings + t.one_time_set_aside + t.unallocated == pytest.approx(norm.net_monthly_income)
    if t.need <= norm.net_monthly_income:
        assert t.unallocated == pytest.approx(0, abs=tol)

    reclassified = {a.line_id for a in budget.adjustments if a.action is AdjustmentAction.reclassify}
    originals = {l.id: l for l in norm.lines + norm.one_time}
    for l in budget.lines + budget.one_time:
        orig = originals.get(l.id)
        # 2. No protected line has changed (a one-time item only after a recorded reclassification).
        if orig is not None and orig.tier is Tier.protected and l.id not in reclassified:
            assert l.monthly_amount == orig.monthly_amount, l.id
        if l.id in reclassified:
            assert l.kind is LineKind.one_time and l.discretionary
        # 3. No line has increased, except savings lines raised by the surplus policy.
        if l.monthly_amount > l.original_amount + tol:
            assert l.tier is Tier.savings
            assert any(a.action is AdjustmentAction.allocate and a.decided_by is DecidedBy.policy
                       for a in budget.adjustments if a.line_id == l.id), l.id
        if orig is not None and l.tier is not Tier.savings:
            assert l.monthly_amount >= policy.min_allowed(l, config) - tol, l.id
        # 4. Every changed line has exactly one amount record, with a rationale.
        baseline = orig.monthly_amount if orig is not None else 0.0
        changed = abs(l.monthly_amount - baseline) > tol
        records = [a for a in budget.adjustments if a.line_id == l.id and a.action is not AdjustmentAction.reclassify]
        if changed:
            assert len(records) == 1, l.id
        else:
            assert records == [], l.id
    assert all(len(a.rationale.strip()) >= config.min_rationale_chars for a in budget.adjustments)
    assert len(Counter(l.id for l in budget.lines)) == len(budget.lines)

    # 5. Either the targets are met, or feasible is False with the best achievable gap.
    g = gaps(budget, config)
    assert budget.feasible == (not g)
    if budget.feasible:
        assert budget.infeasibility is None
    else:
        assert budget.infeasibility.gaps == pytest.approx(g)
        assert budget.infeasibility.reason
        if "want" in g or "savings" in g:
            assert remaining_cuts_possible(budget, config) <= tol
        best = best_achievable(budget, config)
        assert best.need == pytest.approx(t.need)
        if "want" in g:
            assert best.want == pytest.approx(t.want)
    # 6. Wants fit the allowance (or nothing allowed is left to cut), and no more was cut than needed.
    allowance = allowance_for(budget, norm, config)
    assert t.want <= allowance + tol or remaining_cuts_possible(budget, config) <= tol
    cut_wants = [l for l in budget.lines if l.bucket is not None and l.bucket.value == "want"
                 and l.monthly_amount < l.original_amount - tol]
    if t.want < allowance - tol:
        restorable = [l for l in cut_wants
                      if l.tier is Tier.scalable or l.original_amount - l.monthly_amount <= allowance - t.want + tol]
        assert not restorable, [l.id for l in restorable]
