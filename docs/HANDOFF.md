# Handoff — ESPN Live Auction Draft Assistant

State of the build as of **2026-08-17**. Draft day is **2026-08-31, 8pm ET** —
two weeks out.

Branch: `claude/plan-file-review-g05z77`. 793 tests pass. Everything below is
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

## Done: CP1–CP5

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

**CP4 — simulator.** `ffa sim` runs a full budget-aware auction into a real
journal, with fault injection. Details under Next.

**CP5 — live ESPN reader.** `ffa draft --source espn` reads the draft room you
are drafting in, over CDP. Plus `ffa config init` and `ffa data fetch`.

You can run a complete draft by hand today, with live bid guidance, and crash
and resume it exactly — you can rehearse one end to end without ESPN, and you
can run one live against a real ESPN draft.

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

## B1, first half: ANSWERED — `bidAmount` populates

Observed 2026-08-17 by reading this league's **2025** season, which was also an
auction. `--year` was always parameterized, so it took one command and no code:

```
python tools/espn_probe.py --year 2025 --view mDraftDetail
```

180/180 picks filled, every `bidAmount` non-zero, $1–$75, $2,389 total. The
live-price path is viable. Committed anonymized as
`tests/fixtures/espn/mDraftDetail_completed_auction.json` — the first real
filled picks this build has ever seen.

Three things that capture taught us that nothing else could have:

- **`memberId` is on picks, but not all of them.** All 173 human picks have it;
  all 7 autodrafted ones (`autoDraftTypeId: 2`) do not. Never key on it
  unconditionally — `teamId` is the anchor. On draft day an expired nomination
  clock produces exactly this shape.
- **Teams spend ~everything.** $195–$200 each, $2,389 of $2,400 league-wide. The
  market clears at ~99.5%, so hoarding is not a strategy the field plays.
- **The 2025 draft is 180 real auction prices** for this exact league — genuine
  market data for the inflation model and the CP4 simulator, not invented
  samples.

`previousSeasons` lists 2021–2024, so four more auctions are readable the same
way if more data is wanted.

## B1, second half: ANSWERED — the draft room does not use REST

Observed 2026-08-17, during a live practice draft, by reading the draft-room
tab's own network activity.

**ESPN's draft room is driven by Server-Sent Events.**

```
fantasydraft.espn.com/game-1/league-<SHADOW_ID>/sse/JOIN
  ?1=1 &2=<shadow league id> &3=<teamId> &4=<SWID> &5=<63-char token>
  &6=false &7=false &8=KONA &nocache=<int>
```

The supporting call is
`.../segments/0/leagues/<SHADOW_ID>/teams/<teamId>/draftSecurity`, which returns
a bare integer token.

**REST is not written while a draft runs.** With two players bought and the
draft actively in progress, `mDraftDetail` returned `inProgress: true` and
`0/180` filled picks across **60 polls over 15 minutes**, unchanged. While the
draft room played, it made *zero* `apis/v3` requests — only a player headshot.

**Two of the three untested candidates are now settled:**

- **Shadow league id — CONFIRMED.** The practice draft runs under a different
  league id, sitting in plain sight in the draft-room URL. The real league sat
  at `inProgress: false` with an untouched skeleton throughout — 30 polls, no
  change. Practice picks were never "sandboxed"; they were in another league
  nobody had looked at.
- **Real-time channel — CONFIRMED.** SSE, as above.
- **Different segment id — DEAD.** Segments 1 and 2 return HTTP 202.

### What this means for CP5

**Polling `mDraftDetail` on draft day returns an empty skeleton for three
hours.** The CP5 plan as written would have produced nothing, and would have
failed silently — the payload is indistinguishable from "the draft has not
started". Finding this on the 17th rather than the 31st is the entire reason
the probe exists.

The good news is that SSE is plain HTTP: `requests.get(stream=True)` consumes
it, no websocket dependency. The connect sequence is known. CP5's poller
becomes an SSE reader, and `EventSource`/`SourceHealth` already model a
long-lived producer that can drop and reconnect.

Two things REST *does* still give us, both useful:

- `nominatingTeamId` on all 180 picks **before anything sells** — the whole
  nomination sequence, readable in advance.
- The completed draft, after the fact (2025 proves the finished shape).

`tests/fixtures/espn/mDraftDetail_practice_live.json` pins the live-but-empty
payload so CP5 cannot mistake it for a pre-draft baseline.

