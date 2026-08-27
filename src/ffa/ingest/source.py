"""The seam that makes every input path interchangeable.

`EspnPoller`, `ManualSource`, `SimSource`, and `EspnReplaySource` all satisfy
this Protocol. Nothing downstream of it — the store, the reducers, the
projections, the advisory layer — knows or cares which one is running.

CP2's REPL deliberately calls the parser inline rather than routing keystrokes
through a queue: a blocking `input()` alongside a queue drain needs a reader
thread, which buys nothing while there is exactly one producer and would ship
threading bugs into the demo. The Protocol is defined now because it is what
forces the architecture; the queue gets wired in CP5, when the ESPN poller
becomes a genuine second producer.
"""

from __future__ import annotations

import queue
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from ffa.domain.events import BaseEvent


class SourceHealth(str, Enum):
    """Whether a producer is currently able to do its job."""

    OK = "OK"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class SourceStatus:
    name: str
    health: SourceHealth = SourceHealth.OK
    detail: str = ""

    @property
    def is_degraded(self) -> bool:
        return self.health is not SourceHealth.OK


@dataclass(frozen=True)
class BidObservation:
    """The price on the player currently up, as last seen. **Not an event.**

    The draft room shows a running bid, and the assistant's tick agent needs it
    to say anything useful mid-auction. But it is observed every two seconds and
    it is not ground truth about anything: the only bid that ever mattered is the
    one that won, and that arrives as a `PlayerSold` like it always did.

    So this deliberately is **not** a `BaseEvent`. It has no id, it is not in
    `EVENT_TYPES`, no reducer folds it, `codec` cannot serialise it, and it never
    reaches `events.jsonl`. That file is fsynced per line and replayed for undo
    and crash-resume; putting tens of thousands of lines of derived observation
    through it would make ground truth expensive to write and slow to replay, in
    exchange for a number that is stale two seconds later.

    It travels on the same queue as events because the sources already have one
    and the main loop already drains it. `_TaggingQueue` routes it by type.
    """

    player_key: str
    price: int | None = None
    holder_team_id: int | None = None
    at: str = ""

    def as_payload(self) -> dict:
        """What the tick agent is shown. Plain dict; no domain types cross over."""
        return {
            "player_key": self.player_key,
            "price": self.price,
            "holder_team_id": self.holder_team_id,
            "at": self.at,
        }


@runtime_checkable
class EventSource(Protocol):
    """A producer of draft events.

    Implementations put events on the queue and never touch the store, the
    journal, or the state directly. That is the single-writer discipline: the
    main thread stays the only writer, with no locks and no shared mutable
    state.

    A source may also put a `BidObservation`, which is not an event and never
    reaches the journal — see that class. The queue type below still says
    `BaseEvent` because that is what a source is *for*; the observation is a
    side channel that the main loop routes by type and the store never sees.
    """

    name: str

    def start(self, out: "queue.Queue[BaseEvent]") -> None: ...

    def stop(self) -> None: ...

    def status(self) -> SourceStatus: ...
