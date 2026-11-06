"""Pipeline ProgressEvent -> a short message for the dashboard's live log.

Only wording lives here. The one number shown is the amount an agent proposed in `adjust_item`; a change the
rules reject is shown as rejected, with the tool's own error.
"""

from __future__ import annotations

from typing import Any, Optional

from finagent.output.models import TraceStep

BUDGET_NAMES = {"current": "current", "50_30_20": "50/30/20", "goals_5_3": "goals + 5:3"}
STAGES = {
    "normalize": "Reading your numbers",
    "forecast": "Forecasting the next 12 months",
    "retirement": "Projecting retirement",
    "metrics": "Checking the key ratios",
    "advice": "Writing the recommendation",
}


def _budget(budget_id: Any) -> str:
    return BUDGET_NAMES.get(str(budget_id), str(budget_id))


def _line(line_id: Any) -> str:
    """'rec:eating-out' -> 'eating out'."""
    return str(line_id).split(":", 1)[-1].replace("-", " ")


def _money(amount: Any) -> str:
    return f"${amount:,.0f}" if isinstance(amount, (int, float)) else "?"


def _who(agent: str) -> str:
    if agent.startswith("restructurer:"):
        return f"Restructurer ({_budget(agent.split(':', 1)[1])})"
    return agent.capitalize()


def _step_label(step: TraceStep) -> tuple[str, Optional[str]]:
    tool, args, result = step.tool, step.arguments or {}, step.result
    error = result.get("error") if isinstance(result, dict) else None
    if tool == "adjust_item":
        if error:
            return f"A change to {_line(args.get('line_id'))} was rejected by the rules", str(error)
        amount = args.get("new_monthly_amount")
        verb = "Dropping" if amount == 0 else "Cutting"
        tail = "" if amount == 0 else f" to {_money(amount)}"
        return f"{verb} {_line(args.get('line_id'))}{tail}", args.get("rationale")
    if tool == "revert_item":
        return f"Undoing the change to {_line(args.get('line_id'))}", error
    if tool == "mark_one_time_discretionary":
        return f"Treating {_line(args.get('line_id'))} as non-essential", args.get("rationale") if not error else error
    if tool in ("get_budget_state", "list_adjustable"):
        return "Reviewing the budget", error
    if tool == "finalize":
        return ("Finishing the budget", args.get("summary_rationale")) if not error else ("Finalize was rejected", str(error))
    if tool == "compare_budgets":
        return "Comparing the three budgets", error
    if tool == "get_section":
        return f"Reading the {args.get('name', 'plan')} details", error
    if tool == "simulate_retirement":
        return f"Testing {_budget(args.get('budget_id'))} with {_money(args.get('extra_monthly'))} extra a month", error
    if tool == "submit_recommendation":
        if error:
            return "The recommendation was rejected; revising it", str(error)
        return "Submitting the recommendation", args.get("summary")
    if tool:
        return tool.replace("_", " ").capitalize(), error
    return "Thinking it over", step.text


def describe(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The JSON line to send for a pipeline event, or None if there is nothing to show."""
    if event.get("kind") == "stage":
        stage = event["stage"]
        if stage == "restructure":
            return {"type": "progress", "stage": stage, "label": f"Building the {_budget(event.get('budget'))} budget"}
        return {"type": "progress", "stage": stage, "label": STAGES.get(stage, stage)}
    if event.get("kind") == "step":
        step: TraceStep = event["step"]
        if step.tool == "submit_recommendation" and isinstance(step.result, dict) and step.result.get("error"):
            return None  # rejected drafts are internal retries; the page shows only the accepted submission
        label, detail = _step_label(step)
        out: dict[str, Any] = {"type": "progress", "agent": step.agent, "who": _who(step.agent), "label": label}
        if detail:
            out["detail"] = str(detail)
        return out
    return None
