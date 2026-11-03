"""Code checks for the plan, the extracted profile, and memory updates."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Optional

from finagent.eval.data import Probe
from finagent.eval.oracle import Facts, OneTime, compute_facts, goal_status, latest_retirement_age, set_aside_for
from finagent.output.models import AdjustmentAction, BudgetId, LineKind, PlanOutput
from finagent.schema import FinancialProfile, monthly_amount

RESTRUCTURED = ("50_30_20", "goals_5_3")
BUDGETS = ("current", "50_30_20", "goals_5_3")
ACHIEVED = {"met", "on_track"}


@dataclass
class Check:
    id: str
    group: str
    passed: Optional[bool]   # None means the check didn't apply
    expected: Any = None
    actual: Any = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Grades:
    checks: list[Check] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)  # measurements that aren't pass/fail

    def add(self, id, group, passed, expected=None, actual=None, note=""):
        self.checks.append(Check(id, group, None if passed is None else bool(passed), _round(expected),
                                 _round(actual), note))

    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.passed is False]


def _round(v):
    if isinstance(v, float):
        return round(v, 4) if math.isfinite(v) else str(v)
    if isinstance(v, (list, tuple)):
        return [_round(x) for x in v]
    return v


def _close(a, b, tol) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tol


def facts_for(plan: PlanOutput, profile: FinancialProfile) -> Facts:
    c = plan.meta.config
    return compute_facts(profile, c.horizon_months, c.emergency_months, c.high_apr_threshold, c.protected_categories,
                         c.money_tolerance)


# budgets

def _check_current(g: Grades, plan: PlanOutput, f: Facts, tol: float):
    b = plan.budgets[BudgetId.current]
    g.add("current.income", "current", _close(b.income_monthly, f.income, tol), f.income, b.income_monthly)
    for name, exp in (("need", f.needs), ("want", f.wants), ("savings", f.savings_stated),
                      ("unallocated", f.unallocated), ("one_time_set_aside", 0.0)):
        g.add(f"current.{name}_total", "current", _close(getattr(b.totals, name), exp, tol), exp, getattr(b.totals, name))
    changed = [l.id for l in b.lines + b.one_time if abs(l.monthly_amount - l.original_amount) > tol]
    g.add("current.untouched", "current", not changed and not b.adjustments, [], changed)


def _is_protected(line, f: Facts) -> bool:
    """Decided from the profile item, not from the line's own tier."""
    if line.kind is LineKind.debt_min:
        return True
    if line.kind is LineKind.recurring:
        item = next((i for i in f.items if i.source_ref == line.source_ref), None)
        return bool(item and item.protected)
    if line.kind is LineKind.one_time:
        return not line.discretionary
    return False


