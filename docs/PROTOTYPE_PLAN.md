# Prototype Plan: Personal Financial Planning Agent

## Context
This is a CS329Z project. A user describes their finances in natural language. The agent stores the description in long-term memory and produces a financial plan. The user can come back at any time with updates ("new job", "saving for Japan", "weekly yoga subscription"), and the plan is regenerated. The idea has changed since the proposal. The temporal-validity and plan-repair research questions are not part of the prototype. The prototype focuses on input → memory → output, with evaluation of budget numbers and advice quality. Four memory methods will be compared eventually. The prototype starts with **full history** and **rolling summary**.

**Group decisions**
- JSON is how data passes from input to output to evaluation.
- **Memory rebuilds the profile.** Each turn, the profile is rebuilt from memory, so the memory method directly affects the extracted profile.
- **Deterministic Python does the math.** The LLM extracts the profile and writes advice. Python computes the budget variants, forecasts and goal feasibility.

**Not yet decided:** LLM provider and model (credits available: Tinker, Gemini, OpenRouter; the class uses OpenAI GPT), the UI stack, and the dashboard design.

## Overall architecture
```
User message (NL)
   │
   ▼
[1] INPUT + MEMORY (Arina) ── memory (full history | rolling summary) ── per-user storage
   │  outputs FinancialProfile JSON + confirmation  (or follow-up questions if info is missing)
   ▼
[2] OUTPUT (Daisy)
   │  budget variants (current / 50-30-20 / savings-adjusted 50-30 / others),
   │  forecast, goal & retirement feasibility (deterministic Python),
   │  spending & saving suggestions (LLM), chat tab + dashboard
   ▼
[3] EVALUATION (Jenny)
      dataset of scenarios, numerical checks of budget variants,
      LLM-as-judge for actionability/feasibility and tone, memory method comparison
```

## Team responsibilities (high level)
- **Arina: input phase plus full history and rolling summary memory.** Specified below.
- **Daisy: output phase and dashboard.** Takes the FinancialProfile JSON and produces the budget variants, forecast, feasibility checks, suggestions and UI.
- **Jenny: evaluation and dataset.** Builds scenarios with ground truth, numerical grading of the budget variants, an LLM-as-judge for advice, and comparisons between memory methods.

## Shared contracts
1. **FinancialProfile JSON (Arina → Daisy):** the user's financial information in a structured format that Daisy's code can calculate from.
2. **Output JSON (Daisy → Jenny):** the format for the plan and advice that evaluation reads.
3. **LLM provider and model:** to be chosen, with one shared way to configure it.
4. **Scenario format:** so that dataset episodes can be replayed through the input phase.

---

## Input + Memory (Arina)

### Layout
```
src/finagent/
  schema.py            # FinancialProfile Pydantic models + helpers (shared contract)
  llm.py               # input-phase LLM helper on top of the output phase's runtime
  extraction.py        # memory context -> FinancialProfile
  completeness.py      # required-field check -> follow-up questions, confirmation message
  diff.py              # ProfileDiff: what changed between two profile versions
  scenarios.py         # smoke-test scenario checker + live runner
  memory/
    base.py            # MemoryStrategy interface
    full_history.py
    rolling_summary.py
  store.py             # JSON-file persistence per user
  input_pipeline.py    # process_message() / run_episode() entry points
prompts/input/
  extract_profile.md
  follow_up_questions.md
  update_summary.md
schemas/financial_profile.schema.json   # exported for Daisy & Jenny
examples/scenarios/                     # a few hand-written smoke-test scenarios
tests/
```

### 1. FinancialProfile schema
Each money item stores `amount` and `frequency` exactly as the user said them. A deterministic `monthly_amount()` helper converts them, so the LLM never does arithmetic.
- `meta`: `user_id`, `as_of`, `profile_version`, `memory_method`
- `income[]`: `source`, `amount`, `frequency`, `is_net`
- `recurring_expenses[]`: `name`, `amount`, `frequency`, `category` (fixed enum, e.g. housing, groceries, dining_out, transport, subscriptions, fitness…), `need_or_want`, `is_fixed`
- `one_time_expenses[]`: `name`, `amount`, `expected_date`, `category`, `need_or_want`
- `savings_goals[]`: `name`, `target_amount`, `deadline`, `contribution {type: amount | percent_of_income, value, frequency}`, `priority`
- `overall_savings_target {type: amount | percent_of_income, value, frequency}`
- `debts[]`: `name`, `type`, `balance`, `apr`, `minimum_payment`
- `retirement`: `current_age`, `target_retirement_age`, `current_balance`, `monthly_contribution`, `target`, `desired_annual_spending`
- `current_savings_balance`
- Each item has an optional **`effective_date`**, for changes like "rent goes up next month".
- **Need/want labels and categories** are assigned by the extractor and used by the 50/30/20 variants.

