"""Budget variants (TDD §7).

The current budget, the targets of the restructured variants (50/30/20 and
goals + 5:3), the validated adjustment operations the restructurer tools call,
the surplus allocation policy applied by `finalize_budget`, and the rule-based
fallback restructurer.
"""

from __future__ import annotations

import re
from typing import Callable

from finagent.output import goals as goal_math
from finagent.output import policy
from finagent.output.config import PlanConfig
from finagent.output.format import cents as _money
from finagent.output.models import (
    Adjustment,
    AdjustmentAction,
    Bucket,
    Budget,
    BudgetId,
    BudgetLine,
    BucketTotals,
    BudgetTargets,
    DebtInfo,
    DecidedBy,
    GeneratedBy,
    GoalInfo,
    Infeasibility,
    LineKind,
    NormalizedProfile,
    Tier,
)

DESCRIPTIONS = {
    BudgetId.current: "Your budget as you described it.",
    BudgetId.rule_50_30_20: "Restructured to the 50/30/20 rule: at most 50% needs, at most 30% wants, at least 20% savings.",
    BudgetId.goals_5_3: "Restructured to fund your saving goals first, with the rest split 5:3 between needs and wants.",
}

# 50/30/20 shares and the 5:3 split of the remainder are the definition of the rules, not tunables.
_RULE_50_30_20 = (0.50, 0.30, 0.20)
_SPLIT_5_3 = (5 / 8, 3 / 8)

EMERGENCY_FUND_ID = "savings:emergency-fund"
GENERAL_SAVINGS_ID = "savings:general"
ONE_TIME_FUND_ID = "savings:one-time-costs"
RETIREMENT_ID = "ret:contribution"

Restructurer = Callable[[NormalizedProfile, BudgetId, PlanConfig], Budget]


class AdjustmentError(Exception):
    """A proposed budget change breaks a rule; the message is readable by the agent."""


# One-time items have no category or need/want label in the schema (TDD §17 Q1), so this name guard is
# what keeps an agent from reclassifying (and then dropping) an essential cost. Conservative on purpose.
ESSENTIAL_ONE_TIME_RE = re.compile(
    r"medic|dental|dentist|surgery|surgical|doctor|hospital|clinic|prescription|pharmac|therap|health|"
    r"\btax|legal|lawyer|attorney|court|tuition|school|education|textbook|exam\b|repair|\brent\b|deposit|"
    r"mortgage|moving|relocat|childcare|daycare|insurance|funeral|visa\b|passport|licen[cs]e|registration|"
    r"\bvet\b|veterinar|utilit|emergency",
    re.IGNORECASE,
)


# --- shared helpers -------------------------------------------------------------


def compute_totals(lines: list[BudgetLine], income: float) -> BucketTotals:
    """Bucket totals; the one-time set-aside is spending outside the buckets (N + W + S + O + U = I)."""
    totals = {bucket: 0.0 for bucket in Bucket}
    set_aside = 0.0
    for line in lines:
        if line.bucket is not None:
            totals[line.bucket] += line.monthly_amount
        elif line.kind is LineKind.one_time_fund:
            set_aside += line.monthly_amount
    spent = sum(totals.values()) + set_aside
    return BucketTotals(
        need=totals[Bucket.need], want=totals[Bucket.want], savings=totals[Bucket.savings],
        one_time_set_aside=set_aside, unallocated=income - spent,
    )


def compute_shares(totals: BucketTotals, income: float) -> BucketTotals | None:
    if income <= 0:
        return None
    return BucketTotals(**{k: v / income for k, v in totals.model_dump().items()})


def refresh(budget: Budget) -> Budget:
    """Recompute totals and shares after lines changed (in place; returns the budget)."""
    budget.totals = compute_totals(budget.lines, budget.income_monthly)
    budget.shares = compute_shares(budget.totals, budget.income_monthly)
    return budget


def copy_lines(lines: list[BudgetLine]) -> list[BudgetLine]:
    return [line.model_copy(deep=True) for line in lines]


