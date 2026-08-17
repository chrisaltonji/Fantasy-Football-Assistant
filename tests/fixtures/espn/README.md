# ESPN payload fixtures

Structure captured from a real 12-team salary-cap league on 2026-08-17, with
**identifiers anonymized** — member SWIDs are synthetic, team names are
placeholders, and the league name is fake. Field names, types, nesting, and the
id gaps are faithful to what ESPN actually returned.

These exist because **the development sandbox cannot reach ESPN** — the network
policy denies `fantasy.espn.com` and `lm-api-reads.fantasy.espn.com` at the
proxy. CP5's adapter is therefore built and tested entirely offline against
these files. If they drift from reality, the adapter is wrong and nothing will
catch it until draft day.

| File | What it pins |
|---|---|
| `mSettings_auction.json` | `draftSettings.auctionBudget` and `type` — the two fields `espn-api`'s `BaseSettings` drops, so config bootstrap must read raw. Plus `lineupSlotCounts`. |
| `mDraftDetail_predraft.json` | The pre-created 180-pick skeleton, before anyone drafts. |
| `mDraftDetail_inprogress.json` | Same skeleton with three picks filled. Hand-authored; corrected against the 2025 capture. |
| `mDraftDetail_completed_auction.json` | **A real, finished auction** — the 2025 season of this same league. 180 filled picks, real bids. |
| `mTeam_predraft.json` | Team/owner shape, including non-contiguous ids. |

Captures are anonymized by `tools/anonymize_capture.py`, which maps every member
SWID to a synthetic one. Run it on anything before committing it — `espn_probe`'s
own `scrub` only removes *your* credentials, not the other eleven managers'.

## What these encode that isn't obvious

**Team ids are not contiguous.** The source league runs `1-5, 7-13` — there is
no team 6. A league that ever removed and re-added a team leaves a hole.
Assuming `range(1, count+1)` invents a phantom team *and* drops a real one,
silently. The fixtures keep the gap so any code making that assumption fails a
test instead of failing live.

**Identity is `primaryOwner` (the member SWID), never `name`.** Managers rename
their teams constantly, sometimes mid-draft. Team names are in the fixture as
`Volatile Team Name N` specifically so anything keying off them looks obviously
wrong.

**No per-team auction budget field exists.** `transactionCounter.acquisitionBudgetSpent`
is the FAAB *waiver* budget — a different pot entirely. Remaining auction
budget has to be derived from summing that team's `bidAmount`s, which is what
`projections.remaining_budget` already does.

**The draft skeleton is pre-created before anyone bids.** All 180 picks exist
up front with `playerId: -1`, `teamId: -1`, `bidAmount: 0`. So a pick is
*filled* when `playerId != -1` — polling must diff on that, not on the length
of `picks[]`, which never changes.

**Nomination order is known in advance.** `draftSettings.pickOrder` plus each
pick's `nominatingTeamId` means the whole nomination sequence is readable
before the draft starts. The advisory capability spec flagged this as an open
Phase-1 question; it's answered, and capability 4 (nomination strategy) is
unblocked.

## Practice Draft picks are not at `segments/0` — observed 2026-08-17

With a Practice Draft several picks along in the real league,
`/segments/0/leagues/<id>?view=mDraftDetail` returned a payload
**byte-identical to the pre-draft baseline** — all 180 picks still
`playerId: -1`, `bidAmount: 0`, `teamId: -1`, and `inProgress` still `false`.

**State the finding narrowly.** What this shows is that Practice Draft picks do
not land in the `segments/0` draft detail for that league id. It does *not*
show they're unreachable — ESPN's own draft room renders those picks from
somewhere, so an endpoint carrying them exists. Candidates, untested:

- a different **segment id** (the path is `/segments/0/`; practice may use another)
- a **shadow league id** minted for the practice draft, visible in the draft-room URL
- a **different view** or a draft-specific service the room's client calls
- a real-time channel the REST views only mirror after completion

The probe can now chase every one of those (`--segment`, `--history`, `--url`,
`--candidate-league-id`, `--sweep`, `--har`), but nothing here has been run
against ESPN yet — the sandbox can't reach it. Until a capture lands, treat all
four as untested.

Two ways to settle it, cheapest first. The league ran an **auction in a prior
season**, so `--year 2025 --view mDraftDetail` should return a completed real
auction with real bids and no new code. Failing that, DevTools → Network while a
practice draft runs, saved as HAR with content, then `--har`: whatever the draft
room fetches *is* the answer.

## `bidAmount` populates — observed 2026-08-17, from the 2025 season

Captured with `--year 2025`. A finished auction, 180/180 picks filled, every
`bidAmount` non-zero, $1–$75, $2,389 total. **B1's first half is closed** and
`mDraftDetail_completed_auction.json` is the proof.

What that capture taught us beyond the headline:

**`memberId` exists on picks — but not on all of them.** Present on all 173
human picks, absent on all 7 ESPN autodrafted ones (`autoDraftTypeId: 2`).
Nothing may key on it unconditionally; `teamId` is the reliable anchor. This
bites specifically on draft day: a nomination clock that expires produces an
autodrafted pick with no member on it. The hand-authored `inprogress` fixture
had omitted `memberId` entirely — exactly the fixture drift this file warns
about, caught only because a real capture finally existed.

**Every autodraft went for exactly $1**, but they are not all bench slots —
4 BE, plus a WR, a K and a DST. Autodraft means "nobody bid", not "filler slot".

**Teams spend essentially everything.** Per-team totals ran $195–$200 of $200;
the league left $11 of $2,400 on the table. The market clears at ~99.5% of the
money in the room, which is what makes inflation modelling worth doing.

**The id gap is real and durable.** 2025 shows the same `1-5, 7-13` with no team
6, independently confirming it across two seasons.

**`pickOrder` is a full 12-team permutation**, readable before a draft starts.

## Still unverified

**Whether the real draft updates `mDraftDetail` live, or only on completion.**
This is the bigger risk of the two and is equally untested. If ESPN's draft
room is driven by a real-time channel and the REST view is written only at the
end, live polling gets nothing on draft day regardless of what `bidAmount`
does. `price=None` and manual entry are first-class throughout the engine
precisely because of this.
