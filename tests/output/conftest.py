import json
from pathlib import Path

import pytest

from finagent.output.config import PlanConfig
from finagent.output.normalize import normalize
from finagent.schema import FinancialProfile

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

PROFILE_PATHS = {
    "complete": ROOT / "examples" / "profiles" / "complete_profile.json",
    "incomplete": ROOT / "examples" / "profiles" / "incomplete_profile.json",
    "overspender": FIXTURES / "overspender.json",
    "big_goals": FIXTURES / "big_goals.json",
    "no_retirement": FIXTURES / "no_retirement.json",
    "mislabeled": FIXTURES / "mislabeled.json",
}
ALL_PROFILES = list(PROFILE_PATHS)


def load_profile(name: str) -> FinancialProfile:
    return FinancialProfile.model_validate(json.loads(PROFILE_PATHS[name].read_text()))


@pytest.fixture
def config() -> PlanConfig:
    return PlanConfig()


@pytest.fixture
def profile_named():
    return load_profile


@pytest.fixture
def normalized_named(config):
    return lambda name: normalize(load_profile(name), config)