def build_current_budget(norm: NormalizedProfile, config: PlanConfig | None = None) -> Budget:
    """The budget exactly as stated. Any surplus stays unallocated (it goes to cash)."""
    budget = Budget(
        id=BudgetId.current, description=DESCRIPTIONS[BudgetId.current],
        income_monthly=norm.net_monthly_income, lines=copy_lines(norm.lines), one_time=copy_lines(norm.one_time),
        totals=BucketTotals(), generated_by=GeneratedBy.as_stated,
    )
    return refresh(budget)


# --- targets ----------------------------------------------------------------------


def goal_requirement(goal: GoalInfo, norm: NormalizedProfile) -> float:
    """Monthly amount a goal needs: the deadline requirement, else its stated contribution (TDD §7.3).

    A goal whose deadline has already passed has no deadline requirement, so it
    falls back to its stated contribution (warning `goal_deadline_passed`).
    """
    req = goal_math.required_monthly(goal, norm.start_month)
    return req if req is not None else (goal.stated_monthly or 0.0)


def required_savings(norm: NormalizedProfile) -> float:
    """S_req of goals + 5:3 (TDD §7.3): max(overall target, goal requirements + retirement)."""
    goal_total = sum(goal_requirement(goal, norm) for goal in norm.goals)
    retirement = norm.retirement.monthly_contribution if norm.retirement is not None else 0.0
    return max(norm.overall_savings_target_monthly or 0.0, goal_total + retirement)


def goal_shortfall_monthly(budget: Budget, norm: NormalizedProfile) -> float:
    """What surplus step 1 still has to add to goal lines to meet every deadline requirement."""
    total = 0.0
    for goal in norm.goals:
        required = goal_math.required_monthly(goal, norm.start_month)
        if required is None:
            continue
        line = _goal_line(budget, goal, create=False)
        total += max(0.0, required - (line.monthly_amount if line is not None else 0.0))
    return total


def targets_for(budget_id: BudgetId, norm: NormalizedProfile, config: PlanConfig) -> BudgetTargets | None:
    """Ceilings on needs and wants and the savings floor of a variant (None for current)."""
    income = norm.net_monthly_income
    if budget_id is BudgetId.rule_50_30_20:
        need, want, save = _RULE_50_30_20
        return BudgetTargets(need_max=need * income, want_max=want * income, savings_min=save * income)
    if budget_id is BudgetId.goals_5_3:
        s_req = required_savings(norm)
        remainder = max(0.0, income - s_req)
        need, want = _SPLIT_5_3
        return BudgetTargets(need_max=need * remainder, want_max=want * remainder, savings_min=s_req)
    return None


def want_allowance(budget: Budget, targets: BudgetTargets, set_aside: float = 0.0) -> float:
    """Most the wants may total: needs, upcoming one-time costs and the savings floor come first (TDD §7.2 step 2)."""
    needs = compute_totals(budget.lines, budget.income_monthly).need
    return max(0.0, min(targets.want_max, budget.income_monthly - needs - set_aside - targets.savings_min))


def allowance_for(budget: Budget, norm: NormalizedProfile, config: PlanConfig) -> float:
    """`want_allowance` with the one-time set-aside the budget's remaining one-time items require."""
    return want_allowance(budget, budget.targets, one_time_set_aside(budget, norm, config))


# --- restructuring session operations (called by the budget tools) -----------------


def start_restructure(norm: NormalizedProfile, budget_id: BudgetId, config: PlanConfig) -> Budget:
    """A fresh copy of the stated budget, with the variant's targets, ready for adjustments."""
    if budget_id is BudgetId.current:
        raise ValueError("The current budget is never restructured.")
    budget = Budget(
        id=budget_id, description=DESCRIPTIONS[budget_id], income_monthly=norm.net_monthly_income,
        lines=copy_lines(norm.lines), one_time=copy_lines(norm.one_time), totals=BucketTotals(),
        targets=targets_for(budget_id, norm, config), generated_by=GeneratedBy.agent,
    )
    return refresh(budget)


def _find_line(budget: Budget, line_id: str) -> BudgetLine:
    for line in budget.lines + budget.one_time:
        if line.id == line_id:
            return line
    raise AdjustmentError(f"Unknown line id '{line_id}'.")