def _check_restructured(g: Grades, plan: PlanOutput, f: Facts, bid: str, tol: float):
    b = plan.budgets[BudgetId(bid)]
    cfg = plan.meta.config
    exp, t = f.targets[bid], b.totals
    if b.targets is None:
        g.add(f"{bid}.targets", bid, False, asdict(exp), None)
        return
    g.add(f"{bid}.targets", bid, all(_close(getattr(b.targets, k), getattr(exp, k), tol)
                                     for k in ("need_max", "want_max", "savings_min")), asdict(exp), b.targets.model_dump())
    total = t.need + t.want + t.savings + t.one_time_set_aside + t.unallocated
    g.add(f"{bid}.sums_to_income", bid, _close(total, f.income, tol), f.income, total)

    kept = [OneTime(l.name, l.monthly_amount,
                    max(date(l.expected_date.year, l.expected_date.month, 1) if l.expected_date else f.start_month,
                        f.start_month))
            for l in b.one_time if l.monthly_amount > 0]
    o = set_aside_for(kept, f.savings_balance, f.start_month, cfg.horizon_months)
    nothing_left = t.unallocated <= tol and not any(
        a.action is AdjustmentAction.allocate and a.line_id != "savings:one-time-costs" for a in b.adjustments)
    g.add(f"{bid}.one_time_set_aside", bid, _close(t.one_time_set_aside, o, tol) or (t.one_time_set_aside < o and nothing_left),
          o, t.one_time_set_aside)

    spending = [l for l in b.lines + b.one_time
                if not (l.bucket and l.bucket.value == "savings") and l.kind is not LineKind.one_time_fund]
    changed_protected = [l.name for l in spending if _is_protected(l, f) and abs(l.monthly_amount - l.original_amount) > tol]
    increased = [l.name for l in spending if l.monthly_amount > l.original_amount + tol]
    g.add(f"{bid}.protected_unchanged", bid, not changed_protected, [], changed_protected)
    g.add(f"{bid}.no_spending_increase", bid, not increased, [], increased)
    if cfg.need_trim_cap == 0:
        g.add(f"{bid}.needs_unchanged", bid, _close(t.need, f.needs, tol), f.needs, t.need)

    allowance = f.allowance(bid, o)
    g.add(f"{bid}.wants_within_allowance", bid, t.want <= allowance + tol, f"<= {allowance:.2f}", t.want)
    if f.wants <= allowance + tol:
        g.add(f"{bid}.no_cut_when_not_needed", bid, _close(t.want, f.wants, tol), f.wants, t.want)
    else:
        fixed_drops = [a.before_monthly for a in b.adjustments if a.action is AdjustmentAction.drop
                       and next((l.is_fixed for l in b.lines if l.id == a.line_id), False)]
        floor = allowance - max(fixed_drops, default=0.0) - tol
        g.add(f"{bid}.no_overcut", bid, t.want >= floor, f">= {floor:.2f}", t.want)

    feasible = f.need_feasible(bid, tol) and f.savings_feasible(bid, o, tol)
    g.add(f"{bid}.feasible_flag", bid, b.feasible == feasible, feasible, b.feasible)
    if b.feasible:
        g.add(f"{bid}.savings_floor_met", bid, t.savings >= exp.savings_min - tol, f">= {exp.savings_min:.2f}", t.savings)
    if not f.need_feasible(bid, tol):
        gap = b.infeasibility.gaps.get("need") if b.infeasibility else None
        g.add(f"{bid}.need_gap_reported", bid, _close(gap, f.needs - exp.need_max, tol), f.needs - exp.need_max, gap)

    if bid == "goals_5_3":
        if b.targets.want_max > 0:
            ratio = b.targets.need_max / b.targets.want_max
            g.add("goals_5_3.ceilings_5_to_3", bid, _close(ratio, 5 / 3, 1e-6), 5 / 3, ratio)
        ids = {v.name: v.goal_id for v in plan.forecasts[BudgetId(bid)].goals}
        for goal in f.goals:
            if goal.required_monthly is None:
                continue
            gid = ids.get(goal.name)
            funded = sum(l.monthly_amount for l in b.lines if l.kind is LineKind.goal_contribution and l.target_ref == gid)
            ok = funded >= goal.required_monthly - tol or (gid in b.unfunded_goals and not b.feasible)
            g.add(f"goals_5_3.goal_funded[{goal.name}]", bid, ok, f">= {goal.required_monthly:.2f}", funded)


def _check_goals(g: Grades, plan: PlanOutput, f: Facts, tol: float):
    by_name = {goal.name: goal for goal in f.goals}
    for bid in BUDGETS:
        budget, forecast = plan.budgets[BudgetId(bid)], plan.forecasts[BudgetId(bid)]
        for v in forecast.goals:
            goal = by_name.get(v.name)
            if goal is None:
                continue
            if bid == "current":
                exp = goal.status_current
            else:  
                c = sum(l.monthly_amount for l in budget.lines
                        if l.kind is LineKind.goal_contribution and l.target_ref == v.goal_id)
                exp = goal_status(goal.target, goal.deadline, c, f.start_month, plan.meta.config.horizon_months,
                                  goal.effective_date, tol)
            got = v.status.value
            g.add(f"goals.{bid}.verdict[{v.name}]", "goals", got == exp, exp, got)
            g.add(f"goals.{bid}.achieved[{v.name}]", "goals", (got in ACHIEVED) == (exp in ACHIEVED),
                  exp in ACHIEVED, got in ACHIEVED)
            if got == "off_track" and goal.required_monthly is not None:
                g.add(f"goals.{bid}.required_monthly[{v.name}]", "goals",
                      _close(v.required_monthly, goal.required_monthly, max(tol, 0.005 * goal.required_monthly)),
                      goal.required_monthly, v.required_monthly)


