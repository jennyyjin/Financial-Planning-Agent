# Technical Design: Output Phase (Budget Variants, Forecast, Retirement, Advice)

Owner: Daisy · Status: Draft for team review · Branch: `feature/output-phase`
Upstream: `FinancialProfile` JSON from the input phase ([schema.py](../src/finagent/schema.py)) · Downstream: `PlanOutput` JSON for evaluation.

## 1. Summary
The output phase takes a validated `FinancialProfile` and produces a `PlanOutput`. A `PlanOutput` contains:

1. Three budgets:
   - **Current**: the budget as the user stated it.
   - **50/30/20**: restructured to the 50/30/20 rule.
   - **Goals + 5:3**: restructured to fund the saving goals, with the remainder split 5:3 between needs and wants.
2. A rationale for every change made to a budget.
3. A 12-month forecast for each budget, with a verdict on each saving goal.
4. A retirement projection for each budget based on the 4% rule (25× annual spending), compared with the desired retirement age.
5. An overall recommendation, with the major changes the user should make.

LLM agents use tools to make the judgment calls: which non-essential expenses to cut, how much to cut them, why, and what to tell the user. **Deterministic Python does all of the arithmetic and enforces the expense rules.** An agent cannot change a protected expense or publish a number the tools did not compute.

For now the output is a JSON document, plus a Markdown/text report rendered from it. A front end can use the JSON later without any changes to the output phase.

## 2. Goals and non-goals
**Goals**
- Produce the three budgets, the forecasts, the goal verdicts, the retirement projections and the recommendation from one profile.
- Record a rationale for every budget modification.
- Never drop or reduce an essential expense.
- Keep the numbers reproducible: the same profile and config always produce the same numbers. Only the LLM-written text may vary.
- Make every component testable without network access. Agents are tested with a scripted fake model.

**Non-goals (prototype)**
- Tax calculation (gross to net income), investment allocation advice, and inflation modelling. All amounts are in today's dollars and returns are real returns.
- UI or dashboard. The output is designed so that one can be added later.
- Plan repair or temporal reasoning beyond `effective_date`.

## 3. Key design decisions
| # | Decision | Why |
|---|----------|-----|
| D1 | **The LLM decides, the tools compute.** Agents only choose adjustments and write prose. Every number comes from a tool result. | Matches the group decision. The numbers can be graded deterministically. |
| D2 | **The rules are enforced in the tool layer, not in the prompt.** `adjust_item` rejects changes to protected items, increases, and changes without a rationale. | Prompts cannot guarantee "never cut medication". Code can. |
| D3 | **Code orchestrates and agents do the steps.** A fixed Python pipeline calls two kinds of tool-using agent: the Budget Restructurer (once per restructured budget) and the Advisor. | The order of steps never changes, so an LLM orchestrator would add failure modes and gain nothing. Agents are used only where judgment is needed. |
| D4 | **Every agent has a deterministic fallback.** If an agent fails (step limit, invalid calls), a rule-based restructurer or template produces the result, marked `generated_by: "fallback"`. | The plan always completes, and evaluation can compare the agent against the baseline. |
| D5 | **Ratios are ceilings on spending and a floor on savings.** We never recommend spending more on needs or wants. Leftover money goes to savings, and the unused allowance is reported as "headroom". | An advisor should never say "spend more on dining out to hit 30%". |
| D6 | **Output is JSON first.** `PlanOutput` is a Pydantic model with an exported JSON schema. The text report is a pure function of it. | This is the contract with evaluation and a future front end. |
| D7 | **Provider-agnostic, OpenAI-compatible tool calling.** | Every candidate provider (OpenAI, OpenRouter, Gemini through its OpenAI-compatible endpoint) supports this format. |

## 4. Architecture
```
FinancialProfile JSON
   │
   ▼
[normalize]  profile ─► BudgetLines (monthly amounts, ids, bucket, flexibility tier)   (deterministic)
   │
   ├─► Current budget (as stated; surplus = unallocated cash)                         (deterministic)
   │
   ├─► Budget Restructurer Agent ── rule = 50/30/20 ──────┐   tools: get_budget_state, list_adjustable,
   ├─► Budget Restructurer Agent ── rule = goals + 5:3 ───┤          adjust_item, revert_item,
   │        (each runs in its own session; can run in    │          mark_one_time_discretionary, finalize
   │         parallel; fallback = rule-based restructurer)│
   │                                                      ▼
   │                                   3 × Budget (lines, buckets, adjustments + rationales)
   ▼
[forecast]   12-month month-by-month simulation per budget ─► goal verdicts             (deterministic)
[retirement] long-run projection per budget, 4% rule, compare to target age             (deterministic)
[metrics]    savings rate, emergency-fund months, debt-to-income, high-APR debt         (deterministic)
   │
   ▼
Advisor Agent   tools: get_section, compare_budgets, simulate_retirement, submit_recommendation
   │            (fallback = templated recommendation from metrics)
   ▼
PlanOutput JSON ─► render_text() ─► Markdown/text report
```

## 5. Normalization: from profile to budget lines
`normalize(profile, config) -> NormalizedProfile` converts each profile item into a `BudgetLine`:

