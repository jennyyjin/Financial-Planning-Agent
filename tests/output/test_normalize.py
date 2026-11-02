"""Tests for FinancialProfile -> NormalizedProfile (TDD §5, §14.1)."""

from __future__ import annotations

from datetime import date

import pytest

from finagent.output.config import PlanConfig
from finagent.output.models import Bucket, LineKind, Tier
from finagent.output.normalize import first_of_next_month, normalize, slugify
from finagent.schema import FinancialProfile

META = {"user_id": "u_test", "as_of": "2026-10-28T10:00:00", "profile_version": 1, "memory_method": "full_history"}
NET_INCOME = {"source": "Job", "amount": 4000, "frequency": "monthly", "is_net": True}


def make_profile(**sections) -> FinancialProfile:
    data = {"meta": dict(META), "income": [NET_INCOME]}
    data.update(sections)
    return FinancialProfile.model_validate(data)


def expense(name: str, amount: float | None = 100, **kw) -> dict:
    return {"name": name, "amount": amount, "frequency": "monthly", "category": "other",
            "need_or_want": "want", "is_fixed": False, **kw}


def line(norm, line_id: str):
    matches = [ln for ln in norm.lines + norm.one_time if ln.id == line_id]
    assert len(matches) == 1, f"expected one line {line_id!r}, got {[ln.id for ln in norm.lines + norm.one_time]}"
    return matches[0]


def codes(norm, ref: str | None = None) -> list[str]:
    return [w.code for w in norm.warnings if ref is None or w.ref == ref]


def bucket_total(norm, bucket: Bucket) -> float:
    return sum(ln.monthly_amount for ln in norm.lines if ln.bucket is bucket)


# --- helpers -------------------------------------------------------------------


@pytest.mark.parametrize("name, slug", [
    ("Eating out", "eating-out"),
    ("Federal student loan", "federal-student-loan"),
    ("  Rent!! (NYC) ", "rent-nyc"),
    ("401(k)", "401-k"),
    ("!!!", "item"),
])
def test_slugify(name, slug):
    assert slugify(name) == slug


@pytest.mark.parametrize("d, expected", [
    (date(2026, 10, 28), date(2026, 11, 1)),
    (date(2026, 10, 1), date(2026, 11, 1)),
    (date(2026, 12, 31), date(2027, 1, 1)),
])
def test_first_of_next_month(d, expected):
    assert first_of_next_month(d) == expected


# --- complete profile -------------------------------------------------------------


def test_complete_profile_numbers(normalized_named):
    norm = normalized_named("complete")
    assert norm.net_monthly_income == pytest.approx(6200)
    assert bucket_total(norm, Bucket.need) == pytest.approx(2443.33, abs=0.01)
    assert bucket_total(norm, Bucket.want) == pytest.approx(570)
    assert bucket_total(norm, Bucket.savings) == pytest.approx(900)
    spent = sum(ln.monthly_amount for ln in norm.lines)
    assert norm.net_monthly_income - spent == pytest.approx(2286.67, abs=0.01)
    assert norm.overall_savings_target_monthly == pytest.approx(1240)  # 20% of 6200
    assert norm.current_savings_balance == pytest.approx(6500)


def test_complete_profile_ids_and_buckets(normalized_named):
    norm = normalized_named("complete")
    assert [ln.id for ln in norm.lines] == [
        "rec:rent", "rec:groceries", "rec:eating-out", "rec:yoga-membership",
        "debt:federal-student-loan:min", "goal:japan-trip:contribution", "ret:contribution",
    ]
    assert [ln.id for ln in norm.one_time] == ["one:laptop"]
    assert [g.id for g in norm.goals] == ["goal:japan-trip"]
    assert [d.id for d in norm.debts] == ["debt:federal-student-loan"]
    assert [i.id for i in norm.income] == ["inc:software-engineer-salary"]

    assert line(norm, "rec:rent").bucket is Bucket.need
    assert line(norm, "rec:groceries").monthly_amount == pytest.approx(100 * 52 / 12)
    assert line(norm, "rec:eating-out").bucket is Bucket.want
    assert line(norm, "rec:eating-out").tier is Tier.scalable
    assert line(norm, "rec:yoga-membership").tier is Tier.drop_only
    assert line(norm, "rec:rent").tier is Tier.protected
    assert line(norm, "rec:groceries").tier is Tier.protected  # need_trim_cap = 0 by default


