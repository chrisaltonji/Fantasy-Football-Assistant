# Fantasy Football Assistant

A live companion for an ESPN **auction** draft. When a player gets nominated,
it tells you what to bid and who's actually going to fight you for him —
factoring in every rival's remaining budget *and* whether they still need that
position. A team with $90 left that's already full at RB is not a threat on an
RB, and the tool should say so.

Built to degrade gracefully: if ESPN's API gives us nothing on draft day, you
type picks in by hand and every downstream calculation works identically.

> **Status: Checkpoint 3 of 5.** Scaffold, config, ESPN probe, state engine,
> event journal, manual REPL, reference-data loader, and the deterministic
> advisory layer are done — you can run a complete draft by hand today with
> live bid guidance, crash it, and resume it exactly. The simulator and the
> production ESPN adapter land next. See [Roadmap](#roadmap) below.

---

## Setup

Requires Python 3.11+.

```bash
git clone https://github.com/chrisaltonji/Fantasy-Football-Assistant.git
cd Fantasy-Football-Assistant

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e '.[dev]'

pytest                            # should be all green
ffa --version
```

If you have `uv`, `uv venv && uv pip install -e '.[dev]'` is the faster path.

---

## Configure your league

```bash
cp .env.example .env              # then fill in your cookies (below)
cp config/league.example.toml config/league.toml
$EDITOR config/league.toml        # set league_id, year, team count, roster
ffa config check                  # validates everything and explains what's wrong
```

`ffa config check` fails loudly on anything that would produce silently wrong
advice later — a budget too small to fill a roster, a snake-draft league, a
poll interval tight enough to get you rate-limited.

> `ffa config init`, which fills this in automatically from ESPN's own league
> settings, arrives in Checkpoint 5.

### Getting your ESPN cookies

Only needed for **private** leagues. Both values are session credentials tied
to your ESPN login — treat them like a password. `.env` is gitignored, and the
probe scrubs them out of anything it writes to disk.

1. Log in to <https://fantasy.espn.com> in Chrome or Firefox.
2. Open DevTools (`F12`) → **Application** tab (Chrome) or **Storage** tab
   (Firefox) → **Cookies** → `https://fantasy.espn.com`.
3. Copy the value of **`espn_s2`** (a long URL-encoded string) into `ESPN_S2`.
4. Copy the value of **`SWID`** (looks like `{XXXXXXXX-XXXX-...}`) into
   `ESPN_SWID`. Keep the braces — if you drop them, the tool puts them back.

These expire. If you start seeing `401 Unauthorized`, re-copy them.

---

## Checkpoint 1: probe ESPN

`tools/espn_probe.py` is a standalone diagnostic — nothing in the app imports
it. It dumps raw JSON from ESPN's undocumented views and analyses it to answer
the three questions the build depends on:

1. **Does an auction pick's JSON actually carry the winning bid amount?**
2. What does a team object expose for remaining budget and roster?
3. Does cookie auth work end-to-end against your private league?

```bash
python tools/espn_probe.py --league-id 123456 --year 2026

# during a live draft, re-probe every 5s to catch picks as they land:
python tools/espn_probe.py --league-id 123456 --year 2026 --watch 5
```

Payloads land in `probe_out/` (gitignored) alongside a plaintext report.
Credentials come from `.env` only, never the command line, so they can't end up
in your shell history.

### What you need to do

**Run an ESPN Practice Draft in salary-cap mode**, let a handful of players
sell for real dollars, and run the probe against it with `--watch 5`. That is
the only way to answer question 1 — and the captured payloads become the
offline fixtures the real ESPN adapter gets built and tested against in
Checkpoint 5.

Then paste the report back, or commit the scrubbed JSON to
`tests/fixtures/espn/`.

### What we already know

Checked directly against the installed `espn-api` library rather than its docs:

- **`espn_api/base_pick.py` — `BasePick.__init__` already accepts
  `bid_amount`, `keeper_status`, and `nominatingTeam`.** The library is not
  snake-only; it models auction picks natively. That's a meaningful de-risk over
  what the original handoff doc assumed.
- **`espn_api/base_settings.py` does *not* expose `draftSettings.auctionBudget`
  or the draft `type`.** Both exist in the raw `mSettings` JSON but the library
  drops them, so `ffa config init` will need a raw view fetch alongside the
  library.

So the open question is no longer *"is the data reachable?"* but *"does ESPN
populate `bidAmount` during a live auction?"* — which only your Practice Draft
can settle.

> **The development sandbox cannot reach ESPN.** Its network policy denies
> `fantasy.espn.com` and `lm-api-reads.fantasy.espn.com` at the proxy. Every
> live-ESPN step therefore has to run on your machine; the sandbox works
> against captured fixtures.

---

## Running a draft

```bash
ffa draft --new        # start; prints the draft id
ffa draft --resume     # pick up the most recent draft for this league
```

Everything is fsynced before the next prompt appears, so Ctrl-C, a closed
laptop, or a dead battery lose exactly nothing. Resume restores the board
precisely.

### Commands

| Command | Does |
|---|---|
| `sold <player...> <price\|?> <team> [pos]` | Record a sale. `?` means "sold, price unknown". |
| `nominate <player...> [team]` | Flag who's up for bid. |
| `price <player...> <amount>` | Fill in or correct a price. |
| `amend <entity> <field> <value>` | General correction, e.g. `amend team:3 manager dave`. |
| `undo [#id]` / `redo` | Undo the last event, or a specific one. |
| `budgets [team]` | Remaining money, max bid, and starter gaps per team. |
| `state` / `log [n]` | Full board / recent events with their ids. |
| `help [verb]` / `quit` | |

Player names are free text and multi-word names need no quoting — price and
team are read as the **last two tokens**, so `sold patrick mahomes 45 dave`
parses correctly. Fuzzy matching against real rankings arrives in CP3.

Teams are addressed by the nicknames in your `[managers]` config block
(`dave`, or any unique prefix like `dav`), and always by slot as `t3` /
`team3` / `3`.

```
> sold ceedee lamb 55 dave wr
#3 ceedee lamb -> dave $55
> sold mahomes ? sam
#4 mahomes -> sam $? (price unknown)
> budgets
TEAM      SPENT  REMAINING  MAX BID  SLOTS  NEEDS
*chris    $0     $200       $185     0/16   DST FLEX K QB RBx2 TE WRx2
 dave     $55    $145       $131     1/16   DST FLEX K QB RBx2 TE WR
 sam      $0     $199+      $185     1/16   DST FLEX K QB RBx2 TE WRx2
+ = at most; some prices unknown (fill in with: price <player> <amount>)
> price mahomes 45
#5 mahomes price set to $45
```

`$199+` is the important detail: a sale with an unknown price is charged at the
$1 minimum, so that team's remaining budget is a **floor, not a fact**, and the
tool says so rather than presenting a guess as a number.

Runs live in `runs/<draft_id>/events.jsonl` — an append-only log that *is* the
draft. It's plain JSON lines; you can read it, grep it, and hand it to anyone
debugging a discrepancy.

### Your auction values

Export from your auction-value calculator **at your real league settings**
(teams, budget, scoring, roster), drop the file in `data/reference/`, and point
`[reference].path` at it. CSV, XLSX, and pasted tab- or space-separated text all
work — copy-paste is a first-class path, since CSV export is a paid feature on
most sources.

Check it before draft day, not during:

```bash
ffa data validate data/reference/auction_values.csv
```

```
data/reference/auction_values.csv
  header found on line 3
  columns: auction_value, bye_week, nfl_team, overall_rank, player_name, position, ...
  248 row(s) ok, 0 dropped
```

The loader is deliberately hard to upset. It searches for the header rather than
assuming line 1 (exports routinely carry a title row), it splits combined cells
like `Ja'Marr Chase CIN (WR1)` into four fields, and it maps header aliases
(`Value` / `$` / `AAV` / `Auction Value` all mean the same thing). A row it
can't read is dropped with a reason and the load continues — you only get a hard
error if the whole file is unusable.

**With no file configured it runs on `data/fixtures/sample_rankings.csv`, whose
values are invented**, and says so loudly on every startup.

### Bid guidance

Flag a nomination and the readout fires automatically:

```
> nominate rashad halson
#14 up for bid: Rashad Halson
Rashad Halson (WR)
  sheet value  $42 -> $48 at current market
  bid up to    $48   (range $41-$55)
  legal max    $115   fills a starting slot
  who can take him (11 live):
    dave         up to $135  (has $149, 9 slot(s))
    sam          up to $133  (has $147, 10 slot(s))
  - market running inflated (1.15x)
```

Two numbers, deliberately distinct. **`legal max`** is arithmetic you cannot
exceed — every other roster slot still costs $1, so that money isn't available.
**`bid up to`** is what the sheet, current inflation, and your roster needs say
he's worth.

"Who can take him" counts only rivals who can afford him *and* still start him.
A team with $90 and no open RB slot doesn't appear on an RB — that omission is
the whole point of the tool.

Also: `scarcity` (what's left per position, tiered against your league's real
starting demand), `market` (inflation vs. your sheet), and `advice <player>` for
anyone not currently nominated.

Player names resolve against your file, so `sold halson 45 dave` works. A typo
or an ambiguous name is **never** guessed — you get numbered candidates and
retype. A wrong match wears a real player's price and fails silently, which is
the worst thing this tool could do.

### Feeding the dashboard

```bash
ffa export-state --out docs/sample_state.json
```

Emits the JSON view model that the dashboard, ambient feed, and chat layer all
read. See **[`docs/dashboard_requirements.md`](docs/dashboard_requirements.md)**
for the panel-by-panel spec and **`docs/sample_state.json`** for a realistic
mid-draft payload.

---

## Design in one page

**There is no "API mode" and no "manual mode."** There are event *producers*,
and every individual field carries its own provenance:

```
Sourced[T] = { value | None, provenance, confidence, observed_at }
Provenance:  MANUAL > ESPN_API > SIM > INFERRED > UNKNOWN
```

`value=None` is legal and meaningful — *"a sale happened, we don't know the
price."* One merge function is the entire fallback feature:

1. **Knowledge beats ignorance, in both directions** — a known value always
   wins over an unknown one, *regardless of provenance*.
2. Both known: higher provenance wins, so a later poll can never stomp your
   manual correction.
3. Same provenance: newer wins; on an exact tie, journal order.

Rule 1 is the one that's easy to get wrong and expensive to omit. Without its
second half, typing `sold mahomes ? t3` (MANUAL, unknown) would make the real
ESPN price lose on precedence — permanently rejecting the correct number and
inverting the feature the whole design exists to provide.

Everything computable is computed, never stored — `remaining_budget`,
`max_bid`, `open_slots` are pure functions of state. That kills the whole class
of "budget says $87 but the roster says $92" drift bugs.

State persists as an **append-only JSONL event journal** (`runs/<id>/`), so undo,
crash-resume, and the audit trail all fall out of one mechanism.

```
src/ffa/
  domain/     enums, sourced, models, events, reducers, projections
  state/      journal, store (single-writer), recovery
  ingest/     source.py (the EventSource seam)
              espn/    client, poller, mapping, health
              manual/  grammar, resolve, source
  reference/  schema, loader, playerbook
  advice/     bidding, needs, threats, risk, engine
  sim/        bots, simulator, scenarios
  cli/        app, repl, render
  config/     schema, loader, espn_settings
```

The store, reducers, and advisory engine have **zero knowledge that ESPN
exists**. Adding a web UI later is one new `DraftStore` subscriber plus one new
`EventSource`; the engine is untouched.

---

## Roadmap

| CP | Scope | Status |
|----|-------|--------|
| 1 | Scaffold, config layer, ESPN probe | **done** — needs your Practice Draft |
| 2 | State engine, event journal, manual REPL, undo, crash-resume, dashboard contract | **done** |
| 3 | Reference data loader + deterministic advisory layer | **done** — running on sample data until you add yours |
| 4 | Auction simulator with budget-aware bots (the real acceptance test) | next |
| 5 | Production ESPN adapter, graceful degradation, draft-day kit | |

Explicitly deferred: tuning how *good* the advice is (separate workstream), the
web UI, and live in-progress bid state (almost certainly websocket-only, and
unnecessary since you flag nominations manually).

**Draft-day gate:** this must have run end-to-end against real ESPN traffic at
least once before draft day. Never run it live for the first time on the day.

---

## Development

```bash
pytest                    # full suite
pytest tests/unit -q      # fast path
```

Never commit `.env`, `config/league.toml`, or anything under `data/reference/`
— all three are gitignored because they hold either credentials or your private
research.