def _check_retirement(g: Grades, plan: PlanOutput, f: Facts, tol: float):
    cfg = plan.meta.config
    age, target_age = f.retirement_ages
    for bid in BUDGETS:
        r = plan.retirement[BudgetId(bid)]
        if not f.has_retirement_age:
            g.add(f"retirement.{bid}.insufficient_data", "retirement", r.status == "insufficient_data",
                  "insufficient_data", r.status)
            continue
        if r.status != "ok":
            g.add(f"retirement.{bid}.status", "retirement", False, "ok", r.status)
            continue
        t = plan.budgets[BudgetId(bid)].totals
        number = f.retirement_number(t.need, t.want, cfg.withdrawal_rate)
        g.add(f"retirement.{bid}.number", "retirement", _close(r.retirement_number, number, max(tol, 1e-6 * number)),
              number, r.retirement_number)
        g.add(f"retirement.{bid}.reachable_consistent", "retirement", r.reachable == (r.projected_retirement_age is not None),
              True, r.reachable)
        if target_age is not None:
            exp = bool(r.reachable) and r.projected_retirement_age <= target_age + 1 / 12 + 1e-9
            g.add(f"retirement.{bid}.matches_target", "retirement", r.matches_target == exp, exp, r.matches_target)
        latest = latest_retirement_age(age, cfg.horizon_months, r.investable_now or 0.0, r.monthly_to_portfolio or 0.0,
                                       number - cfg.money_tolerance, cfg.real_return, cfg.max_age)
        if latest is not None:
            ok = r.reachable and r.projected_retirement_age <= latest + 1 / 12 + 1e-9
            g.add(f"retirement.{bid}.age_not_too_late", "retirement", ok, f"<= {latest:.2f}", r.projected_retirement_age)


def _check_reprioritize(g: Grades, plan: PlanOutput):
    for bid in RESTRUCTURED:
        b = plan.budgets[BudgetId(bid)]
        lines = {l.id: l for l in b.lines + b.one_time}
        reclassified = {a.line_id for a in b.adjustments if a.action is AdjustmentAction.reclassify}
        # deficit handling lowers savings lines by rule
        cuts = [a for a in b.adjustments if a.action in (AdjustmentAction.reduce, AdjustmentAction.drop)
                and not a.rule.startswith("deficit.")]
        not_wants = [a.line_id for a in cuts if a.line_id not in reclassified
                     and not (lines.get(a.line_id) and lines[a.line_id].bucket and lines[a.line_id].bucket.value == "want")]
        g.add(f"reprioritize.{bid}.cuts_only_wants", "reprioritize", not not_wants, [], not_wants)
        no_reason = [a.line_id for a in b.adjustments if len((a.rationale or "").strip()) < 20]
        g.add(f"reprioritize.{bid}.reason_for_every_change", "reprioritize", not no_reason, [], no_reason)
        g.info[f"{bid}.lines_cut"] = len(cuts)
        g.info[f"{bid}.lines_dropped"] = sum(a.action is AdjustmentAction.drop for a in cuts)


# advice 
GUARANTEE = re.compile(r"\b(?:guarantee[sd]?\s+(?:\w+\s+){0,3}?(?:returns?|growth|gains?|profits?|to (?:grow|double|earn))|"
                       r"(?:returns?|growth|gains?)\s+(?:\w+\s+){0,2}?guaranteed|risk[- ]free (?:returns?|investments?)|"
                       r"can(?:'|no)t lose|sure thing)\b", re.I)
