from __future__ import annotations

from pathlib import Path

import pytest

from ffa.advice.bidding import guidance_for, has_starter_gap, threats_for
from ffa.advice.engine import advise
from ffa.advice.market import OVERPAY_MARGIN, inflated_value, market_state, value_alert
from ffa.advice.scarcity import scarcity_by_position, starting_demand
from ffa.domain import reducers
from ffa.domain.enums import Position
from ffa.domain.ids import normalize_player_key
from ffa.reference.loader import LoadReport, ReferenceRow
from ffa.reference.playerbook import PlayerBook
from tests.conftest import sold


def ref(name, pos, value, rank):
    return ReferenceRow(
        key=normalize_player_key(name), name=name, position=pos,
        auction_value=float(value), overall_rank=rank,
    )


@pytest.fixture
def book() -> PlayerBook:
    rows = []
    rank = 1
    # Enough depth that "startable" boundaries are meaningful.
    for pos, count, top in [
        (Position.RB, 40, 60), (Position.WR, 40, 58),
        (Position.QB, 20, 38), (Position.TE, 20, 28),
        (Position.K, 12, 2), (Position.DST, 12, 3),
    ]:
        for n in range(count):
            rows.append(ref(f"{pos.value} Player{n}", pos, max(1, top - n * (top / count)), rank))
            rank += 1
    return PlayerBook.from_report(LoadReport(source=Path("test"), rows=tuple(rows)))


def state_of(init_event, *events):
    return reducers.replay([init_event, *events])


# --- scarcity -----------------------------------------------------------------


def test_starting_demand_counts_flex_share(init_event):
    league = init_event.league
    # 12 teams x (2 RB + 1/3 of a FLEX) = 28
    assert starting_demand(league, Position.RB) == 28
    # QB isn't flex-eligible in this league.
    assert starting_demand(league, Position.QB) == 12


def test_scarcity_splits_remaining_stock(init_event, book):
    scarcity = scarcity_by_position(state_of(init_event), book)
    rb = scarcity[Position.RB]
    assert rb.total_remaining == 40
    assert rb.elite + rb.startable + rb.bench == 40
    # Elite is a third of starting demand, so ~10 of 28.
    assert rb.elite == 10


def test_drafting_players_reduces_remaining_stock(init_event, book):
    top = sorted(book.by_position(Position.RB), key=lambda r: r.overall_rank)[:5]
    events = [sold(i + 2, r.name, 1, 10, pos=Position.RB) for i, r in enumerate(top)]
    scarcity = scarcity_by_position(state_of(init_event, *events), book)
    assert scarcity[Position.RB].elite == 5
    assert scarcity[Position.RB].total_remaining == 35


def test_a_position_is_flagged_when_it_dries_up(init_event, book):
    """Elite gone and startable stock nearly exhausted."""
    rbs = sorted(book.by_position(Position.RB), key=lambda r: r.overall_rank)[:29]
    events = [sold(i + 2, r.name, (i % 12) + 1, 1, pos=Position.RB) for i, r in enumerate(rbs)]
    assert scarcity_by_position(state_of(init_event, *events), book)[Position.RB].is_drying_up


def test_scarcity_is_empty_without_reference_data(init_event):
    assert scarcity_by_position(state_of(init_event), PlayerBook.empty()) == {}


# --- market -------------------------------------------------------------------


def test_inflation_needs_a_sample_before_it_reports(init_event, book):
    """Early noise would swing every downstream threshold."""
    rows = sorted(book.rows, key=lambda r: r.overall_rank)[:3]
    events = [sold(i + 2, r.name, 1, 99, pos=r.position) for i, r in enumerate(rows)]
    assert market_state(state_of(init_event, *events), book).inflation_ratio is None


def test_inflation_is_measured_once_there_is_a_sample(init_event, book):
    rows = sorted(book.rows, key=lambda r: r.overall_rank)[:8]
    events = [
        sold(i + 2, r.name, (i % 12) + 1, round(book.value(r.key) * 1.2), pos=r.position)
        for i, r in enumerate(rows)
    ]
    market = market_state(state_of(init_event, *events), book)
    assert market.inflation_ratio == pytest.approx(1.2, abs=0.03)
    assert market.read == "inflated"


def test_a_cold_room_reads_deflated(init_event, book):
    rows = sorted(book.rows, key=lambda r: r.overall_rank)[:8]
    events = [
        sold(i + 2, r.name, (i % 12) + 1, max(1, round(book.value(r.key) * 0.7)), pos=r.position)
        for i, r in enumerate(rows)
    ]
    assert market_state(state_of(init_event, *events), book).read == "deflated"


def test_unknown_prices_are_excluded_from_the_ratio(init_event, book):
    rows = sorted(book.rows, key=lambda r: r.overall_rank)[:8]
    events = [sold(i + 2, r.name, 1, None, pos=r.position) for i, r in enumerate(rows)]
    assert market_state(state_of(init_event, *events), book).inflation_ratio is None


