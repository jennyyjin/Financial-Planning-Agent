"""The judge, the baseline, the runner and the report, using fake models (no network)."""

import json
from pathlib import Path

import pytest

from finagent.eval import run as run_mod
from finagent.eval.data import load_scenarios
from finagent.eval.judge import (
    DIMENSIONS, PERTURBATIONS, BadJudgment, Judge, JudgeCase, advice_from_plan, agreement, bootstrap_ci, build_case,
    build_facts, build_messages, human_agreement, load_examples, parse_judgment, read_ratings, spearman,
    summarize_perturbations, weighted_kappa, write_rating_sheet,
)
from finagent.eval.report import summarize, write_report
from finagent.eval.run import RunConfig, grade_baseline, run, run_baseline
from finagent.output import generate_plan
from finagent.output.agents.runtime import AssistantTurn
from finagent.output.models import BudgetId
from finagent.output.pipeline import load_profile

ROOT = Path(__file__).resolve().parents[2]


def judgment(dim, score, no=()):
    return json.dumps({"checklist": {i: {"answer": "no" if i in no else "yes", "evidence": "x"} for i in DIMENSIONS[dim].items},
                       "reasoning": "r", "score": score})


class FakeJudge:
    name = "fake-judge"

    def __init__(self, junk_first=0):
        self.scores = {"actionability": 4, "feasibility": 5, "tone": 3}
        self.junk, self.calls = junk_first, 0

    def chat(self, messages, tools):
        self.calls += 1
        if self.junk > 0:
            self.junk -= 1
            return AssistantTurn(text="Looks great to me!", usage={"total_tokens": 5})
        dim = next(d for d in DIMENSIONS if f"Grade the {d}" in messages[-1].content)
        return AssistantTurn(text=judgment(dim, self.scores[dim]), usage={"total_tokens": 10})


class FakeBaseline:
    name = "fake-baseline"

    def __init__(self, output, junk_first=False):
        self.output, self.junk = output, junk_first

    def chat(self, messages, tools):
        if self.junk:
            self.junk = False
            return AssistantTurn(text="{not json")
        return AssistantTurn(text=json.dumps(self.output), usage={"total_tokens": 100})


def copy_of_plan(plan):
    """A baseline answer that just copies the deterministic plan."""
    rec = plan.recommendation
    return {
        "budgets": {b.value: {"lines": [{"name": l.name, "bucket": l.bucket.value, "monthly": l.monthly_amount}
                                        for l in x.lines if l.bucket], "feasible": x.feasible} for b, x in plan.budgets.items()},
        "goals": [{"name": v.name, "achieved_current": v.status.value in ("met", "on_track"), "achieved_recommended": True}
                  for v in plan.forecasts[BudgetId.current].goals],
        "retirement_on_target_current": plan.retirement[BudgetId.current].matches_target,
        "recommendation": {"overall_status": rec.overall_status.value, "recommended_budget": rec.recommended_budget.value,
                           "summary": rec.summary, "major_changes": [c.model_dump() for c in rec.major_changes],
                           "strengths": rec.strengths, "risks": rec.risks},
    }


@pytest.fixture(scope="module")
def gold():
    return {s.id: s for s in load_scenarios(ROOT / "eval" / "scenarios" / "gold")}


@pytest.fixture(scope="module")
def over():
    profile = load_profile(ROOT / "tests/output/fixtures/overspender.json")
    return generate_plan(profile), profile


# judge

def test_parse_judgment_caps_and_rejects():
    out = parse_judgment(judgment("feasibility", 5, no=("F1",)), DIMENSIONS["feasibility"])
    assert out["score"] == 2 and out["raw_score"] == 5 and out["capped"]
    assert parse_judgment("```json\n" + judgment("tone", 4) + "\n```", DIMENSIONS["tone"])["score"] == 4
    with pytest.raises(BadJudgment):
        parse_judgment('{"checklist": {}, "score": 9}', DIMENSIONS["tone"])
    bad = json.loads(judgment("actionability", 3))
    bad["checklist"]["A1"]["answer"] = "na"
    with pytest.raises(BadJudgment, match="can't be 'na'"):
        parse_judgment(json.dumps(bad), DIMENSIONS["actionability"])


