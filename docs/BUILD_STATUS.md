# Build status

**As of 2026-08-18. Draft day is 2026-08-31 — 13 days out.**

Read this first. [`HANDOFF.md`](HANDOFF.md) is the running record of *how* things
came to be the way they are, and it has grown to 645 lines; this is the
stage-by-stage picture of *where they stand*.

> **You can draft today.** Every checkpoint is complete, all v1 capabilities
> ship, and the whole path was rehearsed against a live ESPN practice draft on
> 2026-08-17. Everything listed as not started is an improvement, not a
> prerequisite.

929 tests · 75 source modules · 37 test modules.

---

## The shape of it

```mermaid
flowchart TB
  subgraph done["Built and rehearsed"]
    E["Engine — CP1-CP5<br/>state, journal, undo, crash-resume"]
    A["Deterministic advisory<br/>capacity, scarcity, inflation, ceilings"]
    L["Live ESPN reader<br/>CDP into the draft room"]
    T["Terminal REPL<br/>the surface you will actually use"]
    H["Draft history + dossiers<br/>4 seasons, 12 managers"]
  end
  subgraph open["Not started"]
    D["Dashboard<br/>297-line spec, zero code"]
    C["Chat / LLM layer<br/>the inference half"]
  end
  N["Nomination order — CP4<br/>whole schedule, known in advance"]
  E --> A --> T
  L --> T
  H --> T
  N --> T
  A -.->|"build_view() is the contract"| D
  H -.->|"inputs now in place"| C
  style done fill:#e8f5e9,stroke:#2e7d32
  style open fill:#fff3e0,stroke:#ef6c00
  style N fill:#e8f5e9,stroke:#2e7d32
```

---

## Engine checkpoints — all complete

| | Scope | Status |
|---|---|---|
| **CP1** | Scaffold, config layer, ESPN probe | ✅ |
| **CP2** | State engine, event journal, REPL, undo, crash-resume, view-model contract | ✅ |
| **CP3** | Reference-data loader + deterministic advisory layer | ✅ |
| **CP4** | Auction simulator with budget-aware bots | ✅ |
| **CP5** | Live ESPN draft-room reader, `config init`, `data fetch` | ✅ |

**There is no CP6.** Everything below is product work.

---

## The 12 advisory capabilities

### v1 — the nominate → forecast → bid → win/lose loop

| # | Capability | Status | Where |
|---|---|---|---|
| 1 | Opponent snapshot | ✅ | `advice/bidding.py::threats_for` |
| 2 | Bid forecasting | 🟡 **half** | capacity ships; **intent needs the LLM layer** |
| 3 | Our bid recommendation | ✅ | `guidance_for`, incl. `safe_legal_bid` |
| 5 | Overspend alerts | ✅ | `advice/market.py::value_alert` |
| 7 | Owner dossiers | ✅ | `dossier/` — and **100% filled** |
| 4 | Nomination strategy | ✅ **new** | `advice/nomination.py` — schedule exact; candidate lists thin by design |
| 10 | Positional scarcity | ✅ | `advice/scarcity.py` |
| — | Market inflation | ✅ | `advice/market.py::market_state` |

Capability 2 is the only partial one, and the split is deliberate: **capacity is
arithmetic and is finished; intent is inference and is not.**

### v1.1 — one shipped early

| # | Capability | Note |
|---|---|---|
| 6 | Ambient feed | Needs a surface that is allowed to interrupt. |
| 8 | Post-pick fit | `starter_gaps` / `open_slots_by_pos` already compute the inputs. |
| 9 | Danger-zone watchlist | |
| 12 | Strategy adherence | Also needs **C2**, the strategy preset. |

### v2 — not started

| # | Capability | Note |
|---|---|---|
| 11 | Post-draft grade | Fires after the final pick, so zero draft-day risk. Build whenever. |

---

## The three surfaces

