"""C2 — the declared plan, and capability 12's adherence to it.

The plan is the second thing in this codebase that is testimony rather than
arithmetic, after the owner dossiers. Most of what is tested here is therefore
about keeping that boundary intact: a declared preference must not move a
computed number, and an empty plan must never render as a plan of zeroes.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ffa.advice.bidding import guidance_for
from ffa.advice.market import market_state
from ffa.advice.strategy import MATERIAL, strategy_read
from ffa.cli import render
from ffa.config.schema import ConfigError, LeagueConfig
from ffa.config.strategy import (
    ARCHETYPES,
    BY_NAME,
    StrategyPreset,
    bench_floor,
    default_preset,
    market_shape,
    opening_max_bid,
    parse_strategy,
    strategy_problem,
    strategy_to_dict,
)
from ffa.domain.enums import Position, RosterSlot
from ffa.domain.events import DraftInitialized, TeamSeed
from ffa.domain.ids import normalize_player_key
from ffa.domain.models import LeagueSnapshot
from ffa.domain.reducers import replay
from ffa.reference.loader import ReferenceRow
from ffa.reference.playerbook import PlayerBook
from tests.conftest import at, sold

SEATS = (1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13)


@pytest.fixture
def snapshot() -> LeagueSnapshot:
    return LeagueSnapshot(
        league_id=1, year=2026, name="Test", draft_type="AUCTION", budget=200,
        team_count=12, my_team_id=4,
        roster={
            RosterSlot.QB: 1, RosterSlot.RB: 2, RosterSlot.WR: 2, RosterSlot.TE: 1,
            RosterSlot.FLEX: 1, RosterSlot.K: 1, RosterSlot.DST: 1, RosterSlot.BE: 5,
            RosterSlot.IR: 1,
        },
        flex_positions=(Position.RB, Position.WR, Position.TE),
    )


def row(name: str, position: Position, value: float, rank: int) -> ReferenceRow:
    return ReferenceRow(
        key=normalize_player_key(name), name=name, position=position,
        auction_value=value, overall_rank=rank,
    )


@pytest.fixture
def book() -> PlayerBook:
    rows = []
    rank = 1
    # Enough depth at each position that `starting_demand` has something to read.
    for position, top in (
        (Position.RB, 60), (Position.WR, 55), (Position.TE, 20),
        (Position.QB, 18), (Position.K, 1), (Position.DST, 1),
    ):
        for i in range(40):
            rows.append(row(f"{position.value}{i}", position, max(1, top - i), rank))
            rank += 1
    return PlayerBook(tuple(rows), source=Path("test.csv"))


def state_for(snapshot: LeagueSnapshot, events=()):
    init = DraftInitialized(
        id=1, at=at(), draft_id="d", league=snapshot,
        teams=tuple(TeamSeed(team_id=i, manager=f"mgr{i}") for i in SEATS),
    )
    return replay((init, *events))


# --- the plan is derived, not invented -------------------------------------------------


def test_the_default_split_comes_from_the_market_not_an_opinion(snapshot, book):
    """An archetype could have shipped a hardcoded 'RB 35%' table, and that
    table would be somebody's opinion wearing the costume of a default."""
    shares = market_shape(snapshot, book)

    assert set(shares) <= set(Position)
    assert sum(shares.values()) == pytest.approx(1.0)
    # RB and WR carry the money in this book, and the split says so because the
    # values say so — not because anything here asserts it.
    assert shares[Position.RB] > shares[Position.TE]
    assert shares[Position.K] < shares[Position.QB]


def test_a_default_plan_spends_exactly_the_budget(snapshot, book):
    preset = default_preset(snapshot, book, "balanced")

    assert preset.planned_total + preset.bench_reserve == snapshot.budget
    assert preset.max_on_one_player == round(200 * BY_NAME["balanced"].max_share)
    assert strategy_problem(preset, snapshot.budget) is None


@pytest.mark.parametrize("archetype", [a.name for a in ARCHETYPES])
def test_every_archetype_produces_a_followable_plan(snapshot, book, archetype):
    preset = default_preset(snapshot, book, archetype)
    assert strategy_problem(preset, snapshot.budget) is None
    assert preset.is_set


def test_archetypes_differ_in_concentration_not_in_positional_taste(snapshot, book):
    """"Stars and scrubs" is a claim about spending in fewer, larger pieces —
    not a claim that running backs deserve more of the budget. The positional
    split is derivable, so it is derived, and only the concentration changes."""
    stars = default_preset(snapshot, book, "stars-and-scrubs")
    hoarder = default_preset(snapshot, book, "hoarder")

    assert stars.max_on_one_player > hoarder.max_on_one_player
    assert stars.bench_reserve < hoarder.bench_reserve
    # Same shape underneath: RB still outranks TE for both of them.
    for preset in (stars, hoarder):
        assert preset.budget_by_position[Position.RB] > preset.budget_by_position[Position.TE]


