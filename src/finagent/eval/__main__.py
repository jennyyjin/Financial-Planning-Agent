"""Command line for the evaluation. Run `python -m finagent.eval -h` for the commands."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

from finagent.output.agents.runtime import DEFAULT_BASE_URL, OpenAICompatibleModel

REPO = Path(__file__).resolve().parents[3]
SCENARIOS = REPO / "eval" / "scenarios"


def make_model(prefix: str, temperature: float = 0.0):
    env = lambda k: os.environ.get(f"{prefix}_{k}") or os.environ.get(f"FINAGENT_LLM_{k}")
    if not env("MODEL"):
        return None
    return OpenAICompatibleModel(env("MODEL"), base_url=env("BASE_URL") or DEFAULT_BASE_URL,
                                 api_key=env("API_KEY") or os.environ.get("OPENAI_API_KEY"),
                                 temperature=temperature, timeout=120)


def pick(scenarios, limit, filter_, seed=0):
    if filter_:
        scenarios = [s for s in scenarios if filter_ in s.id or filter_ in s.tags]
    if limit:
        scenarios = sorted(random.Random(seed).sample(scenarios, min(limit, len(scenarios))), key=lambda s: s.id)
    return scenarios


def cmd_generate(a):
    from finagent.eval.data import generate, save_jsonl

    scenarios = generate(a.n, a.seed)
    print(f"Wrote {len(scenarios)} scenarios to {save_jsonl(scenarios, a.out)}")


def cmd_validate(a):
    from finagent.eval.checks import run_probe
    from finagent.eval.data import load_scenarios
    from finagent.eval.oracle import compute_facts

    scenarios = load_scenarios(a.paths)
    problems = []
    for s in scenarios:
        for i, t in enumerate(s.turns):
            for p in t.probes:
                if not run_probe(t.profile(), p).passed:
                    problems.append(f"{s.id} turn {i}: probe fails on its own expected profile")
                if i > 0 and run_probe(s.turns[i - 1].profile(), p).passed:
                    problems.append(f"{s.id} turn {i}: probe already passes before the update")
    facts = [compute_facts(s.final_profile()) for s in scenarios]
    stats = {
        "scenarios": len(scenarios), "by_source": dict(Counter(s.source for s in scenarios)),
        "by_archetype": dict(Counter(s.archetype for s in scenarios)),
        "update_events": dict(Counter(t.event for s in scenarios for t in s.turns[1:])),
        "probes": sum(len(t.probes) for s in scenarios for t in s.turns),
        "needs over 50%": sum(f.needs > 0.5 * f.income for f in facts),
        "needs over income": sum(f.needs > f.income for f in facts),
        "wants must be cut (50/30/20)": sum(f.expected_want_cut("50_30_20") > 0.01 for f in facts),
        "high-APR debt": sum(bool(f.high_apr_debts) for f in facts),
        "goal off track (current)": sum(any(g.status_current == "off_track" for g in f.goals) for f in facts),
        "no retirement age": sum(not f.has_retirement_age for f in facts),
    }
    print(json.dumps(stats, indent=1))
    if problems:
        print("\n".join(["", f"{len(problems)} problems:"] + problems))
        sys.exit(1)
    print("\nOK: every probe passes on its own turn and fails on the turn before.")


def cmd_run(a):
    from finagent.eval.data import load_scenarios
    from finagent.eval.judge import Judge
    from finagent.eval.report import write_report
    from finagent.eval.run import RunConfig, run

    scenarios = pick(load_scenarios(a.scenarios), a.limit, a.filter)
    systems = a.systems.split(",")
    agent = make_model("FINAGENT_LLM") if set(systems) & {"agent", "vanilla"} else None
    judge = None
    if a.judge:
        model = make_model("FINAGENT_JUDGE", temperature=0.0 if a.samples == 1 else 0.7)
        if model is None:
            sys.exit("--judge needs FINAGENT_JUDGE_MODEL or FINAGENT_LLM_MODEL")
        if agent is not None and model.name == agent.name:
            print("Warning: the judge is the same model as the agent, so it may favour its own writing.", file=sys.stderr)
        judge = Judge(model, n_samples=a.samples, examples=not a.no_examples)
    cfg = RunConfig(systems=systems, mode=a.mode, memory_methods=a.memory.split(","), agent_model=agent,
                    baseline_model=agent, judge=judge, workers=a.workers, overwrite=a.overwrite)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps({"args": vars(a) | {"fn": None}, "agent_model": getattr(agent, "name", None),
                                                 "judge_model": getattr(judge, "name", None)}, indent=1, default=str))
    print(f"Report: {write_report(run(scenarios, cfg, out), out)}")


def cmd_report(a):
    from finagent.eval.report import write_report
    from finagent.eval.run import load_cases

    print(write_report(load_cases(a.run), a.run).read_text())


def cmd_check_judge(a):
    from finagent.eval.data import load_scenarios
    from finagent.eval.judge import PERTURBATIONS, Judge, advice_from_plan, build_case, summarize_perturbations
    from finagent.eval.run import GENERATED_AT, _turns
    from finagent.output import generate_plan

    model = make_model("FINAGENT_JUDGE")
    if model is None:
        sys.exit("check-judge needs FINAGENT_JUDGE_MODEL or FINAGENT_LLM_MODEL")
    judge = Judge(model)
    rows = []
    for s in pick(load_scenarios(a.scenarios), a.limit, None):
        profile = s.final_profile()
        plan = generate_plan(profile, generated_at=GENERATED_AT)
        case, advice = build_case(_turns(s), plan, profile), advice_from_plan(plan)
        before = judge.grade(case, advice)
        for name, (target, change) in PERTURBATIONS.items():
            changed = change(advice, plan)
            if changed is None:
                continue
            after = judge.grade(case, changed)
            rows += [{"case": s.id, "perturbation": name, "target": target, "dimension": d,
                      "original": before[d].score, "perturbed": after[d].score} for d in before]
        print(f"graded {s.id}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = summarize_perturbations(rows)
    (out / "rows.json").write_text(json.dumps(rows, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


def cmd_rating_sheet(a):
    from finagent.eval.judge import JudgeCase, render, write_rating_sheet
    from finagent.eval.run import load_cases

    recs = [r for r in load_cases(a.run) if r["status"] == "ok"]
    by_system = {}
    for r in recs:
        by_system.setdefault(r["system"], []).append(r)
    rng = random.Random(0)
    per = max(1, a.n // max(1, len(by_system)))
    chosen = [r for rs in by_system.values() for r in rng.sample(rs, min(per, len(rs)))]
    items = [{"item_id": f"{r['scenario_id']}__{r['system']}", "text": render(JudgeCase(**r["judge_case"]), r["advice"])}
             for r in chosen]
    path = write_rating_sheet(items, Path(a.out), a.raters.split(","))
    print(f"Wrote {len(items)} items for each rater to {path}. Score 1-5 using prompts/eval/judge.md.")


def cmd_agreement(a):
    from finagent.eval.judge import human_agreement, read_ratings
    from finagent.eval.run import load_cases

    scores = {f"{r['scenario_id']}__{r['system']}": {d: v["score"] for d, v in r["judge"].items()}
              for r in load_cases(a.run) if r.get("judge")}
    result = human_agreement(read_ratings(a.ratings), scores)
    (Path(a.run) / "agreement.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m finagent.eval")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("generate", help="write synthetic scenarios")
    p.add_argument("--n", type=int, default=6, help="personas per archetype")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", default=str(SCENARIOS / "synthetic" / "v1.jsonl"))
    p.set_defaults(fn=cmd_generate)

    p = sub.add_parser("validate", help="check the scenarios and print what they cover")
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("run", help="run and grade systems, then write the report")
    p.add_argument("--scenarios", nargs="+", default=[str(SCENARIOS)])
    p.add_argument("--systems", default="deterministic")
    p.add_argument("--mode", choices=["profile", "episode"], default="profile")
    p.add_argument("--memory", default="full_history,rolling_summary")
    p.add_argument("--judge", action="store_true")
    p.add_argument("--samples", type=int, default=1, help="judge samples per dimension")
    p.add_argument("--no-examples", action="store_true", help="judge without the worked examples")
    p.add_argument("--limit", type=int)
    p.add_argument("--filter", help="only scenarios whose id contains this, or with this tag")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("report", help="rebuild the report for a run")
    p.add_argument("run")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("check-judge", help="perturbation test of the judge")
    p.add_argument("--scenarios", nargs="+", default=[str(SCENARIOS)])
    p.add_argument("--limit", type=int, default=12)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_check_judge)

    p = sub.add_parser("rating-sheet", help="blind rating sheet for human raters")
    p.add_argument("--run", required=True)
    p.add_argument("--raters", required=True)
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_rating_sheet)

    p = sub.add_parser("agreement", help="compare human ratings with the judge")
    p.add_argument("--run", required=True)
    p.add_argument("--ratings", nargs="+", required=True)
    p.set_defaults(fn=cmd_agreement)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
