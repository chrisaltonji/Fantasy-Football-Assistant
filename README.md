# Fantasy Football Assistant

A live companion for an ESPN **auction** draft. When a player gets nominated,
it tells you what to bid and who's actually going to fight you for him —
factoring in every rival's remaining budget *and* whether they still need that
position. A team with $90 left that's already full at RB is not a threat on an
RB, and the tool should say so.

Built to degrade gracefully: if the browser attach fails on draft day, you type
picks in by hand and every downstream calculation works identically.

> **Status: built and rehearsed.** All five checkpoints are done. It has been
> run end to end against a live ESPN practice draft — attach, pre-flight, live
> ingest, correct team attribution, bid guidance — so the draft-day gate is met.
> What remains is product work, not plumbing: the dashboard, the inference
> layer, and the owner dossiers that layer would read.
>
> **[`docs/BUILD_STATUS.md`](docs/BUILD_STATUS.md) is where things stand** —
> every checkpoint, all twelve capabilities, and what is actually left.
> **[`docs/HANDOFF.md`](docs/HANDOFF.md)** is the running record of the
> decisions behind them and everything learned about ESPN's internals.

---

## Setup

Requires Python 3.11+.

```bash
git clone https://github.com/chrisaltonji/Fantasy-Football-Assistant.git
cd Fantasy-Football-Assistant

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e '.[dev,espn]'

pytest                            # should be all green
ffa --version
```

If you have `uv`, `uv venv && uv pip install -e '.[dev,espn]'` is the faster path.

The `espn` extra is Playwright, used only by the live draft-room reader. Leave
it out and everything else — manual entry, the simulator, replay — still works.
The draft must never be blocked on an install.

---

## Configure your league

```bash
cp .env.example .env              # then fill in your cookies (below)
ffa config init                   # reads the real league settings from ESPN
ffa config nicknames              # names you can type with a clock running
ffa data fetch                    # ESPN's own consensus auction values
ffa config check                  # validates everything and explains what's wrong
```

`ffa config init` writes `config/league.toml` from ESPN's own `mSettings` and
`mTeam` views: budget, roster slots, scoring, and — importantly — the real team
ids. **ESPN team ids are not contiguous.** The league this was built against
runs 1–5 and 7–13, with no team 6; generating `1..count` would invent a team
that does not exist while silently dropping one that does.

`config/league.toml` is gitignored (it holds the real league id, member SWIDs
and your nicknames), so losing it is expected on a new machine and costs one
command. Re-running with `--force` keeps the settings ESPN knows nothing about:
your reference path, your polling settings, and your hand-set nicknames.

`ffa config init` also reads `draftSettings.pickOrder` — **the entire nomination
order, published before anyone drafts.** It is one list of twelve seats that
repeats verbatim every round, so who nominates 137th is knowable on day one. The
order is a shuffle the commissioner sets, so a rebuild deliberately takes ESPN's
fresh copy rather than carrying the old one forward, and an order that does not
match the league's team ids is dropped with a warning rather than half-trusted.

`ffa config check` fails loudly on anything that would produce silently wrong
advice later — a budget too small to fill a roster, a snake-draft league, a poll
interval tight enough to get you rate-limited, an unset `my_team_id`.

### Manager nicknames

ESPN's answer to "who manages team 5" is `espn78078705`. That is what
`[managers]` gets seeded with, and it makes the correction path — the thing you
reach for when a pick lands on the wrong team — into
`sold barkley 62 espn78078705`. Nobody types that twice under pressure.

```bash
ffa config nicknames                       # walk all twelve; Enter keeps, `-` clears
ffa config nicknames --set 3=dave --set 5=mike
ffa config nicknames --list                # just look
```

Names are validated before they are written, not on the next load: no
whitespace (commands split on it), nothing shaped like `t3`/`team3`/`3` (the
slot form always wins, so such a nickname is unreachable), and no duplicates.

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

## Running a draft

### Live, against the ESPN draft room

```bash
python tools/draft_room_probe.py --launch   # one-time: a Chrome with a debug port
# log into ESPN in that window and open your draft room, then:
ffa draft --new --source espn
```

