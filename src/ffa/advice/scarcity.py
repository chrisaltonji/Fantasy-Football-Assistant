"""What's left on the board, tiered against this league's actual demand.

The tiering deliberately does *not* use the reference file's own tier column as
its primary signal. A generic "Tier 3" means nothing without knowing how many
starters this league needs: in a 12-team league starting 2 RBs and a flex,
roughly the top 30 RBs are startable and the 31st is a bench body — and that
boundary moves with league size, not with the sheet's opinion.
"""

from __future__ import annotations

import math
from typing import Mapping

from ffa.domain.enums import Position, RosterSlot
from ffa.domain.models import DraftState
from ffa.advice.types import PositionScarcity
from ffa.reference.playerbook import PlayerBook

# Share of a flex slot attributed to each eligible position when computing
# league-wide starting demand. Even split — refining it is inference, not
# arithmetic, so it stays out of here.
def _flex_share(league, position: Position) -> float:
    eligible = [p for p in league.flex_positions]
    if position not in eligible or not eligible:
        return 0.0
    return league.roster.get(RosterSlot.FLEX, 0) / len(eligible)


def starting_demand(league, position: Position) -> int:
    """How many of this position the league starts in total."""
    if league is None:
        return 0
    native = 0
    try:
        native = league.roster.get(RosterSlot(position.value), 0)
    except ValueError:  # pragma: no cover - every Position has a slot today
        native = 0
    per_team = native + _flex_share(league, position)
    return int(round(per_team * league.team_count))


def scarcity_by_position(
    state: DraftState, book: PlayerBook
) -> Mapping[Position, PositionScarcity]:
    """Remaining stock per position, split elite / startable / bench."""
    league = state.league
    if league is None or not len(book):
        return {}

    taken = {p.ref.key for p in state.sold_players()}
    out: dict[Position, PositionScarcity] = {}

    for position in Position:
        demand = starting_demand(league, position)
        rows = sorted(
            book.by_position(position),
            key=lambda r: (-(book.value(r.key) or 0), r.overall_rank or 9999),
        )
        # Rank across the whole position pool, drafted or not, so a player's
        # class doesn't inflate as better players come off the board.
        elite_cut = max(1, math.ceil(demand / 3)) if demand else 0

        elite = startable = bench = 0
        top_remaining = 0
        for index, row in enumerate(rows):
            if row.key in taken:
                continue
            value = book.value(row.key) or 0
            top_remaining = max(top_remaining, value)
            if index < elite_cut:
                elite += 1
            elif index < demand:
                startable += 1
            else:
                bench += 1

        out[position] = PositionScarcity(
            position=position,
            elite=elite,
            startable=startable,
            bench=bench,
            total_remaining=elite + startable + bench,
            starting_demand=demand,
            top_value_remaining=top_remaining,
        )

    return out
