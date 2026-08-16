# Fantasy Football Assistant

A live companion for an ESPN **auction** draft. When a player gets nominated,
it tells you what to bid and who's actually going to fight you for him —
factoring in every rival's remaining budget *and* whether they still need that
position. A team with $90 left that's already full at RB is not a threat on an
RB, and the tool should say so.

Built to degrade gracefully: if ESPN's API gives us nothing on draft day, you
type picks in by hand and every downstream calculation works identically.

> **Status: Checkpoint 1 of 5.** The scaffold, config layer, and the ESPN probe
> are done. The draft REPL, reference data, advisory engine, simulator, and the
> production ESPN adapter land in later checkpoints. See
> [Roadmap](#roadmap) below.

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

## Design in one page

**There is no "API mode" and no "manual mode."** There are event *producers*,
and every individual field carries its own provenance:

```
Sourced[T] = { value | None, provenance, confidence, observed_at }
Provenance = MANUAL > ESPN_API > SIM > INFERRED > UNKNOWN
```

`value=None` is legal and meaningful — *"a sale happened, we don't know the
price."* One merge function (higher provenance wins; same provenance, newer
wins; **a `None` never overwrites a known value**) is the entire fallback
feature. ESPN can tell us *who won* while you type *for how much*, on the same
pick, and a later poll can never stomp your correction.

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
| 2 | State engine, event journal, manual entry REPL, undo, crash-resume | next |
| 3 | Reference data loader + advisory layer | needs your rankings files |
| 4 | Auction simulator with budget-aware bots (the real acceptance test) | |
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