def test_an_unknown_archetype_is_refused(snapshot, book):
    with pytest.raises(ValueError, match="unknown archetype"):
        default_preset(snapshot, book, "moneyball")


def test_no_market_gives_half_a_plan_rather_than_an_invented_one(snapshot):
    """Half a plan you can see beats a whole one you cannot check."""
    preset = default_preset(snapshot, PlayerBook((), source=Path("x.csv")), "balanced")

    assert preset.budget_by_position == {}
    assert preset.max_on_one_player > 0


# --- the bench floor, which is derivable rather than preferred ------------------------


def test_the_bench_floor_is_one_dollar_per_bench_slot(snapshot):
    """Needs no history and no archetype to be true, which is what makes it a
    floor rather than an opinion."""
    assert bench_floor(snapshot) == 5  # BE: 5 in this roster


def test_a_reserve_below_the_floor_cannot_be_filled(snapshot, book):
    """Not a lean plan — an unfollowable one. You would reach the last round
    with slots open and nothing earmarked for them."""
    preset = replace(default_preset(snapshot, book), bench_reserve=2)

    problem = strategy_problem(preset, snapshot.budget, snapshot)
    assert problem and "cannot be filled" in problem


def test_the_default_never_lands_below_the_floor(snapshot, book):
    preset = default_preset(snapshot, book, bench_reserve=0)
    assert preset.bench_reserve == bench_floor(snapshot)


def test_freeing_the_reserve_puts_the_money_back_into_positions(snapshot, book):
    """The whole point of the override: the archetype's bench share is the one
    number here with nothing behind it, and a league's own record can flatly
    contradict it. Four seasons of this league put the median bench at the
    floor, against an archetype default four times that."""
    default = default_preset(snapshot, book, "balanced")
    lean = default_preset(snapshot, book, "balanced", bench_reserve=bench_floor(snapshot))

    assert lean.bench_reserve < default.bench_reserve
    assert lean.planned_total > default.planned_total
    # Nothing is created or lost — it moves.
    assert (lean.planned_total + lean.bench_reserve
            == default.planned_total + default.bench_reserve
            == snapshot.budget)
    # And it lands where the market says, so the richest position gains most.
    richest = max(default.budget_by_position, key=lambda p: default.budget_by_position[p])
    assert lean.budget_by_position[richest] > default.budget_by_position[richest]


def test_a_plan_with_no_positions_is_not_held_to_the_floor(snapshot):
    """Half a plan — concentration only, no split — has nothing to under-fund."""
    preset = StrategyPreset(archetype="balanced", max_on_one_player=70)
    assert strategy_problem(preset, snapshot.budget, snapshot) is None


# --- the cap ceiling, also derived ----------------------------------------------------


def test_the_cap_ceiling_is_what_you_could_actually_bid_on_pick_one(snapshot):
    """Every other roster slot still costs $1, so that money is not available
    for this player. Same arithmetic as `max_legal_bid`, on an empty roster."""
    # $200 budget, 14 draftable slots: 13 others at $1 leaves $187.
    assert opening_max_bid(snapshot) == snapshot.budget - (snapshot.draftable_slots - 1)


def test_a_cap_above_the_legal_ceiling_is_inert_and_says_so(snapshot, book):
    """Not aggressive — inert. You could never reach the number, so a cap set
    there silently never binds, which is worse than no cap at all."""
    preset = replace(default_preset(snapshot, book), max_on_one_player=195)

    problem = strategy_problem(preset, snapshot.budget, snapshot)
    assert problem and "can never bind" in problem


def test_a_cap_at_the_ceiling_is_allowed(snapshot, book):
    preset = replace(
        default_preset(snapshot, book), max_on_one_player=opening_max_bid(snapshot)
    )
    assert strategy_problem(preset, snapshot.budget, snapshot) is None


def test_the_cap_can_be_declared_outright(snapshot, book):
    """The archetype's share is a starting point, not the only way to say it."""
    preset = default_preset(snapshot, book, "balanced", max_on_one_player=75)

    assert preset.max_on_one_player == 75
    # And it does not disturb the split, which is derived separately.
    assert preset.budget_by_position == default_preset(snapshot, book, "balanced").budget_by_position