Example:
```json
{
  "meta": {
    "user_id": "u_001",
    "as_of": "2026-10-28T14:05:00",
    "profile_version": 3,
    "memory_method": "rolling_summary"
  },
  "income": [
    {"source": "Software engineer salary", "amount": 6200, "frequency": "monthly",
     "is_net": true, "effective_date": "2026-11-01"}
  ],
  "recurring_expenses": [
    {"name": "Rent", "amount": 1800, "frequency": "monthly", "category": "housing",
     "need_or_want": "need", "is_fixed": true, "effective_date": null},
    {"name": "Groceries", "amount": 100, "frequency": "weekly", "category": "groceries",
     "need_or_want": "need", "is_fixed": false, "effective_date": null},
    {"name": "Eating out", "amount": 450, "frequency": "monthly", "category": "dining_out",
     "need_or_want": "want", "is_fixed": false, "effective_date": null},
    {"name": "Yoga membership", "amount": 120, "frequency": "monthly", "category": "fitness",
     "need_or_want": "want", "is_fixed": true, "effective_date": null}
  ],
  "one_time_expenses": [
    {"name": "Laptop", "amount": 1500, "expected_date": "2026-12-15", "category": "shopping",
     "need_or_want": "want", "effective_date": null}
  ],
  "savings_goals": [
    {"name": "Japan trip", "target_amount": 4000, "deadline": "2027-06-01",
     "contribution": {"type": "amount", "value": 500, "frequency": "monthly"},
     "priority": "high", "effective_date": null}
  ],
  "overall_savings_target": {"type": "percent_of_income", "value": 20, "frequency": "monthly"},
  "debts": [
    {"name": "Federal student loan", "type": "student_loan", "balance": 18000,
     "apr": 5.5, "minimum_payment": 210, "effective_date": null}
  ],
  "retirement": {
    "current_age": 24, "target_retirement_age": 60, "current_balance": 8000,
    "monthly_contribution": 400, "target": 1500000, "desired_annual_spending": 60000
  },
  "current_savings_balance": 6500
}
```

### 2. Memory strategies
Both strategies share one interface: `add_user_turn(text, timestamp)`, `add_assistant_turn(text, timestamp)` (so the extractor sees the follow-up question a user is answering), `extraction_context()` and `state()` / `load()`.
- **Full history:** the context is the entire transcript in time order, with timestamps and an instruction that later statements override earlier ones. Token count is tracked, and a flag is set if the context limit is exceeded.
- **Rolling summary:** the extractor sees **the summary plus the latest raw exchange**: the newest user message, together with any assistant messages directly before it (such as the follow-up question it answers). When the next user message arrives, one LLM call (`update_summary.md`, tag `summary_update`) merges the previous exchange into the summary. The prompt asks for current values to be kept, superseded ones dropped, and relative dates written as absolute dates. The summary is capped at about 400 words (configurable).
- The raw transcript is always saved for audit and replay, but only full history shows it to the extractor.

### 3. Extraction
- `extract_profile(context) -> FinancialProfile`. The prompt includes the schema, the category enum and the need/want rules, and says unknown fields stay null and are never guessed.
- The output is validated with Pydantic. Invalid output is retried up to 2 times with the validation error included. Temperature is 0.

### 4. Missing info: ask the user for further information before planning
- `completeness.py` checks the extracted profile against a list of **required fields**. Draft list:
  - at least one income with `amount`, `frequency` and `is_net`
  - `amount` and `frequency` on every recurring expense, and `amount` on every one-time expense
  - `balance`, `apr` and `minimum_payment` on every debt
  - a target and deadline (or contribution) on every goal
  - ages, if retirement is mentioned
- If anything is missing, the pipeline returns `status: "needs_clarification"` with specific follow-up questions, one per item with missing fields. The code decides which fields are missing (`find_missing`); the LLM writes the wording (`prompts/input/follow_up_questions.md`), with templated questions as a fallback if the LLM reply is invalid. The output phase is **not** triggered until the user answers. The answers go through `process_message` like any other message, so memory captures them.
- Once every required field is present, the input phase returns `status: "ready"` with a short **confirmation message** that summarizes what was recorded or changed. It is built in code from the profile, not by the LLM, so its numbers always match. The profile is then passed to the output phase.
- The required-field list is a short config (`RequiredFields`), so it can be made stricter or looser.

