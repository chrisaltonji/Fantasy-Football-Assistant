from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ffa.domain.events import EventUndone
from ffa.domain.projections import remaining_budget
from ffa.domain.reducers import replay
from ffa.state.journal import read_events
from ffa.state.store import LOCK_NAME, DraftStore, LockError, _pid_alive
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


# --- pid liveness: platform-specific and easy to get subtly wrong ------------


def test_a_dead_pid_reads_as_dead_on_every_platform():
    """The stale-lock path depends entirely on this returning False.

    POSIX raises ProcessLookupError for a missing pid; Windows raises a plain
    OSError (WinError 87). Only the first was handled, so on Windows a crashed
    session left a lock that could never be reclaimed — the run directory
    stayed locked until the file was deleted by hand.
    """
    assert _pid_alive("999999") is False


def test_our_own_pid_reads_as_alive():
    assert _pid_alive(str(os.getpid())) is True


@pytest.mark.parametrize("raw", ["", "nonsense", "-1", "0", "1.5"])
def test_unusable_pid_values_read_as_dead(raw):
    """A truncated or garbage lockfile must not wedge the next session."""
    assert _pid_alive(raw) is False


def test_probing_a_live_process_does_not_disturb_it(tmp_path: Path):
    """`os.kill(pid, 0)` must stay a probe, never a signal.

    Windows implements `os.kill` as OpenProcess + TerminateProcess for real
    signals and special-cases 0. If anyone ever "simplifies" this to a real
    signal, the lock check would terminate the very session it was asked to
    detect — so pin it.

    The child announces readiness through a file rather than a sleep. Probing
    a process that is still loading its DLLs races with Windows process
    startup, and a child that dies with STATUS_DLL_INIT_FAILED looks exactly
    like a probe that killed it.
    """
    ready = tmp_path / "ready"
    child = subprocess.Popen([
        sys.executable, "-c",
        f"import pathlib,time; pathlib.Path(r'{ready}').touch(); time.sleep(30)",
    ])
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and child.poll() is None:
            if time.monotonic() > deadline:
                pytest.fail("child never signalled readiness")
            time.sleep(0.05)

        if child.poll() is not None:
            pytest.skip(f"child failed to start (exit {child.returncode})")

        assert _pid_alive(str(child.pid)) is True
        time.sleep(0.3)
        assert child.poll() is None, "the liveness probe terminated the process"
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)

    # Deliberately not asserting the pid reads dead here. Windows keeps a pid
    # valid while any handle to it remains open, and Popen holds one for the
    # lifetime of this object — so a just-reaped child can still probe as
    # alive. That is handle lifetime, not the probe's contract, and it does not
    # arise for the case that matters (a crashed session with no parent left to
    # hold a handle). `test_a_dead_pid_reads_as_dead_on_every_platform` covers
    # the real path.