def test_worked_examples_are_valid_and_disagree_across_dimensions():
    scores = {}
    for ex in load_examples()["examples"]:
        for dim, j in ex["judgments"].items():
            assert parse_judgment(json.dumps(j), DIMENSIONS[dim])["score"] == j["score"]
            scores[ex["id"], dim] = j["score"]
    assert scores["specific_but_harsh_and_infeasible", "actionability"] > scores["warm_but_vague", "actionability"]
    assert scores["specific_but_harsh_and_infeasible", "tone"] < scores["warm_but_vague", "tone"]


def test_facts_and_messages(over):
    plan, profile = over
    facts = build_facts(plan, profile)
    assert any("Rent $2,200" in f for f in facts) and any("24% APR" in f for f in facts)
    assert any("OFF TRACK" in f for f in facts)
    msgs = build_messages(DIMENSIONS["tone"], JudgeCase([{"timestamp": "2026-10-28", "text": "hi"}], facts),
                          advice_from_plan(plan), load_examples())
    assert [m.role for m in msgs] == ["system"] + ["user", "assistant"] * 3 + ["user"]
    assert "## Tone" in msgs[0].content and "## Feasibility" not in msgs[0].content


def test_judge_retries_and_takes_the_median(over):
    plan, profile = over
    case = build_case([{"timestamp": "2026-10-28", "text": "hello"}], plan, profile)
    model = FakeJudge(junk_first=1)
    res = Judge(model, n_samples=3).grade(case, advice_from_plan(plan))
    assert [res[d].score for d in DIMENSIONS] == [4, 5, 3]
    assert len(res["actionability"].samples) == 3 and res["actionability"].spread == 0
    assert model.calls == 10


def test_judge_records_failures(over):
    plan, profile = over
    case = build_case([{"timestamp": "2026-10-28", "text": "hello"}], plan, profile)
    res = Judge(FakeJudge(junk_first=99), retries=1, dimensions=["tone"]).grade(case, advice_from_plan(plan))
    assert res["tone"].score is None and res["tone"].errors


def test_perturbations_only_change_a_copy(over):
    plan, _ = over
    advice = advice_from_plan(plan)
    before = json.dumps(advice)
    for name, (_, change) in PERTURBATIONS.items():
        assert change(advice, plan) not in (None, advice), name
    assert json.dumps(advice) == before
    assert "Rent" in PERTURBATIONS["cut_essentials"][1](advice, plan)["major_changes"][0]["title"]


def test_perturbation_summary():
    rows = [{"case": "a", "perturbation": "shaming", "target": "tone", "dimension": "tone", "original": 5, "perturbed": 2},
            {"case": "a", "perturbation": "shaming", "target": "tone", "dimension": "feasibility", "original": 4, "perturbed": 4},
            {"case": "b", "perturbation": "shaming", "target": "tone", "dimension": "tone", "original": 4, "perturbed": 4},
            {"case": "a", "perturbation": "padding", "target": "none", "dimension": "tone", "original": 4, "perturbed": 5}]
    s = summarize_perturbations(rows)
    assert s["shaming"]["detected"] == 0.5 and s["shaming"]["mean_drop"] == 1.5 and s["shaming"]["other_dims_change"] == 0
    assert s["padding"]["rose"] == 1.0


def test_agreement_stats(tmp_path):
    x = [1, 2, 3, 4, 5, 3, 2]
    assert agreement(x, x)["kappa"] == pytest.approx(1) and agreement(x, x)["exact"] == 1
    assert spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1)
    assert 0 < weighted_kappa([1, 2, 3, 4, 5], [2, 3, 4, 5, 5]) < 1
    lo, hi = bootstrap_ci([3, 4, 5, 4, 3, 5, 4])
    assert lo <= 4 <= hi
    sheet = write_rating_sheet([{"item_id": "c1__agent", "text": "a"}], tmp_path / "s.csv", ["r1", "r2"])
    rows = read_ratings([sheet])
    for r in rows:
        r.update(actionability=4, feasibility=3, tone=5)
    res = human_agreement(rows, {"c1__agent": {"actionability": 4, "feasibility": 3, "tone": 5}})
    assert res["tone"]["judge_vs_humans"]["exact"] == 1 and res["tone"]["between_humans"][0]["exact"] == 1