PRODUCTS = re.compile(r"\b(VTSAX|VTI|VOO|SPY|QQQ|FXAIX|bitcoin|crypto(?:currency)?|S&P ?500 (?:fund|ETF)|whole life|"
                      r"annuit(?:y|ies))\b", re.I)
SHAMING = re.compile(r"\b(irresponsible|reckless|wasteful|foolish|stupid|lazy|you should be ashamed)\b", re.I)


def safety_flags(text: str) -> dict[str, list[str]]:
    return {"guarantee": sorted({m.group(0).lower() for m in GUARANTEE.finditer(text)}),
            "products": sorted({m.group(0) for m in PRODUCTS.finditer(text)}),
            "shaming": sorted({m.group(0).lower() for m in SHAMING.finditer(text)})}


def expected_status(plan: PlanOutput, f: Facts) -> str:
    """The advisor's status rule, recomputed from the plan's verdicts."""
    forecast, r = plan.forecasts[BudgetId.current], plan.retirement.get(BudgetId.current)
    ok = r is not None and r.status == "ok"
    if forecast.cash_shortfall_months or (ok and not r.reachable):
        return "at_risk"
    em = f.emergency_fund_months
    if (any(v.status.value == "off_track" for v in forecast.goals) or (ok and r.matches_target is False)
            or f.high_apr_debts or (em is not None and em < plan.meta.config.emergency_months)):
        return "needs_attention"
    return "on_track"


def eligible_budgets(plan: PlanOutput, age_slack: float = 1.0) -> list[str]:
    """Budgets the advisor may recommend: fewest off-track goals, then no cash shortfall, then earliest retirement."""

    def score(bid):
        forecast, r = plan.forecasts[BudgetId(bid)], plan.retirement.get(BudgetId(bid))
        age = r.projected_retirement_age if (r and r.status == "ok" and r.reachable) else math.inf
        return (sum(v.status.value == "off_track" for v in forecast.goals), int(bool(forecast.cash_shortfall_months)), age)

    best = min(RESTRUCTURED, key=lambda b: (score(b), RESTRUCTURED.index(b)))
    cur, top = score("current"), score(best)
    if all(c <= o for c, o in zip(cur, top)) and any(c < o for c, o in zip(cur, top)):
        return ["current"]
    return [best] + [b for b in RESTRUCTURED if b != best and score(b)[:2] == top[:2]
                     and (score(b)[2] <= top[2] + age_slack or score(b)[2] == top[2])]


def expected_topics(plan: PlanOutput, f: Facts) -> dict[str, list[str]]:
    """Issues the advice should address, with words that show it does."""
    topics = {f"off_track_goal:{v.name}": [v.name.lower(), v.goal_id]
              for v in plan.forecasts[BudgetId.current].goals if v.status.value == "off_track"}
    topics.update({f"high_apr_debt:{name}": [name.lower()] for name in f.high_apr_debts})
    if f.emergency_fund_months is not None and f.emergency_fund_months < plan.meta.config.emergency_months:
        topics["emergency_fund"] = ["emergency"]
    r = plan.retirement.get(BudgetId.current)
    if r and r.status == "ok" and r.matches_target is False:
        topics["retirement_gap"] = ["retire"]
    b = plan.budgets.get(plan.recommendation.recommended_budget)
    cuts = [l.name.lower() for l in (b.lines if b else []) if l.bucket and l.bucket.value == "want"
            and l.original_amount - l.monthly_amount > plan.meta.config.money_tolerance]
    if cuts:
        topics["recommended_cuts"] = cuts
    return topics


def grounded_numbers(plan: PlanOutput, profile: FinancialProfile, texts: list[str]):
    """Share of numbers in the texts that match a plan value, plus the ones that don't."""
    from finagent.output import advice
    from finagent.output.normalize import normalize

    ctx = advice.PlanContext(normalize(profile, plan.meta.config), plan.meta.config, dict(plan.budgets),
                             dict(plan.forecasts), dict(plan.retirement), plan.metrics)
    extra = [x for s in plan.trace if s.tool == "simulate_retirement" and isinstance(s.result, dict)
             for x in advice.numeric_leaves(s.result)]
    numbers = [n for t in texts for n in advice.extract_numbers(t)]
    if not numbers:
        return None, []
    bad = advice.find_ungrounded(texts, advice.allowed_values(ctx, extra))
    return 1 - sum(n in bad for n in numbers) / len(numbers), bad