| Field | Source |
|-------|--------|
| `id` | Stable slug, e.g. `rec:eating-out`, `debt:federal-student-loan:min`, `goal:japan-trip`, `ret:contribution`, `one:laptop` |
| `kind` | `income` · `recurring` · `one_time` · `debt_min` · `goal_contribution` · `retirement_contribution` |
| `bucket` | `need` · `want` · `savings` (see below) |
| `monthly_amount` | `monthly_amount()` from `schema.py`. Percent-of-income savings are converted using net monthly income. |
| `tier` | `protected` · `drop_only` · `scalable` (§6) |
| `source_ref` | A JSON pointer into the profile, e.g. `/recurring_expenses/2`, so the front end can trace each line back |

Bucket assignment:
- Recurring expenses use the extractor's `need_or_want`. If it is null, the line is a **need** and a warning is added.
- Debt minimum payments are **needs**. Extra payments above the minimum are **savings**.
- Goal contributions, retirement contributions and the overall savings target are **savings**.
- One-time expenses are kept outside the monthly buckets. They enter the forecast in the month of `expected_date`. If `expected_date` is null (or before the forecast), the expense is assumed to fall in the first forecast month and a warning is added.
- The monthly cash set aside for upcoming one-time costs (§7.4 step 0) is also outside the buckets. It is planned spending, not savings, and is reported as `totals.one_time_set_aside`, so `N + W + S + O + U = I`.

Income: the budget is based on the sum of net monthly income. If any income has `is_net` set to false or null, the amount is used as given and the warning `income_may_be_gross` is attached, because 50/30/20 is defined on take-home pay (see open question Q1 in the prototype plan).

Effective dates: the profile describes the user's go-forward situation (a new value replaces the old one), so the **budgets use every item** in the profile. The **forecast** respects `effective_date`: an item is active in a month if its `effective_date` is null or falls on or before the last day of that month.

## 6. Expense flexibility rules
Every expense line gets a tier. **Tools enforce the tiers**, and an adjustment that breaks them is rejected with an error the agent can read.

| Tier | Rule | Allowed actions | Examples |
|------|------|-----------------|----------|
| `protected` | Any **need**, any debt minimum payment, any line in a protected category (`housing`, `utilities`, `healthcare`, `insurance`, `childcare`, `education`) whatever its label, and any line whose label is null | None | Rent, medication, property tax, insurance, loan minimums |
| `drop_only` | A **want** with `is_fixed = true` (an all-or-nothing subscription or membership) | Keep or drop (set to 0) | Yoga membership, streaming |
| `scalable` | A **want** with `is_fixed` false or null | Any amount from 0 up to the current amount | Dining out, shopping, entertainment |

Further rules:
- **Category overrides label.** If the extractor labels a `healthcare` item as a want, it is still protected, and the warning `label_conflict` is recorded for the input phase.
- **One-time expenses** have no `need_or_want` field in the current schema, so they start as `protected`. The agent may call `mark_one_time_discretionary(id, rationale)` for an item that is clearly non-essential, such as a "Disneyland trip", after which it can be dropped. The reclassification and its rationale are recorded, and the pipeline adds a `one_time_reclassified` warning for each one so the user and evaluation see it.
  - Because the schema gives code nothing to check, the tool refuses names that look essential (`budget.ESSENTIAL_ONE_TIME_RE`: medical, dental, surgery, hospital, prescription, tax, legal, tuition, repair, rent, deposit, moving, childcare, insurance, emergency, ...). This is a conservative stop-gap; the real fix is a label on `OneTimeExpense` (open question 1).
  - A one-time item may be cut only while there is a shortfall to fix (wants over the allowance, the set-aside or a goal deadline unfunded, or the savings floor out of reach); otherwise it stays as the person planned it.
- **No increases.** Restructuring never raises an expense.
- **Optional trimming of variable needs** (such as groceries) is behind `config.need_trim_cap`, which is **0% by default**. The requirement says only non-essential expenses may be changed.

## 7. Budget variants
Notation: `I` is net monthly income. `N`, `W` and `S` are the monthly totals of needs, wants and savings. `U = I − N − W − S` is the unallocated surplus.

### 7.1 Current
The budget as stated. `U` is reported as unallocated cash, which goes to the cash balance in the forecast. No adjustments are made.

### 7.2 50/30/20
Targets: `N ≤ 0.50·I`, `W ≤ 0.30·I`, `S ≥ 0.20·I`.

Order of priority when the targets conflict:
1. Protected needs are fixed. If `N > 0.5·I`, the needs target cannot be met. This is recorded as an **infeasibility** with the size of the gap. It is not an error.
2. **The savings floor comes before wants.** The wants allowance is `min(0.30·I, I − N − O − 0.20·I)`, never below 0, where `O` is the monthly set-aside the remaining one-time items need (§7.4 step 0). One-time costs are protected spending until reclassified, so like needs they come before the floor.
3. If the wants are above that allowance, the agent cuts `scalable` and `drop_only` wants, and discretionary one-time items, until they fit. Each cut needs a rationale.
4. If cutting every allowed want to 0 still leaves savings below 20%, the budget is marked infeasible. The **best achievable** split and the remaining gap are reported.
5. Any surplus is allocated to savings by the allocation policy in §7.4.

