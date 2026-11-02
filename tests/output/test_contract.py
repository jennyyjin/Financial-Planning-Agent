"""Output contract tests (TDD §12, §14.5): the exported PlanOutput schema and the example plans."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from conftest import ALL_PROFILES, ROOT, load_profile
from finagent.output import PlanOutput, generate_plan
from finagent.output.models import PLAN_SCHEMA_PATH, plan_json_schema

FIXED = datetime(2026, 10, 28, 15, 0, 0)
EXAMPLES = ROOT / "examples" / "plans"


def test_exported_plan_schema_is_up_to_date():
    assert json.loads(PLAN_SCHEMA_PATH.read_text()) == plan_json_schema(), "Run: python -m finagent.output.models"


def test_schema_top_level_matches_tdd():
    schema = json.loads(PLAN_SCHEMA_PATH.read_text())
    assert set(schema["properties"]) == {"meta", "budgets", "forecasts", "retirement", "metrics", "recommendation", "trace"}
    assert set(schema["required"]) == {"meta", "budgets", "forecasts", "retirement", "metrics", "recommendation"}


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_every_fixture_plan_validates_against_exported_schema(name):
    jsonschema = pytest.importorskip("jsonschema")
    plan = generate_plan(load_profile(name), generated_at=FIXED)
    jsonschema.validate(json.loads(plan.model_dump_json()), json.loads(PLAN_SCHEMA_PATH.read_text()))


@pytest.mark.parametrize("name", ALL_PROFILES)
def test_plan_json_keys_match_schema_properties(name):
    """Dependency-free check: every key the plan emits is declared in the schema (extra='forbid')."""
    schema = json.loads(PLAN_SCHEMA_PATH.read_text())
    data = json.loads(generate_plan(load_profile(name), generated_at=FIXED).model_dump_json())
    assert set(data) <= set(schema["properties"])
    defs = schema["$defs"]
    assert set(data["meta"]) <= set(defs["PlanMeta"]["properties"])
    assert set(data["recommendation"]) <= set(defs["Recommendation"]["properties"])
    for budget in data["budgets"].values():
        assert set(budget) <= set(defs["Budget"]["properties"])
    for forecast in data["forecasts"].values():
        assert set(forecast) <= set(defs["Forecast"]["properties"])
        assert set(forecast["months"][0]) <= set(defs["ForecastMonth"]["properties"])


def test_example_plan_parses_as_plan_output():
    path = EXAMPLES / "complete_plan.json"
    plan = PlanOutput.model_validate_json(path.read_text())
    assert plan.meta.generated_at == FIXED and plan.meta.model is None
    assert (EXAMPLES / "complete_plan.md").read_text().startswith("# Financial plan")
    assert (EXAMPLES / "overspender_plan.md").read_text().startswith("# Financial plan")


# --- snapshot of the committed example plans (TDD §14.5) ------------------------------------------

SNAPSHOTS = [
    ("complete", "complete_plan.json", "json"),
    ("complete", "complete_plan.md", "text"),
    ("overspender", "overspender_plan.md", "text"),
]


@pytest.mark.parametrize("name, filename, fmt", SNAPSHOTS)
def test_example_plans_match_a_fresh_deterministic_run(name, filename, fmt):
    """The committed examples are byte-identical to `python -m finagent.output <profile> --no-llm
    --generated-at 2026-10-28T15:00:00 [--format json]`. Regenerate them when the output changes."""
    from finagent.output.pipeline import plan_to_json
    from finagent.output.render import render_text

    plan = generate_plan(load_profile(name), generated_at=FIXED)
    fresh = plan_to_json(plan) + "\n" if fmt == "json" else render_text(plan)
    assert (EXAMPLES / filename).read_text() == fresh, f"Regenerate examples/plans/{filename}"
