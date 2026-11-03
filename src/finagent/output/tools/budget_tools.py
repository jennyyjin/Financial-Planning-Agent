"""Budget Restructurer tools (TDD §11.2, §6, D2).

A `BudgetSession` holds the mutable `Budget` of one restructured variant. The
tools let the agent inspect it and propose cuts; every change goes through
`budget.apply_adjustment` (tier rules, no increases, rationale length). The
tools also enforce, in code:

- `finalize` refuses to close the session while allowed cuts could still bring
  wants within the allowance (under-cutting), and while wants sit below the
  allowance by more than one of the agent's own cuts could restore
  (over-cutting: TDD §11.2 "the smallest set of cuts").
- A one-time item may be cut only while there is a shortfall to fix.
- Every number in a rationale or the summary must be a value the tools
  returned (the numeric grounding check of `advice.find_ungrounded`, applied
  to the session state before and after the change).

All numbers come from the deterministic code.

Money in tool results is dollars per month rounded to cents (one-time items:
the full amount); shares are percentages of net income rounded to 0.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from pydantic import Field

from finagent.output import budget as budget_ops
from finagent.output import goals as goal_math
from finagent.output import policy
from finagent.output.advice import RULE_CONSTANTS, find_ungrounded, numeric_leaves
from finagent.output.agents.runtime import NoArgs, ToolArgs, ToolError, ToolRegistry
from finagent.output.budget import AdjustmentError
from finagent.output.config import PlanConfig
from finagent.output.format import cents as _money
from finagent.output.models import (
    Adjustment,
    AdjustmentAction,
    Bucket,
    Budget,
    BudgetId,
    BudgetLine,
    BucketTotals,
    DecidedBy,
    GeneratedBy,
    LineKind,
    NormalizedProfile,
    Tier,
)

RECLASSIFIABLE = "reclassifiable_with_rationale"


@dataclass
class BudgetSession:
    norm: NormalizedProfile
    config: PlanConfig
    budget: Budget
    finalized: Optional[Budget] = None

    @classmethod
    def start(cls, norm: NormalizedProfile, budget_id: BudgetId, config: PlanConfig) -> BudgetSession:
        return cls(norm=norm, config=config, budget=budget_ops.start_restructure(norm, budget_id, config))

    # --- derived values (always recomputed from the budget) ---

    def allowance(self) -> float:
        return budget_ops.allowance_for(self.budget, self.norm, self.config)

    def wants_over_allowance(self) -> float:
        return max(0.0, self.budget.totals.want - self.allowance())

    def remaining_cuts(self) -> float:
        return budget_ops.remaining_cuts_possible(self.budget, self.config)


# --- Tool argument models --------------------------------------------------------


class AdjustArgs(ToolArgs):
    line_id: str = Field(description="Id of the line to change, from get_budget_state or list_adjustable.")
    new_monthly_amount: float = Field(
        description="New amount in dollars per month (for a one-time item: its new full amount). "
        "Never above the original; 0 drops the item."
    )
    rationale: str = Field(
        description="Why this change, specific to this user, naming what they give up and what it achieves."
    )


class LineArgs(ToolArgs):
    line_id: str = Field(description="Id of the line whose adjustment to undo.")


class ReclassifyArgs(ToolArgs):
    line_id: str = Field(description="Id of a one-time item (starts with 'one:').")
    rationale: str = Field(description="Why this one-time item is clearly non-essential for this user.")


class FinalizeArgs(ToolArgs):
    summary_rationale: str = Field(
        description="Two to four sentences: what was cut and why, the trade-offs, and any target that cannot be met."
    )


# --- Serialization helpers ---------------------------------------------------------


def _r(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 2)


def _pct(x: float, income: float) -> Optional[float]:
    return round(100 * x / income, 1) if income > 0 else None


def _totals(t: BucketTotals) -> dict[str, float]:
    return {"need": _r(t.need), "want": _r(t.want), "savings": _r(t.savings),
            "one_time_set_aside": _r(t.one_time_set_aside), "unallocated": _r(t.unallocated)}


def _shares(t: BucketTotals, income: float) -> dict[str, Optional[float]]:
    return {k: _pct(v, income) for k, v in t.model_dump().items()}


def _line(line: BudgetLine, config: PlanConfig) -> dict[str, Any]:
    return {
        "id": line.id, "name": line.name, "kind": line.kind.value,
        "bucket": line.bucket.value if line.bucket else None, "tier": line.tier.value,
        "amount": _r(line.monthly_amount), "original_amount": _r(line.original_amount),
        "min_allowed": _r(policy.min_allowed(line, config)),
        "category": line.category.value if line.category else None,
    }


def _one_time(line: BudgetLine) -> dict[str, Any]:
    return {
        "id": line.id, "name": line.name, "amount": _r(line.monthly_amount),
        "original_amount": _r(line.original_amount),
        "expected_date": line.expected_date.isoformat() if line.expected_date else None,
        "discretionary": line.discretionary, "tier": line.tier.value,
    }


def _adjustment(adj: Optional[Adjustment]) -> Optional[dict[str, Any]]:
    if adj is None:
        return None
    out = adj.model_dump(mode="json")
    out["before_monthly"], out["after_monthly"] = _r(adj.before_monthly), _r(adj.after_monthly)
    return out


def _outlook_values(s: BudgetSession) -> dict[str, float]:
    """What the surplus policy will need from the money left after spending (TDD §7.4 steps 0-1), unrounded."""
    b, norm = s.budget, s.norm
    available = b.income_monthly - b.totals.need - b.totals.want - b.totals.savings
    set_aside = budget_ops.one_time_set_aside(b, norm, s.config)
    goal_short = budget_ops.goal_shortfall_monthly(b, norm)
    best = budget_ops.best_achievable(b, s.config)
    best_savings = b.income_monthly - best.need - best.want - set_aside
    return {
        "available_for_savings": available,
        "one_time_set_aside_monthly": set_aside,
        "one_time_set_aside_unfunded": max(0.0, set_aside - available),
        "goal_required_monthly_total": goal_short,
        "goal_funding_short": max(0.0, goal_short - max(0.0, available - set_aside)),
        "savings_floor_short_even_after_all_cuts": max(0.0, b.targets.savings_min - best_savings),
    }


def _savings_outlook(s: BudgetSession) -> dict[str, float]:
    return {k: _r(v) for k, v in _outlook_values(s).items()}


def _has_shortfall(s: BudgetSession) -> bool:
    """True while cutting could still fix something: wants over the allowance, cash or goal money short."""
    tol = s.config.money_tolerance
    o = _outlook_values(s)
    return (s.wants_over_allowance() > tol or o["one_time_set_aside_unfunded"] > tol
            or o["goal_funding_short"] > tol or o["savings_floor_short_even_after_all_cuts"] > tol)


def _summary(s: BudgetSession) -> dict[str, Any]:
    """Compact state returned after every change."""
    b = s.budget
    return {
        "totals": _totals(b.totals),
        "shares_pct_of_income": _shares(b.totals, b.income_monthly),
        "gaps": {k: _r(v) for k, v in budget_ops.gaps(b, s.config).items()},
        "want_allowance": _r(s.allowance()),
        "wants_over_allowance": _r(s.wants_over_allowance()),
        "remaining_cuts_possible": _r(s.remaining_cuts()),
        "savings_outlook": _savings_outlook(s),
    }


def _state(session: BudgetSession) -> dict[str, Any]:
    """Full state of the budget being restructured (the get_budget_state result)."""
    b, norm, config = session.budget, session.norm, session.config
    t, income = b.targets, b.income_monthly
    best = budget_ops.best_achievable(b, config)
    return {
        "budget_id": b.id.value,
        "rule": b.description,
        "income_monthly": _r(income),
        "targets": {"need_max": _r(t.need_max), "want_max": _r(t.want_max), "savings_min": _r(t.savings_min)},
        "targets_pct_of_income": {
            "need_max": _pct(t.need_max, income), "want_max": _pct(t.want_max, income),
            "savings_min": _pct(t.savings_min, income),
        },
        **_summary(session),
        "best_achievable": {"totals": _totals(best), "shares_pct_of_income": _shares(best, income)},
        "lines": [_line(line, config) for line in b.lines],
        "one_time_items": [_one_time(line) for line in b.one_time],
        "goals": [
            {
                "id": g.id, "name": g.name, "target": _r(g.target_amount),
                "deadline": g.deadline.isoformat() if g.deadline else None,
                "required_monthly": _r(goal_math.required_monthly(g, norm.start_month)),
                "stated_monthly": _r(g.stated_monthly),
                "priority": g.priority.value if g.priority else None,
            }
            for g in norm.goals
        ],
        "debts": [
            {"id": d.id, "name": d.name, "apr": d.apr, "balance": _r(d.balance),
             "minimum_payment": _r(d.minimum_payment)}
            for d in norm.debts
        ],
        "current_savings_balance": _r(norm.current_savings_balance),
        "adjustments": [_adjustment(a) for a in b.adjustments],
        "finalized": session.finalized is not None,
    }


def _allowed(*states: dict[str, Any], budgets: tuple[Budget, ...] = (), extra: tuple[float, ...] = ()) -> list[float]:
    """Values a rationale may cite: every number in the given tool results (with the derived forms of
    advice.allowed_values: x12, x100 for fractions, ...), each line's cut size, and the rule constants."""
    vals = list(RULE_CONSTANTS) + [float(x) for x in extra]
    for state in states:
        vals += numeric_leaves(state)
    for budget in budgets:
        for line in budget.lines + budget.one_time:
            cut = line.original_amount - line.monthly_amount
            vals += [cut, cut * 12, line.monthly_amount * 12, line.original_amount * 12]
    return vals


