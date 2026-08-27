"""Where inference is allowed to be slow, and the draft is not.

The rule this module exists to enforce: **the deterministic readout prints
first, unchanged, and nothing here can delay it.** A nomination lands, the bid
ceiling and threat list are on screen in milliseconds because they are
arithmetic over the journal, and a read from a model arrives seconds later
through `console.notify()` — which is exactly what `RawConsole` was built to
make safe over a half-typed command. If the API is slow, or down, or the key is
wrong, the only difference is that nothing extra ever appears.

**Plain daemon threads, not a `ThreadPoolExecutor`.** Its workers are non-daemon
and joined at interpreter exit, so a 30-second request in flight would hold up
`quit` and swallow a Ctrl-C. On draft night that is unforgivable: the one thing
a person must always be able to do is stop the tool. Daemon threads are killed
outright, and every result they would have produced is discardable by
construction.

**One writer, still.** Workers never touch `DraftStore`, the journal, or the
`ReadLog`. They are handed plain dicts at spawn time and post their result back
onto the same inbox the keyboard and the ESPN reader use, tagged `ASSIST`. The
main thread does the recording. That is the identical argument that makes the
rest of the loop lock-free, and it is why there is no mutex in this file.

**Superseded reads are recorded, not printed.** The auction moves; a read about
a player who sold ninety seconds ago is noise on screen and evidence in the
ledger. The Grader wants "we said $39, he went $47" — so it is written down with
`status="late"` and never shown.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from ffa.assist.budget import SpendGuard, cost_cents
from ffa.assist.context import ReadRecord
from ffa.assist.errors import AssistError

# The tag this module posts under, beside LINE / EVENT / EOF.
ASSIST = "assist"

# Two at once. The Room and a Narrator overlapping is normal; four in flight
# means the API is slow and piling on more will not make any of them land while
# the player is still on the block.
MAX_IN_FLIGHT = 2

# **The tick gets its own slot, and this is not a tuning number.**
#
# Sharing the pool above would have starved it outright: a 25s Room read plus one
# Strategist holds both slots for most of a nomination, which is exactly the
# window the tick exists to cover. It would have fired, found no slot, dropped,
# and the failure would have looked like the API being slow rather than like the
# design being wrong.
#
# One, not two, because a second concurrent tick can only describe a board the
# first one is already describing. When one is in flight the next is skipped —
# same argument as `submit`'s refusal to queue: a tick that waits for a slot
# lands describing a price nobody is bidding any more.
TICK_IN_FLIGHT = 1

# Which agents draw on the tick slot rather than the shared pool.
TICK_AGENTS = frozenset({"room_tick"})

# Consecutive failures before muting. Three is enough to ride out a blip and few
# enough that a genuinely broken setup stops costing time.
FAILURE_LIMIT = 3


@dataclass
class AssistOutcome:
    """One finished call, on its way back to the main thread.

    Carries a `ReadRecord` rather than a reply because the worker has already
    done everything that does not need the main thread: called, parsed, guarded.
    What is left is recording and printing, both of which must be single-threaded.
    """

    record: ReadRecord
    generation: int
    text: str = ""              # what to show, or "" for nothing
    note: str = ""              # an operational line: muted, rate limited
    violations: list[str] = field(default_factory=list)


class AssistRunner:
    """Spawns agent calls, and guarantees they can only ever add to the screen.

    `complete` is any callable matching `ClaudeClient.complete`, which is the
    seam the whole package is tested through — the suite drives this class end
    to end with a fake that never touches a network.
    """

    def __init__(self, complete: Callable[..., Any], *, inbox: "queue.Queue",
                 prefix: str, spend: SpendGuard | None = None,
                 max_in_flight: int = MAX_IN_FLIGHT) -> None:
        self._complete = complete
        self._inbox = inbox
        self._prefix = prefix
        self.spend = spend or SpendGuard()
        self._slots = threading.Semaphore(max_in_flight)
        self._tick_slots = threading.Semaphore(TICK_IN_FLIGHT)
        # How many workers are alive. The only counter here that more than one
        # thread touches, so it is the only thing in this file with a lock.
        # It exists for the simulator, which has no REPL loop to drain results
        # for it and needs to know when there is nothing more coming.
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()

        # Bumped on every nomination. A result whose generation is stale
        # describes a player who is no longer on the block.
        self._generation = 0
        self.muted_reason = ""
        self._consecutive_failures = 0
        self._counts: dict[str, int] = {}

    # --- state the main thread owns ----------------------------------------

    @property
    def in_flight(self) -> int:
        """Workers still running. A live draft never asks; the simulator does."""
        with self._in_flight_lock:
            return self._in_flight

    @property
    def inbox(self) -> "queue.Queue":
        """The queue results are posted onto.

        Public because the simulator drains it directly — it has no REPL loop to
        do that for it, and reaching through `_inbox` from `cli/app.py` was a
        hole in the one seam this class exists to define.

        A live draft must never read this: the REPL owns the drain, and a second
        consumer would take results the loop needed.
        """
        return self._inbox

    @property
    def muted(self) -> bool:
        return bool(self.muted_reason)

    @property
    def generation(self) -> int:
        return self._generation

    def advance(self) -> int:
        """A new player is on the block; everything in flight is now stale."""
        self._generation += 1
        return self._generation

    def mute(self, reason: str) -> None:
        """Stop, once, with a line saying why. Never silently."""
        if not self.muted_reason:
            self.muted_reason = reason

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    # --- spawning -----------------------------------------------------------

    def submit(self, agent: str, payload: dict[str, Any], *,
               moment: str, event_id: int = 0, player_key: str = "",
               schema: dict[str, Any] | None = None,
               instructions: str | None = None,
               check: Callable[[dict], tuple[dict, list[str]]] | None = None,
               show: Callable[[dict], str] | None = None) -> bool:
        """Start one call on its own thread. Returns whether it was started.

        Every argument is a plain value or a pure function, captured now. The
        worker never reads live state, so nothing it touches can change under it
        while the draft moves on.
        """
        if self.muted:
            return False
        if self.spend.exceeded:
            self.mute(self.spend.reason())
            return False
        slots = self._tick_slots if agent in TICK_AGENTS else self._slots
        if not slots.acquire(blocking=False):
            # Deliberately not queued. A call that has to wait for a slot will
            # land after the player sold, and an unbounded backlog is how a slow
            # API turns into a bill for reads nobody ever saw.
            return False

        generation = self._generation
        with self._in_flight_lock:
            self._in_flight += 1
        thread = threading.Thread(
            target=self._work,
            args=(agent, payload, moment, event_id, player_key, schema,
                  instructions, check, show, generation, slots),
            daemon=True,
            name=f"assist-{agent}-{generation}",
        )
        thread.start()
        return True

    # --- the worker ---------------------------------------------------------

    def _work(self, agent, payload, moment, event_id, player_key, schema,
              instructions, check, show, generation, slots) -> None:
        """Runs off the main thread. Must never raise, and never touch the store."""
        try:
            outcome = self._call(agent, payload, moment, event_id, player_key,
                                 schema, instructions, check, show, generation)
        except BaseException as exc:  # noqa: BLE001 - a worker must not die loudly
            # Nothing above this catches, so this is the last line of defence.
            # A traceback on a daemon thread would print straight over a live
            # auction and there is no reading it back.
            outcome = AssistOutcome(
                record=ReadRecord(agent=agent, moment=moment, event_id=event_id,
                                  player_key=player_key, status="failed",
                                  detail=f"{type(exc).__name__}: {exc}"),
                generation=generation,
            )
        finally:
            # The one it took, not `self._slots` — releasing the wrong semaphore
            # would grow the shared pool by one every tick.
            slots.release()
        # Decremented *before* the post, so a consumer that drains until
        # `in_flight` is zero cannot see zero while a result is still in the air.
        with self._in_flight_lock:
            self._in_flight -= 1
        self._inbox.put((ASSIST, outcome))

    def _call(self, agent, payload, moment, event_id, player_key, schema,
              instructions, check, show, generation) -> AssistOutcome:
        from ffa.assist.prompts import system_blocks, user_text

        system = system_blocks(self._prefix, agent)
        if instructions:
            system = [system[0], {"type": "text", "text": instructions}]

        def record(status: str, **kw) -> ReadRecord:
            return ReadRecord(agent=agent, moment=moment, event_id=event_id,
                              player_key=player_key, status=status, **kw)

        try:
            reply = self._complete(agent, system, user_text(payload),
                                   schema=schema)
        except AssistError as exc:
            return AssistOutcome(
                record=record("failed", detail=exc.message),
                generation=generation,
                note=exc.message if exc.fatal_to_assist else "",
            )

        usage = dict(reply.usage or {})
        usage["cents"] = cost_cents(reply.usage or {}, reply.model)

        parsed = dict(reply.payload or {})
        violations: list[str] = []
        if check is not None:
            parsed, violations = check(parsed)

        # A reply that broke the one rule is kept and not shown. Discarding it
        # would hide the fact that it happened, and how often it happens is the
        # thing you would want to know before trusting any of it.
        if violations and not parsed:
            return AssistOutcome(
                record=record("rejected", payload=dict(reply.payload or {}),
                              detail="; ".join(violations), usage=usage,
                              model=reply.model),
                generation=generation, violations=violations,
            )

        return AssistOutcome(
            record=record("ok", payload=parsed, usage=usage, model=reply.model,
                          detail="; ".join(violations)),
            generation=generation,
            text=show(parsed) if show else "",
            violations=violations,
        )

    # --- the main thread's half ---------------------------------------------

    def settle(self, outcome: AssistOutcome, *, log=None) -> str:
        """Record a finished call and return what to print. Main thread only.

        The whole point of the split: everything above ran on a worker, and
        every mutation lands here, on the one thread that owns the ledger.
        """
        record = outcome.record
        usage = record.usage or {}
        self.spend.record(int(usage.get("cents", 0) or 0))

        if record.status == "ok":
            self._consecutive_failures = 0
        elif record.status == "failed":
            self._consecutive_failures += 1
            if outcome.note:
                self.mute(outcome.note)
            elif self._consecutive_failures >= FAILURE_LIMIT:
                self.mute(
                    f"assistant muted after {FAILURE_LIMIT} failures in a row "
                    f"({record.detail}). Everything on screen is arithmetic and "
                    "is unaffected."
                )

        stale = outcome.generation != self._generation
        if stale and record.status == "ok":
            # Recorded as evidence, never shown. See the module docstring.
            record = replace(record, status="late")

        self._counts[record.status] = self._counts.get(record.status, 0) + 1
        if log is not None:
            log.append(record)

        if self.spend.exceeded:
            self.mute(self.spend.reason())

        if record.status != "ok" or stale:
            return ""
        return outcome.text
