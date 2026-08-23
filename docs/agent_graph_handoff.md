# Agent graph design — handoff brief

**What I want from this conversation:** help me design the LangGraph topology for
a five-agent system inside a live fantasy football auction draft assistant. The
agents are built and measured; the orchestration is being migrated to LangGraph.
I want to work through the graph shape, and there are four open questions at the
bottom.

This brief is self-contained. You do not have the repository — everything you
need is here.

---

## 1. The domain, briefly

A **fantasy football auction draft**. Twelve people in a room (or on a video
call). Each has **$200** and must fill **15 roster slots**. Players are nominated
one at a time and bid on openly until the gavel; whoever bids highest pays that
price and rosters that player. It runs about three hours, roughly **180
nominations**, and it is **irreversible** — you get one evening a year.

Two consequences shape everything:

- **A nomination window is seconds to a minute.** Advice that arrives after the
  gavel is worthless as advice.
- **Money is conserved and visible.** Everything about budgets, ceilings and
  roster needs is *arithmetic over what has already happened*, not estimation.

Draft day is **2026-08-31**.

## 2. The architecture the agents live in

A Python CLI. The spine is an **append-only JSONL event journal** — every
nomination, bid and sale is an event; state is a fold over the journal; undo is a
replay filter. The journal is the only source of truth.

The central rule of the codebase, which drives the whole agent design:

> **Arithmetic and inference are never blurred.**