The reader attaches over CDP to the Chrome you are already drafting in and
reads the rendered board. It is **not** an API client and not an SSE client, and
that is a finding rather than a preference:

- The REST view (`mDraftDetail`) is **not written while a draft runs** — sixty
  polls over fifteen minutes of live drafting returned `0/180` filled picks with
  `inProgress: true` throughout. A poller would have produced nothing, silently,
  for three hours.
- The draft room is driven by Server-Sent Events, and ESPN allows **one
  connection per team** — a client of our own would evict you from your own
  draft.

So reading the page you are already in is the only non-disruptive channel.
[`docs/ESPN_DATA_ACCESS.md`](docs/ESPN_DATA_ACCESS.md) has the full catalogue
and the observations behind it.

Before it starts, it refuses on anything that would make the draft silently
wrong:

| It refuses when | Because | Override |
|---|---|---|
| Draft-room team names don't match your config | Picks would be attributed by board position, which is *draft* order, not id order | `--ignore-unmatched-teams` |
| Two draft-room tabs are open | Reading the wrong one attaches cleanly, matches 12/12, and never moves | `--tab <text from the URL>` |
| The board is already complete | That is a finished draft — last season's, or a practice room | `--allow-finished-board` |

A *partly* filled board still starts, because attaching mid-draft is worth
doing. It just says how many picks it is about to record.

`python tools/draft_watch.py` serves a local page showing the raw scrape beside
the parsed snapshot, for confirming the read with your own eyes.

### By hand, or from a rehearsal

```bash
ffa draft --new                        # manual entry; always works
ffa draft --resume                     # pick up the most recent draft
ffa sim --seed 42 --drop-prices 0.3    # a full auction against budget-aware bots
```

Everything is fsynced before the next prompt appears, so Ctrl-C, a closed
laptop, or a dead battery lose exactly nothing. Resume restores the board
precisely.

While a live source is running, the tool takes over the input line so a pick
landing mid-command doesn't garble what you are typing — it wipes the line,
prints the sale, and puts your half-typed command back underneath. That only
happens on a real terminal; piped input keeps plain line-buffered behaviour.

### Commands

| Command | Does |
|---|---|
| `sold <player...> <price\|?> <team> [pos]` | Record a sale. `?` means "sold, price unknown". |
| `nominate <player...> [team]` | Flag who's up for bid. |
| `price <player...> <amount>` | Fill in or correct a price. |
| `amend <entity> <field> <value>` | General correction, e.g. `amend team:3 manager dave`. |
| `undo [#id]` / `redo` | Undo the last event, or a specific one. |
| `advice [player...]` | Bid readout for the nominated player, or anyone you name. |
| `turns` | Nomination order: who is up, when you are, what to put up. |
| `scarcity` / `market` | What's left per position; inflation vs. your sheet. |
| `budgets [team]` | Remaining money, max bid, and starter gaps per team. |
| `state [team\|player]` / `log [n]` | Full board / recent events with their ids. |
| `help [verb]` / `quit` | |

Every verb has short aliases (`s`, `n`, `p`, `u`, `r`, `a`, `b`, `q`) — `help`
lists them.

Player names are free text and multi-word names need no quoting — price and
team are read as the **last two tokens**, so `sold patrick mahomes 45 dave`
parses correctly. Names resolve against your reference file, so `sold halson 45
dave` works too; an ambiguous name is **never** guessed, you get numbered
candidates and retype. A wrong match wears a real player's price and fails
silently, which is the worst thing this tool could do.

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

`ffa data fetch` pulls ESPN's own `ownership.auctionValueAverage` for ~355
players and points `[reference].path` at the result. It is arguably the best
available source: the league drafts on ESPN, so ESPN's consensus is what this
room actually pays.

To use your own export instead, take it from your calculator **at your real
league settings** (teams, budget, scoring, roster), drop it in
`data/reference/`, and point `[reference].path` at it. CSV, XLSX, and pasted
tab- or space-separated text all work — copy-paste is a first-class path, since
CSV export is a paid feature on most sources.

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

A nomination — typed, or read off the live board — fires the readout
automatically. That is the moment it is worth anything, and asking for it costs
seconds you don't have while the auctioneer counts.

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

