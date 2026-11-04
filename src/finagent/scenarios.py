"""Hand-written multi-turn smoke-test scenarios for the input phase (examples/scenarios/*.json).

A scenario is {"name", "description", "turns": [turn, ...]}. Each turn has:
    "text", "timestamp" (ISO)          the user message
    "expected_status"                  "ready" | "needs_clarification" (optional)
    "expect_unchanged"                 true if nothing should change since the last plan (optional)
    "checks": [check, ...]             facts the extracted profile must contain (optional)
A check is one of:
    {"path": "/retirement/current_age", "equals": 26}     a value at a JSON pointer
    {"section": "income", "has": {"amount": 5600}}         some item in the section has these values
    {"section": "debts", "lacks": {"amount": 1400}}        no item in the section has these values
    {"section": "savings_goals", "count": 1}               the section has this many items
Item values are compared as JSON (dates are "YYYY-MM-DD"); nested fields use dots ("contribution.value").
Items are matched by values, not names, because the model may name them differently.

Run against a real model:  python -m finagent.scenarios [paths ...] [--memory full_history|rolling_summary|both]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from finagent.input_pipeline import InputPhaseUnavailable, InputResult, resolve_model, run_episode
from finagent.llm import ChatModel
from finagent.schema import FinancialProfile, MemoryMethod

SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "examples" / "scenarios"
SECTIONS = ("income", "recurring_expenses", "one_time_expenses", "savings_goals", "debts")


def scenario_paths() -> list[Path]:
    return sorted(SCENARIOS_DIR.glob("*.json"))


def load_scenario(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def _get(data: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        data = data.get(part) if isinstance(data, dict) else None
    return data


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)
    return a == b


def _matches(item: dict, wanted: dict) -> bool:
    return all(_same(_get(item, k), v) for k, v in wanted.items())


def _pointer(data: Any, path: str) -> Any:
    for part in path.strip("/").split("/"):
        if isinstance(data, list) and part.isdigit():
            data = data[int(part)] if int(part) < len(data) else None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def check_profile(profile: FinancialProfile, checks: list[dict[str, Any]]) -> list[str]:
    """Failure messages for the checks that don't hold (empty if all pass)."""
    data = profile.model_dump(mode="json")
    failures = []
    for check in checks:
        if "path" in check:
            got = _pointer(data, check["path"])
            if not _same(got, check["equals"]):
                failures.append(f"{check['path']}: expected {check['equals']!r}, got {got!r}")
            continue
        items = data[check["section"]]
        if "has" in check and not any(_matches(i, check["has"]) for i in items):
            failures.append(f"{check['section']}: no item with {check['has']}")
        if "lacks" in check and any(_matches(i, check["lacks"]) for i in items):
            failures.append(f"{check['section']}: unexpected item with {check['lacks']}")
        if "count" in check and len(items) != check["count"]:
            failures.append(f"{check['section']}: expected {check['count']} items, got {len(items)}")
    return failures


def check_result(result: InputResult, turn: dict[str, Any]) -> list[str]:
    failures = []
    expected = turn.get("expected_status")
    if expected and result.status != expected:
        failures.append(f"status: expected {expected}, got {result.status}")
    if turn.get("expect_unchanged") and (result.diff is None or not result.diff.is_empty):
        failures.append(f"expected no changes, got {result.diff.describe() if result.diff else 'no previous plan'}")
    return failures + check_profile(result.profile, turn.get("checks", []))


def run_scenario(
    scenario: dict[str, Any], memory_method: MemoryMethod, model: Optional[ChatModel] = None
) -> tuple[list[InputResult], list[list[str]]]:
    """Replay the scenario; returns the results and the failures for each turn."""
    turns = [{"text": t["text"], "timestamp": t.get("timestamp")} for t in scenario["turns"]]
    results = run_episode(turns, memory_method, model=model, user_id="scenario")
    return results, [check_result(r, t) for r, t in zip(results, scenario["turns"])]


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m finagent.scenarios", description="Run input-phase scenarios live.")
    p.add_argument("paths", nargs="*", type=Path, help="Scenario files (default: all in examples/scenarios).")
    p.add_argument("--memory", choices=[*(m.value for m in MemoryMethod), "both"], default="both")
    p.add_argument("--out", type=Path, help="Write each run's results as JSON into this directory.")
    args = p.parse_args(argv)
    methods = list(MemoryMethod) if args.memory == "both" else [MemoryMethod(args.memory)]
    try:
        model = resolve_model(None)
    except InputPhaseUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    failed = 0
    for path in args.paths or scenario_paths():
        scenario = load_scenario(path)
        for method in methods:
            results, failures = run_scenario(scenario, method, model)
            ok = not any(failures)
            failed += not ok
            tokens = sum(r.usage_totals["prompt_tokens"] + r.usage_totals["completion_tokens"] for r in results)
            print(f"{'PASS' if ok else 'FAIL'}  {scenario['name']} [{method.value}]  ({tokens} tokens)")
            for i, turn_failures in enumerate(failures, 1):
                for f in turn_failures:
                    print(f"      turn {i}: {f}")
            if args.out:
                args.out.mkdir(parents=True, exist_ok=True)
                (args.out / f"{scenario['name']}__{method.value}.json").write_text(
                    json.dumps([r.to_dict() for r in results], indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