### Still open

**Does REST backfill when a draft completes?** Untested. If it does, it is a
usable end-of-draft reconciliation path and a safety net for anything the SSE
stream dropped. If it does not, SSE is the only source of truth and there is no
fallback. This is now the last unanswered ESPN question.

**The trap, unchanged and now vindicated:** an unfilled skeleton is
indistinguishable from "ESPN never populates prices". The probe reports
**INCONCLUSIVE** on zero filled picks rather than concluding anything, and that
is exactly what it did against the live draft — correctly refusing to call it
either way. Preserve that behavior.

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
| A1 | ~~FantasyPros auction values~~ **DONE 2026-08-17 — and no longer a user task.** `ffa data fetch` pulls ESPN's own `ownership.auctionValueAverage` for 355 players. Arguably the better source: the league drafts on ESPN, so ESPN's consensus is what this room actually pays. | — |
| A4 | ~~ESPN cookies~~ **DONE 2026-08-17** — `.env` is populated and working against the live API | — |
| A5 | ~~Prior-season capture~~ **DONE 2026-08-17.** Still open: a DevTools HAR during a *running* draft, for the live-timing half | B1 second half |
| C1 | **Owner dossiers** — the intake is **built** (2026-08-17); the ~20–30 min interview across 12 managers is now yours to run: `ffa dossier form`, then `ffa dossier interview` | Capability 2's bid forecasting reads them; v1 |
| C2 | **Draft strategy preset** — one archetype | v1.1 |

`config/league.toml` holds real league id, member SWIDs, and manager nicknames.
It is **gitignored and lives only in the ephemeral container** — copy it out or
regenerate it from the captured settings.

---

## Next

**CP4 — simulator. DONE 2026-08-17.** `ffa sim --seed 42 --drop-prices 0.3`
runs a full auction into a real journal. Budget-aware bots in five archetypes
bid against the same `projections` the advisory layer reads, so a bug in
`max_legal_bid` or `open_slots_by_pos` shows up as a visibly illegal draft
instead of a draft-day surprise. `SimSource` satisfies `EventSource` — there is
no `if simulating:` anywhere in the engine.

Clearing price is second-price-plus-one, the way an English auction actually
ends. A clean run fills 12/12 rosters with no warnings and no overspend, and a
seed reproduces a run exactly.

**Two things the simulator found immediately:**

1. **Bidding on floored budget information over-commits.** `remaining_budget`
   charges an unknown price at the $1 minimum, which *over*-estimates what a
   team has left. With `--drop-prices 0.3`, teams bid against that inflated
   figure and finish over budget once the real prices land — the engine flags
   it (`mgr1 is $8 over budget`) rather than corrupting state, which is correct,
   but it means **our own** `max_legal_bid` is unsafe whenever our own prices
   are incomplete. The $1 floor is the safe direction for judging a rival's
   ammunition and the dangerous direction for setting our own ceiling.
   `Threat.remaining_is_floor` exposes this for rivals; `BidGuidance` does not
   expose it for us. Worth closing before draft day.

2. **Truncating the player pool by value strands money.** Kickers and defenses
   are the cheapest rows in any auction-value export, so a pool capped at
   "enough bodies" cuts them first, teams cannot fill K/DST, and the run looks
   like a bidding bug. The pool is now the whole board.

Bots clear ~92% of the money vs. the real 2025 auction's 99.5%. The gap is
structural, not a tuning failure: a bot outbid early cannot retroactively
reallocate, so it fills its roster cheaply and carries cash. Raising the
aggression clamps changes nothing, which is how we know.

**CP5 — live ESPN reader. DONE, and rehearsed live on 2026-08-17.**

Three commands, in the order you use them:

```
python tools/draft_room_probe.py --launch   # one-time: a Chrome with the debug port
ffa config init                             # rebuild config/league.toml from ESPN
ffa config nicknames                        # one-time: names you can type
ffa data fetch                              # 355 real ESPN auction values
ffa draft --new --source espn               # draft
```

`src/ffa/ingest/espn/` attaches to a Chrome started with
`--remote-debugging-port` and reads the draft room's DOM. Not an SSE client and
not a poller — `docs/ESPN_DATA_ACCESS.md` has the catalogue and the three
observations that rule both out. The short version: ESPN allows **one
connection per team**, so any client of our own would evict you from your own
draft. Reading the page you are already in is the only non-disruptive channel.

