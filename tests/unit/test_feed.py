"""Capability 6 — the ambient feed.

Two properties carry the whole design, and both are tested here rather than
asserted in a docstring:

- **It is a fold, not a log.** Nothing is appended anywhere. The same journal
  always produces the same feed, and undoing an event removes its entry rather
  than leaving a line about something that no longer happened.
- **Every rule is bounded.** A rule that can fire on every pick makes the feed
  unreadable, which is worse than not having one. The boundedness test below is
  the enforcement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ffa.advice.feed import build_feed
from ffa.config.strategy import StrategyPreset
from ffa.domain.enums import Position, RosterSlot
from ffa.domain.events import DraftInitialized, EventUndone, TeamSeed
from ffa.domain.ids import normalize_player_key
from ffa.domain.models import LeagueSnapshot
from ffa.reference.loader import ReferenceRow
from ffa.reference.playerbook import PlayerBook
from tests.conftest import at, sold

SEATS = (1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13)
ME = 4


@pytest.fixture
def snapshot() -> LeagueSnapshot:
    return LeagueSnapshot(
        league_id=1, year=2026, name="Test", draft_type="AUCTION", budget=200,
        team_count=12, my_team_id=ME,
        roster={
            RosterSlot.QB: 1, RosterSlot.RB: 2, RosterSlot.WR: 2, RosterSlot.TE: 1,
            RosterSlot.FLEX: 1, RosterSlot.K: 1, RosterSlot.DST: 1, RosterSlot.BE: 5,
            RosterSlot.IR: 1,
        },
        flex_positions=(Position.RB, Position.WR, Position.TE),
    )


def row(name: str, position: Position, value: float, rank: int) -> ReferenceRow:
    return ReferenceRow(key=normalize_player_key(name), name=name,
                        position=position, auction_value=value, overall_rank=rank)


@pytest.fixture
def book() -> PlayerBook:
    rows, rank = [], 1
    for position, top in ((Position.RB, 60), (Position.WR, 55), (Position.TE, 20),
                          (Position.QB, 18), (Position.K, 2), (Position.DST, 2)):
        for i in range(40):
            rows.append(row(f"{position.value}{i}", position, max(1, top - i), rank))
            rank += 1
    return PlayerBook(tuple(rows), source=Path("t.csv"))


def init(snapshot) -> DraftInitialized:
    return DraftInitialized(
        id=1, at=at(), draft_id="d", league=snapshot,
        teams=tuple(TeamSeed(team_id=i, manager=f"mgr{i}") for i in SEATS),
    )


def full_draft(snapshot, book):
    """A whole auction: every team fills every draftable slot.

    Built rather than fixtured because the boundedness claim is about a *full*
    draft — a rule that behaves at pick 20 and floods at pick 150 is exactly
    what this is here to catch.
    """
    events = [init(snapshot)]
    eid, second = 2, 0
    pool = {p: list(book.by_position(p)) for p in Position}
    taken = {p: 0 for p in Position}
    order = [Position.RB, Position.WR, Position.QB, Position.WR, Position.TE,
             Position.RB, Position.K, Position.WR, Position.DST, Position.RB,
             Position.TE, Position.WR, Position.QB, Position.RB]

    def next_player(preferred):
        """Take from the preferred position, else from wherever stock remains.

        Draining one position dry is the realistic case, not a setup bug — but
        it must not end the draft early, or the boundedness claim would be
        tested against two thirds of an auction.
        """
        candidates = [preferred] + [p for p in Position if p is not preferred]
        for position in candidates:
            if taken[position] < len(pool[position]):
                player = pool[position][taken[position]]
                taken[position] += 1
                return position, player
        return None, None

    for slot_index in range(snapshot.draftable_slots):
        for team in SEATS:
            position, player = next_player(order[slot_index % len(order)])
            if player is None:
                continue
            price = max(1, int((book.value(player.key) or 1) * 1.4))
            events.append(sold(eid, player.name, team, price,
                               pos=position, seconds=second))
            eid += 1
            second += 1
    return events


# --- a fold, not a log ------------------------------------------------------


def test_the_same_journal_always_gives_the_same_feed(snapshot, book):
    events = full_draft(snapshot, book)
    first = build_feed(events, book)
    second = build_feed(events, book)

    assert [e.as_dict() for e in first] == [e.as_dict() for e in second]


def test_undoing_a_pick_removes_its_entry_rather_than_contradicting_it(snapshot, book):
    """The entry is not retracted — it never happened. That falls out of being
    a fold over the surviving events, and it is why nothing is stored."""
    buy = sold(2, "RB0", ME, 40, pos=Position.RB, seconds=1)
    events = [init(snapshot), buy]

    before = build_feed(events, book)
    assert any(e.channel == "yours" for e in before)

    after = build_feed(events + [EventUndone(id=3, at=at(2), target_id=2)], book)
    assert not any(e.channel == "yours" for e in after)


def test_no_journal_no_feed(snapshot, book):
    assert build_feed([], book) == ()
    assert build_feed([init(snapshot)], book) == ()


def test_it_works_with_no_reference_data(snapshot):
    """Half the rules need prices. The half that does not still runs, because a
    draft with no sheet is a draft the tool still has to follow."""
    empty = PlayerBook((), source=Path("t.csv"))
    events = [init(snapshot), sold(2, "Somebody", ME, 40, pos=Position.RB, seconds=1)]

    feed = build_feed(events, empty)
    assert any(e.channel == "yours" for e in feed)


# --- the enforcement --------------------------------------------------------


def test_every_rule_stays_bounded_over_a_full_draft(snapshot, book):
    """The bar a rule has to clear. Every entry costs attention during a live
    auction, so a rule that scales with picks is a rule that ruins the feed —
    the `room` channel originally reported every decrement and produced eleven
    near-identical lines in a row.
    """
    events = full_draft(snapshot, book)
    feed = build_feed(events, book, limit=10_000)

    counts: dict[str, int] = {}
    for entry in feed:
        counts[entry.channel] = counts.get(entry.channel, 0) + 1

    sales = sum(1 for e in events if e.__class__.__name__ == "PlayerSold")
    assert sales >= 150, "the point of this test is a *full* draft"

    # One per pick of mine, and no more: this is capability 8, per purchase.
    assert counts.get("yours", 0) <= snapshot.draftable_slots
    # Two per position at most — elite-gone and drying-up each fire once.
    assert counts.get("scarcity", 0) <= 2 * len(Position)
    # One per milestone, not one per decrement.
    assert counts.get("room", 0) <= 3
    # The whole feed has to stay readable in a sidebar.
    assert len(feed) < sales / 3, f"{len(feed)} entries for {sales} picks is a log, not a feed"


def test_the_feed_is_newest_first(snapshot, book):
    feed = build_feed(full_draft(snapshot, book), book, limit=10_000)
    stamps = [e.at for e in feed if e.at]
    assert stamps == sorted(stamps, reverse=True)


def test_the_limit_keeps_the_newest(snapshot, book):
    everything = build_feed(full_draft(snapshot, book), book, limit=10_000)
    capped = build_feed(full_draft(snapshot, book), book, limit=5)

    assert len(capped) == 5
    assert [e.text for e in capped] == [e.text for e in everything[:5]]


# --- individual rules -------------------------------------------------------


def test_your_own_pick_reports_what_is_left_to_fill(snapshot, book):
    """Capability 8. What it filled, never whether it was a good buy — that is
    capability 11's job, after the draft, with the whole roster to judge."""
    events = [init(snapshot), sold(2, "RB0", ME, 40, pos=Position.RB, seconds=1)]

    entry = next(e for e in build_feed(events, book) if e.channel == "yours")
    assert "You bought" in entry.text and "$40" in entry.text
    assert "Still to fill" in entry.text
    assert "good" not in entry.text.lower()


