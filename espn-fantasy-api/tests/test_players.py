"""ESPN's player universe — auction values, ADP, projections."""

from __future__ import annotations

import csv

from espn_fantasy.players import players_from_payload, write_csv


def parse(payload, **kwargs):
    return players_from_payload(payload["players"], year=2026, **kwargs)


def test_players_come_back_priced_and_positioned(player_slice):
    players, _ = parse(player_slice)
    assert players
    gibbs = next(p for p in players if p.name == "Jahmyr Gibbs")
    assert gibbs.position == "RB"
    assert gibbs.nfl_team == "DET"
    assert gibbs.auction_value and gibbs.auction_value > 0


def test_the_most_expensive_player_is_first(player_slice):
    players, _ = parse(player_slice)
    values = [p.auction_value for p in players if p.auction_value is not None]
    assert values == sorted(values, reverse=True)


def test_an_unpriced_player_is_dropped_rather_than_written_as_zero():
    """Zero is a *known* value and says the player is worthless.

    "ESPN did not price him" is a different claim, and the two must not
    collapse into each other — anything ranking on value would sort a whole
    unpriced tail into last place as though that were a judgement.
    """
    payload = {
        "players": [
            {"player": {"fullName": "Priced", "defaultPositionId": 2,
                        "ownership": {"auctionValueAverage": 12}}},
            {"player": {"fullName": "Unpriced", "defaultPositionId": 2,
                        "ownership": {"auctionValueAverage": 0}}},
        ]
    }
    players, warnings = parse(payload)
    assert [p.name for p in players] == ["Priced"]
    assert any("rather than recorded as $0" in w for w in warnings)


def test_unpriced_players_can_be_kept_with_the_value_left_unset():
    payload = {
        "players": [
            {"player": {"fullName": "Priced", "defaultPositionId": 2,
                        "ownership": {"auctionValueAverage": 12}}},
            {"player": {"fullName": "Unpriced", "defaultPositionId": 2,
                        "ownership": {}}},
        ]
    }
    players, _ = parse(payload, priced_only=False)
    assert [(p.name, p.auction_value) for p in players] == [
        ("Priced", 12.0), ("Unpriced", None)
    ]


def test_defensive_players_are_skipped_and_counted():
    """Position id 8 is a linebacker. Not draftable in a standard league."""
    payload = {
        "players": [
            {"player": {"fullName": "A Linebacker", "defaultPositionId": 8,
                        "ownership": {"auctionValueAverage": 3}}},
        ]
    }
    players, warnings = parse(payload)
    assert players == []
    assert any("[8]" in w for w in warnings)


def test_values_keep_two_decimal_places():
    """Rounding 355 values to whole dollars loses the order within a tier."""
    payload = {
        "players": [
            {"player": {"fullName": "A", "defaultPositionId": 2,
                        "ownership": {"auctionValueAverage": 4.6}}},
            {"player": {"fullName": "B", "defaultPositionId": 2,
                        "ownership": {"auctionValueAverage": 4.4}}},
        ]
    }
    players, _ = parse(payload)
    assert [p.name for p in players] == ["A", "B"]
    assert players[0].auction_value == 4.6


def test_the_season_projection_is_read_not_a_weekly_split(player_slice):
    """`statSourceId 1` is a projection, `statSplitTypeId 0` is the full season.

    Both matter: source 0 is actuals, and a non-zero split is a single week.
    """
    players, _ = parse(player_slice)
    projected = [p for p in players if p.projected_points is not None]
    assert projected, "the fixture carries at least one season projection"
    assert all(p.projected_points > 0 for p in projected)


def test_a_player_with_no_name_is_skipped():
    payload = {"players": [{"player": {"fullName": "  ", "defaultPositionId": 2,
                                       "ownership": {"auctionValueAverage": 9}}}]}
    assert parse(payload)[0] == []


def test_csv_round_trips_with_blanks_for_unknowns(tmp_path, player_slice):
    players, _ = parse(player_slice)
    path = tmp_path / "out" / "values.csv"

    assert write_csv(players, path) == len(players)

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == len(players)
    assert rows[0]["player_name"] == players[0].name
    # Unknowns are empty cells, never "None" and never 0.
    assert all(v != "None" for row in rows for v in row.values())