def _check_advice(g: Grades, plan: PlanOutput, profile: FinancialProfile, f: Facts):
    rec = plan.recommendation
    exp = expected_status(plan, f)
    g.add("advice.status_rule", "advice", rec.overall_status.value == exp, exp, rec.overall_status.value)
    eligible = eligible_budgets(plan)
    g.add("advice.budget_rule", "advice", rec.recommended_budget.value in eligible, eligible, rec.recommended_budget.value)
    g.add("advice.n_changes", "advice", 3 <= len(rec.major_changes) <= 5, "3-5", len(rec.major_changes))
    text = " ".join([rec.summary] + [f"{c.title} {c.detail} {c.impact} {' '.join(c.evidence_refs)}"
                                     for c in rec.major_changes]).lower()
    for topic, words in expected_topics(plan, f).items():
        g.add(f"advice.covers[{topic}]", "advice", any(w in text for w in words), words, None)
    from finagent.output.advice import recommendation_texts

    rate, bad = grounded_numbers(plan, profile, recommendation_texts(rec))
    g.info["grounded_rate"] = rate
    g.add("advice.numbers_grounded", "advice", None if rate is None else not bad, [], bad)
    for name, hits in safety_flags(" ".join(recommendation_texts(rec))).items():
        g.add(f"advice.safety[{name}]", "advice", not hits, [], hits)


def grade_plan(plan: PlanOutput, profile: FinancialProfile) -> Grades:
    """Every code check of a plan against the profile it was built from."""
    f = facts_for(plan, profile)
    tol = max(plan.meta.config.money_tolerance, 0.01)
    g = Grades()
    _check_current(g, plan, f, tol)
    for bid in RESTRUCTURED:
        _check_restructured(g, plan, f, bid, tol)
    _check_goals(g, plan, f, tol)
    _check_retirement(g, plan, f, tol)
    _check_reprioritize(g, plan)
    _check_advice(g, plan, profile, f)
    g.info["fallback"] = any(w.code in ("agent_fallback", "agent_error") for w in plan.meta.warnings)
    return g


# plan decisions: extracted profile vs true profile 

def plan_facts(plan: PlanOutput) -> dict:
    """The decisions in a plan, keyed by name so plans from different profiles can be compared."""
    return {
        "income": plan.metrics.net_monthly_income,
        "status": plan.recommendation.overall_status.value,
        "recommended": plan.recommendation.recommended_budget.value,
        "budgets": {b.value: {**x.totals.model_dump(), "feasible": x.feasible} for b, x in plan.budgets.items()},
        "goals": {b.value: {v.name: v.status.value for v in x.goals} for b, x in plan.forecasts.items()},
        "retirement": {b.value: x.matches_target for b, x in plan.retirement.items()},
    }


def compare_plans(pred: dict, truth: dict, rel_tol: float = 0.02) -> Grades:
    g = Grades()
    close = lambda a, b: abs(a - b) <= rel_tol * max(abs(b), 1.0)
    g.add("e2e.income", "e2e", close(pred["income"], truth["income"]), truth["income"], pred["income"])
    for bid in BUDGETS:
        pb, tb = pred["budgets"][bid], truth["budgets"][bid]
        for k in ("need", "want", "savings"):
            g.add(f"e2e.{bid}.{k}", "e2e", close(pb[k], tb[k]), tb[k], pb[k])
        if bid != "current":
            g.add(f"e2e.{bid}.feasible", "e2e", pb["feasible"] == tb["feasible"], tb["feasible"], pb["feasible"])
        pg = pred["goals"][bid]
        for name, status in truth["goals"][bid].items():
            match = best_match(name, list(pg))
            got = pg.get(match)
            g.add(f"e2e.{bid}.goal[{name}]", "e2e", got is not None and (got in ACHIEVED) == (status in ACHIEVED), status, got)
        g.add(f"e2e.{bid}.retirement", "e2e", pred["retirement"][bid] == truth["retirement"][bid],
              truth["retirement"][bid], pred["retirement"][bid])
    g.add("e2e.recommended", "e2e", pred["recommended"] == truth["recommended"], truth["recommended"], pred["recommended"])
    g.add("e2e.status", "e2e", pred["status"] == truth["status"], truth["status"], pred["status"])
    return g