def test_a_rivals_pick_is_not_reported_as_yours(snapshot, book):
    events = [init(snapshot), sold(2, "RB0", 9, 40, pos=Position.RB, seconds=1)]
    assert not any(e.channel == "yours" for e in build_feed(events, book))


def test_a_small_overpay_is_not_news(snapshot, book):
    """`value_alert` already applies the dynamic threshold; the feed adds a
    dollar floor so a $3 overpay on a kicker does not read like a headline."""
    events = [init(snapshot), sold(2, "K0", 9, 4, pos=Position.K, seconds=1)]
    assert not any(e.channel == "sale" for e in build_feed(events, book))


def test_the_plan_channel_needs_a_plan(snapshot, book):
    events = full_draft(snapshot, book)

    assert not any(e.channel == "plan"
                   for e in build_feed(events, book, limit=10_000))
    assert not any(e.channel == "plan"
                   for e in build_feed(events, book, strategy=StrategyPreset(),
                                       limit=10_000))


def test_a_declared_plan_reports_its_crossings(snapshot, book):
    plan = StrategyPreset(
        archetype="balanced",
        budget_by_position={Position.RB: 70, Position.WR: 70, Position.QB: 15,
                            Position.TE: 15, Position.K: 1, Position.DST: 2},
        max_on_one_player=70, bench_reserve=27,
    )
    # Spend nearly everything on one player: the plan becomes unaffordable.
    events = [init(snapshot), sold(2, "RB0", ME, 190, pos=Position.RB, seconds=1)]

    feed = build_feed(events, book, strategy=plan)
    assert any(e.channel == "plan" and "time to move" in e.text for e in feed)


def test_entries_carry_structure_not_just_prose(snapshot, book):
    """A surface should style from fields rather than parse the sentence."""
    events = [init(snapshot), sold(2, "RB0", ME, 40, pos=Position.RB, seconds=1)]

    entry = next(e for e in build_feed(events, book) if e.channel == "yours")
    assert entry.amount == 40
    assert entry.player == "RB0"
    assert entry.priority in ("high", "normal", "low")
    assert entry.at
