"""Scenario format and the synthetic scenario generator."""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from finagent.schema import FinancialProfile

# format 

class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Probe(_Model):
    """Checks one thing a turn should have changed; `stale` is the old value, to catch outdated memory."""

    kind: Literal["item_present", "item_absent", "item_amount", "scalar"]
    in_list: Optional[Literal["income", "recurring_expenses", "one_time_expenses", "savings_goals", "debts"]] = None
    match: list[str] = Field(default_factory=list)
    field: Optional[str] = None
    path: Optional[str] = None
    expected: Optional[float] = None
    expected_text: Optional[str] = None
    stale: Optional[float] = None
    rel_tol: float = 0.01
    note: str = ""


class Turn(_Model):
    timestamp: datetime
    text: str
    event: str
    expected_status: Literal["ready", "needs_clarification"] = "ready"
    expected_missing: list[str] = Field(default_factory=list)
    expected_profile: dict
    probes: list[Probe] = Field(default_factory=list)

    def profile(self) -> FinancialProfile:
        return FinancialProfile.model_validate(self.expected_profile)


class Scenario(_Model):
    id: str
    source: Literal["synthetic", "handwritten"]
    archetype: str
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    seed: Optional[int] = None
    turns: list[Turn]

    def final_turn(self) -> Turn:
        """The last turn where the profile is complete, which is where we build the plan."""
        ready = [t for t in self.turns if t.expected_status == "ready"]
        return ready[-1] if ready else self.turns[-1]

    def final_profile(self) -> FinancialProfile:
        return self.final_turn().profile()


