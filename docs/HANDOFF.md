# Handoff — ESPN Live Auction Draft Assistant

State of the build as of **2026-08-17**. Draft day is **2026-08-31, 8pm ET** —
two weeks out.

Branch: `claude/plan-file-review-g05z77`. 376 tests pass. Everything below is
pushed.

---

## What this is

A live companion for a 12-team, $200 ESPN auction draft. When a player is
nominated it says what to bid and who can actually outbid you — joining every rival's
remaining budget with whether they still need that position. A team with $90 and
no open RB slot is not an RB threat, and making that legible is the whole point.

Three source docs drive it, all in the conversation history rather than the repo:
`04_handoff_plan_espn_draft_tool.md` (integration build),
`05_advisory_layer_capabilities.md` (12 capabilities, 3 surfaces, 4 gaps), and
the running build plan.

---

## Done: CP1–CP3

**CP1 — scaffold, config, ESPN probe.** `tools/espn_probe.py` fetches and
analyses raw ESPN views, and now also runs offline via `--analyse FILE` (the dev
sandbox cannot reach ESPN at all — see Constraints).

**CP2 — state engine.** Append-only JSONL event journal, five closed event
types, per-field provenance, undo/redo as a replay filter, crash-resume,
single-writer store with a pid lockfile, terse-command REPL. Plus the dashboard
requirements doc and the JSON view-model contract.

**CP3 — reference data + deterministic advisory.** Tolerant loader (CSV / XLSX /
pasted TSV, header-row search, combined-cell splitting), PlayerBook with fuzzy
matching that refuses to guess, scarcity tiering, market inflation, and
`max_advisable_bid`.

You can run a complete draft by hand today, with live bid guidance, and crash
and resume it exactly.

---

## The load-bearing ideas

Read these before changing anything; each exists to prevent a specific failure.

**Per-field provenance is the whole architecture.** There is no "API mode" and
no "manual mode." Every observed value is a `Sourced[T]` carrying where it came
from. `merge` decides conflicts:

1. **Knowledge beats ignorance, both directions** — a known value always beats
   an unknown one, *regardless of provenance*. The second half of this is easy
   to omit and catastrophic: without it, typing `sold mahomes ? t3` (MANUAL,
   unknown) makes a later real ESPN price lose on precedence, permanently
   rejecting the correct number.
2. Both known: higher provenance wins, so a poll never stomps your correction.
3. Same provenance: newer wins; exact tie goes to journal order.

**Nothing computable is stored.** Teams have no budget or roster field;
`remaining`, `max_legal_bid`, `open_slots` are pure functions. This kills
budget-vs-roster drift and is *why undo is a one-line filter*.

**Reducers are total and never reject.** By the time `apply` sees an event it is
already durably on disk; refusing it would make journal and state disagree on
resume. Validation that can say no happens at parse and command time, before
anything is written. An overspend warns and records — ESPN is the truth about
what happened.

**Undo never inverse-applies.** Merge is lossy, so the pre-merge value is
unrecoverable. Undo is a replay filter. Undoing an undo is redo, free, no sixth
event type.

**`max_legal_bid` vs `max_advisable_bid.`** The first is roster arithmetic you
cannot exceed (every other open slot still costs $1). The second is judgment.
They must never be conflated in code or UI.

**Identity anchors on owner SWID, never team name.** Managers rename teams
constantly. `label` resolves to the manager nickname or `teamN` — deliberately
never the team name. Team resolution matches nicknames only.

**Team ids are not contiguous.** The real league runs 1–5 and 7–13; there is no
team 6. `range(1, count+1)` invents a phantom team and drops a real one,
silently.

---

## Confirmed about ESPN

From a real capture of league `1167816302` (values in `config/league.toml`,
gitignored; anonymized structure in `tests/fixtures/espn/`):

- `auctionBudget` (200) and draft `type` (AUCTION) exist only in the **raw**
  `mSettings` — `espn-api`'s `BaseSettings` drops both, so config bootstrap
  cannot use the library alone.
- Roster from `lineupSlotCounts`: QB1 RB2 **WR3** TE1 FLEX1 K1 DST1 **BE5** IR1
  → **15 draftable slots**, opening max bid **$186**.
