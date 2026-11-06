import json
import sys
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from finagent.output.models import BudgetId
from finagent.output.pipeline import generate_plan, load_profile
from finagent.web import extract as extract_mod
from finagent.web.extract import example_text, extract
from finagent.web.server import make_server
from finagent.web.view import ORDER, build_view

ROOT = Path(__file__).resolve().parents[2]
PROFILES = [
    ROOT / "examples" / "profiles" / "narrative_profile.json",
    ROOT / "examples" / "profiles" / "complete_profile.json",
    ROOT / "tests" / "output" / "fixtures" / "overspender.json",
    ROOT / "tests" / "output" / "fixtures" / "big_goals.json",
    ROOT / "tests" / "output" / "fixtures" / "no_retirement.json",
]


@pytest.fixture(scope="module")
def narrative_plan():
    return generate_plan(load_profile(PROFILES[0]))


# --- view model ----------------------------------------------------------------


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.stem)
def test_view_numbers_come_from_the_plan(path):
    plan = generate_plan(load_profile(path))
    view = build_view(plan)
    json.dumps(view)  # serializable as-is
    assert [b["id"] for b in view["budgets"]] == [bid.value for bid in ORDER]
    assert sum(b["recommended"] for b in view["budgets"]) == 1
    for card in view["budgets"]:
        totals = plan.budgets[BudgetId(card["id"])].totals
        by_key = {s["key"]: s["amount"] for s in card["slices"]}
        assert by_key["need"] == totals.need and by_key["savings"] == totals.savings
        assert by_key["unallocated"] == max(totals.unallocated, 0.0)
        assert len(card["goals"]) == len(plan.forecasts[BudgetId(card["id"])].goals)
    assert view["essentials"]["amounts"] == [plan.budgets[bid].totals.need for bid in ORDER]
    for group in view["groups"]:
        for row in group["rows"]:
            assert len(row["cells"]) == 3 and not row["cells"][0]["changed"]


def test_narrative_view_matches_the_design_story(narrative_plan):
    view = build_view(narrative_plan)
    assert view["headline"] == "Both of your goals are within reach."
    recommended = next(b for b in view["budgets"] if b["recommended"])
    assert recommended["id"] == "goals_5_3"
    assert all(g["ok"] for g in recommended["goals"])
    current = view["budgets"][0]
    assert current["goals"][1]["status"].startswith("Not started, $6,000 to go")
    labels = [s["label"] for s in view["suggestions"]]
    assert labels == ["Japan trip, each month", "Moving deposit, each month", "Less on wants, each month"]
    assert view["note"]["title"].startswith("Pay down Credit card")
    assert view["essentials"]["description"].startswith("Rent, utilities, groceries and minimum debt payments.")


def test_changed_cells_carry_a_readable_rationale(narrative_plan):
    view = build_view(narrative_plan)
    wants = next(g for g in view["groups"] if g["key"] == "want")
    dining = next(r for r in wants["rows"] if r["name"] == "Dining out")
    assert dining["cells"][2]["changed"] and dining["cells"][2]["tip"]
    assert not dining["cells"][2]["tip"].startswith("Fallback rule")
    yoga = next(r for r in wants["rows"] if r["name"] == "Yoga membership")
    assert not any(c["changed"] for c in yoga["cells"])
    left = next(g for g in view["groups"] if g["key"] == "unallocated")["rows"][0]
    assert left["cells"][0]["amount"] == pytest.approx(302) and left["cells"][1]["tip"]


# --- extraction hook ------------------------------------------------------------------


def test_extract_example_text_and_json():
    assert extract(example_text()).status == "ready"
    # whitespace and curly quotes don't matter
    assert extract("  " + example_text().replace("’", "'").replace(". ", ".\n")).status == "ready"
    assert extract(PROFILES[1].read_text()).profile.meta.user_id == "u_001"


def test_extract_reports_what_it_cannot_do():
    assert extract("").status == "invalid"
    assert extract('{"meta": {}}').status == "invalid"
    assert extract("I earn 4k a month").status == "unavailable"


def test_extract_uses_the_input_phase_when_it_exists(monkeypatch):
    calls = []

    def process_message(user_id, text, memory_method):
        calls.append((user_id, text, memory_method))
        if "rent" not in text:
            return {"status": "needs_clarification", "follow_up_questions": ["How much is your rent?"],
                    "confirmation_message": None, "profile": None}
        return {"status": "ready", "profile": json.loads(PROFILES[1].read_text()),
                "confirmation_message": "Got it.", "follow_up_questions": []}

    monkeypatch.setitem(sys.modules, "finagent.input_pipeline", types.SimpleNamespace(process_message=process_message))
    asked = extract("I earn 4k", user_id="u1")
    assert asked.status == "needs_clarification" and asked.questions == ["How much is your rent?"]
    done = extract("rent is 1800", user_id="u1")
    assert done.status == "ready" and done.profile.meta.user_id == "u_001" and done.message == "Got it."
    assert [c[0] for c in calls] == ["u1", "u1"]