def test_complete_profile_source_refs(normalized_named):
    norm = normalized_named("complete")
    assert line(norm, "rec:eating-out").source_ref == "/recurring_expenses/2"
    assert line(norm, "debt:federal-student-loan:min").source_ref == "/debts/0"
    assert line(norm, "goal:japan-trip:contribution").source_ref == "/savings_goals/0"
    assert line(norm, "ret:contribution").source_ref == "/retirement"
    assert line(norm, "one:laptop").source_ref == "/one_time_expenses/0"
    assert norm.income[0].source_ref == "/income/0"


def test_complete_profile_has_no_warnings(normalized_named):
    assert normalized_named("complete").warnings == []


def test_start_month_is_first_of_month_after_as_of(normalized_named):
    assert normalized_named("complete").start_month == date(2026, 11, 1)
    norm = normalize(make_profile(meta={**META, "as_of": "2026-12-31T23:59:00"}))
    assert norm.start_month == date(2027, 1, 1)


def test_profile_is_not_modified(profile_named, config):
    profile = profile_named("complete")
    before = profile.model_dump()
    normalize(profile, config)
    assert profile.model_dump() == before


def test_deterministic(profile_named, config):
    profile = profile_named("mislabeled")
    assert normalize(profile, config) == normalize(profile, config)


def test_config_defaults_when_omitted(profile_named, config):
    profile = profile_named("complete")
    assert normalize(profile) == normalize(profile, config)


# --- debts, goals, retirement -------------------------------------------------------


def test_debt_minimum_is_protected_need(normalized_named):
    debt_line = line(normalized_named("complete"), "debt:federal-student-loan:min")
    assert debt_line.kind is LineKind.debt_min
    assert debt_line.bucket is Bucket.need
    assert debt_line.tier is Tier.protected
    assert debt_line.monthly_amount == pytest.approx(210)
    assert debt_line.target_ref == "debt:federal-student-loan"


def test_debt_unknown_fields_warn_and_default(normalized_named):
    norm = normalized_named("incomplete")
    debt = norm.debts[0]
    assert debt.id == "debt:credit-card"
    assert debt.balance == pytest.approx(2400)
    assert line(norm, "debt:credit-card:min").monthly_amount == 0
    assert line(norm, "debt:credit-card:min").tier is Tier.protected
    assert {"debt_apr_unknown", "debt_minimum_unknown"} <= set(codes(norm, "debt:credit-card"))
    assert "debt_balance_unknown" not in codes(norm)


def test_debt_balance_unknown():
    norm = normalize(make_profile(debts=[{"name": "Loan", "balance": None, "apr": 5, "minimum_payment": 50}]))
    assert norm.debts[0].balance == 0
    assert codes(norm, "debt:loan") == ["debt_balance_unknown"]


def test_goal_contribution_line(normalized_named):
    norm = normalized_named("complete")
    contrib = line(norm, "goal:japan-trip:contribution")
    assert contrib.kind is LineKind.goal_contribution
    assert contrib.bucket is Bucket.savings
    assert contrib.tier is Tier.savings
    assert contrib.monthly_amount == pytest.approx(500)
    assert contrib.target_ref == "goal:japan-trip"
    goal = norm.goals[0]
    assert goal.stated_monthly == pytest.approx(500)
    assert goal.target_amount == pytest.approx(4000)
    assert goal.deadline == date(2027, 6, 1)


def test_goal_without_contribution_has_no_line(normalized_named):
    norm = normalized_named("big_goals")
    ids = [ln.id for ln in norm.lines]
    assert "goal:house-down-payment:contribution" not in ids
    assert "goal:wedding:contribution" in ids
    assert norm.goals[0].stated_monthly is None
    assert "goal_unscheduled" not in codes(norm)  # has a deadline


def test_goal_percent_of_income_contribution():
    norm = normalize(make_profile(savings_goals=[{
        "name": "Car", "target_amount": 5000, "deadline": None,
        "contribution": {"type": "percent_of_income", "value": 5},
    }]))
    assert line(norm, "goal:car:contribution").monthly_amount == pytest.approx(200)  # 5% of 4000
    assert norm.goals[0].stated_monthly == pytest.approx(200)


def test_percent_of_income_with_no_income_is_zero():
    norm = normalize(make_profile(
        income=[],
        savings_goals=[{"name": "Car", "target_amount": 5000, "contribution": {"type": "percent_of_income", "value": 5}}],
        overall_savings_target={"type": "percent_of_income", "value": 20},
    ))
    assert "goal:car:contribution" not in [ln.id for ln in norm.lines]
    assert norm.overall_savings_target_monthly == 0