`tools/draft_watch.py` serves a local page showing raw scrape next to parsed
snapshot, for confirming the read with your own eyes.

### What the live rehearsal proved, and what it found

Picks land with correct prices and correct team attribution; `budgets` is
readable under time pressure; and ESPN's own draft room displayed
`MANUAL OFFER (MAX $33)` at a moment when `max_legal_bid` independently
computed **$33** — the roster arithmetic confirmed against ESPN's number rather
than our own tests.

Bugs it found that every test had passed over, because they only existed live:

- **`advice` was dead.** The reader emitted only completed sales, so the engine
  never knew who was on the block and the headline capability never fired.
  Fixed — `.player-selected` now yields a `PlayerNominated`.
- **Team attribution was wrong.** Picks were credited by board position + 1.
  The DOM carries no team id and its columns are in *draft* order — team 3 sits
  in column 1 — so it invented a team 6 and never mentioned team 13. Fixed via
  `TeamResolver`, joining on team name, with a pre-flight audit at attach time
  that refuses to start on a mismatch.
- **Playwright's sync API is not thread-safe.** Connecting on the main thread
  and polling from the worker failed on every poll. The reader connects lazily
  on the thread that uses it.

## Draft history: four prior auctions, read (2026-08-18)

`ffa history fetch` then `ffa history report`. `src/ffa/history/` pulls every
season ESPN still serves, and `docs/draft_history.html` is the result.

**What is actually there**, and it is not what `previousSeasons` implies:

| season | verdict |
|---|---|
| 2018–2020 | HTTP 202 — the league did not exist |
| 2021 | present, but `type=OFFLINE` and 10 teams: no prices, no nominators |
| 2022–2025 | **AUCTION, 12 teams, 180/180 filled and priced, `nominatingTeamId` on every pick** |

So four usable seasons, 720 real auction prices, and the nomination order for
all of them. Gaps are recorded as `SeasonGap` with a reason rather than dropped —
"no data for 2021" and "2021 was a ten-team offline draft" support very
different conclusions and only one is true.

**The price reference is a different ESPN field in different seasons**, which is
the trap here. `ownership.auctionValueAverage` covers 348 players in 2022 and
comes back as a **column of zeroes in 2025**; `draftRanksByRankType.PPR.
auctionValue` covers the top 160 but is absent in 2022. Trusting the first field
blindly would price 2025 at zero and report that every manager massively
overpaid for everybody. `price_reference` merges both (consensus wins where they
overlap) and the result is **scaled to the money actually spent that season**, so
a season covered at 46% and one at 82% can sit in the same table. Coverage and
source are printed per season at the top of the report, above any finding.

**Every classification is league-relative**, scored against that season's field
at 0.6σ. An absolute rule encodes what auctions look like in general; the only
question that matters is who does something more than the people bidding against
him. Two metrics needed care:

- **Best-available nomination is degenerate as a raw number.** Every manager
  scores ≈ +0.9 on order-vs-price correlation, because auctions run in price
  order by construction. It only discriminates as a z-score.
- **Homerism needs a surplus, not a rate.** Three players from one team is a
  coincidence; the test is picks ≥ 3, index ≥ 1.8, *and* at least 2 more than
  the league's own rate would hand you. The mirror test (`avoids`) is
  deliberately weak and labelled as such — with 32 NFL teams and ~59 picks per
  manager, the most-drafted team has an expectation of 2.9, so it can never be
  more than suggestive.

`ffa history report` also writes `docs/dossier_suggested.json` — the archetypes
in the dossier interchange format. It is **never written into the record**: a
measurement and an observation are different things, and the file is a draft to
argue with, which is also the fastest way to run the interview. Labels that did
not hold in more than half the seasons are omitted, and `random` is never
offered as a finding because it is the residual bucket, not a discovery.

---

## Known debt, ranked by draft-day risk

**Would bite during a draft — all four CLOSED 2026-08-17**

Every one of these was found by running the thing rather than by testing it,
and every one was invisible from inside the tool: the failure mode in each case
is a draft that looks like it is working.