def load_scenarios(paths) -> list[Scenario]:
    """Load scenarios from files or folders (*.json holds one scenario, *.jsonl one per line)."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    files = []
    for p in map(Path, paths):
        files += sorted(p.rglob("*.json")) + sorted(p.rglob("*.jsonl")) if p.is_dir() else [p]
    out = []
    for f in files:
        if f.suffix == ".jsonl":
            out += [Scenario.model_validate_json(line) for line in f.read_text().splitlines() if line.strip()]
        else:
            out.append(Scenario.model_validate_json(f.read_text()))
    ids = [s.id for s in out]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate scenario ids: {sorted({i for i in ids if ids.count(i) > 1})}")
    return sorted(out, key=lambda s: s.id)


def save_jsonl(scenarios, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(s.model_dump_json() + "\n" for s in scenarios))
    return path


# generator helpers 

FACTOR = {"weekly": 52 / 12, "biweekly": 26 / 12, "semimonthly": 2, "monthly": 1, "quarterly": 1 / 3, "annual": 1 / 12}
FREQ_TEXT = {"weekly": ["a week", "per week"], "biweekly": ["every two weeks"], "semimonthly": ["twice a month"],
             "monthly": ["a month", "per month", "monthly"], "annual": ["a year"]}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def nice(x: float) -> float:
    """Round to a number people would actually say."""
    step = 5 if x < 100 else 10 if x < 1000 else 50
    return float(max(step, round(x / step) * step))


def money(x: float) -> str:
    return f"${x:,.0f}" if float(x).is_integer() else f"${x:,.2f}"


def month_start(d: date, ahead: int = 0) -> date:
    n = d.year * 12 + d.month - 1 + ahead
    return date(n // 12, n % 12 + 1, 1)


def month_name(d: date) -> str:
    return f"{MONTHS[d.month - 1]} {d.year}"


# name, category, is_fixed, share of income, sentence, match keywords
NEEDS = {
    "utilities": ("Utilities", "utilities", False, (0.02, 0.04), "Utilities are around {amt} {freq}.", ["utilit"]),
    "phone": ("Phone bill", "utilities", True, (0.01, 0.02), "My phone bill is {amt} {freq}.", ["phone"]),
    "car_insurance": ("Car insurance", "insurance", True, (0.025, 0.04), "Car insurance is {amt} {freq}.", ["insurance"]),
    "gas": ("Gas", "transport", False, (0.03, 0.05), "I spend about {amt} {freq} on gas.", ["gas"]),
    "transit": ("Transit pass", "transport", True, (0.015, 0.025), "A transit pass costs {amt} {freq}.", ["transit"]),
    "medication": ("Medication", "healthcare", True, (0.01, 0.03), "I pay {amt} {freq} for medication.", ["medic"]),
    "health_ins": ("Health insurance", "insurance", True, (0.04, 0.07), "Health insurance is {amt} {freq}.", ["health"]),
    "childcare": ("Daycare", "childcare", True, (0.12, 0.2), "Daycare for my kid is {amt} {freq}.", ["daycare", "child"]),
}
# name, category, is_fixed, phrase, match keywords
WANTS = {
    "dining": ("Eating out", "dining_out", False, "around {amt} {freq} eating out", ["eat", "dining", "restaurant"]),
    "shopping": ("Shopping", "shopping", False, "about {amt} {freq} on shopping", ["shopping"]),
    "concerts": ("Concerts and events", "entertainment", False, "{amt} {freq} on concerts and events", ["concert"]),
    "rideshare": ("Rideshares", "transport", False, "{amt} {freq} on rideshares", ["ride", "uber"]),
    "streaming": ("Streaming services", "subscriptions", True, "{amt} {freq} on streaming", ["stream"]),
    "gym": ("Gym membership", "fitness", True, "a {amt} {freq} gym membership", ["gym"]),
    "gaming": ("Video games", "entertainment", False, "{amt} {freq} on video games", ["game"]),
    "coffee": ("Coffee shops", "dining_out", False, "{amt} {freq} on coffee", ["coffee"]),
    "beauty": ("Hair and nails", "personal_care", False, "{amt} {freq} on hair and nails", ["hair", "nail"]),
}
FIXED_WANT_AMOUNTS = {"streaming": (25, 60), "gym": (40, 120)}
# name, phrase, target range, deadline range in months
GOALS = {
    "japan": ("Japan trip", "a trip to Japan", (2500, 6000), (5, 14)),
    "emergency": ("Emergency fund", "an emergency fund", None, (5, 24)),
    "house": ("House down payment", "a down payment on a house", (15000, 45000), (24, 48)),
    "wedding": ("Wedding", "my wedding", (8000, 25000), (5, 24)),
    "car": ("New car", "a new car", (5000, 15000), (5, 24)),
    "moving": ("Moving deposit", "a deposit for moving", (2000, 5000), (4, 12)),
    "laptop": ("New laptop", "a new laptop", (1000, 2500), (3, 10)),
}
ONE_TIME = {
    "laptop": ("Laptop", "a {amt} laptop I need to buy in {when}"),
    "medical": ("Medical bill", "a {amt} medical bill due in {when}"),
    "vacation": ("Disneyland trip", "a {amt} Disneyland trip planned for {when}"),
    "car_repair": ("Car repair", "a {amt} car repair coming up in {when}"),
    "wedding_gift": ("Friend's wedding", "about {amt} for a friend's wedding in {when}"),
}


@dataclass(frozen=True)
class Archetype:
    name: str
    description: str
    income: tuple[float, float]       # monthly take-home
    rent_share: tuple[float, float]
    n_needs: tuple[int, int]          # extra need items besides rent and groceries
    want_share: tuple[float, float]
    n_wants: tuple[int, int]
    p_card: float                     # chance of each debt
    p_student: float
    p_car: float
    n_goals: tuple[int, int]
    goal_scale: float                 # >1 makes goals need more than 20% of income
    n_one_time: tuple[int, int]
    age: tuple[int, int]
    p_retirement: float = 0.9
    pay: str = "monthly"              # monthly | biweekly | gross_annual


ARCHETYPES = {a.name: a for a in [
    Archetype("healthy_saver", "Comfortable surplus and modest wants; no cuts needed.",
              (5500, 9000), (0.22, 0.30), (1, 3), (0.06, 0.14), (2, 4), 0.1, 0.4, 0.2, (1, 2), 0.8, (0, 1), (24, 40)),
    Archetype("overspender", "Needs above 50% and heavy wants; wants must be cut.",
              (3200, 4800), (0.42, 0.55), (2, 4), (0.18, 0.30), (3, 6), 0.7, 0.3, 0.3, (1, 2), 1.0, (0, 2), (23, 38)),
    Archetype("big_goals", "Goals that need more than 20% of income.",
              (4500, 8000), (0.22, 0.32), (1, 3), (0.10, 0.22), (3, 5), 0.2, 0.3, 0.1, (2, 3), 2.6, (0, 1), (26, 36)),
    Archetype("debt_heavy", "High-APR credit card plus loans.",
              (3800, 6500), (0.25, 0.35), (1, 3), (0.12, 0.22), (3, 5), 1.0, 0.8, 0.5, (1, 2), 1.0, (0, 1), (25, 42)),
    Archetype("deficit", "Essential costs at or above income.",
              (2400, 3400), (0.55, 0.70), (3, 5), (0.08, 0.15), (2, 4), 0.6, 0.4, 0.4, (1, 1), 1.0, (0, 1), (22, 35)),
    Archetype("gross_income", "Income given as a yearly salary before tax.",
              (5000, 9000), (0.22, 0.32), (1, 3), (0.10, 0.20), (2, 4), 0.2, 0.4, 0.2, (1, 2), 1.0, (0, 1), (25, 45),
              pay="gross_annual"),
    Archetype("irregular_pay", "Weekly or biweekly pay and mixed expense frequencies.",
              (3500, 6500), (0.25, 0.35), (1, 3), (0.10, 0.22), (3, 5), 0.3, 0.3, 0.2, (1, 2), 1.0, (0, 2), (22, 40),
              pay="biweekly"),
    Archetype("one_time_heavy", "Several one-time costs inside the 12-month window.",
              (4000, 7000), (0.25, 0.35), (1, 3), (0.10, 0.20), (2, 4), 0.3, 0.3, 0.2, (1, 2), 1.0, (2, 3), (24, 45)),
    Archetype("no_retirement", "No retirement information.",
              (4000, 7000), (0.25, 0.35), (1, 3), (0.10, 0.20), (2, 4), 0.3, 0.3, 0.2, (1, 2), 1.0, (0, 1), (22, 30),
              p_retirement=0.0),
    Archetype("near_retirement", "Age 50-60 with a close target age.",
              (5000, 9500), (0.20, 0.30), (1, 3), (0.10, 0.20), (2, 4), 0.2, 0.0, 0.3, (0, 1), 1.0, (0, 1), (50, 60),
              p_retirement=1.0),
]}


class _Persona:
    """Builds the true profile and the sentences describing it at the same time."""

    def __init__(self, rng: random.Random, arch: Archetype, user_id: str, as_of: datetime):
        self.rng, self.arch = rng, arch
        self.start = month_start(as_of.date(), 1)
        self.p = {"meta": {"user_id": user_id, "as_of": as_of.isoformat(), "profile_version": 1,
                           "memory_method": "full_history"},
                  "income": [], "recurring_expenses": [], "one_time_expenses": [], "savings_goals": [],
                  "overall_savings_target": None, "debts": [], "retirement": None, "current_savings_balance": None}
        self.sentences: list[str] = []
        self.missing: list[str] = []
        self.income = 0.0

    def u(self, lo, hi):
        return self.rng.uniform(lo, hi)

    def freq_text(self, freq):
        return self.rng.choice(FREQ_TEXT[freq])

    def add_income(self, leave_net_unclear: bool):
        net = nice(self.u(*self.arch.income))
        job = self.rng.choice(["Salary", "Paycheck", "Job"])
        if self.arch.pay == "gross_annual":
            gross = nice(net * 12 / self.u(0.70, 0.78))
            self.p["income"].append({"source": job, "amount": gross, "frequency": "annual", "is_net": False,
                                     "effective_date": None})
            self.sentences.append(f"I make {money(gross)} a year before taxes.")
            self.income = gross / 12
            return
        freq = self.rng.choice(["biweekly", "weekly"]) if self.arch.pay == "biweekly" else \
            self.rng.choice(["monthly", "monthly", "semimonthly"])
        amt = nice(net / FACTOR[freq])
        if leave_net_unclear:  # doesn't say take-home or gross, so the input phase should ask
            self.missing.append("income.is_net")
            self.sentences.append(f"I get paid {money(amt)} {self.freq_text(freq)}.")
        else:
            self.sentences.append(self.rng.choice([f"I take home about {money(amt)} {self.freq_text(freq)} after tax.",
                                                   f"My take-home pay is {money(amt)} {self.freq_text(freq)}."]))
        self.p["income"].append({"source": job, "amount": amt, "frequency": freq,
                                 "is_net": None if leave_net_unclear else True, "effective_date": None})
        self.income = amt * FACTOR[freq]

    def _expense(self, name, cat, need, fixed, monthly, sentence, freq="monthly"):
        amt = nice(monthly / FACTOR[freq])
        self.p["recurring_expenses"].append({"name": name, "amount": amt, "frequency": freq, "category": cat,
                                             "need_or_want": "need" if need else "want", "is_fixed": fixed,
                                             "effective_date": None})
        return sentence.format(amt=money(amt), freq=self.freq_text(freq))

    def add_needs(self):
        a = self.arch
        self.sentences.append(self._expense("Rent", "housing", True, True, self.income * self.u(*a.rent_share),
                                            self.rng.choice(["Rent is {amt} {freq}.", "I pay {amt} {freq} in rent."])))
        freq = "weekly" if a.pay == "biweekly" else self.rng.choice(["weekly", "monthly"])
        self.sentences.append(self._expense("Groceries", "groceries", True, False, self.income * self.u(0.06, 0.10),
                                            "I spend about {amt} {freq} on groceries.", freq))
        keys = list(NEEDS)
        if self.rng.random() < 0.5:
            keys.remove("gas"); keys.remove("car_insurance")
        else:
            keys.remove("transit")
        if a.name != "deficit" and self.rng.random() < 0.85:
            keys.remove("childcare")
        for k in self.rng.sample(keys, min(self.rng.randint(*a.n_needs), len(keys))):
            name, cat, fixed, share, sentence, _ = NEEDS[k]
            self.sentences.append(self._expense(name, cat, True, fixed, self.income * self.u(*share), sentence))

    def add_wants(self):
        keys = self.rng.sample(list(WANTS), self.rng.randint(*self.arch.n_wants))
        fixed = {k: nice(self.u(*FIXED_WANT_AMOUNTS[k])) for k in keys if k in FIXED_WANT_AMOUNTS}
        flexible = [k for k in keys if k not in fixed]
        weights = [self.rng.random() + 0.3 for _ in flexible]
        rest = max(0.0, self.income * self.u(*self.arch.want_share) - sum(fixed.values()))
        parts = []
        for k in keys:
            name, cat, is_fixed, phrase, _ = WANTS[k]
            monthly = fixed.get(k) or max(20.0, rest * weights[flexible.index(k)] / sum(weights))
            freq = "weekly" if k in ("coffee", "dining") and self.rng.random() < 0.3 else "monthly"
            parts.append(self._expense(name, cat, False, is_fixed, monthly, phrase, freq))
        if parts:
            body = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
            self.sentences.append(f"{self.rng.choice(['Most months I spend', 'For fun I spend'])} {body}.")

    def add_debts(self, hide_apr: bool):
        a, r = self.arch, self.rng
        if r.random() < a.p_student:
            bal, apr = nice(self.u(8000, 40000)), round(self.u(3.5, 7.0), 1)
            mn = nice(bal * self.u(0.009, 0.012))
            self.p["debts"].append({"name": "Student loan", "type": "student_loan", "balance": bal, "apr": apr,
                                    "minimum_payment": mn, "effective_date": None})
            self.sentences.append(f"I have {money(bal)} in student loans at {apr}% with a {money(mn)} minimum payment.")
        if r.random() < a.p_car:
            bal, apr = nice(self.u(5000, 20000)), round(self.u(4.5, 9.5), 1)
            mn = nice(bal * self.u(0.022, 0.03))
            self.p["debts"].append({"name": "Car loan", "type": "auto_loan", "balance": bal, "apr": apr,
                                    "minimum_payment": mn, "effective_date": None})
            self.sentences.append(f"My car loan has {money(bal)} left at {apr}% and I pay {money(mn)} a month.")
        if r.random() < a.p_card:
            bal, apr = nice(self.u(1200, 9000)), float(r.choice([18.9, 21.0, 22.0, 24.0, 26.9, 29.0]))
            mn = nice(max(25, bal * 0.03))
            self.p["debts"].append({"name": "Credit card", "type": "credit_card", "balance": bal,
                                    "apr": None if hide_apr else apr, "minimum_payment": mn, "effective_date": None})
            if hide_apr:
                self.missing.append("debts.apr")
                self.sentences.append(f"I also have about {money(bal)} on a credit card and pay the {money(mn)} minimum.")
            else:
                self.sentences.append(f"There's {money(bal)} on my credit card at {apr:g}% and I pay the {money(mn)} minimum.")

    def add_goals(self):
        for k in self.rng.sample(list(GOALS), self.rng.randint(*self.arch.n_goals)):
            name, phrase, target_range, month_range = GOALS[k]
            months = self.rng.randint(*month_range)
            deadline = month_start(self.start, months)
            lo, hi = target_range or (self.income * 1.5, self.income * 3.5)
            target = nice(self.u(lo, hi) * self.arch.goal_scale)
            goal = {"name": name, "target_amount": target, "deadline": deadline.isoformat(), "contribution": None,
                    "priority": None, "effective_date": None}
            s = f"I'd like to save {money(target)} for {phrase} by {month_name(deadline)}"
            if self.rng.random() < 0.5:
                c = nice(target / months * self.u(0.6, 1.3))
                goal["contribution"] = {"type": "amount", "value": c, "frequency": "monthly"}
                s += f", and I'm putting {money(c)} a month toward it"
            if self.rng.random() < 0.35:
                goal["priority"] = self.rng.choice(["high", "low"])
                s += "; it's my top priority" if goal["priority"] == "high" else "; it's not urgent though"
            self.p["savings_goals"].append(goal)
            self.sentences.append(s + ".")
        if self.rng.random() < 0.4:
            pct = float(self.rng.choice([10, 15, 20, 25]))
            self.p["overall_savings_target"] = {"type": "percent_of_income", "value": pct, "frequency": "monthly"}
            self.sentences.append(f"Overall I want to save {pct:g}% of my income.")

    def add_one_time(self):
        for k in self.rng.sample(list(ONE_TIME), self.rng.randint(*self.arch.n_one_time)):
            name, phrase = ONE_TIME[k]
            when = month_start(self.start, self.rng.randint(0, 10)).replace(day=15)
            amt = nice(self.u(400, 3000))
            self.p["one_time_expenses"].append({"name": name, "amount": amt, "expected_date": when.isoformat(),
                                                "effective_date": None})
            self.sentences.append("There's " + phrase.format(amt=money(amt), when=month_name(when)) + ".")

    def add_retirement(self):
        if self.rng.random() >= self.arch.p_retirement:
            return
        age = self.rng.randint(*self.arch.age)
        target_age = min(70, max(age + 5, self.rng.choice([55, 60, 62, 65, 67])))
        bal = nice(max(0.0, (age - 22) * self.income * self.u(0.3, 1.4)))
        contrib = nice(self.income * self.u(0.0, 0.10)) if self.rng.random() < 0.8 else 0.0
        target = nice(self.u(0.8, 2.0) * 1_000_000) if self.rng.random() < 0.5 else None
        self.p["retirement"] = {"current_age": age, "target_retirement_age": target_age, "current_balance": bal,
                                "monthly_contribution": contrib, "target": target}
        s = f"I'm {age} and would like to retire at {target_age}. I have {money(bal)} in my retirement account"
        s += f" and put {money(contrib)} a month into it" if contrib else " but I'm not contributing right now"
        if target:
            s += f". I think I'll need about {money(target)}"
        self.sentences.append(s + ".")

    def add_savings(self):
        bal = nice(self.income * self.u(0.1, 3.0))
        self.p["current_savings_balance"] = bal
        self.sentences.append(self.rng.choice([f"I have about {money(bal)} in savings.", f"Right now I've got {money(bal)} saved."]))

    def text(self) -> str:
        rest = self.sentences[1:]
        for i in range(len(rest) - 1): 
            if self.rng.random() < 0.25:
                rest[i], rest[i + 1] = rest[i + 1], rest[i]
        return " ".join(self.sentences[:1] + rest)


# update events 
# Each event edits the profile in place and returns (message, probes), or None if it doesn't apply

def _income(p) -> float:
    return sum((i["amount"] or 0) * FACTOR.get(i["frequency"] or "monthly", 1) for i in p["income"])


def _find(p, lst, keywords):
    return next((i for i in p[lst] if any(k in (i.get("name") or i.get("source") or "").lower() for k in keywords)), None)


def ev_layoff(r, p, ts):
    old = _income(p)
    if old <= 0:
        return None
    benefit = nice(min(2400.0, old * r.uniform(0.3, 0.45)))
    p["income"] = [{"source": "Unemployment benefits", "amount": benefit, "frequency": "monthly", "is_net": True,
                    "effective_date": None}]
    text = r.choice([f"hey I got laid off today. I'll be getting about {money(benefit)} a month in unemployment for now.",
                     f"Bad news, I got laid off :( Unemployment will pay me around {money(benefit)} a month."])
    return text, [Probe(kind="scalar", path="derived.income_monthly", expected=benefit, stale=old)]


def ev_raise(r, p, ts):
    job = p["income"][0] if p["income"] else None
    if job is None or not job.get("is_net"):
        return None
    old = _income(p)
    job["amount"] = nice(job["amount"] * r.uniform(1.06, 1.18))
    eff = month_start(ts.date(), 1)
    job["effective_date"] = eff.isoformat()
    text = f"Got a raise! My take-home goes up to {money(job['amount'])} {FREQ_TEXT[job['frequency']][0]} starting {month_name(eff)}."
    return text, [Probe(kind="scalar", path="derived.income_monthly", expected=_income(p), stale=old)]


def ev_trip_goal(r, p, ts):
    dest = r.choice(["Iceland", "Mexico City", "Portugal", "Korea", "Peru", "Italy"])
    if _find(p, "savings_goals", [dest.lower()]):
        return None
    deadline = month_start(ts.date(), r.randint(6, 12))
    target = nice(r.uniform(1500, 5000))
    p["savings_goals"].append({"name": f"{dest} trip", "target_amount": target, "deadline": deadline.isoformat(),
                               "contribution": None, "priority": None, "effective_date": None})
    text = r.choice([f"I want to go to {dest} in {month_name(deadline)}! It'll cost about {money(target)} total.",
                     f"thinking about a trip to {dest} by {month_name(deadline)}, probably around {money(target)}"])
    return text, [Probe(kind="item_amount", in_list="savings_goals", match=[dest.lower()], field="target_amount",
                        expected=target)]


def ev_hobby_class(r, p, ts):
    key = r.choice(["pottery", "climbing", "dance", "guitar", "painting"])
    if _find(p, "recurring_expenses", [key]):
        return None
    weekly = nice(r.uniform(25, 70))
    p["recurring_expenses"].append({"name": f"{key.title()} class", "amount": weekly, "frequency": "weekly",
                                    "category": "entertainment", "need_or_want": "want", "is_fixed": True,
                                    "effective_date": None})
    return f"I started taking a {key} class, it's {money(weekly)} a week.", [
        Probe(kind="item_amount", in_list="recurring_expenses", match=[key], expected=weekly * 52 / 12),
        Probe(kind="item_amount", in_list="recurring_expenses", match=[key], field="need_or_want", expected_text="want")]


def ev_windfall(r, p, ts):
    old = p["current_savings_balance"] or 0.0
    amt = nice(r.uniform(5000, 30000))
    p["current_savings_balance"] = old + amt
    text = r.choice([f"inheritance money just hit lol, {money(amt)} went into my savings",
                     f"my grandma left me {money(amt)}, it's in my savings account now"])
    return text, [Probe(kind="scalar", path="current_savings_balance", expected=old + amt, stale=old)]


def ev_rent_increase(r, p, ts):
    rent = _find(p, "recurring_expenses", ["rent"])
    if rent is None:
        return None
    old = rent["amount"] * FACTOR[rent["frequency"]]
    eff = month_start(ts.date(), r.randint(1, 2))
    rent.update(amount=nice(old * r.uniform(1.05, 1.15)), frequency="monthly", effective_date=eff.isoformat())
    return (f"My landlord is raising the rent to {money(rent['amount'])} starting {month_name(eff)}.",
            [Probe(kind="item_amount", in_list="recurring_expenses", match=["rent"], expected=rent["amount"], stale=old)])


def ev_cancel_subscription(r, p, ts):
    for keywords, label in ((["stream"], "streaming services"), (["gym"], "gym membership")):
        item = _find(p, "recurring_expenses", keywords)
        if item is not None:
            p["recurring_expenses"].remove(item)
            return (f"I cancelled my {label}, so that's gone now.",
                    [Probe(kind="item_absent", in_list="recurring_expenses", match=keywords,
                           stale=item["amount"] * FACTOR[item["frequency"]])])
    return None


def ev_correction(r, p, ts):
    wants = [e for e in p["recurring_expenses"] if e["need_or_want"] == "want" and not e["is_fixed"]]
    if not wants:
        return None
    item = r.choice(wants)
    old = item["amount"]
    item["amount"] = nice(old * r.choice([0.6, 0.75, 1.3, 1.5]))
    if item["amount"] == old:
        return None
    f = FACTOR[item["frequency"]]
    keywords = next(v[4] for v in WANTS.values() if v[0] == item["name"])
    text = (f"Actually I was wrong earlier, {item['name'].lower()} is more like {money(item['amount'])} "
            f"{FREQ_TEXT[item['frequency']][0]}, not {money(old)}.")
    return text, [Probe(kind="item_amount", in_list="recurring_expenses", match=keywords, expected=item["amount"] * f,
                        stale=old * f)]


def ev_debt_payoff(r, p, ts):
    card = _find(p, "debts", ["credit"])
    if card is None:
        return None
    p["debts"].remove(card)
    return ("Finally paid off my credit card!! Used my bonus, so my savings didn't change.",
            [Probe(kind="item_absent", in_list="debts", match=["credit", "card"], stale=card["balance"])])


def ev_car_repair(r, p, ts):
    if _find(p, "one_time_expenses", ["repair"]):
        return None
    amt = nice(r.uniform(600, 2500))
    when = month_start(ts.date(), 1).replace(day=10)
    p["one_time_expenses"].append({"name": "Car repair", "amount": amt, "expected_date": when.isoformat(),
                                   "effective_date": None})
    return (f"ugh my car needs a {money(amt)} repair next month",
            [Probe(kind="item_amount", in_list="one_time_expenses", match=["repair", "car"], field="amount", expected=amt)])


EVENTS = {"layoff": ev_layoff, "raise": ev_raise, "trip_goal": ev_trip_goal, "hobby_class": ev_hobby_class,
          "windfall": ev_windfall, "rent_increase": ev_rent_increase, "cancel_subscription": ev_cancel_subscription,
          "correction": ev_correction, "debt_payoff": ev_debt_payoff, "car_repair": ev_car_repair}


def _clarify(r, p, missing):
    parts, probes = [], []
    if "income.is_net" in missing:
        for i in p["income"]:
            i["is_net"] = True
        parts.append("that's my take-home pay, after taxes")
    if "debts.apr" in missing:
        card = _find(p, "debts", ["credit"])
        card["apr"] = float(r.choice([21.0, 24.0, 27.0]))
        parts.append(f"the credit card APR is {card['apr']:g}%")
        probes.append(Probe(kind="item_amount", in_list="debts", match=["credit"], field="apr", expected=card["apr"]))
    return "Oh sorry, " + " and ".join(parts) + ".", probes


def _snapshot(p, ts, version):
    out = copy.deepcopy(p)
    out["meta"].update(as_of=ts.isoformat(), profile_version=version)
    FinancialProfile.model_validate(out)
    return out


def generate_scenario(arch: Archetype, index: int, seed: int, p_missing: float = 0.2) -> Scenario:
    rng = random.Random(f"{seed}:{arch.name}:{index}")
    ts = datetime(2026, 10, rng.randint(1, 28), rng.randint(8, 21))
    sid = f"syn-{arch.name}-{index:03d}"
    person = _Persona(rng, arch, f"u_{sid}", ts)
    unclear_net = arch.pay != "gross_annual" and rng.random() < p_missing / 2
    person.add_income(unclear_net)
    person.add_needs()
    person.add_wants()
    person.add_debts(hide_apr=not unclear_net and rng.random() < p_missing)
    person.add_goals()
    person.add_one_time()
    person.add_retirement()
    person.add_savings()

    p = person.p
    turns = [Turn(timestamp=ts, text=person.text(), event="initial",
                  expected_status="needs_clarification" if person.missing else "ready",
                  expected_missing=person.missing, expected_profile=_snapshot(p, ts, 1))]
    if person.missing:
        ts += timedelta(minutes=rng.randint(2, 30))
        text, probes = _clarify(rng, p, person.missing)
        turns.append(Turn(timestamp=ts, text=text, event="clarification", expected_profile=_snapshot(p, ts, 2),
                          probes=probes))

    n_updates = rng.choice([0, 1, 1, 2, 2, 3, 4])
    names = list(EVENTS)
    rng.shuffle(names)
    used = []
    for name in names:
        if len(used) == n_updates:
            break
        next_ts = ts + timedelta(days=rng.randint(2, 20), hours=rng.randint(0, 8))
        result = EVENTS[name](rng, p, next_ts)
        if result is None:
            continue
        ts = next_ts
        used.append(name)
        turns.append(Turn(timestamp=ts, text=result[0], event=name, expected_profile=_snapshot(p, ts, len(turns) + 1),
                          probes=result[1]))

    tags = [arch.name, *(f"event:{e}" for e in used)] + (["needs_clarification"] if person.missing else [])
    return Scenario(id=sid, source="synthetic", archetype=arch.name, tags=tags, description=arch.description,
                    seed=seed, turns=turns)


def generate(n_per_archetype: int = 6, seed: int = 7, archetypes: Optional[list[str]] = None,
             p_missing: float = 0.2) -> list[Scenario]:
    return [generate_scenario(ARCHETYPES[a], i, seed, p_missing)
            for a in (archetypes or ARCHETYPES) for i in range(n_per_archetype)]
