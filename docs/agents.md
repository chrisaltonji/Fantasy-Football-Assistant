# The agents

Four agents, one assistant. They share one cached prefix, one transport, and one
memory. This describes what is built — for the LangGraph topology that was
considered and declined, see `docs/declined/`.

## The roles

| Agent | Fires on | Model | Produces | Calls/draft |
|---|---|---|---|---|
| **The Room** (open) | a player is nominated | Sonnet 5, low effort, no thinking | rival bid bands + a read on the nomination | ~180 |
| **The Room** (tick) | every ~5s while on the block | Haiku 4.5, no thinking | a revision of its own opening read | ~900 |
| **The Strategist** | a plan-state transition | Sonnet 5, medium | assessment of the declared plan, and the digest | ~25 |
| **The Narrator** | a notable feed event | Sonnet 5, low | one or two sentences on why it matters | ~33 |
| **The Analyst** | on demand (`ask`) | Opus 5, high | free prose answer | ad hoc |

**Three tiers, and every boundary is a measurement.** Latency here is output size:
15.9 ms per output token on Sonnet 5, 18.0 on Opus 5, near-constant on both. The
three agents that run against the draft clock share the middle tier *and one warm
cache entry* — caching is keyed on the model, so splitting them buys a second
prefix write for nothing. The Analyst stays on Opus because it is invoked on
purpose, with a person waiting and no clock to race; it is alone there, so its
entry goes cold between questions and it pays a prefix write on most calls, which
is the right trade for an agent asked a handful of times.

