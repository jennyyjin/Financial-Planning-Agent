"""Required-field check, follow-up questions and the messages shown to the user.

Code decides what is missing (`find_missing`). The LLM only words the follow-up
questions (`follow_up_questions`), with a templated fallback if it fails. The
confirmation shown once the profile is complete is built deterministically, so
its numbers always match the profile.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from finagent.diff import ProfileDiff
from finagent.llm import ChatModel, LLMError, Message, UsageLog, complete_json
from finagent.output.format import dollars
from finagent.schema import FinancialProfile, monthly_amount

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "input" / "follow_up_questions.md"

CLARIFICATION_MESSAGE = "Thanks! I need a few more details before I can build your plan."


@dataclass(frozen=True)
class RequiredFields:
    """Which fields must be known before planning. Edit to make the check stricter or looser."""

    require_income: bool = True
    income: tuple[str, ...] = ("amount", "frequency", "is_net")
    recurring_expenses: tuple[str, ...] = ("amount", "frequency")
    one_time_expenses: tuple[str, ...] = ("amount",)
    debts: tuple[str, ...] = ("balance", "apr", "minimum_payment")
    savings_goals: tuple[str, ...] = ("target_amount",)
    goal_needs_deadline_or_contribution: bool = True
    retirement: tuple[str, ...] = ("current_age", "target_retirement_age")  # only if retirement is mentioned


DEFAULT_REQUIRED = RequiredFields()

# How each missing field is described in templated questions (and to the LLM).
FIELD_PHRASES: dict[str, str] = {
    "amount": "the amount",
    "frequency": "how often it's paid (for example weekly or monthly)",
    "is_net": "whether that's before or after tax",
    "balance": "the remaining balance",
    "apr": "the interest rate (APR)",
    "minimum_payment": "the minimum monthly payment",
    "target_amount": "how much you want to save in total",
    "deadline_or_contribution": "when you want to reach it, or how much you plan to put toward it each month",
    "current_age": "your current age",
    "target_retirement_age": "the age you'd like to retire",
    "income": "how much you earn, how often you're paid, and whether that's before or after tax",
}


@dataclass
class MissingItem:
    path: str  # JSON pointer to the item, e.g. "/debts/0"
    section: str  # profile section, e.g. "debts"
    label: str  # the item's name as recorded, e.g. "Student loan"
    fields: list[str] = field(default_factory=list)


def _missing_fields(item: object, names: tuple[str, ...]) -> list[str]:
    return [n for n in names if getattr(item, n) is None]


def find_missing(profile: FinancialProfile, required: RequiredFields = DEFAULT_REQUIRED) -> list[MissingItem]:
    """Every required field the profile doesn't have yet, grouped by item."""
    missing: list[MissingItem] = []

    def check(section: str, items: list, names: tuple[str, ...], label_attr: str = "name") -> None:
        for i, item in enumerate(items):
            fields = _missing_fields(item, names)
            if section == "savings_goals" and required.goal_needs_deadline_or_contribution \
                    and item.deadline is None and item.contribution is None:
                fields.append("deadline_or_contribution")
            if fields:
                missing.append(MissingItem(f"/{section}/{i}", section, getattr(item, label_attr), fields))

    if required.require_income and not profile.income:
        missing.append(MissingItem("/income", "income", "income", ["income"]))
    check("income", profile.income, required.income, label_attr="source")
    check("recurring_expenses", profile.recurring_expenses, required.recurring_expenses)
    check("one_time_expenses", profile.one_time_expenses, required.one_time_expenses)
    check("debts", profile.debts, required.debts)
    check("savings_goals", profile.savings_goals, required.savings_goals)
    if profile.retirement is not None:
        fields = _missing_fields(profile.retirement, required.retirement)
        if fields:
            missing.append(MissingItem("/retirement", "retirement", "retirement", fields))
    return missing


# --- Follow-up questions ----------------------------------------------------------