# --- config round trip -----------------------------------------------------------------


def test_a_plan_survives_a_write_and_a_read(snapshot, book):
    preset = default_preset(snapshot, book, "value-hunter")
    assert parse_strategy(strategy_to_dict(preset)) == preset


def test_no_strategy_table_is_a_normal_state():
    empty = parse_strategy(None)
    assert empty.is_set is False
    assert strategy_problem(empty, 200) is None


@pytest.mark.parametrize(
    "bad, match",
    [
        ({"archetype": "moneyball"}, "archetype"),
        ({"budget_by_position": {"LB": 10}}, "unknown position"),
        ({"budget_by_position": {"RB": "lots"}}, "whole dollars"),
        ({"budget_by_position": {"RB": -5}}, "negative"),
        ({"max_on_one_player": -1}, "negative"),
    ],
)
def test_a_malformed_plan_raises_rather_than_degrades(bad, match):
    """Dropping a bad position silently would leave you drafting against a plan
    missing a position you thought you had declared, and the readout would look
    perfectly healthy the whole time."""
    with pytest.raises(ConfigError, match=match):
        parse_strategy(bad)


def test_a_plan_that_cannot_be_afforded_is_caught_at_load():
    config = LeagueConfig(
        league_id=1, year=2026, team_count=12, team_ids=SEATS, budget=200,
        strategy=StrategyPreset(
            budget_by_position={Position.RB: 190, Position.WR: 190},
            bench_reserve=10,
        ),
    )
    with pytest.raises(ConfigError, match="commits"):
        config.validate()


def test_a_one_player_cap_above_the_budget_is_caught():
    preset = StrategyPreset(max_on_one_player=250)
    assert "more than the whole" in (strategy_problem(preset, 200) or "")


def test_a_plan_is_carried_forward_across_a_rebuild():
    """ESPN has no opinion about your plan, so `config init --force` — the
    documented recovery command — must not be the thing that wipes it."""
    from ffa.config.carry import CARRIED_FORWARD, FROM_ESPN

    assert "strategy" in CARRIED_FORWARD
    assert "strategy" not in FROM_ESPN


# --- capability 12: adherence ----------------------------------------------------------


@pytest.fixture
def preset() -> StrategyPreset:
    return StrategyPreset(
        archetype="balanced",
        budget_by_position={
            Position.QB: 15, Position.RB: 70, Position.WR: 70,
            Position.TE: 15, Position.K: 1, Position.DST: 2,
        },
        max_on_one_player=70,
        bench_reserve=27,
    )


def test_no_plan_reads_as_nothing_rather_than_zeroes(snapshot, book):
    assert strategy_read(state_for(snapshot), book, None) is None
    assert strategy_read(state_for(snapshot), book, StrategyPreset()) is None


def test_an_untouched_draft_is_not_off_plan_at_every_position(snapshot, book, preset):
    """The dry run caught this at pick 0: every position showed 'materially off
    plan' because none of the plan had happened yet."""
    read = strategy_read(state_for(snapshot), book, preset)

    assert read.spent_total == 0
    assert read.off_plan == ()


def test_overspending_counts_immediately(snapshot, book, preset):
    """Money already gone is exactly as gone at pick 12 as at pick 180."""
    state = state_for(snapshot, [sold(2, "RB0", 4, 90, pos=Position.RB, seconds=1)])
    read = strategy_read(state, book, preset)

    rb = next(p for p in read.positions if p.position is Position.RB)
    assert rb.variance == 20
    assert rb.is_material is True
    assert read.off_plan[0].position is Position.RB


def test_underspending_waits_until_the_position_is_finished(snapshot, book, preset):
    """With slots still open, 'under plan' is the plan not having happened yet."""
    state = state_for(snapshot, [sold(2, "TE0", 4, 2, pos=Position.TE, seconds=1)])
    read = strategy_read(state, book, preset)

    te = next(p for p in read.positions if p.position is Position.TE)
    assert te.variance == -13
    assert te.open_slots > 0
    assert te.is_material is False


def test_a_finished_position_reports_its_underspend(snapshot, book, preset):
    """Fill every slot a tight end could occupy, and the number is final."""
    events = [sold(2, "TE0", 4, 2, pos=Position.TE, seconds=1)]
    # TE, FLEX and all five bench spots — every slot `slots_for_position` lists
    # for a tight end. Eight running backs fill RB1, RB2, FLEX and the bench.
    for i in range(8):
        events.append(
            sold(3 + i, f"RB{i}", 4, 1, pos=Position.RB, seconds=2 + i)
        )
    state = state_for(snapshot, events)
    read = strategy_read(state, book, preset)

    te = next(p for p in read.positions if p.position is Position.TE)
    assert te.open_slots == 0
    assert te.is_material is True
    assert te.still_calls_for == 0, "a finished position releases its underspend"


