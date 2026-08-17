"""The JSON view model is a published contract — the dashboard design is being
built against it — so these tests pin its shape, not just its values."""

from __future__ import annotations

import json

import pytest

from ffa.domain import reducers
from ffa.domain.enums import Position
from ffa.domain.events import PlayerNominated
from ffa.domain.ids import PlayerRef
from ffa.domain.models import DraftState
from ffa.view.model import VIEW_SCHEMA_VERSION, build_view
from tests.conftest import at, sold


def view_of(init_event, *events):
    return build_view(reducers.replay([init_event, *events]))


def test_uninitialized_state_is_reported_not_crashed():
    view = build_view(DraftState.empty("d1"))
    assert view["initialized"] is False
    assert view["schema_version"] == VIEW_SCHEMA_VERSION


def test_top_level_keys_are_stable(init_event):
    """The dashboard binds to these; adding is fine, removing is a break."""
    assert set(view_of(init_event)) >= {
        "schema_version", "draft_id", "generated_at", "initialized", "league",
        "me", "teams", "nomination", "market", "recent_sales", "scarcity", "warnings",
    }


def test_the_whole_view_is_json_serializable(init_event):
    """Enums and datetimes must not leak into the payload."""
    view = view_of(init_event, sold(2, "mahomes", 3, 45, pos=Position.QB))
    assert json.loads(json.dumps(view))["teams"][2]["roster"][0]["position"] == "QB"


def test_my_team_is_identified(init_event):
    view = view_of(init_event)
    assert view["me"]["team_id"] == 4 and view["me"]["is_me"] is True


def test_team_view_carries_the_precomputed_ceiling(init_event):
    view = view_of(init_event, sold(2, "mahomes", 3, 45))
    team = next(t for t in view["teams"] if t["team_id"] == 3)
    assert (team["spent"], team["remaining"], team["max_legal_bid"]) == (45, 155, 141)


def test_unknown_prices_mark_remaining_as_a_floor(init_event):
    """The dashboard must never render a floor as a plain number."""
    view = view_of(init_event, sold(2, "mahomes", 3, None))
    team = next(t for t in view["teams"] if t["team_id"] == 3)
    assert team["remaining_is_floor"] is True
    assert team["unknown_price_count"] == 1
    assert team["remaining"] == 199


def test_known_prices_are_not_marked_as_a_floor(init_event):
    view = view_of(init_event, sold(2, "mahomes", 3, 45))
    team = next(t for t in view["teams"] if t["team_id"] == 3)
    assert team["remaining_is_floor"] is False


def test_price_provenance_travels_with_the_value(init_event):
    """Ground-truth-vs-inference must survive the trip to the UI."""
    view = view_of(init_event, sold(2, "mahomes", 3, 45))
    player = next(t for t in view["teams"] if t["team_id"] == 3)["roster"][0]
    assert player["price_provenance"] == "MANUAL" and player["price_known"] is True


def test_starter_gaps_exclude_bench(init_event):
    """Panels key off this: an empty bench slot is space, not a need."""
    view = view_of(init_event)
    gaps = view["me"]["starter_gaps"]
    assert "BE" not in gaps and "IR" not in gaps
    assert gaps["RB"] == 2 and gaps["FLEX"] == 1


def test_filling_a_position_shrinks_only_that_gap(init_event):
    view = view_of(init_event, sold(2, "mahomes", 4, 45, pos=Position.QB))
    assert "QB" not in view["me"]["starter_gaps"]
    assert view["me"]["starter_gaps"]["RB"] == 2


def test_nomination_is_exposed_when_one_is_live(init_event):
    view = view_of(
        init_event, PlayerNominated(id=2, at=at(), player=PlayerRef.from_raw("CeeDee Lamb"))
    )
    assert view["nomination"]["name"] == "CeeDee Lamb"
    assert view["nomination"]["key"] == "ceedee-lamb"


def test_nomination_is_null_when_nothing_is_up(init_event):
    assert view_of(init_event)["nomination"] is None


def test_market_totals(init_event):
    view = view_of(init_event, sold(2, "a", 1, 45), sold(3, "b", 2, 30), sold(4, "c", 3, None))
    assert view["market"]["sales"] == 3
    assert view["market"]["dollars_spent"] == 75
    assert view["market"]["dollars_remaining"] == 2400 - 75


def test_reference_dependent_fields_stay_present_when_no_book_is_loaded(init_event):
    """A draft with no reference data still tracks everything else."""
    view = view_of(init_event)
    assert view["scarcity"] == {}
    assert view["market"]["inflation_ratio"] is None
    assert view["reference"] is None


# --- with reference data loaded -----------------------------------------------


@pytest.fixture
def book():
    from pathlib import Path

    from ffa.reference.playerbook import load_playerbook

    fixture = Path(__file__).resolve().parents[2] / "data" / "fixtures" / "sample_rankings.csv"
    return load_playerbook(fixture, teams=12, budget=200, is_sample=True)


def view_with(book, init_event, *events):
    return build_view(reducers.replay([init_event, *events]), book)


def test_scarcity_is_populated_once_a_book_is_loaded(book, init_event):
    scarcity = view_with(book, init_event)["scarcity"]
    assert set(scarcity) == {p.value for p in Position}
    rb = scarcity["RB"]
    assert rb["starting_demand"] == 28
    assert rb["elite"] + rb["startable"] + rb["bench"] == rb["total_remaining"]


def test_reference_meta_flags_sample_data(book, init_event):
    meta = view_with(book, init_event)["reference"]
    assert meta["is_sample"] is True and meta["players"] > 200


def test_sold_players_carry_reference_value_and_delta(book, init_event):
    row = sorted(book.rows, key=lambda r: r.overall_rank or 9999)[0]
    value = book.value(row.key)
    view = view_with(book, init_event, sold(2, row.name, 3, value + 10, pos=row.position))
    player = next(t for t in view["teams"] if t["team_id"] == 3)["roster"][0]
    assert player["reference_value"] == value
    assert player["delta"] == 10


def test_nomination_carries_guidance_for_the_dashboard(book, init_event):
    from ffa.domain.events import PlayerNominated
    from ffa.domain.ids import PlayerRef

    row = sorted(book.rows, key=lambda r: r.overall_rank or 9999)[0]
    view = view_with(
        book, init_event,
        PlayerNominated(id=2, at=at(), player=PlayerRef(key=row.key, raw=row.name)),
    )
    nomination = view["nomination"]
    assert nomination["reference_value"] == book.value(row.key)

    guidance = nomination["guidance"]
    assert guidance["max_advisable_bid"] <= guidance["max_legal_bid"]
    assert len(guidance["threats"]) == 11
    assert all("is_live" in t for t in guidance["threats"])


def test_the_view_stays_json_serializable_with_a_book(book, init_event):
    row = sorted(book.rows, key=lambda r: r.overall_rank or 9999)[0]
    view = view_with(book, init_event, sold(2, row.name, 3, 50, pos=row.position))
    assert json.loads(json.dumps(view))["scarcity"]["RB"]["elite"] >= 0


def test_recent_sales_are_capped(init_event):
    events = [sold(i + 2, f"p{i}", 1, 1) for i in range(15)]
    view = build_view(reducers.replay([init_event, *events]), recent=10)
    assert len(view["recent_sales"]) == 10
