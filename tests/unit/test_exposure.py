"""What a rival could commit, one position at a time.

The threat matrix used to paint a whole team row with `max_legal_bid`, so the
only thing that varied across a row was whether a slot was already full. On a
real board that rendered a rival's kicker exactly as hot as his second receiver,
and the panel stopped being read.

The cases below are the ones that prompted the change, with the numbers taken
from that board rather than invented.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ffa.advice.exposure import (
    FLEX_KEY,
    exposure_by_team,
    exposure_for,
    reach,
    required_at,
)
from ffa.advice.types import PositionScarcity
from ffa.domain.enums import Position, Provenance, RosterSlot
from ffa.domain.events import PlayerSold
from ffa.domain.ids import PlayerRef
from ffa.domain.models import DraftState, LeagueSnapshot
from ffa.domain.reducers import apply
from ffa.domain.sourced import known
from ffa.reference.loader import ReferenceRow
from ffa.reference.playerbook import PlayerBook
from tests.conftest import at

LEAGUE = LeagueSnapshot(
    league_id=1, year=2026, name="Test", draft_type="AUCTION", budget=200,
    team_count=12, my_team_id=4,
    roster={
        RosterSlot.QB: 1, RosterSlot.RB: 2, RosterSlot.WR: 3, RosterSlot.TE: 1,
        RosterSlot.FLEX: 1, RosterSlot.K: 1, RosterSlot.DST: 1,
        RosterSlot.BE: 5, RosterSlot.IR: 1,
    },
    flex_positions=(Position.RB, Position.WR, Position.TE),
)


def stock(top_value: int) -> PositionScarcity:
    """Only `top_value_remaining` matters here; the tiers are scarcity's job."""
    return PositionScarcity(
        position=Position.RB, elite=0, startable=5, bench=10,
        starting_demand=28, top_value_remaining=top_value,
    )


# --- reach: two ceilings, the lower one binds -------------------------------


def test_reach_is_capped_by_what_is_actually_left():
    """A team with $180 cannot spend it at a position where the best player
    left is worth $9. This is the cap that makes a kicker column cool."""
    assert reach(180, stock(9), 1.0) == 9


def test_reach_is_capped_by_the_wallet():
    """And a team that wants a $60 back cannot buy him with $12."""
    assert reach(12, stock(60), 1.0) == 12


def test_reach_is_inflated_because_the_sheet_is_not_what_this_room_pays():
    """`inflation_ratio` is the same number the bid ceilings already use — the
    sheet says what a player was worth before this room started paying 1.3x."""
    assert reach(200, stock(20), 1.3) == 26


def test_a_broke_team_reaches_nothing():
    assert reach(0, stock(60), 1.3) == 0


def test_nothing_left_at_a_position_is_nothing_to_reach_for():
    assert reach(180, stock(0), 1.3) == 0
    assert reach(180, None, 1.3) == 0


# --- the case that prompted this --------------------------------------------


def sold(state, name, position, team, price, seconds=0):
    moment = at(seconds)
    return apply(state, PlayerSold(
        at=moment, source=Provenance.ESPN_API.value,
        player=PlayerRef.from_raw(name),
        team=known(team, Provenance.ESPN_API, moment),
        price=known(price, Provenance.ESPN_API, moment),
        position=known(position, Provenance.ESPN_API, moment),
    ))


@pytest.fixture
def board():
    """One rival with a back and two receivers: RB 1/2 filled, WR 2/3 filled."""
    state = apply(DraftState.empty("d"), _init())
    state = sold(state, "Some Back", Position.RB, 7, 40)
    state = sold(state, "Wide One", Position.WR, 7, 30, 1)
    state = sold(state, "Wide Two", Position.WR, 7, 28, 2)
    return state


def _init():
    from ffa.domain.events import DraftInitialized, TeamSeed

    return DraftInitialized(
        at=at(0), source="TEST", draft_id="d", league=LEAGUE,
        teams=tuple(TeamSeed(team_id=i) for i in range(1, 13)),
    )


def test_an_open_rb2_reads_hotter_than_a_third_receiver(board):
    """**The case this exists for.**

    Both have one slot open, so a gap count cannot tell them apart. But that gap
    is half of this team's backfield and only a third of its receiving corps, and
    a second back is a starter where a third receiver is the marginal one.
    """
    rb = exposure_for(board, 7, Position.RB, stock=stock(23), inflation=1.0)
    wr = exposure_for(board, 7, Position.WR, stock=stock(21), inflation=1.0)

    assert rb > wr, "an open RB2 should read hotter than an open WR3"
    assert (rb, wr) == (round(23 * 1 / 2), round(21 * 1 / 3))


def test_a_kicker_is_bounded_by_what_a_kicker_is_worth(board):
    """**The failure that made the panel useless.**

    A row painted with `max_legal_bid` gave this team the same full-intensity
    cell at kicker as at running back, because the only thing it knew was the
    wallet. What bounds a kicker is not the wallet; it is that the best one left
    is worth $5.

    Note the kicker still carries full *need* — one slot of one, entirely
    unfilled — and that is correct: they do have to buy one. The dollars are what
    keep the cell cool, which is the whole point of taking a minimum.
    """
    wallet = 89
    kicker = exposure_for(board, 7, Position.K, ceiling=wallet,
                          stock=stock(5), inflation=1.3)
    back = exposure_for(board, 7, Position.RB, ceiling=wallet,
                        stock=stock(23), inflation=1.3)

    assert kicker < back
    # And far below the wallet, which is all the old colouring ever showed.
    assert kicker < wallet / 8


