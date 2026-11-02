"""Tests for the 12-month forecast and goal verdicts (TDD §8)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from finagent.output.budget import build_current_budget
from finagent.output.config import PlanConfig
from finagent.output.forecast import add_months, forecast_budget, is_active, month_starts
from finagent.output.models import Bucket, BudgetLine, GoalStatus, LineKind, Tier
from finagent.output.normalize import normalize
from finagent.schema import FinancialProfile

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATHS = {
    "complete": ROOT / "examples" / "profiles" / "complete_profile.json",
    "overspender": Path(__file__).resolve().parent / "fixtures" / "overspender.json",
}
R_M = 1.05 ** (1 / 12) - 1


def profile_dict(name: str) -> dict:
    return json.loads(PROFILE_PATHS[name].read_text())


def run(data: dict | str, config: PlanConfig | None = None, edit=None):
    """Normalize a profile (fixture name or dict), build the current budget, optionally edit it, forecast."""
    config = config or PlanConfig()
    data = profile_dict(data) if isinstance(data, str) else data
    norm = normalize(FinancialProfile.model_validate(data), config)
    budget = build_current_budget(norm, config)
    if edit:
        edit(budget)
    return forecast_budget(budget, norm, config), norm


def verdict(fc, goal_id: str):
    return next(v for v in fc.goals if v.goal_id == goal_id)


def with_japan(**changes) -> dict:
    data = profile_dict("complete")
    data["savings_goals"][0].update(changes)
    return data


# --- date helpers -----------------------------------------------------------------


def test_add_months_and_month_starts():
    assert add_months(date(2026, 11, 1), 2) == date(2027, 1, 1)
    assert add_months(date(2026, 12, 1), 1) == date(2027, 1, 1)
    assert add_months(date(2027, 3, 1), -3) == date(2026, 12, 1)
    assert month_starts(date(2026, 11, 1), 3) == [date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)]


def test_is_active():
    nov = date(2026, 11, 1)
    assert is_active(None, nov)
    assert is_active(date(2026, 11, 30), nov)
    assert is_active(date(2026, 1, 5), nov)
    assert not is_active(date(2026, 12, 1), nov)


# --- complete profile, current budget ---------------------------------------------------


@pytest.fixture
def complete():
    return run("complete")


def test_exactly_twelve_rows(complete):
    fc, norm = complete
    assert len(fc.months) == 12
    assert fc.months[0].month == date(2026, 11, 1) == norm.start_month
    assert fc.months[-1].month == date(2027, 10, 1)
    assert fc.end_state.month == date(2027, 10, 1)


def test_horizon_from_config():
    fc, _ = run("complete", PlanConfig(horizon_months=3))
    assert len(fc.months) == 3


def test_first_month_flows(complete):
    fc, _ = complete
    m = fc.months[0]
    assert m.income == pytest.approx(6200)
    assert m.needs == pytest.approx(1800 + 100 * 52 / 12)
    assert m.wants == pytest.approx(570)
    assert m.one_time == 0
    assert m.debt_payments == pytest.approx(210)
    assert m.goal_contributions == pytest.approx(500)
    assert m.retirement_contribution == pytest.approx(400)
    assert m.emergency_and_general_savings == 0
    assert m.net_cash_flow == pytest.approx(6200 - 1800 - 100 * 52 / 12 - 570 - 210 - 500 - 400)
    assert m.cash_balance == pytest.approx(6500 + m.net_cash_flow)


def test_laptop_lands_in_december(complete):
    fc, _ = complete
    by_month = {m.month: m.one_time for m in fc.months}
    assert by_month[date(2026, 12, 1)] == pytest.approx(1500)
    assert sum(by_month.values()) == pytest.approx(1500)


def test_student_loan_amortization(complete):
    fc, _ = complete
    loan = "debt:federal-student-loan"
    first = 18000 * (1 + 0.055 / 12) - 210
    assert 18000 * 0.055 / 12 == pytest.approx(82.5)
    assert fc.months[0].debt_balances[loan] == pytest.approx(17872.5)
    assert fc.months[0].debt_balances[loan] == pytest.approx(first)
    second = first * (1 + 0.055 / 12) - 210
    assert fc.months[1].debt_balances[loan] == pytest.approx(second)


def test_japan_trip_off_track(complete):
    fc, _ = complete
    v = verdict(fc, "goal:japan-trip")
    assert v.status is GoalStatus.off_track
    assert v.balance_at_deadline == pytest.approx(3500)
    assert v.shortfall == pytest.approx(500)
    assert v.required_monthly == pytest.approx(571.43, abs=0.005)
    assert v.current_monthly == 0  # fund is full by the last month
    assert v.projected_completion_date == date(2027, 6, 1)
    assert v.deadline == date(2027, 6, 1)
    assert v.target_amount == 4000


def test_goal_fund_stops_at_target_and_excess_stays_in_cash(complete):
    fc, _ = complete
    funds = [m.goal_balances["goal:japan-trip"] for m in fc.months]
    assert funds[6] == pytest.approx(3500)        # May 2027
    assert funds[7] == pytest.approx(4000)        # June 2027 reaches the target
    assert all(f == pytest.approx(4000) for f in funds[7:])
    assert fc.months[7].goal_contributions == pytest.approx(500)
    assert fc.months[8].goal_contributions == 0
    assert fc.months[8].net_cash_flow == pytest.approx(fc.months[7].net_cash_flow + 500)


def test_retirement_growth_formula(complete):
    fc, _ = complete
    bal = 8000.0
    for m in fc.months:
        bal = bal * (1 + R_M) + 400
        assert m.retirement_balance == pytest.approx(bal)
    assert fc.end_state.retirement_balance == pytest.approx(bal)


def test_cash_balance_accumulates(complete):
    fc, _ = complete
    cash = 6500.0
    for m in fc.months:
        cash += m.net_cash_flow
        assert m.cash_balance == pytest.approx(cash)
    assert fc.cash_shortfall_months == []
    assert fc.end_state.cash_balance == pytest.approx(cash)
    assert fc.end_state.goal_balances == fc.months[-1].goal_balances
    assert fc.end_state.debt_balances == fc.months[-1].debt_balances


def test_cash_real_return_applied():
    fc, _ = run("complete", PlanConfig(cash_real_return=0.12))
    c_m = 1.12 ** (1 / 12) - 1
    first = fc.months[0]
    assert first.cash_balance == pytest.approx(6500 * (1 + c_m) + first.net_cash_flow)


def test_deterministic():
    a, _ = run("complete")
    b, _ = run("complete")
    assert a.model_dump_json() == b.model_dump_json()


# --- budget edits -----------------------------------------------------------------------------


def test_dropped_one_time_is_not_paid():
    def drop_laptop(budget):
        budget.one_time[0].monthly_amount = 0.0

    fc, _ = run("complete", edit=drop_laptop)
    assert all(m.one_time == 0 for m in fc.months)


def test_restructured_amounts_are_used():
    def cut_and_save(budget):
        for line in budget.lines:
            if line.id == "rec:eating-out":
                line.monthly_amount = 150.0
            if line.kind is LineKind.goal_contribution:
                line.monthly_amount = 4000 / 7

    fc, _ = run("complete", edit=cut_and_save)
    assert fc.months[0].wants == pytest.approx(270)
    v = verdict(fc, "goal:japan-trip")
    assert v.status is GoalStatus.met
    assert v.balance_at_deadline == pytest.approx(4000)
    assert v.shortfall == 0
    assert v.required_monthly is None
    assert v.projected_completion_date == date(2027, 5, 1)


def test_emergency_and_general_savings_stay_in_cash():
    def add_reserve(budget):
        budget.lines.append(BudgetLine(
            id="sav:emergency", name="Emergency fund", kind=LineKind.emergency_fund, bucket=Bucket.savings,
            monthly_amount=300.0, original_amount=0.0, tier=Tier.savings,
        ))

    base, _ = run("complete")
    fc, _ = run("complete", edit=add_reserve)
    assert fc.months[0].emergency_and_general_savings == pytest.approx(300)
    assert fc.months[0].net_cash_flow == pytest.approx(base.months[0].net_cash_flow)
    assert fc.end_state.cash_balance == pytest.approx(base.end_state.cash_balance)


# --- effective dates -------------------------------------------------------------------------


def test_effective_date_switches_item_on():
    data = profile_dict("complete")
    data["recurring_expenses"].append({
        "name": "Car payment", "amount": 300, "frequency": "monthly", "category": "transport",
        "need_or_want": "need", "is_fixed": True, "effective_date": "2027-02-15",
    })
    data["income"].append({
        "source": "Raise", "amount": 500, "frequency": "monthly", "is_net": True, "effective_date": "2027-04-01",
    })
    fc, _ = run(data)
    base_needs = 1800 + 100 * 52 / 12
    needs = {m.month: m.needs for m in fc.months}
    assert needs[date(2027, 1, 1)] == pytest.approx(base_needs)
    assert needs[date(2027, 2, 1)] == pytest.approx(base_needs + 300)
    assert needs[date(2027, 10, 1)] == pytest.approx(base_needs + 300)
    income = {m.month: m.income for m in fc.months}
    assert income[date(2027, 3, 1)] == pytest.approx(6200)
    assert income[date(2027, 4, 1)] == pytest.approx(6700)


# --- debts -------------------------------------------------------------------------------------


def test_debt_payoff_frees_cash():
    data = profile_dict("complete")
    data["debts"][0]["balance"] = 300
    fc, _ = run(data)
    loan = "debt:federal-student-loan"
    i_m = 0.055 / 12
    b1 = 300 * (1 + i_m) - 210
    assert fc.months[0].debt_payments == pytest.approx(210)
    assert fc.months[0].debt_balances[loan] == pytest.approx(b1)
    assert fc.months[1].debt_payments == pytest.approx(b1 * (1 + i_m))  # capped at the balance
    assert fc.months[1].debt_balances[loan] == pytest.approx(0)
    assert fc.months[2].debt_payments == 0
    assert fc.months[2].net_cash_flow == pytest.approx(fc.months[0].net_cash_flow + 210)


def test_debt_without_apr_has_no_interest():
    data = profile_dict("complete")
    data["debts"][0]["apr"] = None
    fc, _ = run(data)
    assert fc.months[0].debt_balances["debt:federal-student-loan"] == pytest.approx(18000 - 210)


# --- cash shortfall ------------------------------------------------------------------------------


def test_overspender_cash_shortfall():
    # 4000 - needs 2930 - wants 660 - card minimum 150 = +260/mo from 500; Disneyland -2500 in March 2027.
    fc, _ = run("overspender")
    assert fc.months[0].net_cash_flow == pytest.approx(260)
    assert fc.months[4].month == date(2027, 3, 1)
    assert fc.months[4].one_time == pytest.approx(2500)
    assert fc.months[4].cash_balance == pytest.approx(-700)
    assert fc.months[6].cash_balance == pytest.approx(-180)
    assert fc.months[7].cash_balance == pytest.approx(80)
    assert fc.cash_shortfall_months == [date(2027, 3, 1), date(2027, 4, 1), date(2027, 5, 1)]
    card = fc.months[0].debt_balances["debt:credit-card"]
    assert card == pytest.approx(5000 * 1.02 - 150)


def test_overspender_goal_without_contribution():
    fc, _ = run("overspender")
    v = verdict(fc, "goal:emergency-fund")
    assert all(m.goal_balances["goal:emergency-fund"] == 0 for m in fc.months)
    assert v.status is GoalStatus.off_track  # deadline 2027-10-01 is inside the window, nothing saved
    assert v.balance_at_deadline == 0
    assert v.shortfall == pytest.approx(3000)
    assert v.current_monthly == 0
    assert v.projected_completion_date is None
    assert v.required_monthly == pytest.approx(3000 / 11)


# --- goal verdicts ---------------------------------------------------------------------------------


def test_on_track_after_window():
    data = with_japan(deadline="2028-06-01", contribution={"type": "amount", "value": 250, "frequency": "monthly"})
    fc, _ = run(data)
    v = verdict(fc, "goal:japan-trip")
    # 12 x 250 = 3000 by Oct 2027, then 7 contribution months (Nov 2027 - May 2028) at 250.
    assert fc.end_state.goal_balances["goal:japan-trip"] == pytest.approx(3000)
    assert v.status is GoalStatus.on_track
    assert v.balance_at_deadline == pytest.approx(4000)  # capped at the target
    assert v.shortfall == 0
    assert v.current_monthly == pytest.approx(250)
    assert v.projected_completion_date == date(2028, 2, 1)
    assert v.required_monthly == pytest.approx(4000 / 19)


def test_off_track_after_window():
    data = with_japan(deadline="2028-06-01", contribution={"type": "amount", "value": 200, "frequency": "monthly"})
    fc, _ = run(data)
    v = verdict(fc, "goal:japan-trip")
    assert v.status is GoalStatus.off_track
    assert v.balance_at_deadline == pytest.approx(2400 + 7 * 200)
    assert v.shortfall == pytest.approx(200)
    assert v.projected_completion_date == date(2028, 6, 1)  # 1600 left / 200 = 8 months: Nov 2027 - Jun 2028
    assert v.required_monthly == pytest.approx(4000 / 19)


def test_no_deadline_with_target():
    fc, _ = run(with_japan(deadline=None))
    v = verdict(fc, "goal:japan-trip")
    assert v.status is GoalStatus.no_deadline
    assert v.shortfall == 0
    assert v.balance_at_deadline is None
    assert v.required_monthly is None
    assert v.projected_completion_date == date(2027, 6, 1)


def test_no_deadline_extrapolated_completion():
    data = with_japan(deadline=None, target_amount=10000)
    fc, _ = run(data)
    v = verdict(fc, "goal:japan-trip")
    # 6000 saved in the window, 4000 left at 500/mo = 8 more months from Nov 2027.
    assert v.projected_completion_date == date(2028, 6, 1)
    assert v.current_monthly == pytest.approx(500)


def test_no_target_goal():
    data = with_japan(target_amount=None)
    fc, _ = run(data)
    v = verdict(fc, "goal:japan-trip")
    assert v.status is GoalStatus.no_deadline
    assert v.shortfall == 0
    assert v.projected_completion_date is None
    assert v.required_monthly is None
    assert fc.end_state.goal_balances["goal:japan-trip"] == pytest.approx(6000)  # no cap without a target


def test_debt_extra_line_adds_to_payment_and_is_capped():
    def add_extra(budget):
        budget.lines.append(BudgetLine(
            id="debt:federal-student-loan:extra", name="Extra loan payment", kind=LineKind.debt_extra,
            bucket=Bucket.savings, monthly_amount=90.0, original_amount=0.0, tier=Tier.savings,
            target_ref="debt:federal-student-loan",
        ))

    data = profile_dict("complete")
    data["debts"][0]["balance"] = 500
    fc, _ = run(data, edit=add_extra)
    i_m = 0.055 / 12
    b1 = 500 * (1 + i_m) - 300
    assert fc.months[0].debt_payments == pytest.approx(300)
    assert fc.months[1].debt_payments == pytest.approx(b1 * (1 + i_m))
    assert fc.months[1].debt_balances["debt:federal-student-loan"] == pytest.approx(0)
    assert fc.months[2].debt_payments == 0


def test_inactive_debt_minimum_accrues_interest_only():
    def defer_minimum(budget):
        for line in budget.lines:
            if line.kind is LineKind.debt_min:
                line.effective_date = date(2027, 1, 1)

    fc, _ = run("complete", edit=defer_minimum)
    loan = "debt:federal-student-loan"
    assert fc.months[0].debt_payments == 0
    assert fc.months[0].debt_balances[loan] == pytest.approx(18000 * (1 + 0.055 / 12))
    assert fc.months[2].debt_payments == pytest.approx(210)


def test_mid_month_deadline_counts_its_own_month():
    # A 2027-06-15 deadline: June's contribution (first day Jun 1) lands before it, so the fund hits 4000.
    v = verdict(run(with_japan(deadline="2027-06-15"))[0], "goal:japan-trip")
    assert v.status is GoalStatus.met
    assert v.balance_at_deadline == pytest.approx(4000)
    assert v.required_monthly is None


def test_deadline_before_forecast_is_off_track():
    v = verdict(run(with_japan(deadline="2026-10-01"))[0], "goal:japan-trip")
    assert v.status is GoalStatus.off_track
    assert v.balance_at_deadline == 0
    assert v.shortfall == pytest.approx(4000)
    assert v.required_monthly == pytest.approx(4000)  # whole remainder in the first month


def test_deadline_on_window_boundary():
    # 2027-11-01 is the first day after the window: all 12 contributions count, judged in-window.
    v = verdict(run(with_japan(deadline="2027-11-01", target_amount=7000))[0], "goal:japan-trip")
    assert v.status is GoalStatus.off_track
    assert v.balance_at_deadline == pytest.approx(6000)
    assert v.shortfall == pytest.approx(1000)
    assert v.projected_completion_date == date(2027, 12, 1)
    # 2027-11-15 is after the window: one extrapolated month (Nov 2027) at 500.
    v = verdict(run(with_japan(deadline="2027-11-15", target_amount=7000))[0], "goal:japan-trip")
    assert v.status is GoalStatus.off_track
    assert v.balance_at_deadline == pytest.approx(6500)
    assert v.shortfall == pytest.approx(500)
    assert v.required_monthly == pytest.approx(7000 / 13)


def test_contribution_effective_date_delays_goal_fund():
    def delay(budget):
        for line in budget.lines:
            if line.kind is LineKind.goal_contribution:
                line.effective_date = date(2027, 1, 10)

    fc, _ = run("complete", edit=delay)
    funds = [m.goal_balances["goal:japan-trip"] for m in fc.months]
    assert funds[:2] == [0, 0]
    v = verdict(fc, "goal:japan-trip")
    assert v.balance_at_deadline == pytest.approx(2500)  # Jan-May
    assert v.shortfall == pytest.approx(1500)