def _check_grounded(texts: list[str], allowed: list[float], what: str) -> None:
    bad = find_ungrounded(texts, allowed)
    if bad:
        listed = ", ".join(f"{v:g}" for v in bad)
        raise ToolError(
            f"The {what} cites numbers no tool returned: {listed}. Quote values from get_budget_state or the "
            "tool results (rounding to whole dollars or percent is fine), or leave the number out."
        )


# --- Tools ---------------------------------------------------------------------------


def build_budget_tools(session: BudgetSession) -> ToolRegistry:
    """The six restructurer tools bound to one session."""
    reg = ToolRegistry()
    config = session.config

    def require_open() -> None:
        if session.finalized is not None:
            raise ToolError("This budget is already finalized; no further changes are possible.")

    @reg.tool(
        "get_budget_state",
        "Full state of the budget being restructured: income, rule targets, the wants allowance, totals and "
        "shares, gaps to targets, the best achievable split, every monthly line and one-time item, goals, "
        "debts, and the adjustments made so far. Call this first.",
        NoArgs,
    )
    def get_budget_state(_: NoArgs) -> dict[str, Any]:
        return _state(session)

    @reg.tool(
        "list_adjustable",
        "The lines you may change and the allowed amounts: scalable wants (any amount in allowed_range), "
        "drop_only wants (allowed_values: keep or 0), discretionary one-time items, and one-time items that "
        "could be reclassified with mark_one_time_discretionary. Protected lines are never listed.",
        NoArgs,
    )
    def list_adjustable(_: NoArgs) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for line in session.budget.lines + session.budget.one_time:
            base = {
                "id": line.id, "name": line.name, "kind": line.kind.value, "tier": line.tier.value,
                "amount": _r(line.monthly_amount), "original_amount": _r(line.original_amount),
            }
            if line.kind is LineKind.one_time:
                base["expected_date"] = line.expected_date.isoformat() if line.expected_date else None
            if line.tier is Tier.drop_only:
                items.append({**base, "status": "adjustable", "allowed_values": [0.0, _r(line.original_amount)]})
            elif line.tier is Tier.scalable:
                allowed = {"min": _r(policy.min_allowed(line, config)), "max": _r(line.original_amount)}
                items.append({**base, "status": "adjustable", "allowed_range": allowed})
            elif line.kind is LineKind.one_time:
                items.append({**base, "status": RECLASSIFIABLE})
        return {"items": items, "remaining_cuts_possible": _r(session.remaining_cuts())}

    @reg.tool(
        "adjust_item",
        "Reduce or drop one expense line. Rejected for protected lines, savings lines, increases, a partial cut "
        "of a drop_only line, a rationale shorter than the minimum or citing numbers no tool returned, and a "
        "cut to a one-time item when there is no shortfall to fix. Setting a line back to its original amount "
        "removes its change. Returns the updated totals, gaps and allowance.",
        AdjustArgs,
    )
    def adjust_item(args: AdjustArgs) -> dict[str, Any]:
        require_open()
        target = next((l for l in session.budget.one_time if l.id == args.line_id), None)
        if (target is not None and target.discretionary and args.new_monthly_amount < target.monthly_amount - config.money_tolerance
                and not _has_shortfall(session)):
            raise ToolError(
                f"No shortfall to fix: wants fit the allowance and the one-time set-aside and goal deadlines are "
                f"funded, so '{target.name}' stays as the person planned it."
            )
        snapshot = session.budget.model_copy(deep=True)
        try:
            record = budget_ops.apply_adjustment(
                session.budget, args.line_id, args.new_monthly_amount, args.rationale, config,
                decided_by=DecidedBy.agent,
            )
        except AdjustmentError as exc:
            raise ToolError(str(exc)) from exc
        try:
            allowed = _allowed(_state(session), _state_of(snapshot, session), budgets=(snapshot, session.budget),
                               extra=(args.new_monthly_amount, args.new_monthly_amount * 12))
            _check_grounded([args.rationale], allowed, "rationale")
        except ToolError:
            session.budget = snapshot  # the change is not applied
            raise
        out: dict[str, Any] = {"line_id": args.line_id, "adjustment": _adjustment(record)}
        if record is None:
            out["note"] = "The line is back at its original amount; no change is recorded for it."
        return {**out, **_summary(session)}

    @reg.tool("revert_item", "Undo the earlier change to a line (a second call undoes a one-time reclassification).",
              LineArgs)
    def revert_item(args: LineArgs) -> dict[str, Any]:
        require_open()
        try:
            budget_ops.revert_adjustment(session.budget, args.line_id)
        except AdjustmentError as exc:
            raise ToolError(str(exc)) from exc
        return {"line_id": args.line_id, "reverted": True, **_summary(session)}

    @reg.tool(
        "mark_one_time_discretionary",
        "Reclassify a clearly non-essential one-time item (a vacation, concert, gadget upgrade) so it can then "
        "be reduced or dropped with adjust_item. Never use it for medical, legal, tax, education, childcare, "
        "housing, moving, repair or other essential costs.",
        ReclassifyArgs,
    )
    def mark_one_time_discretionary(args: ReclassifyArgs) -> dict[str, Any]:
        require_open()
        _check_grounded([args.rationale], _allowed(_state(session), budgets=(session.budget,)), "rationale")
        try:
            record = budget_ops.mark_one_time_discretionary(
                session.budget, args.line_id, args.rationale, config, decided_by=DecidedBy.agent
            )
        except AdjustmentError as exc:
            raise ToolError(str(exc)) from exc
        line = next(l for l in session.budget.one_time if l.id == args.line_id)
        return {
            "adjustment": _adjustment(record),
            "item": {**_one_time(line), "allowed_range": {"min": 0.0, "max": _r(line.original_amount)}},
        }

    @reg.tool(
        "finalize",
        "Finish restructuring. Rejected while wants exceed the allowance and allowed cuts remain, when the "
        "cuts went further below the allowance than needed, or when the summary cites numbers no tool "
        "returned. Applies the surplus policy (savings allocation) and records whether the targets are met.",
        FinalizeArgs,
        terminal=True,
    )
    def finalize(args: FinalizeArgs) -> dict[str, Any]:
        require_open()
        tol = config.money_tolerance
        over, room = session.wants_over_allowance(), session.remaining_cuts()
        if over > tol and room > tol:
            raise ToolError(
                f"Not finished: wants are {_money(session.budget.totals.want)}/mo, {_money(over)} above the "
                f"{_money(session.allowance())}/mo wants allowance, and allowed cuts of up to {_money(room)}/mo "
                "remain (see list_adjustable). Keep cutting until wants fit the allowance, or until no allowed "
                "cut is left, then call finalize again."
            )
        over_cut = _over_cut(session)
        if over_cut:
            raise ToolError(over_cut)
        summary = args.summary_rationale.strip()
        if len(summary) < config.min_rationale_chars:
            raise ToolError(
                f"summary_rationale must be at least {config.min_rationale_chars} characters: say what was cut, "
                "why, and any target that cannot be met."
            )
        before = _state(session)
        final = budget_ops.finalize_budget(
            session.budget.model_copy(deep=True), session.norm, config, GeneratedBy.agent, summary
        )
        result = _final_result(final)
        _check_grounded([summary], _allowed(before, result, budgets=(final,)), "summary_rationale")
        session.budget = final
        session.finalized = final
        return result

    return reg


