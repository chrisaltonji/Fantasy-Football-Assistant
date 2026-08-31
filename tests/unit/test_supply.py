"""A plan can run out of players as well as money.

`shortfall` measured one of those and the verdict was computed from it alone, so
a plan whose flex slot could no longer be filled by anybody read `on track`.
Money is arguable — hold out, spend less, and a shortfall can still come good. A
starting slot with nothing affordable left in it is not arguable at all, which is
why it is checked first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ffa.advice.feed import plan_state
from ffa.advice.scarcity import open_demand, scarcity_by_position, starting_demand
from ffa.advice.strategy import strategy_read, unfillable
from ffa.config.strategy import StrategyPreset
from ffa.domain.enums import Position, Provenance, RosterSlot
from ffa.domain.events import DraftInitialized, PlayerSold, TeamSeed
from ffa.domain.ids import PlayerRef
from ffa.domain.models import DraftState, LeagueSnapshot
from ffa.domain.reducers import apply
from ffa.domain.sourced import known
from ffa.reference.loader import ReferenceRow
from ffa.reference.playerbook import PlayerBook
from tests.conftest import at

LEAGUE = LeagueSnapshot(
    league_id=1, year=2026, name="T", draft_type="AUCTION", budget=200,
    team_count=12, my_team_id=4,
    roster={
        RosterSlot.QB: 1, RosterSlot.RB: 2, RosterSlot.WR: 3, RosterSlot.TE: 1,
        RosterSlot.FLEX: 1, RosterSlot.K: 1, RosterSlot.DST: 1,
        RosterSlot.BE: 5, RosterSlot.IR: 1,
    },
    flex_positions=(Position.RB, Position.WR, Position.TE),
)


def book_of(values, depth=40):
    """A sheet where each position's best player is worth `values[position]`."""
    rows, rank = [], 1
    for position, top in values.items():
        for i in range(depth):
            name = f"{position.value}{i}"
            rows.append(ReferenceRow(
                key=name.lower(), name=name, position=position,
                auction_value=max(1, top - i), overall_rank=rank,
            ))
            rank += 1
    return PlayerBook(tuple(rows), source=Path("t.csv"))


FULL = {Position.RB: 60, Position.WR: 55, Position.TE: 20,
        Position.QB: 18, Position.K: 4, Position.DST: 3}


def start():
    return apply(DraftState.empty("d"), DraftInitialized(
        at=at(0), source="TEST", draft_id="d", league=LEAGUE,
        teams=tuple(TeamSeed(team_id=i) for i in range(1, 13)),
    ))


def buy(state, name, position, team, price, second):
    return apply(state, PlayerSold(
        at=at(second), source=Provenance.ESPN_API.value,
        player=PlayerRef.from_raw(name),
        team=known(team, Provenance.ESPN_API, at(second)),
        price=known(price, Provenance.ESPN_API, at(second)),
        position=known(position, Provenance.ESPN_API, at(second)),
    ))


def drain(state, position, book, keep=0, start_second=100):
    """Sell every player at a position to a rival, except the cheapest `keep`.

    Skips anyone already sold — draining a position we have bought from would
    otherwise hand our own starter to somebody else and quietly change what the
    test is about.
    """
    taken = {p.ref.key for p in state.sold_players()}
    rows = [r for r in sorted(book.by_position(position),
                              key=lambda r: -(book.value(r.key) or 0))
            if r.key not in taken]
    second = start_second
    for row in (rows[:-keep] if keep else rows):
        state = buy(state, row.name, position, 11, 1, second)
        second += 1
    return state


# --- nothing is wrong ---------------------------------------------------------


def test_a_full_board_leaves_nothing_unfillable():
    """The common answer, and it has to be empty rather than absent."""
    assert unfillable(start(), book_of(FULL)) == ()


def test_a_position_we_have_already_filled_is_not_our_problem():
    """We owe no starter there, so an empty tier is somebody else's squeeze."""
    book = book_of(FULL)
    state = buy(start(), "QB0", Position.QB, 4, 10, 1)
    state = drain(state, Position.QB, book)

    assert Position.QB not in unfillable(state, book)


# --- the two ways a slot dies -------------------------------------------------


def test_an_empty_tier_cannot_be_filled():
    """Nothing left at all. The slot stays open however much money we hold."""
    book = book_of(FULL)
    state = drain(start(), Position.TE, book)

    assert Position.TE in unfillable(state, book)


def test_stock_we_cannot_reach_is_the_same_hole():
    """**Being outbid by the room and simply being poor look identical from the
    plan's side**, and both leave the slot unfilled. This is `priced_out`, asked
    without waiting for a squeeze to exist."""
    book = book_of(FULL)
    # Spend almost everything, so our ceiling is a dollar or two.
    state = buy(start(), "RB0", Position.RB, 4, 190, 1)
    holes = unfillable(state, book)

    # The receivers left are worth far more than we can now reach.
    assert Position.WR in holes