def test_goal_contribution_frequency_converted():
    norm = normalize(make_profile(savings_goals=[{
        "name": "Car", "target_amount": 5000,
        "contribution": {"type": "amount", "value": 1200, "frequency": "annual"},
    }]))
    assert line(norm, "goal:car:contribution").monthly_amount == pytest.approx(100)


def test_goal_unscheduled_and_target_unknown(normalized_named):
    norm = normalized_named("incomplete")
    assert "goal_unscheduled" in codes(norm, "goal:emergency-fund")
    norm = normalize(make_profile(savings_goals=[{"name": "Someday", "target_amount": None, "deadline": "2027-06-01"}]))
    assert codes(norm, "goal:someday") == ["goal_target_unknown"]


def test_retirement_contribution_line(normalized_named):
    norm = normalized_named("complete")
    ret = line(norm, "ret:contribution")
    assert ret.kind is LineKind.retirement_contribution
    assert ret.bucket is Bucket.savings
    assert ret.tier is Tier.savings
    assert ret.monthly_amount == pytest.approx(400)
    assert norm.retirement.current_balance == pytest.approx(8000)
    assert norm.retirement.target == pytest.approx(1500000)


def test_zero_retirement_contribution_has_no_line(normalized_named):
    norm = normalized_named("overspender")
    assert norm.retirement is not None
    assert "ret:contribution" not in [ln.id for ln in norm.lines]


def test_no_retirement(normalized_named):
    norm = normalized_named("no_retirement")
    assert norm.retirement is None
    assert all(ln.kind is not LineKind.retirement_contribution for ln in norm.lines)


def test_overall_target_amount_and_percent(normalized_named):
    assert normalized_named("mislabeled").overall_savings_target_monthly == pytest.approx(300)
    assert normalized_named("overspender").overall_savings_target_monthly == pytest.approx(400)  # 10% of 4000
    assert normalized_named("big_goals").overall_savings_target_monthly is None


def test_overall_target_uses_net_income_from_frequency(normalized_named):
    norm = normalized_named("incomplete")  # 1100 biweekly, target 10%
    assert norm.net_monthly_income == pytest.approx(1100 * 26 / 12)
    assert norm.overall_savings_target_monthly == pytest.approx(0.10 * 1100 * 26 / 12)


# --- warnings and labels ---------------------------------------------------------------


def test_null_need_or_want_is_protected_need_with_warning(normalized_named):
    norm = normalized_named("mislabeled")
    mystery = line(norm, "rec:mystery-charge")
    assert mystery.bucket is Bucket.need
    assert mystery.tier is Tier.protected
    assert codes(norm, "rec:mystery-charge") == ["need_or_want_unknown"]


@pytest.mark.parametrize("is_fixed", [None, True, False])
def test_null_need_or_want_stays_protected_even_with_trim_cap(is_fixed):
    # is_fixed=False is the case that slipped through: an unlabelled variable line
    # arrives at classify_tier as a need, and a trim cap would make it scalable.
    profile = make_profile(recurring_expenses=[expense("Misc", need_or_want=None, is_fixed=is_fixed)])
    norm = normalize(profile, PlanConfig(need_trim_cap=0.2))
    assert line(norm, "rec:misc").tier is Tier.protected
    assert line(norm, "rec:misc").bucket is Bucket.need


def test_labelled_variable_need_scalable_with_trim_cap():
    profile = make_profile(recurring_expenses=[expense("Groceries", 500, category="groceries", need_or_want="need")])
    assert line(normalize(profile, PlanConfig(need_trim_cap=0.2)), "rec:groceries").tier is Tier.scalable


def test_label_conflict_want_in_protected_category(normalized_named):
    norm = normalized_named("mislabeled")
    med = line(norm, "rec:medication")
    assert med.bucket is Bucket.need  # protected category overrides the label, so it is not counted as a want
    assert med.tier is Tier.protected
    assert codes(norm, "rec:medication") == ["label_conflict"]
    assert line(norm, "rec:concerts").tier is Tier.scalable
    assert line(norm, "rec:shopping").tier is Tier.scalable
    assert "label_conflict" not in codes(norm, "rec:rent")


