"""Reading `mDraftDetail` — the draft board, with all its traps.

**Read this before building anything on it.** The REST draft view is not what
it appears to be, and each of the following was learned the expensive way,
from real captures rather than from documentation:

1. **The board is pre-created before anyone drafts.** All 180 picks exist up
   front with `playerId: -1`, `teamId: -1`, `bidAmount: 0`. So `len(picks)`
   says nothing about progress, and polling must diff on *which picks are
   filled*, never on the length of the array — which never changes.

2. **It is not written while a draft runs.** Sixty polls over fifteen minutes
   of active drafting returned `inProgress: true` and `0/180` filled, the
   whole time. Worse, that payload is byte-identical to "the draft has not
   started yet", so the failure is completely silent. For live prices, use
   `espn_fantasy.draftroom` instead.

3. **`bidAmount` does populate — afterwards.** A completed auction returns
   every pick with a real bid ($1–$75 in the capture this was built against).
   So this view is excellent for post-mortems and prior seasons, and useless
   during the event.

4. **`memberId` is not on every pick.** It is present on human picks and
   absent on autodrafted ones (`autoDraftTypeId == 2`). Nothing may key on it
   unconditionally; `teamId` is the reliable anchor. This bites precisely on
   draft day, when an expired nomination clock produces exactly that pick.

5. **The nomination order is published in advance.** `nominatingTeamId` is
   filled on all 180 picks before anything sells, and `draftSettings.pickOrder`
   is a full permutation of the league. The entire nomination sequence is
   readable before the draft starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

# ESPN's sentinel for "this slot has not been filled yet".
UNFILLED = -1

# `autoDraftTypeId` values seen in real payloads. 2 is ESPN's autodraft — the
# nomination clock expired and nobody bid.
AUTODRAFT_TYPE = 2


@dataclass(frozen=True)
class DraftPick:
    """One pick off the board.

    `bid_amount` is `None` rather than `0` when ESPN reported no bid. Zero is
    a real price and would be believed; `None` says "not known", which is the
    accurate statement about a board that has not been written yet.
    """

    overall_pick_number: int
    round_id: int
    round_pick_number: int
    team_id: int
    player_id: int
    bid_amount: int | None = None
    nominating_team_id: int | None = None
    lineup_slot_id: int | None = None
    member_id: str | None = None
    keeper: bool = False
    autodrafted: bool = False

    @property
    def filled(self) -> bool:
        return self.player_id != UNFILLED


@dataclass(frozen=True)
class DraftBoard:
    """The whole draft, and what state ESPN says it is in."""

    picks: tuple[DraftPick, ...] = ()
    in_progress: bool = False
    drafted: bool = False
    complete_date: int | None = None
    draft_type: str = ""
    auction_budget: int | None = None
    pick_order: tuple[int, ...] = ()

    @property
    def filled(self) -> tuple[DraftPick, ...]:
        return tuple(p for p in self.picks if p.filled)

    @property
    def has_prices(self) -> bool:
        return any(p.bid_amount for p in self.filled)

    def spend_by_team(self) -> dict[int, int]:
        """Total spent per team. Only meaningful once prices have landed."""
        out: dict[int, int] = {}
        for pick in self.filled:
            if pick.bid_amount:
                out[pick.team_id] = out.get(pick.team_id, 0) + pick.bid_amount
        return dict(sorted(out.items()))

    def nomination_order(self) -> tuple[int, ...]:
        """Nominating team per pick, in pick order.

        Readable before a draft starts, which is the useful part.
        """
        return tuple(
            p.nominating_team_id
            for p in sorted(self.picks, key=lambda p: p.overall_pick_number)
            if p.nominating_team_id is not None
        )

    def describe(self) -> str:
        """A one-line summary that distinguishes the states that look alike."""
        total, filled = len(self.picks), len(self.filled)
        if not total:
            return "no draft board in this payload"
        if self.drafted:
            money = (
                f", ${sum(self.spend_by_team().values())} spent"
                if self.has_prices else ", no bid amounts"
            )
            return f"draft complete: {filled}/{total} picks{money}"
        if self.in_progress and not filled:
            return (
                f"ESPN says the draft is in progress but reports 0/{total} filled "
                "picks — this view is not written during a live draft, so this "
                "is expected and tells you nothing about the real board"
            )
        if self.in_progress:
            return f"draft in progress: {filled}/{total} picks filled"
        return f"draft not started: {filled}/{total} picks filled (skeleton of {total})"


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def pick_from_payload(raw: Mapping[str, Any]) -> DraftPick:
    bid = raw.get("bidAmount")
    member = raw.get("memberId")
    return DraftPick(
        overall_pick_number=_int(raw.get("overallPickNumber")) or 0,
        round_id=_int(raw.get("roundId")) or 0,
        round_pick_number=_int(raw.get("roundPickNumber")) or 0,
        team_id=_int(raw.get("teamId")) if raw.get("teamId") is not None else UNFILLED,
        player_id=(
            _int(raw.get("playerId")) if raw.get("playerId") is not None else UNFILLED
        ),
        # Zero means "no bid recorded", which on an unfilled or unwritten board
        # is ignorance, not a free player.
        bid_amount=int(bid) if isinstance(bid, (int, float)) and bid else None,
        nominating_team_id=_int(raw.get("nominatingTeamId")),
        lineup_slot_id=_int(raw.get("lineupSlotId")),
        member_id=str(member) if member else None,
        keeper=bool(raw.get("keeper")),
        autodrafted=raw.get("autoDraftTypeId") == AUTODRAFT_TYPE,
    )


def board_from_payload(payload: Mapping[str, Any]) -> DraftBoard:
    """Parse an `mDraftDetail` payload. Pure — no network."""
    detail = payload.get("draftDetail") if isinstance(payload, dict) else None
    if not isinstance(detail, dict):
        return DraftBoard()

    settings = (payload.get("settings") or {}) if isinstance(payload, dict) else {}
    draft_settings = settings.get("draftSettings") or {}
    budget = draft_settings.get("auctionBudget")
    order = draft_settings.get("pickOrder") or ()

    return DraftBoard(
        picks=tuple(
            pick_from_payload(p)
            for p in (detail.get("picks") or [])
            if isinstance(p, dict)
        ),
        in_progress=bool(detail.get("inProgress")),
        drafted=bool(detail.get("drafted")),
        complete_date=_int(detail.get("completeDate")),
        draft_type=str(draft_settings.get("type") or "").upper(),
        auction_budget=int(budget) if isinstance(budget, (int, float)) else None,
        pick_order=tuple(int(t) for t in order if isinstance(t, (int, float))),
    )


def new_picks(
    previous: Iterable[DraftPick], current: Iterable[DraftPick]
) -> tuple[DraftPick, ...]:
    """Filled picks present now that were not filled before.

    Keyed on the pick number, because the array is the same 180 entries every
    poll — a pick becomes news when it stops being a sentinel, not when the
    array grows, which it never does.
    """
    seen = {p.overall_pick_number for p in previous if p.filled}
    return tuple(p for p in current if p.filled and p.overall_pick_number not in seen)


def fetch_board(client: Any, **kwargs: Any) -> DraftBoard:
    """Fetch and parse the draft board in one call."""
    return board_from_payload(client.draft_detail(**kwargs))


def key_union(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every key appearing on any record. For inspecting an unfamiliar payload."""
    keys: set[str] = set()
    for record in records:
        keys.update(record.keys())
    return sorted(keys)
