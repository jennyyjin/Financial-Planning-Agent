"""The demo flow in a real browser: story -> follow-up questions -> answer -> dashboard.

Uses a fake input-phase model and the deterministic output phase, so no API key is needed.
Skipped unless Playwright and Google Chrome are installed:

    pip install playwright && pytest tests/test_demo_flow.py
"""

from __future__ import annotations

import json
import threading

import pytest

playwright = pytest.importorskip("playwright.sync_api")

import finagent.input_pipeline as pipeline  # noqa: E402
from finagent.web.server import make_server  # noqa: E402
from test_input_pipeline import NARRATIVE, RoutedModel  # noqa: E402

QUESTIONS = ["What's left on your student loan, and its interest rate?", "How old are you, and when would you like to retire?"]


@pytest.fixture
def base_url(monkeypatch, tmp_path):
    answered = json.loads(json.dumps(NARRATIVE))
    answered["debts"][0].update(balance=18000, apr=5.5)
    answered["retirement"].update(current_age=27, target_retirement_age=62)
    rent_up = json.loads(json.dumps(answered))
    rent_up["recurring_expenses"][0]["amount"] = 1950
    model = RoutedModel(extract=[NARRATIVE, answered, rent_up], follow_up=[{"questions": QUESTIONS}])
    monkeypatch.setenv("FINAGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pipeline, "resolve_model", lambda m: model)
    server = make_server(port=0, quiet=True)  # model=None: deterministic budgets
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


@pytest.fixture
def page():
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception as exc:  # Chrome not installed
            pytest.skip(f"Chrome is not available: {exc}")
        page = browser.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        yield page
        browser.close()
        assert errors == []


def test_story_questions_answer_dashboard(base_url, page):
    page.goto(base_url)
    page.click("#example")
    page.wait_for_function("document.getElementById('situation').value.length > 50")
    page.click("#submit")

    page.wait_for_selector(".msg.agent .questions li")
    assert page.locator(".msg.agent .questions li").all_inner_texts() == QUESTIONS
    assert page.inner_text("#situation-label") == "Your answer"
    assert page.input_value("#situation") == ""

    page.fill("#situation", "$18,000 at 5.5%. I'm 27 and want to retire at 62.")
    page.click("#submit")
    page.wait_for_selector("#results:not([hidden]) .pie", timeout=30_000)
    assert page.locator("#results article").count() == 3
    assert page.locator(".msg").count() == 4  # story, questions, answer, confirmation
    assert page.inner_text("#snippet").startswith("I take home about $5,000")

    page.reload()  # the conversation and the dashboard both come back
    page.wait_for_selector("#results:not([hidden]) .pie")
    assert page.locator(".msg").count() == 4
    old_id = page.evaluate("sessionStorage.getItem('finagent.user_id')")
    page.click("#restart-closed")
    assert page.is_hidden("#results")
    assert page.locator(".msg").count() == 0
    assert page.evaluate("sessionStorage.getItem('finagent.user_id')") != old_id


def test_failed_message_is_put_back(base_url, page, monkeypatch):
    def down(m):
        raise RuntimeError("provider down")
    monkeypatch.setattr(pipeline, "resolve_model", down)
    page.goto(base_url)
    page.fill("#situation", "I earn $3,000 a month")
    page.click("#submit")
    page.wait_for_selector("#feedback .alert")
    assert page.input_value("#situation") == "I earn $3,000 a month"
    assert page.locator(".msg").count() == 0


def test_update_shows_a_summary_until_accepted_and_survives_reload(base_url, page):
    page.goto(base_url)
    page.click("#example")
    page.wait_for_function("document.getElementById('situation').value.length > 50")
    page.click("#submit")
    page.wait_for_selector(".msg.agent .questions li")
    page.fill("#situation", "$18,000 at 5.5%. I'm 27 and want to retire at 62.")
    page.click("#submit")
    page.wait_for_selector("#results:not([hidden]) .pie", timeout=30_000)
    assert page.locator("#update-note").count() == 0  # first plan: nothing to summarize

    page.reload()  # the dashboard comes back without another request
    page.wait_for_selector("#results:not([hidden]) .pie")

    # Edit can be cancelled: Back returns to the same dashboard and drops the unsent draft.
    assert page.is_hidden("#back")  # the card is folded; Back lives in the open card
    page.click("#edit")
    page.fill("#situation", "never mind")
    page.click("#back")
    page.wait_for_selector("#results:not([hidden]) .pie")
    page.click("#edit")
    assert page.input_value("#situation") == ""

    page.fill("#situation", "My rent went up to $1,950.")
    page.click("#submit")
    page.wait_for_selector("#update-note")
    assert "Rent (amount $1,650 → $1,950)" in page.inner_text("#update-note")

    page.reload()
    page.wait_for_selector("#update-note")  # still there until accepted
    page.click("#accept-update")
    assert page.locator("#update-note").count() == 0
    page.reload()
    page.wait_for_selector("#results:not([hidden]) .pie")
    assert page.locator("#update-note").count() == 0
