# espn-fantasy-api

A read-only Python client for ESPN's **undocumented** Fantasy Football API,
plus the one thing a plain HTTP client cannot do: read a live auction draft as
it happens.

Nothing here writes to ESPN. There is no mutation surface in the package at
all — you can point it at a league you are in without any risk of touching it.

```python
from espn_fantasy import EspnClient, load_credentials, fetch_settings, fetch_players

client = EspnClient(league_id=123456, year=2026, credentials=load_credentials())

settings, warnings = fetch_settings(client)
print(settings.draft_type, settings.auction_budget, settings.team_count)

players, _ = fetch_players(client)
print(players[0].name, players[0].auction_value, players[0].adp)
```

## Why this exists

ESPN publishes no documentation, no schema, and no stability guarantee for any
of this. Most of the value in this package is not the HTTP calls — those are
ten lines. It is the accumulated knowledge of **how the API misleads you**,
encoded as code and pinned by tests against real captured payloads.

A sample of what is in here that you would otherwise learn the hard way, during
a draft:

- The REST draft view **is not written while a draft is running**, and the
  payload you get mid-draft is byte-identical to the one you get before it
  starts. Polling it on draft day returns an empty board for three hours and
  never once reports an error.
- A **private league with no cookies returns 404**, not 401, so the obvious
  error message sends you to debug the one thing that is fine.
- **Team ids are not contiguous.** `range(1, count + 1)` invents a team that
  does not exist while dropping one that does.
- ESPN renders "no bid" as the literal string **`$null`**. Coercing it to `0`
  turns "nobody has bid" into a settled price of nothing.
- The player endpoint returns a handful of players unless you send an
  **`x-fantasy-filter` header**. No amount of query-string coaxing substitutes.
- The popular `espn-api` library's `BaseSettings` exposes neither
  `auctionBudget` nor the draft `type` — the two fields a salary-cap league is
  entirely defined by.

`docs/ESPN_DATA_ACCESS.md` is the long version, with the observations each
claim rests on.

## Install

```bash
pip install -e .                     # REST client only
pip install -e '.[draftroom]'        # adds the live draft-room reader
python -m playwright install chromium # only if you want the draft-room reader
```

Python 3.11+. The only hard dependency is `requests`.

## Credentials

Only needed for **private** leagues; public leagues work without them. Both
values are session credentials tied to your ESPN login — treat them like a
password.

```bash
cp .env.example .env    # then fill in ESPN_S2 and ESPN_SWID
```

1. Log in to <https://fantasy.espn.com> in Chrome or Firefox.
2. DevTools (`F12`) → **Application** tab (Chrome) or **Storage** tab (Firefox)
   → **Cookies** → `https://fantasy.espn.com`.
3. Copy **`espn_s2`** (a long URL-encoded string) into `ESPN_S2`.
4. Copy **`SWID`** (looks like `{XXXXXXXX-XXXX-...}`) into `ESPN_SWID`. Keep the
   braces — if you drop them, the client puts them back.

Your league id is in the URL of any league page. These cookies expire; when you
start seeing `401 Unauthorized`, re-copy them.

Credentials are read from the environment or `.env` and never from a command
line argument, so they cannot end up in shell history or a process listing.
`.env` and `*.har` are gitignored.

## The CLI

```bash
espn-fantasy settings                 # draft type, budget, roster, scoring
espn-fantasy teams                    # team ids, names, managers
espn-fantasy players --csv values.csv # ESPN's auction values, ADP, projections
espn-fantasy draft --year 2025        # a completed draft, with real bids
espn-fantasy raw mSettings --out capture.json
espn-fantasy watch                    # tail a live draft room (see below)
```

`--league-id` / `--year` fall back to `ESPN_LEAGUE_ID` / `ESPN_SEASON_YEAR`.

## The library

| Module | What it does |
|---|---|
| `client` | Session, host fallback, URL shapes, error messages that name the actual problem |
| `credentials` | Cookie loading and the folding that makes a copy-pasted SWID match |
| `league` | `mSettings` + `mTeam` → `LeagueSettings`, `TeamDirectory` |
| `players` | The player universe with ESPN's own auction values, ADP, projections |
| `draft` | `mDraftDetail` → `DraftBoard`, and the three states of it that look identical |
| `draftroom` | Reads a live draft room's DOM over the Chrome DevTools protocol |
| `watch` | Polls the draft room and reports only what changed |
| `enums` | ESPN's lineup-slot, position and pro-team id tables |

Everything that parses is pure and side-effect free, so it can be tested
offline against the committed fixtures — which is the only way any of it was
verified in the first place, since ESPN has no staging environment.

### Two independent ways in

**REST** gives you settings, teams, the player universe, and completed drafts.
It does not give you a draft in progress.

**The draft room DOM** is the only channel that is live during an auction. ESPN
drives the draft room over Server-Sent Events whose join token is minted at
runtime and single-use, and it allows **one connection per team** — a second
connection evicts the first. So a headless client cannot sit alongside you; it
would kick you out of your own draft.

The way around that is to read the page you are already drafting in:

```bash
python tools/draft_room_probe.py --launch   # one-time: Chrome on a debug port
# log into ESPN in that window, open your draft room, then:
espn-fantasy watch
```

```python
from espn_fantasy.draftroom import DraftRoomReader
from espn_fantasy.watch import DraftRoomWatcher, TeamResolver, Sale

reader = DraftRoomReader(port=9222)
watcher = DraftRoomWatcher(reader, resolver=TeamResolver(team_names, team_ids))

for event in watcher.events():
    if isinstance(event, Sale):
        print(event.player, event.price, event.team_id)
```

It attaches read-only and never opens a connection of its own, so it cannot
evict you.

## Tools

Diagnostics, not part of the library — nothing under `src/` imports them.

| Tool | What it's for |
|---|---|
| `tools/espn_probe.py` | Dump and analyse raw payloads. Works offline on saved JSON or a DevTools HAR. |
| `tools/draft_room_probe.py` | Launch a debuggable Chrome; catalogue what the draft room exposes. |
| `tools/draft_watch.py` | A local page showing raw vs. parsed snapshots side by side, flashing what changed. |
| `tools/anonymize_capture.py` | Strip other managers' member SWIDs out of a capture before you commit or share it. |

```bash
python tools/espn_probe.py --analyse saved.json          # no network, no cookies
python tools/espn_probe.py --har practice_draft.har      # what the room actually fetched
python tools/draft_watch.py --fixture tests/fixtures/espn/draftroom_snapshot_live.json
```

**Before sharing any capture**: `client.scrub` removes *your* credentials only.
Other league members' account ids are still in the payload. Run
`tools/anonymize_capture.py` over anything that leaves your machine.

## Tests

```bash
pip install -e '.[dev]' && pytest
```

124 tests, all offline. The fixtures in `tests/fixtures/espn/` were captured
from a real 12-team salary-cap league and anonymized — synthetic member ids,
placeholder team names, placeholder league id. Field names, types, nesting and
the id gaps are faithful to what ESPN actually returned. See
`tests/fixtures/espn/README.md` for what each one pins.

## Caveats

This rides on an undocumented API that ESPN can change without notice, and on
CSS class names in a React app that can change with any deploy. Both have been
stable, neither is guaranteed. The tests are the early-warning system: if the
fixtures drift from reality, the parsers are wrong and nothing else will catch
it until draft day.

Not affiliated with or endorsed by ESPN.

## License

MIT.
