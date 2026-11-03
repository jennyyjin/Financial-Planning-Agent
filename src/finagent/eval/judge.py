"""LLM judge for actionability, feasibility and tone, plus tools to check the judge."""

from __future__ import annotations

import copy
import csv
import json
import math
import random
import re
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from finagent.output.agents.runtime import ChatModel, Message
from finagent.output.format import dollars
from finagent.output.models import BudgetId, LineKind, PlanOutput
from finagent.schema import FinancialProfile

PROMPTS = Path(__file__).resolve().parents[3] / "prompts" / "eval"


@dataclass(frozen=True)
class Dimension:
    name: str
    items: tuple[str, ...]
    critical: frozenset
    na_allowed: frozenset = frozenset()


DIMENSIONS = {
    "actionability": Dimension("actionability", tuple(f"A{i}" for i in range(1, 8)), frozenset({"A1"})),
    "feasibility": Dimension("feasibility", tuple(f"F{i}" for i in range(1, 8)), frozenset({"F1", "F2", "F4"}),
                             frozenset({"F5", "F6", "F7"})),
    "tone": Dimension("tone", tuple(f"T{i}" for i in range(1, 9)), frozenset({"T1", "T6"}), frozenset({"T7"})),
}
SCORE_CAP = 2


def load_prompts() -> tuple[str, dict[str, str]]:
    """Split judge.md into the shared instructions and one rubric per dimension."""
    parts = re.split(r"^## ", (PROMPTS / "judge.md").read_text(), flags=re.M)
    rubrics = {p.split("\n", 1)[0].strip().lower(): "## " + p for p in parts[1:]}
    return parts[0].strip(), rubrics


def load_examples() -> dict:
    return json.loads((PROMPTS / "judge_examples.json").read_text())


# what the judge sees 

@dataclass
class JudgeCase:
    messages: list[dict]   # [{"timestamp", "text"}]
    facts: list[str]


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def build_facts(plan: PlanOutput, profile: FinancialProfile) -> list[str]:
    """Key facts in plain sentences, taken from the deterministic plan of the true profile."""
    cur = plan.budgets[BudgetId.current]
    t, s = cur.totals, cur.shares
    facts = [f"Take-home income: {dollars(cur.income_monthly)}/mo"]
    if any(w.code == "income_may_be_gross" for w in plan.meta.warnings):
        facts.append("Some income may be before-tax pay, so real take-home pay is probably lower")
    facts.append(f"Current budget: needs {dollars(t.need)} ({_pct(s and s.need)}), wants {dollars(t.want)} "
                 f"({_pct(s and s.want)}), savings {dollars(t.savings)} ({_pct(s and s.savings)}), "
                 f"unassigned {dollars(t.unallocated)} ({_pct(s and s.unallocated)})")
    for label, bucket in (("Essential costs (never cut)", "need"), ("Discretionary spending", "want")):
        lines = [l for l in cur.lines if l.bucket and l.bucket.value == bucket]
        if lines:
            facts.append(f"{label}: " + "; ".join(f"{l.name} {dollars(l.monthly_amount)}/mo" for l in lines))
    if profile.debts:
        facts.append("Debts: " + "; ".join(
            f"{d.name} {dollars(d.balance or 0)} at " + (f"{d.apr:g}% APR" if d.apr is not None else "unknown APR")
            + f", minimum {dollars(d.minimum_payment or 0)}/mo" + (" (high interest)" if d.name in plan.metrics.high_apr_debts else "")
            for d in profile.debts))
    else:
        facts.append("Debts: none")
    em = plan.metrics.emergency_fund_months
    facts.append(f"Cash savings: {dollars(profile.current_savings_balance or 0)}" + (
        f" = {em:.1f} months of essential costs (guideline: {plan.meta.config.emergency_months:g})" if em is not None else ""))
    if cur.one_time:
        facts.append("Upcoming one-time costs: " + "; ".join(
            f"{l.name} {dollars(l.original_amount)} in {l.expected_date:%b %Y}" if l.expected_date else l.name
            for l in cur.one_time))
    for v in plan.forecasts[BudgetId.current].goals:
        line = f"Goal: {v.name}" + (f" {dollars(v.target_amount)}" if v.target_amount else "")
        line += (f" by {v.deadline:%b %Y}" if v.deadline else "") + f": {v.status.value.replace('_', ' ').upper()}"
        if v.status.value == "off_track" and v.required_monthly is not None:
            line += f", needs {dollars(v.required_monthly)}/mo (now {dollars(v.current_monthly)}/mo)"
        facts.append(line)
    r, ret = plan.retirement.get(BudgetId.current), profile.retirement
    if r is None or r.status != "ok":
        facts.append("Retirement: not enough information to project")
    else:
        when = f"about age {r.projected_retirement_age:.0f}" if r.reachable else "not reachable by 100"
        facts.append(f"Retirement: age {ret.current_age}, target {ret.target_retirement_age}; current budget gets there at {when}")
    return facts


