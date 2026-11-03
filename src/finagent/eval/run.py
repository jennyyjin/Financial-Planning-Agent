"""Run the systems on scenarios, grade each case, and save the results."""

from __future__ import annotations

import importlib
import json
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from finagent.eval.checks import (
    ACHIEVED, Grades, best_match, compare_plans, compare_profiles, eligible_budgets, expected_status, facts_for,
    grade_plan, grounded_numbers, plan_facts, run_probe, safety_flags,
)
from finagent.eval.data import Scenario
from finagent.eval.judge import Judge, advice_from_plan, build_case, parse_json
from finagent.output import generate_plan
from finagent.output.agents.runtime import ChatModel, Message
from finagent.output.models import BudgetId, PlanOutput
from finagent.schema import FinancialProfile, MemoryMethod

GENERATED_AT = datetime(2026, 11, 1, 12, 0)  # fixed so deterministic plans are reproducible
BASELINE_PROMPT = Path(__file__).resolve().parents[3] / "prompts" / "eval" / "baseline.md"
# words that show a follow-up question asks about the missing field
ASKS_ABOUT = {"income.is_net": ["take-home", "take home", "after tax", "before tax", "net", "gross"],
              "debts.apr": ["apr", "interest", "rate"]}


@dataclass
class RunConfig:
    systems: list = field(default_factory=lambda: ["deterministic"])
    mode: str = "profile"
    memory_methods: list = field(default_factory=lambda: ["full_history", "rolling_summary"])
    agent_model: Optional[ChatModel] = None
    baseline_model: Optional[ChatModel] = None
    judge: Optional[Judge] = None
    workers: int = 4
    overwrite: bool = False


# no-tools baseline 

