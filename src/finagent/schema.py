"""FinancialProfile: the structured user financial information passed from the
input phase to the output phase.

Amounts are stored exactly as the user stated them (amount + frequency).
Use `monthly_amount()` to normalize; the LLM never does the arithmetic.
Unknown values are null; the completeness check decides what must be asked.

Regenerate the exported JSON schema with:  python -m finagent.schema
"""

from __future__ import annotations

import json
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Union

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "financial_profile.schema.json"


class Frequency(str, Enum):
    weekly = "weekly"
    biweekly = "biweekly"
    semimonthly = "semimonthly"
    monthly = "monthly"
    quarterly = "quarterly"
    annual = "annual"


# Occurrences per month for each frequency.
MONTHLY_FACTOR: dict[Frequency, float] = {
    Frequency.weekly: 52 / 12,
    Frequency.biweekly: 26 / 12,
    Frequency.semimonthly: 2,
    Frequency.monthly: 1,
    Frequency.quarterly: 1 / 3,
    Frequency.annual: 1 / 12,
}


class ExpenseCategory(str, Enum):
    housing = "housing"
    utilities = "utilities"
    groceries = "groceries"
    dining_out = "dining_out"
    transport = "transport"
    insurance = "insurance"
    healthcare = "healthcare"
    subscriptions = "subscriptions"
    fitness = "fitness"
    entertainment = "entertainment"
    shopping = "shopping"
    personal_care = "personal_care"
    education = "education"
    childcare = "childcare"
    travel = "travel"
    gifts_donations = "gifts_donations"
    other = "other"


class NeedOrWant(str, Enum):
    need = "need"
    want = "want"


class DebtType(str, Enum):
    student_loan = "student_loan"
    credit_card = "credit_card"
    auto_loan = "auto_loan"
    mortgage = "mortgage"
    personal_loan = "personal_loan"
    medical = "medical"
    other = "other"


class Priority(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class MemoryMethod(str, Enum):
    full_history = "full_history"
    rolling_summary = "rolling_summary"


class SavingsAmountType(str, Enum):
    amount = "amount"
    percent_of_income = "percent_of_income"


class _Model(BaseModel):
    # Reject unexpected keys so malformed LLM output fails validation.
    model_config = ConfigDict(extra="forbid")


class Meta(_Model):
    user_id: str
    as_of: datetime = Field(description="Timestamp of the latest user message reflected in this profile.")
    profile_version: int = Field(ge=1)
    memory_method: MemoryMethod


class SavingsAmount(_Model):
    type: SavingsAmountType
    value: float = Field(ge=0, description="Dollars if type is 'amount', percent (0-100) if 'percent_of_income'.")
    frequency: Frequency = Field(
        default=Frequency.monthly,
        description="How often the dollar amount is saved. Ignored for percent_of_income.",
    )


class Income(_Model):
    source: str
    amount: Optional[float] = Field(default=None, ge=0)
    frequency: Optional[Frequency] = None
    is_net: Optional[bool] = Field(default=None, description="True if take-home pay, False if gross, null if unknown.")
    effective_date: Optional[date] = None


class RecurringExpense(_Model):
    name: str
    amount: Optional[float] = Field(default=None, ge=0)
    frequency: Optional[Frequency] = None
    category: ExpenseCategory = ExpenseCategory.other
    need_or_want: Optional[NeedOrWant] = None
    is_fixed: Optional[bool] = Field(default=None, description="True if the amount is the same every period.")
    effective_date: Optional[date] = None


class OneTimeExpense(_Model):
    name: str
    amount: Optional[float] = Field(default=None, ge=0)
    expected_date: Optional[date] = None
    effective_date: Optional[date] = None


class SavingsGoal(_Model):
    name: str
    target_amount: Optional[float] = Field(default=None, ge=0)
    deadline: Optional[date] = None
    contribution: Optional[SavingsAmount] = None
    priority: Optional[Priority] = None
    effective_date: Optional[date] = None


class Debt(_Model):
    name: str
    type: DebtType = DebtType.other
    balance: Optional[float] = Field(default=None, ge=0)
    apr: Optional[float] = Field(default=None, ge=0, le=100, description="Annual percentage rate, e.g. 5.5 for 5.5%.")
    minimum_payment: Optional[float] = Field(default=None, ge=0, description="Minimum monthly payment in dollars.")
    effective_date: Optional[date] = None


class Retirement(_Model):
    current_age: Optional[int] = Field(default=None, ge=0, le=120)
    target_retirement_age: Optional[int] = Field(default=None, ge=0, le=120)
    current_balance: Optional[float] = Field(default=None, ge=0)
    monthly_contribution: Optional[float] = Field(default=None, ge=0)
    target: Optional[float] = Field(default=None, ge=0, description="Target retirement savings in dollars.")


class FinancialProfile(_Model):
    meta: Meta
    income: list[Income] = Field(default_factory=list)
    recurring_expenses: list[RecurringExpense] = Field(default_factory=list)
    one_time_expenses: list[OneTimeExpense] = Field(default_factory=list)
    savings_goals: list[SavingsGoal] = Field(default_factory=list)
    overall_savings_target: Optional[SavingsAmount] = None
    debts: list[Debt] = Field(default_factory=list)
    retirement: Optional[Retirement] = None
    current_savings_balance: Optional[float] = Field(default=None, ge=0)


def monthly_amount(item: Union[Income, RecurringExpense, SavingsAmount]) -> Optional[float]:
    """Normalize an item's amount to dollars per month.

    Returns None if the amount or frequency is unknown, or if the item is a
    percentage (which depends on income and is converted by the output phase).
    """
    if isinstance(item, SavingsAmount):
        if item.type is SavingsAmountType.percent_of_income:
            return None
        return item.value * MONTHLY_FACTOR[item.frequency]
    if item.amount is None or item.frequency is None:
        return None
    return item.amount * MONTHLY_FACTOR[item.frequency]


def json_schema() -> dict:
    return FinancialProfile.model_json_schema()


def export_json_schema(path: Path = SCHEMA_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_schema(), indent=2) + "\n")
    return path


if __name__ == "__main__":
    print(f"Wrote {export_json_schema()}")
