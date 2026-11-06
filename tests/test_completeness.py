import json
from dataclasses import replace
from pathlib import Path

import pytest

from finagent.completeness import (
    DEFAULT_REQUIRED,
    MissingItem,
    confirmation_message,
    find_missing,
    follow_up_questions,
    template_questions,
)
from finagent.llm import UsageLog, scripted_model
from finagent.schema import FinancialProfile

PROFILES = Path(__file__).resolve().parents[1] / "examples" / "profiles"


def load(name: str) -> FinancialProfile:
    return FinancialProfile.model_validate_json((PROFILES / name).read_text())


def summary(missing: list[MissingItem]) -> list[tuple[str, list[str]]]:
    return [(m.path, m.fields) for m in missing]


def test_complete_profile_has_nothing_missing():
    assert find_missing(load("complete_profile.json")) == []


def test_incomplete_profile_missing_fields():
    assert summary(find_missing(load("incomplete_profile.json"))) == [
        ("/income/0", ["is_net"]),
        ("/recurring_expenses/2", ["amount"]),
        ("/debts/0", ["apr", "minimum_payment"]),
        ("/savings_goals/0", ["deadline_or_contribution"]),
    ]


def test_narrative_profile_missing_fields():
    # Daisy's example story leaves the student loan details and retirement ages out.
    assert summary(find_missing(load("narrative_profile.json"))) == [
        ("/debts/0", ["balance", "apr"]),
        ("/retirement", ["current_age", "target_retirement_age"]),
    ]


def test_no_income_is_missing():
    profile = load("complete_profile.json").model_copy(update={"income": []})
    assert summary(find_missing(profile)) == [("/income", ["income"])]


def test_retirement_not_required_when_not_mentioned():
    profile = load("complete_profile.json").model_copy(update={"retirement": None})
    assert find_missing(profile) == []


def test_required_fields_are_configurable():
    looser = replace(DEFAULT_REQUIRED, debts=("balance",), goal_needs_deadline_or_contribution=False)
    assert summary(find_missing(load("incomplete_profile.json"), looser)) == [
        ("/income/0", ["is_net"]),
        ("/recurring_expenses/2", ["amount"]),
    ]


def test_template_questions():
    questions = template_questions(find_missing(load("incomplete_profile.json")))
    assert questions[0] == 'For "Barista job", could you tell me whether that\'s before or after tax?'
    assert questions[2] == 'For "Credit card", could you tell me the interest rate (APR) and the minimum monthly payment?'
    narrative = template_questions(find_missing(load("narrative_profile.json")))
    assert narrative[1] == "For retirement planning, could you tell me your current age and the age you'd like to retire?"
    assert template_questions([MissingItem("/income", "income", "income", ["income"])]) == [
        "Could you tell me how much you earn, how often you're paid, and whether that's before or after tax?"
    ]


def test_no_missing_means_no_questions():
    assert follow_up_questions([], scripted_model("unused")).questions == []


def test_llm_questions_used_when_valid():
    missing = find_missing(load("narrative_profile.json"))
    reply = {"questions": ["What's left on your student loan, and its interest rate?", "How old are you, and when would you like to retire?"]}
    model = scripted_model(json.dumps(reply))
    log = UsageLog()
    result = follow_up_questions(missing, model, log=log)
    assert result.generated_by == "llm" and result.questions == reply["questions"]
    assert [u.tag for u in log.records] == ["follow_up"]
    sent = json.loads(model.calls[0][1].content)
    assert [m["path"] for m in sent["missing"]] == ["/debts/0", "/retirement"]
    assert sent["missing"][0]["fields"][0] == {"name": "balance", "meaning": "the remaining balance"}


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        json.dumps({"questions": ["only one question"]}),  # wrong count
        json.dumps({"questions": ["ok", "  "]}),  # empty question
        json.dumps({"qs": []}),
        "",
    ],
)
def test_falls_back_to_templates_on_bad_llm_reply(reply):
    missing = find_missing(load("narrative_profile.json"))
    result = follow_up_questions(missing, scripted_model(reply))
    assert result.generated_by == "template" and result.questions == template_questions(missing)


def test_no_model_uses_templates():
    missing = find_missing(load("narrative_profile.json"))
    assert follow_up_questions(missing).generated_by == "template"


def test_confirmation_message_complete_profile():
    assert confirmation_message(load("complete_profile.json")) == (
        "Got it. I've recorded income of $6,200/month after tax from Software engineer salary; "
        "4 recurring expenses totalling $2,803/month; 1 upcoming one-time cost; "
        "1 savings goal (Japan trip); 1 debt with $18,000 owed; $400/month toward retirement; "
        "$6,500 in savings. Here are your budgets."
    )


def test_confirmation_message_minimal_profile():
    profile = load("complete_profile.json").model_copy(update={
        "recurring_expenses": [], "one_time_expenses": [], "savings_goals": [], "debts": [],
        "retirement": None, "current_savings_balance": None,
    })
    assert confirmation_message(profile) == (
        "Got it. I've recorded income of $6,200/month after tax from Software engineer salary. "
        "Here are your budgets."
    )