### 7.3 Goals + 5:3
1. **Required savings** `S_req` = max(the overall savings target, Σ the required goal contributions + the stated retirement contribution).
   - The required contribution for a goal with a deadline is `max(0, target − funded) / months_until_deadline`, where the months are the contribution months before the deadline.
   - A goal without a deadline uses its stated contribution.
   - A goal whose deadline has already passed (no forecast month starts before it) is planned like a goal without a deadline, and the warning `goal_deadline_passed` is added. Spreading the whole remainder over "one month" would otherwise become a permanent monthly line the size of the target. Its verdict stays `off_track` with no `required_monthly`.
   - If a goal has neither a deadline nor a contribution, it gets no requirement and the warning `goal_unscheduled` is added.
   - `funded` is 0 by default, because `current_savings_balance` is not earmarked for any goal (open question 3).
2. The remainder `R = I − S_req` is split with ceilings `N ≤ 5/8·R` and `W ≤ 3/8·R`. The wants allowance is `min(3/8·R, I − N − O − S_req)`, as in §7.2.
3. Conflicts follow the same order as in §7.2. Protected needs are fixed, the savings requirement comes before wants, and wants are cut to fit. If the goals still cannot be funded, the goals are funded in priority order (`high` > `medium` > `low`, then earliest deadline) and the unfunded goals are reported.
4. Any surplus goes through the allocation policy.

> Note: when `S_req = 0.2·I`, the 5:3 split of the remaining 80% is exactly 50/30. The two variants differ only when the goals require something other than 20%. The test fixtures must cover both cases.

### 7.4 Surplus allocation policy (both restructured budgets; deterministic)
Leftover money is assigned in this order. Each step records a policy rationale.
0. Cash set aside for one-time costs in the forecast window that current savings cannot cover (line `savings:one-time-costs`, kind `one_time_fund`), so the forecast never overdraws cash. For each cost in date order, the cumulative cost minus current savings must be saved by the end of its month; the line is the largest such monthly amount. This is spending, not savings: the line has no bucket and does not count toward the savings floor or the savings rate. It is one monthly line for the whole budget, so after the last cost it keeps adding to cash; after the window the retirement projection treats it as freed money (§9).
1. Shortfalls on goals with deadlines, by priority.
2. An emergency fund: the reserve is `config.emergency_months` (3 by default) × the monthly needs, and the gap after current savings is spread over `config.emergency_build_months` (12 by default). A user goal named like "emergency fund" counts toward the reserve only with the money its own line will put into it over those months (`min(target, line × build months)`). If that goal is not fully funded by its own line, this step's money goes to the goal's line first (so its verdict and completion date show it), and the rest to `savings:emergency-fund`.
3. Extra payments on debts with APR ≥ `config.high_apr_threshold` (8% by default), highest APR first. The extra is at most what pays the debt off in `horizon_months` level payments (interest included) minus the minimum, never the whole balance as a monthly line.
4. Retirement contributions, which are invested at the real return rate. With no retirement account, the rest goes to `savings:general` (cash).

The order is a design choice: the emergency fund comes before high-APR debt. When nothing is left for step 3, the advice says so ("Pay down X as soon as there is room") instead of telling the user to prioritise a debt the budget does not prepay. Whether a small starter reserve should come before high-APR debt and the rest after is open question 10.

### 7.5 Adjustment record (the record format; §7.6 and §7.7 follow)
Every change, whether made by the agent or by the policy, is stored like this:
```json
{
  "line_id": "rec:eating-out",
  "action": "reduce",                 // reduce | drop | reclassify | allocate
  "before_monthly": 450.00,
  "after_monthly": 150.00,
  "rationale": "Dining out is the largest discretionary line; cutting it to $150 brings wants within the 30% ceiling while keeping some social spending.",
  "rule": "50_30_20.wants_ceiling",
  "decided_by": "agent"               // agent | policy | fallback
}
```

### 7.6 Deficit handling
If needs, wants (after cuts) and the stated savings lines exceed income, `finalize` first lowers the stated savings, recorded with rule `deficit.reduce_savings` (decided by policy): contributions above what a goal's deadline requires first, then the lowest-priority goals, then retirement, then other savings. So savings lines are never changed by an agent, but deficit handling can lower them (the only downward change to a savings line). If protected needs alone exceed income, the budget keeps a negative `unallocated` and is infeasible.

### 7.7 Fallback restructurer
`fallback_restructure` is deterministic:
1. Only if `need_trim_cap > 0` and needs are above their ceiling: trim scalable (variable) needs by a common fraction of their allowed trim (rule `<budget>.needs_ceiling`).
2. Cut wants to the allowance. All-or-nothing wants are dropped only while flexible cuts alone cannot close the gap, choosing the smallest one that closes what flexible cuts cannot (else the largest). Flexible wants are then scaled by a common fraction so wants land on the allowance.
3. `finalize_budget` (§7.6, §7.4).
It never reclassifies one-time items: it cannot tell which are optional (open question 1).

## 8. Twelve-month forecast
`forecast(budget, normalized, config) -> Forecast` simulates the 12 calendar months that follow `meta.as_of`. Each month it does the following, in order:

1. **Income in.** Add each income active that month, respecting `effective_date`.
2. **Recurring expenses out.** Pay the budget's needs and wants.
3. **One-time expenses out.** Pay any one-time expense due that month, unless the budget dropped it.
4. **Debts.** Add a month of interest to each balance (`balance × apr/12`), then make the payment (the minimum plus any allocated extra), capped at the balance. When a debt is paid off, its payment goes back into the surplus.
5. **Goals.** Pay each goal's contribution into a separate goal fund. Contributions stop once a fund reaches its target, and the rest flows to cash.
6. **Retirement.** Add the contribution to the retirement portfolio, which grows at `(1 + real_return)^(1/12) − 1`.
7. **Cash.** The starting cash is `current_savings_balance`. Each month cash changes by the net cash flow and earns `config.cash_real_return` (0% by default).

Each `ForecastMonth` row has these fields: `month`, `income`, `needs`, `wants`, `one_time`, `debt_payments`, `goal_contributions`, `retirement_contribution`, `emergency_and_general_savings` (the cash-savings lines, which stay in cash), `net_cash_flow`, `cash_balance`, `goal_balances{}`, `debt_balances{}`, `retirement_balance`. Any month that ends with a negative cash balance is listed in `cash_shortfall_months`.

**Goal verdict** (per goal, per budget):
- `met`: the fund reaches its target on or before the deadline, within the window.
- `on_track`: the deadline is after the window, and the fund will reach the target in time at the month-12 contribution rate.
- `off_track`: the target will not be reached by the deadline. The verdict also gives `shortfall`, `projected_completion_date` and `required_monthly` to close the gap.
- `no_deadline`: the verdict gives the projected completion date only.

## 9. Retirement projection (4% rule)
**Retirement number** = 25 × the desired annual retirement spending. That spending comes from the first available source:
1. `retirement.desired_annual_spending`, if it is added to the schema (open question 2).
2. `retirement.target`, which is already a dollar figure (`target / 25` is reported as the implied spending).
3. 12 × (N + W) of **that budget**. This means a leaner budget also lowers the retirement number, and the report makes that visible.

**Projection.** The simulation starts from the state at the end of the 12-month forecast and runs month by month until the retirement number is reached or the user is `config.max_age` (100 by default).
- Investable assets = the retirement portfolio plus cash (excluding goal funds and the emergency reserve).
- The portfolio grows at `config.real_return` (5% by default).
- Cash (the surplus and the emergency/general savings lines) grows at `config.cash_real_return` (0% by default), so the two pools grow at different rates (open question 4).
- When a goal's deadline passes, its contribution is redirected to retirement. When a debt is paid off, its payment is also redirected to retirement. The one-time set-aside is treated the same way after the window. All three are configurable (`redirect_freed_payments_to_retirement`); without redirect they flow to cash.

**Outputs per budget:** `retirement_number`, `projected_retirement_age` and year (or `unreachable`), `target_retirement_age`, `delta_years` (negative means early), `matches_target` (bool), `monthly_investment` (everything added to investable assets in the first post-forecast month) split into `monthly_to_portfolio` and `monthly_to_cash`, and `required_monthly_to_hit_target`.

`required_monthly_to_hit_target` is today's `monthly_investment` plus the smallest extra monthly portfolio contribution that reaches the number by the target age. It is solved by bisection over the same projector (to half the money tolerance), not with the one-rate annuity formula, which overstated what the money earns when part of it stays in 0% cash. Feeding the extra back through `project_retirement(extra_monthly=...)` therefore always gives `matches_target = True` (tested on every fixture budget). When the target is already met, the figure is the smallest total that still meets it, taking cash first. It is null when the target age falls inside the forecast window.

If `retirement` is null or `current_age` is missing, the section is returned with `status: "insufficient_data"` and a list of the missing fields. The plan continues without it.

## 10. Metrics and recommendation
`compute_metrics()` (deterministic) produces:
- savings rate for each budget: savings lines / income. Unassigned cash is reported separately as `unallocated_share`, and the advice keeps them apart ("you save 3% and leave 15% unassigned").
- the stated overall savings target per month and, per budget, `savings_target_met`
- needs, wants and savings shares
- months of needs covered by cash (emergency fund)
- debt-to-income ratio
- the list of high-APR debts
- goals that are off track
- the retirement gap

**Advisor Agent** writes the recommendation, using only these tools:
- `get_section(name)` returns budgets, forecasts, retirement, metrics or profile (goals, debts, retirement inputs, warnings, assumptions) as JSON.
- `compare_budgets()` returns side-by-side key numbers for the three budgets.
- `simulate_retirement(budget_id, extra_monthly)` runs what-if checks, such as "what if you invest $300/mo more?". It is the same deterministic projector as §9. Its money results become citable only when `extra_monthly` is itself grounded or a round amount (a multiple of $50 up to the monthly income). Otherwise only the ages and years become citable, so the tool cannot launder an invented number into the allowed set.
- `submit_recommendation(...)` returns the structured output below, and ends the run.

```json
{
  "overall_status": "on_track | needs_attention | at_risk",
  "summary": "2–4 sentence assessment",
  "recommended_budget": "current | 50_30_20 | goals_5_3",
  "major_changes": [
    {"title": "Raise Japan-trip savings to $571/mo",
     "detail": "...", "impact": "Meets the June 2027 deadline instead of missing by $500",
     "evidence_refs": ["forecasts.goals_5_3.goals.goal:japan-trip"]}
  ],
  "strengths": ["..."],
  "risks": ["..."]
}
```