Everything the tool asserts as a number is computed from the journal and is
exact. Anything a language model produces is judgement, is labelled as such, and
can never become a number. There is a three-way provenance split documented in
the code: *tonight's arithmetic* (computed, exact), *history* (the same
arithmetic over previous drafts — a fact about the past and a bet about tonight),
and *dossiers* (recorded human testimony — somebody's opinion, sometimes wrong).

**Concurrency model:** one main thread owns all writes. A tagged inbox queue
receives from the keyboard, from a browser reader watching the live draft room,
and from agent workers. Daemon threads do slow work and post results back; they
never touch state. There are no locks anywhere, by construction.

**The view:** one function `build_view(state, ...)` produces the payload every
surface reads — ~15,200 tokens of board state: our team, eleven rivals, per-player
guidance, positional scarcity, market inflation, the plan, a nomination schedule.

## 3. The five agents

| Agent | Fires on | Produces | Calls/draft | Status |
|---|---|---|---|---|
| **The Room** | a player is nominated | rival bid estimates + a read on the nomination | ~180 | **built, measured** |
| **The Strategist** | a pick lands, *gated on a plan-state transition* | assessment of the declared plan | ~25 | not built |
| **The Narrator** | a notable feed event | one or two sentences on why it matters | ~33 | not built |
| **The Analyst** | on demand, user types `ask` | free prose answer to any question | ad hoc | not built |
| **The Grader** | after the draft, `ffa grade` | grades the roster *and the assistant's own calls* | 1 | not built |

Notes that matter for the design:

- **The Room carries two surfaces on one call** because they fire at the same
  instant on the same inputs. Splitting them would double latency under a clock
  and let the halves contradict each other.
- **The Strategist is deliberately separate from the Room** because it is
  *post-pick assessment*, not live pick assistance. Different moment, different
  question.
- **The Narrator reads the Room's earlier estimate.** "He went $47, eight over
  what we expected." That cross-agent read is why a shared context store exists.
- **The Grader scores the assistant against outcomes.** Every read is joined to
  what actually happened, deterministically, before the Grader sees it.

### The shared context store

A sidecar file `assist.jsonl` beside the journal. One record per agent call:

```
{seq, at, agent, moment, event_id, player_key, status, payload, detail, usage, model}
```

`status` ∈ `ok | failed | skipped | late | rejected | muted`. `event_id` is the
journal id at request time — the join back to ground truth.

**It is explicitly not the journal.** A missing, truncated or corrupt sidecar is a
shrug; the draft replays byte-identically without it. Losing inference costs
nothing; losing the journal is the end of the world.

## 4. What is already built and measured

The Room runs end to end against the real API. Measured over a full simulated
180-nomination draft:

| | |
|---|---|
| Latency | **12–14s** per read (`low` effort; `medium` is 18–24s) |
| Cache hit rate | **100%** |
| Cost | **4.0c/call → ~$7.20** for 180 nominations |
| Payload | ~3,900 tokens (down from 6,075 after removing duplication) |
| Cached prefix | 3,782 tokens, byte-stable, 1h TTL |

Per-agent measured latency and cost (three runs each, real API):

| Agent | Effort | p50 latency | in | out | cost |
|---|---|---|---|---|---|
| room | low | 9.6s | 3,931 | 511 | 5c |
| strategist | medium | 17.6s | 4,989 | 950 | 6c |
| narrator | low | 3.7s | 447 | 100 | 2c |
| analyst | high | 22.4s | **19,853** | 1,384 | **15c** |
| grader | high | — | — | — | truncated at 2k, now 8k |

**The Analyst is the outlier and the reason the graph is interesting.** It costs
15c because it receives the entire board to answer one question.

### Prompt structure (shared by all five agents)

Two system blocks, always in this order:

```
[0] the cached prefix  — contract + glossary + 12 manager dossiers   <- cache breakpoint
[1] this agent's instructions                                        <- after the breakpoint
```

All five share a **byte-identical block 0**, so they share one cache entry rather
than five. The prefix is built from loaded sources and never from `build_view()`,
which stamps a timestamp — one varying byte silently zeroes the hit rate.

The glossary is the highest-leverage part: it states exactly what
`max_legal_bid`, `safe_legal_bid`, `is_live` and a trailing `+` mean, so the model
does not reinvent definitions and does not treat a hard ceiling as a suggestion.

### The guard

Prompting cannot enforce a property, so there is a mechanical guard on every
reply:

- `reject_verdict(text)` — anchored patterns. Must fire on "Bid up to $40", must
  **not** fire on "a pass-catching back" or "the room passed".
- `clamp_rivals(rivals, threats)` — drops any estimate whose top exceeds that
  rival's hard legal ceiling, or whose team is not live on this player.
  **Drop and log, never rewrite** — a corrected estimate is a fabrication.
- `verdict_matches(echoed, computed)` — discards the Strategist entirely if the
  plan state it echoed disagrees with the arithmetic.

Rejected replies are **recorded and not shown**. How often it happens is exactly
what you would want to know before trusting any of it.

## 5. Decisions already made

| | |
|---|---|
| **Depth** | LangGraph goes *inside* the existing worker thread. The daemon-thread transport, supersede, mute and spend cap all stay. Only the call body becomes `graph.invoke()`. |
| **Model layer** | Keep the raw Anthropic SDK client, not `ChatAnthropic`. It holds measured tuning: per-agent effort, adaptive thinking, `output_config` schemas, 1h cache TTL, typed failure translation. LangGraph does not require LangChain models. |
| **Observability** | LangSmith, via `@traceable`. |
| **State** | Per-invocation, not per-draft. No checkpointer. Each invocation builds fresh state from a frozen view snapshot plus prior reads. |
| **The Room** | Goes through the graph too — one system to build and rehearse. |
| **Timing** | Migrate before building agents 2–5, so nothing is built twice. |

## 6. Invariants the graph must not break

1. **The graph never writes.** It runs on a daemon worker. The terminal node
   *builds* a record and returns it; the main thread commits it. One writer, no
   locks.
2. **The graph never blocks the draft.** The deterministic readout — ceilings,
   threats, scarcity — prints in milliseconds and is never delayed. A model read
   can only ever arrive *after* it and add to it.
3. **Arithmetic never comes from a model.** Guard nodes enforce this. I want them
   to be *visible nodes* in the diagram, not folded into the LLM node — the
   constraint belongs in the picture.
4. **Every failure becomes one sentence.** No library traceback ever prints over a
   live auction. Failures split into retryable (timeout, overloaded) and fatal
   (bad key, rejected schema); three consecutive failures mute the assistant with
   one line saying the draft is unaffected.
5. **A read that arrives after the player sold is recorded, not printed.** It is
   noise on screen and evidence in the ledger — the Grader wants "we said $39, he
   went $47".
6. **With the assistant off, the draft is byte-identical.** Asserted by test.

## 7. The parts bin — every node body already exists and is pure

These are all pure functions, no I/O, no SDK. They are the intended node bodies.

```python
# projections — what each agent is allowed to see
room_payload(view, *, prior_reads=None) -> dict
strategist_payload(view, *, sale=None, reconciled=None, plan_state=None) -> dict
narrator_payload(view, *, entry, reconciled=None, prior_read=None) -> dict
analyst_payload(view, *, question, prior_reads=None) -> dict
grader_payload(view, *, reads, feed=None) -> dict

# the guard
check_room(parsed, threats)            -> (cleaned, violations)
clamp_rivals(rivals, threats)          -> (kept, dropped)
verdict_matches(echoed, computed)      -> bool
reject_verdict(text)                   -> str | None

# read vs outcome, computed rather than asked
compare(read, *, price, winner_team_id) -> dict
    # returns expected_lo/hi, delta, band (under|inside|over), winner_was_named

# prompting and the model
system_blocks(prefix, agent)  -> list      # cache breakpoint after block 0
user_text(payload)            -> str
schema_for(agent)             -> dict | None    # None for the Analyst (prose)
complete(agent, system, user, *, schema=None, effort=None, timeout=None) -> Reply
    # model / effort / timeout / max_tokens all default per agent
cost_cents(usage, model)      -> int

# helpers
threats_of(view)                  -> list   # what the guard clamps against
moment_key(player_key, event_id)  -> str    # join key later agents use
render(parsed, *, labels=None)    -> str    # terminal form, "" if nothing survived

# reading the shared store (main thread)
ReadLog.latest(agent, player_key="")     -> ReadRecord | None   # skips non-ok
ReadLog.for_player(player_key, agent="") -> tuple[ReadRecord, ...]
```

Fields available to put in graph state:

`prefix` · `labels` · `trigger` · `view` (frozen snapshot) · `event_id` ·
`player_key` · `question` · `sale` · `entry` · `plan_state` · `prior_reads` ·
`threats`

## 8. The current sketch — react to this, don't defer to it

```
                          START
                            │
                     route(trigger)
        ┌───────────┬───────┴───────┬──────────────┐
        ▼           ▼               ▼              ▼
   NOMINATION     SALE          QUESTION       FINISHED
        │           │               │              │
   project_room  reconcile      ┌──►analyst    grade_roster
        │         (pure)        │    │              │
      room ◄─LLM    │           │  [tools?]     score_calls ──map──► per read
        │       ┌───┴───┐       └────┘              │
   guard_room   ▼       ▼            │          synthesize
        │  strategist  narrator      │              │
        │   (gated)     (LLM)        │              │
        │       └───┬───┘            │              │
        └───────────┴────────────────┴──────────────┘
                            ▼
                        finalize      ← builds a record, writes nothing
                            │
                           END
                            ⇣
              main thread commits: ledger, supersede, mute, spend
```

Honest assessment of where a graph actually earns its keep here:

| Branch | Value | Why |
|---|---|---|
| Nomination | **Low** | Linear four nodes. Gains spans and consistency, nothing structural |
| Sale | **High** | Genuine fan-out/join, and a genuine conditional edge (Strategist gating) |
| Question | **Highest** | A tool loop is a real cycle, and it is the 15c agent |
| Finished | **High** | `score_calls` is a real map over N reads; otherwise one overloaded prompt |

## 9. Open questions I want to work through

**Q1 — Composition.** Parent graph with a router plus four compiled subgraphs
(narrow per-branch state, nested rendering, each testable standalone)? Or one flat
graph with a single state schema unioning all four triggers' fields (simpler
wiring, wider state, no isolated testing)? Or something else — this feels like a
false binary and I would rather be talked out of both.