# --- the zero cases ---------------------------------------------------------


def test_a_filled_position_is_never_exposed(board):
    """Whatever the wallet says. There is nothing left to compete for."""
    # Team 7 has both receivers and a back; give it the third receiver too.
    state = sold(board, "Wide Three", Position.WR, 7, 20, 3)
    assert exposure_for(state, 7, Position.WR, stock=stock(60), inflation=2.0) == 0


def test_a_broke_team_is_not_exposed_however_badly_it_needs_one(board):
    """`reach` is capped by the legal ceiling, so need cannot manufacture money."""
    state = sold(board, "Expensive Guy", Position.QB, 9, 199, 4)
    assert exposure_for(state, 9, Position.RB, stock=stock(60), inflation=1.3) == 0


def test_required_at_counts_native_slots_only(board):
    """Not flex. Flex has its own column and its own requirement."""
    assert required_at(LEAGUE, Position.RB) == 2
    assert required_at(LEAGUE, Position.WR) == 3
    assert required_at(LEAGUE, Position.QB) == 1


# --- the FLEX trap ----------------------------------------------------------


def test_flex_is_its_own_column_and_does_not_inflate_the_native_one(board):
    """`bidding.has_starter_gap` folds FLEX into every eligible position because
    it asks whether a player has *somewhere to go*. This asks a different
    question, per column, and the matrix has its own FLEX column — counting a
    flex opening against RB as well would show the same dollar of pressure twice,
    side by side."""
    # Team 7 has one back of two, and its flex is open.
    gaps = __import__("ffa.domain.projections", fromlist=["x"]).starter_gaps(board, 7)

    assert gaps[RosterSlot.RB] == 1
    assert gaps[RosterSlot.FLEX] == 1

    rb = exposure_for(board, 7, Position.RB, stock=stock(30), inflation=1.0)
    # 30 * 1/2, not 30 * 2/2 — the flex opening is not RB's.
    assert rb == 15


# --- the whole grid ---------------------------------------------------------


def book_with(values):
    rows, rank = [], 1
    for position, top in values.items():
        for i in range(12):
            name = f"{position.value}{i}"
            rows.append(ReferenceRow(
                key=name.lower(), name=name, position=position,
                auction_value=max(1, top - i * 2), overall_rank=rank,
            ))
            rank += 1
    return PlayerBook(tuple(rows), source=Path("t.csv"))


def test_the_grid_covers_every_team_and_every_started_slot(board):
    book = book_with({Position.RB: 40, Position.WR: 35, Position.QB: 20,
                      Position.TE: 15, Position.K: 4, Position.DST: 3})
    grid = exposure_by_team(board, book)

    assert set(grid) == set(board.teams)
    started = {p for p in Position if required_at(LEAGUE, p) > 0}
    # Plus flex, which has no `Position` and is keyed by the slot's own name.
    assert set(grid[7]) == started | {FLEX_KEY}


def test_the_flex_column_is_answered_rather_than_left_missing(board):
    """**Caught by rendering, not by a test.**

    Flex is not a `Position`, so the first version of the grid simply had no key
    for it — and the page falls back to the wallet colour for a missing cell,
    which painted every rich team's flex opening full-intensity. That is the
    exact failure this module exists to remove, reintroduced in the one column
    nobody thought to check.
    """
    book = book_with({Position.RB: 40, Position.WR: 35, Position.QB: 20,
                      Position.TE: 15, Position.K: 4, Position.DST: 3})
    row = exposure_by_team(board, book)[7]

    assert FLEX_KEY in row
    assert row[FLEX_KEY] > 0, "team 7 has an open flex and money"


def test_a_filled_flex_is_not_exposed(board):
    """The same zero rule as every other column.

    Two more backs, not one: `slot_fill` is native-first, so the second fills
    RB2 and only the third reaches the flex."""
    state = sold(board, "Second Back", Position.RB, 7, 18, 5)
    state = sold(state, "Third Back", Position.RB, 7, 15, 6)
    book = book_with({Position.RB: 40, Position.WR: 35, Position.QB: 20,
                      Position.TE: 15, Position.K: 4, Position.DST: 3})

    row = exposure_by_team(state, book)[7]
    assert row[Position.RB] == 0, "both native back slots are filled"
    assert row[FLEX_KEY] == 0


def test_flex_is_worth_the_best_thing_that_could_fill_it(board):
    """A flex spot is worth the most valuable eligible player still on the sheet,
    whichever position he plays, because that is who would be bought for it."""
    from ffa.advice.exposure import flex_exposure
    from ffa.domain import projections as proj

    gaps = proj.starter_gaps(board, 7)
    scarcity = {Position.RB: stock(40), Position.WR: stock(12),
                Position.TE: stock(5)}

    # The best eligible is the $40 back, not the $12 receiver.
    assert flex_exposure(LEAGUE, gaps, 200, scarcity, 1.0) == 40


def test_every_cell_is_a_whole_number_of_dollars(board):
    book = book_with({Position.RB: 40, Position.WR: 35, Position.QB: 20,
                      Position.TE: 15, Position.K: 4, Position.DST: 3})
    for row in exposure_by_team(board, book).values():
        for dollars in row.values():
            assert isinstance(dollars, int) and dollars >= 0


def test_no_book_means_no_grid_rather_than_a_grid_of_zeroes(board):
    """A surface rendering zeroes would say every rival is harmless."""
    assert exposure_by_team(board, PlayerBook((), source=Path("t.csv"))) == {}
