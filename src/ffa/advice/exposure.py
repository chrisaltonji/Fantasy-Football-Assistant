"""What each rival could commit, one position at a time.

`Threat` answers "can this team buy the player on the block" and `max_legal_bid`
answers "how much money could they legally spend at all". Neither varies across
positions, so a board coloured by either says a rival's kicker is as dangerous as
his second receiver — which is how the threat matrix read before this existed.

Exposure is the same question asked per column: **the most this team could
sensibly commit at this position, given what is left there and how much of their
starting lineup is still open.**

## It is arithmetic, and the name is how it stays arithmetic

`bidding.py` says the deterministic engine ships no per-bidder score — no
`will_bid`, no `likelihood` — because what a person intends belongs to the
inference layer. That rule is intact here, and the reason is that both inputs are
facts about the board:

- `reach` is the lesser of a legal ceiling and a sheet value. Both computed.
- `need` is unfilled starting slots over required slots. A count.

Their product is bounded by what a team could actually do, and it claims nothing
about what they will do. **Do not rename this `risk`, `pressure` or `likelihood`**
— the moment it is called one of those it is asserting intent, and the surface
that renders it will start being read as a forecast.

## Native slots only

`bidding.has_starter_gap` folds FLEX into every eligible position, because it is
asking whether a team can start *this player*, and a flex slot is somewhere to put
him. This asks a different question. It is a per-column number and the matrix has
its own FLEX column, so counting a flex opening against RB *and* against FLEX
would show the same one dollar of pressure twice, side by side.

Same choice `watchlist._native_gap` makes, for the same reason.
"""

from __future__ import annotations

from typing import Mapping

from ffa.advice.market import market_state
from ffa.advice.scarcity import scarcity_by_position
from ffa.advice.types import MarketState, PositionScarcity
from ffa.domain import projections as proj
from ffa.domain.enums import Position, RosterSlot
from ffa.domain.models import DraftState
from ffa.reference.playerbook import PlayerBook


def required_at(league, position: Position) -> int:
    """Starting slots this league fields at a position, flex excluded.

    Flex is its own column with its own requirement, so it is not folded in
    here — see the module docstring.
    """
    roster = getattr(league, "roster", None) or {}
    try:
        return int(roster.get(RosterSlot(position.value), 0) or 0)
    except ValueError:                      # pragma: no cover - every Position maps
        return 0


def reach(max_legal_bid: int, stock: PositionScarcity | None,
          inflation: float) -> int:
    """The most a team could put on the best player left at this position.

    Two ceilings, and the lower one binds. A team with $180 cannot spend it here
    if the best receiver left is worth $9, and a team that wants a $60 back
    cannot buy him with $12. Taking the minimum is what makes a kicker column
    cool while a back column stays hot for the same wallet.

    Inflated rather than raw, because the sheet is what a player was worth before
    this room started paying 1.3x for everyone. `market.inflation_ratio` is the
    same number the bid ceilings already use.
    """
    if max_legal_bid <= 0 or stock is None:
        return 0
    top = int(getattr(stock, "top_value_remaining", 0) or 0)
    if top <= 0:
        return 0
    return max(0, min(int(max_legal_bid), round(top * max(0.0, inflation or 1.0))))


def exposure_for(state: DraftState, team_id: int, position: Position, *,
                 gaps: Mapping[RosterSlot, int] | None = None,
                 ceiling: int | None = None,
                 stock: PositionScarcity | None = None,
                 inflation: float = 1.0) -> int:
    """One cell: dollars this team could commit at this position, or 0.

    Zero when the position's starting slots are already filled, which is the
    common case late and is why a completed slot can never colour: there is
    nothing left to compete for, whatever the wallet says.

    The optional arguments exist so a caller doing the whole grid can hoist the
    expensive lookups. `starter_gaps` walks a roster, so asking it per position
    turns one cheap call into dozens of identical ones — the same cost
    `watchlist` already hoists against.
    """
    league = state.league
    if league is None:
        return 0

    if gaps is None:
        gaps = proj.starter_gaps(state, team_id)
    try:
        slot = RosterSlot(position.value)
    except ValueError:                      # pragma: no cover - every Position maps
        return 0

    open_slots = int(gaps.get(slot, 0) or 0)
    if open_slots <= 0:
        return 0

    required = required_at(league, position)
    if required <= 0:
        return 0

    if ceiling is None:
        ceiling = proj.max_legal_bid(state, team_id)

    return round(reach(ceiling, stock, inflation) * open_slots / required)


# The flex column is keyed by the slot's own name, because it has no `Position`.
FLEX_KEY = RosterSlot.FLEX.value