The Grader (post-draft grading of the roster and of the assistant's own calls) is out
of scope. Its prompt, schema and `grader_payload` remain in the tree, unused.

## The Room has two gears

One agent, one module, one voice, one guard — two triggers.

The **open** read fires once, at nomination, on the full board. It is the deep read:
where each live rival plausibly stops, and what is worth noticing.

The **tick** fires on a fixed ~5s cadence while the player is still up. It is a
*revision of the opening read*, not a fresh analysis — it receives the current price,
who is still live, the bands the open read gave, and the digest. That framing is what
makes a small fast model adequate: it is not being asked to think the problem through
again, only to say what the last twenty seconds changed.

A tick renders nothing unless something material moved. Silence is the default,
because a line every five seconds during live bidding is unreadable.

## Shared memory

Two layers, both in `runs/<draft_id>/assist.jsonl` via `assist/context.py`:

- **Every reply.** `ReadLog.latest(agent, player_key)` and `.for_player()` let a later
  agent read an earlier one. The Narrator cites the Room's estimate; the tick reads its
  own opening band.
- **The digest.** A short running summary of where the draft stands, written by the
  Strategist (~25 times a draft) and read by everyone. One writer, low frequency.

The digest travels in the **volatile user block**, never in the cached prefix. Putting
it in the prefix would invalidate the cache on every sale.

**This is not the journal, and its address is a trap.** It sits beside `events.jsonl`
because that is where the draft lives. `events.jsonl` is ground truth: fsynced,
replayed, the source of every number the tool asserts. `assist.jsonl` is opinions.
Nothing in `state/`, `domain/` or `advice/` may read it, and a missing or corrupt
sidecar is a shrug.

## Live bids are ephemeral

`current_bid` is scraped from the draft room every 2s. It reaches the tick through a
`BID` message on the REPL inbox and updates one in-memory value.

**It never enters the journal.** There are five event types and adding a sixth for a
2s poll would put tens of thousands of lines of derived observation into an fsynced
append-only file that undo and crash-resume replay. A draft run with `--assist` and
one without must produce byte-identical `events.jsonl`.

## Two model tiers, and the tick reads no prefix at all

Every agent but the tick shares one model, so they share one cache entry. The
tick runs on Haiku 4.5 because Opus is disqualified on latency: an 11-14s read
cannot follow a live auction, and no effort setting closes that.

**It carries its own small system prompt rather than the shared prefix, and that
is a measured decision.** The minimum cacheable prefix is per model and is *not*
monotonic across generations:

| Model | Floor |
|---|---|
| Claude Opus 5 | 512 tokens |
| Claude Sonnet 5, Opus 4.8 | 1,024 |
| Claude Haiku 4.5 | **4,096** |

The shared prefix is ~3,400 tokens. It caches perfectly on Opus 5 and **silently
not at all** on Haiku 4.5 - no error, no warning, just
`cache_creation_input_tokens: 0` and full price on every call. The first live
rehearsal measured 15 ticks paying for 82,593 uncached tokens.

So the tick gets `TICK_PREAMBLE`: the two rules it must not break and the four
terms its payload actually contains, ~400 tokens, uncached because at that size
a breakpoint buys nothing. It does not need the dossiers - it is revising a read
that already used them, and `opening_read` carries those conclusions with their
citations. Input per call fell 5,431 to 2,880.

`prefix.MIN_CACHEABLE_BY_MODEL` holds the floors and defaults to the highest
rather than the lowest, `prompts.STANDALONE` names the agents that skip the
prefix, and `session.banner()` names the floor it checked against.

Haiku 4.5 also rejects `output_config.effort` and does not take adaptive
thinking, so `AgentProfile` carries both as optional and `client.complete` omits
them when unset.

## What it costs

Measured over `ffa sim --assist`, not estimated:

| | per call | latency p50 | per draft |
|---|---|---|---|
| The Room, open | $0.0227 | 7.6s | ~$4.09 @ 180 |
| The Room, tick | $0.0028 | 0.9s | ~$2.52 @ ~900 |
| Strategist + Narrator | ~$0.047 | ~10s | ~$2.75 |
| | | | **~$9.34** against a $20 cap |

Every number measured over `ffa sim --assist`, not estimated. The Room came down
from $0.0398 and 12.7s on Opus 5 with adaptive thinking, in three steps: the tier
(-$0.013, -1.7s), four rivals instead of six (-0.6s), and thinking off (-2.8s and
the variance with it).

**The ledger counts micro-dollars, not cents.** Whole cents are right while the
cheapest agent costs 4c and wrong the moment one costs 0.29c - and that agent is
also the most numerous. Rounding each tick up to a penny over-reported by 3.4x
and muted the assistant at two thirds of the budget it was given. `cost_cents`
survives for the one line a person reads; nothing accumulates in it.

## The invariants

These survive from the declined design and are still binding.

1. **Nothing here writes.** Workers build a `ReadRecord` and return it; `settle()` on
   the main thread commits it. One writer, no locks — the same argument that makes
   `DraftStore` safe.
2. **Nothing here blocks the draft.** The deterministic readout prints first and is
   never delayed. Calls drop rather than queue when slots are full.
3. **Arithmetic never comes from a model.** `assist/guard.py` enforces it mechanically
   after parsing, because a system prompt is a request and not a constraint.
4. **Every failure is an `AssistError`** — one sentence, never a traceback over a live
   auction. Three in a row mutes.
5. **`--assist` off leaves the draft byte-identical.** Asserted by test.
6. **The cached prefix is byte-stable.** One varying byte silently zeroes the hit rate,
   and nothing in the output says it happened.

## The parts bin

Every agent module is thin because these already exist and are pure:

```
projection.room_payload(view, *, prior_reads=None, digest="")
projection.room_tick_payload(view, *, live_bid, opening_read, digest="")
projection.strategist_payload(view, *, sale=None, reconciled=None, plan_state=None, digest="")
projection.narrator_payload(view, *, entry, reconciled=None, prior_read=None, digest="")
projection.analyst_payload(view, *, question, prior_reads=None, digest="")

guard.check_room(parsed, threats)             -> (cleaned, violations)
guard.check_strategist(parsed, plan_state)    -> (cleaned, violations)
guard.check_narrator(parsed)                  -> (cleaned, violations)
guard.check_analyst(parsed)                   -> (cleaned, violations)
guard.clamp_rivals(rivals, threats)           -> (kept, dropped)
guard.verdict_matches(echoed, deterministic)  -> bool
guard.reject_verdict(text)                    -> str | None

reconcile.compare(read, *, price, winner_team_id) -> dict
prompts.system_blocks(prefix, agent)  -> list    # cache breakpoint after block 0
schemas.schema_for(agent)             -> dict | None
client.complete(agent, system, user, *, schema) -> Reply
```

An agent module supplies only what genuinely differs: which payload, which guard, how
it renders, and when it fires.
