"""PlanOutput -> the view model the dashboard renders.

Every number shown on the page comes from the plan; this module only picks,
groups and formats. Amounts stay floats (the page rounds when it draws), and
labels are plain strings so the front end never has to know the plan schema.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from finagent.output.models import (
    AdjustmentAction,
    Bucket,
    Budget,
    BudgetId,
    BudgetLine,
    GoalStatus,
    GoalVerdict,
    LineKind,
    OverallStatus,
    PlanOutput,
)

ORDER = (BudgetId.current, BudgetId.rule_50_30_20, BudgetId.goals_5_3)
NAMES = {BudgetId.current: "As it stands", BudgetId.rule_50_30_20: "50/30/20", BudgetId.goals_5_3: "Goals first"}
DESCRIPTIONS = {
    BudgetId.current: "Your budget as you described it.",
    BudgetId.rule_50_30_20: "Half to needs, up to 30% on wants and at least 20% saved.",
    BudgetId.goals_5_3: "Fund your goals on time, then split the rest 5:3 between needs and wants.",
}
# Slice order matches the pie and legend: needs, wants, savings, one-time set-aside, not assigned.
SLICES = (
    ("need", "Needs"),
    ("want", "Wants"),
    ("savings", "Savings"),
    ("one_time_set_aside", "Set aside"),
    ("unallocated", "Not assigned"),
)
UNASSIGNED_TIP = ("This money wasn’t going anywhere in particular. Giving it a job means it now goes "
                  "towards your goals and savings.")
CHANGE_EPSILON = 0.5  # dollars; smaller differences are rounding, not a change


def _money(x: float) -> str:
    return f"${round(x):,}"


def _day(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def _tip(rationale: str) -> str:
    """Drop the rule id the fallback restructurer puts in front of its rationale."""
    text = re.sub(r"^Fallback rule [\w.]+: ", "", rationale)
    return text[:1].upper() + text[1:]


def _sentence_case(names: list[str]) -> list[str]:
    return names[:1] + [n[:1].lower() + n[1:] for n in names[1:]]


def _join(names: list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _goal_status(v: GoalVerdict) -> dict[str, Any]:
    if v.status is GoalStatus.no_deadline:
        return {"name": v.name, "status": "No deadline set", "ok": None}
    if v.status is GoalStatus.met and v.projected_completion_date and not v.deadline:
        return {"name": v.name, "status": f"Reached by {v.projected_completion_date:%B %Y}", "ok": True}
    when = _day(v.deadline) if v.deadline else "the deadline"
    if v.status in (GoalStatus.met, GoalStatus.on_track):
        return {"name": v.name, "status": f"On track for {when}", "ok": True}
    if not v.balance_at_deadline and v.target_amount:
        return {"name": v.name, "status": f"Not started, {_money(v.target_amount)} to go by {when}", "ok": False}
    return {"name": v.name, "status": f"{_money(v.shortfall)} short by {when}", "ok": False}


def _slices(budget: Budget) -> list[dict[str, Any]]:
    t = budget.totals.model_dump()
    return [{"key": key, "label": label, "amount": max(t[key], 0.0)} for key, label in SLICES]


def _budget_card(plan: PlanOutput, bid: BudgetId) -> dict[str, Any]:
    budget = plan.budgets[bid]
    return {
        "id": bid.value,
        "name": NAMES[bid],
        "description": DESCRIPTIONS[bid],
        "recommended": plan.recommendation.recommended_budget is bid,
        "income": budget.income_monthly,
        "slices": _slices(budget),
        "feasible": budget.feasible,
        "infeasibility": budget.infeasibility.reason if budget.infeasibility else None,
        "goals": [_goal_status(v) for v in plan.forecasts[bid].goals],
    }


def _headline(plan: PlanOutput) -> str:
    rid = plan.recommendation.recommended_budget
    goals = plan.forecasts[rid].goals
    off = plan.metrics.per_budget[rid.value].goals_off_track
    status = plan.recommendation.overall_status
    if goals and not off:
        if len(goals) == 1:
            return "Your goal is within reach."
        return ("Both of your goals" if len(goals) == 2 else "All of your goals") + " are within reach."
    if status is OverallStatus.on_track:
        return "Your plan is on track."
    if status is OverallStatus.needs_attention:
        return "A few changes will go a long way."
    return "Your plan needs a closer look."


def _savings_tile(plan: PlanOutput) -> dict[str, Any]:
    rid = plan.recommendation.recommended_budget
    rec, cur = plan.budgets[rid], plan.budgets[BudgetId.current]
    rate = plan.metrics.per_budget[rid.value].savings_rate
    cur_rate = plan.metrics.per_budget[BudgetId.current.value].savings_rate
    if rate is None or cur_rate is None:
        note = f"That’s {_money(rec.totals.savings)} a month."
    elif rid is BudgetId.current or round(rate * 100) == round(cur_rate * 100):
        note = f"Same as today: {_money(rec.totals.savings)} a month."
    else:
        word = "Up" if rate > cur_rate else "Down"
        note = (f"{word} from {round(cur_rate * 100)}% today. That’s {_money(rec.totals.savings)} a month "
                f"instead of {_money(cur.totals.savings)}.")
    name = NAMES[rid]
    return {"label": f"Saved each month, {name[0].lower() + name[1:]}", "rate": rate, "note": note}


def _goal_verdict(plan: PlanOutput, bid: BudgetId, goal_id: Optional[str]) -> Optional[GoalVerdict]:
    return next((v for v in plan.forecasts[bid].goals if v.goal_id == goal_id), None)


def _suggestions(plan: PlanOutput) -> list[dict[str, Any]]:
    """Number tiles for the recommended budget: each goal it funds more, then the total cut to wants."""
    rid = plan.recommendation.recommended_budget
    rec, cur = plan.budgets[rid], plan.budgets[BudgetId.current]
    lines = {line.id: line for line in rec.lines}
    tiles: list[dict[str, Any]] = []
    for adj in rec.adjustments:
        line = lines.get(adj.line_id)
        if adj.action is not AdjustmentAction.allocate or line is None or line.kind is not LineKind.goal_contribution:
            continue
        if adj.after_monthly - adj.before_monthly < CHANGE_EPSILON:
            continue
        v = _goal_verdict(plan, rid, line.target_ref)
        name = v.name if v else line.name
        reach = ""
        if v and v.target_amount and v.deadline:
            ok = v.status in (GoalStatus.met, GoalStatus.on_track)
            reach = (f" Gets you to {_money(v.target_amount)} by {_day(v.deadline)}." if ok
                     else f" Still {_money(v.shortfall)} short by {_day(v.deadline)}.")
        start = f"Up from {_money(adj.before_monthly)}." if adj.before_monthly >= CHANGE_EPSILON else "A new fund."
        tiles.append({"label": f"{name}, each month", "amount": adj.after_monthly, "note": start + reach})

    cut = cur.totals.want - rec.totals.want
    if cut >= CHANGE_EPSILON:
        before = {line.id: line for line in cur.lines if line.bucket is Bucket.want}
        changed = sorted(
            (line for line in rec.lines if line.bucket is Bucket.want and line.id in before
             and before[line.id].monthly_amount - line.monthly_amount >= CHANGE_EPSILON),
            key=lambda line: line.monthly_amount - before[line.id].monthly_amount,
        )
        kept = [line.name for line in rec.lines if line.bucket is Bucket.want and line.id in before
                and abs(before[line.id].monthly_amount - line.monthly_amount) < CHANGE_EPSILON]
        note = f"Mostly {_join([line.name.lower() for line in changed[:2]])}." if changed else ""
        if kept:
            note += f" {_join(_sentence_case(kept))} {'stays' if len(kept) == 1 else 'stay'} as {'it is' if len(kept) == 1 else 'they are'}."
        tiles.append({"label": "Less on wants, each month", "amount": cut, "note": note.strip()})
    return tiles


def _note(plan: PlanOutput) -> Optional[dict[str, str]]:
    """The first major change the number tiles do not already cover (debt, retirement, emergency fund…)."""
    for change in plan.recommendation.major_changes:
        if change.title.startswith(("Switch to", "Keep following", "Trim ", "Raise ")):
            continue
        if any(".goals." in ref for ref in change.evidence_refs):
            continue
        return {"title": change.title, "text": change.detail}
    return None


def _essentials(plan: PlanOutput) -> dict[str, Any]:
    cur = plan.budgets[BudgetId.current]
    needs = [line for line in cur.lines if line.bucket is Bucket.need]
    names = [line.name for line in needs if line.kind is not LineKind.debt_min]
    if any(line.kind is LineKind.debt_min for line in needs):
        names.append("minimum debt payments")
    return {
        "description": (_join(_sentence_case(names)) + ". " if names else "") + "We never change these.",
        "amounts": [plan.budgets[bid].totals.need for bid in ORDER],
    }


def _cell(plan: PlanOutput, bid: BudgetId, line_id: str, base: float, idx: int) -> dict[str, Any]:
    budget = plan.budgets[bid]
    line = next((l for l in budget.lines if l.id == line_id), None)
    amount = line.monthly_amount if line else 0.0
    adj = next((a for a in budget.adjustments if a.line_id == line_id), None)
    changed = idx > 0 and abs(amount - base) >= CHANGE_EPSILON
    return {
        "amount": amount,
        "changed": changed,
        "paused": changed and adj is not None and adj.action is AdjustmentAction.drop,
        "tip": _tip(adj.rationale) if (changed and adj) else "",
    }


def _rows(plan: PlanOutput, bucket: Optional[Bucket], kinds: Optional[set[LineKind]] = None) -> list[dict[str, Any]]:
    seen: dict[str, BudgetLine] = {}
    for bid in ORDER:
        for line in plan.budgets[bid].lines:
            if line.bucket is bucket and (kinds is None or line.kind in kinds) and line.id not in seen:
                seen[line.id] = line
    rows = []
    for line_id, line in seen.items():
        cur = next((l for l in plan.budgets[BudgetId.current].lines if l.id == line_id), None)
        base = cur.monthly_amount if cur else 0.0
        cells = [_cell(plan, bid, line_id, base, i) for i, bid in enumerate(ORDER)]
        if all(c["amount"] < CHANGE_EPSILON for c in cells):
            continue
        rows.append({"id": line_id, "name": line.name, "cells": cells})
    return rows


def _groups(plan: PlanOutput) -> list[dict[str, Any]]:
    groups = [
        {"key": "want", "name": "Wants", "rows": _rows(plan, Bucket.want)},
        {"key": "savings", "name": "Savings", "rows": _rows(plan, Bucket.savings)},
        {"key": "one_time_set_aside", "name": "Set aside for one-time costs",
         "rows": _rows(plan, None, {LineKind.one_time_fund})},
    ]
    unallocated = [max(plan.budgets[bid].totals.unallocated, 0.0) for bid in ORDER]
    if any(u >= CHANGE_EPSILON for u in unallocated):
        cells = [{"amount": u, "changed": i > 0 and abs(u - unallocated[0]) >= CHANGE_EPSILON, "paused": False,
                  "tip": ""} for i, u in enumerate(unallocated)]
        for c in cells:
            if c["changed"]:
                c["tip"] = UNASSIGNED_TIP if c["amount"] < unallocated[0] else (
                    "Part of your take-home pay that this budget leaves without a job.")
        groups.append({"key": "unallocated", "name": "Not assigned",
                       "rows": [{"id": "unallocated", "name": "Left over each month", "cells": cells}]})
    return [g for g in groups if g["rows"]]


def _upcoming(plan: PlanOutput) -> list[dict[str, Any]]:
    return [{"name": line.name, "amount": line.monthly_amount,
             "date": _day(line.expected_date) if line.expected_date else None}
            for line in plan.budgets[BudgetId.current].one_time]


def build_view(plan: PlanOutput) -> dict[str, Any]:
    """Everything the results page shows, in display order."""
    rec = plan.recommendation
    return {
        "income": plan.metrics.net_monthly_income,
        "status": rec.overall_status.value,
        "headline": _headline(plan),
        "summary": rec.summary,
        "savings": _savings_tile(plan),
        "budgets": [_budget_card(plan, bid) for bid in ORDER],
        "suggestions": _suggestions(plan),
        "note": _note(plan),
        "essentials": _essentials(plan),
        "groups": _groups(plan),
        "columns": [NAMES[bid] for bid in ORDER],
        "upcoming": _upcoming(plan),
        "strengths": rec.strengths,
        "risks": rec.risks,
        "meta": {
            "model": plan.meta.model,
            "advice_by": rec.generated_by.value,
            "start_month": f"{plan.meta.start_month:%B %Y}",
            "warnings": [w.message for w in plan.meta.warnings if w.code in ("agent_fallback", "agent_error")],
        },
    }
