# Vera Bot — Submission

## Approach

A deterministic, rule-based composer (no LLM call at runtime) dispatched by
`trigger.kind`. There are 24 dedicated handlers, one per trigger kind seen in
the dataset (`research_digest`, `perf_spike`, `perf_dip`, `recall_due`,
`wedding_package_followup`, `supply_alert`, etc.), plus customer-scoped
handlers that switch `send_as` to `merchant_on_behalf`.

Every handler reads facts **only** from the pushed context (category digest
items, merchant performance/offers/signals, trigger payload, customer
identity/relationship) and returns `None` — i.e. the bot chooses not to send
— whenever it can't find a real, verifiable anchor for that trigger. This
directly targets the "don't fabricate" constraint and the "restraint is
rewarded" scoring note.

`/v1/reply` handles three special conversational modes ahead of the generic
fallback:
1. **Hostility** — regex-detected → one short apology + opt-out, then silence.
2. **Auto-reply detection** — canned-phrase regex, with a streak counter kept
   *per merchant_id* (not per conversation_id, since the judge/simulator can
   route repeats through different conversation ids) → nudge once, wait 24h,
   then end after the third repeat, matching the phase-4 replay spec exactly.
3. **Intent transition** — explicit commitment phrases ("let's do it", "go
   ahead", "confirm") immediately switch from pitch to action mode with a
   concrete next step, never another qualifying question.

## Trade-offs

- No LLM in the loop: this guarantees determinism, sub-second latency, zero
  API cost, and zero risk of hallucinated facts — at the cost of less
  linguistic variety than a well-prompted LLM composer would produce.
- Hindi-English code-mix is applied lightly (a couple of stock phrases) based
  on `identity.languages` containing `"hi"`, rather than full bilingual
  generation.
- Trigger kinds not seen in the provided dataset will simply be skipped
  (`compose()` returns `None`) rather than guessed at.

## What additional context would have helped most

- A confirmed mapping from `trigger.kind` → expected payload keys (some are
  inferred from the sample seed data rather than documented explicitly).
- Real (non-placeholder) payloads for the generator's extra 75 triggers,
  which currently ship with `{"placeholder": true}` and are therefore
  correctly skipped rather than acted on.