def test_a_bench_body_does_not_rescue_the_plan():
    """A slot filled by somebody below the startable tier is filled in the
    roster sense and not in the plan sense, which is what this measures.

    Tiers rank over the whole pool rather than the survivors, so the last man at
    a position does not get promoted into the startable tier by everyone above
    him being drafted — which is exactly why he cannot rescue the plan."""
    book = book_of(FULL)
    state = drain(start(), Position.TE, book, keep=1)

    assert Position.TE in unfillable(state, book)


# --- flex is not counted three times ------------------------------------------


def test_a_flex_opening_is_not_reported_against_every_eligible_position():
    """Counting it against RB, WR and TE would report one hole three times."""
    book = book_of(FULL)
    state = start()
    # Fill every native starting slot; only the flex is left open.
    for name, position, price in (("QB0", Position.QB, 5), ("RB0", Position.RB, 5),
                                  ("RB1", Position.RB, 5), ("WR0", Position.WR, 5),
                                  ("WR1", Position.WR, 5), ("WR2", Position.WR, 5),
                                  ("TE0", Position.TE, 5), ("K0", Position.K, 1),
                                  ("DST0", Position.DST, 1)):
        state = buy(state, name, position, 4, price, len(name) + price)

    # No native gap anywhere, so nothing is reported even though flex is open.
    assert unfillable(state, book) == ()


# --- the verdict --------------------------------------------------------------


def preset():
    return StrategyPreset(
        archetype="balanced",
        budget_by_slot={"QB": 11, "RB1": 40, "RB2": 25, "WR1": 35, "WR2": 21,
                        "WR3": 14, "TE": 9, "FLEX": 22, "K": 1, "DST": 2},
        max_on_one_player=70, bench_reserve=20,
    )


def test_an_unfillable_slot_is_time_to_move_even_with_money_in_hand():
    """**The case the verdict used to miss.** Plenty of budget, no shortfall, and
    a starting slot that nobody can fill any more — which read `on track`."""
    book = book_of(FULL)
    state = drain(start(), Position.TE, book)
    read = strategy_read(state, book, preset())

    assert Position.TE in read.beyond_supply
    assert read.shortfall == 0, "this is not a money failure"
    assert plan_state(read) == "time to move"


def test_supply_is_checked_before_the_money():
    """A shortfall can be argued with by spending less. A slot with nothing
    affordable left cannot be argued with at all, so it is the one that decides
    the word."""
    book = book_of(FULL)
    state = drain(start(), Position.TE, book)
    read = strategy_read(state, book, preset())

    assert read.beyond_supply and plan_state(read) == "time to move"


def test_a_healthy_plan_is_unchanged_by_the_new_check():
    """The check must be silent when it has nothing to say, or every draft reads
    `time to move` from the first pick."""
    read = strategy_read(start(), book_of(FULL), preset())

    assert read.beyond_supply == ()
    assert plan_state(read) in ("on track", "at risk")


def test_no_plan_still_means_no_verdict():
    assert plan_state(None) is None
    assert strategy_read(start(), book_of(FULL), StrategyPreset()) is None


# --- demand that moves --------------------------------------------------------


def test_demand_starts_at_what_the_league_starts():
    """Nothing is filled yet, so every starting slot is still open."""
    state = start()
    assert open_demand(state)[Position.QB] == 12 == starting_demand(LEAGUE, Position.QB)


def test_demand_falls_as_slots_fill():
    """**The number that never moved.** `starting_demand` is what the league
    starts and is static by design - it cuts the tiers. This is how many of those
    slots are still open, and once everybody has a quarterback it is zero."""
    state = start()
    for team in range(1, 13):
        state = buy(state, f"QB{team}", Position.QB, team, 5, team)

    assert open_demand(state)[Position.QB] == 0
    # And the static one is untouched, because the tiers depend on it.
    assert starting_demand(LEAGUE, Position.QB) == 12


def test_the_tiers_do_not_move_when_demand_does():
    """A boundary that slid as slots filled would promote a player into
    "startable" for no reason except that better ones were drafted."""
    book = book_of(FULL)
    state = start()
    before = scarcity_by_position(state, book)[Position.QB].starting_demand
    for team in range(1, 13):
        state = buy(state, f"QB{team}", Position.QB, team, 5, team)
    after = scarcity_by_position(state, book)[Position.QB]

    assert after.starting_demand == before
    assert after.open_demand == 0


def test_a_flex_opening_is_split_across_the_positions_that_could_fill_it():
    """One open flex slot is a third of a need at each eligible position, not a
    whole one at each - the same split `starting_demand` already uses."""
    state = start()
    demand = open_demand(state)

    # 12 flex slots open, three eligible positions -> 4 apiece, on top of native.
    assert demand[Position.RB] == 12 * 2 + 4
    assert demand[Position.WR] == 12 * 3 + 4
    assert demand[Position.TE] == 12 * 1 + 4
    # A quarterback can never fill the flex, so none of it reaches him.
    assert demand[Position.QB] == 12

