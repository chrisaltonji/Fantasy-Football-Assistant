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

from ffa.domain.events import BaseEvent, PlayerNominated, PlayerSold
from ffa.ingest.source import BidObservation, SourceHealth, SourceStatus
from ffa.sim.engine import AuctionSim

# How many observations to walk up a contested price. Four is enough to exercise
# the tick's every branch — a first read with nothing to compare against, two
# that may or may not be news, and one that crosses a ceiling — without pretending
# to model a real auction's rhythm, which this is not trying to do.
BID_STEPS = 4


class SimSource:
    """Feeds a simulated draft through the normal ingest seam."""

    name = "sim"

    def __init__(self, sim: AuctionSim, *, bids: bool = False) -> None:
        self._sim = sim
        self._health = SourceHealth.OK
        self._emitted = 0
        # Off by default. A `BidObservation` is not an event, and a source that
        # produced them unasked would change what every existing sim test sees
        # on the queue for the benefit of the one caller that wants them.
        self._bids = bids

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

    def events_with_bids(self) -> Iterator[object]:
        """The same stream, with a synthetic price ladder between nominate and sell.

        **The sim resolves each auction internally and yields only the outcome**,
        which is right — `AuctionSim` models who wins for how much, not the
        theatre of getting there. But it means the tick agent, whose entire
        subject is a price moving while a player is up, has nothing to read in a
        simulated draft.

        So the ladder is manufactured here rather than in the engine. It is one
        event of lookahead: hold the nomination, see the sale that follows, and
        walk the price up to it. That keeps the fiction where it belongs — in the
        source, clearly named, off by default — instead of putting a notion of
        intermediate bids into a simulator whose correctness argument is that it
        does not have one.

        These are **not** events and are not counted in `_emitted`. Nothing folds
        them and they never reach the journal.
        """
        pending: BaseEvent | None = None

        for event in self.events():
            if pending is None:
                pending = event
                continue

            yield pending
            if isinstance(pending, PlayerNominated) and isinstance(event, PlayerSold):
                yield from self._ladder(pending, event)
            pending = event

            if self._health is SourceHealth.STOPPED:
                return

        if pending is not None:
            yield pending

    def _ladder(self, nomination: PlayerNominated,
                sale: PlayerSold) -> Iterator[BidObservation]:
        """Observations climbing from the opening bid to the price that won.

        Stops one step short of the sale price. The winning number arrives as
        `PlayerSold` like it always did, and emitting it twice — once as an
        observation and once as ground truth — is exactly the blur this whole
        split exists to prevent.
        """
        price = getattr(sale.price, "value", None)
        if not isinstance(price, int) or price < 2:
            return

        opening = getattr(nomination.opening_bid, "value", None) or 1
        if price - opening < 2:
            return

        key = nomination.player.key
        step = (price - opening) / BID_STEPS
        seen: set[int] = set()
        for i in range(1, BID_STEPS):
            at = int(opening + step * i)
            if at <= opening or at >= price or at in seen:
                continue
            seen.add(at)
            yield BidObservation(player_key=key, price=at, at=sale.at.isoformat())

    def stop(self) -> None:
        self._health = SourceHealth.STOPPED

    def status(self) -> SourceStatus:
        return SourceStatus(
            name=self.name,
            health=self._health,
            detail=f"seed={self._sim.seed}, {self._emitted} event(s) emitted",
        )
