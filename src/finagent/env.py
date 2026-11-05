"""Minimal .env loader (standard library only).

    from finagent.env import load_dotenv
    load_dotenv()          # ./.env, else <repo root>/.env; variables already in the environment win

Set FINAGENT_SKIP_DOTENV=1 to disable the automatic lookup (the test suite does, so it never uses real keys).
Supports `KEY=value`, `export KEY=value`, single or double quotes, blank lines and `#` comments.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()  # inline comment on an unquoted value
        values[key] = value
    return values


def load_dotenv(path: Optional[Path] = None) -> Optional[Path]:
    """Load a .env file into os.environ without overriding variables that are already set.

    Without `path`, the first of ./.env and <repo root>/.env that exists is used.
    Returns the file that was loaded, or None.
    """
    if path is None and os.environ.get("FINAGENT_SKIP_DOTENV"):
        return None
    candidates = [Path(path)] if path else [Path.cwd() / ".env", PROJECT_ROOT / ".env"]
    for candidate in candidates:
        if candidate.is_file():
            for key, value in parse_dotenv(candidate.read_text()).items():
                os.environ.setdefault(key, value)
            return candidate
    return None
