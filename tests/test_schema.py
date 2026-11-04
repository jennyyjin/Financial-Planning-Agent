import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from finagent.schema import (
    SCHEMA_PATH,
    FinancialProfile,
    Frequency,
    Income,
    RecurringExpense,
    SavingsAmount,
    json_schema,
    monthly_amount,
)

PROFILES_DIR = Path(__file__).resolve().parents[1] / "examples" / "profiles"


def load(name: str) -> dict:
    return json.loads((PROFILES_DIR / name).read_text())


@pytest.mark.parametrize("name", ["complete_profile.json", "incomplete_profile.json"])
def test_example_profiles_validate(name):
    profile = FinancialProfile.model_validate(load(name))
    # Round-trip through JSON without losing information.
    assert FinancialProfile.model_validate_json(profile.model_dump_json()) == profile


@pytest.mark.parametrize(
    "frequency, expected",
    [
        (Frequency.weekly, 1200 * 52 / 12),
        (Frequency.biweekly, 1200 * 26 / 12),
        (Frequency.semimonthly, 2400),
        (Frequency.monthly, 1200),
        (Frequency.quarterly, 400),
        (Frequency.annual, 100),
    ],
)
def test_monthly_amount_frequencies(frequency, expected):
    item = RecurringExpense(name="x", amount=1200, frequency=frequency)
    assert monthly_amount(item) == pytest.approx(expected)


def test_monthly_amount_unknown_returns_none():
    assert monthly_amount(Income(source="job", amount=None, frequency=Frequency.monthly)) is None
    assert monthly_amount(Income(source="job", amount=5000, frequency=None)) is None


def test_monthly_amount_savings():
    assert monthly_amount(SavingsAmount(type="amount", value=100, frequency="weekly")) == pytest.approx(100 * 52 / 12)
    assert monthly_amount(SavingsAmount(type="percent_of_income", value=20)) is None


def test_unknown_fields_rejected():
    data = load("complete_profile.json")
    data["income"][0]["bonus"] = 1000
    with pytest.raises(ValidationError):
        FinancialProfile.model_validate(data)


def test_invalid_enum_rejected():
    data = load("complete_profile.json")
    data["recurring_expenses"][0]["category"] = "rent"
    with pytest.raises(ValidationError):
        FinancialProfile.model_validate(data)


def test_negative_amount_rejected():
    data = load("complete_profile.json")
    data["recurring_expenses"][0]["amount"] = -5
    with pytest.raises(ValidationError):
        FinancialProfile.model_validate(data)


def test_one_time_expense_labels_default_to_unknown():
    item = FinancialProfile.model_validate(load("complete_profile.json")).one_time_expenses[0]
    assert (item.category.value, item.need_or_want.value) == ("shopping", "want")
    data = load("complete_profile.json")
    del data["one_time_expenses"][0]["category"], data["one_time_expenses"][0]["need_or_want"]
    item = FinancialProfile.model_validate(data).one_time_expenses[0]
    assert (item.category.value, item.need_or_want) == ("other", None)


def test_negative_desired_annual_spending_rejected():
    data = load("complete_profile.json")
    data["retirement"]["desired_annual_spending"] = -1
    with pytest.raises(ValidationError):
        FinancialProfile.model_validate(data)


def test_exported_schema_is_up_to_date():
    assert json.loads(SCHEMA_PATH.read_text()) == json_schema(), "Run: python -m finagent.schema"