def build_case(turns: list[dict], plan: PlanOutput, profile: FinancialProfile) -> JudgeCase:
    return JudgeCase([{"timestamp": str(t["timestamp"])[:10], "text": t["text"]} for t in turns],
                     build_facts(plan, profile))


def advice_from_plan(plan: PlanOutput) -> dict:
    """What the user reads: the recommendation and the changes in the recommended budget."""
    rec = plan.recommendation
    b = plan.budgets[rec.recommended_budget]
    changes = [f"{l.name}: {'raised' if l.monthly_amount > l.original_amount else 'cut'} from "
               f"{dollars(l.original_amount)} to {dollars(l.monthly_amount)}/mo"
               for l in b.lines + b.one_time
               if abs(l.monthly_amount - l.original_amount) > 0.005 and l.kind is not LineKind.one_time_fund]
    return {"summary": rec.summary, "major_changes": [c.model_dump(include={"title", "detail", "impact"}) for c in rec.major_changes],
            "strengths": list(rec.strengths), "risks": list(rec.risks),
            "recommended_budget": rec.recommended_budget.value, "budget_changes": changes}


def render(case: JudgeCase, advice: dict) -> str:
    out = ["## User messages"] + [f"{i}. [{m['timestamp']}] {m['text']}" for i, m in enumerate(case.messages, 1)]
    out += ["", "## Key facts"] + [f"- {f}" for f in case.facts]
    out += ["", "## Advice", f"Summary: {advice.get('summary', '')}", "", "Major changes:"]
    for i, c in enumerate(advice.get("major_changes") or [], 1):
        out += [f"{i}. {c.get('title', '')}", f"   Detail: {c.get('detail', '')}", f"   Impact: {c.get('impact', '')}"]
    if advice.get("budget_changes"):
        out += ["", f"Recommended budget ({advice.get('recommended_budget')}):"] + [f"- {x}" for x in advice["budget_changes"]]
    out += ["", "Strengths:"] + [f"- {x}" for x in advice.get("strengths") or ["(none)"]]
    out += ["", "Risks:"] + [f"- {x}" for x in advice.get("risks") or ["(none)"]]
    return "\n".join(out)


def build_messages(dim: Dimension, case: JudgeCase, advice: dict, examples: Optional[dict]) -> list[Message]:
    shared, rubrics = load_prompts()
    ask = f"\n\nGrade the {dim.name} of this advice. Return only the JSON object."
    msgs = [Message(role="system", content=shared + "\n\n" + rubrics[dim.name])]
    if examples:
        ex_case = JudgeCase(examples["case"]["messages"], examples["case"]["facts"])
        for ex in examples["examples"]:
            msgs.append(Message(role="user", content=render(ex_case, ex["advice"]) + ask))
            msgs.append(Message(role="assistant", content=json.dumps(ex["judgments"][dim.name])))
    msgs.append(Message(role="user", content=render(case, advice) + ask))
    return msgs


# parsing and scoring

class BadJudgment(ValueError):
    pass


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise BadJudgment("no JSON object found")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise BadJudgment(f"invalid JSON: {e}") from e


def parse_judgment(text: str, dim: Dimension) -> dict:
    data = parse_json(text)
    checklist = data.get("checklist")
    if not isinstance(checklist, dict):
        raise BadJudgment("'checklist' must be an object keyed by item id")
    answers, problems = {}, []
    for item in dim.items:
        entry = checklist.get(item)
        answer = str(entry.get("answer") if isinstance(entry, dict) else entry).strip().lower()
        if answer not in ("yes", "no", "na"):
            problems.append(f"{item} needs yes/no/na")
        elif answer == "na" and item not in dim.na_allowed:
            problems.append(f"{item} can't be 'na'")
        else:
            answers[item] = answer
    score = data.get("score")
    if isinstance(score, str) and score.strip().isdigit():
        score = int(score)
    if not isinstance(score, int) or isinstance(score, bool) or not 1 <= score <= 5:
        problems.append("score must be a whole number from 1 to 5")
    if problems:
        raise BadJudgment("; ".join(problems))
    failed = sorted(i for i in dim.critical if answers.get(i) == "no")
    return {"score": min(score, SCORE_CAP) if failed else score, "raw_score": score, "capped": bool(failed) and score > SCORE_CAP,
            "failed_critical": failed, "checklist": answers, "reasoning": data.get("reasoning", ""),
            "evidence": {k: checklist[k].get("evidence") for k in answers if isinstance(checklist[k], dict)}}


