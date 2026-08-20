"""Capability 9 — the danger zone.

Scarcity says how much is left. This says whether that is a problem, and the
missing term is who can still act on it. The tests that matter are the ones
about *counting*: demand has to be counted the way a roster actually works, or
the list flags positions nobody is competing for and stops being read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ffa.advice.market import market_state
from ffa.advice.watchlist import describe, squeezes, watchlist
from ffa.domain.enums import Position, RosterSlot
from ffa.domain.events import DraftInitialized, TeamSeed
from ffa.domain.ids import normalize_player_key
from ffa.domain.models import LeagueSnapshot
from ffa.domain.reducers import replay
from ffa.reference.loader import ReferenceRow
from ffa.reference.playerbook import PlayerBook
from tests.conftest import at, sold

SEATS = (1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13)
ME = 4


@pytest.fixture
def snapshot() -> LeagueSnapshot:
    return LeagueSnapshot(
        league_id=1, year=2026, name="T", draft_type="AUCTION", budget=200,
        team_count=12, my_team_id=ME,
        roster={
            RosterSlot.QB: 1, RosterSlot.RB: 2, RosterSlot.WR: 2, RosterSlot.TE: 1,
            RosterSlot.FLEX: 1, RosterSlot.K: 1, RosterSlot.DST: 1, RosterSlot.BE: 5,
            RosterSlot.IR: 1,
        },
        flex_positions=(Position.RB, Position.WR, Position.TE),
    )


def row(name, position, value, rank):
    return ReferenceRow(key=normalize_player_key(name), name=name,
                        position=position, auction_value=value, overall_rank=rank)


def book_with(te_count: int, te_value: int = 8) -> PlayerBook:
    """A book whose tight-end shelf we control, everything else deep."""
    rows, rank = [], 1
    for i in range(te_count):
        rows.append(row(f"TE{i}", Position.TE, te_value, rank)); rank += 1
    for position, value in ((Position.RB, 40), (Position.WR, 38),
                            (Position.QB, 12), (Position.K, 2), (Position.DST, 2)):
        for i in range(60):
            rows.append(row(f"{position.value}{i}", position, max(1, value - i), rank))
            rank += 1
    return PlayerBook(tuple(rows), source=Path("t.csv"))


def state_for(snapshot, events=()):
    init = DraftInitialized(
        id=1, at=at(), draft_id="d", league=snapshot,
        teams=tuple(TeamSeed(team_id=i, manager=f"mgr{i}") for i in SEATS),
    )
    return replay((init, *events))


def fill_te_for(teams, start_id=2):
    """Give each named team a tight end, closing that starting slot."""
    return [
        sold(start_id + i, f"Their TE {t}", t, 1, pos=Position.TE, seconds=i)
        for i, t in enumerate(teams)
    ]


# --- the counting decisions -------------------------------------------------


def test_a_position_nobody_is_short_of_is_not_a_squeeze(snapshot):
    """Empty is the normal answer, and a watchlist that always has something on
    it is a watchlist nobody reads."""
    book = book_with(te_count=40)
    state = state_for(snapshot)

    assert squeezes(state, book, market_state(state, book)) == ()


def test_demand_counts_the_position_s_own_slot_not_the_flex(snapshot):
    """`has_starter_gap` folds the flex in, which is right for "could they start
    him" and wrong here: twelve open flexes would add twelve phantom contenders
    to running back, receiver and tight end at once, and every position would
    look squeezed from pick one."""
    book = book_with(te_count=13)      # one more than the twelve TE slots
    state = state_for(snapshot)

    # Every team's flex is open, so the naive count would be 24 contenders.
    found = squeezes(state, book, market_state(state, book))
    assert not any(s.position is Position.TE for s in found), (
        "counting the flex as tight-end demand invents a squeeze that is not there"
    )


def test_a_team_that_cannot_pay_is_not_a_contender(snapshot):
    """Needing one and being able to buy one are different things, and only the
    second competes with you."""
    book = book_with(te_count=2)

    # Everyone still needs a TE, but eleven of them have spent out.
    broke = []
    for i, team in enumerate(t for t in SEATS if t != ME):
        broke.append(sold(100 + i, f"Bust {team}", team, 200, pos=Position.RB, seconds=i))
    state = state_for(snapshot, broke)

    found = [s for s in squeezes(state, book, market_state(state, book))
             if s.position is Position.TE]
    assert not found or found[0].contenders <= 1


# --- the reading ------------------------------------------------------------


def test_a_real_squeeze_names_the_players_at_risk(snapshot):
    """Two tight ends left and everybody still needs one."""
    book = book_with(te_count=2)
    state = state_for(snapshot)

    te = next(s for s in squeezes(state, book, market_state(state, book))
              if s.position is Position.TE)

    assert te.supply == 2
    assert te.contenders == 12          # every team, none of them filled
    assert te.shortfall == 10
    assert te.mine is True
    assert len(te.names) == 2
    assert "TE0" in te.names


def test_ours_sorts_first(snapshot):
    book = book_with(te_count=1)
    # Close our own tight-end slot; the squeeze remains, but not for us.
    state = state_for(snapshot, fill_te_for([ME]))

    found = squeezes(state, book, market_state(state, book))
    te = next(s for s in found if s.position is Position.TE)
    assert te.mine is False
    assert watchlist(state, book, market_state(state, book)) == () or all(
        s.mine for s in watchlist(state, book, market_state(state, book)))


def test_the_watchlist_is_only_what_is_about_us(snapshot):
    book = book_with(te_count=2)
    state = state_for(snapshot)

    assert all(s.mine for s in watchlist(state, book, market_state(state, book)))


def test_being_priced_out_is_its_own_state(snapshot):
    """Stock left and none of it reachable is a different problem from no stock,
    and the two need different answers."""
    book = book_with(te_count=2, te_value=90)
    # Spend almost everything, so the remaining tight ends are out of reach.
    state = state_for(snapshot, [sold(2, "RB0", ME, 190, pos=Position.RB, seconds=1)])

    te = next(s for s in squeezes(state, book, market_state(state, book))
              if s.position is Position.TE)

    assert te.supply > 0
    assert te.affordable == 0
    assert te.priced_out is True
    assert "ceiling" in describe(te)


def test_a_squeeze_we_can_pay_for_is_not_priced_out(snapshot):
    book = book_with(te_count=2, te_value=5)
    state = state_for(snapshot)

    te = next(s for s in squeezes(state, book, market_state(state, book))
              if s.position is Position.TE)
    assert te.affordable > 0
    assert te.priced_out is False


def test_it_says_how_many_go_without(snapshot):
    book = book_with(te_count=2)
    state = state_for(snapshot)
    te = next(s for s in squeezes(state, book, market_state(state, book))
              if s.position is Position.TE)

    assert "will go without" in describe(te)


def test_no_reference_data_means_no_watchlist(snapshot):
    empty = PlayerBook((), source=Path("t.csv"))
    state = state_for(snapshot)
    assert squeezes(state, empty, market_state(state, empty)) == ()


def test_it_closes_when_the_slots_close(snapshot):
    """Self-limiting by construction: once teams have filled the slot they are
    no longer contenders, so the squeeze disappears rather than needing a
    threshold to switch it off."""
    book = book_with(te_count=2)
    everyone = state_for(snapshot)
    assert any(s.position is Position.TE
               for s in squeezes(everyone, book, market_state(everyone, book)))

    filled = state_for(snapshot, fill_te_for([t for t in SEATS]))
    assert not any(s.position is Position.TE
                   for s in squeezes(filled, book, market_state(filled, book)))