def _amount_record_index(budget: Budget, line_id: str) -> int | None:
    """Index of the reduce/drop record of a line (at most one exists)."""
    for i, adj in enumerate(budget.adjustments):
        if adj.line_id == line_id and adj.action in (AdjustmentAction.reduce, AdjustmentAction.drop):
            return i
    return None


def apply_adjustment(
    budget: Budget,
    line_id: str,
    new_amount: float,
    rationale: str,
    config: PlanConfig,
    decided_by: DecidedBy = DecidedBy.agent,
    rule: str | None = None,
) -> Adjustment | None:
    """Reduce or drop an expense line, after validating it against the tier rules.

    Keeps one record per line: a new change replaces the earlier record. Setting
    a line back to its original amount removes its record and returns None.
    `rule` defaults to '<budget_id>.wants_ceiling'.
    """
    line = _find_line(budget, line_id)
    error = policy.validate_adjustment(line, new_amount, rationale, config)
    if error:
        raise AdjustmentError(error)
    tol = config.money_tolerance
    index = _amount_record_index(budget, line_id)
    if abs(new_amount - line.original_amount) <= tol:
        line.monthly_amount = line.original_amount
        if index is not None:
            budget.adjustments.pop(index)
        refresh(budget)
        return None
    new_amount = 0.0 if new_amount <= tol else new_amount
    record = Adjustment(
        line_id=line_id,
        action=AdjustmentAction.drop if new_amount == 0 else AdjustmentAction.reduce,
        before_monthly=line.original_amount, after_monthly=new_amount, rationale=rationale.strip(),
        rule=rule or f"{budget.id.value}.wants_ceiling", decided_by=decided_by,
    )
    line.monthly_amount = new_amount
    if index is None:
        budget.adjustments.append(record)
    else:
        budget.adjustments[index] = record
    refresh(budget)
    return record


def revert_adjustment(budget: Budget, line_id: str) -> None:
    """Undo the reduce/drop of a line; if it has none, undo its one-time reclassification."""
    line = _find_line(budget, line_id)
    index = _amount_record_index(budget, line_id)
    if index is not None:
        budget.adjustments.pop(index)
        line.monthly_amount = line.original_amount
    else:
        reclass = [i for i, a in enumerate(budget.adjustments)
                   if a.line_id == line_id and a.action is AdjustmentAction.reclassify]
        if not reclass:
            raise AdjustmentError(f"'{line.name}' has no adjustment to revert.")
        budget.adjustments.pop(reclass[0])
        line.discretionary = False
        line.tier = policy.one_time_tier(False)
    refresh(budget)


def mark_one_time_discretionary(
    budget: Budget, line_id: str, rationale: str, config: PlanConfig, decided_by: DecidedBy = DecidedBy.agent
) -> Adjustment:
    """Reclassify a one-time expense as non-essential so it can be dropped (TDD §6)."""
    line = _find_line(budget, line_id)
    if line.kind is not LineKind.one_time:
        raise AdjustmentError(f"'{line.name}' is not a one-time expense.")
    if line.discretionary:
        raise AdjustmentError(f"'{line.name}' is already marked discretionary.")
    if ESSENTIAL_ONE_TIME_RE.search(line.name):
        raise AdjustmentError(
            f"'{line.name}' looks like an essential cost (medical, legal, tax, education, housing, repair...), "
            "so it cannot be reclassified as discretionary."
        )
    if len(rationale.strip()) < config.min_rationale_chars:
        raise AdjustmentError(f"Every change needs a specific rationale of at least {config.min_rationale_chars} characters.")
    line.discretionary = True
    line.tier = policy.one_time_tier(True)
    record = Adjustment(
        line_id=line_id, action=AdjustmentAction.reclassify, before_monthly=line.monthly_amount,
        after_monthly=line.monthly_amount, rationale=rationale.strip(),
        rule=f"{budget.id.value}.one_time_discretionary", decided_by=decided_by,
    )
    budget.adjustments.append(record)
    return record


# --- feasibility --------------------------------------------------------------------


