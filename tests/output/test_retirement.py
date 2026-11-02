from __future__ import annotations

from datetime import date, datetime

import pytest

from finagent.output.budget import build_current_budget
from finagent.output.config import PlanConfig
from finagent.output.models import (
    Bucket,
    BudgetId,
    BudgetLine,
    DebtInfo,
    EndState,
    Forecast,
    GoalInfo,
    LineKind,
    NormalizedProfile,
    RetirementInfo,
    Tier,
)
from finagent.output.retirement import (
    emergency_reserve,
    fv,
    monthly_rate,
    project_retirement,
    retirement_number,
)
from finagent.schema import DebtType

START = date(2026, 11, 1)
END = date(2027, 10, 1)


def line(id: str, kind: LineKind, bucket: Bucket, amount: float, target_ref: str | None = None) -> BudgetLine:
    tier = Tier.savings if bucket == Bucket.savings else Tier.protected
    return BudgetLine(id=id, name=id, kind=kind, bucket=bucket, monthly_amount=amount, original_amount=amount,
                      tier=tier, target_ref=target_ref)


def rent(amount: float = 2000) -> BudgetLine:
    return line("rec:rent", LineKind.recurring, Bucket.need, amount)


def ret_line(amount: float) -> BudgetLine:
    return line("ret:contribution", LineKind.retirement_contribution, Bucket.savings, amount)


def make_norm(lines, *, income=None, goals=(), debts=(), retirement=None, current_age=30, target_age=60,
              target=1_000_000.0) -> NormalizedProfile:
    if retirement is None:
        retirement = RetirementInfo(current_age=current_age, target_retirement_age=target_age, target=target)
    total = sum(l.monthly_amount for l in lines)
    return NormalizedProfile(
        user_id="u", profile_version=1, as_of=datetime(2026, 10, 28), start_month=START,
        net_monthly_income=total if income is None else income, income=[], lines=list(lines), one_time=[],
        goals=list(goals), debts=list(debts), retirement=retirement,
    )


def make_forecast(cash: float, retirement_balance: float, *, goals=None, debts=None,
                  budget_id: BudgetId = BudgetId.current) -> Forecast:
    return Forecast(
        budget_id=budget_id, months=[], goals=[],
        end_state=EndState(month=END, cash_balance=cash, goal_balances=goals or {}, debt_balances=debts or {},
                           retirement_balance=retirement_balance),
    )


def months_to_reach(start_balance: float, monthly: float, number: float, r: float, tol: float = 0.01) -> int:
    k = 0
    while fv(start_balance, monthly, k, r) < number - tol:
        k += 1
    return k


# --- fv / helpers -----------------------------------------------------------------


def test_fv_complete_profile_worked_example():
    # TDD §16: $8,000 + $400/mo at 5% real for 36 years (age 24 -> 60).
    assert fv(8000, 400, 36 * 12, monthly_rate(0.05)) == pytest.approx(516_798, abs=5)


def test_fv_zero_rate_and_zero_months():
    assert fv(1000, 100, 12, 0.0) == pytest.approx(2200)
    assert fv(1000, 100, 0, 0.01) == pytest.approx(1000)


def test_emergency_reserve(normalized_named, config):
    budget = build_current_budget(normalized_named("complete"))
    assert emergency_reserve(budget, config) == pytest.approx(3 * 2443.333, abs=0.01)


# --- retirement number --------------------------------------------------------------


def test_number_source_precedence(normalized_named, config):
    norm = normalized_named("complete")
    budget = build_current_budget(norm)

    number, source, spending = retirement_number(budget, norm, config)
    assert source == "target"
    assert number == pytest.approx(1_500_000)
    assert spending == pytest.approx(60_000)

    with_desired = norm.model_copy(update={"retirement": norm.retirement.model_copy(
        update={"desired_annual_spending": 40_000})})
    number, source, spending = retirement_number(budget, with_desired, config)
    assert (source, number, spending) == ("desired_annual_spending", pytest.approx(1_000_000), pytest.approx(40_000))

    no_target = norm.model_copy(update={"retirement": norm.retirement.model_copy(update={"target": None})})
    number, source, spending = retirement_number(budget, no_target, config)
    expected_spending = 12 * (budget.totals.need + budget.totals.want)
    assert source == "budget_spending"
    assert spending == pytest.approx(expected_spending)
    assert number == pytest.approx(25 * expected_spending)