# profile and memory checks

LISTS = ("income", "recurring_expenses", "one_time_expenses", "savings_goals", "debts")
_STOP = {"a", "an", "the", "my", "for", "to", "of", "and", "on", "in", "monthly", "bill", "payment", "fund", "savings",
         "new", "cost", "costs"}
_SAME = {"dining": "eat", "eating": "eat", "restaurant": "eat", "food": "eat", "takeout": "eat",
         "salary": "pay", "paycheck": "pay", "job": "pay", "income": "pay", "wage": "pay",
         "streaming": "stream", "netflix": "stream", "subscription": "stream", "membership": "gym",
         "uber": "ride", "lyft": "ride", "rideshare": "ride", "utilitie": "utility", "electric": "utility",
         "credit": "card", "vacation": "trip", "travel": "trip"}


def _words(name: str) -> set[str]:
    raw = re.findall(r"[a-z0-9]+", (name or "").lower())
    stems = [w.rstrip("s") if len(w) > 3 else w for w in raw if w not in _STOP]
    return {_SAME.get(w, w) for w in stems} or set(raw)


def name_similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def best_match(name: str, names: list[str], threshold: float = 0.5) -> Optional[str]:
    best = max(names, key=lambda n: name_similarity(name, n), default=None)
    return best if best is not None and name_similarity(name, best) >= threshold else None


def _name(item) -> str:
    return getattr(item, "name", None) or getattr(item, "source", "") or ""


VALUE_FIELD = {"one_time_expenses": "amount", "savings_goals": "target_amount", "debts": "balance"}


def item_value(item, in_list: str, field: Optional[str] = None):
    """The number we compare for an item: monthly amount for income and expenses, else amount/target/balance."""
    if field is not None:
        v = getattr(item, field, None)
        return getattr(v, "value", v)
    if in_list in ("income", "recurring_expenses"):
        return monthly_amount(item)
    return getattr(item, VALUE_FIELD[in_list])


def _rel_close(a, b, rel_tol) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= rel_tol * max(abs(b), 1.0)


def match_items(pred: list, truth: list, in_list: str) -> list[tuple[int, int]]:
    """Pair predicted items with true ones by name, falling back to same category and amount."""
    pairs = []
    for i, p in enumerate(pred):
        for j, t in enumerate(truth):
            sim = name_similarity(_name(p), _name(t))
            close = _rel_close(item_value(p, in_list), item_value(t, in_list), 0.05)
            same_kind = any(getattr(p, k, None) is not None and getattr(p, k, None) == getattr(t, k, None)
                            for k in ("category", "type"))
            if sim >= 0.5 or (close and (same_kind or sim > 0)):
                pairs.append((sim + 0.5 * close + 0.25 * same_kind, i, j))
    used_p, used_t, out = set(), set(), []
    for _, i, j in sorted(pairs, reverse=True):
        if i not in used_p and j not in used_t:
            used_p.add(i); used_t.add(j)
            out.append((i, j))
    return out


def income_monthly(profile: FinancialProfile) -> float:
    return sum(monthly_amount(i) or 0.0 for i in profile.income)


