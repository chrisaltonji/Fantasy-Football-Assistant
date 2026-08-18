"""Rebuilding league config from ESPN's raw views.

`config/league.toml` is gitignored, so it *will* go missing again — on a new
machine, a fresh clone, or when a container evaporates (which is exactly how it
was lost the first time). These tests pin the parts that would silently produce
a wrong-but-plausible config.

All offline, against the committed fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ffa.config.schema import ConfigError
from ffa.domain.enums import RosterSlot
from ffa.ingest.espn.settings import (
    LINEUP_SLOT_IDS,
    config_from_payloads,
    my_team_id,
    roster_from_lineup_counts,
    teams_from_payload,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "espn"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def settings_payload():
    return load("mSettings_auction.json")


@pytest.fixture(scope="module")
def teams_payload():
    return load("mTeam_predraft.json")


# --- lineup slots -----------------------------------------------------------


def test_the_real_league_roster_is_reconstructed_exactly(settings_payload):
    """QB1 RB2 WR3 TE1 FLEX1 K1 DST1 BE5 IR1 — 15 draftable slots.

    This is the arithmetic every max-bid calculation rests on. One slot wrong
    and every ceiling is wrong by a dollar per slot, all draft long.
    """
    counts = settings_payload["settings"]["rosterSettings"]["lineupSlotCounts"]
    roster, warnings = roster_from_lineup_counts(counts)

    assert roster == {
        RosterSlot.QB: 1, RosterSlot.RB: 2, RosterSlot.WR: 3, RosterSlot.TE: 1,
        RosterSlot.FLEX: 1, RosterSlot.K: 1, RosterSlot.DST: 1,
        RosterSlot.BE: 5, RosterSlot.IR: 1,
    }
    assert warnings == []
    assert sum(n for s, n in roster.items() if s is not RosterSlot.IR) == 15


def test_zero_count_slots_are_dropped():
    """ESPN sends every slot id it knows, most of them zero."""
    roster, _ = roster_from_lineup_counts({"0": 1, "8": 0, "10": 0, "20": 5})
    assert roster == {RosterSlot.QB: 1, RosterSlot.BE: 5}


def test_an_unmapped_slot_warns_rather_than_vanishing():
    """A silently dropped slot is a roster spot the budget never knows about."""
    roster, warnings = roster_from_lineup_counts({"0": 1, "11": 2})
    assert roster == {RosterSlot.QB: 1}
    assert any("11" in w for w in warnings)


def test_restricted_flex_slots_fold_into_flex():
    """ESPN's RB/WR (3) and WR/TE (5) both land on FLEX, and they add up."""
    roster, _ = roster_from_lineup_counts({"3": 1, "5": 1, "23": 1})
    assert roster == {RosterSlot.FLEX: 3}


def test_unreadable_counts_warn_instead_of_raising():
    roster, warnings = roster_from_lineup_counts({"0": 1, "bogus": "x"})
    assert roster == {RosterSlot.QB: 1}
    assert any("bogus" in w for w in warnings)


def test_every_mapped_slot_id_is_a_real_roster_slot():
    assert all(isinstance(s, RosterSlot) for s in LINEUP_SLOT_IDS.values())


# --- teams ------------------------------------------------------------------


def test_team_ids_are_read_never_generated(teams_payload):
    """The real league has no team 6.

    `range(1, count + 1)` would invent one and drop team 13 — silently
    attributing picks to a team that does not exist.
    """
    directory = teams_from_payload(teams_payload)
    assert 6 not in directory.team_ids
    assert directory.team_ids == tuple(sorted(directory.team_ids)), "ids come back sorted"
    assert len(directory.owners) == len(directory.team_ids)


def test_members_resolve_through_the_owner_swid(teams_payload):
    """Names come from `members[]`, joined on `primaryOwner`."""
    directory = teams_from_payload(teams_payload)
    assert directory.display_names, "expected at least one resolved member"
    for team_id in directory.display_names:
        assert team_id in directory.owners


def test_both_names_are_kept_because_they_are_not_interchangeable(teams_payload):
    """The bug this replaced: `displayName` was preferred and the real name
    thrown away, so every surface showed `macurl1392` while "Michael Curley"
    sat in the same payload."""
    directory = teams_from_payload(teams_payload)

    assert directory.real_names, "expected firstName/lastName to survive"
    assert directory.display_names
    for team_id, real in directory.real_names.items():
        assert real != directory.display_names.get(team_id)


def test_a_member_with_no_real_name_still_yields_a_handle():
    """ESPN leaves firstName/lastName off some accounts. That is not an error."""
    payload = {
        "teams": [{"id": 1, "primaryOwner": "{A}"}],
        "members": [{"id": "{A}", "displayName": "espn06814226"}],
    }
    directory = teams_from_payload(payload)

    assert directory.real_names == {}
    assert directory.display_names == {1: "espn06814226"}


def test_a_team_with_no_owner_still_gets_an_id():
    payload = {"teams": [{"id": 4}], "members": []}
    directory = teams_from_payload(payload)
    assert directory.team_ids == (4,)
    assert directory.owners == {} and directory.display_names == {}


def test_unreadable_team_entries_are_skipped():
    payload = {"teams": [{"id": 1}, {"nope": True}, {"id": "x"}]}
    assert teams_from_payload(payload).team_ids == (1,)