def test_number_follows_withdrawal_rate(normalized_named):
    norm = normalized_named("complete")
    norm = norm.model_copy(update={"retirement": norm.retirement.model_copy(update={"desired_annual_spending": 30_000})})
    number, _, _ = retirement_number(build_current_budget(norm), norm, PlanConfig(withdrawal_rate=0.03))
    assert number == pytest.approx(1_000_000)


# --- insufficient data ------------------------------------------------------------------


def test_no_retirement_section(normalized_named, config):
    norm = normalized_named("no_retirement")
    budget = build_current_budget(norm)
    proj = project_retirement(budget, make_forecast(1000, 0), norm, config)
    assert proj.status == "insufficient_data"
    assert proj.missing == ["retirement"]
    assert proj.budget_id == BudgetId.current
    assert proj.projected_retirement_age is None


def test_missing_current_age(normalized_named, config):
    norm = normalized_named("complete")
    norm = norm.model_copy(update={"retirement": norm.retirement.model_copy(update={"current_age": None})})
    proj = project_retirement(build_current_budget(norm), make_forecast(1000, 0), norm, config)
    assert proj.status == "insufficient_data"
    assert proj.missing == ["retirement.current_age"]


def test_missing_target_age_is_allowed():
    norm = make_norm([rent(), ret_line(1000)], target_age=None)
    proj = project_retirement(build_current_budget(norm), make_forecast(0, 10_000), norm,
                              PlanConfig(count_cash_as_investable=False))
    assert proj.status == "ok"
    assert proj.projected_retirement_age is not None
    assert proj.delta_years is None
    assert proj.matches_target is None
    assert proj.required_monthly_to_hit_target is None


# --- projection -----------------------------------------------------------------------


def test_complete_profile_projection_matches_closed_form(normalized_named):
    config = PlanConfig(count_cash_as_investable=False, redirect_freed_payments_to_retirement=False)
    norm = normalized_named("complete")
    budget = build_current_budget(norm)
    r = monthly_rate(config.real_return)
    end_balance = fv(8000, 400, 12, r)
    proj = project_retirement(budget, make_forecast(6500, end_balance), norm, config)

    assert proj.status == "ok"
    assert proj.number_source == "target"
    assert proj.investable_now == pytest.approx(end_balance)
    assert proj.monthly_investment == pytest.approx(400)
    k = months_to_reach(end_balance, 400, 1_500_000, r)
    assert proj.reachable is True
    assert proj.projected_retirement_age == pytest.approx(24 + (12 + k) / 12)
    assert proj.projected_retirement_year == 2026 + (10 + 12 + k) // 12
    assert proj.target_retirement_age == 60
    assert proj.delta_years == pytest.approx(proj.projected_retirement_age - 60)
    assert proj.delta_years > 0
    assert proj.matches_target is False
    assert proj.required_monthly_to_hit_target > 400


def test_reached_at_forecast_end():
    norm = make_norm([rent(), ret_line(500)], current_age=50, target_age=60, target=100_000)
    proj = project_retirement(build_current_budget(norm), make_forecast(0, 150_000), norm,
                              PlanConfig(count_cash_as_investable=False))
    assert proj.reachable is True
    assert proj.projected_retirement_age == pytest.approx(51)
    assert proj.projected_retirement_year == 2027
    assert proj.matches_target is True
    assert proj.delta_years == pytest.approx(-9)
    assert proj.required_monthly_to_hit_target == pytest.approx(0)


