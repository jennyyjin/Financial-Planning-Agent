"""Agent system prompts, kept as Markdown in the repository's `prompts/output/` directory.

They live outside the package, so they are found only in a source checkout or an
editable install (`pip install -e .`); a missing prompt raises a clear error.
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[4] / "prompts" / "output"


def prompt_path(name: str) -> Path:
    return PROMPTS_DIR / f"{name}.md"


def read_prompt(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Agent prompt not found at {path}. The prompts live in the repository's prompts/output/ directory; "
            "run from a source checkout or an editable install."
        ) from exc