If one of *your own* picks has no price yet, `legal max` reads
`$115+ (~$71 priced)`. The `+` means the same thing it does on a rival's budget,
but the direction of the error flips: charging an unknown price at $1
over-states the money you have. That is the safe way to be wrong about a rival —
never be surprised by a bid they can make — and the dangerous way to be wrong
about yourself. The simulator found it directly, so advice is capped by the
lower figure and says why.

"Who can take him" counts only rivals who can afford him *and* still start him.
A team with $90 and no open RB slot doesn't appear on an RB — that omission is
the whole point of the tool.

### Nomination order

ESPN publishes the whole nomination order before anyone drafts, so `turns`
answers the question the bid readout cannot: what happens *before* a player is
on the block.

```
> turns
nomination 60 of 180
  on the block  the order says andrewd put him up
  up next       nickc (round 6)
  your turn     in 3 nominations (round 6) - 10 left
  then          nickc, jared, john, b, michael
  past your $3 ceiling, and the room can pay (showing 5 of 64):
    CeeDee Lamb            WR   $70   10 live, up to $186
    De'Von Achane          RB   $68   11 live, up to $186
```

The schedule half needs no auction values and never guesses. If ESPN published
no order, it says so and names nobody — team-id order is not a degraded answer
here, it is a wrong one, and it would put a specific manager on the clock while
you watch a different one on your own screen.

The two candidate lists are deliberately thin, and both are often empty:

- **Free money** — a player who fills a starting hole for you that *no rival can
  both afford and start*. There is nobody to bid him up. Rare, because a FLEX
  slot keeps a rival's RB/WR/TE need open long after their native slot is full.
- **Past your ceiling** — a player who costs more than you can pay and that a
  live rival can actually pay for. Putting him up cannot cost you somebody you
  could have won.

There is no third list ranking what you *should* nominate, because that depends
on a strategy — drain the room, chase your guys, sit on your money — and the
strategy preset does not exist yet. Whether draining a rival is wise is
inference; everything above is arithmetic.

If the board ever credits a nomination to someone the order did not expect, the
readout leads with a warning and tells you to trust the board. The order is a
model of ESPN's schedule; the board is what happened.

### Owner dossiers

Everything above is **capacity**: can this rival afford him, does he have a slot
for him. That is arithmetic, and it is finished. Dossiers are the other half —
**intent**. Will they actually bid, how high, and on whom. It cannot be
computed, only observed, and you are the only person who has observed it.

```bash
ffa dossier init                    # one empty entry per seat, keyed by SWID
ffa dossier brief                   # a prompt you paste into a chat, and talk
ffa dossier import answers.md       # take back what the chat produced
ffa dossier status                  # who is covered, who is not
```

Thirteen questions per manager, all optional, each carrying the reason it is
being asked — how they spend the $200, when their money goes out, whether they
chase past value, which positions and which NFL teams they overpay for, and the
tells only somebody who has drafted with them would know.

**Doing it as a conversation.** Thirteen questions across twelve managers is 156
prompts, and the content is recall — arguing with yourself about what somebody
did in the third round two years ago. That goes better spoken than typed, so
`ffa dossier brief` renders the whole interview as a prompt: paste it into a
chat, talk it through (dictation works fine), and `ffa dossier import` takes the
answers back. The brief tells the far end **not to invent answers**, because an
empty field is honest and a plausible guess gets read as observation.

Import is not a looser second door into the record — every value goes through
the same validation the terminal interview uses. A bad value is dropped and
named rather than rounded into something plausible, and a field that was not
discussed is left exactly as it was, so partial passes are safe.

There are two other ways in, if you prefer them: `ffa dossier interview` answers
at the prompt, saving after each manager so a twenty-minute pass survives a
stray Ctrl-C; and `ffa dossier form` writes a fill-in questionnaire for away
from the keyboard.

Dossiers are keyed on the **owner's SWID**, so they follow the person rather
than the seat: they survive a team rename, a config rebuild, and a league
renumbering between seasons. `data/dossiers.json` is plain JSON and meant to be
opened — fixing a typo should not mean redoing an interview — and it is
gitignored, because it is candid written opinions about people you know.

