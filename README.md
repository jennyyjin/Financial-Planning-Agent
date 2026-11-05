# Financial-Planning-Agent

Personal financial planning agent (CS329Z). See [docs/PROTOTYPE_PLAN.md](docs/PROTOTYPE_PLAN.md) for the prototype plan.

## Setup
```
pip install -e ".[dev]"
pytest
```

## FinancialProfile schema
The input phase outputs a `FinancialProfile` JSON (defined in `src/finagent/schema.py`).
- JSON Schema: `schemas/financial_profile.schema.json` (regenerate with `python -m finagent.schema`)
- Examples: `examples/profiles/complete_profile.json`, `examples/profiles/incomplete_profile.json`
- Amounts are stored as stated (`amount` + `frequency`); use `monthly_amount()` to normalize. Unknown values are `null`.

## Input phase
The input phase (`src/finagent/input_pipeline.py`, plan in [docs/PROTOTYPE_PLAN.md](docs/PROTOTYPE_PLAN.md)) turns each user message into an updated `FinancialProfile`:
- The user's memory is either **full history** (the whole transcript) or a **rolling summary** (a running summary plus the latest exchange). The LLM rebuilds the whole profile from it every turn, and the result is checked against the schema and retried if it is invalid.
- Code checks the required fields (`completeness.RequiredFields`). If something is missing, the result is `needs_clarification` with follow-up questions worded by the LLM. Otherwise it is `ready` with a confirmation message built in code, which lists what changed since the last plan.
- Every LLM call is logged with a tag, token counts and latency (`InputResult.usage`) for the memory comparison.

```
export FINAGENT_LLM_MODEL=... FINAGENT_LLM_API_KEY=... FINAGENT_LLM_BASE_URL=...   # same config as the output phase
python -m finagent.input_pipeline --user me --memory rolling_summary --show-profile    # chat in the terminal
python -m finagent.scenarios --memory both                                            # live smoke-test scenarios
```
From Python: `process_message(user_id, text, memory_method)` and, for evaluation, `run_episode(turns, memory_method)`, which uses a fresh temporary store. Per-user data is written to `data/users/` (git-ignored; override with `FINAGENT_DATA_DIR`). Tests: `pytest tests/test_*.py`. The live tests (`pytest -m live`) run only when `FINAGENT_LLM_MODEL` is set.

## Output phase
The output phase (`src/finagent/output/`, design in [docs/OUTPUT_PHASE_TDD.md](docs/OUTPUT_PHASE_TDD.md)) turns a `FinancialProfile` into a `PlanOutput`:
- three budgets: **current** (as stated), **50/30/20**, and **goals + 5:3**, with a rationale for every change;
- a 12-month forecast per budget with a verdict for each saving goal;
- a 4%-rule retirement projection per budget, compared with the target retirement age;
- metrics and an overall recommendation with the major changes to make.

LLM agents (the Budget Restructurer and the Advisor) only choose which wants to cut and write the prose. Deterministic Python computes every number and enforces the rules in the tools: essential expenses are never reduced (including essential-looking one-time costs such as medical or legal bills), cuts go no further than needed, every number in a rationale or in the advice must match a computed value, and the advisor's status and recommended budget must follow the rules. If an agent fails, a rule-based fallback takes over, the plan still completes, and a warning (`agent_fallback` or `agent_error`) is added to `meta.warnings`. A one-time item an agent reclassifies as non-essential is reported as `one_time_reclassified`.

### Run it
```
# Deterministic: no LLM, reproducible (fallback restructurer + templated advice)
python -m finagent.output examples/profiles/complete_profile.json --no-llm              # Markdown report
python -m finagent.output examples/profiles/complete_profile.json --no-llm --format json --out plan.json

# With an LLM (any OpenAI-compatible endpoint). Put these in a gitignored .env (see .env.example);
# both CLIs load ./.env or <repo root>/.env, and variables already set in your shell win.
export FINAGENT_LLM_MODEL=gpt-4o-mini
export FINAGENT_LLM_API_KEY=sk-...          # or OPENAI_API_KEY
export FINAGENT_LLM_BASE_URL=https://openrouter.ai/api/v1   # optional; defaults to https://api.openai.com/v1
python -m finagent.output examples/profiles/complete_profile.json
```
Without `--no-llm`, the CLI uses the LLM when `FINAGENT_LLM_MODEL` is set. If it is not set, the CLI runs deterministically and says so on stderr. Other flags: `--format text|json` (or `--text` / `--json`), `--out PATH`, and `--generated-at ISO` to fix the timestamp for reproducible output.

From Python:
```python
from finagent.output import generate_plan, render_text, PlanConfig
from finagent.output.agents.runtime import OpenAICompatibleModel
plan = generate_plan(profile)                       # model=None: deterministic
plan = generate_plan(profile, PlanConfig(real_return=0.04), model=OpenAICompatibleModel.from_env())
print(render_text(plan))                            # Markdown; the agent trace is not shown
```

### The contract
- `PlanOutput` in `src/finagent/output/models.py` is the contract with evaluation and any future front end. Its JSON Schema is `schemas/plan_output.schema.json`; regenerate it with `python -m finagent.output.models`, and a test fails if it is stale.
- `meta` records the config (every assumption), the model name (`null` for deterministic runs), summed token usage per agent (`agent_usage`) and the warnings. `trace` holds every agent step (tool calls, arguments, results; each model turn's usage on its first step) for evaluation.
- Money keeps full float precision in the JSON. Rounding happens only in the text report.
- Cash set aside for upcoming one-time costs is reported as `totals.one_time_set_aside`, separate from savings: `need + want + savings + one_time_set_aside + unallocated = income`. `savings_rate` counts savings lines only; unassigned cash is `unallocated_share`.
- Sample outputs: `examples/plans/complete_plan.json`, `complete_plan.md` and `overspender_plan.md` (deterministic, `--generated-at 2026-10-28T15:00:00`). A test fails if they differ from a fresh run; regenerate them with the CLI commands above.
- Tests: `pytest tests/output`. The live smoke test (`pytest -m live tests/output/test_live.py`) runs only when `FINAGENT_LLM_MODEL` is set. The JSON-schema validation tests run only if `jsonschema` is installed.
- Agent prompts live in `prompts/output/`.

## Dashboard
A web front end for the output phase (`src/finagent/web/`), built from the "Financial planning agent" design. It uses only the standard library: a static page plus a small JSON API.
```
python -m finagent.web               # http://127.0.0.1:8000/ ; uses the LLM agents if FINAGENT_LLM_MODEL is set
python -m finagent.web --no-llm --port 8765
```
- The user describes their situation in the text box. `finagent/web/extract.py` sends free text to the input phase (`process_message`, with the server's `--memory-method`), and `needs_clarification` follow-up questions are shown under the text box. If `FINAGENT_LLM_MODEL` is not set or the input phase fails, it still accepts the bundled example narrative ("Fill in an example": `examples/profiles/narrative_profile.txt`, which maps to `narrative_profile.json`) and a `FinancialProfile` pasted as JSON.
- `POST /api/plan {"text", "user_id"?}` returns `{"status": "ready", "view", "plan"}`. `plan` is the `PlanOutput` without `trace`. `view` comes from `finagent/web/view.py`, which only selects, groups and formats plan values (plus simple differences such as the total cut to wants).
- Tests: `pytest tests/web`.
