# What ESPN actually gives us during a live auction

Established 2026-08-17 against a running practice draft. Every claim here was
observed, not inferred from documentation.

---

## The short version

| Source | Live during the draft? | Carries prices? |
|---|---|---|
| REST `mDraftDetail` | **No** — empty skeleton throughout | Only after completion |
| SSE stream | Yes — it *is* the draft room | Not reachable (see below) |
| **Draft room DOM** | **Yes** | **Yes** |

The tool reads the DOM of the draft room you are drafting in, over the Chrome
DevTools protocol. That is the only channel that is both live and reachable.

---

## Why not REST

`GET /apis/v3/games/ffl/seasons/2026/segments/0/leagues/<id>?view=mDraftDetail`

**Does not update while a draft runs.** Sixty polls over fifteen minutes of
active drafting returned `inProgress: true` and `0/180` filled picks,
unchanged, while players were being bought. Polling this on draft day returns
an empty board for three hours — and fails *silently*, because that payload is
byte-identical to "the draft has not started yet".

Two things REST is still good for:

- **`nominatingTeamId`** on all 180 picks *before anything sells* — the whole
  nomination order, readable in advance.
- **The completed draft, afterwards.** The 2025 season returned all 180 picks
  with real `bidAmount`s ($1–$75, $2,389 total).

A practice draft also runs under a **different, ephemeral league id** than the
real league — visible in the draft-room URL, minted fresh per draft. The real
league's REST view never reflects a practice draft at all.

## Why not the SSE stream

The draft room is driven by Server-Sent Events:

```
fantasydraft.espn.com/game-1/league-<id>/sse/JOIN
  ?1=1 &2=<league id> &3=<teamId> &4=<SWID> &5=<63-char token>
  &6=false &7=false &8=KONA &nocache=<int>
```

Three observations rule out talking to it directly:

1. The token in param 5 is **minted at runtime** by the page's own JavaScript.
   It is not in the page HTML — what looks like a candidate there is a webpack
   `sha512-` integrity hash.
2. It is **single-use**. Replaying it on a second connection is refused.
3. ESPN allows **one connection per team**. A second connection evicts the
   first with a "Duplicate Connection" notice. A headless client would kick you
   out of your own draft.

Point 3 is the load-bearing one, and it is why the architecture is what it is.

---

## What the DOM gives us

`src/ffa/ingest/espn/draftroom.py`. All of this is live, mid-draft.

### Per completed pick — `.draft-board-grid-pick-cell.completedPick`

| Field | Selector | Example |
|---|---|---|
| Winning price | `.winningPrice` | `$76` |
| Player | `.playerFirstName` + `.playerLastName` | `Jahmyr Gibbs` |
| Position | `.positionPill` | `RB` |
| Roster slot | `.rosterSlot` | `RB` |
| NFL team | `.playerProTeam` | `DET` |
| Bye week | `.byeWeek` | `(6)` |
| Mine? | `myTeam` class on the cell | — |

**`innerText` on these cells is empty** — the layout is CSS-driven. Everything
must be read by element query. A text scrape returns nothing.

### Per team — `.auction-pick-component` (12 of them)

| Field | Selector | Example |
|---|---|---|
| Name | `.team-name` | `3. Fighting Finkelsteins` |
| Remaining budget | `.cash` | `$112` |
| Current bid | `.bid-amount` | `$2` |
| On autodraft | `autopick` class | — |
| My team | `--own` modifier | — |
| Nominating now | `--selecting` modifier | — |

Names arrive with a leading ordinal (`3. `) which is stripped on parse.

### The board itself

- 180 cells in one flat grid, laid out **a whole team at a time**
- `team_index = grid_index // slots_per_team`, and `slots_per_team` is derived
  as `cell_count / team_count` rather than hardcoded
- `.draft-board-grid-header-cell` gives the 12 team names in column order
- Pick counter (`PK 80 OF 180`) and clock are in the page text

### The `$null` trap

ESPN renders "no bid" as the literal string **`$null`**. Coercing that to `0`
would be serious: zero is a *known* value, so `merge` would treat "nobody has
bid" as a real price of nothing and let it beat a later genuine number. It
parses to `None`, which is an explicit unknown that merge fills in later.

---

## How it is wired

`DraftRoomSource` satisfies the same `EventSource` protocol as `ManualSource`
and `SimSource` — nothing downstream knows the events came from a browser.

- Diffs are keyed on **player**, not grid index, so a re-render or reorder does
  not replay the whole board as 80 fresh sales.
- A failed snapshot **degrades** rather than raising, so a blip does not take
  the tool down mid-draft.
- After `max_failures` consecutive failures it **stops and says so** — a reader
  that never recovers means the tab is gone, and retrying forever would hang
  the caller while hiding the reason.

---

## Draft-day setup

Chrome refuses `--remote-debugging-port` on your default profile, so the tool
uses a dedicated one. Log into ESPN there once; it persists.

```
python tools/draft_room_probe.py --launch      # one-time: log in, then reuse
python tools/draft_room_probe.py --catalog     # verify it can see the board
```

Then draft in that Chrome window as normal. The tool attaches read-only and
never opens a connection of its own, so it cannot evict you.

## Watching it live

```
python tools/draft_watch.py                    # attaches over CDP, opens a page
python tools/draft_watch.py --fixture tests/fixtures/espn/draftroom_snapshot_live.json
```

A throwaway diagnostic that serves one local page showing what `SNAPSHOT_JS`
returned next to what `parse_snapshot` made of it, polling once a second and
flashing whatever changed. Both panels come from a *single* evaluate, so a
disagreement between them is real rather than two different instants.

Use it to confirm the things a test cannot show you: that picks appear as they
land, that prices and positions match the board, and above all that each pick is
credited to the **right team** — the column on ESPN's board should match
`team_index` on the page.

---

## A completed practice draft is deleted

Observed 2026-08-17. A practice draft ran to completion — 180/180 picks, $2,299
spent, still fully rendered in the DOM. The moment it finished, its shadow
league returned **HTTP 404**:

```
/apis/v3/games/ffl/seasons/2026/segments/0/leagues/639481650?view=mDraftDetail
  -> 404
```

Not a credentials problem: the real league answered 200 on the same run with the
same cookies. ESPN reaps the ephemeral league when the practice draft ends.

Three consequences:

- **The DOM is the only record of a practice draft.** Capture anything you want
  from it *before* the draft finishes, because afterwards there is nothing to
  go back to.
- **Practice drafts can never test the post-draft REST path.** Not "we haven't
  tried" — it is structurally impossible, because the league ceases to exist.
- **Whether a *real* draft backfills is still unknown.** The real league is not
  ephemeral, so it may well populate on completion the way the 2025 season did.
  That can only be settled on draft day, and nothing depends on it: the live
  path is the DOM either way.

## Still unknown

Whether the **real** league's `mDraftDetail` backfills when the real draft
completes. Only draft day can answer it. It would be a nice end-of-draft
reconciliation pass; it is not on the critical path.