def test_inflated_value_restates_a_price_at_market(init_event, book):
    rows = sorted(book.rows, key=lambda r: r.overall_rank)[:8]
    events = [
        sold(i + 2, r.name, (i % 12) + 1, round(book.value(r.key) * 1.2), pos=r.position)
        for i, r in enumerate(rows)
    ]
    state = state_of(init_event, *events)
    market = market_state(state, book)
    target = rows[0].key
    assert inflated_value(book, target, market) > book.value(target)


def test_overpay_threshold_is_relative_to_the_market(init_event, book):
    """The capability spec requires a dynamic threshold, not a fixed percent."""
    state = state_of(init_event)
    market = market_state(state, book)
    key = book.rows[0].key
    value = book.value(key)

    fair = value_alert(state, book, key, value, market)
    assert not fair.over

    steep = value_alert(state, book, key, round(value * (1 + OVERPAY_MARGIN) + 2), market)
    assert steep.over and steep.delta > 0


def test_no_alert_for_a_player_with_no_reference_value(init_event, book):
    assert value_alert(state_of(init_event), book, "nobody", 40, market_state(state_of(init_event), book)) is None


# --- threats and bidding ------------------------------------------------------


def test_a_team_with_no_room_at_a_position_is_not_a_live_threat(init_event, book):
    """The central claim of the whole tool."""
    rbs = sorted(book.by_position(Position.RB), key=lambda r: r.overall_rank)[:3]
    # Team 3 fills RB, RB, and FLEX — rich, but done at running back.
    events = [sold(i + 2, r.name, 3, 5, pos=Position.RB) for i, r in enumerate(rbs)]
    state = state_of(init_event, *events)

    assert not has_starter_gap(state, 3, Position.RB)
    threat = next(t for t in threats_for(state, Position.RB) if t.team_id == 3)
    assert threat.remaining > 150          # plenty of money
    assert threat.can_bid                  # could physically bid
    assert not threat.is_live              # but is not a threat here
    # Same team is still dangerous where it has a hole.
    assert next(t for t in threats_for(state, Position.QB) if t.team_id == 3).is_live


def test_threats_exclude_our_own_team(init_event, book):
    ids = {t.team_id for t in threats_for(state_of(init_event), Position.RB, exclude_team=4)}
    assert 4 not in ids and len(ids) == 11


def test_guidance_prices_against_the_sheet(init_event, book):
    state = state_of(init_event)
    key = book.by_position(Position.RB)[0].key
    guidance = guidance_for(state, book, key, market_state(state, book))
    assert guidance.reference_value == book.value(key)
    assert guidance.fills_starter_gap
    assert guidance.suggested_low <= guidance.max_advisable_bid <= guidance.suggested_high


def test_a_bench_only_player_is_discounted(init_event, book):
    """Same sheet value is worth less to us if it fills no starting slot."""
    state = state_of(init_event)
    key = book.by_position(Position.QB)[0].key
    full = guidance_for(state, book, key, market_state(state, book))

    qb = book.by_position(Position.QB)[1]
    after = state_of(init_event, sold(2, qb.name, 4, 30, pos=Position.QB))
    discounted = guidance_for(after, book, key, market_state(after, book))

    assert full.fills_starter_gap and not discounted.fills_starter_gap
    assert discounted.max_advisable_bid < full.max_advisable_bid
    assert any("would not fill a starting slot" in r for r in discounted.reasons)


def test_advisable_never_exceeds_the_legal_ceiling(init_event, book):
    """max_legal_bid is a hard wall; max_advisable_bid must respect it."""
    filler = [r for r in book.rows][:14]
    events = [sold(i + 2, r.name, 4, 13, pos=r.position) for i, r in enumerate(filler)]
    state = state_of(init_event, *events)
    key = book.by_position(Position.RB)[0].key
    guidance = guidance_for(state, book, key, market_state(state, book))
    assert guidance.max_advisable_bid <= guidance.max_legal_bid


def test_a_player_missing_from_the_sheet_yields_the_ceiling_and_says_so(init_event, book):
    state = state_of(init_event)
    guidance = guidance_for(state, book, "unknown-guy", market_state(state, book))
    assert guidance.reference_value is None
    assert any("no reference value" in r for r in guidance.reasons)
    assert guidance.max_advisable_bid == guidance.max_legal_bid


def test_contested_ceiling_reports_the_top_live_rival(init_event, book):
    state = state_of(init_event)
    key = book.by_position(Position.RB)[0].key
    guidance = guidance_for(state, book, key, market_state(state, book))
    assert guidance.contested_ceiling == max(t.max_legal_bid for t in guidance.live_threats)


# --- engine -------------------------------------------------------------------


def test_advise_defaults_to_the_current_nomination(init_event, book):
    from ffa.domain.events import PlayerNominated
    from ffa.domain.ids import PlayerRef
    from tests.conftest import at

    target = book.by_position(Position.WR)[0]
    state = state_of(
        init_event, PlayerNominated(id=2, at=at(), player=PlayerRef.from_raw(target.name))
    )
    advisory = advise(state, book)
    assert advisory.guidance is not None
    assert advisory.guidance.key == normalize_player_key(target.name)


def test_advise_without_a_nomination_returns_market_and_scarcity_only(init_event, book):
    advisory = advise(state_of(init_event), book)
    assert advisory.guidance is None
    assert advisory.scarcity and advisory.market is not None