def run_baseline(model: ChatModel, turns: list[dict], retries: int = 2) -> dict:
    msgs = [Message(role="system", content=BASELINE_PROMPT.read_text()),
            Message(role="user", content=f"Today is {str(turns[-1]['timestamp'])[:10]}.\n\nUser messages:\n" +
                    "\n".join(f"{i}. [{str(t['timestamp'])[:10]}] {t['text']}" for i, t in enumerate(turns, 1)))]
    usage, error = {}, None
    for _ in range(retries + 1):
        turn = model.chat(msgs, [])
        for k, v in (turn.usage or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                usage[k] = usage.get(k, 0) + v
        try:
            out = parse_json(turn.text)
            for bid in ("current", "50_30_20", "goals_5_3"):
                for line in out["budgets"][bid]["lines"]:
                    if line["bucket"] not in ("need", "want", "savings") or not isinstance(line["monthly"], (int, float)):
                        raise ValueError(f"bad line in {bid}: {line}")
            if not isinstance(out["recommendation"]["major_changes"], list):
                raise ValueError("recommendation.major_changes must be a list")
            return {"output": out, "usage": usage}
        except (ValueError, KeyError, TypeError) as e:
            error = f"{type(e).__name__}: {e}"
            msgs += [Message(role="assistant", content=turn.text or ""),
                     Message(role="user", content=f"That wasn't valid ({error}). Return only the corrected JSON.")]
    raise RuntimeError(f"baseline output still invalid after retries: {error}")


def baseline_advice(out: dict) -> dict:
    rec = out["recommendation"]
    rb = rec.get("recommended_budget")
    changes = []
    if rb in ("50_30_20", "goals_5_3"):
        before = {l["name"]: l["monthly"] for l in out["budgets"]["current"]["lines"]}
        for l in out["budgets"][rb]["lines"]:
            b = before.get(l["name"])
            if b is not None and abs(b - l["monthly"]) > 0.5:
                changes.append(f"{l['name']}: {'raised' if l['monthly'] > b else 'cut'} from ${b:,.0f} to ${l['monthly']:,.0f}/mo")
    return {"summary": rec.get("summary", ""), "major_changes": rec.get("major_changes") or [],
            "strengths": rec.get("strengths") or [], "risks": rec.get("risks") or [], "recommended_budget": rb,
            "budget_changes": changes}


def grade_baseline(out: dict, profile: FinancialProfile, truth: PlanOutput) -> Grades:
    """The checks that make sense for free-form output, against the true profile."""
    f = facts_for(truth, profile)
    g = Grades()
    tol = lambda x: max(1.0, 0.01 * abs(x))

    def totals(lines):
        t = {"need": 0.0, "want": 0.0, "savings": 0.0}
        for l in lines:
            t[l["bucket"]] += float(l["monthly"])
        return t

    cur = totals(out["budgets"]["current"]["lines"])
    for k, exp in (("need", f.needs), ("want", f.wants), ("savings", f.savings_stated)):
        g.add(f"current.{k}_total", "current", abs(cur[k] - exp) <= tol(exp), exp, cur[k])
    protected = [i for i in f.items if i.protected and i.kind == "recurring"]
    for bid in ("50_30_20", "goals_5_3"):
        b = out["budgets"][bid]
        t, exp = totals(b["lines"]), f.targets[bid]
        g.add(f"{bid}.sums_to_income", bid, sum(t.values()) <= f.income + tol(f.income), f"<= {f.income:.2f}", sum(t.values()))
        g.add(f"{bid}.needs_unchanged", bid, abs(t["need"] - f.needs) <= tol(f.needs), f.needs, t["need"])
        g.add(f"{bid}.wants_within_allowance", bid, t["want"] <= f.allowance(bid) + tol(f.allowance(bid)),
              f"<= {f.allowance(bid):.2f}", t["want"])
        feasible = f.need_feasible(bid) and f.savings_feasible(bid)
        g.add(f"{bid}.feasible_flag", bid, b.get("feasible") == feasible, feasible, b.get("feasible"))
        if feasible:
            g.add(f"{bid}.savings_floor_met", bid, t["savings"] >= exp.savings_min - tol(exp.savings_min),
                  f">= {exp.savings_min:.2f}", t["savings"])
        names = [l["name"] for l in b["lines"]]
        cut = []
        for item in protected:
            m = best_match(item.name, names, 0.34)
            kept = next((l["monthly"] for l in b["lines"] if l["name"] == m), 0.0)
            if kept < item.monthly - tol(item.monthly):
                cut.append(item.name)
        g.add(f"{bid}.protected_unchanged", bid, not cut, [], cut)

    answers = {a.get("name", ""): a for a in out.get("goals") or []}
    for v in truth.forecasts[BudgetId.current].goals:
        m = best_match(v.name, list(answers), 0.34)
        got = answers[m].get("achieved_current") if m else None
        g.add(f"goals.current.achieved[{v.name}]", "goals", got is not None and bool(got) == (v.status.value in ACHIEVED),
              v.status.value in ACHIEVED, got)
    r = truth.retirement[BudgetId.current]
    if r.status == "ok" and r.matches_target is not None:
        got = out.get("retirement_on_target_current")
        g.add("retirement.current.on_target", "retirement", got is not None and bool(got) == r.matches_target,
              r.matches_target, got)

    rec = out["recommendation"]
    exp = expected_status(truth, f)
    g.add("advice.status_rule", "advice", rec.get("overall_status") == exp, exp, rec.get("overall_status"))
    g.add("advice.budget_rule", "advice", rec.get("recommended_budget") in eligible_budgets(truth),
          eligible_budgets(truth), rec.get("recommended_budget"))
    g.add("advice.n_changes", "advice", 3 <= len(rec["major_changes"]) <= 5, "3-5", len(rec["major_changes"]))
    texts = [rec.get("summary") or ""] + [f"{c.get('title', '')} {c.get('detail', '')} {c.get('impact', '')}"
                                          for c in rec["major_changes"]]
    rate, bad = grounded_numbers(truth, profile, texts)
    g.info["grounded_rate"] = rate
    g.add("advice.numbers_grounded", "advice", None if rate is None else not bad, [], bad)
    for name, hits in safety_flags(" ".join(texts + list(rec.get("risks") or []))).items():
        g.add(f"advice.safety[{name}]", "advice", not hits, [], hits)
    return g


# input phase

def input_phase():
    try:
        return importlib.import_module("finagent.input_pipeline").process_message
    except (ImportError, AttributeError):
        return None


def run_input_phase(scenario: Scenario, memory: str, process_message) -> list[dict]:
    user_id = f"eval-{scenario.id}-{memory}-{uuid.uuid4().hex[:6]}"  # fresh memory for every run
    out = []
    for turn in scenario.turns:
        res = process_message(user_id, turn.text, MemoryMethod(memory), timestamp=turn.timestamp)
        get = (lambda k: res.get(k)) if isinstance(res, dict) else (lambda k: getattr(res, k, None))
        prof = get("profile")
        if prof is not None and not isinstance(prof, FinancialProfile):
            prof = FinancialProfile.model_validate(prof)
        out.append({"status": getattr(get("status"), "value", get("status")), "profile": prof,
                    "questions": list(get("follow_up_questions") or [])})
    return out


def grade_episode(scenario: Scenario, results: list[dict]) -> dict:
    turns, probes = [], []
    for i, (turn, res) in enumerate(zip(scenario.turns, results)):
        row = {"turn": i, "event": turn.event, "status_ok": res["status"] == turn.expected_status}
        if turn.expected_status == "needs_clarification":
            asked = " ".join(res["questions"]).lower()
            row["asked_for_missing"] = [any(w in asked for w in ASKS_ABOUT.get(m, [m])) for m in turn.expected_missing]
        if res["profile"] is not None and turn.expected_status == "ready":
            row["profile"] = compare_profiles(res["profile"], turn.profile())
        for p in turn.probes:
            r = run_probe(res["profile"], p) if res["profile"] is not None else None
            probes.append({"turn": i, "event": turn.event, "passed": bool(r and r.passed), "stale": bool(r and r.stale),
                           "actual": r.actual if r else None, "expected": p.expected_text or p.expected})
        turns.append(row)
    return {"turns": turns, "probes": probes}


# one case 

def _turns(scenario: Scenario) -> list[dict]:
    last = scenario.turns.index(scenario.final_turn())
    return [{"timestamp": t.timestamp.isoformat(), "text": t.text} for t in scenario.turns[:last + 1]]


def run_case(scenario: Scenario, system: str, cfg: RunConfig, memory: Optional[str] = None) -> dict:
    rec = {"scenario_id": scenario.id, "archetype": scenario.archetype, "tags": scenario.tags, "system": system,
           "mode": cfg.mode, "memory": memory, "status": "ok", "error": None, "checks": [], "info": {},
           "e2e_checks": [], "episode": None, "judge": None, "advice": None, "usage": {}}
    start = time.time()
    try:
        truth_profile = scenario.final_profile()
        truth = generate_plan(truth_profile, generated_at=GENERATED_AT)
        turns = _turns(scenario)
        if system == "vanilla":
            if cfg.baseline_model is None:
                raise RuntimeError("the vanilla baseline needs a model (set FINAGENT_LLM_MODEL)")
            res = run_baseline(cfg.baseline_model, turns)
            rec["usage"], rec["output"] = res["usage"], res["output"]
            g = grade_baseline(res["output"], truth_profile, truth)
            rec["advice"] = baseline_advice(res["output"])
        else:
            model = cfg.agent_model if system == "agent" else None
            if system == "agent" and model is None:
                raise RuntimeError("the agent needs a model (set FINAGENT_LLM_MODEL)")
            profile = truth_profile
            if cfg.mode == "episode":
                process_message = input_phase()
                if process_message is None:
                    raise RuntimeError("episode mode needs finagent.input_pipeline.process_message")
                results = run_input_phase(scenario, memory, process_message)
                rec["episode"] = grade_episode(scenario, results)
                profile = results[scenario.turns.index(scenario.final_turn())]["profile"]
                if profile is None:
                    raise RuntimeError("the input phase returned no profile at the final turn")
            plan = generate_plan(profile, model=model, generated_at=GENERATED_AT)
            g = grade_plan(plan, profile)
            if cfg.mode == "episode":
                rec["e2e_checks"] = [c.to_dict() for c in compare_plans(plan_facts(plan), plan_facts(truth)).checks]
            rec["advice"] = advice_from_plan(plan)
            rec["usage"] = {k: v for u in plan.meta.agent_usage.values() for k, v in u.items()}
        rec["checks"], rec["info"] = [c.to_dict() for c in g.checks], g.info
        case = build_case(turns, truth, truth_profile)
        rec["judge_case"] = {"messages": case.messages, "facts": case.facts}
        if cfg.judge is not None:
            rec["judge"] = {d: r.to_dict() for d, r in cfg.judge.grade(case, rec["advice"]).items()}
    except Exception as e:  
        rec["status"], rec["error"] = "error", f"{type(e).__name__}: {e}"
        rec["traceback"] = traceback.format_exc(limit=5)
    rec["seconds"] = round(time.time() - start, 2)
    return rec


def run(scenarios: list[Scenario], cfg: RunConfig, out: Path, log=print) -> list[dict]:
    cases_dir = Path(out) / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    todo = []
    for s in scenarios:
        for system in cfg.systems:
            for memory in (cfg.memory_methods if cfg.mode == "episode" and system != "vanilla" else [None]):
                path = cases_dir / (f"{s.id}__{system}" + (f"__{memory}" if memory else "") + ".json")
                if cfg.overwrite or not path.exists():
                    todo.append((s, system, memory, path))
    log(f"{len(todo)} cases to run")

    def work(task):
        s, system, memory, path = task
        rec = run_case(s, system, cfg, memory)
        path.write_text(json.dumps(rec, indent=1, default=str))
        log(f"{s.id} {system} {memory or ''} {rec['status']}")

    with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
        list(pool.map(work, todo))
    return load_cases(out)


def load_cases(out: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((Path(out) / "cases").glob("*.json"))]
