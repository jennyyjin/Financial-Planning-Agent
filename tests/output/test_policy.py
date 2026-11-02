"""Tests for the expense flexibility rules (TDD §6, §14.1)."""

from __future__ import annotations

from datetime import date

import pytest

from finagent.output.config import DEFAULT_PROTECTED_CATEGORIES, PlanConfig
from finagent.output.normalize import normalize
from finagent.output.models import Bucket, BudgetLine, LineKind, Tier
from finagent.output.policy import classify_tier, is_adjustable, min_allowed, one_time_tier, validate_adjustment
from finagent.schema import ExpenseCategory
from conftest import ALL_PROFILES, load_profile

GOOD = "Dining out is the largest discretionary line, so trimming it closes the gap."
UNPROTECTED = [c for c in ExpenseCategory if c not in DEFAULT_PROTECTED_CATEGORIES]


def make_line(
    *, tier: Tier, kind: LineKind = LineKind.recurring, bucket: Bucket | None = Bucket.want,
    amount: float = 300.0, **kw,
) -> BudgetLine:
    return BudgetLine(
        id="rec:x", name="X", kind=kind, bucket=bucket, monthly_amount=amount,
        original_amount=amount, tier=tier, **kw,
    )


# --- classify_tier ---------------------------------------------------------------


@pytest.mark.parametrize("category", DEFAULT_PROTECTED_CATEGORIES)
@pytest.mark.parametrize("bucket", [Bucket.need, Bucket.want])
@pytest.mark.parametrize("is_fixed", [True, False, None])
@pytest.mark.parametrize("cap", [0.0, 0.3])
def test_protected_category_overrides_label(category, bucket, is_fixed, cap):
    assert classify_tier(bucket, category, is_fixed, PlanConfig(need_trim_cap=cap)) is Tier.protected


@pytest.mark.parametrize("category", UNPROTECTED)
@pytest.mark.parametrize("is_fixed", [True, False, None])
def test_need_protected_by_default(category, is_fixed, config):
    assert classify_tier(Bucket.need, category, is_fixed, config) is Tier.protected


@pytest.mark.parametrize("is_fixed, tier", [(False, Tier.scalable), (True, Tier.protected), (None, Tier.protected)])
def test_need_trim_cap_makes_only_variable_needs_scalable(is_fixed, tier):
    config = PlanConfig(need_trim_cap=0.2)
    assert classify_tier(Bucket.need, ExpenseCategory.groceries, is_fixed, config) is tier


@pytest.mark.parametrize("category", UNPROTECTED)
@pytest.mark.parametrize("is_fixed, tier", [(True, Tier.drop_only), (False, Tier.scalable), (None, Tier.scalable)])
def test_want_tiers(category, is_fixed, tier, config):
    assert classify_tier(Bucket.want, category, is_fixed, config) is tier


def test_no_category_uses_label(config):
    assert classify_tier(Bucket.want, None, True, config) is Tier.drop_only
    assert classify_tier(Bucket.need, None, False, config) is Tier.protected


def test_custom_protected_categories():
    config = PlanConfig(protected_categories=[ExpenseCategory.fitness])
    assert classify_tier(Bucket.want, ExpenseCategory.fitness, True, config) is Tier.protected
    assert classify_tier(Bucket.want, ExpenseCategory.housing, False, config) is Tier.scalable


def test_savings_bucket(config):
    assert classify_tier(Bucket.savings, None, None, config) is Tier.savings


def test_one_time_tier():
    assert one_time_tier(False) is Tier.protected
    assert one_time_tier(True) is Tier.scalable


# --- min_allowed / is_adjustable ---------------------------------------------------


def test_is_adjustable():
    assert is_adjustable(make_line(tier=Tier.scalable))
    assert is_adjustable(make_line(tier=Tier.drop_only))
    assert not is_adjustable(make_line(tier=Tier.protected))
    assert not is_adjustable(make_line(tier=Tier.savings, bucket=Bucket.savings, kind=LineKind.goal_contribution))


def test_min_allowed(config):
    assert min_allowed(make_line(tier=Tier.protected, bucket=Bucket.need), config) == pytest.approx(300)
    assert min_allowed(make_line(tier=Tier.savings, bucket=Bucket.savings), config) == pytest.approx(300)
    assert min_allowed(make_line(tier=Tier.scalable), config) == 0
    assert min_allowed(make_line(tier=Tier.drop_only), config) == 0
    one_time = make_line(tier=Tier.scalable, kind=LineKind.one_time, bucket=None, discretionary=True)
    assert min_allowed(one_time, config) == 0


def test_min_allowed_variable_need_floor():
    config = PlanConfig(need_trim_cap=0.2)
    need = make_line(tier=Tier.scalable, bucket=Bucket.need, amount=500)
    assert min_allowed(need, config) == pytest.approx(400)
    assert min_allowed(make_line(tier=Tier.scalable, amount=500), config) == 0  # wants unaffected


# --- validate_adjustment: accepted -------------------------------------------------


