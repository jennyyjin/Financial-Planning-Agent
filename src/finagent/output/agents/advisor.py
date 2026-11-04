"""Advisor Agent (TDD §10, §11.3, D4).

The agent reads the computed plan through the advisor tools and writes the
recommendation. If it does not submit an accepted recommendation (step limit,
model failure, repeated ungrounded numbers), the deterministic templated
recommendation is used instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from finagent.output.advice import PlanContext, fallback_recommendation
from finagent.output.agents.prompts import prompt_path, read_prompt
from finagent.output.agents.runtime import AgentResult, ChatModel, run_agent
from finagent.output.format import BUDGET_LABELS
from finagent.output.models import Recommendation
from finagent.output.tools.advisor_tools import AdvisorSession, build_advisor_tools

PROMPT_PATH = prompt_path("advisor")


def load_prompt(path: Path = PROMPT_PATH) -> str:
    return read_prompt(path)


def build_user_message(context: PlanContext) -> str:
    """The task; deliberately numberless so every figure comes from a tool call."""
    budgets = ", ".join(f"{bid.value} ({BUDGET_LABELS[bid]})" for bid in context.budgets)
    return (
        "Write the overall recommendation for this financial plan. "
        f"The plan has these budgets: {budgets}. "
        "Start with compare_budgets, read the sections you need, and finish by calling submit_recommendation."
    )


def advise_with_agent(
    context: PlanContext, model: ChatModel, *, max_steps: Optional[int] = None
) -> tuple[Recommendation, AgentResult]:
    """Run the Advisor Agent; falls back to the templated recommendation if it fails."""
    session = AdvisorSession(context)
    result = run_agent(
        model,
        load_prompt(),
        build_advisor_tools(session),
        build_user_message(context),
        agent_name="advisor",
        max_steps=max_steps or context.config.agent_max_steps,
        max_tokens=context.config.agent_max_tokens,
    )
    if result.completed and session.submitted is not None:
        return session.submitted, result
    if result.error is None:
        result.error = session.failed or "no recommendation submitted"
    return fallback_recommendation(context), result
