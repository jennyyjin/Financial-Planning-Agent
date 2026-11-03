"""Output phase: budget variants, forecasts, retirement projections and advice.

    from finagent.output import generate_plan, render_text
    plan = generate_plan(profile)          # model=None: fully deterministic
    print(render_text(plan))

Exports are resolved lazily so `python -m finagent.output.models` (schema
export) does not import the whole pipeline first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["generate_plan", "render_text", "PlanConfig", "PlanOutput"]

_EXPORTS = {
    "generate_plan": "finagent.output.pipeline",
    "render_text": "finagent.output.render",
    "PlanConfig": "finagent.output.config",
    "PlanOutput": "finagent.output.models",
}

if TYPE_CHECKING:
    from finagent.output.config import PlanConfig
    from finagent.output.models import PlanOutput
    from finagent.output.pipeline import generate_plan
    from finagent.output.render import render_text


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module 'finagent.output' has no attribute '{name}'")
    import importlib

    return getattr(importlib.import_module(_EXPORTS[name]), name)
