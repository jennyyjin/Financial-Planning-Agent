"""Advice support: plan context, evidence refs, numeric grounding, fallback recommendation (TDD §10, D4).

The Advisor Agent only writes prose; this module holds everything around it
that must be deterministic:

- `PlanContext` bundles the computed plan sections the advisor reads.
- `resolve_ref` checks that an `evidence_ref` points at something real.
- `find_ungrounded` is the numeric grounding check: every number in the
  advice text must match a value the tools computed, up to display rounding.
- `fallback_recommendation` is the templated recommendation used when the
  agent fails or no model is configured.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Iterable, Iterator, Optional

from finagent.output.config import PlanConfig
from finagent.output.format import BUDGET_LABELS, dollars, percent
from finagent.output.models import (
    AdjustmentAction,
    Budget,
    BudgetId,
    DecidedBy,
    Forecast,
    GeneratedBy,
    GoalStatus,
    GoalVerdict,
    LineKind,
    MajorChange,
    Metrics,
    NormalizedProfile,
    OverallStatus,
    Recommendation,
    RetirementProjection,
)

RESTRUCTURED: tuple[BudgetId, ...] = (BudgetId.rule_50_30_20, BudgetId.goals_5_3)
SECTIONS: tuple[str, ...] = ("budgets", "forecasts", "retirement", "metrics")

# Numbers that the rules themselves define (50/30/20, 5:3 = 62.5%/37.5%, 4% rule = 25x, 12-month window).
RULE_CONSTANTS: tuple[float, ...] = (50, 30, 20, 5, 3, 8, 62.5, 37.5, 4, 25, 12)
MAX_MAJOR_CHANGES = 5
MIN_MAJOR_CHANGES = 3


# --- Plan context -------------------------------------------------------------------


@dataclass
class PlanContext:
    """The computed plan sections the advisor reasons over."""

    norm: NormalizedProfile
    config: PlanConfig
    budgets: dict[BudgetId, Budget]
    forecasts: dict[BudgetId, Forecast]
    retirement: dict[BudgetId, RetirementProjection]
    metrics: Metrics

    def data(self) -> dict[str, Any]:
        """The plan sections as JSON, shaped like the same keys of PlanOutput.model_dump(mode='json')."""
        return {
            "budgets": {bid.value: b.model_dump(mode="json") for bid, b in self.budgets.items()},
            "forecasts": {bid.value: f.model_dump(mode="json") for bid, f in self.forecasts.items()},
            "retirement": {bid.value: r.model_dump(mode="json") for bid, r in self.retirement.items()},
            "metrics": self.metrics.model_dump(mode="json"),
        }


# --- Evidence refs ---------------------------------------------------------------------

_ID_FIELDS = ("id", "goal_id", "line_id", "month")


def resolve_ref(data: Any, ref: str) -> Any:
    """Follow a dotted path such as 'forecasts.current.goals.goal:japan-trip'. Raises KeyError.

    Dict segments are keys; list segments are an index or the value of an
    element's 'id' / 'goal_id' / 'line_id' / 'month' field (or the element
    itself, for lists of ids). Ids never contain '.', so splitting is safe.
    """
    if not isinstance(ref, str) or not ref:
        raise KeyError(ref)
    node = data
    for seg in ref.split("."):
        if isinstance(node, dict):
            if seg not in node:
                raise KeyError(ref)
            node = node[seg]
        elif isinstance(node, list):
            if seg.isdigit() and int(seg) < len(node):
                node = node[int(seg)]
                continue
            match = next(
                (x for x in node
                 if x == seg or (isinstance(x, dict) and any(x.get(f) == seg for f in _ID_FIELDS))),
                None,
            )
            if match is None:
                raise KeyError(ref)
            node = match
        else:
            raise KeyError(ref)
    return node


def invalid_refs(data: Any, refs: Iterable[str]) -> list[str]:
    bad = []
    for ref in refs:
        try:
            resolve_ref(data, ref)
        except KeyError:
            bad.append(ref)
    return bad


# --- Numeric grounding ------------------------------------------------------------------
#
# Heuristic (documented for evaluation):
# - Numbers are read from prose with an optional "$", thousands commas, decimals, a "%" (ignored),
#   and a magnitude suffix: k/K/thousand, M/million, B/billion ("$517k" = 517000). Signs are
#   ignored; all comparisons are on absolute values.
# - Evidence-ref strings ("forecasts.current.goals...") and account names like "401(k)" are
#   removed first so their digits are not read as claims.
# - A number is grounded if it is an integer from 0 to 12 (counts, months), or if some allowed
#   value v is within max(0.51, min(p, 5% of v)), where p is the written precision: half a unit of
#   the last written digit for a magnitude suffix ("$1.2M": 50,000) or of the last nonzero digit
#   for a whole number ending in zeros ("$517,000": 500), else 0. So "$571" matches 571.43, "46"
#   matches age 45.67, "39%" matches a 0.394 share (allowed values include fractions x100),
#   "$1.2M" matches 1,152,089 and "$517,000" matches 516,812, but "$1 million" does not match
#   1,500,000 and "$2k" does not match 2,443 (each is more than 5% off).
# - A decade such as "40s" (as in "your mid-40s") is grounded if some allowed value lies in [40, 50).
# - False-accept rate (measured on the deterministic complete and overspender plans, 2,000 random
#   "$N" values per range): see TDD §10; integers 0-12 always pass by design.
# - Allowed values are every numeric leaf of the plan data and the normalized profile, plus a few
#   derived forms the prose naturally uses: fractions as percents, monthly amounts x12 (annual),
#   month counts /12 (years), differences of the same metric across budgets, cut sizes,
#   increases needed (required minus current), the emergency reserve, years until retirement,
#   list lengths, the years/months/days of every date, config values, the rule constants, and
#   numbers inside rationales that deterministic code wrote (policy/fallback, warnings).

_REF_RE = re.compile(r"\b(?:budgets|forecasts|retirement|metrics)\.[\w.:\-]*[\w:\-]")
_ACCOUNT_RE = re.compile(r"\b\d{3}\([a-zA-Z]\)")
_NUM_RE = re.compile(
    r"(?<![\w.,])(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suffix>\s?(?:thousand|million|billion)\b|[kKMB](?![A-Za-z]))?"
)
_MULTIPLIERS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


_DECADE_RE = re.compile(r"s\b")


def _parse_numbers(text: str) -> list[tuple[float, float, bool]]:
    """(value, precision, is_decade) triples; see the heuristic above for the precision."""
    text = _ACCOUNT_RE.sub(" ", _REF_RE.sub(" ", text or ""))
    out = []
    for m in _NUM_RE.finditer(text):
        raw = m.group("num").replace(",", "")
        value = float(raw)
        precision = 0.0
        suffix = (m.group("suffix") or "").strip().lower()
        decade = (not suffix and "." not in raw and len(raw) == 2 and raw.endswith("0")
                  and _DECADE_RE.match(text, m.end()) is not None)
        if suffix:
            mult = _MULTIPLIERS[suffix]
            decimals = len(raw.split(".")[1]) if "." in raw else 0
            value *= mult
            precision = 0.5 * 10 ** -decimals * mult
        elif "." not in raw and len(raw) >= 3 and raw.endswith("0") and raw.strip("0"):
            zeros = len(raw) - len(raw.rstrip("0"))
            precision = 0.5 * 10 ** zeros
        out.append((value, precision, decade))
    return out


def extract_numbers(text: str) -> list[float]:
    """Every number written in `text` (see the heuristic above)."""
    return [value for value, _, _ in _parse_numbers(text)]


def _leaves(obj: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], float]]:
    """Numeric leaves of a JSON tree; ISO date strings yield their year, month and day."""
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        if math.isfinite(obj):
            yield path, float(obj)
    elif isinstance(obj, str):
        m = _ISO_DATE_RE.match(obj)
        if m:
            for part in m.groups():
                yield path + ("<date>",), float(part)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from _leaves(value, path + (str(key),))
    elif isinstance(obj, list):
        yield path + ("<len>",), float(len(obj))
        for i, value in enumerate(obj):
            yield from _leaves(value, path + (str(i),))


_MONTHLY_KEYS = {
    "monthly_amount", "original_amount", "income_monthly", "net_monthly_income", "stated_monthly",
    "required_monthly", "current_monthly", "required_monthly_to_hit_target", "monthly_investment",
    "before_monthly", "after_monthly", "monthly_contribution", "overall_savings_target_monthly",
}


def _with_derived_forms(path: tuple[str, ...], value: float) -> list[float]:
    out = [value]
    key = path[-1] if path else ""
    if abs(value) <= 2:
        out.append(value * 100)  # fractions written as percents
    if key in _MONTHLY_KEYS or "totals" in path or "headroom" in path or "targets" in path:
        out.append(value * 12)  # monthly amounts written as annual
    if key.endswith("months"):
        out.append(value / 12)  # month counts written as years
    return out


def numeric_leaves(tree: Any) -> list[float]:
    """Every number in a JSON tree, with the derived forms prose uses (see the heuristic above)."""
    out: list[float] = []
    for path, value in _leaves(tree):
        out += _with_derived_forms(path, value)
    return out


def _deterministic_texts(context: PlanContext) -> list[str]:
    """Prose written by deterministic code, whose numbers are therefore tool values."""
    texts = [w.message for w in context.norm.warnings]
    for budget in context.budgets.values():
        texts += [a.rationale for a in budget.adjustments if a.decided_by is not DecidedBy.agent]
        if budget.infeasibility is not None:
            texts.append(budget.infeasibility.reason)
        if budget.summary_rationale and budget.generated_by is not GeneratedBy.agent:
            texts.append(budget.summary_rationale)
    return texts


def _pairwise_diffs(values: Iterable[Optional[float]]) -> list[float]:
    present = [v for v in values if v is not None]
    return [abs(a - b) for a, b in combinations(present, 2)]


def allowed_values(context: PlanContext, extra: Iterable[float] = ()) -> list[float]:
    """Every value the advisor may cite (sorted, deduplicated); see the heuristic above."""
    vals: list[float] = list(RULE_CONSTANTS)
    for tree in (context.data(), context.norm.model_dump(mode="json")):
        for path, value in _leaves(tree):
            vals += _with_derived_forms(path, value)
    for path, value in _leaves(context.config.model_dump(mode="json")):
        vals += _with_derived_forms(path, value)
    vals.append(context.config.emergency_months)

    budgets, forecasts, retirement = context.budgets, context.forecasts, context.retirement
    for field in ("need", "want", "savings", "unallocated"):
        diffs = _pairwise_diffs(getattr(b.totals, field) for b in budgets.values())
        vals += diffs + [d * 12 for d in diffs]
    for field in ("projected_retirement_age", "retirement_number", "monthly_investment",
                  "required_monthly_to_hit_target", "investable_now"):
        vals += _pairwise_diffs(getattr(r, field) for r in retirement.values())
    vals += _pairwise_diffs(f.end_state.cash_balance for f in forecasts.values())
    goal_ids = {v.goal_id for f in forecasts.values() for v in f.goals}
    for goal_id in goal_ids:
        verdicts = [v for f in forecasts.values() for v in f.goals if v.goal_id == goal_id]
        vals += _pairwise_diffs(v.shortfall for v in verdicts)
        vals += [abs(v.required_monthly - v.current_monthly) for v in verdicts if v.required_monthly is not None]
    for budget in budgets.values():
        vals.append(context.config.emergency_months * budget.totals.need)
        vals.append(max(0.0, context.config.emergency_months * budget.totals.need - context.norm.current_savings_balance))
        for adj in budget.adjustments:
            cut = abs(adj.before_monthly - adj.after_monthly)
            vals += [cut, cut * 12]
    ret = context.norm.retirement
    for proj in retirement.values():
        if proj.required_monthly_to_hit_target is not None and proj.monthly_investment is not None:
            vals.append(abs(proj.required_monthly_to_hit_target - proj.monthly_investment))
        if ret is not None and ret.current_age is not None:
            for age in (proj.projected_retirement_age, proj.target_retirement_age):
                if age is not None:
                    vals.append(age - ret.current_age)
    for text in _deterministic_texts(context):
        vals += extract_numbers(text)
    vals += [float(x) for x in extra]
    return sorted({abs(v) for v in vals if math.isfinite(v)})


def _grounded(value: float, precision: float, allowed: list[float], decade: bool = False) -> bool:
    if value == int(value) and 0 <= value <= 12:
        return True
    if decade:
        return any(value <= a < value + 10 for a in allowed)
    return any(abs(value - a) <= max(0.51, min(precision, 0.05 * a)) for a in allowed)


def find_ungrounded(texts: Iterable[str], allowed: Iterable[float]) -> list[float]:
    """Numbers in `texts` that match no allowed value (first-seen order, deduplicated)."""
    allowed = list(allowed)
    out: list[float] = []
    for text in texts:
        for value, precision, decade in _parse_numbers(text):
            if value not in out and not _grounded(value, precision, allowed, decade):
                out.append(value)
    return out


def recommendation_texts(rec: Recommendation | dict[str, Any]) -> list[str]:
    """All prose fields of a recommendation that the grounding check covers."""
    r = rec.model_dump() if isinstance(rec, Recommendation) else rec
    texts = [r.get("summary") or ""]
    for change in r.get("major_changes") or []:
        texts += [change.get("title") or "", change.get("detail") or "", change.get("impact") or ""]
    return texts + list(r.get("strengths") or []) + list(r.get("risks") or [])


# --- Fallback recommendation (D4) ----------------------------------------------------------


_usd = dollars
_pct = percent


def _age(age: float) -> str:
    return f"{age:.0f}"


def _goal_name(context: PlanContext, goal_id: str) -> str:
    return next((g.name for g in context.norm.goals if g.id == goal_id), goal_id)


def _line_name(budget: Budget, line_id: str) -> str:
    return next((l.name for l in budget.lines + budget.one_time if l.id == line_id), line_id)


def _goal_contribution(budget: Budget, goal_id: str) -> float:
    return sum(l.monthly_amount for l in budget.lines if l.kind is LineKind.goal_contribution and l.target_ref == goal_id)


def _missing_text(missing: list[str]) -> str:
    names = {"retirement": "retirement details", "retirement.current_age": "current age"}
    return ", ".join(names.get(m, m.split(".")[-1].replace("_", " ")) for m in missing)


def _off_track(forecast: Forecast) -> list[GoalVerdict]:
    return [v for v in forecast.goals if v.status is GoalStatus.off_track]


def _ok(proj: Optional[RetirementProjection]) -> bool:
    return proj is not None and proj.status == "ok"


def overall_status(context: PlanContext) -> OverallStatus:
    """at_risk: cash runs out or retirement is unreachable; needs_attention: a goal, retirement, debt or
    emergency-fund issue; else on_track. Judged on the current budget (the user's real situation)."""
    cur = BudgetId.current
    forecast, proj = context.forecasts.get(cur), context.retirement.get(cur)
    if (forecast is not None and forecast.cash_shortfall_months) or (_ok(proj) and not proj.reachable):
        return OverallStatus.at_risk
    em = context.metrics.emergency_fund_months
    if (
        (forecast is not None and _off_track(forecast))
        or (_ok(proj) and proj.matches_target is False)
        or context.metrics.high_apr_debts
        or (em is not None and em < context.config.emergency_months)
    ):
        return OverallStatus.needs_attention
    return OverallStatus.on_track


def _score(context: PlanContext, bid: BudgetId) -> tuple[int, int, float]:
    forecast, proj = context.forecasts[bid], context.retirement.get(bid)
    age = proj.projected_retirement_age if _ok(proj) and proj.reachable else None
    return (len(_off_track(forecast)), int(bool(forecast.cash_shortfall_months)), age if age is not None else math.inf)


def choose_budget(context: PlanContext) -> BudgetId:
    """Best restructured budget by (fewest off-track goals, no cash shortfall, earliest retirement), ties to
    50/30/20; the current budget wins only if it dominates (no worse on every criterion, better on one)."""
    candidates = [bid for bid in RESTRUCTURED if bid in context.budgets and bid in context.forecasts]
    if not candidates:
        return BudgetId.current
    best = min(candidates, key=lambda bid: (_score(context, bid), RESTRUCTURED.index(bid)))
    if BudgetId.current in context.forecasts:
        cur, other = _score(context, BudgetId.current), _score(context, best)
        if all(c <= o for c, o in zip(cur, other)) and any(c < o for c, o in zip(cur, other)):
            return BudgetId.current
    return best


def eligible_budgets(context: PlanContext, age_slack: float = 1.0) -> list[BudgetId]:
    """Budgets the advisor may recommend: the `choose_budget` pick, plus any other restructured budget that
    ties it on off-track goals and cash shortfalls and retires within `age_slack` years of it (a judgment
    call between near-equal options). The current budget only when it is the pick (it must dominate)."""
    best = choose_budget(context)
    if best is BudgetId.current:
        return [best]
    b_off, b_short, b_age = _score(context, best)
    out = [best]
    for bid in RESTRUCTURED:
        if bid is best or bid not in context.forecasts:
            continue
        off, short, age = _score(context, bid)
        if (off, short) == (b_off, b_short) and (age <= b_age + age_slack or age == b_age):
            out.append(bid)
    return out


# --- Claim checks (submit_recommendation) -------------------------------------------------------

_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+")
_POSITIVE_RE = re.compile(r"\b(?:on track|is met|are met|been met|fully funded|reach(?:es|ed)? (?:its|their|the) target)\b", re.I)
_NEGATIVE_RE = re.compile(r"\b(?:not|off track|short|miss\w*|behind|unfunded|fails?|cannot|can't|won't)\b", re.I)
_ALL_GOALS_RE = re.compile(r"\b(?:every|all)(?: of)?(?: your| the)?(?: saving)? goals?\b", re.I)
_RETIRE_AGE_RE = re.compile(
    r"\bretir\w*\s+(?:(?:at|by|around|near|from)\s+)?(?:about\s+|around\s+|roughly\s+)?(?:age\s+)?(\d{2})(?:\.\d)?\b", re.I)
_HEDGE_RE = re.compile(r"\b(?:target|desired|goal|aim|want|hope|would|could|if|need|needed|plan to|to retire)\b", re.I)


def _budgets_named(context: PlanContext, sentence: str) -> list[BudgetId]:
    low = sentence.lower()
    names = {BudgetId.current: ("current",), BudgetId.rule_50_30_20: ("50/30/20", "50_30_20"),
             BudgetId.goals_5_3: ("5:3", "goals_5_3", "goals +")}
    return [bid for bid, keys in names.items() if bid in context.forecasts and any(k in low for k in keys)]


def false_claims(context: PlanContext, rec: Recommendation, extra_ages: Iterable[float] = ()) -> list[str]:
    """Sentences whose goal-status or retirement-age claim contradicts the computed plan.

    Heuristic, sentence by sentence (documented in TDD §10): a sentence that calls a goal (by name, or
    "every goal") met / on track, without negation, is false if that goal is off track in the budget the
    sentence names (or, if it names none, in the recommended budget). A sentence that says "retire at N"
    must quote a projected retirement age of some budget or a simulate_retirement what-if (within one
    year), or the target age with hedging ("your target of 65", "would retire by 65").
    """
    problems: list[str] = []
    ages = [p.projected_retirement_age for p in context.retirement.values() if p.projected_retirement_age is not None]
    ages += list(extra_ages)
    targets = {p.target_retirement_age for p in context.retirement.values() if p.target_retirement_age is not None}
    goal_names = {g.id: g.name.lower() for g in context.norm.goals}
    for text in recommendation_texts(rec):
        for sentence in _SENTENCE_RE.split(text or ""):
            budgets = _budgets_named(context, sentence) or [rec.recommended_budget]
            if _POSITIVE_RE.search(sentence) and not _NEGATIVE_RE.search(sentence):
                low = sentence.lower()
                named = [gid for gid, name in goal_names.items() if name and name in low]
                if _ALL_GOALS_RE.search(sentence):
                    named = list(goal_names)
                for bid in budgets:
                    verdicts = {v.goal_id: v for v in context.forecasts[bid].goals}
                    bad = [gid for gid in named if gid in verdicts and verdicts[gid].status is GoalStatus.off_track]
                    if bad:
                        problems.append(f"'{sentence.strip()}' (off track in the {BUDGET_LABELS[bid]}: "
                                        + ", ".join(verdicts[g].name for g in bad) + ")")
                        break
            for m in _RETIRE_AGE_RE.finditer(sentence):
                age = float(m.group(1))
                if any(abs(age - a) <= 1.0 for a in ages):
                    continue
                if age in targets and _HEDGE_RE.search(sentence):
                    continue
                problems.append(f"'{sentence.strip()}' (retirement age {age:g} matches no projection)")
    return problems


def _switch_change(ctx: PlanContext, rid: BudgetId) -> MajorChange:
    budget, label = ctx.budgets[rid], BUDGET_LABELS[rid]
    t = budget.totals
    split = (f"needs {_usd(t.need)}, wants {_usd(t.want)} and savings {_usd(t.savings)} per month "
             f"out of {_usd(budget.income_monthly)} take-home pay")
    rate = ctx.metrics.per_budget[rid.value].savings_rate
    refs = [f"budgets.{rid.value}.totals", f"metrics.per_budget.{rid.value}.savings_rate"]
    if rid is BudgetId.current:
        return MajorChange(
            title="Keep following your current budget",
            detail=f"Your current budget does at least as well as the restructured options: {split}.",
            impact="Keeps every goal and the retirement outlook where they are today.",
            evidence_refs=refs,
        )
    cur = ctx.budgets[BudgetId.current]
    detail = f"The {label} sets {split}"
    detail += f", a savings rate of {_pct(rate)}." if rate is not None else "."
    if t.one_time_set_aside > 0.5:
        detail += f" It also keeps {_usd(t.one_time_set_aside)}/mo in cash for upcoming one-time costs."
    if cur.totals.unallocated > 0.5:
        detail += f" It gives a purpose to the {_usd(cur.totals.unallocated)}/mo your current budget leaves unassigned."
    impacts = []
    off_cur, off_rec = len(_off_track(ctx.forecasts[BudgetId.current])), len(_off_track(ctx.forecasts[rid]))
    if off_cur != off_rec:
        impacts.append(f"off-track goals go from {off_cur} to {off_rec}")
    short_cur, short_rec = (len(ctx.forecasts[b].cash_shortfall_months) for b in (BudgetId.current, rid))
    if short_cur != short_rec:
        impacts.append(f"months with negative cash go from {short_cur} to {short_rec}")
    p_cur, p_rec = ctx.retirement.get(BudgetId.current), ctx.retirement.get(rid)
    if _ok(p_cur) and _ok(p_rec) and p_rec.reachable and p_rec.projected_retirement_age is not None:
        before = f"about {_age(p_cur.projected_retirement_age)}" if p_cur.reachable else "out of reach"
        impacts.append(f"projected retirement age moves from {before} to about {_age(p_rec.projected_retirement_age)}")
        refs.append(f"retirement.{rid.value}.projected_retirement_age")
    impact = ("In this budget, " + "; ".join(impacts) + ".") if impacts else "Gives every dollar of income a planned use."
    refs.append(f"forecasts.{rid.value}.goals")
    return MajorChange(title=f"Switch to the {label}", detail=detail, impact=impact, evidence_refs=refs)


def _cuts_change(ctx: PlanContext, rid: BudgetId, allowed: list[float]) -> Optional[MajorChange]:
    budget = ctx.budgets[rid]
    cuts = [a for a in budget.adjustments
            if a.action in (AdjustmentAction.reduce, AdjustmentAction.drop) and a.decided_by is not DecidedBy.policy]
    if not cuts:
        return None
    cuts = sorted(cuts, key=lambda a: (-(a.before_monthly - a.after_monthly), a.line_id))[:3]
    names = [_line_name(budget, a.line_id) for a in cuts]
    detail = "; ".join(
        f"{name} from {_usd(a.before_monthly)} to {_usd(a.after_monthly)}" for a, name in zip(cuts, names)
    ) + "."
    if not find_ungrounded([cuts[0].rationale], allowed):
        detail += f" Rationale: {cuts[0].rationale}"
    cur_want, rec_want = ctx.budgets[BudgetId.current].totals.want, budget.totals.want
    if cur_want - rec_want > 0.5:
        impact = (f"Lowers monthly wants from {_usd(cur_want)} to {_usd(rec_want)}, "
                  f"freeing {_usd(cur_want - rec_want)} per month for savings.")
    else:
        adj = cuts[0]
        impact = f"Frees {_usd(adj.before_monthly - adj.after_monthly)} for savings."
    return MajorChange(
        title="Trim " + (" and ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + " and " + names[-1]),
        detail=detail,
        impact=impact,
        evidence_refs=[f"budgets.{rid.value}.adjustments.{a.line_id}" for a in cuts] + [f"budgets.{rid.value}.totals.want"],
    )


def _goal_changes(ctx: PlanContext, rid: BudgetId) -> list[MajorChange]:
    changes = []
    rec_verdicts = {v.goal_id: v for v in ctx.forecasts[rid].goals}
    for v in _off_track(ctx.forecasts[BudgetId.current])[:2]:
        name = _goal_name(ctx, v.goal_id)
        title = f"Raise {name} savings to {_usd(v.required_monthly)}/mo" if v.required_monthly else f"Close the {name} gap"
        by = f" by {v.deadline:%B %Y}" if v.deadline else ""
        contribution = _goal_contribution(ctx.budgets[BudgetId.current], v.goal_id)
        if contribution > 0.5:
            detail = (f"At {_usd(contribution)}/mo in your current budget, {name} reaches "
                      f"{_usd(v.balance_at_deadline or 0.0)} of its {_usd(v.target_amount or 0.0)} target{by}, "
                      f"{_usd(v.shortfall)} short.")
        else:
            detail = (f"Your current budget sets nothing aside for {name}, so it would be "
                      f"{_usd(v.shortfall)} short of its {_usd(v.target_amount or 0.0)} target{by}.")
        rec = rec_verdicts.get(v.goal_id)
        if rid is BudgetId.current or rec is None:
            impact = "Meeting the required contribution closes the gap by the deadline."
        elif rec.status in (GoalStatus.met, GoalStatus.on_track):
            impact = f"The {BUDGET_LABELS[rid]} funds this goal, so it is {rec.status.value.replace('_', ' ')}."
        else:
            impact = (f"Even the {BUDGET_LABELS[rid]} leaves it {_usd(rec.shortfall)} short; "
                      "consider a later deadline or a smaller target.")
        refs = [f"forecasts.current.goals.{v.goal_id}"]
        if rid is not BudgetId.current:
            refs.append(f"forecasts.{rid.value}.goals.{v.goal_id}")
        changes.append(MajorChange(title=title, detail=detail, impact=impact, evidence_refs=refs))
    return changes


def _debt_change(ctx: PlanContext, rid: BudgetId) -> Optional[MajorChange]:
    debts = [d for d in ctx.norm.debts if d.id in ctx.metrics.high_apr_debts]
    if not debts:
        return None
    debt = max(debts, key=lambda d: (d.apr or 0.0, d.balance))
    label, rv = BUDGET_LABELS[rid], rid.value
    detail = (f"{debt.name} charges {debt.apr:g}% APR on a {_usd(debt.balance)} balance, far above the "
              f"{_pct(ctx.config.real_return)} real return assumed for investments.")
    refs = [f"metrics.high_apr_debts.{debt.id}"]
    extra = _debt_extra(ctx.budgets[rid], debt.id)
    end = ctx.forecasts[rid].end_state.debt_balances.get(debt.id)
    if extra is not None:
        detail += f" The {label} adds {_usd(extra.monthly_amount)}/mo on top of the minimum."
        refs.append(f"budgets.{rv}.lines.{extra.id}")
        title, impact = f"Pay down {debt.name} first", "Paying it down first is a guaranteed saving on interest."
    else:
        # The budget itself has no room to prepay it: say so instead of advising what it does not do.
        others = [b for b in RESTRUCTURED if b is not rid and b in ctx.budgets and _debt_extra(ctx.budgets[b], debt.id)]
        if others:
            other = _debt_extra(ctx.budgets[others[0]], debt.id)
            detail += (f" The {label} has no money left for extra payments, so only the minimum is paid; the "
                       f"{BUDGET_LABELS[others[0]]} adds {_usd(other.monthly_amount)}/mo.")
            refs.append(f"budgets.{others[0].value}.lines.{other.id}")
        else:
            detail += (" None of the budgets has money left for extra payments after essentials, upcoming one-time "
                       "costs and the savings floor, so only the minimum is paid.")
        title = f"Pay down {debt.name} as soon as there is room"
        impact = "Paying more than the minimum, once you can, is a guaranteed saving on interest."
    if end is not None:
        impact = f"Projected balance after 12 months: {_usd(end)} in the {label}. " + impact
        refs.append(f"forecasts.{rv}.end_state.debt_balances.{debt.id}")
    return MajorChange(title=title, detail=detail, impact=impact, evidence_refs=refs)


def _debt_extra(budget: Budget, debt_id: str):
    return next((l for l in budget.lines if l.kind is LineKind.debt_extra and l.target_ref == debt_id
                 and l.monthly_amount > 0.5), None)


def _retirement_change(ctx: PlanContext, rid: BudgetId) -> Optional[MajorChange]:
    proj = ctx.retirement.get(rid)
    if not _ok(proj) or proj.target_retirement_age is None or proj.matches_target:
        return None
    label, target = BUDGET_LABELS[rid], proj.target_retirement_age
    refs = [f"retirement.{rid.value}.projected_retirement_age", f"retirement.{rid.value}.retirement_number"]
    if proj.reachable and proj.projected_retirement_age is not None:
        where = f"around age {_age(proj.projected_retirement_age)}, after your target of {target}"
    else:
        where = f"not before age {ctx.config.max_age}"
    detail = (f"In the {label} you would add about {_usd(proj.monthly_investment or 0.0)}/mo to retirement "
              f"savings{_split_text(ctx, proj)} and reach the retirement number of "
              f"{_usd(proj.retirement_number or 0.0)} {where}.")
    req = proj.required_monthly_to_hit_target
    if req is None:
        return MajorChange(
            title="Revisit your retirement timeline", detail=detail,
            impact="A later target age or lower retirement spending would make the goal reachable.",
            evidence_refs=refs,
        )
    refs.append(f"retirement.{rid.value}.required_monthly_to_hit_target")
    gap = req - (proj.monthly_investment or 0.0)
    # required_monthly_to_hit_target is solved with this same projector (the gap invested in the portfolio),
    # so this claim holds by construction; tests re-run the projector to check it.
    impact = (f"Saving about {_usd(req)}/mo" + (f", with the {_usd(gap)} more than today invested," if gap > 0.5 else "")
              + f" would reach the retirement number by age {target}.")
    title = (f"Invest about {_usd(gap)}/mo more to retire by {target}" if gap > 0.5
             else f"Keep saving about {_usd(req)}/mo to retire by {target}")
    return MajorChange(title=title, detail=detail, impact=impact, evidence_refs=refs)


def _split_text(ctx: PlanContext, proj: RetirementProjection) -> str:
    """' ($273 invested at 5%, $527 kept as cash at 0%)' when part of the monthly amount stays in cash."""
    cash, invested = proj.monthly_to_cash or 0.0, proj.monthly_to_portfolio or 0.0
    if cash <= 0.5 or not ctx.config.count_cash_as_investable:
        return ""
    return (f" ({_usd(invested)} invested at a {_pct(ctx.config.real_return)} real return, {_usd(cash)} kept as cash "
            f"at {_pct(ctx.config.cash_real_return)})")


def _emergency_change(ctx: PlanContext, rid: BudgetId) -> Optional[MajorChange]:
    em, months = ctx.metrics.emergency_fund_months, ctx.config.emergency_months
    if em is None or em >= months:
        return None
    need = ctx.budgets[BudgetId.current].totals.need
    detail = (f"Your {_usd(ctx.norm.current_savings_balance)} of savings covers {em:.1f} months of needs; "
              f"the guideline is {months:g} months, or {_usd(months * need)}.")
    refs = ["metrics.emergency_fund_months"]
    budget = ctx.budgets[rid]
    funded = [a for a in budget.adjustments if a.rule == "surplus.emergency_fund"]
    amount = sum(a.after_monthly - a.before_monthly for a in funded)
    if amount > 0.5:
        detail += f" The {BUDGET_LABELS[rid]} sets aside {_usd(amount)}/mo toward it."
        refs += [f"budgets.{rid.value}.lines.{a.line_id}" for a in funded]
    else:
        detail += f" The {BUDGET_LABELS[rid]} has no money left to set aside for it."
    return MajorChange(
        title=f"Build a {months:g}-month emergency fund", detail=detail,
        impact="A cash cushion keeps an unexpected bill from turning into high-interest debt.",
        evidence_refs=refs,
    )


def _filler_changes(ctx: PlanContext, rid: BudgetId) -> list[MajorChange]:
    rv, label = rid.value, BUDGET_LABELS[rid]
    out = []
    proj = ctx.retirement.get(rid)
    if proj is not None and proj.status == "insufficient_data":
        out.append(MajorChange(
            title="Add your retirement details",
            detail=f"Retirement could not be projected because your {_missing_text(proj.missing)} are missing.",
            impact="With your age and retirement savings, the plan can check whether you are on track to retire.",
            evidence_refs=[f"retirement.{rv}.missing"],
        ))
    elif _ok(proj) and proj.matches_target and proj.projected_retirement_age is not None:
        out.append(MajorChange(
            title="Stay the course on retirement investing",
            detail=(f"The {label} reaches the retirement number of {_usd(proj.retirement_number or 0.0)} around age "
                    f"{_age(proj.projected_retirement_age)}, at or before your target of {proj.target_retirement_age}."),
            impact="Keeping contributions steady protects that timeline.",
            evidence_refs=[f"retirement.{rv}.projected_retirement_age"],
        ))
    rate = ctx.metrics.per_budget[rv].savings_rate
    if rate is not None:
        relation = "at or above" if rate >= 0.2 else "below"
        out.append(MajorChange(
            title="Protect your savings rate",
            detail=f"The {label} saves {_pct(rate)} of take-home pay, {relation} the 20% guideline.",
            impact="Automating transfers on payday makes the savings happen before spending does.",
            evidence_refs=[f"metrics.per_budget.{rv}.savings_rate"],
        ))
    out.append(MajorChange(
        title="Review the plan every few months",
        detail=f"The {label} is projected to end the next 12 months with {_usd(ctx.forecasts[rid].end_state.cash_balance)} in cash.",
        impact="Checking in regularly catches changes in income or costs early.",
        evidence_refs=[f"forecasts.{rv}.end_state.cash_balance"],
    ))
    return out


def _summary(ctx: PlanContext, status: OverallStatus, rid: BudgetId) -> str:
    opening = {
        OverallStatus.on_track: "Overall, your finances look on track.",
        OverallStatus.needs_attention: "Overall, your finances are in reasonable shape but need attention in a few areas.",
        OverallStatus.at_risk: "Overall, your plan is at risk and needs changes soon.",
    }[status]
    cur = ctx.forecasts[BudgetId.current]
    off, total = len(_off_track(cur)), len(cur.goals)
    facts = []
    if total == 1:
        facts.append(_single_goal_fact(cur.goals[0]))
    elif total:
        facts.append(f"{off} of your {total} saving goals {'is' if off == 1 else 'are'} off track")
    if cur.cash_shortfall_months:
        facts.append(f"cash runs negative in {len(cur.cash_shortfall_months)} of the next 12 months")
    cur_metrics = ctx.metrics.per_budget[BudgetId.current.value]
    if cur_metrics.savings_target_met is False and ctx.metrics.savings_target_monthly is not None:
        facts.append(f"you save less than your target of {_usd(ctx.metrics.savings_target_monthly)}/mo")
    if not facts:
        facts.append(_savings_fact(cur_metrics) or "no saving goals are at risk")
    joined = facts[0] if len(facts) == 1 else ", ".join(facts[:-1]) + " and " + facts[-1]
    sentences = [opening, "In your current budget, " + joined + "."]
    clauses = []
    if rid is BudgetId.current:
        rec, lead = "Your current budget performs best of the three options, so we suggest keeping it", "; it "
    else:
        rec, lead = f"We suggest the {BUDGET_LABELS[rid]}", ", which "
        if any(v.deadline is not None for v in ctx.forecasts[rid].goals):
            rec_off = len(_off_track(ctx.forecasts[rid]))
            clauses.append(f"leaves {rec_off} goal{'s' if rec_off != 1 else ''} off track")
    proj = ctx.retirement.get(rid)
    if _ok(proj) and proj.target_retirement_age is not None:
        if proj.reachable and proj.projected_retirement_age is not None:
            clauses.append(f"projects retirement around age {_age(proj.projected_retirement_age)}"
                           f" against your target of {proj.target_retirement_age}")
        else:
            clauses.append(f"does not reach your retirement number before age {ctx.config.max_age}")
    if clauses:
        rec += lead + " and ".join(clauses)
    sentences.append(rec + ".")
    return " ".join(sentences)


def _single_goal_fact(v: GoalVerdict) -> str:
    if v.status is GoalStatus.off_track:
        return "your saving goal is off track"
    if v.status in (GoalStatus.met, GoalStatus.on_track):
        return f"your saving goal is {v.status.value.replace('_', ' ')}"
    if v.projected_completion_date is not None:
        return f"your saving goal has no deadline and is projected to be reached in {v.projected_completion_date:%B %Y}"
    return "your saving goal has no deadline, with nothing set aside for it yet"


def _savings_fact(m) -> Optional[str]:
    """'you save 3% of take-home pay and leave 15% unassigned' (savings and unassigned cash kept apart)."""
    if m.savings_rate is None:
        return None
    out = f"you save {_pct(m.savings_rate)} of take-home pay"
    if m.unallocated_share is not None and m.unallocated_share >= 0.005:
        out += f" and leave {_pct(m.unallocated_share)} unassigned"
    return out


def _strengths(ctx: PlanContext, rid: BudgetId) -> list[str]:
    cur, out = BudgetId.current, []
    forecast = ctx.forecasts[cur]
    if not forecast.cash_shortfall_months:
        out.append("Your current budget never runs out of cash over the next 12 months.")
    elif not ctx.forecasts[rid].cash_shortfall_months:
        out.append(f"The {BUDGET_LABELS[rid]} keeps your cash positive in every one of the next 12 months.")
    if rid is not cur and _off_track(forecast) and not _off_track(ctx.forecasts[rid]):
        out.append(f"The {BUDGET_LABELS[rid]} puts every saving goal back on track using your existing income.")
    for v in forecast.goals:
        if v.status in (GoalStatus.met, GoalStatus.on_track):
            out.append(f"{_goal_name(ctx, v.goal_id)} is {v.status.value.replace('_', ' ')} in your current budget.")
    rate = ctx.metrics.per_budget[cur.value].savings_rate
    if rate is not None and rate >= 0.2:
        out.append(f"You already save {_pct(rate)} of take-home pay, above the 20% guideline.")
    if ctx.metrics.per_budget[cur.value].savings_target_met and ctx.metrics.savings_target_monthly:
        out.append(f"Your current budget meets your savings target of {_usd(ctx.metrics.savings_target_monthly)}/mo.")
    known = [d for d in ctx.norm.debts if d.apr is not None]
    if not ctx.norm.debts:
        out.append("You carry no debt.")
    elif known and not ctx.metrics.high_apr_debts:
        which = "your debts" if len(known) == len(ctx.norm.debts) else "your debts with a known APR"
        out.append(f"None of {which} charge {ctx.config.high_apr_threshold:g}% APR or more.")
    em = ctx.metrics.emergency_fund_months
    if em is not None and em >= ctx.config.emergency_months:
        out.append(f"Your savings cover {em:.1f} months of needs, meeting the {ctx.config.emergency_months:g}-month "
                   "emergency-fund guideline.")
    proj = ctx.retirement.get(cur)
    if _ok(proj) and proj.matches_target and proj.projected_retirement_age is not None:
        out.append(f"Your current budget projects retirement around age {_age(proj.projected_retirement_age)}, "
                   f"at or before your target of {proj.target_retirement_age}.")
    return out


def _risks(ctx: PlanContext, rid: BudgetId, allowed: list[float]) -> list[str]:
    cur, out = BudgetId.current, []
    forecast = ctx.forecasts[cur]
    if forecast.cash_shortfall_months:
        out.append(f"Your current budget runs out of cash in {len(forecast.cash_shortfall_months)} of the next 12 months.")
    proj = ctx.retirement.get(cur)
    if _ok(proj) and not proj.reachable:
        out.append(f"In your current budget, the retirement number of {_usd(proj.retirement_number or 0.0)} "
                   f"is not reached before age {ctx.config.max_age}.")
    elif _ok(proj) and proj.matches_target is False and proj.projected_retirement_age is not None:
        out.append(f"Your current budget projects retirement around age {_age(proj.projected_retirement_age)}, "
                   f"later than your target of {proj.target_retirement_age}.")
    elif proj is not None and proj.status == "insufficient_data":
        out.append(f"Retirement could not be projected because your {_missing_text(proj.missing)} are missing.")
    for debt in ctx.norm.debts:
        if debt.id in ctx.metrics.high_apr_debts:
            out.append(f"{debt.name} charges {debt.apr:g}% APR, so its balance grows quickly if only the minimum is paid.")
        if debt.balance > 0 and not debt.minimum_payment:
            out.append(f"{debt.name} has no payment in your profile, so no budget pays it down: its "
                       f"{_usd(debt.balance)} balance stays as it is.")
        if debt.balance > 0 and debt.apr is None:
            out.append(f"The APR of {debt.name} is unknown, so its interest cost is not included.")
    if ctx.metrics.per_budget[cur.value].savings_target_met is False and ctx.metrics.savings_target_monthly:
        out.append(f"Your current budget saves less than your target of {_usd(ctx.metrics.savings_target_monthly)}/mo.")
    em = ctx.metrics.emergency_fund_months
    if em is not None and em < ctx.config.emergency_months:
        out.append(f"Your savings cover only {em:.1f} months of needs.")
    for bid in RESTRUCTURED:
        budget = ctx.budgets.get(bid)
        if budget is not None and budget.infeasibility is not None and budget.infeasibility.gaps:
            gaps = ", ".join(f"{bucket} by {_usd(gap)}/mo" for bucket, gap in budget.infeasibility.gaps.items())
            out.append(f"The {BUDGET_LABELS[bid]} cannot fully meet its targets (misses {gaps}).")
    unfunded = ctx.budgets[rid].unfunded_goals if rid in ctx.budgets else []
    if unfunded:
        out.append(f"The {BUDGET_LABELS[rid]} cannot fully fund: " + ", ".join(_goal_name(ctx, g) for g in unfunded) + ".")
    for warning in ctx.norm.warnings[:3]:
        if not find_ungrounded([warning.message], allowed):
            out.append(f"Data note: {warning.message}")
    return out


def fallback_recommendation(context: PlanContext) -> Recommendation:
    """Deterministic templated recommendation (generated_by=fallback); every number comes from the plan."""
    allowed = allowed_values(context)
    status = overall_status(context)
    rid = choose_budget(context)
    candidates: list[Optional[MajorChange]] = [
        _switch_change(context, rid),
        _debt_change(context, rid),
        *_goal_changes(context, rid),
        _cuts_change(context, rid, allowed),
        _retirement_change(context, rid),
        _emergency_change(context, rid),
    ]
    changes = [c for c in candidates if c is not None][:MAX_MAJOR_CHANGES]
    for filler in _filler_changes(context, rid):
        if len(changes) >= MIN_MAJOR_CHANGES:
            break
        changes.append(filler)
    return Recommendation(
        overall_status=status,
        summary=_summary(context, status, rid),
        recommended_budget=rid,
        major_changes=changes,
        strengths=_strengths(context, rid),
        risks=_risks(context, rid, allowed),
        generated_by=GeneratedBy.fallback,
    )
