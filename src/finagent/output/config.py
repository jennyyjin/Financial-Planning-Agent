"""PlanConfig: every tunable assumption of the output phase in one place.

The config is copied into PlanOutput.meta so each plan records the
assumptions it was computed under.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from finagent.schema import ExpenseCategory

DEFAULT_PROTECTED_CATEGORIES: list[ExpenseCategory] = [
    ExpenseCategory.housing,
    ExpenseCategory.utilities,
    ExpenseCategory.healthcare,
    ExpenseCategory.insurance,
    ExpenseCategory.childcare,
    ExpenseCategory.education,
]


class PlanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    horizon_months: int = Field(default=12, ge=1, description="Length of the forecast window.")
    real_return: float = Field(default=0.05, description="Annual real return of the retirement portfolio.")
    cash_real_return: float = Field(default=0.0, description="Annual real return of cash savings.")
    withdrawal_rate: float = Field(default=0.04, gt=0, description="4% rule: retirement number = spending / rate.")
    max_age: int = Field(default=100, description="Retirement projections stop at this age.")
    count_cash_as_investable: bool = Field(
        default=True, description="Count cash (minus the emergency reserve) toward the retirement number."
    )
    redirect_freed_payments_to_retirement: bool = Field(
        default=True,
        description="After the forecast, goal contributions past their deadline and paid-off debt payments are invested.",
    )

    emergency_months: float = Field(default=3, ge=0, description="Emergency fund target in months of needs.")
    emergency_build_months: int = Field(default=12, ge=1, description="Months over which to build the emergency fund.")
    high_apr_threshold: float = Field(default=8.0, ge=0, description="Debts at or above this APR get extra payments.")

    protected_categories: list[ExpenseCategory] = Field(default_factory=lambda: list(DEFAULT_PROTECTED_CATEGORIES))
    need_trim_cap: float = Field(
        default=0.0, ge=0, le=1, description="Max fraction a variable need may be trimmed (0 = needs untouchable)."
    )
    min_rationale_chars: int = Field(default=20, ge=1)

    agent_max_steps: int = Field(default=25, ge=1)
    money_tolerance: float = Field(default=0.01, description="Dollar tolerance for target and invariant checks.")

    @property
    def retirement_multiple(self) -> float:
        return 1 / self.withdrawal_rate