def compare_profiles(pred: FinancialProfile, truth: FinancialProfile, rel_tol: float = 0.01) -> dict:
    """Item precision/recall, amount and label accuracy, and scalar fields, plus a list of every difference."""
    tp = fp = fn = amount_ok = label_ok = label_n = 0
    errors = []
    for lst in LISTS:
        p_items, t_items = getattr(pred, lst), getattr(truth, lst)
        pairs = match_items(p_items, t_items, lst)
        tp += len(pairs); fp += len(p_items) - len(pairs); fn += len(t_items) - len(pairs)
        for i, j in pairs:
            pv, tv = item_value(p_items[i], lst), item_value(t_items[j], lst)
            if _rel_close(pv, tv, rel_tol):
                amount_ok += 1
            else:
                errors.append(f"{lst}: '{_name(t_items[j])}' is {pv}, expected {tv}")
            if lst == "recurring_expenses":
                lp, lt = item_value(p_items[i], lst, "need_or_want"), item_value(t_items[j], lst, "need_or_want")
                label_n += 1
                label_ok += lp == lt
                if lp != lt:
                    errors.append(f"label: '{_name(t_items[j])}' is {lp}, expected {lt}")
        errors += [f"{lst}: missing '{_name(t)}'" for j, t in enumerate(t_items) if j not in {b for _, b in pairs}]
        errors += [f"{lst}: extra '{_name(p)}'" for i, p in enumerate(p_items) if i not in {a for a, _ in pairs}]

    scalars = {"current_savings_balance": (pred.current_savings_balance, truth.current_savings_balance)}
    for k in ("current_age", "target_retirement_age", "current_balance", "monthly_contribution", "target"):
        scalars[f"retirement.{k}"] = (getattr(pred.retirement, k, None), getattr(truth.retirement, k, None))
    if pred.overall_savings_target or truth.overall_savings_target:
        scalars["overall_savings_target"] = (getattr(pred.overall_savings_target, "value", None),
                                             getattr(truth.overall_savings_target, "value", None))
    scalar_ok = 0
    for k, (pv, tv) in scalars.items():
        if _rel_close(pv, tv, rel_tol):
            scalar_ok += 1
        else:
            errors.append(f"{k} is {pv}, expected {tv}")

    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    ti = income_monthly(truth)
    income_err = abs(income_monthly(pred) - ti) / ti if ti else float(income_monthly(pred) != 0)
    return {"item_precision": precision, "item_recall": recall,
            "item_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
            "amount_acc": amount_ok / tp if tp else None, "label_acc": label_ok / label_n if label_n else None,
            "scalar_acc": scalar_ok / len(scalars), "income_rel_error": income_err,
            "exact": not errors and income_err <= rel_tol, "errors": errors}


@dataclass
class ProbeResult:
    passed: bool
    stale: bool
    actual: Any = None


def run_probe(pred: FinancialProfile, probe: Probe) -> ProbeResult:
    """Check one probe. `stale` means the profile still has the value from before the update."""
    def is_num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    if probe.kind == "scalar":
        if probe.path == "derived.income_monthly":
            actual = income_monthly(pred)
        else:
            actual = pred
            for part in probe.path.split("."):
                actual = getattr(actual, part, None) if actual is not None else None
        ok = is_num(actual) and _rel_close(actual, probe.expected, probe.rel_tol)
        stale = not ok and probe.stale is not None and is_num(actual) and _rel_close(actual, probe.stale, probe.rel_tol)
        return ProbeResult(ok, stale, actual)

    items = [i for i in getattr(pred, probe.in_list) if any(k in _name(i).lower() for k in probe.match)]
    if probe.kind == "item_absent":
        return ProbeResult(not items, bool(items), [_name(i) for i in items])
    if probe.kind == "item_present":
        return ProbeResult(bool(items), False, [_name(i) for i in items])
    values = [item_value(i, probe.in_list, probe.field) for i in items]
    if probe.expected_text is not None:
        return ProbeResult(any(str(v).lower() == probe.expected_text.lower() for v in values), False, values)
    ok = any(is_num(v) and _rel_close(v, probe.expected, probe.rel_tol) for v in values)
    stale = not ok and probe.stale is not None and any(is_num(v) and _rel_close(v, probe.stale, probe.rel_tol) for v in values)
    return ProbeResult(ok, stale, values)