def best_achievable(budget: Budget, config: PlanConfig) -> BucketTotals:
    """Totals with every adjustable monthly line at its lowest allowed amount."""
    lines = [
        line.model_copy(update={"monthly_amount": policy.min_allowed(line, config)}) if policy.is_adjustable(line) else line
        for line in budget.lines
    ]
    return compute_totals(lines, budget.income_monthly)


def remaining_cuts_possible(budget: Budget, config: PlanConfig) -> float:
    """Dollars per month that further allowed cuts could still free up."""
    return sum(
        max(0.0, line.monthly_amount - policy.min_allowed(line, config))
        for line in budget.lines if policy.is_adjustable(line)
    )


def gaps(budget: Budget, config: PlanConfig) -> dict[str, float]:
    """Bucket -> dollars by which the budget misses its target (only real misses)."""
    if budget.targets is None:
        return {}
    t, totals = budget.targets, compute_totals(budget.lines, budget.income_monthly)
    candidates = {
        "need": totals.need - t.need_max,
        "want": totals.want - t.want_max,
        "savings": t.savings_min - totals.savings,
    }
    return {k: v for k, v in candidates.items() if v > config.money_tolerance}


def _infeasibility_reason(budget: Budget, found: dict[str, float], config: PlanConfig) -> str:
    t, totals = budget.targets, budget.totals
    room = remaining_cuts_possible(budget, config)
    parts = []
    if "need" in found:
        parts.append(f"needs are {_money(totals.need)}/mo, {_money(found['need'])} above the {_money(t.need_max)} ceiling")
    if "want" in found:
        parts.append(f"wants are {_money(totals.want)}/mo, {_money(found['want'])} above the {_money(t.want_max)} ceiling")
    if "savings" in found:
        kept = (f" after {_money(totals.one_time_set_aside)}/mo is kept for upcoming one-time costs"
                if totals.one_time_set_aside > config.money_tolerance else "")
        parts.append(f"savings are {_money(totals.savings)}/mo{kept}, {_money(found['savings'])} short of the "
                     f"{_money(t.savings_min)} floor")
    if "want" not in found and "savings" not in found:
        tail = "so cutting wants cannot close this gap."
    elif room > config.money_tolerance:
        tail = f"but further allowed cuts of up to {_money(room)}/mo to flexible expenses remain."
    else:
        tail = "and no further allowed cut to wants can close the remaining gap."
    return (
        f"This budget cannot meet its targets: {'; '.join(parts)}. "
        f"Protected needs (housing, healthcare, insurance, debt minimums and other essentials) cannot be cut, {tail}"
    )


# --- finalize: deficit handling and surplus allocation (TDD §7.4) -------------------


def _get_line(budget: Budget, line_id: str) -> BudgetLine | None:
    return next((line for line in budget.lines if line.id == line_id), None)


def _savings_line(budget: Budget, line_id: str, name: str, kind: LineKind, **fields) -> BudgetLine:
    """Existing savings line, or a new one at 0 appended to the budget (the one-time fund has no bucket)."""
    line = _get_line(budget, line_id)
    if line is None:
        bucket = None if kind is LineKind.one_time_fund else Bucket.savings
        line = BudgetLine(
            id=line_id, name=name, kind=kind, bucket=bucket, monthly_amount=0.0,
            original_amount=0.0, tier=Tier.savings, **fields,
        )
        budget.lines.append(line)
    return line


def _allocate(budget: Budget, line: BudgetLine, amount: float, step: str, rationale: str) -> None:
    before = line.monthly_amount
    line.monthly_amount = before + amount
    budget.adjustments.append(Adjustment(
        line_id=line.id, action=AdjustmentAction.allocate, before_monthly=before,
        after_monthly=line.monthly_amount, rationale=rationale, rule=f"surplus.{step}", decided_by=DecidedBy.policy,
    ))
    refresh(budget)


def _goal_line(budget: Budget, goal: GoalInfo, create: bool) -> BudgetLine | None:
    line_id = f"{goal.id}:contribution"
    if not create:
        return _get_line(budget, line_id)
    return _savings_line(
        budget, line_id, f"{goal.name} savings", LineKind.goal_contribution,
        target_ref=goal.id, source_ref=goal.source_ref,
    )