| Surface | Status |
|---|---|
| **Terminal REPL** | ✅ Complete. Rehearsed live 2026-08-17; full 180-pick dry runs 2026-08-18, incl. the new `turns` readout. |
| **Dashboard** | ❌ **Not started.** `docs/dashboard_requirements.md` is a 297-line panel-by-panel spec with no implementation. `build_view()` already emits every key it asks for, including the new `history` block. |
| **Chat / LLM layer** | ❌ **Not started.** Every input is now in place: dossiers filled, history measured, `build_view()` as the contract. Keep it **beside** the hot path — `advice` is instant and cannot fail, and a network call in that loop would add latency and a failure mode while a clock runs. |

---

## Blocking items

| | Item | Status |
|---|---|---|
| A1 | Auction values | ✅ `ffa data fetch` — 355 players from ESPN |
| A4 | ESPN cookies | ✅ |
| A5 | Prior-season capture | ✅ |
| B1 | Does ESPN populate prices? Is there a real-time channel? | ✅ **Both halves answered.** Prices yes; the draft room is SSE and REST stays empty during a draft — hence the CDP reader. |
| C1 | Owner dossiers | ✅ **100% covered** — 5 fields derived, 11 managers interviewed |
| C2 | Draft strategy preset | ❌ Not started (was always v1.1). Now also the thing standing between capability 4's two candidate lists and a real "nominate this next" recommendation. |
| Debt 1–7 | | ✅ All closed |

---

## Built this session, outside the original plan

| | |
|---|---|
| **Draft history** | 4 usable seasons (2022–25), 720 priced picks, full nomination order |
| **Evidence harness** | Every claim permutation-tested; **5 of 9 dropped as noise** |
| **Manager aliases** | Brian Cona's two ESPN accounts merged; orphan discovery built in |
| **Pace vs precedent** | Live in the bid readout, bounded to where the record discriminates |
| **Nomination order** | Capability 4. `pickOrder` read, cycle verified against a real 180-pick auction, `turns` readout, `nomination_plan` in `build_view()` |

---

## What is actually left

**One thing on the critical path:**

> **A second live rehearsal.** Everything shipped since 2026-08-17 — the raw
> console, the multi-tab guard, the stale-board guard, `safe_legal_bid`, the
> pace readout — is unit-tested and dry-run, but has never touched real ESPN
> traffic.
>
> ```
> python tools/draft_room_probe.py --launch
> ffa draft --new --source espn
> ```
>
> Start an ESPN practice draft, buy three or four players. Half an hour, and it
> is the only thing between "tested" and "proven".

**One thing that needs a command before it works:** the nomination order is read
by `ffa config init`, and your current `config/league.toml` predates that — it
loads fine, but carries no order, so `turns` cannot name a seat. One command
fixes it, and `ffa config check` now says so.

```
ffa config init --force
```

**Two large optional builds:** the dashboard and the LLM layer. Both fully
unblocked. Neither is needed on 2026-08-31.

**One question a rehearsal answers for free.** The draft room marks a seat
`--selecting`, and `RoomTeam.is_nominating` already parses it, but what it means
*while bidding runs* — the seat that put this player up, or the seat due to put
up the next one — has never been observed. It is deliberately not wired to
`nominated_by` until it is: guessing wrong would make the stale-order banner
fire on nearly every pick. `tools/draft_watch.py` already flashes it on change,
so watching one nomination land settles it.

---

## Draft-day command sequence

```
python tools/draft_room_probe.py --launch   # one-time: Chrome with a debug port
ffa config init                             # rebuild league.toml from ESPN
ffa config nicknames                        # names you can type under a clock
ffa config alias                            # check for second accounts
ffa data fetch                              # ESPN's consensus auction values
ffa history fetch && ffa history report     # prior seasons -> precedent artifact
ffa dossier status                          # confirm coverage
ffa config check                            # last gate before the day
ffa draft --new --source espn               # draft
```

`ffa config init` is what puts the nomination order in place; `ffa config check`
prints it back as names you recognise, and says so plainly if it is missing.
