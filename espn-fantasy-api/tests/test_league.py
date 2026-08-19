"""Settings and team parsing, against real captured payloads.

Several of these pin facts that are counterintuitive enough that a
well-meaning refactor would break them and every reasonable-looking test would
still pass.
"""

from __future__ import annotations

import pytest

from espn_fantasy.enums import RosterSlot
from espn_fantasy.errors import EspnApiError
from espn_fantasy.league import (
    my_team_id,
    roster_from_lineup_counts,
    settings_from_payloads,
    teams_from_payload,
)


# --- lineup slots -----------------------------------------------------------


def test_lineup_counts_map_to_slots():
    roster, warnings = roster_from_lineup_counts({"0": 1, "2": 2, "4": 2, "20": 7})
    assert roster == {
        RosterSlot.QB: 1, RosterSlot.RB: 2, RosterSlot.WR: 2, RosterSlot.BE: 7
    }
    assert warnings == []


def test_zero_count_slots_are_dropped():
    """ESPN sends every slot id it knows about, most of them zero.

    Keeping them puts empty QB/DT/LB entries into the roster, and anything
    summing the values then counts slots the league does not have.
    """
    roster, _ = roster_from_lineup_counts({"0": 1, "8": 0, "9": 0, "10": 0})
    assert roster == {RosterSlot.QB: 1}


def test_both_restricted_flex_ids_fold_into_one_flex_count():
    """3 (RB/WR) and 5 (WR/TE) are separate slots that both mean "flex"."""
    roster, _ = roster_from_lineup_counts({"3": 1, "5": 1, "23": 1})
    assert roster == {RosterSlot.FLEX: 3}


def test_an_unmapped_slot_is_reported_rather_than_dropped_silently():
    """A slot nobody mapped is still a roster spot somebody can fill."""
    roster, warnings = roster_from_lineup_counts({"0": 1, "11": 2})
    assert roster == {RosterSlot.QB: 1}
    assert any("11" in w for w in warnings)


def test_an_unreadable_slot_entry_is_reported_not_crashed_on():
    roster, warnings = roster_from_lineup_counts({"nonsense": "also nonsense"})
    assert roster == {}
    assert any("unreadable" in w for w in warnings)


# --- teams ------------------------------------------------------------------


def test_team_ids_are_read_and_are_not_contiguous(teams_payload):
    """The load-bearing fact about this league, and about many others.

    A team was removed and re-added at some point, leaving a hole. Generating
    ids with `range(1, count + 1)` would invent a team 6 that does not exist
    while never mentioning team 13, and nothing about that failure is visible
    until somebody's picks go missing.
    """
    directory = teams_from_payload(teams_payload)
    assert directory.team_ids == (1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13)
    assert 6 not in directory.team_ids
    assert directory.team_ids != tuple(range(1, len(directory.team_ids) + 1))


def test_every_team_has_an_owner_and_they_are_distinct(teams_payload):
    directory = teams_from_payload(teams_payload)
    assert set(directory.owners) == set(directory.team_ids)
    assert len(set(directory.owners.values())) == len(directory.team_ids)


def test_both_names_are_kept_because_choosing_one_gets_it_wrong():
    """`displayName` is an account handle; the real name sits in the same payload.

    Collapsing them at parse time forces the choice on the caller, which is
    how `macurl1392` ends up on screen next to a member record that says
    "Michael Curley".
    """
    payload = {
        "teams": [{"id": 1, "name": "Team A", "primaryOwner": "{X}"}],
        "members": [
            {"id": "{X}", "displayName": "macurl1392",
             "firstName": "Michael", "lastName": "Curley"}
        ],
    }
    directory = teams_from_payload(payload)
    assert directory.real_names[1] == "Michael Curley"
    assert directory.display_names[1] == "macurl1392"
    assert directory.name_for(1) == "Michael Curley"


def test_the_display_name_is_the_fallback_when_espn_has_no_real_name():
    payload = {
        "teams": [{"id": 1, "primaryOwner": "{X}"}],
        "members": [{"id": "{X}", "displayName": "espn78078705"}],
    }
    assert teams_from_payload(payload).name_for(1) == "espn78078705"


def test_a_team_with_an_unreadable_id_is_skipped_not_fatal():
    payload = {"teams": [{"id": "bogus"}, {"id": 4}], "members": []}
    assert teams_from_payload(payload).team_ids == (4,)


# --- which team is mine -----------------------------------------------------


def test_my_team_matches_a_swid_in_any_spelling(teams_payload):
    directory = teams_from_payload(teams_payload)
    real = directory.owners[7]

    assert my_team_id(directory.owners, real) == 7
    assert my_team_id(directory.owners, real.strip("{}")) == 7
    assert my_team_id(directory.owners, real.lower()) == 7


def test_an_unmatched_swid_returns_zero_rather_than_guessing(teams_payload):
    """Zero is the honest answer. A guess here describes someone else's team."""
    directory = teams_from_payload(teams_payload)
    assert my_team_id(directory.owners, "{NOBODY}") == 0
    assert my_team_id(directory.owners, None) == 0


# --- the whole thing --------------------------------------------------------


def test_settings_carry_the_two_fields_the_espn_api_library_drops(settings_payload,
                                                                  teams_payload):
    """`auctionBudget` and draft `type` are why this reads the raw view at all."""
    settings, _ = settings_from_payloads(
        settings_payload, teams_payload, league_id=123456, year=2026
    )
    assert settings.draft_type == "AUCTION"
    assert settings.is_auction is True
    assert settings.auction_budget == 200


def test_settings_describe_the_roster(settings_payload, teams_payload):
    settings, _ = settings_from_payloads(
        settings_payload, teams_payload, league_id=123456, year=2026
    )
    # 16 spots, of which 10 start — bench and IR cost roster space but score
    # nothing, which is why the two counts are kept apart.
    assert settings.roster_size == 16
    assert settings.starter_count == 10
    assert settings.roster[RosterSlot.BE] == 5
    assert settings.roster[RosterSlot.IR] == 1


def test_settings_report_the_team_count_and_ids_together(settings_payload,
                                                         teams_payload):
    settings, _ = settings_from_payloads(
        settings_payload, teams_payload, league_id=123456, year=2026
    )
    assert settings.team_count == 12
    assert len(settings.teams.team_ids) == 12


def test_a_disagreement_between_size_and_the_team_list_is_surfaced(settings_payload):
    """ESPN's `size` and the teams it actually returns can disagree.

    The returned ids win, because they are what picks are credited to — but
    silently preferring them hides a league that is mid-change.
    """
    teams = {"teams": [{"id": 1}, {"id": 2}], "members": []}
    settings, warnings = settings_from_payloads(
        settings_payload, teams, league_id=123456, year=2026
    )
    assert settings.team_count == 2
    assert any("size=12" in w for w in warnings)


def test_a_payload_with_no_settings_block_says_which_view_to_ask_for():
    with pytest.raises(EspnApiError, match="mSettings"):
        settings_from_payloads({}, {}, league_id=1, year=2026)


def test_nothing_is_invented_for_fields_espn_did_not_send():
    """A plausible default is indistinguishable from a real value downstream."""
    settings, _ = settings_from_payloads(
        {"settings": {"draftSettings": {}, "rosterSettings": {}}},
        {},
        league_id=1,
        year=2026,
    )
    assert settings.auction_budget is None
    assert settings.draft_type == ""
    assert settings.scoring_type == ""
    assert settings.is_auction is False