def _reduce_savings_for_deficit(budget: Budget, norm: NormalizedProfile, config: PlanConfig) -> None:
    """If spending plus stated savings exceed income, cut savings: lowest-priority goals first, then retirement."""
    tol = config.money_tolerance
    goal_rank = {g.id: i for i, g in enumerate(sorted(norm.goals, key=goal_math.goal_priority_key))}
    savings = [l for l in budget.lines if l.bucket is Bucket.savings and l.monthly_amount > tol]
    goal_lines = sorted(
        (l for l in savings if l.kind is LineKind.goal_contribution),
        key=lambda l: (-goal_rank.get(l.target_ref, len(goal_rank)), l.id),
    )
    others = [l for l in savings if l.kind is LineKind.retirement_contribution]
    others += [l for l in savings if l.kind not in (LineKind.goal_contribution, LineKind.retirement_contribution)]
    goals_by_id = {g.id: g for g in norm.goals}
    starting = {l.id: l.monthly_amount for l in savings}

    def cut(line: BudgetLine, floor: float, why: str) -> None:
        deficit = -budget.totals.unallocated
        if deficit <= tol or line.monthly_amount <= floor + tol:
            return
        after = max(floor, line.monthly_amount - deficit)
        after = 0.0 if after <= tol else after
        line.monthly_amount = after
        before = starting[line.id]
        record = Adjustment(
            line_id=line.id, action=AdjustmentAction.drop if after == 0 else AdjustmentAction.reduce,
            before_monthly=before, after_monthly=after,
            rationale=(
                f"Spending and stated savings exceeded income by {_money(deficit)}/mo, so {line.name} goes from "
                f"{_money(before)} to {_money(after)}/mo; {why}"
            ),
            rule="deficit.reduce_savings", decided_by=DecidedBy.policy,
        )
        # One record per line: a second cut to the same line replaces the first.
        budget.adjustments = [a for a in budget.adjustments
                              if not (a.line_id == line.id and a.rule == "deficit.reduce_savings")]
        budget.adjustments.append(record)
        refresh(budget)

    # Contributions above what a goal's deadline requires go first, so no goal is
    # left short while another is over-funded; then lower-priority savings.
    for line in goal_lines:
        goal = goals_by_id.get(line.target_ref)
        required = goal_math.required_monthly(goal, norm.start_month) if goal is not None else None
        if required is not None:
            cut(line, required, "the excess above what the goal's deadline requires is cut first.")
    for line in goal_lines + others:
        cut(line, 0.0, "lower-priority savings are reduced first.")


def payoff_extra(debt: DebtInfo, months: int) -> float:
    """Payment above the minimum that pays the debt off in `months` level payments (interest included)."""
    rate = (debt.apr or 0.0) / 100 / 12
    if rate > 0:
        payment = debt.balance * rate / (1 - (1 + rate) ** -months)
    else:
        payment = debt.balance / months
    return max(0.0, payment - (debt.minimum_payment or 0.0))


def one_time_set_aside(budget: Budget, norm: NormalizedProfile, config: PlanConfig) -> float:
    """Monthly amount to keep in cash so one-time costs in the forecast window never overdraw savings.

    For each cost (in date order) the cumulative cost so far, minus current
    savings, must be saved by the end of that cost's month.
    """
    window_end = goal_math.add_months(norm.start_month, config.horizon_months)
    due = sorted(
        (line for line in budget.one_time
         if line.monthly_amount > 0 and line.expected_date is not None and line.expected_date < window_end),
        key=lambda line: line.expected_date,
    )
    cumulative, needed = 0.0, 0.0
    for line in due:
        cumulative += line.monthly_amount
        months = goal_math.months_between(norm.start_month, line.expected_date) + 1
        needed = max(needed, (cumulative - norm.current_savings_balance) / months)
    return max(0.0, needed)


