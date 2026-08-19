"""Parsing the draft room's DOM, against a snapshot taken mid-auction.

No browser is involved: `SNAPSHOT_JS` returns raw strings, and every piece of
coercion happens in Python precisely so it can be tested like this.
"""

from __future__ import annotations

from espn_fantasy.draftroom import (
    RoomNomination,
    RoomPick,
    RoomSnapshot,
    RoomTeam,
    _money,
    _team_label,
    parse_snapshot,
)


# --- the snapshot as a whole ------------------------------------------------


def test_a_live_board_parses_into_teams_and_picks(room_snapshot):
    snapshot = parse_snapshot(room_snapshot)
    assert len(snapshot.teams) == 12
    assert snapshot.filled == 80
    assert snapshot.pick_header == "PK 81 OF 180"
    assert snapshot.clock == "00:06"


def test_slots_per_team_is_derived_not_hardcoded(room_snapshot):
    """180 cells across 12 teams. Hardcoding 15 breaks any other roster size."""
    assert parse_snapshot(room_snapshot).slots_per_team == 15


def test_a_pick_carries_everything_the_cell_shows(room_snapshot):
    snapshot = parse_snapshot(room_snapshot)
    gibbs = next(p for p in snapshot.picks if p.player == "Jahmyr Gibbs")
    assert gibbs.price == 76
    assert gibbs.position == "RB"
    assert gibbs.roster_slot == "RB"
    assert gibbs.pro_team == "DET"
    assert gibbs.is_mine is True


def test_a_picks_team_comes_from_its_position_in_the_flat_grid(room_snapshot):
    """The board is one grid laid out a whole team at a time."""
    snapshot = parse_snapshot(room_snapshot)
    for pick in snapshot.picks:
        assert pick.team_index == pick.grid_index // snapshot.slots_per_team
        assert 0 <= pick.team_index < len(snapshot.teams)


# --- the $null trap ---------------------------------------------------------


def test_no_bid_parses_to_none_and_never_to_zero(room_snapshot):
    """ESPN renders "no bid" as the literal string `$null`.

    Coercing it to 0 would be serious: zero is a real price, so a consumer
    merging observations would take "nobody has bid" as a settled price of
    nothing and let it beat the real number when it lands.
    """
    snapshot = parse_snapshot(room_snapshot)
    no_bid = [t for t in snapshot.teams if t.bid is None]
    assert len(no_bid) == 11
    assert all(t.bid != 0 for t in snapshot.teams)


def test_money_parsing_handles_every_shape_the_dom_produces():
    assert _money("$76") == 76
    assert _money("$1,200") == 1200
    assert _money("$null") is None
    assert _money("") is None
    assert _money(None) is None
    assert _money("garbage") is None
    assert _money("$0") == 0, "a genuine zero is still a zero"


# --- team labels ------------------------------------------------------------


def test_the_leading_ordinal_is_stripped_from_panel_names():
    assert _team_label("3. Fighting Finkelsteins", 2) == "Fighting Finkelsteins"


def test_a_name_that_merely_contains_a_dot_is_left_intact():
    assert _team_label("St. Louis Ramrods", 0) == "St. Louis Ramrods"


def test_a_missing_name_falls_back_to_a_positional_label():
    assert _team_label(None, 4) == "team5"
    assert _team_label("  ", 4) == "team5"


def test_a_team_name_falls_back_to_the_header_cell():
    raw = {
        "teams": [{"index": 0, "name": None, "cash": "$5", "bid": "$null"}],
        "headers": ["Column Header Name"],
        "picks": [],
        "cellCount": 0,
    }
    assert parse_snapshot(raw).teams[0].name == "Column Header Name"


# --- flags ------------------------------------------------------------------


def test_team_flags_are_read_from_class_modifiers():
    raw = {
        "teams": [{"index": 0, "name": "A", "cash": "$5", "bid": "$2",
                   "auto": True, "own": True, "selecting": True}],
        "picks": [],
        "cellCount": 0,
    }
    team = parse_snapshot(raw).teams[0]
    assert (team.is_auto, team.is_me, team.is_nominating) == (True, True, True)


# --- nomination -------------------------------------------------------------


def test_the_player_on_the_block_is_read_every_poll():
    raw = {
        "teams": [], "picks": [], "cellCount": 0,
        "nominated": {"player": "Bijan Robinson", "position": "RB",
                      "proTeam": "ATL", "currentBid": "Current offer: $36"},
    }
    nomination = parse_snapshot(raw).nominated
    assert nomination is not None
    assert nomination.player == "Bijan Robinson"
    assert nomination.current_bid == 36


def test_a_nomination_before_the_first_bid_has_no_amount():
    raw = {"teams": [], "picks": [], "cellCount": 0,
           "nominated": {"player": "Bijan Robinson", "currentBid": None}}
    assert parse_snapshot(raw).nominated.current_bid is None


def test_an_empty_nomination_is_no_nomination():
    raw = {"teams": [], "picks": [], "cellCount": 0, "nominated": {"player": "  "}}
    assert parse_snapshot(raw).nominated is None


# --- keys -------------------------------------------------------------------


def test_players_fold_to_a_stable_key():
    assert RoomPick(0, 0, "Ja'Marr Chase", None, None, None, None).key == "jamarr-chase"
    assert RoomNomination("Ja'Marr  Chase").key == "jamarr-chase"


def test_generational_suffixes_are_kept_because_they_distinguish_people():
    assert RoomPick(0, 0, "Odell Beckham Jr.", None, None, None, None).key \
        == "odell-beckham-jr"


# --- degenerate input -------------------------------------------------------


def test_an_empty_page_parses_to_an_empty_snapshot():
    snapshot = parse_snapshot({})
    assert snapshot == RoomSnapshot()
    assert snapshot.filled == 0


def test_a_board_with_no_teams_does_not_divide_by_zero():
    assert parse_snapshot({"teams": [], "picks": [], "cellCount": 180}).slots_per_team == 0


def test_an_unknown_team_index_still_gets_a_label():
    snapshot = RoomSnapshot(teams=(RoomTeam(0, "A", None, None),))
    assert snapshot.team_name(9) == "team10"
