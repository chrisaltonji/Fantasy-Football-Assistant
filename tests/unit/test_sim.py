"""The simulator, and what it is for.

CP4's claim is that the sim is an *acceptance test*, not a toy: bots bid
against the same `projections` the advisory layer reads, so a bug in
`max_legal_bid` or `open_slots_by_pos` shows up here as a draft that is
visibly illegal rather than as a surprise on draft day.

So these tests assert the invariants a real auction cannot violate, not the
particular numbers a seed happens to produce.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ffa.domain import projections as proj
from ffa.domain.codec import dump_event
from ffa.domain.enums import Provenance
from ffa.domain.events import FieldAmended, PlayerNominated, PlayerSold
from ffa.domain.models import DraftState
from ffa.domain.reducers import apply, replay
from ffa.ingest.source import EventSource, SourceHealth
from ffa.reference.playerbook import load_playerbook
from ffa.sim.bots import ARCHETYPES, Bot, archetype_for, build_room
from ffa.sim.engine import AuctionSim
from ffa.sim.faults import FaultInjector, FaultProfile
from ffa.sim.source import SimSource

SAMPLE = Path(__file__).resolve().parents[2] / "data" / "fixtures" / "sample_rankings.csv"


@pytest.fixture(scope="module")
def book():
    return load_playerbook(SAMPLE, teams=12, budget=200)


@pytest.fixture
def sim(init_event, book):
    """A short run, for tests about mechanics rather than draft outcomes."""
    return AuctionSim(league=init_event, book=book, seed=42, pool_size=SMALL_POOL)


def run_to_state(sim: AuctionSim) -> DraftState:
    state = apply(DraftState.empty(sim.league.draft_id), sim.league)
    for event in sim.events():
        state = apply(state, event)
    return state


# A full draft is ~400 events over a 248-player board, and most tests below
# interrogate the same run. Simulating once per (seed, faults) keeps this file
# from dominating the suite's runtime.
_RUNS: dict[tuple, tuple] = {}

# Most tests here assert something about the *shape* of the output — provenance
# tags, determinism, the source seam — and do not care whether every roster
# filled. Those run against a truncated board, which is several times faster.
# Only the roster/budget invariants need the whole 248-player pool.
SMALL_POOL = 60


def simulate(init_event, book, seed: int, faults: FaultProfile | None = None,
             pool_size: int | None = None):
    """Return (events, final_state) for a run, simulating it at most once."""
    profile = faults or FaultProfile()
    cache_key = (seed, profile, pool_size)
    if cache_key not in _RUNS:
        sim = AuctionSim(league=init_event, book=book, seed=seed, faults=profile,
                         pool_size=pool_size)
        events = tuple(sim.events())
        state = apply(DraftState.empty(sim.league.draft_id), sim.league)
        for event in events:
            state = apply(state, event)
        _RUNS[cache_key] = (events, state)
    return _RUNS[cache_key]


# --- the invariants a real auction cannot violate ---------------------------


def test_no_team_ever_ends_up_over_budget(init_event, book):
    _, state = simulate(init_event, book, 42)
    for team_id in state.teams:
        assert proj.remaining_budget(state, team_id) >= 0, f"team {team_id} overspent"


def test_no_team_buys_more_players_than_it_has_slots(init_event, book):
    _, state = simulate(init_event, book, 42)
    slots = state.league.draftable_slots
    for team_id in state.teams:
        assert proj.roster_count(state, team_id) <= slots


def test_every_sale_respects_the_legal_ceiling(init_event, book):
    """The bid cap is the whole reason the sim is an acceptance test.

    Checked *before* each sale is folded in, which is when the ceiling
    actually binds.
    """
    sim = AuctionSim(league=init_event, book=book, seed=7, pool_size=120)
    state = apply(DraftState.empty("t"), sim.league)
    for event in sim.events():
        if isinstance(event, PlayerSold) and event.price.is_known:
            ceiling = proj.max_legal_bid(state, event.team.value)
            assert event.price.value <= ceiling, (
                f"{event.player.key} sold to team {event.team.value} for "
                f"${event.price.value}, above the ${ceiling} ceiling"
            )
        state = apply(state, event)


def test_a_clean_run_fills_every_roster(init_event, book):
    """No stranded teams when every observation is honest.

    This is what caught the pool bug: truncating the pool by value cuts
    kickers and defenses first, so teams could not fill K/DST and stranded
    their money — which looked like a bidding bug and was not.
    """
    _, state = simulate(init_event, book, 42)
    slots = state.league.draftable_slots
    rosters = {t: proj.roster_count(state, t) for t in state.teams}
    assert all(n == slots for n in rosters.values()), rosters


def test_the_run_produces_no_state_warnings(init_event, book):
    assert simulate(init_event, book, 42)[1].warnings == ()


def test_teams_spend_most_of_their_budget(init_event, book):
    """A sanity band, not a pin.

    The real 2025 auction cleared at 99.5% of the money in the room. The bots
    land lower because a manager who is outbid early cannot retroactively
    reallocate; the band is wide enough to allow that and narrow enough to
    catch a room that has stopped bidding altogether.
    """
    _, state = simulate(init_event, book, 42)
    league = state.league
    total = league.budget * len(state.teams)
    spent = sum(proj.spent(state, t) for t in state.teams)
    assert 0.80 <= spent / total <= 1.0, f"spent ${spent} of ${total}"


# --- determinism ------------------------------------------------------------


def test_the_same_seed_reproduces_the_draft_exactly(init_event, book):
    """Without this every failure is an anecdote instead of a bug report."""
    def run():
        sim = AuctionSim(league=init_event, book=book, seed=5, pool_size=SMALL_POOL)
        return [dump_event(e) for e in sim.events()]

    first, second = run(), run()
    assert first == second
    assert len(first) >= SMALL_POOL


def test_different_seeds_produce_different_drafts(init_event, book):
    events = [simulate(init_event, book, seed, pool_size=SMALL_POOL)[0] for seed in (1, 2)]
    a, b = ([dump_event(e) for e in run] for run in events)
    assert a != b


# --- provenance -------------------------------------------------------------


def test_every_simulated_event_is_tagged_sim(init_event, book):
    """Sim output must never be mistakable for an ESPN or manual observation."""
    for event in simulate(init_event, book, 42, pool_size=SMALL_POOL)[0]:
        assert event.source == Provenance.SIM.value
        if isinstance(event, PlayerSold) and event.price.is_known:
            assert event.price.provenance is Provenance.SIM


def test_a_sale_carries_the_nominating_team(init_event, book):
    events = simulate(init_event, book, 42, pool_size=SMALL_POOL)[0]
    nominations = [e for e in events if isinstance(e, PlayerNominated)]
    assert nominations
    assert all(n.nominated_by is not None and n.nominated_by.is_known for n in nominations)


# --- fault injection: the partial-provenance path ---------------------------


def test_dropping_prices_produces_unknown_prices(init_event, book):
    sim = AuctionSim(
        league=init_event, book=book, seed=3,
        faults=FaultProfile(drop_prices=0.5, late_prices=0.0),
       pool_size=SMALL_POOL,
    )
    sales = [e for e in sim.events() if isinstance(e, PlayerSold)]
    unknown = [e for e in sales if not e.price.is_known]
    assert unknown, "no prices were dropped at a 50% rate"
    assert all(e.price.provenance is Provenance.UNKNOWN for e in unknown)


def test_late_prices_arrive_as_amendments_and_are_merged(init_event, book):
    """The flagship path: sale first, price later.

    `merge`'s knowledge-beats-ignorance rule is what makes the correction
    land, and this is the only test that exercises it end to end at scale.
    """
    sim = AuctionSim(
        league=init_event, book=book, seed=3,
        faults=FaultProfile(drop_prices=0.4, late_prices=1.0),
       pool_size=SMALL_POOL,
    )
    events = list(sim.events())
    amendments = [e for e in events if isinstance(e, FieldAmended)]
    assert amendments, "no late prices were emitted"
    assert all(a.field_name == "price" for a in amendments)

    state = apply(DraftState.empty("t"), sim.league)
    for event in events:
        state = apply(state, event)

    # Every dropped price was recovered, so nothing should still be unknown.
    assert sum(proj.unknown_price_count(state, t) for t in state.teams) == 0


def test_prices_that_never_arrive_stay_unknown(init_event, book):
    _, state = simulate(init_event, book, 3, FaultProfile(drop_prices=0.5, late_prices=0.0),
                        pool_size=SMALL_POOL)
    assert sum(proj.unknown_price_count(state, t) for t in state.teams) > 0


def test_incomplete_prices_can_push_a_team_over_budget_and_that_is_flagged(init_event, book):
    """Bidding on floored information over-commits. The sim proves it.

    `remaining_budget` charges an unknown price at the $1 minimum, which
    *over*-estimates what a team has left. That is the safe direction for
    judging a rival's threat capacity — better to over-estimate their ammo —
    but it is the dangerous direction for deciding your own ceiling. A bidder
    who trusts it spends money it does not have, and the overspend only
    surfaces when the real prices arrive.

    The engine's job is not to prevent this; ESPN is the truth about what
    happened. Its job is to notice and say so, which is what the warning is.
    """
    _, state = simulate(init_event, book, 11, FaultProfile(drop_prices=0.3))

    overspent = [t for t in state.teams if proj.remaining_budget(state, t) < 0]
    assert overspent, "expected the floored budget to cause over-commitment"
    assert any("over budget" in w for w in state.warnings)
    # Still coherent: a flagged overspend, not corrupted state.
    assert all(
        proj.roster_count(state, t) <= state.league.draftable_slots for t in state.teams
    )


def test_a_clean_run_never_pushes_a_team_over_budget(init_event, book):
    """The same run with honest prices stays inside every budget."""
    _, state = simulate(init_event, book, 42)
    assert [t for t in state.teams if proj.remaining_budget(state, t) < 0] == []


def test_dropping_positions_leaves_the_position_unset(init_event, book):
    sim = AuctionSim(
        league=init_event, book=book, seed=3,
        faults=FaultProfile(drop_positions=0.5),
       pool_size=SMALL_POOL,
    )
    sales = [e for e in sim.events() if isinstance(e, PlayerSold)]
    assert any(e.position is None for e in sales)


@pytest.mark.parametrize("field", ["drop_prices", "drop_positions", "late_prices"])
@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_fault_rates_outside_zero_to_one_are_rejected(field, bad):
    with pytest.raises(ValueError, match=field):
        FaultProfile(**{field: bad})


def test_a_clean_profile_reports_itself_as_clean():
    assert FaultProfile().is_clean
    assert "clean" in FaultProfile().describe()
    assert not FaultProfile(drop_prices=0.1).is_clean


def test_the_injector_counts_what_it_hid():
    import random

    injector = FaultInjector(FaultProfile(drop_prices=1.0, late_prices=1.0), random.Random(0))
    for _ in range(5):
        assert injector.price_is_visible() is False
        injector.price_arrives_late()
    assert injector.prices_dropped == 5
    assert injector.prices_recovered == 5
    assert "5 price(s) dropped" in injector.summary()


# --- the EventSource seam ---------------------------------------------------


def test_the_sim_is_just_another_event_source(sim):
    """No `if simulating:` anywhere — it satisfies the same Protocol."""
    source = SimSource(sim)
    assert isinstance(source, EventSource)
    assert source.status().health is SourceHealth.OK


def test_the_source_feeds_a_queue_like_any_producer(sim):
    import queue

    out: "queue.Queue" = queue.Queue()
    SimSource(sim).start(out)
    assert out.qsize() >= SMALL_POOL


def test_stopping_the_source_halts_the_draft(sim):
    source = SimSource(sim)
    seen = 0
    for _ in source.events():
        seen += 1
        if seen == 10:
            source.stop()
    assert seen == 10
    assert source.status().health is SourceHealth.STOPPED


# --- the journal a sim writes is a real journal ------------------------------


def test_a_simulated_draft_replays_identically(sim):
    """The point of running it through the real store: resume must work.

    Folding events forward incrementally and replaying them from scratch have
    to agree, or a crash mid-sim would resume into a different draft.
    """
    events = list(sim.events())
    incremental = apply(DraftState.empty(sim.league.draft_id), sim.league)
    for event in events:
        incremental = apply(incremental, event)

    stamped = [sim.league, *events]
    for index, event in enumerate(stamped, start=1):
        object.__setattr__(event, "id", index)
    replayed = replay(stamped, sim.league.draft_id)

    assert replayed.players == incremental.players
    assert replayed.teams == incremental.teams


def test_the_store_accepts_every_simulated_event(tmp_path, sim):
    from ffa.state.store import DraftStore

    with DraftStore.create(tmp_path / "run", sim.league) as store:
        for event in sim.events():
            store.dispatch(event)
        state = store.state

    assert state.warnings == ()
    assert len(state.sold_players()) == SMALL_POOL
    assert (tmp_path / "run" / "events.jsonl").read_bytes().count(b"\r\n") == 0


# --- bots -------------------------------------------------------------------


def test_a_bot_never_bids_above_its_legal_ceiling(init_event, book):
    import random

    state = apply(DraftState.empty("t"), init_event)
    bot = Bot(3, archetype_for(0), random.Random(0))
    key = book.rows[0].key
    assert bot.willingness(state, book, key) <= proj.max_legal_bid(state, 3)


def test_a_bot_with_no_budget_bids_nothing(init_event, book):
    import random

    state = apply(DraftState.empty("t"), init_event)
    bot = Bot(3, archetype_for(0), random.Random(0))
    # Fill the roster so there is no legal room left.
    from tests.conftest import sold

    for index, row in enumerate(book.rows[: state.league.draftable_slots]):
        state = apply(state, sold(index + 10, row.name, 3, 13))
    assert proj.max_legal_bid(state, 3) == 0
    assert bot.willingness(state, book, book.rows[0].key) == 0


def test_the_room_has_one_bot_per_team_with_spread_archetypes(init_event):
    import random

    ids = [t.team_id for t in init_event.teams]
    room = build_room(ids, random.Random(1))
    assert sorted(room) == sorted(ids)
    assert len({b.archetype.name for b in room.values()}) == len(ARCHETYPES)


def test_a_seat_can_be_left_open_for_a_human(init_event):
    import random

    ids = [t.team_id for t in init_event.teams]
    room = build_room(ids, random.Random(1), exclude=4)
    assert 4 not in room
    assert len(room) == len(ids) - 1


def test_a_human_seat_never_gets_bid_on_its_behalf(init_event, book):
    sim = AuctionSim(league=init_event, book=book, seed=9, human_team=4, pool_size=SMALL_POOL)
    state = run_to_state(sim)  # human_team is not part of the cache key
    assert proj.roster_count(state, 4) == 0
    assert proj.remaining_budget(state, 4) == state.league.budget