def _allocate_surplus(budget: Budget, norm: NormalizedProfile, config: PlanConfig) -> None:
    tol = config.money_tolerance

    def surplus() -> float:
        return budget.totals.unallocated

    # 0. Save ahead for one-time costs that current savings cannot cover (avoids overdrawing cash).
    #    This is spending, not savings: the line has no bucket and is reported as totals.one_time_set_aside.
    amount = min(surplus(), one_time_set_aside(budget, norm, config))
    if amount > tol:
        names = ", ".join(line.name for line in budget.one_time if line.monthly_amount > 0)
        line = _savings_line(budget, ONE_TIME_FUND_ID, "Upcoming one-time costs", LineKind.one_time_fund)
        _allocate(budget, line, amount, "one_time_costs", (
            f"Kept {_money(amount)}/mo in cash for upcoming one-time costs ({names}) that current savings of "
            f"{_money(norm.current_savings_balance)} cannot cover, so cash is never overdrawn. This is money for "
            "planned spending, so it does not count toward the savings target."
        ))

    # 1. Shortfalls on goals with deadlines, by priority.
    for goal in sorted(norm.goals, key=goal_math.goal_priority_key):
        required = goal_math.required_monthly(goal, norm.start_month)
        if required is None or surplus() <= tol:
            continue
        existing = _goal_line(budget, goal, create=False)
        current = existing.monthly_amount if existing is not None else 0.0
        amount = min(surplus(), required - current)
        if amount <= tol:
            continue
        line = _goal_line(budget, goal, create=True)
        months = goal_math.contribution_months(norm.start_month, goal.deadline)
        full = amount >= required - current - tol
        rationale = (
            f"Raised {goal.name} savings from {_money(current)} to {_money(current + amount)}/mo"
            + (", the amount" if full else f", toward the {_money(required)}/mo")
            + f" needed to reach {_money(goal.target_amount)} by {goal.deadline:%B} {goal.deadline.day}, {goal.deadline.year}"
            f" ({months} monthly contributions)."
        )
        _allocate(budget, line, amount, "goal_shortfall", rationale)

    # 2. Emergency fund, built over config.emergency_build_months.
    needs = budget.totals.need
    reserve = config.emergency_months * needs
    build = config.emergency_build_months
    # A user goal that is itself an emergency fund builds part of the reserve, but only with the money its
    # own line actually puts into it over the build period.
    em_goals = sorted((g for g in norm.goals if "emergency" in g.name.lower() and g.target_amount),
                      key=goal_math.goal_priority_key)

    def goal_funding(goal: GoalInfo) -> float:
        own = _goal_line(budget, goal, create=False)
        return min(goal.target_amount, (own.monthly_amount if own is not None else 0.0) * build)

    earmarked = sum(goal_funding(g) for g in em_goals)
    gap = max(0.0, reserve - norm.current_savings_balance - earmarked)
    amount = min(surplus(), gap / build)
    # The emergency goal's own line gets this money first, so its verdict and completion date show it.
    for goal in em_goals:
        give = min(amount, max(0.0, goal.target_amount - goal_funding(goal)) / build)
        if give <= tol:
            continue
        own = _goal_line(budget, goal, create=True)
        _allocate(budget, own, give, "emergency_fund", (
            f"Put {_money(give)}/mo into {goal.name}, your emergency-fund goal of {_money(goal.target_amount)}, as "
            f"the first part of an emergency reserve of {config.emergency_months:g} months of needs "
            f"({_money(reserve)}); current savings of {_money(norm.current_savings_balance)} leave a {_money(gap)} "
            f"gap, built over {build} months."
        ))
        amount -= give
    if amount > tol:
        line = _savings_line(budget, EMERGENCY_FUND_ID, "Emergency fund", LineKind.emergency_fund)
        _allocate(budget, line, amount, "emergency_fund", (
            f"Set aside {_money(amount)}/mo toward an emergency fund of {config.emergency_months:g} months of needs "
            f"({_money(reserve)}); current savings of {_money(norm.current_savings_balance)} leave a {_money(gap)} gap, "
            f"built over {config.emergency_build_months} months."
        ))

    # 3. Extra payments on high-APR debts, highest APR first: at most what pays the debt off over the
    #    forecast window (a level payment), so a monthly line is never sized to the whole balance.
    high_apr = [d for d in norm.debts if d.apr is not None and d.apr >= config.high_apr_threshold and d.balance > 0]
    for debt in sorted(high_apr, key=lambda d: (-d.apr, d.id)):
        needed = payoff_extra(debt, config.horizon_months)
        amount = min(surplus(), needed)
        if amount <= tol:
            continue
        line = _savings_line(
            budget, f"{debt.id}:extra", f"{debt.name} (extra payment)", LineKind.debt_extra,
            target_ref=debt.id, source_ref=debt.source_ref,
        )
        full = amount >= needed - tol
        _allocate(budget, line, amount, "high_apr_debt", (
            f"Put an extra {_money(amount)}/mo toward {debt.name}, which charges {debt.apr:g}% APR "
            f"(at or above the {config.high_apr_threshold:g}% high-interest threshold); "
            + (f"with the minimum, that pays it off within {config.horizon_months} months."
               if full else f"paying it off within {config.horizon_months} months would take {_money(needed)}/mo extra.")
            + " The highest rate is paid first."
        ))

    # 4. The rest to retirement (or general savings when there is no retirement account).
    amount = surplus()
    if amount <= tol:
        return
    if norm.retirement is not None:
        line = _savings_line(budget, RETIREMENT_ID, "Retirement contribution", LineKind.retirement_contribution,
                             source_ref="/retirement")
        rationale = (
            f"Invested the remaining {_money(amount)}/mo surplus in retirement (now {_money(line.monthly_amount + amount)}/mo), "
            f"assumed to grow at a {config.real_return:.0%} real return."
        )
    else:
        line = _savings_line(budget, GENERAL_SAVINGS_ID, "General savings", LineKind.general_savings)
        rationale = f"No retirement account was given, so the remaining {_money(amount)}/mo surplus goes to general savings."
    _allocate(budget, line, amount, "retirement", rationale)