def test_unreachable_at_max_age():
    norm = make_norm([rent(), ret_line(10)], current_age=30, target=5_000_000)
    proj = project_retirement(build_current_budget(norm), make_forecast(0, 0), norm,
                              PlanConfig(count_cash_as_investable=False, max_age=70))
    assert proj.status == "ok"
    assert proj.reachable is False
    assert proj.projected_retirement_age is None
    assert proj.projected_retirement_year is None
    assert proj.delta_years is None
    assert proj.matches_target is False
    assert proj.required_monthly_to_hit_target > 10


def test_required_monthly_round_trip():
    config = PlanConfig(count_cash_as_investable=False)
    norm = make_norm([rent(), ret_line(300)], current_age=30, target_age=60, target=1_200_000)
    budget = build_current_budget(norm)
    forecast = make_forecast(0, 20_000)
    proj = project_retirement(budget, forecast, norm, config)
    pmt = proj.required_monthly_to_hit_target
    n = 30 * 12 - config.horizon_months
    # Without a cash pool the solver agrees with the closed-form annuity (to the bisection tolerance).
    assert fv(proj.investable_now, pmt, n, monthly_rate(config.real_return)) == pytest.approx(1_200_000, abs=5)

    # Feeding the required amount back through the projector lands on the target age.
    hit = project_retirement(budget, forecast, norm, config, extra_monthly=pmt - proj.monthly_investment)
    assert hit.monthly_investment == pytest.approx(pmt)
    assert hit.projected_retirement_age == pytest.approx(60, abs=1 / 12 + 1e-9)
    assert hit.matches_target is True


def test_required_monthly_none_when_target_within_forecast():
    norm = make_norm([rent(), ret_line(100)], current_age=59, target_age=60, target=10_000_000)
    proj = project_retirement(build_current_budget(norm), make_forecast(0, 0), norm,
                              PlanConfig(count_cash_as_investable=False))
    assert proj.required_monthly_to_hit_target is None


def test_extra_monthly_retires_earlier():
    config = PlanConfig(count_cash_as_investable=False)
    norm = make_norm([rent(), ret_line(500)])
    budget = build_current_budget(norm)
    base = project_retirement(budget, make_forecast(0, 10_000), norm, config)
    more = project_retirement(budget, make_forecast(0, 10_000), norm, config, extra_monthly=300)
    assert more.monthly_investment == pytest.approx(800)
    assert more.projected_retirement_age < base.projected_retirement_age


# --- cash pool ---------------------------------------------------------------------------


def test_count_cash_as_investable():
    lines = [rent(2000), ret_line(500),
             line("sav:emergency", LineKind.emergency_fund, Bucket.savings, 200)]
    norm = make_norm(lines, income=3000)  # unallocated 300
    budget = build_current_budget(norm)
    forecast = make_forecast(cash=50_000, retirement_balance=10_000)
    reserve = 3 * 2000

    with_cash = project_retirement(budget, forecast, norm, PlanConfig(count_cash_as_investable=True))
    without = project_retirement(budget, forecast, norm, PlanConfig(count_cash_as_investable=False))

    assert with_cash.investable_now == pytest.approx(10_000 + 50_000 - reserve)
    assert with_cash.monthly_investment == pytest.approx(500 + 300 + 200)
    assert without.investable_now == pytest.approx(10_000)
    assert without.monthly_investment == pytest.approx(500)
    assert with_cash.projected_retirement_age < without.projected_retirement_age


def test_cash_below_reserve_is_not_investable():
    norm = make_norm([rent(2000), ret_line(500)])
    proj = project_retirement(build_current_budget(norm), make_forecast(cash=4000, retirement_balance=10_000), norm,
                              PlanConfig())
    assert proj.investable_now == pytest.approx(10_000)


def test_negative_unallocated_adds_nothing_to_cash():
    norm = make_norm([rent(2000), ret_line(500)], income=2300)
    proj = project_retirement(build_current_budget(norm), make_forecast(0, 0), norm, PlanConfig())
    assert proj.monthly_investment == pytest.approx(500)


