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


@runtime_checkable
class EventSource(Protocol):
    """A producer of draft events.

    Implementations put events on the queue and never touch the store, the
    journal, or the state directly. That is the single-writer discipline: the
    main thread stays the only writer, with no locks and no shared mutable
    state.
    """

    name: str

    def start(self, out: "queue.Queue[BaseEvent]") -> None: ...

    def stop(self) -> None: ...

    def status(self) -> SourceStatus: ...
