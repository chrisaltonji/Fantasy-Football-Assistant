"""Parsing a live ESPN draft room.

The fixture is a real snapshot taken from a running practice draft on
2026-08-17, with team names anonymized. Everything here runs offline — the
browser side is a thin attach-and-evaluate wrapper, and all the logic that can
be wrong lives in `parse_snapshot`, which is pure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ffa.domain.enums import Position, Provenance
from ffa.domain.events import PlayerSold
from ffa.ingest.espn.draftroom import (
    RoomPick,
    RoomSnapshot,
    _money,
    _team_label,
    parse_snapshot,
)
from ffa.ingest.espn.source import DraftRoomSource, diff_picks, events_for
from ffa.ingest.source import EventSource, SourceHealth

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures" / "espn" / "draftroom_snapshot_live.json"
)


@pytest.fixture(scope="module")
def raw():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def snap(raw):
    return parse_snapshot(raw)


# --- money ------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("$76", 76), ("$1", 1), ("$1,200", 1200), ("76", 76), (" $5 ", 5),
])
def test_money_parses_rendered_amounts(text, expected):
    assert _money(text) == expected


@pytest.mark.parametrize("text", ["$null", "null", "", "   ", None, "$-", "abc"])
def test_absent_money_is_none_not_zero(text):
    """ESPN renders "no bid" as the literal string `$null`.

    Coercing that to 0 would be catastrophic: zero is a *known* value, so merge
    would treat "nobody has bid" as a real price of nothing and let it beat a
    later genuine number under the same provenance.
    """
    assert _money(text) is None


# --- team labels ------------------------------------------------------------


def test_the_panel_ordinal_is_stripped_from_team_names():
    assert _team_label("3. Fighting Finkelsteins", 2) == "Fighting Finkelsteins"


def test_a_name_that_merely_contains_a_dot_is_left_alone():
    assert _team_label("A.J.'s Team", 0) == "A.J.'s Team"


def test_a_missing_name_falls_back_to_a_positional_label():
    assert _team_label(None, 4) == "team5"
    assert _team_label("   ", 0) == "team1"


# --- the board --------------------------------------------------------------


def test_the_snapshot_reads_twelve_teams_and_a_full_board(snap):
    assert len(snap.teams) == 12
    assert snap.slots_per_team == 15  # 180 cells / 12 teams, derived not assumed
    assert snap.filled == 80


def test_grid_position_maps_to_the_owning_team(snap):
    """The board is one flat grid laid out a whole team at a time.

    Cells 0-14 are team 1, 15-29 team 2, and so on, so the owning team is
    `grid_index // slots_per_team`. Getting this wrong silently credits every
    pick to the wrong manager.
    """
    for pick in snap.picks:
        assert pick.team_index == pick.grid_index // snap.slots_per_team


def test_my_picks_all_belong_to_exactly_one_team(snap):
    """The `myTeam` class is an independent check on the stride arithmetic."""
    mine = {p.team_index for p in snap.picks if p.is_mine}
    assert len(mine) == 1


def test_prices_are_read_from_the_board(snap):
    priced = [p for p in snap.picks if p.price is not None]
    assert len(priced) == snap.filled, "every completed pick showed a price"
    assert all(p.price >= 1 for p in priced)
    assert max(p.price for p in priced) > 50, "top of the market is present"


def test_player_keys_match_the_playerbook_convention(snap):
    """The key is what a sale merges on, so it must match reference data."""
    from ffa.domain.ids import normalize_player_key

    pick = next(p for p in snap.picks if " " in p.player)
    assert pick.key == normalize_player_key(pick.player)
    assert pick.key == pick.key.lower()
    assert " " not in pick.key


def test_positions_and_slots_survive_parsing(snap):
    assert all(p.position for p in snap.picks)
    assert {p.position for p in snap.picks} <= {"QB", "RB", "WR", "TE", "K", "DST", "D/ST"}


def test_the_live_bid_and_budget_are_read(snap):
    assert any(t.cash is not None for t in snap.teams)
    assert all(t.name for t in snap.teams)


def test_an_empty_page_parses_to_an_empty_snapshot():
    """A board that has not rendered yet must not raise, and must not divide by zero."""
    empty = parse_snapshot({"headers": [], "teams": [], "picks": [], "cellCount": 0})
    assert empty.filled == 0
    assert empty.slots_per_team == 0
    assert empty.teams == ()


def test_a_pick_with_no_price_yet_is_kept_with_price_none():
    """The cell can render before the price settles; that is not a reason to drop it."""
    parsed = parse_snapshot({
        "teams": [{"index": 0, "name": "1. A"}],
        "picks": [{"gridIndex": 0, "player": "Some Guy", "price": "$null", "position": "RB"}],
        "cellCount": 15,
    })
    assert parsed.picks[0].price is None
    assert parsed.picks[0].player == "Some Guy"


# --- diffing ----------------------------------------------------------------


def test_the_first_snapshot_is_all_news(snap):
    assert len(diff_picks(None, snap)) == snap.filled


def test_an_unchanged_board_produces_nothing(snap):
    assert diff_picks(snap, snap) == ()


def test_only_the_new_pick_is_emitted(snap):
    """A re-render must not replay the whole board.

    Keyed on player rather than grid index precisely so a board that reorders
    or re-mounts does not look like 80 fresh sales.
    """
    earlier = RoomSnapshot(teams=snap.teams, picks=snap.picks[:-1],
                           slots_per_team=snap.slots_per_team)
    fresh = diff_picks(earlier, snap)
    assert len(fresh) == 1
    assert fresh[0].key == snap.picks[-1].key


def test_reordering_the_board_is_not_news(snap):
    shuffled = RoomSnapshot(teams=snap.teams, picks=tuple(reversed(snap.picks)),
                            slots_per_team=snap.slots_per_team)
    assert diff_picks(snap, shuffled) == ()


# --- events -----------------------------------------------------------------


def test_a_pick_becomes_a_priced_sale(snap):
    pick = next(p for p in snap.picks if p.price is not None)
    event = events_for(pick, snap)

    assert isinstance(event, PlayerSold)
    assert event.player.key == pick.key
    assert event.price.value == pick.price
    assert event.price.provenance is Provenance.ESPN_API
    assert event.team.value == pick.team_index + 1
    assert event.source == Provenance.ESPN_API.value


def test_a_priceless_pick_becomes_an_explicit_unknown(snap):
    """Not skipped, not zeroed — an explicit unknown that merge can fill later."""
    pick = RoomPick(grid_index=0, team_index=0, player="Nobody Priced",
                    price=None, position="RB", roster_slot="RB", pro_team="DET")
    event = events_for(pick, snap)

    assert event.price.value is None
    assert event.price.provenance is Provenance.UNKNOWN
    assert not event.price.is_known


def test_the_position_is_carried_when_readable(snap):
    pick = next(p for p in snap.picks if p.position == "RB")
    assert events_for(pick, snap).position.value is Position.RB


def test_an_unreadable_position_is_omitted_rather_than_guessed(snap):
    pick = RoomPick(grid_index=0, team_index=0, player="Mystery Man",
                    price=5, position="ZZ", roster_slot=None, pro_team=None)
    assert events_for(pick, snap).position is None


# --- the seam ---------------------------------------------------------------


class _FakeReader:
    def __init__(self, snapshots):
        self._snapshots = list(snapshots)
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        if not self._snapshots:
            raise RuntimeError("no more snapshots")
        return self._snapshots.pop(0)


def test_the_draft_room_satisfies_the_event_source_protocol(snap):
    source = DraftRoomSource(_FakeReader([snap]), interval=0)
    assert isinstance(source, EventSource)
    assert source.status().health is SourceHealth.OK


def test_it_emits_one_event_per_new_pick_then_stops(snap):
    partial = RoomSnapshot(teams=snap.teams, picks=snap.picks[:5],
                           slots_per_team=snap.slots_per_team)
    source = DraftRoomSource(_FakeReader([partial, snap]), interval=0)

    emitted = []
    for event in source.events():
        emitted.append(event)
        if len(emitted) >= snap.filled:
            source.stop()

    assert len(emitted) == snap.filled
    assert all(isinstance(e, PlayerSold) for e in emitted)


def test_a_transient_failure_degrades_but_keeps_going(snap):
    """On draft day a blip must not take the tool down."""

    class Flaky:
        def __init__(self):
            self.calls = 0

        def snapshot(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("blip")
            return snap

    source = DraftRoomSource(Flaky(), interval=0, max_failures=5)
    first = next(source.events())
    assert isinstance(first, PlayerSold)
    assert source.status().health is SourceHealth.OK


def test_a_reader_that_never_recovers_stops_instead_of_hanging(snap):
    """A permanently broken reader must not spin without yielding.

    Retrying forever would hang the caller — `next()` would never return — and
    silently hide that the draft room tab is gone.
    """
    source = DraftRoomSource(_FakeReader([]), interval=0, max_failures=3)
    assert list(source.events()) == []
    assert source.status().health is SourceHealth.STOPPED
    assert "giving up after 3" in source.status().detail


def test_the_fixture_carries_no_real_team_names(raw):
    blob = json.dumps(raw)
    assert "Finkelstein" not in blob
    assert "Gibbs Me Dat" not in blob


# --- team resolution: the most dangerous mapping in the live path -----------


def test_board_position_is_not_team_id(snap):
    """The bug this resolver exists to prevent, stated as a test.

    The draft room's DOM carries no team id, and its columns are in draft
    order. In the real league, team 3 sits in column 1. `column + 1` therefore
    credits picks to the wrong manager — and in a league with an id gap it
    invents a team 6 that does not exist while never mentioning team 13.
    """
    from ffa.ingest.espn.source import TeamResolver

    real_ids = [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13]
    names = {tid: snap.team_name(i) for i, tid in enumerate(real_ids)}
    # Shuffle the board relative to id order, the way ESPN actually does.
    shuffled = {real_ids[0]: names[real_ids[2]], real_ids[2]: names[real_ids[0]]}
    names.update(shuffled)

    resolver = TeamResolver(names, real_ids)
    resolved = {resolver.resolve(snap, p) for p in snap.picks}

    assert 6 not in resolved, "never invent a team the league does not have"
    assert resolved <= set(real_ids)


def test_every_pick_resolves_to_a_real_league_team_id(snap):
    from ffa.ingest.espn.source import TeamResolver

    real_ids = [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13]
    names = {tid: snap.team_name(i) for i, tid in enumerate(real_ids)}
    resolver = TeamResolver(names, real_ids)

    assert {resolver.resolve(snap, p) for p in snap.picks} == set(real_ids)
    assert resolver.warning is None


def test_matching_ignores_case_and_punctuation():
    from ffa.ingest.espn.draftroom import RoomPick, RoomSnapshot, RoomTeam
    from ffa.ingest.espn.source import TeamResolver, normalize_team_name

    assert normalize_team_name("Chase'n the Chip!") == normalize_team_name("chasen the chip")

    snapshot = RoomSnapshot(
        teams=(RoomTeam(index=0, name="Chase'N  The Chip", cash=None, bid=None),),
        slots_per_team=15,
    )
    pick = RoomPick(grid_index=0, team_index=0, player="X", price=1,
                    position="RB", roster_slot="RB", pro_team="DET")
    assert TeamResolver({8: "Chase'n the Chip"}, [8]).resolve(snapshot, pick) == 8


def test_an_unmatched_team_falls_back_and_says_so():
    """A rename mid-draft breaks the join. It must be loud, not silent."""
    from ffa.ingest.espn.draftroom import RoomPick, RoomSnapshot, RoomTeam
    from ffa.ingest.espn.source import TeamResolver

    snapshot = RoomSnapshot(
        teams=(RoomTeam(index=0, name="Renamed Overnight", cash=None, bid=None),),
        slots_per_team=15,
    )
    pick = RoomPick(grid_index=0, team_index=0, player="X", price=1,
                    position="RB", roster_slot="RB", pro_team="DET")

    resolver = TeamResolver({3: "Gibbs Me Dat"}, [3, 5, 7])
    assert resolver.resolve(snapshot, pick) == 3  # positional fallback
    assert "Renamed Overnight" in resolver.warning
    assert "config init" in resolver.warning


def test_with_no_names_at_all_it_still_uses_league_ids_not_positions():
    """Even the fallback must not invent ids."""
    from ffa.ingest.espn.draftroom import RoomPick, RoomSnapshot, RoomTeam
    from ffa.ingest.espn.source import TeamResolver

    snapshot = RoomSnapshot(
        teams=tuple(RoomTeam(index=i, name=f"T{i}", cash=None, bid=None) for i in range(3)),
        slots_per_team=15,
    )
    resolver = TeamResolver({}, [1, 7, 13])
    picks = [RoomPick(grid_index=i, team_index=i, player=f"P{i}", price=1,
                      position="RB", roster_slot="RB", pro_team="DET") for i in range(3)]

    assert [resolver.resolve(snapshot, p) for p in picks] == [1, 7, 13]


def test_a_sale_carries_the_resolved_team_id(snap):
    from ffa.ingest.espn.source import TeamResolver, events_for

    real_ids = [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13]
    names = {tid: snap.team_name(i) for i, tid in enumerate(real_ids)}
    resolver = TeamResolver(names, real_ids)

    pick = snap.picks[0]
    event = events_for(pick, snap, resolver)
    assert event.team.value == resolver.resolve(snap, pick)
    assert event.team.value in real_ids
