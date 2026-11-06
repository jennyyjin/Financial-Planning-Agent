"""What changed between two profile versions (ignoring `meta`).

List items are matched by name (income by `source`), case- and punctuation-
insensitively, so a renamed item shows up as one removal plus one addition.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, Field

from finagent.output.format import dollars
from finagent.schema import FinancialProfile

LIST_SECTIONS: dict[str, str] = {
    "income": "source",
    "recurring_expenses": "name",
    "one_time_expenses": "name",
    "savings_goals": "name",
    "debts": "name",
}
OBJECT_SECTIONS = ("retirement", "overall_savings_target")
SCALARS = ("current_savings_balance",)
MONEY_FIELDS = {
    "amount", "balance", "minimum_payment", "target_amount", "current_balance", "monthly_contribution",
    "target", "desired_annual_spending", "current_savings_balance",
}
SECTION_NOUNS = {
    "income": "income", "recurring_expenses": "expense", "one_time_expenses": "one-time cost",
    "savings_goals": "goal", "debts": "debt",
}


class FieldChange(BaseModel):
    field: str  # dotted for nested values, e.g. "contribution.value"
    old: Any = None
    new: Any = None


class ItemChange(BaseModel):
    section: str
    name: str
    changes: list[FieldChange] = Field(default_factory=list)


class ItemRef(BaseModel):
    section: str
    name: str


class ProfileDiff(BaseModel):
    added: list[ItemRef] = Field(default_factory=list)
    removed: list[ItemRef] = Field(default_factory=list)
    changed: list[ItemChange] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def describe(self) -> list[str]:
        """One short phrase per change, for the confirmation message."""
        lines = [f"added {SECTION_NOUNS.get(a.section, a.section)} \"{a.name}\"" for a in self.added]
        lines += [f"removed {SECTION_NOUNS.get(r.section, r.section)} \"{r.name}\"" for r in self.removed]
        for c in self.changed:
            if c.section in SCALARS:
                f = c.changes[0]
                lines.append(f"updated {c.name.replace('_', ' ')} {_fmt(f.field, f.old)} → {_fmt(f.field, f.new)}")
                continue
            what = ", ".join(f"{f.field.replace('_', ' ')} {_fmt(f.field, f.old)} → {_fmt(f.field, f.new)}" for f in c.changes)
            lines.append(f"updated {c.name.replace('_', ' ')} ({what})")
        return lines


    def summary(self, max_items: int = 3) -> str:
        """One or two sentences for the dashboard after an update, naming at most `max_items` changes."""
        if self.is_empty:
            return "Nothing in your finances changed, so your budgets are the same."
        lines = self.describe()
        shown = "; ".join(lines[:max_items])
        more = len(lines) - max_items
        text = f"Your budgets were updated: {shown}."
        if more > 0:
            text += f" There {'is' if more == 1 else 'are'} {more} other change{'' if more == 1 else 's'} too."
        return text


def _fmt(field: str, value: Any) -> str:
    if value is None:
        return "unknown"
    if field.split(".")[-1] in MONEY_FIELDS and isinstance(value, (int, float)):
        return dollars(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}{k}."))
        return out
    return {prefix[:-1]: value}


def _field_changes(old: Optional[dict], new: Optional[dict]) -> list[FieldChange]:
    old_flat, new_flat = _flatten(old or {}), _flatten(new or {})
    return [
        FieldChange(field=k, old=old_flat.get(k), new=new_flat.get(k))
        for k in dict.fromkeys([*old_flat, *new_flat])
        if old_flat.get(k) != new_flat.get(k)
    ]


def _keyed(items: list[dict], name_field: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in items:
        key, n = _key(item[name_field]), 2
        base = key
        while key in out:  # same name twice: keep both
            key, n = f"{base} {n}", n + 1
        out[key] = item
    return out


def diff_profiles(old: Optional[FinancialProfile], new: FinancialProfile) -> ProfileDiff:
    """Changes from `old` to `new`. With no previous profile, everything counts as added."""
    before = old.model_dump(mode="json", exclude={"meta"}) if old else {}
    after = new.model_dump(mode="json", exclude={"meta"})
    diff = ProfileDiff()
    for section, name_field in LIST_SECTIONS.items():
        a, b = _keyed(before.get(section, []), name_field), _keyed(after[section], name_field)
        diff.added += [ItemRef(section=section, name=b[k][name_field]) for k in b if k not in a]
        diff.removed += [ItemRef(section=section, name=a[k][name_field]) for k in a if k not in b]
        for k in a.keys() & b.keys():
            changes = [c for c in _field_changes(a[k], b[k]) if c.field != name_field]
            if changes:
                diff.changed.append(ItemChange(section=section, name=b[k][name_field], changes=changes))
    for section in OBJECT_SECTIONS:
        if old is None and after[section] is None:
            continue
        changes = _field_changes(before.get(section), after[section])
        if changes:
            diff.changed.append(ItemChange(section=section, name=section, changes=changes))
    for field in SCALARS:
        if before.get(field) != after[field] and not (old is None and after[field] is None):
            diff.changed.append(ItemChange(section=field, name=field,
                                           changes=[FieldChange(field=field, old=before.get(field), new=after[field])]))
    # Keep a stable order regardless of set iteration.
    diff.changed.sort(key=lambda c: (list(LIST_SECTIONS).index(c.section) if c.section in LIST_SECTIONS else 99, _key(c.name)))
    return diff
