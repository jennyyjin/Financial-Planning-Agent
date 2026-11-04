"""Budget Restructurer Agent (TDD §11.2, D4).

One agent session per restructured budget. The agent chooses which wants to
cut and writes the rationales; the budget tools validate every change and
compute every number. If the agent does not finalize (step limit, model
failure), the rule-based fallback restructurer produces the budget instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from finagent.output import budget as budget_ops
from finagent.output import goals as goal_math
from finagent.output.agents.prompts import prompt_path, read_prompt
from finagent.output.agents.runtime import AgentResult, ChatModel, run_agent
from finagent.output.budget import Restructurer
from finagent.output.config import PlanConfig
from finagent.output.format import cents as _money
from finagent.output.models import Budget, BudgetId, NormalizedProfile, TraceStep
from finagent.output.tools.budget_tools import BudgetSession, build_budget_tools

PROMPT_PATH = prompt_path("budget_restructurer")


def load_prompt(path: Path = PROMPT_PATH) -> str:
    return read_prompt(path)


def _rule_text(session: BudgetSession) -> str:
    b, t, income = session.budget, session.budget.targets, session.budget.income_monthly
    if b.id is BudgetId.rule_50_30_20:
        return (
            f"Restructure this budget to the 50/30/20 rule on net income of {_money(income)}/mo: needs at most "
            f"{_money(t.need_max)} (50%), wants at most {_money(t.want_max)} (30%), savings at least "
            f"{_money(t.savings_min)} (20%)."
        )
    return (
        f"Restructure this budget to fund the saving goals first, then split the rest 5:3 between needs and wants. "
        f"Net income is {_money(income)}/mo; savings must be at least {_money(t.savings_min)} (the goal "
        f"requirements plus retirement, or the overall savings target if larger). The remaining "
        f"{_money(max(0.0, income - t.savings_min))} is split into needs at most {_money(t.need_max)} and wants "
        f"at most {_money(t.want_max)}."
    )


def build_user_message(session: BudgetSession) -> str:
    """The task for one rule, with the key numbers taken from the session."""
    b, norm, t = session.budget, session.norm, session.budget.targets
    allowance, over = session.allowance(), session.wants_over_allowance()
    parts = [_rule_text(session)]
    parts.append(
        f"Needs are {_money(b.totals.need)}/mo and are protected. Because the savings floor comes before wants, "
        f"the wants allowance is {_money(allowance)}/mo; wants are currently {_money(b.totals.want)}/mo."
    )
    set_aside = budget_ops.one_time_set_aside(b, norm, session.config)
    if set_aside > session.config.money_tolerance:
        parts.append(
            f"Upcoming one-time costs need {_money(set_aside)}/mo kept in cash; that is spending, not savings, so it "
            "comes out of the money before the wants allowance."
        )
    if b.totals.need > t.need_max + session.config.money_tolerance:
        parts.append(
            f"Needs exceed their {_money(t.need_max)} ceiling. That gap cannot be closed by cutting wants; "
            "report it in your summary."
        )
    if over > session.config.money_tolerance:
        parts.append(
            f"Wants are {_money(over)}/mo over the allowance. Cut non-essential expenses (scalable or drop_only "
            "wants) until they fit, or until nothing allowed is left to cut."
        )
    else:
        parts.append("Wants are already within the allowance, so no cuts are needed: make none and finalize.")
    if b.one_time:
        items = "; ".join(
            f"{l.name} ({_money(l.monthly_amount)}, expected {l.expected_date.isoformat() if l.expected_date else 'date unknown'})"
            for l in b.one_time
        )
        parts.append(
            f"One-time items, treated as essential unless reclassified: {items}. Current savings are "
            f"{_money(norm.current_savings_balance)}."
        )
    goals = [g for g in norm.goals if g.deadline is not None]
    if goals:
        desc = "; ".join(
            f"{g.name} ({_money(g.target_amount or 0)} by {g.deadline.isoformat()}, needs "
            f"{_money(goal_math.required_monthly(g, norm.start_month) or 0)}/mo)"
            for g in goals
        )
        parts.append(f"Goals with deadlines: {desc}.")
    parts.append("Start with get_budget_state and finish with finalize.")
    return "\n\n".join(parts)


def restructure_with_agent(
    norm: NormalizedProfile,
    budget_id: BudgetId,
    config: PlanConfig,
    model: ChatModel,
    *,
    max_steps: Optional[int] = None,
    on_step: Optional[Callable[[TraceStep], None]] = None,
) -> tuple[Budget, AgentResult]:
    """Run the restructurer agent for one rule; fall back to the rule-based restructurer if it fails."""
    session = BudgetSession.start(norm, budget_id, config)
    registry = build_budget_tools(session)
    # Tool exceptions come back to the model as errors (runtime.dispatch), so the trace survives them; anything
    # else propagates to the pipeline, which reports `agent_error` and falls back (same as the advisor).
    result = run_agent(
        model, load_prompt(), registry, build_user_message(session),
        agent_name=f"restructurer:{budget_id.value}", max_steps=max_steps or config.agent_max_steps,
        max_tokens=config.agent_max_tokens, on_step=on_step,
    )
    if result.completed and session.finalized is not None:
        return session.finalized, result
    return budget_ops.fallback_restructure(norm, budget_id, config), result


def make_agent_restructurer(
    model: ChatModel,
    results: Optional[list[AgentResult]] = None,
    on_step: Optional[Callable[[TraceStep], None]] = None,
) -> Restructurer:
    """A `build_budgets(restructure=...)` callable; each run's AgentResult is appended to `results`."""

    def restructure(norm: NormalizedProfile, budget_id: BudgetId, config: PlanConfig) -> Budget:
        budget, result = restructure_with_agent(norm, budget_id, config, model, on_step=on_step)
        if results is not None:
            results.append(result)
        return budget

    return restructure
