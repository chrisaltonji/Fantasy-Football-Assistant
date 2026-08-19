# ESPN payload fixtures

Structure captured from a real 12-team salary-cap league on 2026-08-17, with
**identifiers anonymized** — member SWIDs are synthetic, team names are
placeholders, the league name is fake, and the league ids are placeholders.
Field names, types, nesting, and the id gaps are faithful to what ESPN actually
returned.

These exist because ESPN publishes no schema and has no staging environment.
They are the only contract this package has. **If they drift from reality, the
parsers are wrong and nothing will catch it until draft day.**

| File | What it pins |
|---|---|
| `mSettings_auction.json` | `draftSettings.auctionBudget` and `type` — the two fields `espn-api`'s `BaseSettings` drops. Plus `lineupSlotCounts`. |
| `mTeam_predraft.json` | Team/owner shape, including non-contiguous ids. |
| `mDraftDetail_predraft.json` | The pre-created 180-pick skeleton, before anyone drafts. |
| `mDraftDetail_inprogress.json` | Same skeleton with three picks filled. Hand-authored; corrected against the 2025 capture. |
| `mDraftDetail_practice_live.json` | Captured *during* a running draft. Looks identical to the pre-draft baseline. |
| `mDraftDetail_practice_sandboxed.json` | The real league's view while a practice draft ran elsewhere. |
| `mDraftDetail_completed_auction.json` | **A real, finished auction** — the 2025 season of this league. 180 filled picks, real bids. |
| `kona_player_info_slice.json` | Player universe shape: `ownership.auctionValueAverage`, ADP, projection stats. |
| `draftroom_snapshot_live.json` | What `SNAPSHOT_JS` returned from a live draft room, 80 picks in. |

Captures are anonymized by `tools/anonymize_capture.py`, which maps every member
SWID to a synthetic one. Run it on anything before committing it — the client's
own `scrub` only removes *your* credentials, not the other managers'.

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
budget has to be derived by summing that team's `bidAmount`s.

**The draft skeleton is pre-created before anyone bids.** All 180 picks exist
up front with `playerId: -1`, `teamId: -1`, `bidAmount: 0`. So a pick is
*filled* when `playerId != -1` — polling must diff on that, not on the length
of `picks[]`, which never changes.

**Nomination order is known in advance.** `draftSettings.pickOrder` plus each
pick's `nominatingTeamId` means the whole nomination sequence is readable
before the draft starts.

## A live draft returns the pre-draft payload — observed 2026-08-17

`mDraftDetail_practice_live.json` was captured from the **shadow league id** a
practice draft runs under, with two players already bought and the draft
genuinely in progress. All 180 picks still read `playerId: -1`, `bidAmount: 0`,
`teamId: -1`. Sixty polls over fifteen minutes never showed one.

The draft room is driven by a Server-Sent Events stream at
`fantasydraft.espn.com`, and the REST view is not written while the draft runs.
Polling `mDraftDetail` on draft day returns this exact payload for three hours.
`inProgress: true` is the only thing distinguishing it from a draft that has
not started.

## `bidAmount` populates afterwards — observed 2026-08-17, from the 2025 season

Captured with `--year 2025`. A finished auction, 180/180 picks filled, every
`bidAmount` non-zero, $1–$75, $2,389 total.

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
money in the room.

**The id gap is real and durable.** 2025 shows the same `1-5, 7-13` with no team
6, independently confirming it across two seasons.

**`pickOrder` is a full 12-team permutation**, readable before a draft starts.

## Still unverified

**Whether the real league's `mDraftDetail` backfills when the real draft
completes.** A practice draft cannot answer it: the shadow league it runs under
is reaped the moment the draft ends and returns 404 afterwards, so there is
structurally nothing to go back to. The real league is not ephemeral, so it may
well populate the way the 2025 season did — but only draft day can settle it.