@dataclass
class Result:
    dimension: str
    score: Optional[float] = None  # median over samples
    samples: list = field(default_factory=list)
    checklist: dict = field(default_factory=dict)  # majority answer per item
    spread: Optional[int] = None
    errors: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class Judge:
    def __init__(self, model: ChatModel, n_samples: int = 1, dimensions=None, retries: int = 2, examples: bool = True):
        self.model, self.n_samples, self.retries = model, n_samples, retries
        self.dimensions = [DIMENSIONS[d] for d in (dimensions or DIMENSIONS)]
        self.examples = load_examples() if examples else None

    @property
    def name(self) -> str:
        return getattr(self.model, "name", "judge")

    def _ask(self, dim: Dimension, msgs: list[Message], usage: dict) -> dict:
        error = ""
        for _ in range(self.retries + 1):
            turn = self.model.chat(msgs, [])
            for k, v in (turn.usage or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    usage[k] = usage.get(k, 0) + v
            try:
                return parse_judgment(turn.text, dim)
            except BadJudgment as e:
                error = str(e)
                msgs = msgs + [Message(role="assistant", content=turn.text or ""),
                               Message(role="user", content=f"That wasn't valid: {e}. Grade the {dim.name} again and "
                                                            f"return only the JSON, with items {', '.join(dim.items)}.")]
        raise BadJudgment(error)

    def grade(self, case: JudgeCase, advice: dict) -> dict[str, Result]:
        out = {}
        for dim in self.dimensions:
            msgs = build_messages(dim, case, advice, self.examples)
            res = Result(dim.name)
            for _ in range(self.n_samples):
                try:
                    res.samples.append(self._ask(dim, msgs, res.usage))
                except Exception as e: 
                    res.errors.append(f"{type(e).__name__}: {e}")
            if res.samples:
                scores = [s["score"] for s in res.samples]
                res.score, res.spread = statistics.median(scores), max(scores) - min(scores)
                for item in dim.items:
                    votes = [s["checklist"][item] for s in res.samples if item in s["checklist"]]
                    if votes:  
                        res.checklist[item] = max(("yes", "no", "na"), key=lambda a: (votes.count(a), a == "no"))
            out[dim.name] = res
        return out


# checking the judge: perturbations 

def _largest_essential(plan: PlanOutput):
    lines = [l for l in plan.budgets[BudgetId.current].lines if l.tier.value == "protected" and l.kind.value == "recurring"]
    return max(lines, key=lambda l: l.monthly_amount) if lines else None


def p_vague(advice, plan):
    out = copy.deepcopy(advice)
    generic = [("Be more mindful of spending", "Small changes add up.", "You'll feel more in control."),
               ("Try to save a bit more", "Saving regularly is a good habit.", "More security."),
               ("Look into your debts", "Paying down debt is usually a good idea.", "Less stress."),
               ("Review your goals", "Check in on your goals now and then.", "Better clarity."),
               ("Think about retirement", "It's never too early to plan.", "Peace of mind.")]
    out["major_changes"] = [dict(zip(("title", "detail", "impact"), g)) for g in generic[:len(advice["major_changes"])]]
    out["summary"], out["budget_changes"] = "There's room to make your money work harder.", []
    return out


def p_cut_essentials(advice, plan):
    line = _largest_essential(plan)
    if line is None:
        return None
    out = copy.deepcopy(advice)
    out["major_changes"].insert(0, {"title": f"Stop paying {line.name} for a few months",
                                    "detail": f"Skip the {dollars(line.monthly_amount)}/mo {line.name.lower()} payment until savings recover.",
                                    "impact": f"Frees {dollars(line.monthly_amount)} a month."})
    return out


def p_impossible_savings(advice, plan):
    income = plan.budgets[BudgetId.current].income_monthly
    if income <= 0:
        return None
    out = copy.deepcopy(advice)
    out["major_changes"].insert(0, {"title": f"Save {dollars(income * 1.1)} every month",
                                    "detail": f"Move {dollars(income * 1.1)} into savings each month.", "impact": "Builds savings fast."})
    return out


def p_shaming(advice, plan):
    out = copy.deepcopy(advice)
    out["summary"] = "Frankly, your spending is irresponsible and you should know better by now. " + out["summary"]
    return out


def p_guarantee(advice, plan):
    out = copy.deepcopy(advice)
    out["major_changes"].append({"title": "Put your extra cash in VOO", "detail": "Move leftover money into the VOO index fund.",
                                 "impact": "Index funds are guaranteed to return 10% a year."})
    return out


def p_padding(advice, plan):
    out = copy.deepcopy(advice)
    filler = (" Remember that personal finance is a journey and every situation is unique, so keep thinking "
              "carefully about your options as things change over time.")
    out["summary"] += filler
    for c in out["major_changes"]:
        c["detail"] += filler
    return out


PERTURBATIONS = {
    "vague": ("actionability", p_vague),
    "cut_essentials": ("feasibility", p_cut_essentials),
    "impossible_savings": ("feasibility", p_impossible_savings),
    "shaming": ("tone", p_shaming),
    "guarantee": ("tone", p_guarantee),
    "padding": ("none", p_padding),
}


def summarize_perturbations(rows: list[dict]) -> dict:
    """rows: {case, perturbation, target, dimension, original, perturbed}."""
    out = {}
    for name in sorted({r["perturbation"] for r in rows}):
        rs = [r for r in rows if r["perturbation"] == name and None not in (r["original"], r["perturbed"])]
        if not rs:
            continue
        target = rs[0]["target"]
        e = {"target": target, "cases": len({r["case"] for r in rs})}
        if target == "none":
            e["rose"] = _mean([r["perturbed"] > r["original"] for r in rs])
        else:
            drops = [r["original"] - r["perturbed"] for r in rs if r["dimension"] == target]
            e["detected"] = _mean([d >= 1 for d in drops])
            e["mean_drop"] = _mean(drops)
            e["other_dims_change"] = _mean([abs(r["original"] - r["perturbed"]) for r in rs if r["dimension"] != target])
        out[name] = e
    return out


# checking the judge: human ratings

def _mean(xs):
    xs = [float(x) for x in xs]
    return sum(xs) / len(xs) if xs else None


def _ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks, i = [0.0] * len(xs), 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(x, y) -> Optional[float]:
    if len(x) < 2:
        return None
    rx, ry = _ranks(x), _ranks(y)
    mx, my = _mean(rx), _mean(ry)
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sx * sy) if sx and sy else None


