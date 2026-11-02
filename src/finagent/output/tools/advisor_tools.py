"""Advisor Agent tools (TDD §10, §11.3).

The advisor is read-only apart from `submit_recommendation`. Every number it
may cite comes from these tools: `get_section` and `compare_budgets` expose the
computed plan, `simulate_retirement` runs what-if projections with the same
deterministic projector (and registers its results as citable values), and
`submit_recommendation` checks, before it accepts the recommendation: the
number of major changes, that evidence refs point at specific values (not a
whole section or budget), numeric grounding, that `overall_status` and
`recommended_budget` follow the rules (the same code as the fallback), and
that no sentence calls an off-track goal met or quotes a retirement age that
no projection gives.

`simulate_retirement` registers its results as citable only when its
`extra_monthly` input is itself grounded or a round what-if amount (a
multiple of $50 up to the monthly income); otherwise only the outputs that
do not encode the input (ages, years, reachability) become citable, so the
tool cannot be used to launder an invented number.

Money in tool results is rounded to cents; shares are given as percentages
rounded to 0.1. Both are within the grounding tolerance of the exact values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import Field

from finagent.output import retirement as retirement_ops
from finagent.output.advice import (
    MAX_MAJOR_CHANGES,
    MIN_MAJOR_CHANGES,
    SECTIONS,
    PlanContext,
    allowed_values,
    eligible_budgets,
    false_claims,
    find_ungrounded,
    invalid_refs,
    overall_status,
    recommendation_texts,
)
from finagent.output.agents.runtime import NoArgs, ToolArgs, ToolError, ToolRegistry
from finagent.output.models import (
    BudgetId,
    Forecast,
    GeneratedBy,
    GoalStatus,
    MajorChange,
    OverallStatus,
    Recommendation,
)

SectionName = Literal["budgets", "forecasts", "retirement", "metrics", "profile"]


@dataclass
class AdvisorSession:
    context: PlanContext
    extra_allowed: list[float] = field(default_factory=list)
    simulated_ages: list[float] = field(default_factory=list)
    submitted: Optional[Recommendation] = None
    max_grounding_retries: int = 1   # TDD §10: one retry after an ungrounded submission, then the run fails
    grounding_failures: int = 0
    failed: Optional[str] = None     # set when the run ended without an accepted recommendation

    def allowed(self) -> list[float]:
        return allowed_values(self.context, self.extra_allowed)


def _rounded(obj: Any) -> Any:
    """Round floats for compact tool results (cents for money, 4 decimals for small fractions)."""
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, float):
        return round(obj, 4) if abs(obj) < 10 else round(obj, 2)
    if isinstance(obj, dict):
        return {k: _rounded(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rounded(v) for v in obj]
    return obj


def _pct(fraction: Optional[float]) -> Optional[float]:
    return None if fraction is None else round(fraction * 100, 1)


# --- Section views ---------------------------------------------------------------------------


def _budget_view(data: dict[str, Any], full: bool) -> dict[str, Any]:
    if full:
        return data
    keep = ("id", "name", "bucket", "kind", "tier", "monthly_amount", "original_amount", "target_ref")
    out = {k: v for k, v in data.items() if k not in ("lines", "one_time")}
    out["lines"] = [{k: line[k] for k in keep if line.get(k) is not None} for line in data["lines"]]
    out["one_time"] = [
        {"id": l["id"], "name": l["name"], "amount": l["monthly_amount"], "original_amount": l["original_amount"],
         "expected_date": l["expected_date"], "tier": l["tier"], "discretionary": l["discretionary"]}
        for l in data["one_time"]
    ]
    return out


def _forecast_view(forecast: Forecast, full: bool) -> dict[str, Any]:
    data = forecast.model_dump(mode="json")
    if full:
        return data
    months = data.pop("months")
    low = min(months, key=lambda m: m["cash_balance"])
    data["months_summary"] = {
        "count": len(months),
        "first": months[0],
        "last": months[-1],
        "min_cash": {"month": low["month"], "cash_balance": low["cash_balance"]},
        "note": "Call get_section('forecasts', full=true) for every month; refs: forecasts.<id>.months.<YYYY-MM-01>.",
    }
    return data


def _profile_view(context: PlanContext) -> dict[str, Any]:
    norm = context.norm
    return {
        "start_month": norm.start_month.isoformat(),
        "net_monthly_income": norm.net_monthly_income,
        "income": [{"name": i.name, "monthly_amount": i.monthly_amount, "is_net": i.is_net} for i in norm.income],
        "current_savings_balance": norm.current_savings_balance,
        "overall_savings_target_monthly": norm.overall_savings_target_monthly,
        "goals": [g.model_dump(mode="json", exclude={"source_ref"}) for g in norm.goals],
        "debts": [d.model_dump(mode="json", exclude={"source_ref"}) for d in norm.debts],
        "retirement": norm.retirement.model_dump(mode="json") if norm.retirement else None,
        "one_time_expenses": [
            {"id": l.id, "name": l.name, "amount": l.monthly_amount,
             "expected_date": l.expected_date.isoformat() if l.expected_date else None}
            for l in norm.one_time
        ],
        "warnings": [{"code": w.code, "message": w.message} for w in norm.warnings],
        "assumptions": {
            "real_return": context.config.real_return,
            "withdrawal_rate": context.config.withdrawal_rate,
            "emergency_months": context.config.emergency_months,
            "high_apr_threshold": context.config.high_apr_threshold,
            "count_cash_as_investable": context.config.count_cash_as_investable,
        },
    }


def section(context: PlanContext, name: str, budget_id: Optional[BudgetId] = None, full: bool = False) -> dict[str, Any]:
    """One plan section as JSON; optionally just one budget's part of it."""
    if name == "profile":
        return {"section": "profile", "data": _rounded(_profile_view(context))}
    ids = [budget_id] if budget_id is not None else list(context.budgets)
    for bid in ids:
        if bid not in context.budgets:
            raise ToolError(f"Unknown budget_id '{bid.value}'. Budgets: {', '.join(b.value for b in context.budgets)}")
    if name == "budgets":
        data: Any = {bid.value: _budget_view(context.budgets[bid].model_dump(mode="json"), full) for bid in ids}
    elif name == "forecasts":
        data = {bid.value: _forecast_view(context.forecasts[bid], full) for bid in ids}
    elif name == "retirement":
        data = {bid.value: context.retirement[bid].model_dump(mode="json") for bid in ids if bid in context.retirement}
    else:
        data = context.metrics.model_dump(mode="json")
        data["per_budget"] = {k: v for k, v in data["per_budget"].items() if k in {bid.value for bid in ids}}
    return {"section": name, "evidence_ref_prefix": f"{name}.", "data": _rounded(data)}