# --- redirects -------------------------------------------------------------------------


def test_goal_contribution_redirected_after_deadline():
    goal = GoalInfo(id="goal:house", name="House", target_amount=50_000, deadline=date(2029, 1, 1),
                    source_ref="/savings_goals/0")
    lines = [rent(), ret_line(500),
             line("goal:house:contribution", LineKind.goal_contribution, Bucket.savings, 1000, "goal:house")]
    norm = make_norm(lines, goals=[goal])
    budget = build_current_budget(norm)
    forecast = make_forecast(0, 10_000, goals={"goal:house": 12_000})

    on = project_retirement(budget, forecast, norm, PlanConfig(count_cash_as_investable=False))
    off = project_retirement(budget, forecast, norm, PlanConfig(count_cash_as_investable=False,
                                                                 redirect_freed_payments_to_retirement=False))
    # Goal funds are never investable; before the deadline the contribution is still goal-bound.
    assert on.investable_now == pytest.approx(10_000)
    assert on.monthly_investment == pytest.approx(500)
    assert on.projected_retirement_age < off.projected_retirement_age


def test_goal_past_deadline_redirects_immediately():
    goal = GoalInfo(id="goal:trip", name="Trip", target_amount=4000, deadline=date(2027, 6, 1),
                    source_ref="/savings_goals/0")
    lines = [rent(), ret_line(500),
             line("goal:trip:contribution", LineKind.goal_contribution, Bucket.savings, 400, "goal:trip")]
    norm = make_norm(lines, goals=[goal])
    proj = project_retirement(build_current_budget(norm), make_forecast(0, 0), norm,
                              PlanConfig(count_cash_as_investable=False))
    assert proj.monthly_investment == pytest.approx(900)


def test_debt_payment_redirected_after_payoff():
    debt = DebtInfo(id="debt:car", name="Car", type=DebtType.auto_loan, balance=10_000, apr=6.0,
                    minimum_payment=300, source_ref="/debts/0")
    lines = [rent(), ret_line(500),
             line("debt:car:min", LineKind.debt_min, Bucket.need, 300, "debt:car"),
             line("debt:car:extra", LineKind.debt_extra, Bucket.savings, 200, "debt:car")]
    norm = make_norm(lines, debts=[debt])
    budget = build_current_budget(norm)
    forecast = make_forecast(0, 10_000, debts={"debt:car": 4000})

    on = project_retirement(budget, forecast, norm, PlanConfig(count_cash_as_investable=False))
    off = project_retirement(budget, forecast, norm, PlanConfig(count_cash_as_investable=False,
                                                                 redirect_freed_payments_to_retirement=False))
    assert on.monthly_investment == pytest.approx(500)  # still paying the debt in the first month
    assert on.projected_retirement_age < off.projected_retirement_age


def test_paid_off_debt_redirects_full_payment_immediately():
    debt = DebtInfo(id="debt:card", name="Card", type=DebtType.credit_card, balance=0, apr=24.0,
                    minimum_payment=150, source_ref="/debts/0")
    lines = [rent(), ret_line(500), line("debt:card:min", LineKind.debt_min, Bucket.need, 150, "debt:card")]
    norm = make_norm(lines, debts=[debt])
    proj = project_retirement(build_current_budget(norm), make_forecast(0, 0, debts={"debt:card": 0.0}), norm,
                              PlanConfig(count_cash_as_investable=False))
    assert proj.monthly_investment == pytest.approx(650)


def test_debt_final_partial_payment_redirects_remainder():
    debt = DebtInfo(id="debt:card", name="Card", type=DebtType.credit_card, balance=100, apr=0.0,
                    minimum_payment=150, source_ref="/debts/0")
    lines = [rent(), ret_line(500), line("debt:card:min", LineKind.debt_min, Bucket.need, 150, "debt:card")]
    norm = make_norm(lines, debts=[debt])
    proj = project_retirement(build_current_budget(norm), make_forecast(0, 0, debts={"debt:card": 100.0}), norm,
                              PlanConfig(count_cash_as_investable=False))
    assert proj.monthly_investment == pytest.approx(550)