- ESPN **pre-creates all 180 picks** (15 rounds × 12) before anyone drafts, with
  `playerId: -1`. So a pick is *filled* when `playerId != -1` — polling must
  diff on that, never on `len(picks)`, which never changes.
- **Nomination order is known in advance** via `draftSettings.pickOrder` plus
  each pick's `nominatingTeamId`. This answers the capability spec's open
  Phase-1 question and unblocks capability 4.
- **No per-team auction budget field exists.**
  `transactionCounter.acquisitionBudgetSpent` is the FAAB waiver pot, a
  different thing. Remaining budget must be summed from `bidAmount`s — which is
  what `projections.remaining_budget` already does.

---

## Open — and the trap in it

**B1: does `bidAmount` populate during a live auction? Still unobserved.**

A Practice Draft was run several picks deep. `segments/0` `mDraftDetail` came
back **byte-identical to the pre-draft baseline**, `inProgress` still false.

Stated narrowly: practice picks are not at `segments/0` for that league id. They
are presumably readable somewhere — ESPN's draft room renders them. Untested
candidates:

- a different **segment id** (path is `/segments/0/`)
- a **shadow league id** minted for the practice draft, visible in the draft-room URL
- a different **view**, or a draft-specific service
- a real-time channel the REST views only mirror after completion

**The way to settle it:** DevTools → Network, filter `apis/v3`, while a practice
draft runs. Whatever the room fetches is the answer. Failing that, a throwaway
private auction league drafted against autopick gives a draft ESPN considers
real.

**The trap:** an unfilled skeleton is indistinguishable from "ESPN never
populates prices." Reading it that way inverts the truth and would strand the
build on manual entry permanently. The probe therefore reports **INCONCLUSIVE**
on zero filled picks rather than concluding anything. Preserve that behavior.

**The bigger untested risk:** whether the real draft updates `mDraftDetail`
*live* or only on completion. If the draft room is real-time-driven and REST is
written at the end, live polling gets nothing on draft day regardless of
`bidAmount`. This is why `price=None` and manual entry are first-class
throughout, not a fallback.

---

## Constraints on the dev environment

**This sandbox cannot reach ESPN.** The network policy denies
`fantasy.espn.com` and `lm-api-reads.fantasy.espn.com` at the proxy (403 at
CONNECT). It also denies `fantasypros.com`. Every live capture must happen on
the user's machine — easiest by pasting an API URL into a browser already
logged into ESPN, since session cookies ride along automatically.

Consequence: `tests/fixtures/espn/` is the only contract CP5's adapter can be
built against. If those fixtures drift from reality, nothing catches it until
draft day.

---

## Waiting on the user

| # | Item | Blocks |
|---|---|---|
| A1 | **FantasyPros auction values** at real settings → `data/reference/`, then `ffa data validate <path>` | Every number is currently invented sample data |
| A4 | **ESPN cookies** (`espn_s2`, `SWID`) → `.env` | The probe and CP5; the league is private |
| A5 | **A draft ESPN considers real**, or the DevTools trace above | B1 |
| C1 | **Owner dossiers** — a ~20–30 min interview, 12 managers | Capability 2's bid forecasting reads them; v1 |
| C2 | **Draft strategy preset** — one archetype | v1.1 |

`config/league.toml` holds real league id, member SWIDs, and manager nicknames.
It is **gitignored and lives only in the ephemeral container** — copy it out or
regenerate it from the captured settings.

---

## Next

**CP4 — simulator.** Budget-aware bots as just another `EventSource`, no
`if simulating:` anywhere in the engine. Fault injection (`--drop-prices 0.3`)
exercises the partial-provenance path. This is the real acceptance test and
needs no ESPN access, so it is the obvious next move.

**CP5 — production ESPN adapter.** Config bootstrap from raw `mSettings`, poller
with backoff and circuit breaker, queue wiring, degradation, draft-day runbook.
Built against the fixtures; verified on the user's machine.

**Draft-day gate, unchanged:** the tool must run end-to-end against real ESPN
traffic at least once before 2026-08-31. That gate is currently **unmet**.