@dataclass
class FollowUp:
    questions: list[str]
    generated_by: Literal["llm", "template"]


def _join(phrases: list[str]) -> str:
    if len(phrases) <= 2:
        return " and ".join(phrases)
    return ", ".join(phrases[:-1]) + ", and " + phrases[-1]


def template_question(item: MissingItem) -> str:
    phrases = _join([FIELD_PHRASES[f] for f in item.fields])
    if item.fields == ["income"]:
        return f"Could you tell me {phrases}?"
    if item.section == "retirement":
        return f"For retirement planning, could you tell me {phrases}?"
    return f'For "{item.label}", could you tell me {phrases}?'


def template_questions(missing: list[MissingItem]) -> list[str]:
    return [template_question(m) for m in missing]


@lru_cache(maxsize=1)
def system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Follow-up prompt not found at {PROMPT_PATH}. Run from a source checkout or an editable install."
        ) from exc


def _llm_input(missing: list[MissingItem]) -> str:
    items = [{**asdict(m), "fields": [{"name": f, "meaning": FIELD_PHRASES[f]} for f in m.fields]} for m in missing]
    return json.dumps({"missing": items}, indent=2)


def follow_up_questions(
    missing: list[MissingItem], model: Optional[ChatModel] = None, *, log: Optional[UsageLog] = None
) -> FollowUp:
    """Questions asking for the missing fields: one per item, worded by the LLM if a model is given."""
    if not missing:
        return FollowUp([], "template")
    if model is not None:
        messages = [Message(role="system", content=system_prompt()), Message(role="user", content=_llm_input(missing))]
        try:
            data, _ = complete_json(model, messages, tag="follow_up", log=log)
            questions = data.get("questions")
            if isinstance(questions, list) and len(questions) == len(missing) \
                    and all(isinstance(q, str) and q.strip() for q in questions):
                return FollowUp([q.strip() for q in questions], "llm")
        except LLMError:
            pass
    return FollowUp(template_questions(missing), "template")


# --- Confirmation -------------------------------------------------------------------


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _monthly_total(items: list) -> float:
    return sum(monthly_amount(i) or 0.0 for i in items)


def confirmation_message(profile: FinancialProfile, diff: Optional[ProfileDiff] = None) -> str:
    """Shown once every required field is known: what changed since the last version
    (if `diff` is given), otherwise a summary of everything recorded."""
    if diff is not None:
        if diff.is_empty:
            return "Got it. Nothing in your finances changed, so your plan stays the same."
        return f"Got it. I've {'; '.join(diff.describe())}. Your plan is being updated."
    parts = []
    if profile.income:
        tax = {True: " after tax", False: " before tax"}
        income = ", ".join(
            f"{dollars(monthly_amount(i))}/month{tax.get(i.is_net, '')} from {i.source}" for i in profile.income
        )
        parts.append(f"income of {income}")
    if profile.recurring_expenses:
        parts.append(f"{_count(len(profile.recurring_expenses), 'recurring expense')} "
                     f"totalling {dollars(_monthly_total(profile.recurring_expenses))}/month")
    if profile.one_time_expenses:
        parts.append(_count(len(profile.one_time_expenses), "upcoming one-time cost"))
    if profile.savings_goals:
        names = ", ".join(g.name for g in profile.savings_goals)
        parts.append(f"{_count(len(profile.savings_goals), 'savings goal')} ({names})")
    if profile.debts:
        balance = sum(d.balance or 0.0 for d in profile.debts)
        parts.append(f"{_count(len(profile.debts), 'debt')} with {dollars(balance)} owed")
    if profile.retirement is not None and profile.retirement.monthly_contribution:
        parts.append(f"{dollars(profile.retirement.monthly_contribution)}/month toward retirement")
    if profile.current_savings_balance:
        parts.append(f"{dollars(profile.current_savings_balance)} in savings")
    summary = "; ".join(parts) if parts else "your details"
    return f"Got it. I've recorded {summary}. Your plan is being updated."
