> # DECLINED — 2026-08-27
>
> This plan was never implemented and will not be. It proposed replacing ~359 lines
> of working, rehearsed orchestration (`assist/runner.py`, `assist/session.py`) with
> LangGraph + LangSmith. Three reasons it was dropped:
>
> - **It replaces the half that works.** The transport was verified live over a full
>   simulated draft. The half that does *not* work is that four of five agents had no
>   modules at all — a framework migration does not write them.
> - **It never cleared its own Step 0.** LangGraph 1.2.11 classifies up to Python
>   3.13; this machine runs 3.14.3. The blocker sat unverified.
> - **It put real people on someone else's cloud.** Full LangSmith traces include the
>   dossiers — written testimony about twelve named managers — for the stated benefit
>   of a `draw_mermaid()` diagram.
>
> What was worth keeping — the parts bin and the six invariants — moved to
> `docs/agents.md`, which describes what was actually built. Those invariants are
> still binding.

# The inference layer, on LangGraph — DRAFT

> **Status: draft, blocked on the graph topology.**
> Everything below is decided except the shape of the graph itself, which Chris
> is drafting. `docs/agent_graph_handoff.md` is the self-contained brief for that
> design conversation.

## Context

Capabilities 1, 3–10 and 12 ship. The inference layer is 20/30 modules: the pure
foundation, the client, and **The Room — verified live over a full simulated
draft at 12–14s per read, 100% cache hits, 4.0c/call, ~$7.20 for 180
nominations.** Steps 4 (surfaces), 5 (four more agents) and 6 (rehearsal) remain.
Draft day is **2026-08-31**.

Earlier in this build LangGraph was considered and declined, on the argument that
the agents fire on independent external triggers and so do not form a graph. That
argument was too narrow. Three things it missed:

- **The sale and grade flows really are graphs.** Reconcile fans out to the
  Strategist and Narrator and joins; the Grader is roster → per-read scoring →
  synthesis. Both are hand-coded control flow today.
- **The Analyst is a cycle waiting to happen.** It is the most expensive agent by
  far — 15c/call — because it receives the entire 15,200-token board to answer one
  question. As a tool loop it fetches only what it needs.
- **`assist/` is already 84% graph-shaped.** 1,872 of 2,231 lines are pure
  functions; only `runner.py` (263) and `session.py` (96) know about threads or
  queues. The pure modules *are* the node bodies. This migration replaces ~359
  lines of hand-rolled orchestration, not the inference layer.

Two further motives are legitimate and worth naming: the agent org will keep
growing, and a live `draw_mermaid()` of it is the right artefact for a
model-based thinker.

## Decisions taken

| | |
|---|---|
| **Timing** | After step 4, before step 5. Step 4 is surface work orthogonal to orchestration; step 5 is entirely orchestration. Nothing gets built twice. |
| **Depth** | Graph *inside* the worker. `runner.py`'s daemon-thread transport, supersede, mute and spend cap all stay. Only the call body becomes `graph.invoke()`. |
| **Model layer** | Keep `client.py`. LangGraph does not require LangChain models, and `client.py` holds the measured tuning: per-agent effort, adaptive thinking, `output_config` schemas, 1h cache TTL, every typed failure translated to one sentence. |
| **Observability** | LangSmith. `@traceable` instruments plain functions — no LangChain dependency implied. |
| **Trace contents** | Full traces, dossiers included. Raised and decided: manager nicknames and written testimony about real people will sit on LangChain's cloud. Reversible at any time by one env var or one `hide_inputs` callable; noted so the choice stays visible rather than becoming a default. |
| **The Room** | Goes through the graph too. One system to rehearse, ~8 days to do it, same pure functions underneath, `--assist` still a kill switch. |
| **Topology** | **Chris drafts it.** See the design inputs below. |
| **Composition** | Open — subgraphs vs. flat falls out of the topology once drawn. |

## Step 0 — the blocker to clear first

LangGraph 1.2.11 declares `requires-python >=3.10`, but its classifiers stop at
**3.13**. This machine runs **3.14.3**. It will probably install; it is not
claimed to work.

Verify before anything else, in a throwaway venv — never the working environment:

```
py -3.14 -m venv /tmp/lgcheck
/tmp/lgcheck/Scripts/pip install langgraph langsmith anthropic
/tmp/lgcheck/Scripts/python -c "from langgraph.graph import StateGraph; print('ok')"
```

It pulls `langchain-core`, `langgraph-checkpoint`, `langgraph-prebuilt`,
`langgraph-sdk`, `pydantic`, `xxhash`. If 3.14 fails, stop and re-decide: a 3.13
venv for the whole project is possible but is its own migration, 10 days out.

## Step 4 first — surfaces (unchanged, do now)

Orthogonal to orchestration and needed either way.

- `assist` key in `build_view()` — one top-level key, **absent entirely when
  off**, so today's payload stays byte-identical. Asserted by test. `view/model.py`
  gains a fourth provenance entry in the same voice: *nothing measured and nothing
  recorded*.
- Dashboard reads the sidecar. It stays a pure reader and makes no API calls.
- The "highest risk to me" panel un-pends.

## Design inputs — for drafting the topology

### The four triggers

| Trigger | Fires on | Today |
|---|---|---|
| `nomination` | `PlayerNominated` | The Room. Built, measured. |
| `sale` | `PlayerSold` + a notable feed entry | Reconcile → Strategist (gated) + Narrator |
| `question` | Terminal `ask` | Analyst |
| `finished` | `ffa grade`, after the fact | Grader |

