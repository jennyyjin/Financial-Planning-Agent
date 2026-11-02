"""Data models of the output phase.

`PlanOutput` is the output contract (Daisy -> Jenny, and later the front end).
All money is dollars per month unless a field says otherwise. Values keep full
float precision; rounding happens only when rendering text.

Regenerate the exported JSON schema with:  python -m finagent.output.models
"""

from __future__ import annotations

import json
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from finagent.output.config import PlanConfig
from finagent.schema import DebtType, ExpenseCategory, Priority

PLAN_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "plan_output.schema.json"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Enums -----------------------------------------------------------------


class BudgetId(str, Enum):
    current = "current"
    rule_50_30_20 = "50_30_20"
    goals_5_3 = "goals_5_3"


class Bucket(str, Enum):
    need = "need"
    want = "want"
    savings = "savings"


class LineKind(str, Enum):
    recurring = "recurring"                              # recurring expense (need or want)
    one_time = "one_time"                                # lump sum; amount is the full cost, not monthly
    debt_min = "debt_min"                                # minimum debt payment (need)
    debt_extra = "debt_extra"                            # payment above the minimum (savings)
    goal_contribution = "goal_contribution"              # into a savings-goal fund (savings)
    retirement_contribution = "retirement_contribution"  # into the retirement portfolio (savings)
    emergency_fund = "emergency_fund"                    # into cash until the reserve is built (savings)
    general_savings = "general_savings"                  # into cash (savings)


class Tier(str, Enum):
    protected = "protected"  # never changed by restructuring
    drop_only = "drop_only"  # keep or set to 0
    scalable = "scalable"    # any amount between 0 and the current amount
    savings = "savings"      # savings lines; only the surplus policy changes them (upward)


class AdjustmentAction(str, Enum):
    reduce = "reduce"
    drop = "drop"
    reclassify = "reclassify"  # one-time item marked discretionary
    allocate = "allocate"      # surplus allocated to a savings line


class DecidedBy(str, Enum):
    agent = "agent"
    policy = "policy"
    fallback = "fallback"


class GeneratedBy(str, Enum):
    as_stated = "as_stated"
    agent = "agent"
    fallback = "fallback"


class GoalStatus(str, Enum):
    met = "met"
    on_track = "on_track"
    off_track = "off_track"
    no_deadline = "no_deadline"


class OverallStatus(str, Enum):
    on_track = "on_track"
    needs_attention = "needs_attention"
    at_risk = "at_risk"


# --- Normalized profile ------------------------------------------------------


class PlanWarning(_Model):
    code: str
    message: str
    ref: Optional[str] = Field(default=None, description="JSON pointer into the profile, or a line id.")


class IncomeLine(_Model):
    id: str
    name: str
    monthly_amount: float
    is_net: Optional[bool] = None
    effective_date: Optional[date] = None
    source_ref: str


class BudgetLine(_Model):
    id: str = Field(description="Stable id, e.g. 'rec:eating-out', 'debt:federal-student-loan:min'.")
    name: str
    kind: LineKind
    bucket: Optional[Bucket] = Field(default=None, description="None for one-time items.")
    monthly_amount: float = Field(ge=0, description="Dollars per month; for one-time items, the full amount.")
    original_amount: float = Field(ge=0, description="Amount before restructuring.")
    tier: Tier
    category: Optional[ExpenseCategory] = None
    is_fixed: Optional[bool] = None
    expected_date: Optional[date] = Field(default=None, description="One-time items: month the cost falls in.")
    effective_date: Optional[date] = None
    target_ref: Optional[str] = Field(default=None, description="Goal or debt id this line pays into.")
    discretionary: bool = Field(default=False, description="One-time items reclassified as non-essential.")
    source_ref: Optional[str] = None


class GoalInfo(_Model):
    id: str  # 'goal:<slug>'
    name: str
    target_amount: Optional[float] = None
    deadline: Optional[date] = None
    stated_monthly: Optional[float] = Field(default=None, description="Stated contribution converted to monthly.")
    priority: Optional[Priority] = None
    source_ref: str


class DebtInfo(_Model):
    id: str  # 'debt:<slug>'
    name: str
    type: DebtType
    balance: float
    apr: Optional[float] = None
    minimum_payment: Optional[float] = None
    source_ref: str


class RetirementInfo(_Model):
    current_age: Optional[int] = None
    target_retirement_age: Optional[int] = None
    current_balance: float = 0.0
    monthly_contribution: float = 0.0
    target: Optional[float] = None
    desired_annual_spending: Optional[float] = None


class NormalizedProfile(_Model):
    user_id: str
    profile_version: int
    as_of: datetime
    start_month: date = Field(description="First day of the first forecast month (the month after as_of).")
    net_monthly_income: float
    income: list[IncomeLine]
    lines: list[BudgetLine] = Field(description="Monthly lines: expenses, debt minimums, stated savings.")
    one_time: list[BudgetLine]
    goals: list[GoalInfo]
    debts: list[DebtInfo]
    retirement: Optional[RetirementInfo] = None
    overall_savings_target_monthly: Optional[float] = None
    current_savings_balance: float = 0.0
    warnings: list[PlanWarning] = Field(default_factory=list)


# --- Budgets -------------------------------------------------------------------


class BucketTotals(_Model):
    need: float = 0.0
    want: float = 0.0
    savings: float = 0.0
    unallocated: float = 0.0


class BudgetTargets(_Model):
    need_max: float
    want_max: float
    savings_min: float


class Infeasibility(_Model):
    reason: str
    gaps: dict[str, float] = Field(description="Bucket -> dollars by which the target is missed.")