def test_budget_id_comes_from_budget(normalized_named, config):
    norm = normalized_named("complete")
    budget = build_current_budget(norm).model_copy(update={"id": BudgetId.goals_5_3})
    proj = project_retirement(budget, make_forecast(0, 0, budget_id=BudgetId.goals_5_3), norm, config)
    assert proj.budget_id == BudgetId.goals_5_3


# --- review edge cases ---------------------------------------------------------------------


def test_matches_target_one_month_late_is_within_tolerance():
    # Reached at 8 years + 1 month after age 18: the float ages 18 + 97/12 and 26 + 1/12 differ by an ulp.
    config = PlanConfig(count_cash_as_investable=False, real_return=0.0)
    norm = make_norm([rent(), ret_line(1000)], current_age=18, target_age=26, target=85_000)
    proj = project_retirement(build_current_budget(norm), make_forecast(0, 0), norm, config)
    assert proj.projected_retirement_age == pytest.approx(18 + 97 / 12)
    assert proj.matches_target is True

    late = make_norm([rent(), ret_line(1000)], current_age=18, target_age=26, target=86_000)
    assert project_retirement(build_current_budget(late), make_forecast(0, 0), late, config).matches_target is False


@pytest.mark.parametrize("fund, expected", [(4000, 1000), (3800, 800), (0, 500)])
def test_full_goal_fund_overflows_to_cash(fund, expected):
    # Like the forecast, contributions stop at the target and the rest flows to cash.
    goal = GoalInfo(id="goal:car", name="Car", target_amount=4000, deadline=date(2030, 1, 1),
                    source_ref="/savings_goals/0")
    lines = [rent(), ret_line(500),
             line("goal:car:contribution", LineKind.goal_contribution, Bucket.savings, 500, "goal:car")]
    norm = make_norm(lines, goals=[goal])
    proj = project_retirement(build_current_budget(norm), make_forecast(0, 0, goals={"goal:car": fund}), norm,
                              PlanConfig())
    assert proj.monthly_investment == pytest.approx(expected)


@pytest.mark.parametrize("deadline, expected", [(date(2027, 11, 15), 500), (date(2027, 11, 1), 900)])
def test_goal_redirect_starts_with_first_month_on_or_after_deadline(deadline, expected):
    # A mid-month deadline still counts that month's contribution toward the goal (goals.contribution_months).
    goal = GoalInfo(id="goal:trip", name="Trip", target_amount=50_000, deadline=deadline,
                    source_ref="/savings_goals/0")
    lines = [rent(), ret_line(500),
             line("goal:trip:contribution", LineKind.goal_contribution, Bucket.savings, 400, "goal:trip")]
    norm = make_norm(lines, goals=[goal])
    proj = project_retirement(build_current_budget(norm), make_forecast(0, 0), norm,
                              PlanConfig(count_cash_as_investable=False))
    assert proj.monthly_investment == pytest.approx(expected)


def test_freed_debt_payment_returns_to_cash_without_redirect():
    debt = DebtInfo(id="debt:card", name="Card", type=DebtType.credit_card, balance=0, apr=24.0,
                    minimum_payment=150, source_ref="/debts/0")
    lines = [rent(), ret_line(500), line("debt:card:min", LineKind.debt_min, Bucket.need, 150, "debt:card")]
    norm = make_norm(lines, debts=[debt])
    forecast = make_forecast(0, 0, debts={"debt:card": 0.0})
    off = PlanConfig(redirect_freed_payments_to_retirement=False)
    assert project_retirement(build_current_budget(norm), forecast, norm, off).monthly_investment == pytest.approx(650)
    off_no_cash = off.model_copy(update={"count_cash_as_investable": False})
    assert project_retirement(build_current_budget(norm), forecast, norm,
                              off_no_cash).monthly_investment == pytest.approx(500)