### The parts bin — every node body already exists and is pure

```
projection.room_payload(view, *, prior_reads=None) -> dict
projection.strategist_payload(view, *, sale=None, reconciled=None, plan_state=None) -> dict
projection.narrator_payload(view, *, entry, reconciled=None, prior_read=None) -> dict
projection.analyst_payload(view, *, question, prior_reads=None) -> dict
projection.grader_payload(view, *, reads, feed=None) -> dict

guard.check_room(parsed, threats)          -> (cleaned, violations)
guard.clamp_rivals(rivals, threats)        -> (kept, dropped)
guard.verdict_matches(echoed, deterministic) -> bool
guard.reject_verdict(text)                 -> str | None

reconcile.compare(read, *, price, winner_team_id) -> dict

prompts.system_blocks(prefix, agent) -> list      # cache breakpoint after block 0
prompts.user_text(payload)           -> str
schemas.schema_for(agent)            -> dict | None   # None for the Analyst
client.complete(agent, system, user, *, schema, effort=None, timeout=None) -> Reply
                                                  # model/effort/timeout/max_tokens
                                                  # default from PROFILES[agent]
budget.cost_cents(usage, model) -> int

room.threats_of(view)                -> list      # what the guard clamps against
room.moment_key(player_key, event_id) -> str      # the join key later agents use
room.render(parsed, *, labels=None)  -> str       # terminal form, "" if nothing survived
```

Read-side, available to build state from:

```
ReadLog.latest(agent, player_key="")    -> ReadRecord | None   # skips non-ok records
ReadLog.for_player(player_key, agent="") -> tuple[ReadRecord, ...]
```

### Invariants the graph must not break

1. **The graph never writes.** It runs on a daemon worker. The terminal node
   *builds* a `ReadRecord` and returns it; `runner.settle()` on the main thread
   commits it. One writer, no locks — the same argument that makes `DraftStore`
   safe.
2. **The graph never blocks the draft.** It lives inside `runner.submit()`, which
   already returns immediately and drops rather than queues when slots are full.
3. **Arithmetic never comes from a model.** Guard nodes are the enforcement, and
   they should be *visible nodes* rather than folded into the LLM node — the
   constraint belongs in the picture.
4. **Every failure is an `AssistError`.** No traceback ever reaches a live auction.
5. **`--assist` off leaves the draft byte-identical.** Journal-identity test.
6. **The cached prefix stays byte-stable.** It is system block 0 for all five
   agents and one varying byte silently zeroes the hit rate.

### State, and why it is per-invocation

Graph-inside-worker with no checkpointer means each `graph.invoke()` builds fresh
state from a frozen view snapshot plus prior reads read out of `ReadLog`. Nothing
accumulates across nominations inside the graph, so no reducer has to be
draft-lifetime safe. Available to put in state:

`prefix` · `labels` · `trigger` · `view` (frozen) · `event_id` · `player_key` ·
`question` · `sale` · `entry` · `plan_state` · `prior_reads` · `threats`

### Open questions for the draft

- **Composition** — parent + subgraphs (narrow per-branch state, nested diagram,
  standalone testable) vs. one flat graph (simpler wiring, wider state).
- **Analyst tools** — whether it gets read-only pure functions over the frozen
  view (`get_team`, `get_position`, `get_player`, `get_plan`, `get_recent_sales`),
  and whether that extends to history and dossiers. This is the largest single
  win available and the only branch with a genuine cycle.
- **Strategist gating** — the transition test lives in `advice/strategy.py` today.
  Conditional edge, or a node that returns early?
- **Retry policy** — LangGraph has per-node retries. The Room should have none: a
  retry lands on a player who has already sold. The Grader could.

## Migration, once the topology lands

1. `pyproject`: `assist` extra gains `langgraph`, `langsmith`. Same lazy-import
   discipline — `test_assist_boundary.py` extends to assert nothing outside
   `assist/graph/` imports them, and not at module scope.
2. `assist/graph/` — state, nodes, wiring. Node bodies are thin adapters over the
   parts bin; no inference logic moves.
3. `runner._call` swaps its body for `graph.invoke()`. Transport untouched.
4. LangSmith: env-var activated, **fail-open** — a tracing outage must never cost
   a read. `draft_id` in run metadata so 180 invocations group as one draft.
5. Delete what the graph subsumes. Keep the transport.
6. Build agents 2–5 natively as branches.

## Verification

- **The suite still runs with no key, no network, no SDK.** The `complete`
  callable seam is unchanged, so existing fakes keep working. Add: no LangSmith
  traffic under test.
- **`ffa sim --assist` is the acceptance test**, before and after. It must
  reproduce 12–14s per read, 100% cache hits and ~4c/call — a regression in any
  of the three means the graph changed the request shape.
- **Journal-identity test** with and without `--assist`.
- **`draw_mermaid()` output committed to `docs/`** so the agent org is reviewable
  as a diagram, and diffs when the topology changes.
- Live rehearsal against a practice room, both paths, before the freeze.

## Also outstanding

- Three real SWIDs in `tests/unit/test_aliases.py`, public since `d33655a` —
  scrub from HEAD, or rewrite history and force-push. Still undecided.
- Dashboard `/ask` would be the first write path on a deliberately read-only
  surface. Terminal `ask` ships first.
