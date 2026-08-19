"""Polling, diffing, and crediting picks to the right team.

`TeamResolver` gets the most attention here because it is the one place a
wrong answer is both easy to produce and invisible once produced.
"""

from __future__ import annotations

from espn_fantasy.draftroom import RoomNomination, RoomPick, RoomSnapshot, RoomTeam
from espn_fantasy.watch import (
    DraftRoomWatcher,
    Health,
    Nominated,
    Sale,
    TeamResolver,
    diff_picks,
)


def pick(player: str, *, grid: int = 0, team: int = 0, price: int | None = 10) -> RoomPick:
    return RoomPick(
        grid_index=grid, team_index=team, player=player, price=price,
        position="RB", roster_slot="RB", pro_team="DET",
    )


def snapshot(*picks: RoomPick, teams: tuple[RoomTeam, ...] = ()) -> RoomSnapshot:
    return RoomSnapshot(teams=teams, picks=picks, slots_per_team=15)


class FakeReader:
    """Returns queued snapshots; an Exception in the queue is raised instead."""

    def __init__(self, *results) -> None:
        self.results = list(results)
        self.reads = 0

    def snapshot(self) -> RoomSnapshot:
        self.reads += 1
        result = self.results.pop(0) if self.results else RoomSnapshot()
        if isinstance(result, Exception):
            raise result
        return result


# --- diffing ----------------------------------------------------------------


def test_a_new_player_on_the_board_is_news():
    before = snapshot(pick("Jahmyr Gibbs"))
    after = snapshot(pick("Jahmyr Gibbs"), pick("Bijan Robinson", grid=1))

    assert [p.player for p in diff_picks(before, after)] == ["Bijan Robinson"]


def test_a_re_render_does_not_replay_the_whole_board():
    """Diffs key on the player, not the grid index.

    ESPN's board reorders and re-renders. Keying on position would republish
    eighty sales as fresh news the first time a column shifted.
    """
    before = snapshot(pick("Jahmyr Gibbs", grid=0), pick("Bijan Robinson", grid=1))
    after = snapshot(pick("Bijan Robinson", grid=7), pick("Jahmyr Gibbs", grid=9))

    assert diff_picks(before, after) == ()


def test_the_first_poll_reports_everything_already_on_the_board():
    assert len(diff_picks(None, snapshot(pick("A"), pick("B", grid=1)))) == 2


# --- crediting picks --------------------------------------------------------


def test_a_pick_is_credited_by_team_name_not_by_column():
    """The DOM carries no team id, and column order is draft order, not id order.

    In the league this was built against, team 3 sits in column 1. `column +
    1` therefore credits picks to the wrong manager on almost every pick, and
    nothing about the result looks wrong until someone checks a budget.
    """
    resolver = TeamResolver({3: "Fighting Finkelsteins", 1: "Other Guys"}, (1, 3))
    board = snapshot(
        pick("Jahmyr Gibbs", team=0),
        teams=(RoomTeam(0, "Fighting Finkelsteins", 100, None),),
    )

    assert resolver.resolve(board, board.picks[0]) == 3


def test_name_matching_ignores_case_and_punctuation():
    resolver = TeamResolver({5: "The Ram-Rods!"}, (5,))
    board = snapshot(pick("A", team=0), teams=(RoomTeam(0, "the ram rods", 1, None),))
    assert resolver.resolve(board, board.picks[0]) == 5


def test_an_unmatched_team_falls_back_to_league_id_order_and_warns():
    """Never a silent guess: the fallback is announced."""
    resolver = TeamResolver({1: "Known"}, (1, 3, 7))
    board = snapshot(pick("A", team=1), teams=(
        RoomTeam(0, "Known", 1, None), RoomTeam(1, "Renamed Overnight", 1, None),
    ))

    assert resolver.resolve(board, board.picks[0]) == 3
    assert "Renamed Overnight" in resolver.warning


def test_with_no_league_data_at_all_the_fallback_is_column_order():
    resolver = TeamResolver()
    board = snapshot(pick("A", team=4))

    assert resolver.resolve(board, board.picks[0]) == 5
    # And it says so as "no data" rather than "could not match", which would
    # send someone hunting for a rename that never happened.
    assert "no league team data" in resolver.warning


def test_an_audit_finds_a_rename_before_the_draft_rather_than_at_pick_forty():
    resolver = TeamResolver({1: "Known", 2: "Also Known"}, (1, 2))
    board = snapshot(teams=(
        RoomTeam(0, "Known", 1, None), RoomTeam(1, "Renamed Overnight", 1, None),
    ))

    matched, unmatched = resolver.audit(board)
    assert matched == {"Known": 1}
    assert unmatched == ["Renamed Overnight"]


