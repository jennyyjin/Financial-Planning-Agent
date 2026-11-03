You're grading advice from a personal finance assistant. Grade only the dimension below; the others are graded separately. Friendly wording doesn't make vague advice actionable, and specific advice isn't automatically feasible.

You'll see the user's messages (oldest first, later ones override earlier ones), the key facts about their finances, which are correct, and the advice.

Go through each checklist item and answer yes, no, or na (na only where the item allows it). Back each answer with a short quote, or say what's missing. Then give a score from 1 to 5. If a critical item is no, the score can't be higher than 2. Don't reward length, and grade what's written rather than what the assistant probably meant.

Reply with only this JSON:
{"checklist": {"<id>": {"answer": "yes|no|na", "evidence": "..."}}, "reasoning": "2-3 sentences", "score": 1-5}

## Actionability

Could the person start on this today, knowing what to do, how much, and in what order?

A1 (critical): Each change is a concrete action, like "cut eating out from $520 to $300 a month", not a general aim like "spend less".
A2: Amounts or targets are given where they matter.
A3: It refers to the person's own expenses, goals or debts.
A4: It's clear when to start or what to do first.
A5: The most important issue (biggest gap, risk or costliest debt) comes first.
A6: Each change says what it will achieve.
A7: No two changes contradict or repeat each other.

5: every change is specific, quantified and in a sensible order, with a stated result.
4: mostly concrete, with one small gap.
3: a mix of concrete and generic changes.
2: mostly general aims with few numbers.
1: nothing to act on.

## Feasibility

If they followed this as written, would it work with their real income, costs, debts and goals?

F1 (critical): It doesn't tell them to cut or skip essentials (housing, utilities, insurance, healthcare, childcare, minimum debt payments, or an essential one-time cost like a medical bill). Suggesting they shop around is fine.
F2 (critical): What it asks them to save or pay fits the money they have.
F3: Cuts to wants are a size someone could stick to, and it says what they give up.
F4 (critical): It doesn't get their facts wrong, like calling an off-track goal on track or inventing an expense.
F5: If a goal or retirement target can't be met, it says so and offers a realistic path. na if everything is on track.
F6: It handles high-interest debt and the emergency fund sensibly, for example not investing extra while carrying a 24% card. na if neither applies.
F7: It reflects the latest messages, for example not relying on a salary they just lost. na if there's no update.

5: everything could be done as written.
4: workable, with one stretch.
3: one real problem, like an unrealistic cut or a glossed-over goal.
2: big parts wouldn't work.
1: harmful or impossible.

## Tone

Does it sound like a knowledgeable, kind friend: respectful, honest, calm and clear?

T1 (critical): No shaming or lecturing about their spending.
T2: Problems are stated plainly, not sugar-coated.
T3: It stays calm and doesn't catastrophise or pressure them.
T4: It notes what's going well, without empty praise.
T5: It's easy to read, with no unexplained jargon.
T6 (critical): Suggestions are offered as options. It never promises investment returns or names specific funds or tickers.
T7: If they sound stressed or mention a life event, it acknowledges that. na if not.
T8: It reads as written for this person, not boilerplate.

5: warm, honest, calm and clear throughout.
4: good, with one small issue.
3: cold, generic, preachy or alarmist in places.
2: judgmental or alarmist in several places.
1: shaming, frightening, or it promises returns.