def _state_of(budget: Budget, session: BudgetSession) -> dict[str, Any]:
    """The tool state of another budget copy (e.g. the one before a change) in the same session."""
    return _state(BudgetSession(norm=session.norm, config=session.config, budget=budget))


def _over_cut(session: BudgetSession) -> Optional[str]:
    """Why the agent's cuts went further than needed, or None (TDD §11.2: the smallest set of cuts).

    Wants may end below the allowance only by less than any single agent cut
    could restore: a scalable want can be restored partially, so any gap above
    the tolerance is too much; an all-or-nothing want counts only if restoring
    it whole would still fit.
    """
    tol = session.config.money_tolerance
    b = session.budget
    under = session.allowance() - b.totals.want
    if under <= tol:
        return None
    cut_ids = {a.line_id for a in b.adjustments
               if a.decided_by is DecidedBy.agent and a.action in (AdjustmentAction.reduce, AdjustmentAction.drop)}
    restorable = [
        l for l in b.lines
        if l.id in cut_ids and l.bucket is Bucket.want
        and (l.tier is Tier.scalable or l.original_amount - l.monthly_amount <= under + tol)
    ]
    if not restorable:
        return None
    names = ", ".join(f"{l.name} ({l.id})" for l in restorable)
    return (
        f"Too many cuts: wants are {_money(b.totals.want)}/mo, {_money(under)} below the "
        f"{_money(session.allowance())}/mo allowance, so some of the cut to {names} is not needed. Restore it "
        "(adjust_item to a higher amount, or revert_item) so wants land on the allowance, then finalize."
    )


def _final_result(final: Budget) -> dict[str, Any]:
    return {
        "budget_id": final.id.value,
        "feasible": final.feasible,
        "totals": _totals(final.totals),
        "shares_pct_of_income": _shares(final.totals, final.income_monthly),
        "gaps": {k: _r(v) for k, v in (final.infeasibility.gaps if final.infeasibility else {}).items()},
        "infeasibility_reason": final.infeasibility.reason if final.infeasibility else None,
        "unfunded_goals": final.unfunded_goals,
        "adjustments_count": len(final.adjustments),
        "agent_adjustments_count": sum(a.decided_by is DecidedBy.agent for a in final.adjustments),
    }
