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