def comparison(context: PlanContext) -> dict[str, Any]:
    """Side-by-side key numbers of every budget."""
    rows = []
    for bid, budget in context.budgets.items():
        forecast, proj = context.forecasts[bid], context.retirement.get(bid)
        shares, t = budget.shares, budget.totals
        goals = forecast.goals
        rows.append({
            "budget_id": bid.value,
            "needs": t.need, "wants": t.want, "savings": t.savings, "unallocated": t.unallocated,
            "need_pct": _pct(shares.need if shares else None),
            "want_pct": _pct(shares.want if shares else None),
            "savings_pct": _pct(shares.savings if shares else None),
            "savings_rate_pct": _pct(context.metrics.per_budget[bid.value].savings_rate),
            "feasible": budget.feasible,
            "infeasibility_gaps": budget.infeasibility.gaps if budget.infeasibility else {},
            "adjustments": len(budget.adjustments),
            "goals_met": [v.goal_id for v in goals if v.status is GoalStatus.met],
            "goals_on_track": [v.goal_id for v in goals if v.status is GoalStatus.on_track],
            "goals_off_track": [v.goal_id for v in goals if v.status is GoalStatus.off_track],
            "cash_shortfall_months": len(forecast.cash_shortfall_months),
            "end_cash": forecast.end_state.cash_balance,
            "retirement_status": proj.status if proj else None,
            "projected_retirement_age": proj.projected_retirement_age if proj else None,
            "matches_target": proj.matches_target if proj else None,
            "required_monthly_to_hit_target": proj.required_monthly_to_hit_target if proj else None,
        })
    ret = context.norm.retirement
    return _rounded({
        "income_monthly": context.norm.net_monthly_income,
        "target_retirement_age": ret.target_retirement_age if ret else None,
        "current_age": ret.current_age if ret else None,
        "budgets": rows,
    })


# --- Tool argument models -------------------------------------------------------------------


