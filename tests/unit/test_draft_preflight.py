"""What has to be true before a draft starts.

Three of the four things that bit during the live rehearsal were not bugs in the
reading of the board — they were the tool cheerfully starting against the wrong
board. Every check here is a refusal, and every refusal exists because the
alternative failure is silent: an attach that succeeds, a pre-flight that says
`matched 12/12`, and a draft that quietly never records anything.
"""

from __future__ import annotations

import argparse

import pytest

from ffa.config.schema import ConfigError
from ffa.ingest.espn.draftroom import (
    DRAFT_URL_FRAGMENT,
    DraftRoomError,
    DraftRoomReader,
    RoomPick,
    RoomSnapshot,
    RoomTeam,
)


# --- fakes for the browser side -----------------------------------------------


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeContext:
    def __init__(self, pages) -> None:
        self.pages = pages


class FakeBrowser:
    def __init__(self, *urls: str) -> None:
        self.contexts = [FakeContext([FakePage(u) for u in urls])]


def reader_over(*urls: str, tab_match: str = "") -> DraftRoomReader:
    reader = DraftRoomReader(tab_match=tab_match)
    reader._browser = FakeBrowser(*urls)
    return reader


LEAGUE = f"https://{DRAFT_URL_FRAGMENT}?leagueId=1167816302"
PRACTICE = f"https://{DRAFT_URL_FRAGMENT}?leagueId=9990001"


# --- one draft room, and only one ---------------------------------------------


def test_the_single_open_draft_room_is_the_one_read():
    reader = reader_over("https://example.com", LEAGUE)
    assert reader._find_page().url == LEAGUE


def test_no_draft_room_open_is_a_miss_not_a_guess():
    assert reader_over("https://example.com")._find_page() is None


def test_two_draft_rooms_open_is_refused_rather_than_guessed():
    """The rehearsal failure. Picking the first match is a coin flip.

    Both tabs attach cleanly and both pre-flight `matched 12/12`, because they
    are the same league — so reading the wrong one looks exactly like a draft
    that has not started yet. There is nothing to notice until the board has
    been frozen for ten minutes.
    """
    reader = reader_over(LEAGUE, PRACTICE)

    with pytest.raises(DraftRoomError) as exc:
        reader._find_page()

    message = str(exc.value)
    assert "2 draft-room tabs" in message
    # Both URLs listed: the user has to be able to tell which to close.
    assert LEAGUE in message and PRACTICE in message
    assert "--tab" in message


def test_tab_narrows_an_otherwise_ambiguous_pair():
    reader = reader_over(LEAGUE, PRACTICE, tab_match="1167816302")
    assert reader._find_page().url == LEAGUE


def test_tab_matching_is_case_insensitive_because_you_paste_it():
    reader = reader_over(f"https://{DRAFT_URL_FRAGMENT}?leagueId=ABC123")
    reader.tab_match = "abc123"
    assert reader._find_page() is not None


def test_a_tab_filter_that_matches_nothing_is_a_miss_not_the_first_tab():
    """Narrowing must never widen back out. That would be the original bug."""
    reader = reader_over(LEAGUE, PRACTICE, tab_match="no-such-league")
    assert reader._find_page() is None


def test_draft_tabs_lists_what_is_open_for_the_error_message():
    assert reader_over(LEAGUE, "https://example.com", PRACTICE).draft_tabs() == (
        LEAGUE,
        PRACTICE,
    )


# --- the stale board ------------------------------------------------------------


def board(filled: int, teams: int = 12, slots: int = 15) -> RoomSnapshot:
    picks = tuple(
        RoomPick(
            grid_index=i, team_index=i // slots, player=f"Player {i}",
            price=1, position="RB", roster_slot="BE", pro_team="CIN",
        )
        for i in range(filled)
    )
    return RoomSnapshot(
        teams=tuple(
            RoomTeam(index=i, name=f"Team {i}", cash=200, bid=None)
            for i in range(teams)
        ),
        picks=picks,
        slots_per_team=slots,
    )


def guard(snapshot, *, resuming=False, allow=False):
    from ffa.cli.app import _check_board_is_live

    args = argparse.Namespace(allow_finished_board=allow)
    _check_board_is_live(snapshot, resuming=resuming, args=args)


def test_a_new_draft_against_a_finished_board_is_refused():
    """180/180 is not a draft about to happen. It is one that already did."""
    with pytest.raises(ConfigError) as exc:
        guard(board(180))

    message = str(exc.value)
    assert "180/180" in message
    assert "--allow-finished-board" in message
    assert "--resume" in message


def test_an_empty_board_is_exactly_what_a_new_draft_should_see():
    guard(board(0))  # no raise, and nothing to say


def test_a_part_filled_board_starts_but_says_what_it_is_about_to_record(capsys):
    """Attaching mid-draft works and is worth doing.

    Those picks are about to pour into a brand-new journal, though, so it gets
    said out loud rather than discovered in `budgets`.
    """
    guard(board(40))

    warned = capsys.readouterr().err
    assert "40 pick(s)" in warned
    assert "180" in warned


def test_resuming_into_a_finished_board_is_normal_and_silent(capsys):
    """The draft ended; you are reopening it to look at it."""
    guard(board(180), resuming=True)
    assert capsys.readouterr().err == ""


def test_the_override_records_a_finished_board_and_says_so(capsys):
    guard(board(180), allow=True)
    assert "180/180" in capsys.readouterr().err


def test_a_board_whose_size_is_unknown_does_not_trip_the_guard():
    """`slots_per_team` is derived, so zero teams means zero total.

    Refusing on `filled >= 0` would block every draft.
    """
    empty_shape = RoomSnapshot(teams=(), picks=(), slots_per_team=0)
    guard(empty_shape)