def test_a_small_wobble_is_not_worth_a_line(snapshot, book, preset):
    state = state_for(
        snapshot, [sold(2, "QB0", 4, 15 + MATERIAL - 1, pos=Position.QB, seconds=1)]
    )
    read = strategy_read(state, book, preset)
    assert next(p for p in read.positions if p.position is Position.QB).is_material is False


def test_the_shortfall_says_when_the_plan_is_finished(snapshot, book, preset):
    """The one number that can tell you to stop following the plan."""
    state = state_for(snapshot, [sold(2, "RB0", 4, 185, pos=Position.RB, seconds=1)])
    read = strategy_read(state, book, preset)

    assert read.remaining == 15
    assert read.still_calls_for > read.remaining
    assert read.shortfall == read.still_calls_for - read.remaining
    assert read.slack == 0


def test_a_healthy_plan_has_no_shortfall(snapshot, book, preset):
    read = strategy_read(state_for(snapshot), book, preset)
    assert read.shortfall == 0
    assert read.slack > 0


def test_the_cap_records_a_breach_without_preventing_one(snapshot, book, preset):
    state = state_for(snapshot, [sold(2, "RB0", 4, 90, pos=Position.RB, seconds=1)])
    read = strategy_read(state, book, preset)

    assert read.biggest_buy == 90
    assert read.breached_cap is True


def test_a_pick_with_no_position_still_counts_against_the_total(snapshot, book, preset):
    """It cannot be attributed, so it is counted where it is true and nowhere
    else — which is why the per-position figures never derive the total."""
    state = state_for(snapshot, [sold(2, "Nobody At All", 4, 30, seconds=1)])
    read = strategy_read(state, book, preset)

    assert read.spent_total == 30
    assert sum(p.spent for p in read.positions) == 0


# --- the boundary: a preference must not move a computed number ------------------------


def test_the_plan_cap_never_moves_the_bid(snapshot, book, preset):
    """The load-bearing test. `max_advisable_bid` is what the market says he is
    worth; folding a declared preference into it would produce a readout saying
    a player is worth $58 because you once said $58."""
    state = state_for(snapshot)
    market = market_state(state, book)
    key = normalize_player_key("RB0")

    without = guidance_for(state, book, key, market)
    tight = guidance_for(
        state, book, key, market, strategy=replace(preset, max_on_one_player=1)
    )

    assert tight.max_advisable_bid == without.max_advisable_bid
    assert tight.suggested_low == without.suggested_low
    assert tight.suggested_high == without.suggested_high
    assert tight.max_legal_bid == without.max_legal_bid
    assert tight.safe_legal_bid == without.safe_legal_bid
    # It is carried, and it is visible, and that is all it does.
    assert tight.plan_cap == 1
    assert tight.exceeds_plan_cap is True
    assert without.plan_cap == 0


def test_the_cap_is_surfaced_as_yours_to_overrule(snapshot, book, preset):
    state = state_for(snapshot)
    guidance = guidance_for(
        state, book, normalize_player_key("RB0"), market_state(state, book),
        strategy=replace(preset, max_on_one_player=1),
    )
    text = render.render_guidance(guidance)

    assert "plan cap" in text
    assert "not applied" in text


# --- the readout -----------------------------------------------------------------------


def test_no_plan_renders_as_no_plan():
    text = render.render_plan(None)
    assert "no draft plan" in text
    assert "$0" not in text


def test_the_readout_marks_the_planned_figures_as_declared(snapshot, book, preset):
    """Every other number this tool prints is computed. Said once, on screen,
    rather than trusted to be remembered."""
    text = render.render_plan(strategy_read(state_for(snapshot), book, preset))
    assert "declared" in text


def test_the_readout_leads_with_a_dead_plan(snapshot, book, preset):
    state = state_for(snapshot, [sold(2, "RB0", 4, 185, pos=Position.RB, seconds=1)])
    text = render.render_plan(strategy_read(state, book, preset))

    assert "SHORT" in text


def test_reachable_is_not_the_same_claim_as_on_plan(snapshot, book, preset):
    """A draft can be perfectly affordable while being $45 under at receiver.
    The headline is about money left, and must not read as an all-clear."""
    text = render.render_plan(strategy_read(state_for(snapshot), book, preset))

    assert "reachable" in text
    assert "on track" not in text