class GetSectionArgs(ToolArgs):
    name: SectionName = Field(description="Plan section: budgets, forecasts, retirement, metrics, or profile.")
    budget_id: Optional[BudgetId] = Field(default=None, description="Only this budget's part (ignored for profile).")
    full: bool = Field(default=False, description="Include every forecast month and every line field.")


class SimulateRetirementArgs(ToolArgs):
    budget_id: BudgetId
    extra_monthly: float = Field(ge=0, description="Extra dollars invested every month after the forecast.")


class MajorChangeArgs(ToolArgs):
    title: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1, description="Dotted paths into the plan, e.g. 'forecasts.current.goals.goal:japan-trip'.")


class SubmitRecommendationArgs(ToolArgs):
    overall_status: OverallStatus
    summary: str = Field(min_length=1, description="2-4 sentence assessment.")
    recommended_budget: BudgetId
    major_changes: list[MajorChangeArgs] = Field(
        default_factory=list, description=f"{MIN_MAJOR_CHANGES}-{MAX_MAJOR_CHANGES} changes, most important first."
    )
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


# --- Registry ----------------------------------------------------------------------------------


def _too_broad(ref: str) -> bool:
    """A ref naming only a section, a budget's whole section part, or the per-budget metrics map."""
    parts = ref.split(".")
    if parts[0] not in SECTIONS or len(parts) >= 3:
        return False
    if parts[0] == "metrics":
        return len(parts) < 2 or parts[1] == "per_budget"
    return True