def flex_exposure(league, gaps: Mapping[RosterSlot, int], ceiling: int,
                  scarcity: Mapping[Position, PositionScarcity],
                  inflation: float) -> int:
    """The same question asked of the one slot that has no position.

    **This column existed and had no answer**, which is worse than having no
    column: a surface that falls back to the wallet when a cell is missing paints
    the flex opening full-intensity for anyone rich, which is the exact failure
    the rest of this module removes. A live board caught it doing so.

    `reach` here is the best of what could fill the slot — a flex spot is worth
    the most valuable eligible player still on the sheet, whichever position he
    plays, because that is who would actually be bought for it.
    """
    flex_slots = int((getattr(league, "roster", None) or {}).get(RosterSlot.FLEX, 0) or 0)
    if flex_slots <= 0:
        return 0
    open_slots = int(gaps.get(RosterSlot.FLEX, 0) or 0)
    if open_slots <= 0:
        return 0

    eligible = tuple(getattr(league, "flex_positions", ()) or ())
    best = max(
        (reach(ceiling, scarcity.get(position), inflation) for position in eligible),
        default=0,
    )
    return round(best * open_slots / flex_slots)


def reach_by_position(state: DraftState, book: PlayerBook, team_id: int, *,
                      scarcity: Mapping[Position, PositionScarcity] | None = None,
                      market: MarketState | None = None,
                      ) -> Mapping[object, int]:
    """What one team could commit per column **ignoring their own need**.

    This is the scale exposure is read against, and the choice took three
    attempts on a live board.

    A whole-budget ceiling leaves the top half of the ramp dead: a ceiling is
    what you could spend on one player out of everything left, while exposure is
    capped by what a single position's best player is worth. Measured across a
    real draft the hottest cell never passed 43% - and at pick one, when every
    rival can outbid you everywhere, the grid rendered cool. That is not a quiet
    board; it is a scale that cannot say anything.

    Reading each column against *its own* entry here fixes the ramp and breaks
    the panel: "can they outbid me at kicker" is yes, so the kicker goes bright
    red again, which is the failure exposure exists to remove.

    So a surface should take the **maximum** of this mapping and use that one
    number for every cell. The ramp is then fully used and the columns keep their
    relative worth - $7 of kicker against $23 of running back. It moves when the
    board empties and never when a single rival spends, which is the jitter that
    made a wallet-derived scale unreadable.
    """
    league = state.league
    if league is None or book is None or not len(book):
        return {}

    if scarcity is None:
        scarcity = scarcity_by_position(state, book)
    if market is None:
        market = market_state(state, book)
    inflation = float(getattr(market, "inflation_ratio", 1.0) or 1.0)
    ceiling = proj.max_legal_bid(state, team_id)

    out: dict[object, int] = {
        position: reach(ceiling, scarcity.get(position), inflation)
        for position in Position if required_at(league, position) > 0
    }
    eligible = tuple(getattr(league, "flex_positions", ()) or ())
    out[FLEX_KEY] = max(
        (reach(ceiling, scarcity.get(p), inflation) for p in eligible), default=0,
    )
    return out


def exposure_by_team(state: DraftState, book: PlayerBook, *,
                     scarcity: Mapping[Position, PositionScarcity] | None = None,
                     market: MarketState | None = None,
                     ) -> Mapping[int, Mapping[Position, int]]:
    """The whole grid, one row per team.

    Built in one pass with the per-team lookups hoisted, because every surface
    that wants this wants all of it — a dashboard paints twelve rows on a
    two-second poll, and doing it a cell at a time would walk each roster once
    per position.
    """
    league = state.league
    if league is None or book is None or not len(book):
        return {}

    if scarcity is None:
        scarcity = scarcity_by_position(state, book)
    if market is None:
        market = market_state(state, book)
    inflation = float(getattr(market, "inflation_ratio", 1.0) or 1.0)

    # Positions this league actually starts. A league with no kicker slot gets no
    # kicker column rather than a column of zeroes.
    positions = [p for p in Position if required_at(league, p) > 0]

    out: dict[int, dict[Position, int]] = {}
    for team_id in state.teams:
        gaps = proj.starter_gaps(state, team_id)
        ceiling = proj.max_legal_bid(state, team_id)
        row = {
            position: exposure_for(
                state, team_id, position,
                gaps=gaps, ceiling=ceiling,
                stock=scarcity.get(position), inflation=inflation,
            )
            for position in positions
        }
        row[FLEX_KEY] = flex_exposure(league, gaps, ceiling, scarcity, inflation)
        out[team_id] = row
    return out
