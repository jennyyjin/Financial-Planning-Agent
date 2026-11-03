"""PlanOutput -> Markdown report (TDD §4, D6).

A pure function of the plan: every number shown comes from the PlanOutput
(nothing is recomputed beyond simple formatting), and the agent trace is
never shown. Money is whole dollars ($1,234) and shares whole percents (45%);
the JSON keeps full precision.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

from finagent.output.format import BUDGET_TITLES, dollars, percent
from finagent.output.models import (
    Adjustment,
    AdjustmentAction,
    Budget,
    BudgetId,
    Forecast,
    GoalStatus,
    GoalVerdict,
    LineKind,
    PlanOutput,
    RetirementProjection,
)

ORDER: tuple[BudgetId, ...] = (BudgetId.current, BudgetId.rule_50_30_20, BudgetId.goals_5_3)

STATUS_LABELS = {"on_track": "On track", "needs_attention": "Needs attention", "at_risk": "At risk"}
GOAL_LABELS = {
    GoalStatus.met: "Met",
    GoalStatus.on_track: "On track",
    GoalStatus.off_track: "Off track",
    GoalStatus.no_deadline: "No deadline",
}
NUMBER_SOURCES = {
    "desired_annual_spending": "your desired retirement spending",
    "target": "your retirement savings target",
    "budget_spending": "this budget's spending (needs + wants)",
}
ACTION_LABELS = {
    AdjustmentAction.reduce: "reduced",
    AdjustmentAction.drop: "dropped",
    AdjustmentAction.reclassify: "marked discretionary",
    AdjustmentAction.allocate: "allocated",
}


# --- formatting helpers -------------------------------------------------------------


money = dollars
pct = percent


def month(d: Optional[date]) -> str:
    return "n/a" if d is None else f"{d:%b %Y}"


def day(d: Optional[date]) -> str:
    return "n/a" if d is None else d.isoformat()


def age(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.1f}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: Iterable[list[str]]) -> list[str]:
    out = ["| " + " | ".join(_cell(h) for h in header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out


def _title(budget_id: BudgetId) -> str:
    return f"{BUDGET_TITLES[budget_id]} budget"


def _ids(plan: PlanOutput) -> list[BudgetId]:
    return [bid for bid in ORDER if bid in plan.budgets]


def _line_name(budget: Budget, line_id: str) -> str:
    for line in budget.lines + budget.one_time:
        if line.id == line_id:
            return line.name
    return line_id


def _goal_names(plan: PlanOutput) -> dict[str, str]:
    return {v.goal_id: v.name for f in plan.forecasts.values() for v in f.goals}


_MIN_SUFFIX = " (minimum payment)"


def _debt_names(plan: PlanOutput) -> dict[str, str]:
    """Debt id -> name, read off the debt-minimum lines (the plan has no separate debt list)."""
    names: dict[str, str] = {}
    for budget in plan.budgets.values():
        for line in budget.lines:
            if line.kind is LineKind.debt_min and line.target_ref:
                name = line.name[: -len(_MIN_SUFFIX)] if line.name.endswith(_MIN_SUFFIX) else line.name
                names.setdefault(line.target_ref, name)
    return names


# --- sections -----------------------------------------------------------------------


def _header(plan: PlanOutput) -> list[str]:
    m = plan.meta
    engine = f"model `{m.model}`" if m.model else "deterministic (no LLM)"
    return [
        "# Financial plan",
        "",
        f"User `{m.user_id}` · profile v{m.profile_version} · generated {m.generated_at.isoformat(timespec='minutes')} "
        f"· {engine} · forecast from {month(m.start_month)}",
        "",
        "_Educational guidance computed from the profile you provided; not personalized investment, tax or legal advice._",
    ]


def _recommendation(plan: PlanOutput) -> list[str]:
    rec = plan.recommendation
    out = [
        "## Overall recommendation",
        "",
        f"**Status:** {STATUS_LABELS[rec.overall_status.value]} · **Recommended budget:** "
        f"{_title(rec.recommended_budget)} · _written by: {rec.generated_by.value}_",
        "",
        rec.summary,
        "",
        "### Major changes",
        "",
    ]
    if not rec.major_changes:
        out.append("None.")
    for i, change in enumerate(rec.major_changes, 1):
        out.append(f"{i}. **{change.title}**  ")
        out.append(f"   {change.detail}  ")
        out.append(f"   _Impact:_ {change.impact}")
    for heading, items in (("Strengths", rec.strengths), ("Risks", rec.risks)):
        out += ["", f"### {heading}", ""]
        out += [f"- {item}" for item in items] or ["None noted."]
    return out


def _share_cell(amount: float, share: Optional[float]) -> str:
    return f"{money(amount)} ({pct(share)})"


def _comparison(plan: PlanOutput) -> list[str]:
    ids = _ids(plan)
    budgets = [plan.budgets[bid] for bid in ids]

    def row(label: str, cells: list[str]) -> list[str]:
        return [label, *cells]

    def bucket(name: str) -> list[str]:
        return [_share_cell(getattr(b.totals, name), getattr(b.shares, name) if b.shares else None) for b in budgets]

    def target(attr: str, prefix: str) -> list[str]:
        return [f"{prefix} {money(getattr(b.targets, attr))}" if b.targets else "—" for b in budgets]

    rows = [
        row("Income", [money(b.income_monthly) for b in budgets]),
        row("Needs", bucket("need")),
        row("Wants", bucket("want")),
        row("Savings", bucket("savings")),
        *([row("One-time set-aside", bucket("one_time_set_aside"))]
          if any(b.totals.one_time_set_aside > 0.005 for b in budgets) else []),
        row("Unallocated", bucket("unallocated")),
        row("Needs target", target("need_max", "≤")),
        row("Wants target", target("want_max", "≤")),
        row("Savings target", target("savings_min", "≥")),
        row("Meets targets", ["—" if b.targets is None else ("yes" if b.feasible else "no") for b in budgets]),
        row("Made by", [b.generated_by.value for b in budgets]),
    ]
    return [
        "## Budget comparison",
        "",
        "Monthly amounts; percentages are shares of take-home income. Cash set aside for upcoming one-time "
        "costs is planned spending, so it is not counted as savings.",
        "",
        *_table(["", *[BUDGET_TITLES[bid] for bid in ids]], rows),
    ]


def _change_line(budget: Budget, adj: Adjustment) -> str:
    name = _line_name(budget, adj.line_id)
    if adj.action is AdjustmentAction.reclassify:
        amounts = f"{money(adj.before_monthly)} one-time"
    else:
        amounts = f"{money(adj.before_monthly)} → {money(adj.after_monthly)}/mo"
    return f"- **{name}** ({ACTION_LABELS[adj.action]}, {amounts}; by {adj.decided_by.value}, `{adj.rule}`): {adj.rationale}"


def _budget_changes(plan: PlanOutput) -> list[str]:
    out = ["## Budget changes"]
    goal_names = _goal_names(plan)
    for bid in _ids(plan):
        budget = plan.budgets[bid]
        if budget.targets is None:
            continue
        out += ["", f"### {_title(bid)}", "", budget.description]
        if budget.summary_rationale:
            out += ["", f"_Summary:_ {budget.summary_rationale}"]
        out.append("")
        cuts = [a for a in budget.adjustments if a.action is not AdjustmentAction.allocate]
        allocations = [a for a in budget.adjustments if a.action is AdjustmentAction.allocate]
        out.append("**Spending changes**")
        out.append("")
        out += [_change_line(budget, a) for a in cuts] or ["- None: spending stays as you stated it."]
        out += ["", "**Savings allocations**", ""]
        out += [_change_line(budget, a) for a in allocations] or ["- None."]
        out += ["", "**Result**", ""]
        if budget.feasible:
            out.append("- Targets: all met.")
        else:
            reason = budget.infeasibility.reason if budget.infeasibility else "Targets not met."
            gaps = ", ".join(f"{k} {money(v)}/mo" for k, v in (budget.infeasibility.gaps if budget.infeasibility else {}).items())
            out.append(f"- Targets not met: {reason}" + (f" Gaps: {gaps}." if gaps else ""))
        if budget.headroom:
            room = ", ".join(f"{k} {money(v)}/mo" for k, v in budget.headroom.items())
            out.append(f"- Headroom (unused allowance, not a suggestion to spend more): {room}.")
        if budget.unfunded_goals:
            names = ", ".join(goal_names.get(g, g) for g in budget.unfunded_goals)
            out.append(f"- Unfunded goals: {names}.")
    return out


def _one_time_text(budget: Budget, forecast: Forecast) -> str:
    months = {r.month for r in forecast.months}
    first, last = (min(months), max(months)) if months else (None, None)
    items = []
    for line in sorted(budget.one_time, key=lambda l: (l.expected_date or date.min, l.id)):
        when = month(line.expected_date)
        in_window = line.expected_date is not None and first is not None and first <= line.expected_date.replace(day=1) <= last
        if line.monthly_amount <= 0 < line.original_amount:
            items.append(f"{line.name} {money(line.original_amount)} in {when} (dropped)")
        elif in_window:
            items.append(f"{line.name} {money(line.monthly_amount)} in {when}")
        else:
            items.append(f"{line.name} {money(line.monthly_amount)} in {when} (outside the window)")
    return "; ".join(items) if items else "none"


def _verdict_row(v: GoalVerdict) -> list[str]:
    if v.status is GoalStatus.off_track and v.required_monthly is None:
        note = f"short {money(v.shortfall)}; the deadline has passed"
    elif v.status is GoalStatus.off_track:
        note = f"short {money(v.shortfall)}; needs {money(v.required_monthly)}/mo"
    elif v.status is GoalStatus.no_deadline:
        note = f"projected completion {month(v.projected_completion_date)}"
    else:
        note = f"reached {month(v.projected_completion_date)}" if v.projected_completion_date else ""
    return [
        v.name, GOAL_LABELS[v.status], money(v.target_amount) if v.target_amount is not None else "n/a",
        day(v.deadline) if v.deadline else "none", money(v.balance_at_deadline), note,
    ]


def _forecasts(plan: PlanOutput) -> list[str]:
    out = ["## 12-month forecast"]
    for bid in _ids(plan):
        forecast = plan.forecasts.get(bid)
        if forecast is None:
            continue
        rows = forecast.months
        out += ["", f"### {_title(bid)}", ""]
        if rows:
            start = rows[0].cash_balance - rows[0].net_cash_flow
            low = min(rows, key=lambda r: r.cash_balance)
            out.append(
                f"- Cash: {money(start)} at the start → {money(rows[-1].cash_balance)} at the end of {month(rows[-1].month)}"
                f" ({len(rows)} months)"
            )
            out.append(f"- Lowest cash: {money(low.cash_balance)} in {month(low.month)}")
        shortfalls = ", ".join(month(m) for m in forecast.cash_shortfall_months)
        out.append(f"- Months with negative cash: {len(forecast.cash_shortfall_months)}"
                   + (f" ({shortfalls})" if shortfalls else ""))
        out.append(f"- One-time costs: {_one_time_text(plan.budgets[bid], forecast)}")
        debt_names = _debt_names(plan)
        debts = ", ".join(f"{debt_names.get(k, k)} {money(v)}" for k, v in forecast.end_state.debt_balances.items())
        if debts:
            out.append(f"- Debt balances at the end: {debts}")
        out.append(f"- Retirement balance at the end: {money(forecast.end_state.retirement_balance)}")
        out.append("")
        if forecast.goals:
            out += _table(["Goal", "Verdict", "Target", "Deadline", "At deadline", "Notes"],
                          [_verdict_row(v) for v in forecast.goals])
        else:
            out.append("No saving goals.")
    return out


def _retirement_lines(proj: RetirementProjection, multiple: float) -> list[str]:
    if proj.status != "ok":
        return [f"- Not projected: insufficient data (missing: {', '.join(proj.missing) or 'unknown'})."]
    source = NUMBER_SOURCES.get(proj.number_source or "", proj.number_source or "unknown")
    out = [
        f"- Retirement number: {money(proj.retirement_number)} ({multiple:g} × {money(proj.implied_annual_spending)}/yr, "
        f"from {source})",
        f"- Investable now (end of forecast): {money(proj.investable_now)}; adding {money(proj.monthly_investment)}/mo"
        + _split(proj),
    ]
    if proj.reachable and proj.projected_retirement_age is not None:
        out.append(f"- Projected retirement: age {age(proj.projected_retirement_age)} ({proj.projected_retirement_year})")
    else:
        out.append("- Projected retirement: unreachable before the maximum age")
    if proj.target_retirement_age is not None:
        match = "yes" if proj.matches_target else "no"
        delta = ""
        if proj.delta_years is not None:
            delta = f", {abs(proj.delta_years):.1f} years {'early' if proj.delta_years < 0 else 'late'}"
        out.append(f"- Target age: {proj.target_retirement_age}; on target: {match}{delta}")
        req = proj.required_monthly_to_hit_target
        extra = (req or 0.0) - (proj.monthly_investment or 0.0)
        note = f" (today's amount plus {money(extra)}/mo invested)" if req is not None and extra > 0.5 else ""
        out.append(f"- Monthly amount needed to retire at {proj.target_retirement_age}: {money(req)}{note}")
    else:
        out.append("- Target age: not given")
    return out


def _split(proj: RetirementProjection) -> str:
    cash = proj.monthly_to_cash or 0.0
    if cash <= 0.5:
        return ""
    return f" ({money(proj.monthly_to_portfolio)} invested, {money(cash)} kept as cash)"


def _retirement(plan: PlanOutput) -> list[str]:
    out = ["## Retirement (4% rule)"]
    for bid in _ids(plan):
        proj = plan.retirement.get(bid)
        if proj is None:
            continue
        out += ["", f"### {_title(bid)}", "", *_retirement_lines(proj, plan.meta.config.retirement_multiple)]
    return out


def _metrics(plan: PlanOutput) -> list[str]:
    m = plan.metrics
    out = [
        "## Key metrics",
        "",
        f"- Net monthly income: {money(m.net_monthly_income)}",
        f"- Emergency fund: {'n/a' if m.emergency_fund_months is None else f'{m.emergency_fund_months:.1f}'} months of needs in cash",
        f"- Total debt: {money(m.debt_total)}; debt minimums are {pct(m.debt_to_income)} of income",
        f"- High-APR debts: {', '.join(_debt_names(plan).get(d, d) for d in m.high_apr_debts) or 'none'}",
    ]
    return out


def _warnings(plan: PlanOutput) -> list[str]:
    out = ["## Warnings", ""]
    out += [f"- `{w.code}`: {w.message}" + (f" ({w.ref})" if w.ref else "") for w in plan.meta.warnings] or ["None."]
    return out


def _assumptions(plan: PlanOutput) -> list[str]:
    c = plan.meta.config
    return [
        "## Assumptions",
        "",
        "All amounts are in today's dollars; returns are real (after inflation).",
        "",
        f"- Forecast window: {c.horizon_months} months from {month(plan.meta.start_month)}",
        f"- Retirement portfolio real return: {c.real_return:.1%}; cash real return: {c.cash_real_return:.1%}",
        f"- Withdrawal rate: {c.withdrawal_rate:.1%} (retirement number = {c.retirement_multiple:g} × annual spending)",
        f"- Cash counts as investable for retirement: {'yes' if c.count_cash_as_investable else 'no'}; "
        f"freed payments redirected to retirement: {'yes' if c.redirect_freed_payments_to_retirement else 'no'}",
        f"- Emergency fund: {c.emergency_months:g} months of needs, built over {c.emergency_build_months} months",
        f"- High-APR threshold: {c.high_apr_threshold:g}% APR",
        f"- Needs may be trimmed by at most {c.need_trim_cap:.0%}; projections stop at age {c.max_age}",
    ]


def render_text(plan: PlanOutput) -> str:
    """The plan as a Markdown report (the agent trace is excluded)."""
    sections = [
        _header(plan), _recommendation(plan), _comparison(plan), _budget_changes(plan),
        _forecasts(plan), _retirement(plan), _metrics(plan), _warnings(plan), _assumptions(plan),
    ]
    return "\n\n".join("\n".join(s) for s in sections) + "\n"