1. ~~**Async output collides with typing.**~~ **DONE.** `src/ffa/cli/console.py`
   takes the echo away from the terminal. `RawConsole` reads one key at a time
   and prints the characters itself, so it always knows what is on the input
   line — and can wipe it, print the pick, and put your half-typed command back
   underneath. Both `readline` and `notify` take one lock, which also fixes the
   source thread printing straight at the screen from off the main thread.

   Two deliberate limits. It engages **only when both stdin and stdout are a
   terminal** — a piped transcript keeps the old byte-for-byte behaviour, which
   is what every integration test drives — and a console it cannot take over
   falls back rather than failing, because line editing is a comfort and
   recording the draft is not. It uses **no ANSI**: erasing is spaces and
   carriage returns, since Windows consoles need virtual-terminal processing
   switched on first and a draft is the wrong place to find out this one did
   not have it.

   Ctrl-C now lands in the key reader instead of nowhere, and both it and EOF
   drain the queue on the way out — a pick already in hand must not be dropped
   by the exit.

2. ~~**Multi-tab draft room.**~~ **DONE.** `_find_page()` collects every
   matching tab and **refuses on more than one**, listing the URLs. Picking the
   first was a coin flip whose failure is silent: both tabs attach cleanly and
   both pre-flight `matched 12/12`, because they are the same league, so reading
   the wrong one is indistinguishable from a draft that has not started.
   `ffa draft --tab <text from the URL>` narrows it when two really are open.

3. ~~**No stale-board guard.**~~ **DONE.** `_check_board_is_live` refuses a new
   draft against a complete board — that is last season's draft or a practice
   room, and starting anyway pours 180 finished picks into a fresh journal as if
   they were happening now. `--allow-finished-board` overrides; `--resume` into a
   finished board stays silent and normal. A **partly** filled board still
   starts, because attaching mid-draft is worth doing, but says how many picks
   it is about to record.

4. ~~**Manager nicknames are ESPN usernames.**~~ **DONE — and the root cause
   was ours, found 2026-08-18.** ESPN's `mTeam` sends `firstName`/`lastName`
   *alongside* `displayName`; `_members_by_id` preferred the handle and threw
   the real name away, so `macurl1392` reached every surface while "Michael
   Curley" sat in the same payload. `teams_from_payload` now returns a
   `TeamDirectory` keeping both — a record rather than a tuple precisely because
   these are different names for the same person and picking the wrong one is
   invisible.

   `[managers]` is now derived from first names, escalating only as far as it
   must: `michael`, then `nickl`/`nickc` on a collision (this league has two
   Nicks and two Andrews), then the full last name, then the handle. `[real_names]`
   carries the full name for display. A candidate that cannot survive
   `check_nickname` is dropped rather than written — a config entry that never
   resolves is worse than none, since the team stays addressable as `t3` either
   way and only one of the two is honest about it.

   `carry_forward` gained `machine_names` so a rebuild does not mistake a stale
   *handle* for a hand-set nickname; `looks_generated` only catches the
   `espn06814226` shape, and without this a config written before the fix would
   pin `macurl1392` forever. `ffa config
   nicknames` walks the twelve teams once — Enter keeps, `-` clears — or takes
   `--set 3=dave` repeatably, or `--list` to just look. Names are validated
   before they are written, not on the next load: no whitespace (the grammar
   splits on it), nothing slot-shaped (`resolve_team` tries `t3`/`team3`/`3`
   first, so a manager called `3` is unreachable), and no duplicates.

   `config init --force` now **carries hand-set nicknames forward**, which
   matters because regenerating is the documented fix for a lost config and
   would otherwise wipe the one thing in that file ESPN cannot give back. It
   drops a nickname when the team changed hands — the owner SWID is who a team
   actually is — and resolves the case where a kept nickname collides with
   ESPN's generated name for a different team, so the rebuild cannot write a
   config that then refuses to load. `config init` and `config check` both say
   which teams are still untypeable.

793 tests pass, up from 578.

**Correctness, not urgency — CLOSED 2026-08-17**

5. ~~`BidGuidance` does not expose `remaining_is_floor` for **our own**
   ceiling.~~ **DONE.** `BidGuidance` now carries `safe_legal_bid` and
   `unknown_prices`, and `max_advisable_bid` is capped by the *safe* ceiling
   rather than the optimistic one.

   `remaining_budget` charges an unknown price at the $1 minimum. That floor is
   the right way to be wrong about a rival — it over-states their ammunition, so
   no bid they make is a surprise — and the wrong way to be wrong about us.
   `safe_legal_bid` restates the same arithmetic with our own unpriced picks
   charged at their inflation-adjusted sheet value, which is the number the tool
   already trusts to advise a bid. A pick with no reference row is left on the
   $1 floor; inventing a value there would be the same failure in the other
   direction.

   `max_legal_bid` is unchanged and still means what it meant. The two ceilings
   this codebase keeps apart do not get a third blurred into them — the readout
   shows `$115+ (~$71 priced)` and says why in a reason line, and `build_view`
   ships both so the dashboard and the LLM layer cannot inherit the bug.

