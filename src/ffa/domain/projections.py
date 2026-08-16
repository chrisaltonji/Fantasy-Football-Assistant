"""Everything derivable from state, derived on demand.

No projection is ever stored. A team has no `remaining` field; it has whatever
these functions say it has given the events that survived replay. That is what
makes undo a one-line filter instead of a patch-up operation, and it means
budget and roster can never disagree with each other.

All money is whole dollars, `int`, everywhere.
"""

from __future__ import annotations

from typing import Mapping

from ffa.domain.enums import Position, RosterSlot
from ffa.domain.models import DraftState, PlayerEntity

# The minimum legal auction bid. Used as the floor for a sale whose price we
# know happened but haven't learned yet.
MIN_BID = 1


def spent(state: DraftState, team_id: int) -> int:
    """Dollars we can actually account for."""
    return sum(
        p.price.value
        for p in state.players_for_team(team_id)
        if p.price is not None and p.price.is_known
    )


def unknown_price_count(state: DraftState, team_id: int) -> int:
    """Sales charged to this team whose price we don't know yet."""
    return sum(
        1
        for p in state.players_for_team(team_id)
        if p.price is None or not p.price.is_known
    )


def remaining_budget(state: DraftState, team_id: int) -> int:
    """Budget minus known spend, with unknown prices charged at the $1 floor.

    Returning `None` for "genuinely unknowable" was the alternative, and it
    poisons every downstream calculation with a null check. Charging the
    minimum legal bid keeps the type `int` and errs toward *over*-estimating a
    rival's remaining ammunition — the safe direction for advice. Callers that
    care about the uncertainty pair this with `unknown_price_count` and render
    it as `$142+`.
    """
    if state.league is None:
        return 0
    return state.league.budget - spent(state, team_id) - MIN_BID * unknown_price_count(state, team_id)


def roster_count(state: DraftState, team_id: int) -> int:
    return len(state.players_for_team(team_id))


def open_draftable_slots(state: DraftState, team_id: int) -> int:
    if state.league is None:
        return 0
    return max(0, state.league.draftable_slots - roster_count(state, team_id))


def max_legal_bid(state: DraftState, team_id: int) -> int:
    """The most this team could legally bid right now.

    Every *other* open slot still has to be filled at $1 apiece, so that money
    isn't available for this player.

    The zero-slot guard is not decorative: `remaining - (open - 1)` evaluates to
    `remaining + 1` when `open` is 0, which would advise bidding real money on a
    roster spot that doesn't exist.
    """
    open_slots = open_draftable_slots(state, team_id)
    if open_slots == 0:
        return 0
    return max(0, remaining_budget(state, team_id) - MIN_BID * (open_slots - 1))


def is_overspent(state: DraftState, team_id: int) -> bool:
    """Our model says this team spent more than it had.

    A true signal, not an error: it usually means a keeper, an unknown price,
    or a pick we recorded wrong twenty minutes ago.
    """
    return remaining_budget(state, team_id) < 0


def slot_fill(state: DraftState, team_id: int) -> dict[RosterSlot, int]:
    """Greedily assign this team's players to roster slots, native slot first.

    Assignment order is `sold_players()`, which is sorted by key, so the result
    is deterministic — dict iteration order must never leak into a projection.
    """
    league = state.league
    if league is None:
        return {}

    filled: dict[RosterSlot, int] = {slot: 0 for slot in league.roster}
    unpositioned: list[PlayerEntity] = []

    for player in state.players_for_team(team_id):
        if player.position is None or not player.position.is_known:
            unpositioned.append(player)
            continue
        for slot in league.slots_for_position(player.position.value):
            if filled.get(slot, 0) < league.roster.get(slot, 0):
                filled[slot] += 1
                break

    # Players whose position we never learned still occupy a roster spot; they
    # just can't be attributed to a positional need. Park them on the bench so
    # the totals stay honest.
    for _ in unpositioned:
        for slot in (RosterSlot.BE, *league.roster):
            if slot is not RosterSlot.IR and filled.get(slot, 0) < league.roster.get(slot, 0):
                filled[slot] += 1
                break

    return filled


def open_slots_by_pos(state: DraftState, team_id: int) -> dict[Position, int]:
    """How many more players of each position this team could still use.

    This is the number that makes a cash-rich team stop being a threat: $90
    left means nothing at RB if every RB and FLEX slot is already full.
    """
    league = state.league
    if league is None:
        return {}

    filled = slot_fill(state, team_id)
    out: dict[Position, int] = {}
    for pos in Position:
        out[pos] = sum(
            max(0, league.roster.get(slot, 0) - filled.get(slot, 0))
            for slot in league.slots_for_position(pos)
        )
    return out


def starter_gaps(state: DraftState, team_id: int) -> dict[RosterSlot, int]:
    """Starting slots this team still has to fill.

    Bench and IR are excluded: an empty bench spot isn't a *need*, it's just
    space. What makes a rival dangerous on a given player is an unfilled
    starting slot.
    """
    league = state.league
    if league is None:
        return {}

    filled = slot_fill(state, team_id)
    return {
        slot: count - filled.get(slot, 0)
        for slot, count in league.roster.items()
        if slot.is_starter and count - filled.get(slot, 0) > 0
    }


def league_totals(state: DraftState) -> Mapping[str, int]:
    """League-wide money, for the market/inflation layer in CP3."""
    if state.league is None:
        return {"sales": 0, "dollars_spent": 0, "unknown_prices": 0}
    sold = state.sold_players()
    return {
        "sales": len(sold),
        "dollars_spent": sum(
            p.price.value for p in sold if p.price is not None and p.price.is_known
        ),
        "unknown_prices": sum(
            1 for p in sold if p.price is None or not p.price.is_known
        ),
    }
