# Draft Dashboard — Requirements for Design

Input brief for the dashboard design effort. Derived from
`05_advisory_layer_capabilities.md`; this document turns that capability list
into concrete panels, an information hierarchy, and the data contract the
engine already emits.

Sample payload to design against: **`docs/sample_state.json`** — real output
from `ffa export-state`, not a mockup.

---

## 1. What this is for

A live $200 ESPN auction draft. Roughly 180 players sell over ~3 hours, one at
a time, in maybe 30–60 seconds each. Between the moment a player is nominated
and the moment the gavel falls, the user needs to answer one question:

> **Should I bid on this guy, and how high can I go before someone else stops
> being able to follow me?**

Everything on screen either serves that question or gets out of the way. The
defining constraint is that the user is reading this *while being bid against*.
A number that takes four seconds to find is a number they will not use.

### The one insight the design should make obvious

Money alone does not make a rival dangerous. A team with $90 left that has
already filled every RB and FLEX slot **cannot meaningfully bid on an RB**. The
dashboard's job is to make "who can actually take this player from me" readable
at a glance — which means budget and positional need have to be visually
*joined*, not shown in two separate places the user has to mentally cross-
reference.

---

## 2. Three surfaces

| Surface | Purpose | Behavior |
|---|---|---|
| **Dashboard** (this doc) | Ambient truth. Where the board stands. | Always current. **Never announces.** No toasts, no flashing, no modal steal. |
| **Ambient feed** | Notable events over time. | Separate persistent sidebar. Append-only, low-frequency. |
| **Chat thread** | Decisions and Q&A. | Speaks only about *our* team or when a decision is needed. |

**Contention rule (closes gap 1 in the capability spec):** one state
transition produces **one coalesced update bundle** — the dashboard re-renders
silently, the feed appends 0–N entries, the chat emits **at most one** message.
Never three independent fires for one pick. During a fast stretch of the draft
this is what keeps the thread readable.

The dashboard is **display-only**. Input stays in the terminal, which preserves
the engine's single-writer discipline and makes the dashboard a pure function
of state. Click-through for detail is expected; nothing on it mutates the
draft.

---

## 3. Panels

Priority tiers: **v1** must exist for draft day. v1.1 is high-value but the
draft works without it.

### 3.1 My Status — v1 — highest priority

The single most-glanced element. Should be readable peripherally.

- Remaining budget, **max legal bid** (the hard ceiling: `remaining − (open
  slots − 1)`, because every other slot still costs $1), roster slots filled.
- Starter gaps as discrete slot chips (`QB` `RB` `WR×2` `FLEX`…), not prose.
- When any of my prices are unknown, remaining must render as a floor (`$142+`)
  — never as a bare number. See §5.

Data: `me.*`.

### 3.2 Opponent Grid — v1 — capability 1

One tile per rival, 11 tiles. **Budget and positional need must read as one
object per tile**, since their intersection is the actual signal.

Per tile: manager label, remaining (with floor marker), max legal bid, slots
filled `n/16`, starter gaps as chips, overspend flag.

Sorting should be meaningful rather than arbitrary — by threat on the
*currently nominated player* when there is one (can they afford him *and* do
they need his position), by remaining budget otherwise. Design should show both
states.

Click-through opens per-team detail: every player drafted, price paid,
remaining slots, and (v1.1) the inferred tendency note.

Data: `teams[]`.

### 3.3 Nomination Focus — v1 — capabilities 2 & 3

Only present when a player is up for bid; the highest-attention region when it
is. Needs to accommodate, without reflowing as numbers arrive:

- Player name, position, reference auction value *(CP3)*.
- Suggested bid range and a yes/no call with 2–3 reasons *(advisory layer)*.
- **Ranked likely bidders**, each with their true ceiling — this is where the
  budget × need join pays off.
- Live price as it climbs, if available (see §6).

Data: `nomination` + `teams[]`. The advisory fields arrive from the Claude
layer, not the engine.

### 3.4 Positional Scarcity — v1 — capability 10

Tier counts per position: elite / startable / bench-only remaining. Small,
persistent, glanceable. Threshold crossings ("last elite RB gone") also emit to
the ambient feed — the tile itself stays quiet.

Data: `scarcity` — **currently `{}`; populated in CP3** once reference tiers
exist. Design against the shape, expect empty today.

### 3.5 Market Inflation — v1 — cross-cutting

One compact readout: total spent league-wide, dollars remaining, and inflation
ratio (actual price ÷ reference value, running). Drives the dynamic overspend
threshold, so it needs to be visible enough to build trust in the alerts.

Data: `market`. `inflation_ratio` is **`null` until CP3** — the design must
degrade gracefully rather than showing a broken gauge.

### 3.6 Recent Sales — v1.1

Last ~10 sales with price and (CP3) delta vs. reference value. Provides the
"is the room hot or cold" read that the inflation number abstracts away.

