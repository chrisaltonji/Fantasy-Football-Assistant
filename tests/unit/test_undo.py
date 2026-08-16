from __future__ import annotations

from ffa.domain import reducers
from ffa.domain.events import EventUndone
from ffa.domain.projections import remaining_budget
from tests.conftest import at, sold


def undo(event_id: int, target_id: int) -> EventUndone:
    return EventUndone(id=event_id, at=at(), target_id=target_id)


def test_undone_event_is_suppressed(init_event):
    events = [init_event, sold(2, "mahomes", 3, 45), undo(3, 2)]
    assert reducers.undone_ids(events) == {2}
    assert reducers.replay(events).sold_players() == ()


def test_two_undos_remove_the_two_most_recent_picks(init_event):
    """What a human expects. The alternative — the second undo toggling the
    first one back on — would be maddening under time pressure."""
    events = [
        init_event,
        sold(2, "mahomes", 3, 45),
        sold(3, "barkley", 1, 62),
        undo(4, 3),
        undo(5, 2),
    ]
    assert reducers.undone_ids(events) == {2, 3}
    assert reducers.replay(events).sold_players() == ()


def test_undoing_an_undo_is_redo(init_event):
    events = [init_event, sold(2, "mahomes", 3, 45), undo(3, 2), undo(4, 3)]
    # Undo #3 is retired; the sale it suppressed is live again.
    assert reducers.undone_ids(events) == {3}
    assert len(reducers.replay(events).sold_players()) == 1


def test_redo_restores_the_exact_prior_state(init_event):
    base = [init_event, sold(2, "mahomes", 3, 45)]
    after_redo = reducers.replay(base + [undo(3, 2), undo(4, 3)])
    assert remaining_budget(after_redo, 3) == remaining_budget(reducers.replay(base), 3)


def test_undoing_an_already_undone_event_is_ignored_on_replay(init_event):
    """The command layer rejects this; a hand-edited journal can still hold it."""
    events = [init_event, sold(2, "mahomes", 3, 45), undo(3, 2), undo(4, 2)]
    assert reducers.undone_ids(events) == {2}


def test_undo_of_a_nonexistent_target_is_harmless(init_event):
    assert reducers.undone_ids([init_event, undo(2, 999)]) == {999}


def test_event_undone_is_never_applied(init_event):
    """Undo lives in the replay filter. Applying it must be a no-op."""
    state = reducers.replay([init_event])
    assert reducers.apply(state, undo(9, 1)) == state


def test_replay_is_deterministic_under_interleaved_undo_and_redo(init_event):
    events = [
        init_event,
        sold(2, "mahomes", 3, 45, seconds=1),
        sold(3, "barkley", 1, 62, seconds=2),
        undo(4, 2),
        sold(5, "chase", 3, 30, seconds=3),
        undo(6, 4),
        sold(7, "kelce", 1, 20, seconds=4),
        undo(8, 3),
    ]
    results = [reducers.replay(events) for _ in range(5)]
    assert all(r == results[0] for r in results)
    # mahomes restored by the redo, barkley undone, chase and kelce live.
    assert {p.ref.key for p in results[0].sold_players()} == {"mahomes", "chase", "kelce"}


def test_undone_ids_does_not_depend_on_input_being_a_list(init_event):
    events = [init_event, sold(2, "mahomes", 3, 45), undo(3, 2)]
    assert reducers.undone_ids(iter(events)) == {2}
