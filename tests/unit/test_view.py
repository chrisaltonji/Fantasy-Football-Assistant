"""The JSON view model is a published contract — the dashboard design is being
built against it — so these tests pin its shape, not just its values."""

from __future__ import annotations

import json

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


def test_cp3_fields_are_reserved_and_empty_not_absent(init_event):
    """So the dashboard can bind to them now and they light up in CP3."""
    view = view_of(init_event)
    assert view["scarcity"] == {}
    assert view["market"]["inflation_ratio"] is None


def test_recent_sales_are_capped(init_event):
    events = [sold(i + 2, f"p{i}", 1, 1) for i in range(15)]
    view = build_view(reducers.replay([init_event, *events]), recent=10)
    assert len(view["recent_sales"]) == 10
