"""`EventSource` wrapper around typed commands.

CP2's REPL parses inline (see `ingest/source.py` for why), so this exists to
prove the Protocol is satisfiable from the manual path and to give CP5 a
symmetric shape when the ESPN poller becomes a second producer. Keeping it thin
is the point.
"""

from __future__ import annotations

import queue
from typing import Iterable, TextIO

from ffa.domain.events import BaseEvent
from ffa.ingest.manual.grammar import EmitCommand, parse_command
from ffa.ingest.source import SourceHealth, SourceStatus


class ManualSource:
    """Reads command lines and emits the events they describe."""

    name = "manual"

    def __init__(self, stream: TextIO, context_factory) -> None:
        self._stream = stream
        self._context_factory = context_factory
        self._health = SourceHealth.OK

    def start(self, out: "queue.Queue[BaseEvent]") -> None:
        for event in self.events():
            out.put(event)

    def events(self) -> Iterable[BaseEvent]:
        for line in self._stream:
            command = parse_command(line, self._context_factory())
            if isinstance(command, EmitCommand):
                yield from command.events

    def stop(self) -> None:
        self._health = SourceHealth.STOPPED

    def status(self) -> SourceStatus:
        return SourceStatus(name=self.name, health=self._health)
