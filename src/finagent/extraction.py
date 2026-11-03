"""Memory context -> FinancialProfile.

The extractor rebuilds the whole current profile from the memory context every
turn. The LLM writes everything except `meta`, which the caller provides. The
reply is validated against the schema; invalid replies are sent back to the
model with the errors, up to `max_retries` times.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from finagent.llm import ChatModel, LLMError, Message, UsageLog, complete, parse_json
from finagent.schema import FinancialProfile, Meta, json_schema

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "input" / "extract_profile.md"
DEFAULT_MAX_RETRIES = 2
MAX_REPORTED_ERRORS = 20


class ExtractionError(RuntimeError):
    """The model did not produce a valid profile within the retry budget."""


def body_schema() -> dict:
    """The FinancialProfile JSON Schema without `meta`, which the LLM doesn't write."""
    schema = copy.deepcopy(json_schema())
    schema["properties"].pop("meta")
    schema["required"] = [r for r in schema.get("required", []) if r != "meta"]
    for name in ("Meta", "MemoryMethod"):
        schema.get("$defs", {}).pop(name, None)
    if not schema["required"]:
        schema.pop("required")
    return schema


@lru_cache(maxsize=1)
def system_prompt() -> str:
    try:
        template = PROMPT_PATH.read_text()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Extraction prompt not found at {PROMPT_PATH}. Run from a source checkout or an editable install."
        ) from exc
    return template.replace("{{SCHEMA}}", json.dumps(body_schema(), indent=2))


def _describe(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        lines = [f"- {'/'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errors[:MAX_REPORTED_ERRORS]]
        if len(errors) > MAX_REPORTED_ERRORS:
            lines.append(f"- ... and {len(errors) - MAX_REPORTED_ERRORS} more")
        return "The JSON does not match the schema:\n" + "\n".join(lines)
    return str(exc)


def _retry_message(exc: Exception) -> Message:
    return Message(
        role="user",
        content=f"Your previous reply was not valid.\n{_describe(exc)}\n"
        "Reply with the corrected, complete JSON object only.",
    )


def extract_profile(
    model: ChatModel,
    context: list[Message],
    meta: Meta,
    *,
    log: Optional[UsageLog] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> FinancialProfile:
    """Rebuild the profile from `context` (a memory strategy's extraction context)."""
    messages = [Message(role="system", content=system_prompt()), *context]
    last_error: Optional[Exception] = None
    for _ in range(max_retries + 1):
        try:
            text = complete(model, messages, tag="extract", log=log).text
        except LLMError as exc:  # empty reply: ask again
            last_error = exc
            continue
        try:
            body = parse_json(text)
            body.pop("meta", None)  # the caller owns meta
            return FinancialProfile.model_validate({**body, "meta": meta.model_dump()})
        except (LLMError, ValidationError) as exc:
            last_error = exc
            messages = [*messages, Message(role="assistant", content=text), _retry_message(exc)]
    raise ExtractionError(f"No valid profile after {max_retries + 1} attempts. Last error: {_describe(last_error)}")