**Q2 — Analyst tools.** The biggest win available. Options: read-only pure
functions over the frozen view (`get_team`, `get_position`, `get_player`,
`get_plan`, `get_recent_sales`); or that plus history and dossier lookup so it can
answer "has this manager ever paid this much for a receiver?"; or no tools at all.
What is the right tool granularity so the loop converges quickly under a person
waiting at a prompt?

**Q3 — Strategist gating.** It should only fire on a plan-state *transition*
(on track → at risk → time to move), on our own pick, or every 30 sales. The
transition test is already computed deterministically. Conditional edge, or a node
that returns early? Which reads better in a rendered diagram?

**Q4 — Where the guard sits.** I want guards as visible nodes. But does each agent
get its own guard node, or is there one shared guard node that dispatches on
agent? The first is clearer in the picture; the second is less duplication.

**Bonus:** is there a fifth trigger I am missing? The current four are nomination,
sale, question, finished. Nothing fires on *inaction* — e.g. a position quietly
drying up while nobody nominates it, or our budget pace drifting. Should there be
a periodic or threshold-triggered branch, and would that fit this shape?

## 10. Constraints worth knowing

- **Python 3.14.3.** LangGraph 1.2.11 declares `requires-python >=3.10` but its
  classifiers stop at 3.13. Unverified on 3.14 — first thing to check.
- **10 days to draft day.** The Room works today; anything that risks it needs a
  kill switch, and there is one (`--assist` is off by default).
- **The repo is public.** No real identifiers in committed code.
- **Spend cap $25/draft**, per draft rather than per call — a per-call target
  invites tuning one agent down while another runs away.
