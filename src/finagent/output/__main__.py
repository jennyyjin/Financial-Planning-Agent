"""CLI: python -m finagent.output PROFILE_JSON [--format text|json] [--no-llm] [--out PATH].

Uses the LLM agents when FINAGENT_LLM_MODEL is set (see OpenAICompatibleModel.from_env),
otherwise runs the deterministic pipeline. Notes go to stderr; the plan goes to
stdout or --out.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from pydantic import ValidationError

from finagent.env import load_dotenv
from finagent.output.agents.runtime import ChatModel, OpenAICompatibleModel
from finagent.output.pipeline import generate_plan, load_profile, plan_to_json
from finagent.output.render import render_text


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m finagent.output", description="Generate a financial plan from a profile.")
    p.add_argument("profile", type=Path, help="FinancialProfile JSON file.")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--format", choices=("text", "json"), default="text", help="Markdown report (default) or PlanOutput JSON.")
    fmt.add_argument("--json", dest="format", action="store_const", const="json", help="Same as --format json.")
    fmt.add_argument("--text", dest="format", action="store_const", const="text", help="Same as --format text.")
    p.add_argument("--no-llm", action="store_true", help="Deterministic run: fallback restructurer and templated advice.")
    p.add_argument("--out", type=Path, help="Write to this file instead of stdout.")
    p.add_argument("--generated-at", type=datetime.fromisoformat, default=None,
                   help="ISO timestamp for meta.generated_at (default: now); makes runs reproducible.")
    return p


def _model(no_llm: bool) -> Optional[ChatModel]:
    if no_llm:
        return None
    if not os.environ.get("FINAGENT_LLM_MODEL"):
        print("note: FINAGENT_LLM_MODEL is not set; running deterministically (no LLM).", file=sys.stderr)
        return None
    model = OpenAICompatibleModel.from_env()
    print(f"note: using model {model.name} at {model.base_url}.", file=sys.stderr)
    return model


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    load_dotenv()
    try:
        profile = load_profile(args.profile)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"error: cannot read profile {args.profile}: {exc}", file=sys.stderr)
        return 2
    plan = generate_plan(profile, model=_model(args.no_llm), generated_at=args.generated_at)
    text = plan_to_json(plan) + "\n" if args.format == "json" else render_text(plan)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    for w in plan.meta.warnings:
        if w.code in ("agent_fallback", "agent_error"):
            print(f"warning: {w.message}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
