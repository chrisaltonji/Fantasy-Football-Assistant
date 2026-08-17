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


@pytest.fixture
def sandboxed():
    return load("mDraftDetail_practice_sandboxed.json")


@pytest.fixture
def completed():
    return load("mDraftDetail_completed_auction.json")


@pytest.fixture
def practice_live():
    return load("mDraftDetail_practice_live.json")


# --- what a live draft actually looks like over REST -------------------------


def test_a_live_draft_reports_in_progress_but_writes_no_picks(practice_live):
    """Observed 2026-08-17, during an actively running practice draft.

    Captured from the **shadow league id** the practice draft runs under,
    which is visible in the draft-room URL and is not the real league id. Two
    players had been bought when this was taken, and 60 polls over 15 minutes
    never showed a single one.

    This is the answer to B1's second half: ESPN's draft room is driven by a
    Server-Sent Events stream at `fantasydraft.espn.com`, and the REST view is
    not written while the draft runs. Polling `mDraftDetail` on draft day
    returns this exact payload for three hours.
    """
    detail = practice_live["draftDetail"]
    assert detail["inProgress"] is True, "the draft was genuinely running"
    assert detail["drafted"] is False
    assert all(p["playerId"] == -1 for p in detail["picks"])
    assert all(p["bidAmount"] == 0 for p in detail["picks"])


def test_a_live_draft_still_publishes_the_nomination_order(practice_live):
    """The one thing REST does give us mid-draft, and it is worth having.

    `nominatingTeamId` is populated on all 180 picks before anything sells, so
    the whole nomination sequence is readable in advance even though no result
    is.
    """
    picks = practice_live["draftDetail"]["picks"]
    assert all(p["nominatingTeamId"] > 0 for p in picks)
    assert len({p["nominatingTeamId"] for p in picks}) == 12


def test_the_practice_draft_runs_under_a_different_league_id(practice_live):
    """Not the real league. This is why every earlier capture looked empty.

    The real league (1167816302) sat at `inProgress: False` with an untouched
    skeleton through the entire practice draft — 30 polls, no change. The two
    are completely separate leagues, so practice picks were never "sandboxed",
    just somewhere nobody had looked.
    """
    assert practice_live["id"] != 1167816302


# --- the 2025 auction: the first real filled picks this build has ever seen ---


def test_bid_amount_is_populated_on_a_real_completed_auction(completed):
    """B1, answered. Captured from the same league's 2025 season on 2026-08-17.

    Everything before this was a skeleton of `playerId: -1` — which is
    indistinguishable from "ESPN never populates prices" and would have
    stranded the build on manual entry. It populates. All 180 of them.
    """
    detail = completed["draftDetail"]
    assert detail["drafted"] is True
    assert detail["inProgress"] is False

    picks = detail["picks"]
    assert len(picks) == 180
    assert all(p["playerId"] != -1 for p in picks), "every pick sold"

    bids = [p["bidAmount"] for p in picks]
    assert all(b > 0 for b in bids)
    assert (min(bids), max(bids)) == (1, 75)
    assert sum(bids) == 2389


def test_human_picks_carry_member_id_but_autodrafted_ones_do_not(completed):
    """`memberId` is the owner SWID on the pick — and it is NOT always there.

    Present on all 173 human picks, absent on all 7 ESPN autodrafted ones
    (`autoDraftTypeId: 2`). Nothing may key on `memberId` unconditionally; the
    fallback is `teamId`, which is always present.

    This matters on draft day specifically: if a nomination clock expires,
    ESPN autodrafts, and that pick arrives with no member on it.
    """
    picks = completed["draftDetail"]["picks"]
    human = [p for p in picks if p["autoDraftTypeId"] == 0]
    auto = [p for p in picks if p["autoDraftTypeId"] != 0]

    assert len(human) == 173 and len(auto) == 7
    assert all("memberId" in p and p["memberId"].startswith("{") for p in human)
    assert not any("memberId" in p for p in auto)
    assert all("teamId" in p for p in picks), "teamId is the reliable anchor"


def test_autodrafted_picks_are_all_dollar_scraps(completed):
    """Every autodraft in 2025 went for exactly $1 — end-of-draft cleanup.

    Not all bench, though: 4 BE, plus a WR, a K and a DST. So autodraft is not
    a proxy for "filler slot", only for "nobody bid".
    """
    auto = [p for p in completed["draftDetail"]["picks"] if p["autoDraftTypeId"] != 0]
    assert all(p["bidAmount"] == 1 for p in auto)
    assert {p["lineupSlotId"] for p in auto} == {20, 4, 17, 16}  # BE, WR, K, DST


def test_the_id_gap_survives_into_a_second_season(completed):
    """Team 6 is missing in 2025 too, independently confirming the gap.

    `range(1, count+1)` invents a phantom team and drops a real one.
    """
    team_ids = sorted({p["teamId"] for p in completed["draftDetail"]["picks"]})
    assert team_ids == [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13]
    assert 6 not in team_ids


def test_every_team_drafted_a_full_roster(completed):
    """15 draftable slots x 12 teams = 180. Confirms the roster arithmetic."""
    from collections import Counter

    per_team = Counter(p["teamId"] for p in completed["draftDetail"]["picks"])
    assert set(per_team.values()) == {15}


def test_real_teams_spend_essentially_their_whole_budget(completed):
    """Nobody hoards. Min observed was $195 of $200.

    This is what makes `max_advisable_bid` and the inflation model matter: the
    market clears at ~99.5% of the money in the room, so every dollar saved
    early is a dollar that has to be spent later at a worse price.
    """
    from collections import Counter

    spend = Counter()
    for pick in completed["draftDetail"]["picks"]:
        spend[pick["teamId"]] += pick["bidAmount"]

    assert all(190 <= total <= 200 for total in spend.values())
    assert sum(spend.values()) == 2389  # of 12 x $200 = $2400


def test_nomination_order_is_declared_up_front(completed):
    """pickOrder is a full 12-team permutation, readable before a draft starts."""
    order = completed["settings"]["draftSettings"]["pickOrder"]
    assert sorted(order) == [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13]


def test_the_completed_auction_carries_no_real_identifiers(completed):
    """Anonymized by tools/anonymize_capture.py. Guard against a raw re-drop."""
    blob = json.dumps(completed)
    assert "37CFAEAC" not in blob
    members = {p["memberId"] for p in completed["draftDetail"]["picks"] if "memberId" in p}
    assert members, "fixture should still carry member ids, just synthetic ones"
    for member_id in members:
        assert member_id.count("0000-4000-8000") == 1, member_id


def test_practice_draft_picks_are_absent_from_segment_zero(sandboxed, predraft):
    """Captured several picks into an active Practice Draft.

    It came back identical to the pre-draft baseline. Stated narrowly: practice
    picks are not in the `segments/0` draft detail for that league id. They are
    presumably readable *somewhere* — ESPN's own draft room renders them — but
    not here.

    Why this matters more than it looks: the payload is indistinguishable from
    "ESPN never populates bidAmount". Reading it that way inverts the truth and
    would strand the build on manual entry forever. Hence the probe reports
    INCONCLUSIVE on zero filled picks rather than concluding anything.
    """
    live, base = sandboxed["draftDetail"], predraft["draftDetail"]

    # inProgress stays false even while a Practice Draft is actively running.
    assert live["inProgress"] is False
    assert live["drafted"] is False
    assert live["picks"] == base["picks"]
    assert all(p["playerId"] == -1 for p in live["picks"])


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
