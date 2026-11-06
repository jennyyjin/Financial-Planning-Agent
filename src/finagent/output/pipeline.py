"""Output-phase pipeline (TDD §4, §11.4).

A fixed sequence: normalize -> three budgets -> 12-month forecasts ->
retirement projections -> metrics -> recommendation -> PlanOutput. With
`model=None` every step is deterministic (fallback restructurer and templated
advice), which is the evaluation baseline. With a model, the Budget
Restructurer and Advisor agents make the judgment calls; any agent failure,
including an exception, falls back so the plan always completes (D4).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Union

from finagent.output import budget as budget_ops
from finagent.output import forecast as forecast_ops
from finagent.output import retirement as retirement_ops
from finagent.output.advice import PlanContext, fallback_recommendation
from finagent.output.agents.advisor import advise_with_agent
from finagent.output.agents.restructurer import make_agent_restructurer
from finagent.output.agents.runtime import AgentResult, ChatModel
from finagent.output.config import PlanConfig
from finagent.output.metrics import compute_metrics
from finagent.output.models import (
    AdjustmentAction,
    Budget,
    BudgetId,
    GeneratedBy,
    NormalizedProfile,
    PlanMeta,
    PlanOutput,
    PlanWarning,
    Recommendation,
    TraceStep,
)
from finagent.output.normalize import normalize
from finagent.schema import FinancialProfile

ProfileSource = Union[FinancialProfile, dict, str, Path]

# Progress events for a live front end: {"kind": "stage", "stage": str, "budget"?: str} when a pipeline step
# starts, and {"kind": "step", "step": TraceStep} for every agent step.
ProgressEvent = dict[str, Any]
OnEvent = Callable[[ProgressEvent], None]


class PlanCancelled(BaseException):
    """Raise from an `on_event` callback to stop a run (e.g. the client went away).

    A BaseException so the per-agent `except Exception` fallbacks do not swallow it and carry on spending tokens.
    """


def _fallback_warning(agent: str, error: Optional[str]) -> PlanWarning:
    why = f" ({error})" if error else ""
    return PlanWarning(
        code="agent_fallback",
        message=f"The {agent} agent did not finish{why}; the deterministic fallback was used.",
        ref=agent,
    )


def _error_warning(agent: str, exc: Exception) -> PlanWarning:
    return PlanWarning(
        code="agent_error",
        message=f"The {agent} agent raised {exc!r}; the deterministic fallback was used.",
        ref=agent,
    )


def _usage_by_agent(results: list[AgentResult]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for result in results:
        name = result.trace[0].agent if result.trace else "agent"
        totals = out.setdefault(name, {})
        for key, value in result.usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return out


def _agent_warnings(budgets: dict[BudgetId, Budget], trace: list[TraceStep]) -> list[PlanWarning]:
    """Make agent decisions that evaluation must see explicit: one-time reclassifications and tool crashes."""
    out = []
    for bid, budget in budgets.items():
        for adj in budget.adjustments:
            if adj.action is AdjustmentAction.reclassify:
                name = next((l.name for l in budget.one_time if l.id == adj.line_id), adj.line_id)
                out.append(PlanWarning(
                    code="one_time_reclassified",
                    message=f"The {bid.value} restructurer marked the one-time expense '{name}' as non-essential: "
                    f"{adj.rationale}",
                    ref=adj.line_id,
                ))
    for step in trace:
        if isinstance(step.result, dict) and step.result.get("internal_error"):
            out.append(PlanWarning(code="agent_tool_error", message=f"{step.agent}: {step.result.get('error')}",
                                   ref=step.agent))
    return out


def _build_budgets(
    norm: NormalizedProfile, config: PlanConfig, model: Optional[ChatModel],
    results: list[AgentResult], warnings: list[PlanWarning], emit: OnEvent, on_step: Callable[[TraceStep], None],
) -> dict[BudgetId, Budget]:
    agent_restructure = make_agent_restructurer(model, results, on_step=on_step) if model is not None else None

    def restructure(n: NormalizedProfile, budget_id: BudgetId, c: PlanConfig) -> Budget:
        emit({"kind": "stage", "stage": "restructure", "budget": budget_id.value})
        if agent_restructure is None:
            return budget_ops.fallback_restructure(n, budget_id, c)
        agent = f"restructurer:{budget_id.value}"
        try:
            budget = agent_restructure(n, budget_id, c)
        except Exception as exc:  # never let an agent break the plan (D4)
            warnings.append(_error_warning(agent, exc))
            return budget_ops.fallback_restructure(n, budget_id, c)
        if budget.generated_by is GeneratedBy.fallback:
            warnings.append(_fallback_warning(agent, results[-1].error if results else None))
        return budget

    return budget_ops.build_budgets(norm, config, restructure)


def _recommend(
    context: PlanContext, model: Optional[ChatModel], results: list[AgentResult], warnings: list[PlanWarning],
    on_step: Callable[[TraceStep], None],
) -> Recommendation:
    if model is None:
        return fallback_recommendation(context)
    try:
        rec, result = advise_with_agent(context, model, on_step=on_step)
    except Exception as exc:  # e.g. a bug in an advisor tool
        warnings.append(_error_warning("advisor", exc))
        return fallback_recommendation(context)
    results.append(result)
    if rec.generated_by is GeneratedBy.fallback:
        warnings.append(_fallback_warning("advisor", result.error))
    return rec


def generate_plan(
    profile: FinancialProfile,
    config: PlanConfig | None = None,
    model: ChatModel | None = None,
    *,
    generated_at: datetime | None = None,
    on_event: OnEvent | None = None,
) -> PlanOutput:
    """Run the whole output phase on one profile. `model=None` gives a fully deterministic plan.

    `on_event` receives a ProgressEvent as each step starts and for every agent step, so a front end can show the
    run live. It does not change the plan; raise `PlanCancelled` from it to stop the run.
    """
    config = config or PlanConfig()

    def emit(event: ProgressEvent) -> None:
        if on_event is not None:
            on_event(event)

    def on_step(step: TraceStep) -> None:
        emit({"kind": "step", "step": step})

    emit({"kind": "stage", "stage": "normalize"})
    norm = normalize(profile, config)
    results: list[AgentResult] = []
    pipeline_warnings: list[PlanWarning] = []

    budgets = _build_budgets(norm, config, model, results, pipeline_warnings, emit, on_step)
    emit({"kind": "stage", "stage": "forecast"})
    forecasts = {bid: forecast_ops.forecast_budget(b, norm, config) for bid, b in budgets.items()}
    emit({"kind": "stage", "stage": "retirement"})
    retirement = {
        bid: retirement_ops.project_retirement(b, forecasts[bid], norm, config) for bid, b in budgets.items()
    }
    emit({"kind": "stage", "stage": "metrics"})
    metrics = compute_metrics(norm, budgets, forecasts, retirement, config)
    context = PlanContext(norm, config, budgets, forecasts, retirement, metrics)
    emit({"kind": "stage", "stage": "advice"})
    recommendation = _recommend(context, model, results, pipeline_warnings, on_step)

    trace: list[TraceStep] = [step for result in results for step in result.trace]
    pipeline_warnings += _agent_warnings(budgets, trace)
    meta = PlanMeta(
        user_id=norm.user_id,
        profile_version=norm.profile_version,
        generated_at=generated_at or datetime.now().astimezone(),
        start_month=norm.start_month,
        config=config,
        model=getattr(model, "name", None) if model is not None else None,
        agent_usage=_usage_by_agent(results),
        warnings=list(norm.warnings) + pipeline_warnings,
    )
    return PlanOutput(
        meta=meta, budgets=budgets, forecasts=forecasts, retirement=retirement,
        metrics=metrics, recommendation=recommendation, trace=trace,
    )


def load_profile(source: ProfileSource) -> FinancialProfile:
    """A FinancialProfile from a model, a dict, a JSON string, or a path to a JSON file."""
    if isinstance(source, FinancialProfile):
        return source
    if isinstance(source, dict):
        return FinancialProfile.model_validate(source)
    if isinstance(source, str) and source.lstrip().startswith("{"):
        return FinancialProfile.model_validate_json(source)
    return FinancialProfile.model_validate(json.loads(Path(source).read_text()))


def plan_from_json(
    source: ProfileSource,
    config: PlanConfig | None = None,
    model: ChatModel | None = None,
    *,
    generated_at: datetime | None = None,
) -> PlanOutput:
    """`generate_plan` on a profile given as a path, JSON string, or dict."""
    return generate_plan(load_profile(source), config, model, generated_at=generated_at)


def plan_to_json(plan: PlanOutput, indent: int | None = 2) -> str:
    return plan.model_dump_json(indent=indent)
