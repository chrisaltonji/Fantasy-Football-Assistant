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
| `mDraftDetail_inprogress.json` | Same skeleton with three picks filled, showing what a completed sale looks like. |
| `mTeam_predraft.json` | Team/owner shape, including non-contiguous ids. |

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

## Still unverified

Whether ESPN populates `bidAmount` with the real winning price during a live
auction. `mDraftDetail_inprogress.json` shows what we *expect*; it is a
hypothesis until a real auction is observed. `price=None` is a first-class
state throughout the engine precisely because this may not hold.