Data: `recent_sales`.

### 3.7 Warnings — v1

Persistent, non-modal. Overspend flags, unknown prices still outstanding,
degraded data source. Must be visible without being alarming — most of these
are informational, not errors.

Data: `warnings`, plus per-team `is_overspent` / `unknown_price_count`.

---

## 4. Information hierarchy

1. My max bid and my remaining budget.
2. The nominated player and who can actually outbid me.
3. Opponent budgets joined with positional need.
4. Scarcity and market temperature.
5. Everything else.

If the layout has to break at a narrow width, it should degrade in reverse
order. Tiers 1–2 must survive any viewport.

---

## 5. Uncertainty is a first-class state

Not an edge case — the normal condition early in a draft, and the thing most
likely to mislead.

- A sale can be recorded **before its price is known**. Those are charged at
  the $1 minimum, so a team's `remaining` is a **floor, not a fact**.
- `remaining_is_floor: true` and `unknown_price_count: n` say when this
  applies. Render as `$142+`, with the count reachable on hover or in detail.
- **Never render a floor as a plain number.** It over-estimates a rival's
  ammunition — deliberately, since that's the safe direction for advice — but
  only if the user can see that's what they're looking at.
- Every price carries `price_provenance` (`MANUAL` / `ESPN_API` / `UNKNOWN`).
  The design should have *some* affordance for "where did this number come
  from", at least in detail views. The whole system is built on the
  ground-truth-vs-inference distinction; the UI shouldn't flatten it.

---

## 6. Live bid price — design for the degraded path

Whether ESPN exposes an in-progress bid (before a sale finalizes) is
**unconfirmed** and probably websocket-only. Both capability 2 and 3 already
specify degraded behavior, so:

**Treat the degraded path as the baseline.** Capability 2 fires on the user's
manual nomination flag; capability 3 re-fires on manual price updates. Live
price is an *enhancement* that lights up if it turns out to be reachable — the
layout must not have a hole in it when it isn't.

---

## 7. Data contract (closes gap 2)

One shared draft-state object, emitted by the engine, read by every surface.
No capability keeps its own store.

```bash
ffa export-state --out docs/sample_state.json
```

Top-level keys: `schema_version`, `draft_id`, `generated_at`, `initialized`,
`league`, `me`, `teams[]`, `nomination`, `market`, `recent_sales[]`,
`scarcity`, `warnings[]`.

Per team:

```jsonc
{
  "team_id": 2, "label": "dave", "manager": "dave", "is_me": false,
  "spent": 55, "remaining": 145,
  "remaining_is_floor": false,     // true => render as "$145+"
  "unknown_price_count": 0,
  "max_legal_bid": 131,            // the hard ceiling, already computed
  "is_overspent": false,
  "roster_count": 1, "open_slots": 15,
  "open_slots_by_pos": { "QB": 8, "RB": 9, ... },
  "starter_gaps":     { "QB": 1, "RB": 2, ... },   // starters only — the real need
  "roster": [ { "name", "position", "price", "price_known", "price_provenance" } ]
}
```

**`open_slots_by_pos` vs `starter_gaps`:** the first includes bench and is
almost always non-zero; the second is starting slots only and is what makes a
rival a genuine threat at a position. **Panels should key off `starter_gaps`.**

Everything above is deterministic and computed by the engine. The advisory
layer never recomputes it — it consumes it and adds judgment. Owner dossiers
(capability 7) live separately in `data/dossiers.json`, since they persist and
mutate on a different cadence.

Refresh: the engine writes the snapshot after every state transition. Poll it
or re-export; the payload is small (a few KB) and cheap to diff.

---

## 8. Build priority (closes gap 4)

**v1 — the nominate → forecast → bid → win/lose loop:**
1 (opponent snapshot), 2 (bid forecasting), 3 (our bid rec), 5 (overspend
alerts), 7 (owner dossiers), 10 (positional scarcity), + market inflation.

Capability 7 is promoted into v1 against the spec's suggested 1–5 split:
capability 2 reads the dossiers directly, so deferring them would ship bid
forecasting with its main inference input missing.

**v1.1:** 4 (nomination strategy), 6 (ambient feed), 8 (post-pick fit),
9 (danger-zone watchlist), 12 (strategy adherence).

**v2:** 11 (post-draft grade) — fires after the final pick, so it carries zero
draft-day risk and can be built whenever.

---

## 9. Constraints

- **Read-only.** No control mutates draft state.
- **Degrades, never blanks.** `scarcity: {}` and `inflation_ratio: null` are
  the *current* state and must render as "not yet available", not as zero and
  not as a broken component.
- **Legible under stress.** Assume a glance, not a read. Assume a laptop
  screen, possibly next to the ESPN draft room window — so it should survive a
  half-width viewport.
- **Quiet.** The dashboard never demands attention; the feed and chat are the
  surfaces allowed to interrupt.
