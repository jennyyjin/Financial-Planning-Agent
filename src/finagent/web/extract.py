"""Text box -> FinancialProfile: the hook between the dashboard and the input phase.

Text that is not JSON goes to the input phase (`finagent.input_pipeline.process_message`,
PROTOTYPE_PLAN §6). If the input phase is missing, has no model configured
(FINAGENT_LLM_MODEL unset) or fails, `extract` falls back to accepting:
  1. a FinancialProfile pasted as JSON (always accepted, without the input phase);
  2. the text of a bundled example narrative, mapped to its hand-written profile;
and otherwise says the text can't be read.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import ValidationError

from finagent.schema import FinancialProfile, MemoryMethod

EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples" / "profiles"
# Narrative text file -> the profile the input phase should produce from it.
EXAMPLES = {"narrative_profile.txt": "narrative_profile.json"}

Status = Literal["ready", "needs_clarification", "unavailable", "invalid"]


@dataclass
class Extraction:
    status: Status
    profile: Optional[FinancialProfile] = None
    message: Optional[str] = None
    questions: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("’", "'")).strip().lower()


def example_text() -> str:
    return (EXAMPLES_DIR / next(iter(EXAMPLES))).read_text().strip()


def _example_profile(text: str) -> Optional[FinancialProfile]:
    key = _norm(text)
    for txt, profile in EXAMPLES.items():
        if _norm((EXAMPLES_DIR / txt).read_text()) == key:
            return FinancialProfile.model_validate_json((EXAMPLES_DIR / profile).read_text())
    return None


def _input_phase() -> Any:
    try:
        return importlib.import_module("finagent.input_pipeline").process_message
    except (ImportError, AttributeError):
        return None


def _is_unavailable(exc: Exception) -> bool:
    """True if the input phase says it has no model (FINAGENT_LLM_MODEL unset)."""
    cls = getattr(sys.modules.get("finagent.input_pipeline"), "InputPhaseUnavailable", None)
    return isinstance(cls, type) and isinstance(exc, cls)


def _from_input_phase(process_message: Any, text: str, user_id: str, memory_method: MemoryMethod) -> Extraction:
    result = process_message(user_id, text, memory_method)
    get = (lambda k: result.get(k)) if isinstance(result, dict) else (lambda k: getattr(result, k, None))
    status = getattr(get("status"), "value", get("status"))
    if status == "needs_clarification":
        return Extraction("needs_clarification", message=get("confirmation_message"),
                          questions=list(get("follow_up_questions") or []))
    profile = get("profile")
    if profile is not None and not isinstance(profile, FinancialProfile):
        profile = FinancialProfile.model_validate(profile)
    return Extraction("ready", profile=profile, message=get("confirmation_message"))


def extract(text: str, user_id: str = "web", memory_method: MemoryMethod = MemoryMethod.full_history) -> Extraction:
    text = text.strip()
    if not text:
        return Extraction("invalid", message="Add a few details about your money first.")
    if text.startswith("{"):
        try:
            return Extraction("ready", profile=FinancialProfile.model_validate(json.loads(text)))
        except (ValueError, ValidationError) as exc:
            return Extraction("invalid", message=f"That JSON is not a valid FinancialProfile: {exc}")
    process_message = _input_phase()
    failed = False
    if process_message is not None:
        try:
            return _from_input_phase(process_message, text, user_id, memory_method)
        except Exception as exc:  # no model configured, or extraction failed: fall back to the examples
            failed = not _is_unavailable(exc)
            if failed:
                print(f"error: input phase failed: {exc!r}", file=sys.stderr)
    profile = _example_profile(text)
    if profile is not None:
        return Extraction("ready", profile=profile)
    if failed:
        return Extraction("unavailable", message="Sorry, I couldn't read that just now. Please try again.")
    return Extraction(
        "unavailable",
        message="We can’t read free text yet because the input phase isn’t connected. "
        "Click Fill in an example, or paste a FinancialProfile as JSON.",
    )