Guardrails (all in `submit_recommendation`, as tool errors the agent can fix):
- 3 to 5 major changes (`advice.MIN_MAJOR_CHANGES` / `MAX_MAJOR_CHANGES`).
- `evidence_refs` must resolve to existing paths in the `PlanOutput`, and must point at a value or element, not a whole section or budget (`budgets`, `forecasts.current`, `metrics.per_budget` are rejected).
- `overall_status` must equal `advice.overall_status(ctx)`, and `recommended_budget` must be one of `advice.eligible_budgets(ctx)`: the fallback's pick, plus the other restructured budget when it ties on off-track goals and cash shortfalls and retires within a year. This is the same rule the fallback uses, so the agent cannot publish a verdict the numbers contradict.
- **Claim checks** (`advice.false_claims`, heuristic, sentence by sentence): a sentence that calls a goal (by name, or "every goal") met or on track, without negation, is rejected if that goal is off track in the budget the sentence names (else the recommended budget). A sentence with "retire at/by N" must quote a projected or simulated age (within a year), or the target age with hedging ("your target of 65", "would retire by 65").
- A **numeric grounding check** extracts every number from the advisor's text and verifies that it matches a tool-produced value within rounding (heuristic below). The first ungrounded submission is a tool error listing the offending numbers. A second one ends the session with `{"accepted": false}`; the run then uses the templated fallback (marked `generated_by: "fallback"`, warning `agent_fallback`). This check also gives evaluation a ready-made metric.
- The prompt asks for general education-style guidance, not regulated investment advice.

**Grounding heuristic** (`advice.find_ungrounded`). A number is grounded if it is an integer 0 to 12 (counts, months), or within `max(0.51, min(p, 5% of v))` of an allowed value `v`, where `p` is the written precision: half a unit of the last written digit for "k/M/B/thousand/million" ("$1.2M": 50,000), or of the last nonzero digit for a whole number ending in zeros ("$517,000": 500), else 0. So "$571" matches 571.43 and "$1.2M" matches 1,152,089, but "$1 million" does not match 1.5M and "$2k" does not match 2,443. A decade ("your mid-40s") is grounded if an allowed value lies in that decade. Allowed values are the numeric leaves of the plan and the normalized profile plus derived forms (fractions × 100, monthly × 12, months / 12, differences across budgets, cut sizes, gaps to required amounts, the emergency reserve, years until retirement, rule constants, numbers in deterministic rationales and warnings), and `simulate_retirement` results.

