"""Turn case records into summary.md and summary.json."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from finagent.eval.judge import DIMENSIONS, bootstrap_ci, spearman

GROUPS = ["current", "50_30_20", "goals_5_3", "goals", "retirement", "reprioritize", "advice", "e2e"]


def _mean(xs):
    xs = [float(x) for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _pct(x) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _table(header, rows) -> str:
    return "\n".join(["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] +
                     ["| " + " | ".join(map(str, r)) + " |" for r in rows])


def _checks(r):
    return [c for c in r["checks"] + r.get("e2e_checks", []) if c["passed"] is not None]


def _rate(recs, pred):
    return _mean(c["passed"] for r in recs for c in _checks(r) if pred(c["id"]))


def _case_rate(recs, groups):
    vals = []
    for r in recs:
        cs = [c for c in _checks(r) if c["group"] in groups]
        if cs:
            vals.append(all(c["passed"] for c in cs))
    return _mean(vals)


def summarize(records: list[dict]) -> dict:
    by_system = defaultdict(list)
    for r in records:
        by_system[r["system"] + (f" / {r['memory']}" if r.get("memory") else "")].append(r)
    summary = {"n_scenarios": len({r["scenario_id"] for r in records}), "systems": {}}
    for name, recs in sorted(by_system.items()):
        ok = [r for r in recs if r["status"] == "ok"]
        s = {"cases": len(recs), "errors": dict(Counter(r["error"].split(":")[0] for r in recs if r["status"] != "ok"))}
        s["headline"] = {
            "Budgets all correct": _case_rate(ok, ("current", "50_30_20", "goals_5_3")),
            "Q1 goal reached? correct": _rate(ok, lambda i: i.startswith("goals.") and ".achieved[" in i),
            "Q2 retirement checks": _rate(ok, lambda i: i.startswith("retirement.")),
            "Q3 cuts valid": _case_rate(ok, ("reprioritize",)),
            "Advice follows the numbers": _rate(ok, lambda i: i in ("advice.status_rule", "advice.budget_rule")),
            "Advice covers key issues": _rate(ok, lambda i: i.startswith("advice.covers")),
            "Numbers grounded (share)": _mean(r["info"].get("grounded_rate") for r in ok),
            "Safety lint clean": _rate(ok, lambda i: i.startswith("advice.safety")),
            "Fell back to rules": _mean(r["info"].get("fallback") for r in ok if "fallback" in r["info"]),
        }
        s["groups"] = {}
        for g in GROUPS:
            passed = [c["passed"] for r in ok for c in _checks(r) if c["group"] == g]
            if passed:
                s["groups"][g] = (_mean(passed), _case_rate(ok, (g,)))

        fails, examples = Counter(), defaultdict(list)
        for r in ok:
            for c in _checks(r):
                if not c["passed"]:
                    key = re.sub(r"\[.*?\]", "", c["id"])
                    fails[key] += 1
                    if len(examples[key]) < 2:
                        examples[key].append(f"{r['scenario_id']} (expected {c['expected']}, got {c['actual']})")
        s["top_failures"] = [(k, n, examples[k]) for k, n in fails.most_common(12)]

        s["judge"] = {}
        for dim in DIMENSIONS:
            graded = [r for r in ok if r.get("judge") and r["judge"][dim]["score"] is not None]
            if not graded:
                continue
            scores = [r["judge"][dim]["score"] for r in graded]
            items = defaultdict(list)
            for r in graded:
                for item, ans in r["judge"][dim]["checklist"].items():
                    if ans != "na":
                        items[item].append(ans == "yes")
            s["judge"][dim] = {
                "mean": _mean(scores), "ci": bootstrap_ci(scores),
                "capped": _mean(x["capped"] for r in graded for x in r["judge"][dim]["samples"]),
                "length_corr": spearman([len(json.dumps(r["advice"])) for r in graded], scores),
                "weakest_items": sorted(((k, _mean(v)) for k, v in items.items()), key=lambda kv: kv[1])[:3],
                "judge_errors": sum(bool(r["judge"][dim]["errors"]) for r in ok if r.get("judge")),
            }

        episodes = [r["episode"] for r in ok if r.get("episode")]
        if episodes:
            turns = [t for e in episodes for t in e["turns"]]
            probes = [p for e in episodes for p in e["probes"]]
            profiles = [t["profile"] for t in turns if t.get("profile")]
            s["memory"] = {
                "Ready/clarify status correct": _mean(t["status_ok"] for t in turns),
                "Asked for the missing field": _mean(x for t in turns for x in t.get("asked_for_missing", [])),
                "Item F1": _mean(p["item_f1"] for p in profiles),
                "Amount accuracy": _mean(p["amount_acc"] for p in profiles),
                "Need/want label accuracy": _mean(p["label_acc"] for p in profiles),
                "Exact profile": _mean(p["exact"] for p in profiles),
                "Update probes passed": _mean(p["passed"] for p in probes),
                "Stale values": _mean(p["stale"] for p in probes),
            }
            s["memory_by_event"] = {ev: (_mean(p["passed"] for p in probes if p["event"] == ev),
                                         _mean(p["stale"] for p in probes if p["event"] == ev))
                                    for ev in sorted({p["event"] for p in probes})}

        s["by_archetype"] = {}
        for arch in sorted({r["archetype"] for r in ok}):
            rs = [r for r in ok if r["archetype"] == arch]
            s["by_archetype"][arch] = [_case_rate(rs, tuple(g for g in GROUPS if g != "advice"))] + [
                _mean(r["judge"][d]["score"] for r in rs if r.get("judge")) for d in DIMENSIONS]
        summary["systems"][name] = s
    return summary


def to_markdown(s: dict) -> str:
    names = list(s["systems"])
    sy = s["systems"]
    out = [f"# Evaluation summary\n\n{s['n_scenarios']} scenarios.\n", "## Headline\n"]
    keys = list(next(iter(sy.values()))["headline"])
    rows = [[k] + [_pct(sy[n]["headline"][k]) for n in names] for k in keys]
    rows.append(["Run errors"] + [sum(sy[n]["errors"].values()) for n in names])
    out += [_table(["Metric"] + names, rows), "", "## Checks by group (check pass rate / all checks pass per case)\n"]
    rows = [[g] + [f"{_pct(sy[n]['groups'][g][0])} / {_pct(sy[n]['groups'][g][1])}" if g in sy[n]["groups"] else "-"
                   for n in names] for g in GROUPS if any(g in sy[n]["groups"] for n in names)]
    out += [_table(["Group"] + names, rows), ""]

    if any(sy[n]["judge"] for n in names):
        out.append("## LLM judge (1-5, mean with 95% CI)\n")
        rows = []
        for dim in DIMENSIONS:
            for n in names:
                d = sy[n]["judge"].get(dim)
                if d:
                    ci = f" [{d['ci'][0]:.2f}, {d['ci'][1]:.2f}]" if d["ci"] else ""
                    corr = "-" if d["length_corr"] is None else f"{d['length_corr']:.2f}"
                    weakest = ", ".join(f"{k} {100 * v:.0f}%" for k, v in d["weakest_items"])
                    rows.append([dim, n, f"{d['mean']:.2f}{ci}", _pct(d["capped"]), corr, weakest, d["judge_errors"]])
        out += [_table(["Dimension", "System", "Score", "Capped", "Length corr.", "Weakest items (yes rate)", "Judge errors"], rows), ""]

    mem = [n for n in names if sy[n].get("memory")]
    if mem:
        out.append("## Input phase and memory\n")
        keys = list(sy[mem[0]]["memory"])
        out += [_table(["Metric"] + mem, [[k] + [_pct(sy[n]["memory"][k]) for n in mem] for k in keys]), ""]
        events = sorted({e for n in mem for e in sy[n]["memory_by_event"]})
        rows = [[e] + [f"{_pct(sy[n]['memory_by_event'][e][0])} ({_pct(sy[n]['memory_by_event'][e][1])} stale)"
                       if e in sy[n]["memory_by_event"] else "-" for n in mem] for e in events]
        out += ["Update probes passed, by event:\n", _table(["Event"] + mem, rows), ""]

    out.append("## By archetype (code checks all pass, excluding advice · judge scores A/F/T)\n")
    archs = sorted({a for n in names for a in sy[n]["by_archetype"]})
    fmt = lambda v: "-" if v[0] is None else _pct(v[0]) + " · " + "/".join("-" if x is None else f"{x:.1f}" for x in v[1:])
    out += [_table(["Archetype"] + names, [[a] + [fmt(sy[n]["by_archetype"][a]) if a in sy[n]["by_archetype"] else "-"
                                               for n in names] for a in archs]), ""]

    out.append("## Most common failures\n")
    for n in names:
        out.append(f"**{n}**")
        out += [f"- `{k}` x{c}: " + "; ".join(ex) for k, c, ex in sy[n]["top_failures"]] or ["- none"]
        if sy[n]["errors"]:
            out.append(f"- run errors: {sy[n]['errors']}")
        out.append("")
    return "\n".join(out)


def write_report(records: list[dict], out: Path) -> Path:
    s = summarize(records)
    (Path(out) / "summary.json").write_text(json.dumps(s, indent=1, default=str))
    path = Path(out) / "summary.md"
    path.write_text(to_markdown(s))
    return path
