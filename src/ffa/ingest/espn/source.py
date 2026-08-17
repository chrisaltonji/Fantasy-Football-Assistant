"""Turn draft-room snapshots into events, through the normal ingest seam.

Satisfies the same `EventSource` Protocol as `ManualSource` and `SimSource`.
Nothing downstream — the store, the reducers, the projections, the advisory
layer — knows the events came from a browser rather than an API.

The diffing rule is deliberately the same one the ESPN REST adapter would have
used: **a pick is news when its player first appears**, not when the cell count
changes. ESPN pre-creates the whole board, so cell count never moves.
"""

from __future__ import annotations

import queue
from typing import Iterator

from ffa.domain.enums import Position, Provenance
from ffa.domain.events import BaseEvent, PlayerSold
from ffa.domain.ids import PlayerRef
from ffa.domain.sourced import known, unknown
from ffa.ingest.espn.draftroom import DraftRoomReader, RoomPick, RoomSnapshot
from ffa.ingest.source import SourceHealth, SourceStatus
from ffa.util.clock import now_utc


def _position(raw: str | None) -> Position | None:
    if not raw:
        return None
    try:
        return Position.parse(raw)
    except Exception:  # noqa: BLE001 - an unknown position is not fatal
        return None


def diff_picks(
    previous: RoomSnapshot | None, current: RoomSnapshot
) -> tuple[RoomPick, ...]:
    """Picks present now that were not present before.

    Keyed on the normalized player key rather than the grid index: a board that
    re-renders or reorders must not replay every pick as new.
    """
    seen = {p.key for p in previous.picks} if previous else set()
    return tuple(p for p in current.picks if p.key not in seen)


def events_for(pick: RoomPick, snapshot: RoomSnapshot) -> BaseEvent:
    """One completed cell becomes one `PlayerSold`.

    A missing price is recorded as an explicit unknown rather than skipped or
    zeroed. The board can render a cell before its price settles, and `merge`
    is built to let the real number land later — whereas a zero would be a
    known value and would win.
    """
    at = now_utc()
    price = (
        known(pick.price, Provenance.ESPN_API, at)
        if pick.price is not None
        else unknown(at, note="draft room showed no winning price")
    )
    position = _position(pick.position)
    return PlayerSold(
        at=at,
        source=Provenance.ESPN_API.value,
        player=PlayerRef.from_raw(pick.player),
        # team_index is 0-based across the board; team ids are 1-based and may
        # be non-contiguous, so this is resolved against the snapshot's own
        # ordering rather than assumed to equal the ESPN team id.
        team=known(pick.team_index + 1, Provenance.ESPN_API, at),
        price=price,
        position=known(position, Provenance.ESPN_API, at) if position else None,
    )


class DraftRoomSource:
    """Polls the draft room and emits what changed."""

    name = "espn-draftroom"

    def __init__(self, reader: DraftRoomReader, *, interval: float = 2.0,
                 max_failures: int = 10) -> None:
        self._reader = reader
        self._interval = interval
        self._max_failures = max_failures
        self._failures = 0
        self._previous: RoomSnapshot | None = None
        self._health = SourceHealth.OK
        self._detail = ""
        self._emitted = 0

    def start(self, out: "queue.Queue[BaseEvent]") -> None:
        for event in self.events():
            out.put(event)

    def events(self) -> Iterator[BaseEvent]:
        """Yield events until stopped.

        A failed snapshot degrades rather than raising: on draft day a blip —
        a re-render, a tab briefly backgrounded — must not take the tool down.
        The status goes DEGRADED so the surface can say so, and polling
        continues.

        But it does not retry forever. A reader that fails `max_failures` times
        running is not blipping, it is gone — the tab was closed, or Chrome
        exited — and a loop that never yields would hang the caller and hide
        the fact. It stops and says which.
        """
        import time

        while self._health is not SourceHealth.STOPPED:
            try:
                snapshot = self._reader.snapshot()
                self._health = SourceHealth.OK
                self._failures = 0
                self._detail = f"{snapshot.filled} pick(s) on the board"
            except Exception as exc:  # noqa: BLE001 - degrade, never die
                self._failures += 1
                self._detail = (
                    f"snapshot failed ({self._failures}/{self._max_failures}): "
                    f"{type(exc).__name__}: {exc}"
                )
                if self._failures >= self._max_failures:
                    self._health = SourceHealth.STOPPED
                    self._detail = (
                        f"giving up after {self._failures} consecutive failures — "
                        f"is the draft room tab still open? Last error: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    return
                self._health = SourceHealth.DEGRADED
                time.sleep(self._interval)
                continue

            for pick in diff_picks(self._previous, snapshot):
                self._emitted += 1
                yield events_for(pick, snapshot)
            self._previous = snapshot

            if self._health is SourceHealth.STOPPED:
                return
            time.sleep(self._interval)

    def stop(self) -> None:
        self._health = SourceHealth.STOPPED

    def status(self) -> SourceStatus:
        return SourceStatus(name=self.name, health=self._health, detail=self._detail)