def _unfunded_goals(budget: Budget, norm: NormalizedProfile, config: PlanConfig) -> list[str]:
    out = []
    for goal in sorted(norm.goals, key=goal_math.goal_priority_key):
        required = goal_math.required_monthly(goal, norm.start_month)
        if required is None:
            continue
        line = _goal_line(budget, goal, create=False)
        funded = line.monthly_amount if line is not None else 0.0
        if funded < required - config.money_tolerance:
            out.append(goal.id)
    return out


def finalize_budget(
    budget: Budget,
    norm: NormalizedProfile,
    config: PlanConfig,
    generated_by: GeneratedBy,
    summary_rationale: str | None = None,
) -> Budget:
    """Resolve any deficit, allocate the surplus, and record feasibility (in place; returns the budget)."""
    if budget.targets is None:
        raise ValueError("Only restructured budgets are finalized.")
    refresh(budget)
    _reduce_savings_for_deficit(budget, norm, config)
    _allocate_surplus(budget, norm, config)
    refresh(budget)

    budget.unfunded_goals = _unfunded_goals(budget, norm, config)
    found = gaps(budget, config)
    budget.feasible = not found
    budget.infeasibility = Infeasibility(reason=_infeasibility_reason(budget, found, config), gaps=found) if found else None
    t, totals = budget.targets, budget.totals
    budget.headroom = {"need": max(0.0, t.need_max - totals.need), "want": max(0.0, t.want_max - totals.want)}
    budget.generated_by = generated_by
    budget.summary_rationale = summary_rationale
    return budget


# --- fallback restructurer (TDD D4) -------------------------------------------------


