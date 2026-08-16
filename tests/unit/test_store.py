from __future__ import annotations

import os
from pathlib import Path

import pytest

from ffa.domain.events import EventUndone
from ffa.domain.projections import remaining_budget
from ffa.domain.reducers import replay
from ffa.state.journal import read_events
from ffa.state.store import LOCK_NAME, DraftStore, LockError
from tests.conftest import at, sold


@pytest.fixture
def store(tmp_path: Path, init_event):
    with DraftStore.create(tmp_path / "run", init_event) as s:
        yield s


def test_create_writes_the_init_event_first(store):
    events, _ = read_events(store.journal_path)
    assert len(events) == 1 and events[0].id == 1
    assert store.state.is_initialized


def test_dispatch_returns_new_state_and_records_the_id(store):
    state = store.dispatch(sold(0, "mahomes", 3, 45))
    assert remaining_budget(state, 3) == 155
    assert store.events[-1].id == 2


def test_incremental_dispatch_always_equals_a_full_replay(store):
    """The most valuable invariant in the store: the fast path and the slow
    path must never disagree."""
    for event in [
        sold(0, "mahomes", 3, 45),
        sold(0, "barkley", 1, 62),
        EventUndone(at=at(), target_id=2),
        sold(0, "chase", 3, 30),
    ]:
        store.dispatch(event)
        from_disk, _ = read_events(store.journal_path)
        assert store.state == replay(from_disk, store.state.draft_id)


def test_undo_triggers_a_replay_and_refunds_the_team(store):
    store.dispatch(sold(0, "mahomes", 3, 45))
    assert remaining_budget(store.state, 3) == 155
    store.dispatch(EventUndone(at=at(), target_id=2))
    assert remaining_budget(store.state, 3) == 200


def test_subscribers_see_every_event(store):
    seen = []
    store.subscribe(lambda state, event: seen.append(event.id))
    store.dispatch(sold(0, "mahomes", 3, 45))
    store.dispatch(sold(0, "barkley", 1, 62))
    assert seen == [2, 3]


# --- resume -------------------------------------------------------------------


def test_resume_reproduces_state_exactly(tmp_path: Path, init_event):
    run = tmp_path / "run"
    with DraftStore.create(run, init_event) as original:
        original.dispatch(sold(0, "mahomes", 3, 45))
        original.dispatch(sold(0, "barkley", 1, 62))
        original.dispatch(EventUndone(at=at(), target_id=3))
        expected = original.state

    with DraftStore.resume(run) as resumed:
        assert resumed.state == expected


def test_resume_continues_the_id_sequence(tmp_path: Path, init_event):
    run = tmp_path / "run"
    with DraftStore.create(run, init_event) as store:
        store.dispatch(sold(0, "mahomes", 3, 45))
    with DraftStore.resume(run) as store:
        assert store.dispatch(sold(0, "barkley", 1, 62)) is store.state
        assert store.events[-1].id == 3


def test_resume_surfaces_recovery_warnings(tmp_path: Path, init_event):
    run = tmp_path / "run"
    with DraftStore.create(run, init_event) as store:
        store.dispatch(sold(0, "mahomes", 3, 45))
    path = run / "events.jsonl"
    path.write_text(path.read_text() + '{"id": 3, "partial', encoding="utf-8")

    with DraftStore.resume(run) as store:
        assert any("interrupted write" in w for w in store.load_warnings)
        assert len(store.events) == 2


# --- the lock -----------------------------------------------------------------


def test_a_second_writer_is_refused(tmp_path: Path, init_event):
    run = tmp_path / "run"
    with DraftStore.create(run, init_event):
        with pytest.raises(LockError, match="already writing"):
            DraftStore.resume(run)


def test_a_stale_lock_is_reclaimed(tmp_path: Path, init_event):
    run = tmp_path / "run"
    with DraftStore.create(run, init_event) as store:
        store.dispatch(sold(0, "mahomes", 3, 45))

    # A killed session leaves a lock behind pointing at a dead pid.
    (run / LOCK_NAME).write_text("999999")
    with DraftStore.resume(run) as store:
        assert remaining_budget(store.state, 3) == 155


def test_closing_releases_the_lock(tmp_path: Path, init_event):
    run = tmp_path / "run"
    store = DraftStore.create(run, init_event)
    assert (run / LOCK_NAME).read_text() == str(os.getpid())
    store.close()
    assert not (run / LOCK_NAME).exists()