@pytest.mark.parametrize("is_net, warns", [(True, False), (False, True), (None, True)])
def test_income_may_be_gross(is_net, warns):
    norm = normalize(make_profile(income=[{**NET_INCOME, "is_net": is_net}]))
    assert norm.net_monthly_income == pytest.approx(4000)  # used as given
    assert ("income_may_be_gross" in codes(norm, "/income/0")) is warns


def test_incomplete_profile_income_warning(normalized_named):
    assert "income_may_be_gross" in codes(normalized_named("incomplete"))


def test_unknown_income_ignored_with_warning():
    norm = normalize(make_profile(income=[NET_INCOME, {"source": "Gig", "amount": None, "frequency": "weekly", "is_net": True}]))
    assert norm.net_monthly_income == pytest.approx(4000)
    assert [i.name for i in norm.income] == ["Job"]
    assert codes(norm, "/income/1") == ["income_unknown"]


def test_no_income_warning():
    norm = normalize(make_profile(income=[]))
    assert norm.net_monthly_income == 0
    assert "no_income" in codes(norm)


def test_unknown_expense_amount_counts_as_zero(normalized_named):
    norm = normalized_named("incomplete")
    streaming = line(norm, "rec:streaming-services")
    assert streaming.monthly_amount == 0
    assert streaming.original_amount == 0
    assert "expense_amount_unknown" in codes(norm, "rec:streaming-services")


def test_unknown_expense_frequency_counts_as_zero():
    norm = normalize(make_profile(recurring_expenses=[expense("Gas", 60, frequency=None)]))
    assert line(norm, "rec:gas").monthly_amount == 0
    assert codes(norm, "rec:gas") == ["expense_amount_unknown"]


# --- one-time expenses ---------------------------------------------------------------


def test_one_time_kept_outside_buckets(normalized_named):
    norm = normalized_named("complete")
    laptop = line(norm, "one:laptop")
    assert laptop.kind is LineKind.one_time
    assert laptop.bucket is None
    assert laptop.tier is Tier.protected
    assert laptop.discretionary is False
    assert laptop.monthly_amount == pytest.approx(1500)
    assert laptop.expected_date == date(2026, 12, 15)
    assert laptop.id not in [ln.id for ln in norm.lines]


def test_one_time_undated_goes_to_start_month():
    norm = normalize(make_profile(one_time_expenses=[{"name": "Couch", "amount": 800, "expected_date": None}]))
    assert line(norm, "one:couch").expected_date == date(2026, 11, 1)
    assert codes(norm, "one:couch") == ["one_time_undated"]


@pytest.mark.parametrize("expected", ["2026-10-30", "2025-01-01"])
def test_one_time_in_past_goes_to_start_month(expected):
    norm = normalize(make_profile(one_time_expenses=[{"name": "Couch", "amount": 800, "expected_date": expected}]))
    assert line(norm, "one:couch").expected_date == date(2026, 11, 1)
    assert codes(norm, "one:couch") == ["one_time_in_past"]


def test_one_time_in_start_month_is_not_moved():
    norm = normalize(make_profile(one_time_expenses=[{"name": "Couch", "amount": 800, "expected_date": "2026-11-01"}]))
    assert line(norm, "one:couch").expected_date == date(2026, 11, 1)
    assert codes(norm) == []


def test_one_time_unknown_amount():
    norm = normalize(make_profile(one_time_expenses=[{"name": "Repair", "amount": None, "expected_date": "2027-02-01"}]))
    assert line(norm, "one:repair").monthly_amount == 0
    assert codes(norm, "one:repair") == ["expense_amount_unknown"]


# --- ids ---------------------------------------------------------------------------


def test_duplicate_names_get_unique_stable_ids():
    profile = make_profile(
        recurring_expenses=[expense("Rent"), expense("Rent"), expense("rent")],
        one_time_expenses=[{"name": "Rent", "amount": 50, "expected_date": "2027-01-01"}],
        debts=[{"name": "Loan", "balance": 100, "apr": 5, "minimum_payment": 10},
               {"name": "Loan", "balance": 200, "apr": 6, "minimum_payment": 20}],
    )
    norm = normalize(profile)
    ids = [ln.id for ln in norm.lines + norm.one_time]
    assert len(ids) == len(set(ids))
    assert ids[:3] == ["rec:rent", "rec:rent-2", "rec:rent-3"]
    assert "one:rent" in ids
    assert [d.id for d in norm.debts] == ["debt:loan", "debt:loan-2"]
    assert "debt:loan-2:min" in ids
    assert normalize(profile) == norm  # same profile -> same ids
