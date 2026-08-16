"""The append-only event log. This file is the draft.

Chosen over periodic snapshots because provenance merging is order-dependent:
the *sequence* of observations is the truth, and a snapshot is a lossy summary
of it. A 200-pick draft replays in milliseconds, so there is no performance
argument for snapshots — and two write paths that can disagree is precisely the
failure mode that would ruin draft day. The log also gives undo, an audit trail
("why does it think team3 has $61?"), and free regression fixtures.

Durability: every append is followed by `flush()` + `fsync()`, so a complete
line on disk is a durable line. New files additionally get one `fsync` on the
*parent directory* — syncing a file does not make its directory entry durable,
so without it a freshly created journal can vanish entirely on power loss.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from ffa.domain.codec import CodecError, dump_event, load_event
from ffa.domain.events import BaseEvent
from ffa.util.clock import now_utc

JOURNAL_NAME = "events.jsonl"


class JournalError(Exception):
    """The journal is unreadable in a way the user has to know about."""


def read_events(path: Path) -> tuple[list[BaseEvent], list[str]]:
    """Load every event. Returns (events, warnings).

    The asymmetry here is the whole point:

    - A bad **trailing** line means we died mid-write. Complete lines are
      fsynced, so the fragment is definitionally the only casualty. Drop it,
      warn, carry on.
    - A bad **middle** line is real corruption. That raises, because silently
      skipping it would produce a wrong-but-plausible draft state — strictly
      worse than refusing to start, since you would trust the numbers.
    """
    if not path.is_file():
        return [], []

    raw = path.read_bytes()
    if not raw:
        return [], []

    text = raw.decode("utf-8", errors="replace")
    trailing_newline = text.endswith("\n")
    lines = text.splitlines()

    events: list[BaseEvent] = []
    warnings: list[str] = []

    for index, line in enumerate(lines):
        lineno = index + 1
        if not line.strip():
            continue
        is_last = lineno == len(lines)
        try:
            events.append(load_event(line))
        except CodecError as exc:
            if is_last and not trailing_newline:
                warnings.append(
                    f"dropped an incomplete final line in {path} — the previous "
                    f"session was interrupted mid-write ({exc})"
                )
                continue
            raise JournalError(
                f"{path}:{lineno} is corrupt: {exc}\n"
                "This is not a crash artifact — a complete line failed to parse. "
                "The file has been left untouched; inspect it before continuing."
            ) from exc

    _check_ids(events, path)
    return events, warnings


def _check_ids(events: list[BaseEvent], path: Path) -> None:
    """Ids must be strictly increasing. Anything else means two writers."""
    previous = 0
    for event in events:
        if event.id <= previous:
            raise JournalError(
                f"{path}: event id {event.id} follows {previous} — ids must "
                "increase. This usually means two processes wrote to the same "
                "journal; check for a second `ffa draft` session."
            )
        previous = event.id


class Journal:
    """Append-only writer. One per run directory, one writer per journal."""

    def __init__(self, path: Path, next_id: int) -> None:
        self.path = path
        self._next_id = next_id
        self._handle = path.open("a", encoding="utf-8")

    @classmethod
    def open(cls, path: Path, *, next_id: int | None = None) -> "Journal":
        created = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)

        if next_id is None:
            events, _ = read_events(path)
            next_id = (events[-1].id + 1) if events else 1

        journal = cls(path, next_id)
        if created:
            journal._fsync_parent()
        return journal

    @property
    def next_id(self) -> int:
        return self._next_id

    def append(self, event: BaseEvent) -> BaseEvent:
        """Stamp the envelope, write, and make it durable before returning.

        Returns the stamped event — callers need the assigned id to report it
        back to the user (`recorded #7`), since that is what `undo #7` takes.
        """
        stamped = replace(event, id=self._next_id, at=event.at or now_utc())
        if stamped.id <= 0:  # pragma: no cover - defensive
            raise JournalError("refusing to write an event with no id")

        self._handle.write(dump_event(stamped) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

        self._next_id += 1
        return stamped

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def _fsync_parent(self) -> None:
        """Make a newly created journal's directory entry durable too."""
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        except OSError:  # pragma: no cover - not all filesystems allow this
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
