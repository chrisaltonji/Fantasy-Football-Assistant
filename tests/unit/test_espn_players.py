"""Pulling auction values from ESPN.

The fixture is a real `kona_player_info` slice covering all six draftable
positions. Everything here is offline; only `fetch_players` touches the network
and it is a thin wrapper.

What matters most is what this module *refuses* to do — write a zero, invent a
position, or scale the values to fill the league's money.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from ffa.ingest.espn.players import (
    COLUMNS,
    POSITION_IDS,
    PRO_TEAM_IDS,
    rows_from_payload,
    write_rows,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures" / "espn" / "kona_player_info_slice.json"
)


@pytest.fixture(scope="module")
def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["players"]


@pytest.fixture(scope="module")
def rows(payload):
    return rows_from_payload(payload, year=2026)[0]


# --- mapping ----------------------------------------------------------------


def test_all_six_draftable_positions_are_mapped(rows):
    assert {r["position"] for r in rows} == {"QB", "RB", "WR", "TE", "K", "DST"}


def test_pro_teams_resolve_to_abbreviations(rows):
    """Display text only, but a wrong team costs a second of doubt mid-draft."""
    by_name = {r["player_name"]: r for r in rows}
    assert by_name["Jahmyr Gibbs"]["nfl_team"] == "DET"
    assert by_name["Josh Allen"]["nfl_team"] == "BUF"


def test_an_unknown_pro_team_id_is_blank_not_wrong():
    payload = [{"player": {"fullName": "X", "defaultPositionId": 2,
                           "proTeamId": 999, "ownership": {"auctionValueAverage": 5}}}]
    assert rows_from_payload(payload, year=2026)[0][0]["nfl_team"] == ""


def test_defensive_players_and_coaches_are_dropped_with_a_warning():
    """ESPN ships IDP and coach slots that this league cannot roster."""
    payload = [
        {"player": {"fullName": "A Linebacker", "defaultPositionId": 10,
                    "ownership": {"auctionValueAverage": 3}}},
        {"player": {"fullName": "A Back", "defaultPositionId": 2,
                    "ownership": {"auctionValueAverage": 3}}},
    ]
    rows, warnings = rows_from_payload(payload, year=2026)
    assert [r["player_name"] for r in rows] == ["A Back"]
    assert any("10" in w for w in warnings)


def test_every_mapped_position_is_a_string_the_loader_understands():
    from ffa.domain.enums import Position

    for value in POSITION_IDS.values():
        assert Position.parse(value) is not None


def test_the_pro_team_table_covers_all_32_plus_free_agency():
    assert len(PRO_TEAM_IDS) == 33
    assert PRO_TEAM_IDS[0] == "FA"


# --- what it refuses to do --------------------------------------------------


def test_nothing_in_the_real_slice_is_written_at_zero(rows):
    assert rows and all(r["auction_value"] > 0 for r in rows)


def test_an_unpriced_player_is_dropped_and_counted():
    """A zero is a *known* value.

    Writing one would tell the advisory layer ESPN priced this player at
    nothing, which is a different claim from "ESPN did not price him" — and it
    would drag the whole market baseline down. The live pull drops ~545 such
    players, so the count is worth reporting rather than swallowing.
    """
    payload = [
        {"player": {"fullName": "Priced", "defaultPositionId": 2,
                    "ownership": {"auctionValueAverage": 5}}},
        {"player": {"fullName": "Unpriced", "defaultPositionId": 2,
                    "ownership": {"auctionValueAverage": 0}}},
    ]
    rows, warnings = rows_from_payload(payload, year=2026)
    assert [r["player_name"] for r in rows] == ["Priced"]
    assert any("no ESPN auction value" in w for w in warnings)


@pytest.mark.parametrize("value", [0, 0.0, None, "", "abc", -3])
def test_every_shape_of_missing_value_is_dropped(value):
    payload = [{"player": {"fullName": "X", "defaultPositionId": 2,
                           "ownership": {"auctionValueAverage": value}}}]
    assert rows_from_payload(payload, year=2026)[0] == []


def test_a_player_with_no_name_is_skipped():
    payload = [{"player": {"fullName": "  ", "defaultPositionId": 2,
                           "ownership": {"auctionValueAverage": 5}}}]
    assert rows_from_payload(payload, year=2026)[0] == []


def test_values_keep_cents_so_ordering_survives():
    """Rounding 355 values to whole dollars collapses real ordering."""
    payload = [
        {"player": {"fullName": "A", "defaultPositionId": 2,
                    "ownership": {"auctionValueAverage": 4.4}}},
        {"player": {"fullName": "B", "defaultPositionId": 2,
                    "ownership": {"auctionValueAverage": 4.6}}},
    ]
    rows = rows_from_payload(payload, year=2026)[0]
    assert [r["player_name"] for r in rows] == ["B", "A"]
    assert rows[0]["auction_value"] != rows[1]["auction_value"]


def test_rows_come_back_sorted_by_value(rows):
    values = [r["auction_value"] for r in rows]
    assert values == sorted(values, reverse=True)


# --- projections ------------------------------------------------------------


def test_a_season_projection_is_carried_when_present(rows):
    assert any(r["projected_points"] for r in rows)


def test_actuals_are_not_mistaken_for_projections():
    """statSourceId 0 is what happened; 1 is what is forecast."""
    payload = [{"player": {
        "fullName": "X", "defaultPositionId": 2,
        "ownership": {"auctionValueAverage": 5},
        "stats": [{"statSourceId": 0, "statSplitTypeId": 0,
                   "seasonId": 2026, "appliedTotal": 999.0}],
    }}]
    assert rows_from_payload(payload, year=2026)[0][0]["projected_points"] == ""


def test_a_missing_projection_is_blank_not_zero():
    payload = [{"player": {"fullName": "X", "defaultPositionId": 2,
                           "ownership": {"auctionValueAverage": 5}}}]
    assert rows_from_payload(payload, year=2026)[0][0]["projected_points"] == ""


# --- the written file -------------------------------------------------------


def test_the_output_loads_through_the_normal_reference_loader(rows, tmp_path):
    """The whole point of writing CSV: nothing downstream learns the source.

    It goes through the same tolerant loader, PlayerBook and `data validate` as
    a hand-exported sheet.
    """
    from ffa.reference.loader import load_reference

    path = tmp_path / "values.csv"
    written = write_rows(rows, path)
    report = load_reference(path)

    assert written == len(rows)
    assert len(report.rows) == len(rows)
    assert report.dropped == ()


def test_the_header_is_the_loaders_canonical_columns(rows, tmp_path):
    path = tmp_path / "values.csv"
    write_rows(rows, path)
    header = next(csv.reader(path.open(encoding="utf-8")))
    assert header == list(COLUMNS)

    from ffa.reference.schema import canonical_column

    assert all(canonical_column(c) is not None for c in header)


def test_a_playerbook_built_from_it_is_not_flagged_as_sample(rows, tmp_path):
    from ffa.reference.playerbook import load_playerbook

    path = tmp_path / "values.csv"
    write_rows(rows, path)
    book = load_playerbook(path, teams=12, budget=200)

    assert book.is_sample is False
    assert len(book) == len(rows)
    assert book.value("jahmyr-gibbs") == 64