# --- identifying ourselves --------------------------------------------------


@pytest.mark.parametrize("swid", [
    "{AAAA-BBBB}", "AAAA-BBBB",
])
def test_our_team_is_found_with_or_without_braces(swid):
    assert my_team_id({7: "{AAAA-BBBB}"}, swid) == 7


def test_an_unmatched_swid_returns_zero_rather_than_guessing():
    """Every projection keys off my_team_id.

    Guessing would produce confident advice about somebody else's budget, which
    is strictly worse than admitting we do not know.
    """
    assert my_team_id({1: "{X}"}, "{Y}") == 0
    assert my_team_id({1: "{X}"}, None) == 0
    assert my_team_id({}, "{X}") == 0


# --- the whole config -------------------------------------------------------


def test_a_full_config_is_rebuilt_from_the_two_views(settings_payload, teams_payload):
    config, _ = config_from_payloads(
        settings_payload, teams_payload, league_id=1167816302, year=2026,
    )
    assert config.league_id == 1167816302
    assert config.draft_type == "AUCTION"
    assert config.budget == 200
    assert config.team_count == 12
    assert config.draftable_slots == 15
    assert 6 not in config.effective_team_ids


def test_a_missing_settings_block_is_a_clear_error(teams_payload):
    with pytest.raises(ConfigError, match="mSettings"):
        config_from_payloads({}, teams_payload, league_id=1, year=2026)


def test_a_roster_with_no_usable_slots_is_refused(teams_payload):
    payload = {"settings": {"rosterSettings": {"lineupSlotCounts": {}}}}
    with pytest.raises(ConfigError, match="lineup slots"):
        config_from_payloads(payload, teams_payload, league_id=1, year=2026)


def test_an_unmatched_swid_warns_loudly(settings_payload, teams_payload):
    _, warnings = config_from_payloads(
        settings_payload, teams_payload, league_id=1, year=2026, swid="{NOBODY}",
    )
    assert any("my_team_id" in w for w in warnings)


def test_a_snake_draft_warns_that_the_advice_assumes_an_auction(teams_payload):
    payload = {
        "settings": {
            "draftSettings": {"type": "SNAKE"},
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "20": 5}},
        }
    }
    _, warnings = config_from_payloads(payload, teams_payload, league_id=1, year=2026)
    assert any("SNAKE" in w for w in warnings)


def test_a_size_that_disagrees_with_the_team_list_trusts_the_list(teams_payload):
    """ESPN's `size` is what the league is set to; the ids are what exist."""
    payload = {
        "settings": {
            "size": 14,
            "draftSettings": {"type": "AUCTION", "auctionBudget": 200},
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "20": 5}},
        }
    }
    config, warnings = config_from_payloads(payload, teams_payload, league_id=1, year=2026)
    assert config.team_count == len(config.team_ids)
    assert any("size=14" in w for w in warnings)


def test_the_rebuilt_config_round_trips_through_the_writer(
    settings_payload, teams_payload, tmp_path
):
    """What we write must be what `load_config` reads back."""
    from ffa.config.loader import load_config, write_config

    config, _ = config_from_payloads(
        settings_payload, teams_payload, league_id=1167816302, year=2026,
    )
    path = tmp_path / "league.toml"
    write_config(config, path)
    reloaded = load_config(path)

    assert reloaded.league_id == config.league_id
    assert reloaded.budget == config.budget
    assert reloaded.roster == config.roster
    assert tuple(reloaded.effective_team_ids) == tuple(config.effective_team_ids)
    assert reloaded.owners == config.owners


def test_team_names_are_captured_for_draft_room_matching(teams_payload):
    """The draft room board carries no team id, so the name is the only join.

    Stored as a hint rather than identity — see `TeamResolver`.
    """
    directory = teams_from_payload(teams_payload)
    assert directory.team_names, "expected ESPN to report team names"
    assert set(directory.team_names) <= set(directory.team_ids)


def test_the_rebuilt_config_carries_team_names(settings_payload, teams_payload):
    config, _ = config_from_payloads(
        settings_payload, teams_payload, league_id=1, year=2026,
    )
    assert config.team_names
    assert set(config.team_names) <= set(config.effective_team_ids)


def test_the_rebuilt_config_carries_the_managers_real_names(
    settings_payload, teams_payload
):
    """The whole point of the fix: a person's name reaches the config."""
    config, _ = config_from_payloads(
        settings_payload, teams_payload, league_id=1, year=2026,
    )

    assert config.real_names
    assert set(config.real_names) <= set(config.effective_team_ids)


def test_nicknames_are_derived_from_first_names_not_account_handles(
    settings_payload, teams_payload
):
    """`[managers]` is what you type under a clock, so it comes from the name.

    The handle stays available as a fallback for anyone ESPN has no real name
    for, but it is no longer the default.
    """
    config, _ = config_from_payloads(
        settings_payload, teams_payload, league_id=1, year=2026,
    )
    directory = teams_from_payload(teams_payload)

    for team_id, nickname in config.managers.items():
        real = directory.real_names.get(team_id)
        if real:
            assert nickname.startswith(real.split()[0].lower())
        # And it is always typeable, whichever source it came from.
        assert " " not in nickname
