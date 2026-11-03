import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from finagent import schema
from finagent.input_pipeline import InputResult
from finagent.diff import ProfileDiff
from finagent.scenarios import SECTIONS, check_profile, check_result, load_scenario, scenario_paths
from finagent.schema import FinancialProfile

PROFILES = Path(__file__).resolve().parents[1] / "examples" / "profiles"
COMPLETE = FinancialProfile.model_validate_json((PROFILES / "complete_profile.json").read_text())
ITEM_MODELS = {
    "income": schema.Income, "recurring_expenses": schema.RecurringExpense,
    "one_time_expenses": schema.OneTimeExpense, "savings_goals": schema.SavingsGoal, "debts": schema.Debt,
}


def _field_exists(model: type[BaseModel], dotted: str) -> bool:
    head, _, rest = dotted.partition(".")
    if head not in model.model_fields:
        return False
    if not rest:
        return True
    return _field_exists(schema.SavingsAmount, rest) if head == "contribution" else False


def test_there_are_scenarios():
    assert len(scenario_paths()) >= 5


@pytest.mark.parametrize("path", scenario_paths(), ids=lambda p: p.stem)
def test_scenario_is_well_formed(path):
    scenario = load_scenario(path)
    assert scenario["name"] == path.stem and scenario["description"]
    for turn in scenario["turns"]:
        assert turn["text"] and turn["timestamp"]
        assert turn.get("expected_status") in (None, "ready", "needs_clarification")
        for check in turn.get("checks", []):
            if "path" in check:
                assert set(check) == {"path", "equals"} and check["path"].startswith("/")
                continue
            assert check["section"] in SECTIONS and set(check) - {"section"} <= {"has", "lacks", "count"}
            for key in [*check.get("has", {}), *check.get("lacks", {})]:
                assert _field_exists(ITEM_MODELS[check["section"]], key), f"{check['section']}.{key}"


def test_check_profile_passes_and_fails():
    passing = [
        {"path": "/retirement/current_age", "equals": 24},
        {"section": "income", "has": {"amount": 6200, "is_net": True, "effective_date": "2026-11-01"}},
        {"section": "savings_goals", "has": {"contribution.value": 500.0}},
        {"section": "recurring_expenses", "lacks": {"amount": 999}},
        {"section": "debts", "count": 1},
    ]
    assert check_profile(COMPLETE, passing) == []
    failing = [
        {"path": "/retirement/current_age", "equals": 30},
        {"section": "income", "has": {"amount": 1}},
        {"section": "recurring_expenses", "lacks": {"amount": 1800}},
        {"section": "debts", "count": 2},
        {"path": "/income/5/amount", "equals": 1},
    ]
    assert len(check_profile(COMPLETE, failing)) == 5


def test_check_result_status_and_unchanged():
    result = InputResult(status="ready", profile=COMPLETE, diff=ProfileDiff(), confirmation_message="")
    assert check_result(result, {"expected_status": "ready", "expect_unchanged": True}) == []
    assert check_result(result, {"expected_status": "needs_clarification"}) == [
        "status: expected needs_clarification, got ready"
    ]
    no_plan = InputResult(status="ready", profile=COMPLETE, diff=None, confirmation_message="")
    assert check_result(no_plan, {"expect_unchanged": True}) == ["expected no changes, got no previous plan"]
    json.dumps(result.to_dict())
