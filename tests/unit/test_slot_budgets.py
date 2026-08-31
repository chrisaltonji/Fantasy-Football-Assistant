"""The plan, declared one starting slot at a time.

A plan that says "$82 at running back" does not say whether you meant $50/$32 or
$41/$41, and those are different drafts. Slots are what you tune; positions are
their sum.

The properties worth pinning are the ones that would let the two disagree, or let
a dollar go missing between them — a plan whose halves contradict each other is
worse than one that will not load, and a plan that quietly totals less than it
says is worse than both.
"""

from __future__ import annotations

import pytest

from ffa.config.schema import ConfigError
from ffa.config.strategy import (
    BY_NAME,
    StrategyPreset,
    default_preset,
    flex_fraction,
    parse_strategy,
    position_of_slot,
    seed_slots,
    slot_keys,
    slot_split,
    strategy_to_dict,
)
from ffa.domain.enums import Position


# This league's real shape: three receivers and a flex, which is what makes
# `RB1/RB2` and a funded FLEX line worth having at all. `test_strategy.py` has a
# two-receiver snapshot for its own reasons; sharing it would make these
# assertions describe a roster nobody drafts.


@pytest.fixture
def snapshot():
    from ffa.domain.enums import RosterSlot
    from ffa.domain.models import LeagueSnapshot

    return LeagueSnapshot(
        league_id=1, year=2026, name="Test", draft_type="AUCTION", budget=200,
        team_count=12, my_team_id=4,
        roster={
            RosterSlot.QB: 1, RosterSlot.RB: 2, RosterSlot.WR: 3, RosterSlot.TE: 1,
            RosterSlot.FLEX: 1, RosterSlot.K: 1, RosterSlot.DST: 1,
            RosterSlot.BE: 5, RosterSlot.IR: 1,
        },
        flex_positions=(Position.RB, Position.WR, Position.TE),
    )


@pytest.fixture
def book():
    from pathlib import Path

    from ffa.reference.loader import ReferenceRow
    from ffa.reference.playerbook import PlayerBook

    rows, rank = [], 1
    for position, top in (
        (Position.RB, 60), (Position.WR, 55), (Position.TE, 20),
        (Position.QB, 18), (Position.K, 1), (Position.DST, 1),
    ):
        for i in range(40):
            name = f"{position.value}{i}"
            rows.append(ReferenceRow(
                key=name.lower(), name=name, position=position,
                auction_value=max(1, top - i), overall_rank=rank,
            ))
            rank += 1
    return PlayerBook(tuple(rows), source=Path("test.csv"))


# --- naming -----------------------------------------------------------------


def test_slots_are_named_the_way_you_would_say_them(snapshot):
    """An ordinal only where a position has more than one spot. Nobody calls
    their only tight end "TE1"."""
    assert slot_keys(snapshot) == (
        "QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "FLEX", "K", "DST",
    )


def test_slot_order_is_the_roster_not_the_market(snapshot):
    """These become rows on a screen read under a clock. A lineup that reorders
    itself as prices move is one you have to re-find every time you look."""
    keys = slot_keys(snapshot)
    assert keys.index("QB") < keys.index("RB1") < keys.index("FLEX") < keys.index("DST")


def test_flex_belongs_to_no_position():
    """Which is exactly why the plan could not budget for it until slots existed:
    `budget_by_position` is keyed by `Position`, and there is no FLEX member."""
    assert position_of_slot("FLEX") is None
    assert position_of_slot("RB1") is Position.RB
    assert position_of_slot("RB2") is Position.RB
    assert position_of_slot("TE") is Position.TE


# --- the split is the archetype, and nothing new ----------------------------


def test_concentration_shapes_the_split_within_a_position():
    """The archetype has always been a concentration preference and nothing
    else. Using it here means one dial for one idea, instead of inventing a
    second shaping rule beside the one the config already carries."""
    stars = slot_split(82, 2, BY_NAME["stars-and-scrubs"].max_share)
    hoarder = slot_split(82, 2, BY_NAME["hoarder"].max_share)

    assert stars[0] > hoarder[0], "stars-and-scrubs should front-load harder"
    assert stars[1] < hoarder[1]
    assert stars[0] / stars[1] > hoarder[0] / hoarder[1]


def test_a_split_totals_exactly_what_it_was_given():
    """Rounding down everywhere would leave dollars unassigned and a plan that
    quietly totals less than it claims."""
    for archetype in BY_NAME.values():
        for total, count in ((82, 2), (85, 3), (13, 1), (7, 4), (100, 5)):
            assert sum(slot_split(total, count, archetype.max_share)) == total


def test_no_starting_slot_is_budgeted_at_zero():
    """A starting spot you have budgeted nothing for is not a lean plan, it is
    an omission — the same argument as the bench floor."""
    assert min(slot_split(4, 3, 0.55)) >= 1
    assert min(slot_split(85, 3, 0.55)) >= 1


def test_one_slot_takes_the_whole_position():
    assert slot_split(13, 1, 0.35) == [13]


# --- flex money was always there --------------------------------------------


