from pathlib import Path

from finagent.completeness import confirmation_message
from finagent.diff import ProfileDiff as ProfileDiffEmpty, diff_profiles
from finagent.schema import FinancialProfile

PROFILES = Path(__file__).resolve().parents[1] / "examples" / "profiles"
BASE = FinancialProfile.model_validate_json((PROFILES / "complete_profile.json").read_text())


def edit(**changes) -> FinancialProfile:
    data = BASE.model_dump(mode="json")
    for key, fn in changes.items():
        fn(data)
    return FinancialProfile.model_validate(data)


def test_identical_profiles_have_no_changes_even_if_meta_differs():
    other = BASE.model_copy(update={"meta": BASE.meta.model_copy(update={"profile_version": 9})})
    diff = diff_profiles(BASE, other)
    assert diff.is_empty
    assert confirmation_message(other, diff) == "Got it. Nothing in your finances changed, so your plan stays the same."


def test_changed_field():
    def raise_rent(d):
        d["recurring_expenses"][0]["amount"] = 1950
    diff = diff_profiles(BASE, edit(rent=raise_rent))
    assert [(c.section, c.name, [(f.field, f.old, f.new) for f in c.changes]) for c in diff.changed] == [
        ("recurring_expenses", "Rent", [("amount", 1800, 1950)])
    ]
    assert diff.describe() == ['updated Rent (amount $1,800 → $1,950)']


def test_added_and_removed_items():
    def changes(d):
        d["recurring_expenses"] = [e for e in d["recurring_expenses"] if e["name"] != "Yoga membership"]
        d["savings_goals"].append({"name": "Emergency fund", "target_amount": 3000, "deadline": None,
                                   "contribution": None, "priority": None, "effective_date": None})
    diff = diff_profiles(BASE, edit(x=changes))
    assert [(a.section, a.name) for a in diff.added] == [("savings_goals", "Emergency fund")]
    assert [(r.section, r.name) for r in diff.removed] == [("recurring_expenses", "Yoga membership")]
    msg = confirmation_message(edit(x=changes), diff)
    assert msg == 'Got it. I\'ve added goal "Emergency fund"; removed expense "Yoga membership". Your plan is being updated.'


def test_names_match_case_and_punctuation_insensitively():
    def rename(d):
        d["recurring_expenses"][2]["name"] = "eating-out"
    assert diff_profiles(BASE, edit(x=rename)).is_empty


def test_nested_and_scalar_changes():
    def changes(d):
        d["savings_goals"][0]["contribution"]["value"] = 650
        d["retirement"]["current_age"] = 25
        d["current_savings_balance"] = 7000
        d["overall_savings_target"] = None
    diff = diff_profiles(BASE, edit(x=changes))
    found = {(c.section, f.field): (f.old, f.new) for c in diff.changed for f in c.changes}
    assert found[("savings_goals", "contribution.value")] == (500, 650)
    assert found[("retirement", "current_age")] == (24, 25)
    assert found[("current_savings_balance", "current_savings_balance")] == (6500, 7000)
    assert found[("overall_savings_target", "type")] == ("percent_of_income", None)
    assert "updated current savings balance $6,500 → $7,000" in diff.describe()


def test_no_previous_profile_counts_everything_as_added():
    diff = diff_profiles(None, BASE)
    assert len(diff.added) == 1 + 4 + 1 + 1 + 1  # income, recurring, one-time, goals, debts
    assert {c.section for c in diff.changed} == {"retirement", "overall_savings_target", "current_savings_balance"}


def test_summary_names_at_most_three_changes():
    assert ProfileDiffEmpty().summary() == "Nothing in your finances changed, so your budgets are the same."

    def raise_rent(d):
        d["recurring_expenses"][0]["amount"] = 1950
    one = diff_profiles(BASE, edit(x=raise_rent))
    assert one.summary() == "Your budgets were updated: updated Rent (amount $1,800 → $1,950)."

    def many(d):
        for e in d["recurring_expenses"]:
            e["amount"] += 10
        d["current_savings_balance"] = 7000
    text = diff_profiles(BASE, edit(x=many)).summary()
    assert text.count("updated") == 4  # "were updated" + three changes
    assert text.endswith("There are 2 other changes too.")