**Tidy-up — CLOSED 2026-08-17**

6. ~~`README.md` is stale.~~ **DONE.** Rewritten against what actually exists:
   the real module tree, the live-draft flow with its three pre-flight refusals,
   `config init` / `config nicknames` / `data fetch`, the safe-ceiling caveat,
   and the probe's `INCONCLUSIVE` behaviour. `docs/HANDOFF.md` is still the
   document to trust for state; the README is now for someone starting from
   scratch.
7. ~~Unused imports and a dangling `"PlayerRef"` annotation.~~ **DONE.**
   `resolve_player` imports `PlayerRef` at module scope and annotates it
   directly, so `get_type_hints` would resolve. `python -m pyflakes src tools
   tests` is clean.

## After CP5

There is no CP6. What remains is product work:

- **C1 — owner dossiers. Intake BUILT 2026-08-17; the interview is yours.**

  `src/ffa/dossier/` is a declarative question set — thirteen questions, all
  optional, each carrying why it is asked — and one table drives the interview,
  the printable form, validation and the readout, so they cannot drift apart.

  ```
  ffa dossier init                    one empty entry per seat
  ffa dossier brief                   a prompt to paste into a chat, and talk
  ffa dossier import answers.md       take back what the chat produced
  ffa dossier interview --team dave   or answer at the prompt
  ffa dossier form                    or fill in a questionnaire
  ffa dossier status                  coverage across the league
  ```

  **`brief` + `import` are the ones to reach for.** 156 prompts is the wrong
  shape for what is really recall, and recall goes better spoken. The brief
  carries the questions, the exact keys each choice will accept, the roster, and
  an output contract; `import` reads a plain `.json` file or a transcript with a
  fenced block in it. The brief's own worked example is imported verbatim by a
  test, so the contract it publishes cannot drift from the one the importer
  accepts.

  `import` is deliberately **not** a looser second door into the record. Every
  value goes through the same `parse_answer` the terminal interview uses; a bad
  value is dropped and named rather than coerced, an absent field is left alone
  (partial passes are the normal case, so treating absence as a clear would make
  every one of them destructive), and an explicit `"unknown"` / `""` / `null` is
  treated as unanswered — something at the far end will eventually fill blanks
  in to be tidy, and storing that would turn a gap into a finding.

  Three decisions worth knowing before changing any of it:

  - **Keyed on the owner SWID, folded.** ESPN hands the same member id out
    braced and bare and users copy whichever they saw; folding at construction
    rather than only at lookup is what stops one person becoming two entries,
    where a sort silently shadows the hand-typed one. The config — never the
    file's own `team_id` — decides who sits where, so a read follows the person
    across a rename, a renumbering, or a season.
  - **Saved after every manager.** A twenty-minute pass must not be lost to a
    stray Ctrl-C at manager ten, and a bad answer costs one question rather than
    the pass.
  - **They reach `build_view()` as observations, never scores.** No `will_bid`,
    no `likelihood`. Everything else in that payload is computed from ground
    truth, so a probability sitting beside it would be read as computed truth.
    The dashboard spec asks for "room for a likelihood annotation per bidder
    without implying the engine supplied it" — this is that room.

  What is left is the part only you can do: sitting down and answering it for
  twelve people. `data/dossiers.json`, `docs/dossier_form.md` and
  `docs/dossier_brief.md` are all gitignored — they are candid written
  opinions about real people.
- **The LLM layer.** `build_view()` was always designed as its input — the
  contract between the engine and every surface. Capacity is arithmetic and is
  built; *intent* is inference and is not. Keep it **beside** the hot path, not
  inside it: `advice` is currently instant and cannot fail, and a network call
  in that loop would add latency and a failure mode while a clock runs.
- **The dashboard.** `docs/dashboard_requirements.md` is a 12.7K spec with zero
  implementation.

**Draft-day gate: MET.** Ran end to end against a live ESPN practice draft on
2026-08-17 — attach, pre-flight, live ingest, correct attribution, bid guidance.