def weighted_kappa(x, y, k: int = 5) -> Optional[float]:
    """Cohen's kappa with quadratic weights, for ratings 1..k."""
    if not x:
        return None
    n = len(x)
    obs = [[0.0] * k for _ in range(k)]
    for a, b in zip(x, y):
        obs[int(a) - 1][int(b) - 1] += 1
    rows = [sum(r) for r in obs]
    cols = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    w = lambda i, j: (i - j) ** 2 / (k - 1) ** 2
    num = sum(w(i, j) * obs[i][j] for i in range(k) for j in range(k))
    den = sum(w(i, j) * rows[i] * cols[j] / n for i in range(k) for j in range(k))
    return 1 - num / den if den else None


def agreement(x, y) -> dict:
    xi, yi = [round(a) for a in x], [round(b) for b in y]
    return {"n": len(x), "spearman": spearman(x, y), "kappa": weighted_kappa(xi, yi),
            "exact": _mean([a == b for a, b in zip(xi, yi)]), "within_1": _mean([abs(a - b) <= 1 for a, b in zip(xi, yi)])}


def bootstrap_ci(xs, n: int = 2000, seed: int = 0):
    if len(xs) < 2:
        return None
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(xs) for _ in xs) / len(xs) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


SHEET_COLUMNS = ["item_id", "case_and_advice", "rater", "actionability", "feasibility", "tone", "notes"]


def write_rating_sheet(items: list[dict], path: Path, raters: list[str], seed: int = 0) -> Path:
    """One row per item and rater, in a different random order for each rater. The system isn't shown."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_COLUMNS)
        w.writeheader()
        for rater in raters:
            for it in rng.sample(items, len(items)):
                w.writerow({"item_id": it["item_id"], "case_and_advice": it["text"], "rater": rater})
    return path


def read_ratings(paths) -> list[dict]:
    rows = []
    for p in paths:
        with Path(p).open() as f:
            for r in csv.DictReader(f):
                for d in DIMENSIONS:
                    v = (r.get(d) or "").strip()
                    r[d] = int(v) if v.isdigit() else None
                rows.append(r)
    return rows


def human_agreement(ratings: list[dict], judge_scores: dict[str, dict]) -> dict:
    """Judge vs the average human rating, and each pair of humans against each other."""
    out = {}
    for dim in DIMENSIONS:
        by_item = {}
        for r in ratings:
            if r[dim] is not None:
                by_item.setdefault(r["item_id"], {})[r["rater"]] = r[dim]
        pairs = [(judge_scores[i][dim], _mean(h.values())) for i, h in by_item.items()
                 if judge_scores.get(i, {}).get(dim) is not None]
        entry = {"judge_vs_humans": agreement([a for a, _ in pairs], [b for _, b in pairs]), "between_humans": []}
        raters = sorted({r for h in by_item.values() for r in h})
        for i, a in enumerate(raters):
            for b in raters[i + 1:]:
                both = [(h[a], h[b]) for h in by_item.values() if a in h and b in h]
                if both:
                    entry["between_humans"].append({"raters": [a, b], **agreement([x for x, _ in both], [y for _, y in both])})
        out[dim] = entry
    return out