@pytest.mark.parametrize("new_amount", [0.0, 0.004, 120.0, 299.99, 300.0, 300.005])
def test_scalable_want_accepts_any_amount_up_to_current(new_amount, config):
    assert validate_adjustment(make_line(tier=Tier.scalable), new_amount, GOOD, config) is None


@pytest.mark.parametrize("new_amount", [0.0, 120.0])
def test_drop_only_accepts_keep_or_drop(new_amount, config):
    assert validate_adjustment(make_line(tier=Tier.drop_only, amount=120), new_amount, GOOD, config) is None


def test_discretionary_one_time_can_be_dropped(config):
    trip = make_line(tier=Tier.scalable, kind=LineKind.one_time, bucket=None, amount=2500,
                     discretionary=True, expected_date=date(2027, 3, 15))
    assert validate_adjustment(trip, 0, GOOD, config) is None


def test_variable_need_trim_within_cap():
    config = PlanConfig(need_trim_cap=0.2)
    groceries = make_line(tier=Tier.scalable, bucket=Bucket.need, amount=500)
    assert validate_adjustment(groceries, 400, GOOD, config) is None
    assert validate_adjustment(groceries, 450, GOOD, config) is None


def test_rationale_exactly_minimum_length(config):
    rationale = "x" * config.min_rationale_chars
    assert validate_adjustment(make_line(tier=Tier.scalable), 100, rationale, config) is None


# --- validate_adjustment: rejected -------------------------------------------------


@pytest.mark.parametrize("new_amount", [0.0, 150.0, 300.0])
def test_protected_rejected(new_amount, config):
    error = validate_adjustment(make_line(tier=Tier.protected, bucket=Bucket.need), new_amount, GOOD, config)
    assert error and "protected" in error


def test_debt_minimum_rejected(config):
    debt = make_line(tier=Tier.protected, kind=LineKind.debt_min, bucket=Bucket.need, amount=210)
    assert validate_adjustment(debt, 0, GOOD, config)


@pytest.mark.parametrize("kind", [LineKind.goal_contribution, LineKind.retirement_contribution])
def test_savings_line_rejected(kind, config):
    error = validate_adjustment(make_line(tier=Tier.savings, kind=kind, bucket=Bucket.savings), 0, GOOD, config)
    assert error and "savings" in error


def test_non_discretionary_one_time_rejected(config):
    laptop = make_line(tier=Tier.protected, kind=LineKind.one_time, bucket=None, amount=1500)
    error = validate_adjustment(laptop, 0, GOOD, config)
    assert error and "mark_one_time_discretionary" in error


def test_negative_rejected(config):
    error = validate_adjustment(make_line(tier=Tier.scalable), -5, GOOD, config)
    assert error and "negative" in error


@pytest.mark.parametrize("tier", [Tier.scalable, Tier.drop_only])
def test_increase_rejected(tier, config):
    error = validate_adjustment(make_line(tier=tier), 300.5, GOOD, config)
    assert error and "increases" in error


@pytest.mark.parametrize("new_amount", [1.0, 60.0, 119.0])
def test_drop_only_partial_rejected(new_amount, config):
    error = validate_adjustment(make_line(tier=Tier.drop_only, amount=120), new_amount, GOOD, config)
    assert error and "all-or-nothing" in error


def test_variable_need_below_floor_rejected():
    config = PlanConfig(need_trim_cap=0.2)
    groceries = make_line(tier=Tier.scalable, bucket=Bucket.need, amount=500)
    error = validate_adjustment(groceries, 399, GOOD, config)
    assert error and "400.00" in error
    assert validate_adjustment(groceries, 0, GOOD, config)


@pytest.mark.parametrize("rationale", ["", "   ", "too short", " " * 30 + "short" + " " * 30])
@pytest.mark.parametrize("tier", [Tier.scalable, Tier.drop_only])
def test_short_or_blank_rationale_rejected(rationale, tier, config):
    error = validate_adjustment(make_line(tier=tier), 0, rationale, config)
    assert error and "rationale" in error


def test_rationale_threshold_configurable():
    assert validate_adjustment(make_line(tier=Tier.scalable), 0, "short", PlanConfig(min_rationale_chars=5)) is None


# --- end to end: essential lines in every fixture can never be cut ----------------------


@pytest.mark.parametrize("name", ALL_PROFILES)
@pytest.mark.parametrize("cap", [0.0, 0.3])
def test_essential_lines_in_fixtures_reject_any_cut(name, cap):
    config = PlanConfig(need_trim_cap=cap)
    profile = load_profile(name)
    norm = normalize(profile, config)
    by_ref = {f"/recurring_expenses/{i}": exp for i, exp in enumerate(profile.recurring_expenses)}
    for ln in norm.lines + norm.one_time:
        exp = by_ref.get(ln.source_ref) if ln.kind is LineKind.recurring else None
        essential = (
            ln.kind in (LineKind.debt_min, LineKind.one_time)
            or ln.bucket is Bucket.savings
            or (exp is not None and (exp.category in config.protected_categories or exp.need_or_want is None))
            or (exp is not None and ln.bucket is Bucket.need and (cap == 0 or exp.is_fixed is not False))
        )
        if essential:
            assert validate_adjustment(ln, 0, GOOD, config), ln.id
