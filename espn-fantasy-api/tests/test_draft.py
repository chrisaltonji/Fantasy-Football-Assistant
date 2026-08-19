"""The REST draft view, and the three states of it that look identical.

The single most expensive thing to get wrong about this API is that a
pre-draft skeleton, a *running* draft, and a draft that ESPN simply never
wrote all return payloads you cannot tell apart by size. Everything here
exists to make that distinction testable.
"""

from __future__ import annotations

from espn_fantasy.draft import UNFILLED, board_from_payload, new_picks


# --- before anything happens ------------------------------------------------


def test_the_whole_board_exists_before_anyone_drafts(predraft):
    """All 180 picks are pre-created with sentinels.

    So `len(picks)` says nothing about progress, and anything polling for
    "more picks" will wait forever — the array never grows.
    """
    board = board_from_payload(predraft)
    assert len(board.picks) == 180
    assert board.filled == ()
    assert all(p.player_id == UNFILLED for p in board.picks)
    assert all(p.bid_amount is None for p in board.picks)


def test_a_zero_bid_is_recorded_as_unknown_not_as_free():
    """`bidAmount: 0` on an unwritten board is ignorance, not a $0 player."""
    payload = {"draftDetail": {"picks": [{"playerId": -1, "bidAmount": 0}]}}
    assert board_from_payload(payload).picks[0].bid_amount is None


def test_the_nomination_order_is_published_before_the_draft_starts(predraft):
    """`nominatingTeamId` is filled on all 180 picks while the board is empty.

    The entire nomination sequence is readable in advance, which is not
    something you would assume from a payload that is otherwise all sentinels.
    """
    board = board_from_payload(predraft)
    order = board.nomination_order()
    assert len(order) == 180
    assert len(set(order)) == 12


def test_the_pick_order_is_a_full_permutation_of_the_league(completed):
    board = board_from_payload(completed)
    assert len(board.pick_order) == 12
    assert len(set(board.pick_order)) == 12


# --- while it runs ----------------------------------------------------------


def test_a_running_draft_reports_in_progress_and_zero_filled(practice_live):
    """Captured during an actively running draft with players already bought.

    Sixty polls over fifteen minutes returned exactly this. The REST view is
    not written while a draft runs — so this payload means "no information",
    despite looking like a confident report of an empty board.
    """
    board = board_from_payload(practice_live)
    assert board.in_progress is True
    assert board.drafted is False
    assert board.filled == ()


def test_that_state_is_described_rather_than_reported_as_an_empty_board(practice_live):
    """The description has to say the reading is meaningless, or nobody knows."""
    text = board_from_payload(practice_live).describe()
    assert "not written during a live draft" in text


def test_a_started_and_a_not_started_draft_are_distinguishable(predraft, practice_live):
    assert board_from_payload(predraft).describe() != \
        board_from_payload(practice_live).describe()


def test_partial_progress_is_reported_when_espn_does_write_it(inprogress):
    board = board_from_payload(inprogress)
    assert 0 < len(board.filled) < len(board.picks)


# --- afterwards -------------------------------------------------------------


def test_a_completed_auction_carries_every_real_bid(completed):
    """The payoff: this view is excellent after the fact, and useless during."""
    board = board_from_payload(completed)
    assert board.drafted is True
    assert len(board.filled) == 180
    assert board.has_prices

    bids = [p.bid_amount for p in board.filled]
    assert min(bids) == 1
    assert max(bids) > 50


def test_teams_spend_essentially_the_whole_budget(completed):
    """Every team lands within a few dollars of $200 of $200.

    Worth pinning because it is the fact that makes the auction data worth
    having: the market clears at ~99.5% of the money in the room.
    """
    board = board_from_payload(completed)
    spend = board.spend_by_team()
    assert len(spend) == 12
    assert all(190 <= total <= 200 for total in spend.values())


def test_autodrafted_picks_carry_no_member_id(completed):
    """`memberId` is on human picks and absent on autodrafted ones.

    Nothing may key on it unconditionally — `teamId` is the reliable anchor.
    This bites on draft day specifically, because an expired nomination clock
    produces exactly this pick.
    """
    board = board_from_payload(completed)
    auto = [p for p in board.filled if p.autodrafted]
    human = [p for p in board.filled if not p.autodrafted]

    assert auto, "the capture contains autodrafted picks"
    assert all(p.member_id is None for p in auto)
    assert all(p.member_id for p in human)


def test_autodraft_means_nobody_bid_not_a_filler_slot(completed):
    """Every autodraft went for $1, but they are not all bench spots."""
    board = board_from_payload(completed)
    auto = [p for p in board.filled if p.autodrafted]
    assert all(p.bid_amount == 1 for p in auto)
    assert len({p.lineup_slot_id for p in auto}) > 1


def test_the_id_gap_holds_across_seasons(completed):
    """2025 shows the same 1-5, 7-13. The hole is durable, not a one-off."""
    team_ids = {p.team_id for p in board_from_payload(completed).filled}
    assert 6 not in team_ids
    assert team_ids == {1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13}


# --- diffing ----------------------------------------------------------------


def test_new_picks_are_keyed_on_the_pick_number_not_the_array_length(predraft,
                                                                    inprogress):
    """The array is the same 180 entries every poll; only the sentinels change."""
    before = board_from_payload(predraft)
    after = board_from_payload(inprogress)

    fresh = new_picks(before.picks, after.picks)
    assert len(fresh) == len(after.filled)
    assert all(p.filled for p in fresh)


def test_re_polling_an_unchanged_board_produces_no_news(inprogress):
    board = board_from_payload(inprogress)
    assert new_picks(board.picks, board.picks) == ()


# --- degenerate input -------------------------------------------------------


def test_a_payload_with_no_draft_detail_is_an_empty_board():
    board = board_from_payload({"settings": {}})
    assert board.picks == ()
    assert "no draft board" in board.describe()