def test_input_phase_missing_falls_back(monkeypatch):
    monkeypatch.setattr(extract_mod, "_input_phase", lambda: None)
    assert extract(example_text()).status == "ready"


# --- server -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_url():
    server = make_server(port=0, quiet=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_server_serves_the_page_and_assets(base_url):
    status, ctype, body = _get(base_url + "/")
    assert status == 200 and ctype.startswith("text/html") and b"Budget planner" in body
    for asset in ("app.js", "app.css"):
        assert _get(f"{base_url}/static/{asset}")[0] == 200
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(base_url + "/static/../server.py")
    assert err.value.code == 404


def test_server_plans_from_text(base_url):
    text = json.loads(_get(base_url + "/api/example")[2])["text"]
    status, data = _post(base_url + "/api/plan", {"text": text})
    assert status == 200 and data["status"] == "ready"
    assert data["view"]["income"] == 5000
    assert "trace" not in data["plan"] and set(data["plan"]["budgets"]) == {"current", "50_30_20", "goals_5_3"}


def test_server_rejects_bad_requests(base_url):
    assert _post(base_url + "/api/plan", {"nope": 1})[0] == 400
    status, data = _post(base_url + "/api/plan", {"text": "I earn 4k"})
    assert status == 422 and data["status"] == "unavailable" and data["message"]


# --- live progress ------------------------------------------------------------------------


def _stream(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        assert r.headers.get("Content-Type") == "application/x-ndjson"
        return [json.loads(line) for line in r.read().decode().splitlines()]


def test_stream_sends_progress_lines_then_the_same_result_as_api_plan(base_url):
    text = json.loads(_get(base_url + "/api/example")[2])["text"]
    lines = _stream(base_url + "/api/plan/stream", {"text": text})
    progress, result = lines[:-1], lines[-1]
    assert [p["label"] for p in progress] == [
        "Reading what you wrote", "Reading your numbers", "Building the 50/30/20 budget",
        "Building the goals + 5:3 budget", "Forecasting the next 12 months", "Projecting retirement",
        "Checking the key ratios", "Writing the recommendation",
    ]
    assert all(p["type"] == "progress" for p in progress) and result["type"] == "result"
    plain = _post(base_url + "/api/plan", {"text": text})[1]
    streamed = {k: v for k, v in result.items() if k != "type"}
    for body in (streamed, plain):
        del body["plan"]["meta"]["generated_at"]
    assert streamed == plain  # same body, deterministic run


def test_stream_reports_a_failed_input_as_a_result_line(base_url):
    lines = _stream(base_url + "/api/plan/stream", {"text": "I earn 4k"})
    assert lines[-1]["type"] == "result" and lines[-1]["status"] == "unavailable"


def test_stream_rejects_a_bad_request_with_plain_json(base_url):
    assert _post(base_url + "/api/plan/stream", {"nope": 1})[0] == 400


def test_progress_wording_for_agent_steps():
    from finagent.output.models import TraceStep
    from finagent.web.progress import describe

    def step(tool, args, result, agent="restructurer:50_30_20"):
        return describe({"kind": "step", "step": TraceStep(agent=agent, step=1, tool=tool, arguments=args, result=result)})

    cut = step("adjust_item", {"line_id": "rec:eating-out", "new_monthly_amount": 120.4, "rationale": "Why."}, {"ok": 1})
    assert cut == {"type": "progress", "agent": "restructurer:50_30_20", "who": "Restructurer (50/30/20)",
                   "label": "Cutting eating out to $120", "detail": "Why."}
    assert step("adjust_item", {"line_id": "rec:rent", "new_monthly_amount": 0}, {"ok": 1})["label"] == "Dropping rent"
    rejected = step("adjust_item", {"line_id": "rec:rent", "new_monthly_amount": 1}, {"error": "'Rent' is protected."})
    assert "rejected by the rules" in rejected["label"] and rejected["detail"] == "'Rent' is protected."
    assert step("compare_budgets", {}, {}, agent="advisor")["who"] == "Advisor"
    assert step("some_new_tool", {}, {})["label"] == "Some new tool"
    assert describe({"kind": "other"}) is None
    # Rejected recommendation drafts are retried internally and not shown.
    assert step("submit_recommendation", {"summary": "x"}, {"error": "overall_status must be ..."}, agent="advisor") is None
    assert step("submit_recommendation", {"summary": "x"}, {"accepted": False, "error": "Recommendation rejected"},
                agent="advisor") is None
    assert step("submit_recommendation", {"summary": "Done."}, {"accepted": True}, agent="advisor")["label"] == \
        "Submitting the recommendation"
