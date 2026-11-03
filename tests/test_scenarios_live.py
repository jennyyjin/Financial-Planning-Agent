"""Live smoke test: every scenario with both memory methods, against a real model.

    FINAGENT_LLM_MODEL=... FINAGENT_LLM_API_KEY=... pytest -m live tests/test_scenarios_live.py
(or `python -m finagent.scenarios` for a readable report).
"""

from __future__ import annotations

import os

import pytest

from finagent.scenarios import load_scenario, run_scenario, scenario_paths
from finagent.schema import MemoryMethod

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("FINAGENT_LLM_MODEL"), reason="FINAGENT_LLM_MODEL is not set"),
]


@pytest.mark.parametrize("method", list(MemoryMethod), ids=lambda m: m.value)
@pytest.mark.parametrize("path", scenario_paths(), ids=lambda p: p.stem)
def test_live_scenario(path, method):
    _, failures = run_scenario(load_scenario(path), method)
    assert not any(failures), {f"turn {i}": f for i, f in enumerate(failures, 1) if f}