They reach every surface through `build_view()` as **recorded observations, not
scores**. There is no `will_bid` and no `likelihood`: everything else in that
payload is computed from ground truth, and an engine that shipped a probability
would be claiming to know something it cannot. Weighing them is the inference
layer's job.

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
`max_legal_bid`, `open_slots` are pure functions of state. That kills the whole
class of "budget says $87 but the roster says $92" drift bugs, and it is *why*
undo is a one-line replay filter rather than an inverse operation.

Identity anchors on the owner's SWID, never the team name. Managers rename teams
constantly, including mid-draft, so nothing resolves against a name — except the
one place it cannot be avoided, matching a draft-room board column to a team id,
and that match is audited before the draft starts rather than discovered at
pick 40.

State persists as an **append-only JSONL event journal** (`runs/<id>/`), so undo,
crash-resume, and the audit trail all fall out of one mechanism.

```
src/ffa/
  domain/     enums, sourced, ids, models, events, codec, reducers, projections
  state/      journal, store (single-writer), recovery
  ingest/     source.py (the EventSource seam)
              espn/    draftroom, source, settings, players
              manual/  grammar, resolve, source, errors
  reference/  schema, loader, playerbook
  advice/     market, scarcity, bidding, nomination, types, engine
  sim/        bots, engine, faults, source
  view/       model (the JSON contract every surface reads)
  cli/        app, repl, console, render
  config/     schema, loader, nicknames
tools/        espn_probe, draft_room_probe, draft_watch, anonymize_capture
```

The store, reducers, and advisory engine have **zero knowledge that ESPN
exists**. `ManualSource`, `SimSource` and `DraftRoomSource` all satisfy the same
`EventSource` protocol — there is no `if simulating:` anywhere in the engine.
Adding a web UI is one new `DraftStore` subscriber plus one new `EventSource`.

---

## What's built

| CP | Scope | Status |
|----|-------|--------|
| 1 | Scaffold, config layer, ESPN probe | **done** |
| 2 | State engine, event journal, manual REPL, undo, crash-resume, dashboard contract | **done** |
| 3 | Reference data loader + deterministic advisory layer | **done** |
| 4 | Auction simulator with budget-aware bots (the real acceptance test) | **done** |
| 5 | Live ESPN draft-room reader, `config init`, `data fetch` | **done** |

**Draft-day gate: met.** Ran end to end against a live ESPN practice draft on
2026-08-17. Never run it live for the first time on the day.

Still ahead, and all of it product rather than plumbing: the dashboard
(`docs/dashboard_requirements.md` is a full spec with no implementation) and the
inference layer that reads `build_view()` beside the hot path rather than inside
it.

---

## Diagnostics

`tools/espn_probe.py` is a standalone diagnostic — nothing in the app imports
it. It dumps raw JSON from ESPN's undocumented views and analyses it.

```bash
python tools/espn_probe.py --league-id 123456 --year 2026
python tools/espn_probe.py --year 2025 --view mDraftDetail   # a finished auction
python tools/espn_probe.py --analyse probe_out/some_capture.json   # offline
```

Payloads land in `probe_out/` (gitignored) alongside a plaintext report.
Credentials come from `.env` only, never the command line, so they can't end up
in your shell history.

The behaviour worth preserving: on a board with **zero filled picks** it reports
`INCONCLUSIVE` rather than concluding anything. An unfilled skeleton is
indistinguishable from "ESPN never populates prices", and the probe refusing to
call it either way is what stopped a wrong conclusion during the live practice
draft.

> **The development sandbox cannot reach ESPN.** Its network policy denies
> `fantasy.espn.com` and `lm-api-reads.fantasy.espn.com` at the proxy, so every
> live capture has to happen on your machine. `tests/fixtures/espn/` is the only
> contract the adapter can be built against; if those drift from reality,
> nothing catches it until draft day.

---

## Development

```bash
pytest                    # full suite
pytest tests/unit -q      # fast path
```

Never commit `.env`, `config/league.toml`, or anything under `data/reference/`
— all three are gitignored because they hold either credentials or your private
research.