# baseline and runner

def test_baseline_grading(gold):
    profile = gold["gold-overspender-correction"].final_profile()
    plan = generate_plan(profile)
    out = copy_of_plan(plan)
    assert [c.id for c in grade_baseline(out, profile, plan).failures() if c.group != "advice"] == []
    for l in out["budgets"]["50_30_20"]["lines"]:
        if l["name"] == "Rent":
            l["monthly"] = 1500
    out["goals"][0]["achieved_current"] = True
    f = {c.id for c in grade_baseline(out, profile, plan).failures()}
    assert {"50_30_20.protected_unchanged", "50_30_20.needs_unchanged"} <= f
    assert any(x.startswith("goals.current.achieved") for x in f)


def test_baseline_retries_bad_json(gold):
    out = copy_of_plan(generate_plan(gold["gold-overspender-correction"].final_profile()))
    assert run_baseline(FakeBaseline(out, junk_first=True), [{"timestamp": "2026-10-28", "text": "hi"}])["output"]


def test_profile_mode_run_and_report(tmp_path, gold):
    scenarios = [gold["gold-overspender-correction"], gold["gold-inheritance-trip"]]
    out = copy_of_plan(generate_plan(scenarios[0].final_profile()))
    cfg = RunConfig(systems=["deterministic", "vanilla"], baseline_model=FakeBaseline(out), judge=Judge(FakeJudge()), workers=1)
    records = run(scenarios, cfg, tmp_path, log=lambda _: None)
    assert len(records) == 4 and all(r["status"] == "ok" for r in records)
    det = [r for r in records if r["system"] == "deterministic"]
    assert all(r["judge"]["feasibility"]["score"] == 5 for r in det)
    assert all(c["passed"] is not False for r in det for c in r["checks"] if c["group"] != "advice")
    assert len(run(scenarios, cfg, tmp_path, log=lambda _: None)) == 4  # resumes without rerunning
    text = write_report(records, tmp_path).read_text()
    assert "## Headline" in text and "vanilla" in text and "LLM judge" in text


def test_missing_model_is_recorded_as_an_error(tmp_path, gold):
    recs = run([gold["gold-inheritance-trip"]], RunConfig(systems=["agent"], workers=1), tmp_path, log=lambda _: None)
    assert recs[0]["status"] == "error" and "needs a model" in recs[0]["error"]


def test_episode_mode_finds_stale_memory(tmp_path, gold, monkeypatch):
    s = gold["gold-memory-stress"]
    by_text = {t.text: t for t in s.turns}

    def process_message(user_id, text, memory_method, timestamp=None):
        turn = by_text[text]
        profile = turn.profile()
        if memory_method.value == "rolling_summary" and turn.event == "reversal":
            profile = s.turns[s.turns.index(turn) - 1].profile()  # forgets the reversal
        return {"status": "ready", "profile": profile, "follow_up_questions": []}

    monkeypatch.setattr(run_mod, "input_phase", lambda: process_message)
    recs = run([s], RunConfig(systems=["deterministic"], mode="episode", workers=1), tmp_path, log=lambda _: None)
    by_memory = {r["memory"]: r for r in recs}
    assert all(p["passed"] for p in by_memory["full_history"]["episode"]["probes"])
    stale = [p for p in by_memory["rolling_summary"]["episode"]["probes"] if p["stale"]]
    assert [p["event"] for p in stale] == ["reversal"]
    assert not [c for c in by_memory["full_history"]["e2e_checks"] if not c["passed"]]
    summary = summarize(recs)["systems"]["deterministic / rolling_summary"]
    assert summary["memory"]["Stale values"] > 0 and summary["memory_by_event"]["reversal"][0] == 0