def test_flex_money_is_extracted_rather_than_invented(snapshot):
    """`starting_demand` counts a flex-eligible position as its own slots plus
    an even share of the flex slot, so RB's dollars have always included a third
    of one. The FLEX line is funded from where its money already was."""
    # RB: 2 native + 1/3 flex -> 1/7 of the budget was flex money.
    assert flex_fraction(snapshot, Position.RB) == pytest.approx(1 / 7, abs=0.01)
    assert flex_fraction(snapshot, Position.WR) == pytest.approx(1 / 10, abs=0.01)
    assert flex_fraction(snapshot, Position.TE) == pytest.approx(1 / 4, abs=0.01)
    # A quarterback can never fill the flex, so none of his money is flex money.
    assert flex_fraction(snapshot, Position.QB) == 0.0


def test_seeding_moves_money_and_never_creates_it(snapshot):
    """The plan totals what it totalled before slots existed."""
    planned = {Position.QB: 12, Position.RB: 82, Position.WR: 85,
               Position.TE: 13, Position.K: 1, Position.DST: 2}
    slots = seed_slots(snapshot, planned, 0.35)

    assert sum(slots.values()) == sum(planned.values())
    assert slots["FLEX"] > 0, "the flex slot should be funded"


def test_seeding_covers_every_starting_slot(snapshot):
    planned = {Position.QB: 12, Position.RB: 82, Position.WR: 85,
               Position.TE: 13, Position.K: 1, Position.DST: 2}
    assert set(seed_slots(snapshot, planned, 0.35)) == set(slot_keys(snapshot))


def test_a_seeded_plan_still_fits_the_budget(snapshot, book):
    """Slots plus the bench reserve is the whole budget, in every archetype."""
    for name in BY_NAME:
        preset = default_preset(snapshot, book, name)
        assert preset.planned_total + preset.bench_reserve == snapshot.budget


# --- positions are the sum, never a second opinion --------------------------


def test_positions_are_derived_from_slots():
    preset = StrategyPreset(budget_by_slot={"RB1": 50, "RB2": 32, "TE": 13})
    assert preset.by_position == {Position.RB: 82, Position.TE: 13}


def test_flex_contributes_to_no_position():
    """Folding its money into RB would make the running-back line mean two
    different things depending on who is reading it."""
    preset = StrategyPreset(budget_by_slot={"RB1": 50, "RB2": 32, "FLEX": 20})
    assert preset.by_position == {Position.RB: 82}
    # But the plan has still spoken for every dollar.
    assert preset.planned_total == 102


def test_a_plan_written_before_slots_existed_still_works():
    """Positions are all it has, and they are returned untouched."""
    preset = StrategyPreset(budget_by_position={Position.RB: 82})
    assert preset.by_position == {Position.RB: 82}
    assert preset.planned_total == 82
    assert preset.is_set


# --- the config will not hold two answers -----------------------------------


def test_declaring_both_is_refused_rather_than_reconciled():
    """Two sources for one number, and the positional one is the derivable half.
    A plan whose halves disagree is worse than one that will not load."""
    with pytest.raises(ConfigError, match="both budget_by_slot and"):
        parse_strategy({
            "budget_by_slot": {"RB1": 50, "RB2": 32},
            "budget_by_position": {"RB": 82},
        })


def test_a_slot_that_does_not_exist_is_refused():
    """`RB3` in a two-back league would otherwise parse, total, and never appear
    against anything — a silent hole in the plan."""
    with pytest.raises(ConfigError, match="unknown slot"):
        parse_strategy({"budget_by_slot": {"QQ9": 10}})


def test_slot_dollars_must_be_whole_and_positive():
    with pytest.raises(ConfigError, match="whole dollars"):
        parse_strategy({"budget_by_slot": {"RB1": "lots"}})
    with pytest.raises(ConfigError, match="cannot be negative"):
        parse_strategy({"budget_by_slot": {"RB1": -5}})


def test_slot_keys_are_read_case_insensitively():
    assert parse_strategy({"budget_by_slot": {"rb1": 50}}).budget_by_slot == {"RB1": 50}


# --- round trip -------------------------------------------------------------


def test_a_seeded_plan_survives_being_written_and_read_back(snapshot, book):
    """The file is the plan. A field that serialises and does not load is a
    plan you only have once."""
    preset = default_preset(snapshot, book, "stars-and-scrubs")
    again = parse_strategy(strategy_to_dict(preset))

    assert again.budget_by_slot == dict(preset.budget_by_slot)
    assert again.planned_total == preset.planned_total
    assert again.by_position == preset.by_position


def test_the_writer_never_emits_both_halves(snapshot, book):
    """Because `parse_strategy` refuses to read them back."""
    written = strategy_to_dict(default_preset(snapshot, book, "balanced"))
    assert "budget_by_slot" in written
    assert "budget_by_position" not in written


def test_an_old_plan_round_trips_as_positions():
    preset = StrategyPreset(archetype="balanced", budget_by_position={Position.RB: 82})
    written = strategy_to_dict(preset)

    assert "budget_by_position" in written
    assert "budget_by_slot" not in written
    assert parse_strategy(written).by_position == {Position.RB: 82}
