"""`EventSource` wrapper around a simulated auction.

The whole architectural claim of CP4 is that the simulator is *not special*.
It satisfies the same Protocol as `ManualSource` and, later, the ESPN poller;
it puts events on a queue and never touches the store, the journal, or the
state. Nothing downstream can tell which producer it is talking to, and there
is no `if simulating:` anywhere in the engine to make it so.
"""

from __future__ import annotations

import queue
from typing import Iterator

from ffa.domain.events import BaseEvent
from ffa.ingest.source import SourceHealth, SourceStatus
from ffa.sim.engine import AuctionSim


class SimSource:
    """Feeds a simulated draft through the normal ingest seam."""

    name = "sim"

    def __init__(self, sim: AuctionSim) -> None:
        self._sim = sim
        self._health = SourceHealth.OK
        self._emitted = 0

    def start(self, out: "queue.Queue[BaseEvent]") -> None:
        for event in self.events():
            out.put(event)

    def events(self) -> Iterator[BaseEvent]:
        """Lazy, so a consumer can stop early without running the whole draft."""
        for event in self._sim.events():
            if self._health is SourceHealth.STOPPED:
                return
            self._emitted += 1
            yield event

    def stop(self) -> None:
        self._health = SourceHealth.STOPPED

    def status(self) -> SourceStatus:
        return SourceStatus(
            name=self.name,
            health=self._health,
            detail=f"seed={self._sim.seed}, {self._emitted} event(s) emitted",
        )
