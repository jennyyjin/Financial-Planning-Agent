"""CLI: python -m finagent.web [--host H] [--port P] [--no-llm] [--memory-method M].

Serves the dashboard. Uses the LLM agents when FINAGENT_LLM_MODEL is set
(see OpenAICompatibleModel.from_env), otherwise plans deterministically.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

from finagent.env import load_dotenv
from finagent.output.agents.runtime import ChatModel, OpenAICompatibleModel
from finagent.schema import MemoryMethod
from finagent.web.server import make_server


def _model(no_llm: bool) -> Optional[ChatModel]:
    if no_llm or not os.environ.get("FINAGENT_LLM_MODEL"):
        if not no_llm:
            print("note: FINAGENT_LLM_MODEL is not set; planning deterministically (no LLM).", file=sys.stderr)
        return None
    model = OpenAICompatibleModel.from_env()
    print(f"note: using model {model.name} at {model.base_url}.", file=sys.stderr)
    return model


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m finagent.web", description="Serve the budget planner dashboard.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-llm", action="store_true", help="Deterministic plans: fallback restructurer and templated advice.")
    p.add_argument("--memory-method", choices=[m.value for m in MemoryMethod], default=MemoryMethod.full_history.value,
                   help="Passed to the input phase once it is connected.")
    args = p.parse_args(argv)
    load_dotenv()
    server = make_server(args.host, args.port, _model(args.no_llm), MemoryMethod(args.memory_method))
    print(f"Budget planner running at http://{args.host}:{server.server_address[1]}/", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
