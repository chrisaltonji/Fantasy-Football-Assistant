"""Capability 9 — the danger zone: where demand outruns what is left.

Scarcity (capability 10) says how much stock remains. It cannot say whether
that is a problem, because a thin position nobody needs is not a problem and a
deep position everybody needs can still be one. The missing term is **who can
still act**, and this supplies it:

    contenders  = teams with an unfilled starting slot at this position
                  who can also still pay for one
    supply      = startable players left there
    shortfall   = contenders − supply, when positive

A positive shortfall means somebody in this room is going to miss out at that
position. If you are one of the contenders, the question stops being "is this
position thin" and becomes "am I early or late".

**Two counting decisions, both deliberate.**

*Native slots only.* `has_starter_gap` counts a flex as a gap at running back,
receiver and tight end alike, which is exactly right when asking whether a rival
could start a specific player — and wrong here. Twelve open flexes would add
twelve phantom contenders to three positions at once and make every position
look squeezed. Demand is counted against the position's own slot.

*Contenders must be able to pay.* A team with an empty tight-end slot and $0 is
not competing for one. `max_legal_bid` is the same test the bid readout uses,
and it keeps the count to people who can actually act.

The list is naturally self-limiting: it is empty whenever supply covers demand,
which is most of a draft at most positions. That is the point. A watchlist that
always has something on it is a watchlist nobody reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ffa.advice.market import inflated_value
from ffa.advice.scarcity import scarcity_by_position
from ffa.advice.types import MarketState
from ffa.domain import projections as proj
from ffa.domain.enums import Position, RosterSlot
from ffa.domain.models import DraftState
from ffa.reference.playerbook import PlayerBook

# How many at-risk names to carry per position. The point is to make the squeeze
# concrete, not to reproduce the board.
NAMES = 4


@dataclass(frozen=True)
class Squeeze:
    """One position where the people who need it outnumber what is left."""

    position: Position
    supply: int                 # startable players still on the board
    contenders: int             # teams that need one *and* can pay for one
    mine: bool                  # do we hold one of those unfilled slots
    my_ceiling: int
    names: tuple[str, ...] = ()         # the players actually at risk
    affordable: int = 0                 # how many of those we could still pay for

    @property
    def shortfall(self) -> int:
        """How many contenders will not get one. Positive by construction."""
        return max(0, self.contenders - self.supply)

    @property
    def is_acute(self) -> bool:
        """Ours, and more than one contender short.

        The threshold that separates "watch this" from "this is about you": a
        shortfall of one at a position we do not need is somebody else's
        problem, and saying so would be the watchlist reporting the weather.
        """
        return self.mine and self.shortfall >= 1

    @property
    def priced_out(self) -> bool:
        """We need one, some are left, and we cannot afford any of them."""
        return self.mine and self.supply > 0 and self.affordable == 0


def _native_gap(state: DraftState, team_id: int, position: Position) -> int:
    """Unfilled starting slots of this position's *own* kind.

    Deliberately not `has_starter_gap`: that folds in the flex, which is correct
    for "could they start this player" and wrong for counting demand. See the
    module docstring.
    """
    league = state.league
    if league is None:
        return 0
    try:
        slot = RosterSlot(position.value)
    except ValueError:                      # pragma: no cover - every Position maps
        return 0
    return proj.starter_gaps(state, team_id).get(slot, 0)


def squeezes(
    state: DraftState, book: PlayerBook, market: MarketState | None = None,
    *, scarcity: "Mapping[Position, object] | None" = None,
) -> tuple[Squeeze, ...]:
    """Every position where contenders outnumber supply, worst first.

    `scarcity` may be passed in by a caller that has already computed it. The
    ambient feed calls this once per event while folding a whole journal, and
    recomputing the tiering there took the fold from 333 ms to 1.75 s for a
    result it was already holding.
    """
    league = state.league
    if league is None or not len(book):
        return ()

    me = league.my_team_id
    market = market or MarketState()
    my_ceiling = proj.max_legal_bid(state, me)
    taken = {p.ref.key for p in state.sold_players()}
    if scarcity is None:
        scarcity = scarcity_by_position(state, book)

    # Every team's gaps and ceiling, once. `starter_gaps` walks the roster, so
    # asking per position turned one cheap call into 72 identical ones per
    # event — invisible in a single readout and the dominant cost when the feed
    # folds a whole journal.
    roster = {
        team_id: (proj.starter_gaps(state, team_id), proj.max_legal_bid(state, team_id))
        for team_id in state.teams
    }

    def native_gap(team_id: int, position: Position) -> int:
        try:
            slot = RosterSlot(position.value)
        except ValueError:                  # pragma: no cover - every Position maps
            return 0
        return roster[team_id][0].get(slot, 0)

    out: list[Squeeze] = []
    for position, stock in scarcity.items():
        supply = stock.elite + stock.startable

        contenders = sum(
            1 for team_id in state.teams
            # Needing one and being able to buy one are different things, and
            # only the second competes with you.
            if native_gap(team_id, position) > 0 and roster[team_id][1] > 0
        )
        if contenders <= supply:
            continue            # no squeeze; say nothing

        rows = sorted(
            (r for r in book.by_position(position) if r.key not in taken),
            key=lambda r: -(book.value(r.key) or 0),
        )[:NAMES]
        affordable = sum(
            1 for r in rows
            if (inflated_value(book, r.key, market) or book.value(r.key) or 0) <= my_ceiling
        )

        out.append(Squeeze(
            position=position,
            supply=supply,
            contenders=contenders,
            mine=native_gap(me, position) > 0,
            my_ceiling=my_ceiling,
            names=tuple(r.name for r in rows),
            affordable=affordable,
        ))

    # Ours first, then by how many contenders go without.
    return tuple(sorted(out, key=lambda s: (not s.mine, -s.shortfall, s.position.value)))


def watchlist(
    state: DraftState, book: PlayerBook, market: MarketState | None = None
) -> tuple[Squeeze, ...]:
    """Only the squeezes that are about us. Empty is the normal answer."""
    return tuple(s for s in squeezes(state, book, market) if s.mine)


def describe(squeeze: Squeeze) -> str:
    """One line, in the terms the decision is actually made in."""
    who = "you and" if squeeze.mine else ""
    people = squeeze.contenders - (1 if squeeze.mine else 0)
    head = (
        f"{squeeze.position.value}: {squeeze.supply} startable left, "
        f"{who} {people} rival(s) still need one".replace("  ", " ")
    )
    if squeeze.priced_out:
        return head + f" — and none of them is inside your ${squeeze.my_ceiling} ceiling."
    if squeeze.mine:
        return head + f" — {squeeze.shortfall} will go without."
    return head + "."