### 5. Changes since the last version
`ProfileDiff` lists the items added, removed and changed between the **last complete ("ready") profile**, the one the current plan is based on, and the new one. Items are matched by name, ignoring case and punctuation. The diff is included in the handoff. Once a plan exists, the confirmation message describes the changes ("updated Rent (amount $1,800 → $1,950)") rather than summarizing everything, and says so if nothing changed.

### 6. Entry points
```python
process_message(user_id, text, memory_method, timestamp=None) -> InputResult
#  InputResult: status ("ready" | "needs_clarification"), profile, diff,
#               confirmation_message, follow_up_questions, follow_up_generated_by,
#               missing, usage (one record per LLM call), usage_totals, memory_stats
run_episode(turns, memory_method) -> list[InputResult]   # for Jenny's eval; uses a fresh temporary store
```
- The model comes from the `FINAGENT_LLM_*` env vars unless one is passed in. If none is configured, `InputPhaseUnavailable` is raised.
- The follow-up questions or confirmation are recorded in memory as an assistant turn, so the next message is read with them as context.
- Nothing is saved unless the whole message is handled successfully. Requests for the same user are handled one at a time.
- The dashboard (`web/extract.py`) uses `process_message` for free text. If no model is configured or the input phase fails, it falls back to the bundled example and pasted JSON.
- Interactive chat for manual testing: `python -m finagent.input_pipeline --user me [--memory rolling_summary] [--show-profile]`.

### 7. Storage: JSON files per user
`data/users/{user_id}/{memory_method}/` holds `transcript.jsonl`, `memory_state.json`, `session.json` (the last ready version) and `profiles/v{n}.json`. Every extracted version is saved, including incomplete ones. The root can be changed with `FINAGENT_DATA_DIR`. A `user_id` must be 1–64 letters, digits, `_` or `-`, so request input can't point outside the data directory.

### 8. LLM client
`llm.py` builds on Daisy's runtime (`finagent/output/agents/runtime.py`), so the input and output phases share one `ChatModel` interface and one provider config (`FINAGENT_LLM_MODEL`, `FINAGENT_LLM_API_KEY`, `FINAGENT_LLM_BASE_URL`). It adds `complete()` and `complete_json()` for calls without tools. JSON is requested in the prompt, then parsed, with code fences stripped. Each call is logged as a `Usage` record with a tag, token counts and latency, collected in a `UsageLog` for the memory comparison. Unit tests use `scripted_model()`, a wrapper around the runtime's `ScriptedModel`.

### Order of work
1. Schema, the exported JSON schema and 1–2 example profiles. Share them with Daisy and Jenny so they can start building.
2. LLM helper on top of Daisy's runtime.
3. Extraction with full history, tested on an initial paragraph.
4. Completeness check, follow-up questions and the confirmation message.
5. Rolling summary.
6. Diff, storage, `process_message` and `run_episode`.
7. 3–5 smoke-test scenarios: initial paragraph → new job → Japan trip → yoga, plus a correction and one with missing information.

### Scenarios
Each file in `examples/scenarios/` is a multi-turn conversation. For each turn it gives the expected status and checks on the extracted profile: a value at a path, an item that must be present or absent (matched by its values, not its name), or an item count. The format is documented in `src/finagent/scenarios.py`. There are five scenarios:
- `plan_updates`: initial message → new job with a start date → Japan trip → yoga
- `correction`
- `missing_info`
- `cancellation`
- `irrelevant_update`, where nothing may change

To run them against a real model: `python -m finagent.scenarios [--memory both] [--out DIR]`, or `pytest -m live`.

### Verification
- **Unit tests** (pytest + `scripted_model`) cover `monthly_amount` for every frequency, schema validation and retry, the required-field check, the extraction context of each memory method, the diff, and saving and reloading state.
- **Live smoke test:** run `run_episode` on each scenario with both memory methods. Check that updates replace old values, new items appear, effective dates are captured, missing info produces follow-up questions, and complete info produces a confirmation.
- **Contract check:** every saved profile validates against `schemas/financial_profile.schema.json`.

### Open questions for the team (not blocking)
1. Does "save 20%" mean 20% of gross pay or of take-home pay?
2. Is the draft required-field list right? For example, should a missing debt APR block the plan?
3. Which provider and model should the prototype use?
