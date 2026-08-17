"""Assertions about the shape of real ESPN payloads.

The sandbox can't reach ESPN, so these fixtures are the only contract CP5's
adapter can be built against. Each test here pins something that was *learned*
from a real capture and would otherwise be a plausible-sounding wrong guess.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "espn"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def settings():
    return load("mSettings_auction.json")


@pytest.fixture
def predraft():
    return load("mDraftDetail_predraft.json")


@pytest.fixture
def inprogress():
    return load("mDraftDetail_inprogress.json")


@pytest.fixture
def teams():
    return load("mTeam_predraft.json")


# --- settings -----------------------------------------------------------------


def test_auction_budget_and_type_live_in_raw_settings(settings):
    """Both are dropped by espn-api's BaseSettings, so config bootstrap must
    read the raw view. This test is why CP5 can't just use the library."""
    draft = settings["settings"]["draftSettings"]
    assert draft["auctionBudget"] == 200
    assert draft["type"] == "AUCTION"


def test_roster_comes_from_numeric_lineup_slot_ids(settings):
    counts = settings["settings"]["rosterSettings"]["lineupSlotCounts"]
    # 0=QB 2=RB 4=WR 6=TE 16=D/ST 17=K 20=BE 21=IR 23=FLEX
    assert counts["0"] == 1 and counts["2"] == 2 and counts["4"] == 3
    assert counts["23"] == 1 and counts["20"] == 5 and counts["21"] == 1
    draftable = sum(int(v) for k, v in counts.items() if k != "21")
    assert draftable == 15


def test_private_leagues_are_marked_public_false(settings):
    assert settings["settings"]["isPublic"] is False


# --- the pick skeleton --------------------------------------------------------


def test_all_picks_exist_before_anyone_drafts(predraft):
    """The skeleton is pre-created, so polling can't diff on len(picks)."""
    picks = predraft["draftDetail"]["picks"]
    assert len(picks) == 180 == 15 * 12
    assert all(p["playerId"] == -1 for p in picks)
    assert all(p["teamId"] == -1 for p in picks)
    assert all(p["bidAmount"] == 0 for p in picks)


def test_a_pick_is_filled_when_player_id_stops_being_minus_one(inprogress, predraft):
    before = predraft["draftDetail"]["picks"]
    after = inprogress["draftDetail"]["picks"]
    assert len(before) == len(after)  # length never changes

    filled = [p for p in after if p["playerId"] != -1]
    assert len(filled) == 3
    for pick in filled:
        assert pick["teamId"] != -1
        assert pick["bidAmount"] > 0


def test_nomination_order_is_known_before_the_draft(settings, predraft):
    """Answers the capability spec's open question for capability 4."""
    order = settings["settings"]["draftSettings"]["pickOrder"]
    assert len(order) == 12
    first_round = [
        p["nominatingTeamId"]
        for p in predraft["draftDetail"]["picks"]
        if p["roundId"] == 1
    ]
    assert first_round == order


def test_the_draft_detail_view_is_what_carries_picks(settings, predraft):
    """mSettings returns draftDetail *without* picks; only mDraftDetail has them."""
    assert "picks" not in settings["draftDetail"]
    assert "picks" in predraft["draftDetail"]


# --- teams --------------------------------------------------------------------


def test_team_ids_are_not_contiguous(teams):
    """No team 6. range(1, count+1) would invent one and drop team 13."""
    ids = [t["id"] for t in teams["teams"]]
    assert ids == [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13]
    assert len(ids) == 12
    assert 6 not in ids
    assert ids != list(range(1, 13))


def test_identity_is_the_owner_swid_not_the_name(teams):
    for team in teams["teams"]:
        assert team["primaryOwner"].startswith("{")
        assert team["primaryOwner"] in team["owners"]
    swids = [t["primaryOwner"] for t in teams["teams"]]
    assert len(set(swids)) == len(swids)


def test_no_per_team_auction_budget_field_exists(teams):
    """acquisitionBudgetSpent is FAAB waivers, a different pot. Remaining
    auction budget has to be derived from summed bidAmounts."""
    for team in teams["teams"]:
        keys = set(team) | set(team.get("transactionCounter", {}))
        auction_budget_keys = {
            k for k in keys
            if "budget" in k.lower() and "acquisition" not in k.lower()
        }
        assert auction_budget_keys == set()