class Adjustment(_Model):
    line_id: str
    action: AdjustmentAction
    before_monthly: float
    after_monthly: float
    rationale: str
    rule: str = Field(description="e.g. '50_30_20.wants_ceiling', 'surplus.goal_shortfall'.")
    decided_by: DecidedBy


class Budget(_Model):
    id: BudgetId
    description: str
    income_monthly: float
    lines: list[BudgetLine]
    one_time: list[BudgetLine]
    totals: BucketTotals
    shares: Optional[BucketTotals] = Field(default=None, description="Fractions of income; None if income is 0.")
    targets: Optional[BudgetTargets] = None
    headroom: dict[str, float] = Field(default_factory=dict, description="Unused allowance: 'need', 'want'.")
    feasible: bool = True
    infeasibility: Optional[Infeasibility] = None
    unfunded_goals: list[str] = Field(default_factory=list)
    adjustments: list[Adjustment] = Field(default_factory=list)
    generated_by: GeneratedBy
    summary_rationale: Optional[str] = None


# --- Forecast --------------------------------------------------------------------


class ForecastMonth(_Model):
    month: date
    income: float
    needs: float
    wants: float
    one_time: float
    debt_payments: float
    goal_contributions: float
    retirement_contribution: float
    emergency_and_general_savings: float
    net_cash_flow: float = Field(description="Change in the cash balance this month.")
    cash_balance: float
    goal_balances: dict[str, float]
    debt_balances: dict[str, float]
    retirement_balance: float


class GoalVerdict(_Model):
    goal_id: str
    name: str
    status: GoalStatus
    target_amount: Optional[float] = None
    deadline: Optional[date] = None
    balance_at_deadline: Optional[float] = Field(default=None, description="Projected fund at the deadline.")
    shortfall: float = 0.0
    projected_completion_date: Optional[date] = None
    current_monthly: float = 0.0
    required_monthly: Optional[float] = Field(default=None, description="Monthly contribution needed from now.")


class EndState(_Model):
    month: date = Field(description="Last forecast month.")
    cash_balance: float
    goal_balances: dict[str, float]
    debt_balances: dict[str, float]
    retirement_balance: float


class Forecast(_Model):
    budget_id: BudgetId
    months: list[ForecastMonth]
    goals: list[GoalVerdict]
    cash_shortfall_months: list[date] = Field(default_factory=list)
    end_state: EndState


# --- Retirement ------------------------------------------------------------------


class RetirementProjection(_Model):
    budget_id: BudgetId
    status: Literal["ok", "insufficient_data"]
    missing: list[str] = Field(default_factory=list)
    retirement_number: Optional[float] = None
    number_source: Optional[Literal["desired_annual_spending", "target", "budget_spending"]] = None
    implied_annual_spending: Optional[float] = None
    investable_now: Optional[float] = Field(default=None, description="Investable assets at the end of the forecast.")
    monthly_investment: Optional[float] = Field(default=None, description="Monthly amount invested after the forecast.")
    reachable: Optional[bool] = None
    projected_retirement_age: Optional[float] = None
    projected_retirement_year: Optional[int] = None
    target_retirement_age: Optional[int] = None
    delta_years: Optional[float] = Field(default=None, description="Projected minus target age; negative = early.")
    matches_target: Optional[bool] = None
    required_monthly_to_hit_target: Optional[float] = None


# --- Metrics ---------------------------------------------------------------------


class BudgetMetrics(_Model):
    savings_rate: Optional[float] = None
    need_share: Optional[float] = None
    want_share: Optional[float] = None
    end_cash_balance: float
    goals_off_track: list[str]
    cash_shortfall_months: int
    retirement_delta_years: Optional[float] = None


class Metrics(_Model):
    net_monthly_income: float
    emergency_fund_months: Optional[float] = Field(default=None, description="Cash / monthly needs (current budget).")
    debt_total: float
    debt_to_income: Optional[float] = Field(default=None, description="Monthly debt minimums / net monthly income.")
    high_apr_debts: list[str]
    per_budget: dict[str, BudgetMetrics]


# --- Recommendation ----------------------------------------------------------------


class MajorChange(_Model):
    title: str
    detail: str
    impact: str
    evidence_refs: list[str] = Field(default_factory=list, description="Dotted paths into PlanOutput.")


class Recommendation(_Model):
    overall_status: OverallStatus
    summary: str
    recommended_budget: BudgetId
    major_changes: list[MajorChange]
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    generated_by: GeneratedBy


# --- Plan output ---------------------------------------------------------------------


class TraceStep(_Model):
    agent: str
    step: int
    tool: Optional[str] = None
    arguments: Optional[dict[str, Any]] = None
    result: Optional[Any] = None
    text: Optional[str] = None
    usage: Optional[dict[str, Any]] = None


class PlanMeta(_Model):
    user_id: str
    profile_version: int
    generated_at: datetime
    start_month: date
    config: PlanConfig
    model: Optional[str] = None
    warnings: list[PlanWarning] = Field(default_factory=list)


class PlanOutput(_Model):
    meta: PlanMeta
    budgets: dict[BudgetId, Budget]
    forecasts: dict[BudgetId, Forecast]
    retirement: dict[BudgetId, RetirementProjection]
    metrics: Metrics
    recommendation: Recommendation
    trace: list[TraceStep] = Field(default_factory=list)


def plan_json_schema() -> dict:
    return PlanOutput.model_json_schema()


def export_plan_json_schema(path: Path = PLAN_SCHEMA_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan_json_schema(), indent=2) + "\n")
    return path


if __name__ == "__main__":
    print(f"Wrote {export_plan_json_schema()}")