**Measured false-accept rate** (2,000 random "$N" values per range against the deterministic plan's ~200-230 allowed values):

| Range of N | complete_profile | overspender |
|------------|------------------|-------------|
| 13-100 | 25% | 31% |
| 100-1,000 | 3% | 6% |
| 1,000-10,000 | 2% | 2% |
| 10,000-100,000 | 0% | 0% |

Before the precision rule (a flat 0.5% relative tolerance) these were 25% / 10% / 29% / 13% and 34% / 20% / 26% / 5%. Two-digit numbers stay dense because ages, percentages and years crowd that range; integers 0-12 always pass by design. Evaluation should read the check as "no number far from a computed value", not as proof that each number is the right one. A tighter, session-scoped allowed set (only values from tool results the agent actually received) is open question 11.

## 11. Agents and tool-calling runtime
### 11.1 Runtime (`agents/runtime.py`)
```python
class ChatModel(Protocol):
    def chat(self, messages: list[Message], tools: list[ToolSpec]) -> AssistantTurn: ...
    # AssistantTurn: text, tool_calls[{id, name, arguments: dict}], usage

def run_agent(model, system_prompt, tools: ToolRegistry, user_msg, max_steps=25) -> AgentResult
```
- Each tool is a Python function with a Pydantic argument model, which provides the JSON schema for the LLM and validates the arguments.
- Tool errors are returned to the model as tool results (`{"error": "..."}`). They are not raised, so the agent can correct itself. An unexpected exception inside a handler (a bug) is also returned, as `{"error": "Internal tool error ...", "internal_error": true}`, so the run and its trace survive; the pipeline adds an `agent_tool_error` warning.
- Arguments with NaN or infinity are rejected before a handler runs (the `ToolArgs` base sets `allow_inf_nan=False`, `dispatch` checks raw arguments, and `NaN`/`Infinity` JSON tokens from a provider are refused at parse time). `policy.validate_adjustment` also rejects non-finite amounts.
- The run stops when the agent calls a terminal tool (`finalize` / `submit_recommendation`), or when it reaches `max_steps`, in which case the fallback runs.
- Every step is logged: tool calls, arguments, results, tokens and latency. A model turn's usage is recorded once, on the turn's first tool call, so summing the trace gives the true totals; the per-agent totals are also in `meta.agent_usage`. The log is stored in `PlanOutput.trace` so evaluation can inspect how the agent behaved.
- Exceptions outside tool handlers (for example a missing prompt file) propagate to the pipeline, which uses the fallback and adds `agent_error`, for both agents alike. A model or provider failure ends the run with `completed = false` (warning `agent_fallback`).
- Implementations: an `OpenAICompatibleModel` (base URL, model name and key from config), and a `ScriptedModel` for tests, which replays a fixed list of turns.
- Coordinate with Arina's planned `llm.py`: either extend `complete()` with a `tools=` argument or let `runtime.py` wrap it, so that the project has a single provider config.

### 11.2 Budget Restructurer Agent (one session per restructured budget)
The agent's session state is a mutable `Budget` copy for one rule. Tools:

| Tool | Purpose |
|------|---------|
| `get_budget_state()` | Lines, bucket totals and percentages, targets, the gap to each target, and the best-achievable split |
| `list_adjustable()` | Lines with tier `drop_only` / `scalable`, discretionary one-time items, and the allowed range for each |
| `adjust_item(line_id, new_monthly_amount, rationale)` | Reduce or drop. Validated by tier, no increases, finite amount, rationale ≥ 20 characters whose numbers are grounded in the session state (before or after the change); a one-time item only while there is a shortfall (§6). A rejected change is not applied. Returns the new state. |
| `revert_item(line_id)` | Undo an earlier adjustment |
| `mark_one_time_discretionary(line_id, rationale)` | Make a one-time item droppable (§6). Refused for essential-looking names. |
| `finalize(summary_rationale)` | Rejected while wants exceed the allowance and allowed cuts remain (under-cutting), and while wants sit below the allowance by more than one of the agent's own cuts could restore: any amount for a scalable want, or the whole of an all-or-nothing want that would still fit (over-cutting; an unavoidable overshoot from dropping an all-or-nothing item is fine). The summary's numbers must be grounded. Otherwise records infeasibility if a target is missed, applies the surplus policy and returns the final budget. |

The prompt (`prompts/output/budget_restructurer.md`) tells the agent to:
- prefer the smallest set of cuts
- cut lower-value wants first
- avoid dropping an item entirely when a partial cut is enough
- make each rationale specific to the user's situation

"The smallest set of cuts" and "no invented numbers" are enforced in the tools as described in the table, not only in the prompt.

### 11.3 Advisor Agent
See §10. It is read-only apart from `submit_recommendation`.

### 11.4 Pipeline entry point
```python
generate_plan(profile: FinancialProfile, config: PlanConfig = PlanConfig(),
              model: ChatModel | None = None) -> PlanOutput
# model=None → fully deterministic run (fallback restructurer + templated advice); useful for tests & eval baseline
render_text(plan: PlanOutput) -> str
```
A CLI makes manual runs easy: `python -m finagent.output examples/profiles/complete_profile.json [--json|--text] [--no-llm]`.

## 12. Output contract: `PlanOutput`
```
PlanOutput
├─ meta: user_id, profile_version, generated_at, start_month, config, model, agent_usage{}, warnings[]
├─ budgets: {current | 50_30_20 | goals_5_3}
│    └─ Budget: id, description, income_monthly, lines[], one_time[],
│               totals{need,want,savings,one_time_set_aside,unallocated}, shares{} (same keys, fractions),
│               targets{need_max,want_max,savings_min}, headroom{}, feasible (null for current),
│               infeasibility{reason, gaps{bucket → $}}, unfunded_goals[], adjustments[], generated_by,
│               summary_rationale
├─ forecasts: {budget_id → Forecast: months[12], goals[GoalVerdict], cash_shortfall_months[], end_state}
├─ retirement: {budget_id → RetirementProjection | {status: insufficient_data, missing[]}}
├─ metrics: {...}
├─ recommendation: {...}  (§10)
└─ trace: [agent steps]   (excluded from the text report)
```
The JSON schema is exported to `schemas/plan_output.schema.json`, using the same pattern as `financial_profile.schema.json`, and a test checks that the export is up to date.

Money in the JSON keeps full float precision (for example `433.3333333333333`); only the text report and the agent tool results round. The tests compare with `pytest.approx`.

## 13. Module layout
```
src/finagent/output/
  __init__.py        # generate_plan, render_text
  __main__.py        # CLI
  config.py          # PlanConfig (returns, thresholds, protected categories, caps, max_age)
  models.py          # BudgetLine, Budget, Adjustment, Forecast, GoalVerdict, RetirementProjection, PlanOutput
  normalize.py       # profile → NormalizedProfile (lines, buckets, tiers, warnings)
  policy.py          # tier rules, adjustment validation, surplus allocation
  budget.py          # targets per rule, feasibility, best-achievable, fallback restructurer
  goals.py           # required contributions, verdicts
  forecast.py        # 12-month simulation
  retirement.py      # retirement number, long-run projection, required contribution
  metrics.py
  advice.py          # plan context, evidence refs, numeric grounding, claim checks, fallback recommendation
  format.py          # shared money/percent formatters, budget labels, cash-savings kinds
  render.py          # PlanOutput → Markdown/text
  tools/
    budget_tools.py  # restructurer tool registry bound to a Budget session
    advisor_tools.py
  agents/
    runtime.py       # ChatModel protocol, run_agent loop, OpenAICompatibleModel, ScriptedModel
    prompts.py       # loads prompts/output/*.md (repo checkout or editable install)
    restructurer.py
    advisor.py
  pipeline.py        # orchestration (§4)
prompts/output/
  budget_restructurer.md
  advisor.md
tests/output/
  fixtures/          # extra profiles (see §14.3)
  invariants.py      # the §14.2 invariants, shared by fallback and agent tests
  test_normalize.py  test_policy.py  test_budget.py  test_goals.py  test_forecast.py
  test_retirement.py test_metrics.py test_budget_tools.py test_advisor.py test_advice.py
  test_runtime.py    test_restructurer.py test_pipeline.py test_render.py test_contract.py test_live.py
```

## 14. Test plan
The deterministic modules are built **test-first**. Each module's tests are written before its implementation, in the order of §15.

### 14.1 Unit tests (deterministic)
- **normalize**:
  - bucket assignment
  - percent-of-income conversion
  - null `need_or_want` → protected need with a warning
  - gross-income warning
  - one-time item with no date
  - stable line ids
- **policy**:
  - every tier and every action, accept and reject cases
  - a category override beats the label (healthcare labelled "want" stays protected)
  - no increases
  - empty or short rationale rejected
  - one-time reclassification
  - `need_trim_cap` respected when it is set
- **budget**:
  - targets for both rules
  - wants allowance when needs > 50%
  - best-achievable and infeasibility
  - fallback restructurer output satisfies every constraint
  - the 5:3 variant matches 50/30/20 when `S_req = 0.2·I`
- **goals**:
  - the required contribution counts months correctly (Nov 2026 to a 2027-06-01 deadline is 7 months)
  - priority ordering when funds are short
  - every verdict type
- **forecast**:
  - one-time expense lands in the correct month
  - effective dates switch items on
  - debt amortization and payoff (payment capped at the balance, freed cash goes back to the surplus)
  - goal fund stops at its target
  - a cash shortfall is detected
  - exactly 12 rows
- **retirement**:
  - precedence of the retirement-number sources
  - projection against a closed-form future value
  - `unreachable` at max_age
  - `required_monthly_to_hit_target` round-trips (feeding it back hits the target age)
  - insufficient data
- **metrics**: emergency-fund months, debt-to-income, high-APR detection.

### 14.2 Invariant tests (every restructured budget, on all fixtures)
- Allocations sum to income: `N + W + S + U = I`.
- No protected line has changed.
- No line has increased.
- Every changed line has exactly one adjustment record, with a non-empty rationale.
- Either the targets are met, or `feasible = false` with a gap that equals the best achievable one.
- Wants fit the allowance (or nothing allowed is left to cut), and no more was cut than needed (§11.2).
- `tests/output/invariants.py` holds these checks. They run on every fallback budget, and on agent budgets from four scripted agents on every fixture: greedy, reclassify-and-drop, over-cutter (refused by `finalize`, so the fallback is used) and revert-then-recut. A reclassified one-time item may change; nothing else protected may.
- Property-based testing with `hypothesis` (an optional dev dependency) would generate random profiles and check the same invariants. Not done yet; the edge profiles of `test_pipeline.py` (no or zero income, no expenses, an all-protected deficit, no age, age past target, a past-deadline goal, each with `need_trim_cap` 0 and 0.1) cover the edge paths.

### 14.3 Fixtures
| Fixture | Purpose |
|---------|---------|
| `complete_profile.json` (existing) | Healthy surplus. No cuts needed, so it tests the surplus policy. The Japan goal is off track in the current budget. |
| `overspender.json` (new) | $4,000 net. Needs are 77% (rent $2,200, medication $80, insurance $150, groceries $500, credit card minimum $150 at 24% APR). Wants: dining $600 and streaming $60. One-time "Disneyland trip" $2,500. 50/30/20 is infeasible on needs, and wants must be cut to protect the 20% floor. |
| `big_goals.json` (new) | Goals that require more than 20%, so the 5:3 variant differs from 50/30/20. |
| `no_retirement.json` (new) | `retirement: null`. Tests the insufficient-data path. |
| `mislabeled.json` (new) | Medication labelled "want", plus a null label. Tests the protections. |

### 14.4 Agent tests (no network)
- **runtime with ScriptedModel:**
  - tool dispatch
  - argument validation errors come back to the model
  - the terminal tool ends the run
  - `max_steps` triggers the fallback
  - the trace is recorded
- **restructurer:**
  - A scripted agent that tries to cut rent gets an error, then cuts dining out. The final budget is valid and the rationale is stored.
  - A scripted agent that calls `finalize` too early is rejected while allowed cuts remain.
- **advisor:**
  - an invalid `evidence_refs` is rejected
  - the numeric grounding check catches a made-up number and triggers the retry
- **pipeline:** `generate_plan(profile, model=None)` is fully deterministic. Running it twice gives identical JSON.

### 14.5 Contract and render
- The `PlanOutput` of every fixture validates against `schemas/plan_output.schema.json`, and the exported schema is up to date. The full validation needs `jsonschema`, which is not yet in the dev extras (open question 9), so it is skipped without it; a dependency-free test checks that every emitted key is declared.
- `render_text` contains every section, plus every adjustment rationale and goal verdict. A snapshot test checks that `examples/plans/*` are byte-identical to a fresh deterministic run (`--generated-at 2026-10-28T15:00:00`).

### 14.6 Live smoke test (manual, `pytest -m live`)
Run the real model on all fixtures. Check that the invariants hold and the numeric grounding check passes, and save the outputs for evaluation to review. `tests/output/test_live.py` does this; it is skipped unless `FINAGENT_LLM_MODEL` is set, and writes the plans to `$FINAGENT_LIVE_OUT` if given.

## 15. Implementation order
1. `models.py`, `config.py`, `normalize.py`, `policy.py`, with their tests.
2. `budget.py` (targets, feasibility, fallback restructurer), `goals.py`, and the invariant tests.
3. `forecast.py`, `retirement.py`, `metrics.py`.
4. `PlanOutput`, the schema export, `render.py`, and the deterministic `generate_plan(model=None)` plus the CLI. **This is the first end-to-end milestone, and it can be shared with evaluation.**
5. `runtime.py` with `ScriptedModel`, then the budget tools and the Restructurer Agent.
6. The advisor tools, the Advisor Agent and the grounding check.
7. `OpenAICompatibleModel`, the prompts, and the live smoke test once the team picks a provider.

## 16. Worked example: `complete_profile.json`
Net income `I` = $6,200/mo.

| Bucket | Lines | Monthly | Share |
|--------|-------|---------|-------|
| Needs | Rent 1,800 · Groceries 433.33 (100/wk) · Student-loan minimum 210 | 2,443.33 | 39.4% |
| Wants | Eating out 450 · Yoga 120 | 570.00 | 9.2% |
| Savings | Japan trip 500 · Retirement 400 | 900.00 | 14.5% |
| Unallocated | | 2,286.67 | 36.9% |

- **Current:** the Japan trip gets 7 contributions (Nov to May) before 2027-06-01, which is $3,500 of $4,000. The verdict is **off track** by $500, and the required contribution is $571.43/mo. The $1,500 laptop in Dec 2026 is paid from cash ($6,500 at the start).
- **50/30/20:** needs and wants are already under their ceilings, so no cuts are needed. Savings is below 20% ($900 against $1,240), and the surplus policy fixes it in this order:
  1. Japan shortfall (+$71.43)
  2. Emergency fund (target 3 × $2,443.33 = $7,330)
  3. No high-APR debt (5.5% < 8%), so this step is skipped
  4. The rest goes to retirement

  Headroom is reported: wants could rise to $1,860, but the plan does not recommend it.
- **Goals + 5:3:** `S_req` = max(20% × 6,200 = 1,240, 571.43 + 400 = 971.43) = $1,240. This equals 20%, so the ceilings are again $3,100 / $1,860. This is the "reduces to 50/30/20" case. Use `big_goals.json` to exercise a real difference.
- **Retirement (target $1.5M, age 24 → 60, 5% real):** the stated $400/mo plus the $8,000 balance reach about $517k by 60 (5% effective annual, compounded monthly). That is far short when only the retirement account counts. Whether cash counts as investable changes the answer a lot, so that assumption is shown in the output (open question 4).

## 17. Open questions
1. **Schema:** add `need_or_want` (and `category`) to `OneTimeExpense`, so that "Disneyland trip" is labelled by the extractor rather than reclassified by the agent, and run it through `policy.classify_tier` like recurring lines. Until then, a name guard blocks reclassifying essential-looking items (§6) and each reclassification is a `one_time_reclassified` warning. *(Arina; proposed change to `schema.py`)*
2. **Schema:** add `retirement.desired_annual_spending`. The 4% rule is defined on spending. `target` works as a stand-in. *(Arina)*
3. Is `current_savings_balance` earmarked for any goal, or is it general and emergency cash? Default: general.
4. Does cash count as investable assets for retirement, and at what return? Default: it counts, at a 0% real return. Because surplus stays in cash, two budgets with the same `monthly_investment` can retire years apart; the report shows the portfolio/cash split, and `required_monthly_to_hit_target` assumes the extra is invested.
5. Is `retirement.monthly_contribution` paid out of the stated net income, or deducted before it (a pre-tax 401(k))? Default: out of net income. If it is deducted before, the savings rate is double-counted.
6. Does the overall savings target set a floor for the Goals + 5:3 variant (as proposed), or should only explicit goals count?
7. Should variable needs such as groceries be trimmable by a small capped amount? Default: no (`need_trim_cap = 0`).
8. Default assumptions: 5% real return, 3-month emergency fund, 8% high-APR threshold. Are these acceptable to evaluation?
9. **Packaging:** add `jsonschema` to `[project.optional-dependencies].dev` in `pyproject.toml` so the §14.5 schema validation runs (it is skipped today), and decide whether `prompts/` should ship as package data (the agents read it from the repository).
10. Should a small starter emergency reserve (for example 1 month of needs) come before extra payments on high-APR debt, with the rest of the reserve after it? Today the whole reserve comes first (§7.4), so the overspender's 24% card gets no extra payment in any budget.
11. Should the grounding check accept only values from tool results the agent actually received in its session, rather than every value in the plan? That would lower the false-accept rate of §10 further, at the cost of rejecting correct numbers the agent saw in an earlier result's derived form.
12. The one-time set-aside is one monthly line for the whole window, so it keeps collecting after the last cost. Should the budget model allow a line with an end month?
