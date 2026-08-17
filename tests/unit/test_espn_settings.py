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
    team_ids, owners, _ = teams_from_payload(teams_payload)
    assert 6 not in team_ids
    assert team_ids == tuple(sorted(team_ids)), "ids come back sorted"
    assert len(owners) == len(team_ids)


def test_managers_resolve_through_member_swids(teams_payload):
    """Nicknames come from `members[]`, joined on `primaryOwner`."""
    _, owners, managers = teams_from_payload(teams_payload)
    assert managers, "expected at least one resolved manager"
    for team_id in managers:
        assert team_id in owners


def test_a_team_with_no_owner_still_gets_an_id():
    payload = {"teams": [{"id": 4}], "members": []}
    team_ids, owners, managers = teams_from_payload(payload)
    assert team_ids == (4,)
    assert owners == {} and managers == {}


def test_unreadable_team_entries_are_skipped():
    payload = {"teams": [{"id": 1}, {"nope": True}, {"id": "x"}]}
    assert teams_from_payload(payload)[0] == (1,)


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