def test_an_audit_does_not_leave_the_resolver_in_a_warning_state():
    """A pre-flight report is a question, not an event."""
    resolver = TeamResolver({1: "Known"}, (1,))
    resolver.audit(snapshot(teams=(RoomTeam(0, "Unknown", 1, None),)))
    assert resolver.warning is None


# --- the watcher ------------------------------------------------------------


def test_a_sale_reports_the_player_the_price_and_the_team():
    reader = FakeReader(snapshot(
        pick("Jahmyr Gibbs", price=76),
        teams=(RoomTeam(0, "Fighting Finkelsteins", 100, None),),
    ))
    watcher = DraftRoomWatcher(reader, resolver=TeamResolver({3: "Fighting Finkelsteins"}))

    events = watcher.poll()
    assert len(events) == 1
    assert isinstance(events[0], Sale)
    assert (events[0].player, events[0].price, events[0].team_id) == \
        ("Jahmyr Gibbs", 76, 3)


def test_a_price_the_board_has_not_settled_is_unknown_not_zero():
    reader = FakeReader(snapshot(pick("Jahmyr Gibbs", price=None)))
    assert DraftRoomWatcher(reader).poll()[0].price is None


def test_the_player_on_the_block_is_reported_before_the_sale():
    """Nomination first: guidance is worth more the earlier it lands."""
    board = RoomSnapshot(
        picks=(pick("Sold Guy"),),
        nominated=RoomNomination("Bijan Robinson", current_bid=36),
        slots_per_team=15,
    )
    events = DraftRoomWatcher(FakeReader(board)).poll()

    assert isinstance(events[0], Nominated)
    assert isinstance(events[1], Sale)
    assert events[0].current_bid == 36


def test_the_same_nomination_is_not_reported_twice():
    board = RoomSnapshot(nominated=RoomNomination("Bijan Robinson"))
    watcher = DraftRoomWatcher(FakeReader(board, board))

    assert len(watcher.poll()) == 1
    assert watcher.poll() == []


def test_a_new_nomination_after_a_clear_is_reported_again():
    nominated = RoomSnapshot(nominated=RoomNomination("Bijan Robinson"))
    empty = RoomSnapshot()
    watcher = DraftRoomWatcher(FakeReader(nominated, empty, nominated))

    assert len(watcher.poll()) == 1
    assert watcher.poll() == []
    assert len(watcher.poll()) == 1


def test_a_failed_read_degrades_rather_than_raising():
    """On draft day a blip must not take the tool down."""
    watcher = DraftRoomWatcher(FakeReader(RuntimeError("tab backgrounded")))

    assert watcher.poll() == []
    assert watcher.status.health is Health.DEGRADED
    assert "tab backgrounded" in watcher.status.detail


def test_a_recovered_read_clears_the_degraded_state():
    board = snapshot(pick("A", team=0), teams=(RoomTeam(0, "Known", 1, None),))
    watcher = DraftRoomWatcher(
        FakeReader(RuntimeError("blip"), board),
        resolver=TeamResolver({1: "Known"}, (1,)),
    )
    watcher.poll()
    watcher.poll()
    assert watcher.status.health is Health.OK


def test_it_stops_instead_of_retrying_a_reader_that_is_gone():
    """Failing forever is not a blip — the tab was closed, or Chrome exited.

    A loop that silently never yields again hides exactly that, which is the
    worst possible failure to have during a draft.
    """
    watcher = DraftRoomWatcher(
        FakeReader(*[RuntimeError("gone")] * 3), max_failures=3
    )
    for _ in range(3):
        watcher.poll()

    assert watcher.status.health is Health.STOPPED
    assert "giving up" in watcher.status.detail


def test_the_event_loop_ends_when_the_reader_gives_up():
    watcher = DraftRoomWatcher(
        FakeReader(*[RuntimeError("gone")] * 2), max_failures=2
    )
    assert list(watcher.events(sleep=lambda _: None)) == []
    assert watcher.status.health is Health.STOPPED


def test_the_event_loop_yields_sales_as_they_land():
    watcher = DraftRoomWatcher(FakeReader(
        snapshot(pick("A")),
        snapshot(pick("A"), pick("B", grid=1)),
        *[RuntimeError("done")] * 2,
    ), max_failures=2)

    players = [e.player for e in watcher.events(sleep=lambda _: None)]
    assert players == ["A", "B"]


def test_a_bad_team_match_is_surfaced_once_through_the_status():
    """Silently miscrediting picks is the failure the resolver exists to prevent."""
    reader = FakeReader(snapshot(pick("A", team=0), teams=(RoomTeam(0, "Renamed", 1, None),)))
    watcher = DraftRoomWatcher(reader, resolver=TeamResolver({1: "Original"}, (1,)))

    watcher.poll()
    assert watcher.status.health is Health.DEGRADED
    assert "Renamed" in watcher.status.detail
