# The agents

Four agents, one assistant. They share one cached prefix, one transport, and one
memory. This describes what is built — for the LangGraph topology that was
considered and declined, see `docs/declined/`.

## The roles

| Agent | Fires on | Model | Produces | Calls/draft |
|---|---|---|---|---|
| **The Room** (open) | a player is nominated | Opus 5, low effort | rival bid bands + a read on the nomination | ~180 |
| **The Room** (tick) | every ~5s while on the block | Haiku 4.5, no thinking | a revision of its own opening read | ~900 |
| **The Strategist** | a pick lands, gated on a plan-state transition | Opus 5, medium | assessment of the declared plan, and the digest | ~25 |
| **The Narrator** | a notable feed event | Opus 5, low | one or two sentences on why it matters | ~33 |
| **The Analyst** | on demand (`ask`) | Opus 5, high | free prose answer | ad hoc |

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

## Two model tiers, on purpose

The rest of the agents share one model so they share one cache entry. The tick does
not, and the reasoning that put them together is exactly why:

> Caching is keyed on the model, so a second model cannot share the prefix the others
> read — it pays its own cache write, more than once as the TTL lapses.

That holds for an agent firing 33 times. It does not hold for one firing every five
seconds: the tick keeps its own entry warm continuously and the 1h TTL never lapses.
It writes the prefix a handful of times across a three-hour auction and reads it
~900 times.

Haiku 4.5 rejects `output_config.effort` and does not take adaptive thinking, so
`AgentProfile` carries both as optional and `client.complete` omits them when unset.

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