def test_reached_at_forecast_end_past_max_age_is_unreachable():
    norm = make_norm([rent(), ret_line(500)], current_age=100, target_age=None, target=1000)
    proj = project_retirement(build_current_budget(norm), make_forecast(0, 10_000), norm,
                              PlanConfig(count_cash_as_investable=False))
    assert proj.reachable is False
    assert proj.projected_retirement_age is None


# --- review regressions: the required amount uses the same two-pool model ---------------------


@pytest.mark.parametrize("cash_return", [0.0, 0.02])
def test_required_monthly_round_trip_with_default_cash_pool(normalized_named, cash_return):
    """Reviewer repro: with cash counted at 0%, feeding the required amount back used to miss the target."""
    from finagent.output.forecast import forecast_budget

    config = PlanConfig(cash_real_return=cash_return)
    norm = normalized_named("complete").model_copy(deep=True)
    norm.retirement.target = 6_000_000
    budget = build_current_budget(norm)
    forecast = forecast_budget(budget, norm, config)
    proj = project_retirement(budget, forecast, norm, config)
    assert proj.matches_target is False
    extra = proj.required_monthly_to_hit_target - proj.monthly_investment
    assert extra > 0
    hit = project_retirement(budget, forecast, norm, config, extra_monthly=extra)
    assert hit.matches_target is True
    assert hit.monthly_investment == pytest.approx(proj.required_monthly_to_hit_target)
    # ...and it is the smallest such amount (to a few dollars).
    miss = project_retirement(budget, forecast, norm, config, extra_monthly=max(0.0, extra - 5))
    assert miss.projected_retirement_age is None or miss.projected_retirement_age > hit.projected_retirement_age - 1e-9


def test_monthly_investment_split_between_portfolio_and_cash():
    lines = [rent(2000), ret_line(500), line("sav:emergency", LineKind.emergency_fund, Bucket.savings, 200)]
    norm = make_norm(lines, income=3000)  # unallocated 300
    proj = project_retirement(build_current_budget(norm), make_forecast(cash=0, retirement_balance=0), norm,
                              PlanConfig())
    assert proj.monthly_to_portfolio == pytest.approx(500)
    assert proj.monthly_to_cash == pytest.approx(500)
    assert proj.monthly_investment == pytest.approx(1000)


def test_one_time_set_aside_is_freed_after_the_window(normalized_named):
    from finagent.output.budget import fallback_restructure
    from finagent.output.forecast import forecast_budget
    from finagent.output.models import BudgetId

    norm = normalized_named("overspender")
    budget = fallback_restructure(norm, BudgetId.rule_50_30_20, PlanConfig())
    forecast = forecast_budget(budget, norm, PlanConfig())
    on = project_retirement(budget, forecast, norm, PlanConfig())
    off = project_retirement(budget, forecast, norm, PlanConfig(redirect_freed_payments_to_retirement=False))
    # Redirected: the $400 trip set-aside and the emergency goal's $272.73 (deadline passed) go to the portfolio.
    assert on.monthly_to_portfolio == pytest.approx(off.monthly_to_portfolio + 400 + 3000 / 11)


@pytest.mark.parametrize("profile", ["complete", "overspender", "big_goals", "mislabeled"])
def test_required_amount_meets_the_target_for_every_fixture_budget(normalized_named, profile):
    """The advice 'investing about $X/mo would reach the number by age N' must hold (re-run the projector)."""
    from finagent.output.budget import build_budgets
    from finagent.output.forecast import forecast_budget

    config = PlanConfig()
    norm = normalized_named(profile)
    for bid, budget in build_budgets(norm, config).items():
        forecast = forecast_budget(budget, norm, config)
        proj = project_retirement(budget, forecast, norm, config)
        if proj.required_monthly_to_hit_target is None:
            continue
        extra = proj.required_monthly_to_hit_target - proj.monthly_investment
        hit = project_retirement(budget, forecast, norm, config, extra_monthly=extra)
        assert hit.matches_target is True, (profile, bid)
