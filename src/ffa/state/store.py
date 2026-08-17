"""The single writer.

Every mutation goes through `dispatch`. Producers — the manual REPL now, the
ESPN poller and the simulator later — only ever hand events to this object;
they never touch the journal or the state themselves. That is what makes the
concurrency story structural rather than a locking exercise.

The store knows nothing about ESPN, about rankings, or about how a command was
typed. It takes events and keeps the journal and the state in agreement.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from ffa.domain.events import BaseEvent, DraftInitialized, EventUndone
from ffa.domain.models import DraftState
from ffa.domain.reducers import apply, replay
from ffa.state.journal import JOURNAL_NAME, Journal
from ffa.state.recovery import load_run

LOCK_NAME = ".lock"

Subscriber = Callable[[DraftState, BaseEvent], None]


class LockError(Exception):
    """Another process is already writing this draft."""


class DraftStore:
    def __init__(self, run_dir: Path, journal: Journal, events: list[BaseEvent],
                 state: DraftState, warnings: list[str]) -> None:
        self.run_dir = run_dir
        self._journal = journal
        self._events = events
        self._state = state
        self.load_warnings = warnings
        self._subscribers: list[Subscriber] = []

    # --- construction --------------------------------------------------------

    @classmethod
    def create(cls, run_dir: Path, init_event: DraftInitialized) -> "DraftStore":
        run_dir.mkdir(parents=True, exist_ok=True)
        _acquire_lock(run_dir)
        journal = Journal.open(run_dir / JOURNAL_NAME)
        store = cls(run_dir, journal, [], DraftState.empty(init_event.draft_id), [])
        store.dispatch(init_event)
        return store

    @classmethod
    def resume(cls, run_dir: Path) -> "DraftStore":
        _acquire_lock(run_dir)
        events, warnings = load_run(run_dir)
        draft_id = next(
            (e.draft_id for e in events if isinstance(e, DraftInitialized)), run_dir.name
        )
        state = replay(events, draft_id)
        journal = Journal.open(
            run_dir / JOURNAL_NAME, next_id=(events[-1].id + 1) if events else 1
        )

        unknown = sum(1 for w in state.warnings if "unknown type" in w)
        if unknown:
            warnings.append(
                f"{unknown} event(s) of an unrecognized type were skipped — this "
                "journal was written by a newer version of ffa, so your state is "
                "incomplete."
            )
        return cls(run_dir, journal, events, state, warnings)

    # --- the only mutation path ---------------------------------------------

    def dispatch(self, event: BaseEvent) -> DraftState:
        """Append, fold in, notify. Returns the new state.

        Normal events fold in incrementally, which is O(1). Undo triggers a
        full replay, because un-applying a merge is impossible — but undo is
        rare and a replay of a few hundred events is imperceptible. A test
        pins that the two paths always agree.
        """
        stamped = self._journal.append(event)
        self._events.append(stamped)

        if isinstance(stamped, EventUndone):
            self._state = replay(self._events, self._state.draft_id)
        else:
            self._state = apply(self._state, stamped)

        for subscriber in self._subscribers:
            subscriber(self._state, stamped)
        return self._state

    # --- reads ----------------------------------------------------------------

    @property
    def state(self) -> DraftState:
        return self._state

    @property
    def events(self) -> tuple[BaseEvent, ...]:
        return tuple(self._events)

    @property
    def journal_path(self) -> Path:
        return self._journal.path

    def subscribe(self, callback: Subscriber) -> None:
        """Register a read-only observer. The seam a dashboard feed hangs off."""
        self._subscribers.append(callback)

    def close(self) -> None:
        self._journal.close()
        _release_lock(self.run_dir)

    def __enter__(self) -> "DraftStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# --- lockfile ----------------------------------------------------------------
#
# "I opened a second terminal" is a plausible draft-day mistake, and two writers
# interleaving into one JSONL produces a journal that cannot be replayed.


def _acquire_lock(run_dir: Path) -> None:
    lock = run_dir / LOCK_NAME
    run_dir.mkdir(parents=True, exist_ok=True)

    if lock.is_file():
        holder = lock.read_text(encoding="utf-8").strip()
        if _pid_alive(holder):
            raise LockError(
                f"another ffa session (pid {holder}) is already writing "
                f"{run_dir}.\nClose it first, or delete {lock} if you are sure "
                "that process is gone."
            )
        # Stale lock from a killed session — the journal is already durable, so
        # reclaiming it is safe and beats making the user hunt for the file.
    lock.write_text(str(os.getpid()), encoding="utf-8")


def _release_lock(run_dir: Path) -> None:
    lock = run_dir / LOCK_NAME
    try:
        if lock.is_file() and lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock.unlink()
    except OSError:  # pragma: no cover - best effort
        pass


def _pid_alive(raw: str) -> bool:
    try:
        pid = int(raw)
    except ValueError:
        return False
    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists, owned by someone else
        return True
    except OSError:
        # Windows reports a nonexistent pid as OSError(EINVAL / WinError 87),
        # not ProcessLookupError. Uncaught, that escaped `_acquire_lock` and
        # meant a stale lock could never be reclaimed here: after any crash the
        # run directory stayed locked until the file was deleted by hand, mid
        # draft. Verified on 3.14 that signal 0 is a pure probe on Windows and
        # does not terminate the target.
        return False
    return True
