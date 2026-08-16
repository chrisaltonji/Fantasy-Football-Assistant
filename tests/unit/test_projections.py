from __future__ import annotations

import pytest

from ffa.domain import projections as proj
from ffa.domain import reducers
from ffa.domain.enums import Position
from ffa.domain.models import DraftState
from tests.conftest import sold


def state_with(init_event, *events):
    return reducers.replay([init_event, *events])


# --- max_legal_bid, including the boundary that was wrong in the plan --------


def test_untouched_team_can_bid_budget_minus_other_slots(init_event):
    """16 draftable slots, so 15 others at $1 each are spoken for."""
    state = state_with(init_event)
    assert proj.open_draftable_slots(state, 3) == 16
    assert proj.max_legal_bid(state, 3) == 200 - 15


def test_last_open_slot_can_spend_everything(init_event):
    events = [sold(i + 2, f"p{i}", 3, 1) for i in range(15)]
    state = state_with(init_event, *events)
    assert proj.open_draftable_slots(state, 3) == 1
    assert proj.max_legal_bid(state, 3) == proj.remaining_budget(state, 3) == 185


def test_full_roster_can_bid_nothing(init_event):
    """`remaining - (open - 1)` returns `remaining + 1` here without the guard,
    advising a bid on a roster spot that does not exist."""
    events = [sold(i + 2, f"p{i}", 3, 1) for i in range(16)]
    state = state_with(init_event, *events)
    assert proj.open_draftable_slots(state, 3) == 0
    assert proj.max_legal_bid(state, 3) == 0


def test_max_legal_bid_never_goes_negative_when_overspent(init_event):
    state = state_with(init_event, sold(2, "superstar", 3, 250))
    assert proj.remaining_budget(state, 3) == -50
    assert proj.max_legal_bid(state, 3) == 0


def test_roster_slots_exclude_ir(init_event):
    """You don't draft into IR, so it must not inflate the slot count."""
    assert init_event.league.draftable_slots == 16


# --- unknown prices -----------------------------------------------------------


def test_unknown_price_is_charged_at_the_one_dollar_floor(init_event):
    state = state_with(init_event, sold(2, "mahomes", 3, None))
    assert proj.remaining_budget(state, 3) == 199
    assert proj.unknown_price_count(state, 3) == 1


def test_unknown_prices_are_counted_so_the_renderer_can_flag_them(init_event):
    state = state_with(init_event, sold(2, "a", 3, None), sold(3, "b", 3, None), sold(4, "c", 3, 40))
    assert proj.spent(state, 3) == 40
    assert proj.unknown_price_count(state, 3) == 2
    assert proj.remaining_budget(state, 3) == 200 - 40 - 2


# --- positional openings ------------------------------------------------------


def test_a_full_position_reports_no_openings_beyond_bench(init_event):
    """The whole point: a cash-rich team with no RB slots isn't an RB threat."""
    state = state_with(
        init_event,
        sold(2, "rb1", 3, 40, pos=Position.RB),
        sold(3, "rb2", 3, 30, pos=Position.RB),
        sold(4, "rb3", 3, 20, pos=Position.RB),  # takes FLEX
    )
    openings = proj.open_slots_by_pos(state, 3)
    assert openings[Position.RB] == 7  # bench only
    assert openings[Position.QB] == 1 + 7  # QB slot plus bench


def test_flex_absorbs_only_flex_eligible_positions(init_event):
    state = state_with(init_event, sold(2, "qb1", 3, 40, pos=Position.QB))
    openings = proj.open_slots_by_pos(state, 3)
    assert openings[Position.QB] == 7  # QB slot consumed, bench remains
    assert openings[Position.RB] == 2 + 1 + 7  # RB slots, FLEX, bench


def test_players_with_unknown_position_still_consume_a_roster_spot(init_event):
    state = state_with(init_event, sold(2, "mystery", 3, 10))
    assert proj.roster_count(state, 3) == 1
    assert proj.open_draftable_slots(state, 3) == 15


# --- determinism and empty state ----------------------------------------------


def test_projections_are_pure(init_event):
    state = state_with(init_event, sold(2, "mahomes", 3, 45))
    first = (proj.remaining_budget(state, 3), proj.open_slots_by_pos(state, 3))
    assert first == (proj.remaining_budget(state, 3), proj.open_slots_by_pos(state, 3))


@pytest.mark.parametrize(
    "fn", [proj.remaining_budget, proj.max_legal_bid, proj.open_draftable_slots, proj.spent]
)
def test_projections_are_safe_on_uninitialized_state(fn):
    assert fn(DraftState.empty(), 1) == 0


def test_league_totals_track_market_spend(init_event):
    state = state_with(init_event, sold(2, "a", 1, 45), sold(3, "b", 2, 30), sold(4, "c", 3, None))
    assert proj.league_totals(state) == {"sales": 3, "dollars_spent": 75, "unknown_prices": 1}
