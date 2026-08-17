"""Who can take this player from us, and how high we should go.

Two numbers that must not be confused:

- **`max_legal_bid`** (CP2, `domain/projections.py`) — the hard roster-arithmetic
  ceiling. Bidding above it is impossible, not unwise.
- **`max_advisable_bid`** (here) — what the numbers say is *worth* paying, given
  the player's reference value, current market inflation, and whether he fills
  a starting slot. Always capped by the legal ceiling.

Threat assessment here is strictly **capacity**: can this rival afford him, and
do they have somewhere to put him. Whether they *will* bid is inference and
belongs to the Claude layer, which reads the dossiers.
"""

from __future__ import annotations

from ffa.advice.market import inflated_value
from ffa.advice.types import BidGuidance, MarketState, Threat
from ffa.domain import projections as proj
from ffa.domain.enums import Position, RosterSlot
from ffa.domain.models import DraftState
from ffa.reference.playerbook import PlayerBook

# A player who only fits on our bench is worth materially less to us than one
# who fills a starting hole, even at identical reference value.
BENCH_DISCOUNT = 0.6
# The suggested range spans this fraction either side of the advisable bid.
RANGE_SPREAD = 0.15


def has_starter_gap(state: DraftState, team_id: int, position: Position) -> bool:
    """Does this team still need a *starter* at this position?"""
    league = state.league
    if league is None:
        return False
    gaps = proj.starter_gaps(state, team_id)
    slots = [s for s in league.slots_for_position(position) if s is not RosterSlot.BE]
    return any(gaps.get(slot, 0) > 0 for slot in slots)


def threats_for(
    state: DraftState, position: Position | None, *, exclude_team: int | None = None
) -> tuple[Threat, ...]:
    """Every rival's capacity to take a player at this position.

    Sorted by the ceiling that actually matters — a live threat with $60 ranks
    above a rich team with no room.
    """
    out: list[Threat] = []
    for team_id in sorted(state.teams):
        if team_id == exclude_team:
            continue
        team = state.teams[team_id]
        openings = (
            proj.open_slots_by_pos(state, team_id).get(position, 0)
            if position is not None
            else proj.open_draftable_slots(state, team_id)
        )
        out.append(
            Threat(
                team_id=team_id,
                label=team.label,
                remaining=proj.remaining_budget(state, team_id),
                max_legal_bid=proj.max_legal_bid(state, team_id),
                remaining_is_floor=proj.unknown_price_count(state, team_id) > 0,
                has_starter_gap=(
                    has_starter_gap(state, team_id, position) if position else True
                ),
                open_at_position=openings,
            )
        )

    return tuple(sorted(out, key=lambda t: (not t.is_live, -t.max_legal_bid, t.team_id)))


def guidance_for(
    state: DraftState, book: PlayerBook, key: str, market: MarketState, *, name: str | None = None
) -> BidGuidance:
    """The deterministic read on one nominated player."""
    league = state.league
    me = league.my_team_id if league else 0
    row = book.get(key)
    position = row.position if row else None

    reference = book.value(key)
    inflated = inflated_value(book, key, market)
    legal = proj.max_legal_bid(state, me) if league else 0
    fills_gap = has_starter_gap(state, me, position) if (league and position) else False

    reasons: list[str] = []
    if reference is None:
        # No reference value: refuse to invent one. The ceiling is all we know.
        advisable = legal
        reasons.append("no reference value for this player — showing the legal ceiling only")
    else:
        base = inflated or reference
        advisable = base if fills_gap else round(base * BENCH_DISCOUNT)
        if not fills_gap:
            reasons.append("would not fill a starting slot for us")
        if market.inflation_ratio and market.inflation_ratio != 1.0:
            reasons.append(f"market running {market.read} ({market.inflation_ratio:.2f}x)")
        advisable = min(advisable, legal)
        if advisable == legal and legal < base:
            reasons.append(f"capped by our legal ceiling of ${legal}")

    advisable = max(0, int(advisable))
    low = max(1, round(advisable * (1 - RANGE_SPREAD))) if advisable else 0
    high = min(legal, round(advisable * (1 + RANGE_SPREAD))) if advisable else 0

    threats = threats_for(state, position, exclude_team=me)
    live = [t for t in threats if t.is_live]
    if live:
        reasons.append(
            f"{len(live)} rival(s) can both afford him and start him; "
            f"top ceiling ${max(t.max_legal_bid for t in live)}"
        )
    elif position is not None:
        reasons.append("no rival both needs this position and can afford him")

    return BidGuidance(
        key=key,
        name=name or (row.name if row else key),
        position=position,
        reference_value=reference,
        inflated_value=inflated,
        max_legal_bid=legal,
        max_advisable_bid=advisable,
        suggested_low=low,
        suggested_high=high,
        fills_starter_gap=fills_gap,
        threats=threats,
        reasons=tuple(reasons),
    )