def build_advisor_tools(session: AdvisorSession) -> ToolRegistry:
    reg = ToolRegistry()
    ctx = session.context

    @reg.tool(
        "get_section",
        "Return one plan section as JSON: 'budgets' (lines, totals, adjustments with rationales), 'forecasts' "
        "(goal verdicts, cash shortfalls, end state; months summarized unless full=true), 'retirement' "
        "(4% rule projections), 'metrics', or 'profile' (goals, debts, retirement inputs, warnings, assumptions).",
        GetSectionArgs,
    )
    def get_section(args: GetSectionArgs) -> dict:
        return section(ctx, args.name, args.budget_id, args.full)

    @reg.tool(
        "compare_budgets",
        "Side-by-side key numbers of the current, 50_30_20 and goals_5_3 budgets: totals and shares, feasibility, "
        "goals met/off track, cash shortfall months, end cash, projected retirement age. Call this first.",
        NoArgs,
    )
    def compare_budgets(args: NoArgs) -> dict:
        return comparison(ctx)

    @reg.tool(
        "simulate_retirement",
        "What-if: project retirement for a budget if extra_monthly more dollars were invested each month. "
        "The results may then be cited, but only if extra_monthly is a value from another tool result (for "
        "example required_monthly_to_hit_target minus monthly_investment) or a round amount (a multiple of 50 "
        "up to the monthly income); otherwise only the ages and years are citable.",
        SimulateRetirementArgs,
    )
    def simulate_retirement(args: SimulateRetirementArgs) -> dict:
        bid = args.budget_id
        if bid not in ctx.budgets:
            raise ToolError(f"Unknown budget_id '{bid.value}'.")
        base = ctx.retirement.get(bid)
        proj = retirement_ops.project_retirement(
            ctx.budgets[bid], ctx.forecasts[bid], ctx.norm, ctx.config, extra_monthly=args.extra_monthly
        )
        out: dict[str, Any] = {
            "budget_id": bid.value,
            "extra_monthly": args.extra_monthly,
            "projection": proj.model_dump(mode="json"),
            "baseline_projected_retirement_age": base.projected_retirement_age if base else None,
        }
        extra = args.extra_monthly
        round_amount = extra % 50 == 0 and extra <= max(ctx.norm.net_monthly_income, 50)
        citable = round_amount or not find_ungrounded([f"{extra:.2f}"], session.allowed())
        # Outputs that do not encode the input are always citable; money outputs only with a citable input.
        new_values = [v for v in (proj.projected_retirement_age, proj.projected_retirement_year, proj.delta_years)
                      if v is not None]
        if proj.projected_retirement_age is not None and ctx.norm.retirement and ctx.norm.retirement.current_age:
            new_values.append(proj.projected_retirement_age - ctx.norm.retirement.current_age)
        if base is not None and base.projected_retirement_age is not None and proj.projected_retirement_age is not None:
            out["years_earlier"] = base.projected_retirement_age - proj.projected_retirement_age
            new_values.append(out["years_earlier"])
        if citable:
            new_values.append(extra)
            new_values += [v for v in (proj.monthly_investment, proj.monthly_to_portfolio, proj.investable_now,
                                       proj.required_monthly_to_hit_target) if v is not None]
            if proj.monthly_investment is not None:
                new_values += [proj.monthly_investment * 12, extra * 12]
        else:
            out["note"] = ("extra_monthly is not a tool-computed or round amount, so it and the money figures of "
                           "this projection are not citable; the ages and years are.")
        session.extra_allowed.extend(float(v) for v in new_values)
        if proj.projected_retirement_age is not None:
            session.simulated_ages.append(proj.projected_retirement_age)
        return _rounded(out)

    @reg.tool(
        "submit_recommendation",
        "Submit the final recommendation; this ends the session. Every evidence_ref must resolve to a plan path "
        "and every number in the text must come from a tool result.",
        SubmitRecommendationArgs,
        terminal=True,
    )
    def submit_recommendation(args: SubmitRecommendationArgs) -> dict:
        n = len(args.major_changes)
        if not MIN_MAJOR_CHANGES <= n <= MAX_MAJOR_CHANGES:
            raise ToolError(
                f"Give {MIN_MAJOR_CHANGES} to {MAX_MAJOR_CHANGES} major changes (you gave {n}), most important "
                "first, each with evidence_refs."
            )
        data = ctx.data()
        refs = [ref for change in args.major_changes for ref in change.evidence_refs]
        bad = invalid_refs(data, refs)
        if bad:
            raise ToolError(
                "These evidence_refs do not resolve to plan paths: " + ", ".join(bad)
                + ". Use dotted paths such as 'forecasts.current.goals.goal:<slug>', "
                "'budgets.50_30_20.adjustments.<line_id>' or 'retirement.goals_5_3.projected_retirement_age'."
            )
        vague = [ref for ref in refs if _too_broad(ref)]
        if vague:
            raise ToolError(
                "These evidence_refs point at a whole section or budget, not at the values a change relies on: "
                + ", ".join(vague) + ". Point at a field or element, e.g. 'budgets.50_30_20.totals.want' or "
                "'metrics.emergency_fund_months'."
            )
        expected_status = overall_status(ctx)
        if args.overall_status is not expected_status:
            raise ToolError(
                f"overall_status must be '{expected_status.value}' under the rules (at_risk: the current budget runs "
                "out of cash or retirement is unreachable; needs_attention: an off-track goal, a missed retirement "
                "age, high-interest debt or a thin emergency fund; else on_track)."
            )
        eligible = eligible_budgets(ctx)
        if args.recommended_budget not in eligible:
            raise ToolError(
                f"recommended_budget '{args.recommended_budget.value}' is not supported by compare_budgets: under the "
                "rule (fewest off-track goals, then no negative-cash months, then earliest retirement; current only "
                "if it is at least as good on all of these) recommend one of: " + ", ".join(b.value for b in eligible) + "."
            )
        rec = Recommendation(
            overall_status=args.overall_status,
            summary=args.summary,
            recommended_budget=args.recommended_budget,
            major_changes=[MajorChange(**change.model_dump()) for change in args.major_changes],
            strengths=args.strengths,
            risks=args.risks,
            generated_by=GeneratedBy.agent,
        )
        ungrounded = find_ungrounded(recommendation_texts(rec), session.allowed())
        if ungrounded:
            session.grounding_failures += 1
            listed = ", ".join(f"{v:g}" for v in ungrounded)
            if session.grounding_failures > session.max_grounding_retries:
                session.failed = f"ungrounded numbers after {session.grounding_failures} attempts: {listed}"
                return {"accepted": False, "error": f"Recommendation rejected: {session.failed}."}
            raise ToolError(
                f"These numbers do not match any tool-computed value: {listed}. Copy numbers from tool results "
                "(rounding to whole dollars or percent is fine), call simulate_retirement for what-if numbers, "
                "or remove them. Do not compute new numbers yourself."
            )
        claims = false_claims(ctx, rec, session.simulated_ages)
        if claims:
            raise ToolError(
                "These sentences contradict the plan: " + "; ".join(claims)
                + ". Describe each goal and the retirement age as the forecasts and projections report them."
            )
        session.submitted = rec
        return {"accepted": True, "recommendation": rec.model_dump(mode="json")}

    return reg