def _cut_wants_to(budget: Budget, allowance: float, config: PlanConfig) -> None:
    """Cut wants to the allowance with as little overshoot as the all-or-nothing items allow.

    Flexible wants are scaled down by a common fraction of their reducible amount.
    Only if that cannot close the gap are all-or-nothing wants dropped: the
    smallest one that closes what flexible cuts cannot, else the largest, and so
    on. The flexible cut is then sized so wants land on the allowance.
    """
    tol = config.money_tolerance
    rule = f"{budget.id.value}.wants_ceiling"
    excess = budget.totals.want - allowance
    if excess <= tol:
        return
    scalable = [l for l in budget.lines if l.bucket is Bucket.want and l.tier is Tier.scalable]
    room = {l.id: l.monthly_amount - policy.min_allowed(l, config) for l in scalable}
    total_room = sum(room.values())

    # 1. Drop all-or-nothing wants only while flexible cuts alone cannot close the gap.
    candidates = [l for l in budget.lines if l.bucket is Bucket.want and l.tier is Tier.drop_only and l.monthly_amount > tol]
    remaining = excess
    while remaining - total_room > tol and candidates:
        short = remaining - total_room
        enough = sorted((l for l in candidates if l.monthly_amount >= short - tol), key=lambda l: (l.monthly_amount, l.id))
        pick = enough[0] if enough else max(candidates, key=lambda l: (l.monthly_amount, l.id))
        candidates.remove(pick)
        remaining -= pick.monthly_amount
        apply_adjustment(budget, pick.id, 0.0, (
            f"Fallback rule {rule}: wants were {_money(allowance + excess)}/mo against a {_money(allowance)}/mo "
            f"allowance and cutting flexible wants could free only {_money(total_room)}/mo, so the all-or-nothing "
            f"{pick.name} ({_money(pick.original_amount)}/mo) is dropped."
        ), config, decided_by=DecidedBy.fallback, rule=rule)

    # 2. Scale every flexible want down by the same fraction of its reducible amount.
    if remaining <= tol or total_room <= tol:
        return
    fraction = min(1.0, remaining / total_room)
    for line in scalable:
        if room[line.id] <= 0:
            continue
        floor = policy.min_allowed(line, config)
        new = max(floor, line.monthly_amount - fraction * room[line.id])
        apply_adjustment(budget, line.id, new, (
            f"Fallback rule {rule}: wants were {_money(allowance + excess)}/mo against a {_money(allowance)}/mo "
            f"allowance, so every flexible want is cut by {fraction:.0%} of its reducible amount; "
            f"{line.name} goes from {_money(line.original_amount)} to {_money(new)}/mo."
        ), config, decided_by=DecidedBy.fallback, rule=rule)


def _trim_needs_to(budget: Budget, config: PlanConfig) -> None:
    """With need_trim_cap > 0, trim scalable (variable) needs proportionally toward the needs ceiling."""
    tol = config.money_tolerance
    excess = budget.totals.need - budget.targets.need_max
    trimmable = [l for l in budget.lines if l.bucket is Bucket.need and l.tier is Tier.scalable]
    room = {l.id: l.monthly_amount - policy.min_allowed(l, config) for l in trimmable}
    total_room = sum(room.values())
    if excess <= tol or total_room <= tol:
        return
    fraction = min(1.0, excess / total_room)
    rule = f"{budget.id.value}.needs_ceiling"
    for line in trimmable:
        if room[line.id] <= tol:
            continue
        new = line.monthly_amount - fraction * room[line.id]
        apply_adjustment(budget, line.id, new, (
            f"Fallback rule {rule}: needs were {_money(budget.targets.need_max + excess)}/mo against a "
            f"{_money(budget.targets.need_max)}/mo ceiling, so each variable need is trimmed by {fraction:.0%} of the "
            f"{config.need_trim_cap:.0%} allowed; {line.name} goes from {_money(line.original_amount)} to {_money(new)}/mo."
        ), config, decided_by=DecidedBy.fallback, rule=rule)


def fallback_restructure(norm: NormalizedProfile, budget_id: BudgetId, config: PlanConfig) -> Budget:
    """Rule-based restructurer: trim variable needs (only if need_trim_cap > 0), cut wants to the allowance,
    then apply the surplus policy."""
    budget = start_restructure(norm, budget_id, config)
    _trim_needs_to(budget, config)
    _cut_wants_to(budget, allowance_for(budget, norm, config), config)
    return finalize_budget(budget, norm, config, GeneratedBy.fallback)


def build_budgets(
    norm: NormalizedProfile, config: PlanConfig, restructure: Restructurer = fallback_restructure
) -> dict[BudgetId, Budget]:
    """The current budget plus both restructured variants."""
    budgets = {BudgetId.current: build_current_budget(norm, config)}
    for budget_id in (BudgetId.rule_50_30_20, BudgetId.goals_5_3):
        budgets[budget_id] = restructure(norm, budget_id, config)
    return budgets
