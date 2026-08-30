"""The draft loop.

Reads from any `TextIO`, not just stdin. That is deliberate: it means an
annotated session transcript can be piped in, which is how the integration
tests drive a complete draft with no pty tricks — and it's also how you'd
replay a session to reproduce a bug.

Nothing is buffered. Every event is fsynced before the next prompt appears, so
Ctrl-C, a closed laptop, or a kernel panic all lose exactly nothing.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, TextIO

from ffa.advice.engine import advise
from ffa.advice.market import market_state
from ffa.advice.nomination import nomination_plan
from ffa.advice.strategy import strategy_read
from ffa.cli import render
from ffa.cli.console import make_console
from ffa.domain.events import PlayerNominated, PlayerSold
from ffa.domain.models import DraftState
from ffa.ingest.manual.errors import CommandError
from ffa.ingest.manual.resolve import resolve_player
from ffa.ingest.manual.grammar import (
    EmitCommand,
    NoopCommand,
    ParseContext,
    QuitCommand,
    ViewCommand,
    parse_command,
)
from ffa.ingest.source import BidObservation, EventSource, SourceHealth
from ffa.state.store import DraftStore
from ffa.util.clock import now_utc

PROMPT = "> "

# Tags on the single inbound queue. Both producers - your keyboard and the
# ESPN reader - post here, and the main thread is the only thing that ever
# writes to the store. That is the whole concurrency story: no locks, no
# shared mutable state, one writer by construction.
LINE = "line"
EVENT = "event"
EOF = "eof"
# A price move on the player already up. **Not an event** — nothing dispatches
# it, nothing folds it, and it never reaches the journal. See `BidObservation`.
BID = "bid"
# The clock the tick agent runs on. Posted by `_run_ticker`, and the only tag
# whose payload is always `None`: it says "time has passed", nothing more.
TICK = "tick"

# **Five seconds, and it is a cadence rather than a deadline.** A tick that
# cannot be answered in time is skipped by the runner rather than queued, so
# this is how often the assistant is *offered* the chance to speak, not how
# often it does. Most offers are declined — see `room.render_tick`.
TICK_SECONDS = 5.0
# Posted by assist workers. Imported rather than redefined so there is one
# spelling of it — a second constant that drifted would silently route every
# read into the "you typed something" branch.
try:                                    # pragma: no cover - import shape only
    from ffa.assist.runner import ASSIST
except Exception:                       # noqa: BLE001 - assist is optional
    ASSIST = "assist"


class _TaggingQueue(queue.Queue):
    """Looks like a plain queue to an `EventSource`, tags on the way through.

    `EventSource.start(out)` puts bare events. Wrapping the queue rather than
    adapting afterwards keeps the Protocol honest — the source has no idea it
    is being multiplexed, which is what lets `ManualSource`, `SimSource` and
    `DraftRoomSource` all drop into the same loop.
    """

    def __init__(self, target: "queue.Queue", tag: str) -> None:
        super().__init__()
        self._target = target
        self._tag = tag

    def put(self, item, block=True, timeout=None):  # noqa: D102 - stdlib signature
        # Routed by type, here, rather than by giving sources a second queue or
        # a second method. A `BidObservation` is not an event and must never be
        # dispatched to the store, and the one place that can be guaranteed is
        # the one place everything a source produces passes through.
        tag = BID if isinstance(item, BidObservation) else self._tag
        self._target.put((tag, item), block=block, timeout=timeout)


class _Bidding:
    """What is happening on the player currently up. **Main thread only.**

    Three things the tick agent needs and the journal deliberately does not hold:
    the live price, the open read it is revising, and the last tick that actually
    reached the screen.

    It is a plain mutable object with no lock because exactly one thread touches
    it — the same argument that makes `DraftStore` safe. Workers are handed
    frozen copies at spawn time and never read it. If that ever stops being true
    this needs a lock, and the fix would be to stop doing that instead.

    Reset wholesale on every nomination. Carrying any of it across players is how
    a read about one auction ends up printed under another.
    """

    __slots__ = ("player_key", "price", "holder_team_id", "opening_read",
                 "last_tick", "event_id", "crossed")

    def __init__(self) -> None:
        self.reset()

    def reset(self, player_key: str = "", event_id: int = 0) -> None:
        self.player_key = player_key
        self.event_id = event_id
        self.price: int | None = None
        self.holder_team_id: int | None = None
        # What the open read said. Filled in when that read lands, which may be
        # a dozen seconds after the first tick has already fired — an empty one
        # is normal early and the prompt is written to cope.
        self.opening_read: dict | None = None
        # The last tick whose text was actually shown, which is what the next
        # one is suppressed against. A tick that was rendered as "" never
        # becomes the thing to compare with; otherwise silence would make the
        # next genuine change look like a repeat.
        self.last_tick: dict | None = None
        # Which of our ceilings the price has already been reported as passing.
        # Without this, `crossing` keeps answering for every tick while the price
        # stays above the number, and since a crossing overrides the repeat
        # guard, one auction printed the same sentence three times.
        self.crossed: str = ""

    def newly_crossed(self, now: str) -> str:
        """The crossing to announce, or "" if it has already been announced.

        Latching, not comparing: once a price is past the plan cap it is past,
        and a bid that dips back under and climbs again has not discovered
        anything new about the same number.
        """
        if not now or now == self.crossed:
            return ""
        self.crossed = now
        return now

    def observe(self, observation) -> bool:
        """Fold in one price move. Returns whether it was about this player."""
        if observation.player_key != self.player_key:
            return False
        self.price = observation.price
        if observation.holder_team_id is not None:
            self.holder_team_id = observation.holder_team_id
        return True

    def as_payload(self) -> dict:
        return {"price": self.price, "holder_team_id": self.holder_team_id}

    @property
    def live(self) -> bool:
        return bool(self.player_key)


def _run_ticker(inbox: "queue.Queue", stop: "threading.Event") -> None:
    """Post a `TICK` every few seconds until the draft ends.

    A thread rather than a timeout on `inbox.get()`, deliberately. The main loop
    blocks on a bare `get()` and that is load-bearing: it is what makes the
    console's half-typed-line handling correct and what keeps the loop from
    spinning. Adding a timeout would turn every idle moment into a wakeup and
    put the redraw logic on a path it was never designed for.

    It ticks unconditionally, including with nothing on the block. Deciding
    whether there is anything to ask about is the main thread's job, on the
    thread that can actually see the store — a ticker that read state to decide
    whether to fire would be a second reader of the thing only one thread owns.
    """
    while not stop.wait(TICK_SECONDS):
        inbox.put((TICK, None))


def _pump_stdin(console, out: "queue.Queue") -> None:
    """Move blocking reads off the main thread.

    Without this the loop sits in `readline()` and a pick that lands while you
    are not typing would not appear until you pressed Enter — which is exactly
    when you are least able to press it.

    Reads through the console rather than the raw stream: on a terminal that is
    what keeps a half-typed command intact when a pick prints over it.
    """
    try:
        while True:
            line = console.readline()
            if line == "":
                out.put((EOF, None))
                return
            out.put((LINE, line))
    except KeyboardInterrupt:
        # Ctrl-C arrives here rather than on the main thread, because that is
        # where the keys are read. Say the same thing the manual loop says: a
        # draft that exits without a word looks like a crash.
        console.notify("stopped. everything is saved.")
        out.put((EOF, None))
    except Exception:  # noqa: BLE001 - a dead stdin is an EOF, not a crash
        out.put((EOF, None))


def run_repl(
    store: DraftStore,
    *,
    stdin: TextIO,
    stdout: TextIO,
    now_fn: Callable[[], object] = now_utc,
    prompt: str = PROMPT,
    book=None,
    source: EventSource | None = None,
    precedent=None,
    seats=None,
    strategy=None,
    assist_factory: Callable[[Any], Any] | None = None,
) -> int:
    """Drive a draft until quit, EOF, or Ctrl-C. Returns an exit code.

    With no `source` this is the original loop, unchanged: a blocking read on
    the calling thread. That is what every integration test drives, and what a
    piped transcript needs.

    With a `source`, the same loop runs off a queue that both your keyboard and
    the source post to. Picks land on their own and you type only to correct
    them.
    """
    # `--assist` needs the queue loop even with no source: the manual fallback
    # — the one you are on when the browser attach dies at pick 40 — is exactly
    # when you would least like to lose the assistant too.
    if source is not None or assist_factory is not None:
        return _run_live(store, stdin=stdin, stdout=stdout, now_fn=now_fn,
                         prompt=prompt, book=book, source=source,
                         precedent=precedent, seats=seats, strategy=strategy,
                         assist_factory=assist_factory)
    emit = _writer(stdout)

    if store.load_warnings:
        emit(render.render_warnings(store.load_warnings))
    emit(_banner(store.state))
    emit(_reference_banner(book))

    seen_warnings = len(store.state.warnings)

    while True:
        stdout.write(prompt)
        stdout.flush()
        try:
            line = stdin.readline()
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            emit("\nstopped. everything is saved.")
            return 0

        if line == "":  # EOF
            emit("")
            return 0

        outcome = _handle_line(
            line, store, emit, book, now_fn, seen_warnings, precedent, seats,
            strategy,
        )
        if outcome.exit_code is not None:
            return outcome.exit_code
        seen_warnings = outcome.seen_warnings


def _emit_new_warnings(emit, state: DraftState, seen: int) -> int:
    if len(state.warnings) > seen:
        emit(render.render_warnings(state.warnings[seen:]))
    return len(state.warnings)


def _view(store: DraftStore, command: ViewCommand, book=None,
          precedent=None, seats=None, strategy=None) -> str:
    state = store.state
    if command.kind == "plan":
        # Like `turns`, ahead of the reference-data gate: adherence is your
        # dollars against your own declared numbers, and needs no sheet.
        return render.render_plan(strategy_read(state, book, strategy))

    if command.kind == "turns":
        # Ahead of the reference-data gate on purpose. Whose turn it is needs no
        # auction values at all — only the candidate lists do, and they simply
        # come back empty. Refusing the whole readout for want of a sheet would
        # withhold the half that always works.
        market = market_state(state, book) if (book is not None and len(book)) else None
        return render.render_nomination_plan(nomination_plan(state, book, market))

    if command.kind in ("advice", "scarcity", "market", "watch"):
        if book is None or not len(book):
            return (
                "no reference data loaded.\n"
                "Set [reference].path in your config to your exported auction "
                "values, then restart."
            )
        if command.kind == "scarcity":
            return render.render_scarcity(advise(state, book).scarcity)
        if command.kind == "market":
            return render.render_market(advise(state, book).market)
        if command.kind == "watch":
            from ffa.advice.watchlist import squeezes
            from ffa.advice.market import market_state as _mkt

            return render.render_watchlist(squeezes(state, book, _mkt(state, book)))

        key = None
        if command.arg:
            try:
                key = resolve_player(command.arg, book).key
            except CommandError as exc:
                return f"error: {exc}"
        elif state.current_nomination is None:
            return "nothing nominated. Try: advice <player>"
        return render.render_guidance(
            advise(
                state, book, key=key, precedent=precedent, seats=seats,
                strategy=strategy,
            ).guidance
        )

    if command.kind == "budgets":
        team_id = _team_arg(state, command.arg)
        return render.render_budgets(state, team_id)
    if command.kind == "state":
        return render.render_state(state, command.arg)
    if command.kind == "log":
        limit = int(command.arg) if command.arg and command.arg.isdigit() else 12
        return render.render_log(store.events, limit)
    return render.render_help(command.arg)


def _team_arg(state: DraftState, arg: str | None) -> int | None:
    if not arg:
        return None
    from ffa.ingest.manual.resolve import resolve_team

    try:
        return resolve_team(state, arg)
    except CommandError:
        return None


def _banner(state: DraftState) -> str:
    if not state.is_initialized:
        return "draft not initialized"
    league = state.league
    sold = len(state.sold_players())
    return (
        f"{league.name or 'draft'} — {league.team_count} teams, ${league.budget} each, "
        f"{league.draftable_slots} roster spots\n"
        f"{sold} pick(s) recorded. Type 'help' for commands, 'quit' to stop."
    )


def _reference_banner(book) -> str:
    if book is None or not len(book):
        return "no reference data — bid advice is unavailable (set [reference].path)"
    if book.is_sample:
        # Loud on purpose. Sample values are invented; acting on them would be
        # worse than having no advice at all.
        return (
            f"USING SAMPLE DATA ({len(book)} players) — these values are INVENTED. "
            "Set [reference].path to your real export before draft day."
        )
    return f"reference: {len(book)} players from {book.source}"


def _writer(stdout: TextIO):
    def emit(text: str) -> None:
        if text:
            stdout.write(text + "\n")
            stdout.flush()

    return emit


def _run_live(
    store: DraftStore,
    *,
    stdin: TextIO,
    stdout: TextIO,
    now_fn: Callable[[], object],
    prompt: str,
    book,
    source: EventSource | None,
    precedent=None,
    seats=None,
    strategy=None,
    assist_factory: Callable[[Any], Any] | None = None,
) -> int:
    """The same draft loop, fed by two producers instead of one."""
    # Everything that reaches the screen goes through the console, including the
    # source thread's failure notices. That is what makes the writes orderly:
    # two threads printing straight at a terminal is precisely the collision
    # this is here to stop.
    console = make_console(stdin, stdout, prompt)
    emit = console.notify
    inbox: "queue.Queue" = queue.Queue()

    if store.load_warnings:
        emit(render.render_warnings(store.load_warnings))
    emit(_banner(store.state))
    emit(_reference_banner(book))
    if source is not None:
        emit(f"live source: {source.name} — picks arrive on their own. "
             "Type to correct them, `quit` to stop.")

    # Built here because the runner posts onto this inbox. Any failure is fatal
    # to the assistant and to nothing else — the draft below runs identically.
    assist = None
    if assist_factory is not None:
        try:
            assist = assist_factory(inbox)
            emit(assist.banner())
        except Exception as exc:  # noqa: BLE001 - never block a draft
            emit(f"! assistant unavailable: {exc}")
            assist = None

    threading.Thread(target=_pump_stdin, args=(console, inbox), daemon=True).start()
    if source is not None:
        threading.Thread(
            target=_run_source, args=(source, inbox, emit), daemon=True
        ).start()

    # The live board, and the clock the tick agent runs on. Both exist only when
    # there is an assistant to ask — with `--assist` off nothing posts a `TICK`,
    # nothing routes a `BID`, and the loop below is the loop that shipped.
    bidding = _Bidding()
    ticking = threading.Event()
    if assist is not None:
        threading.Thread(
            target=_run_ticker, args=(inbox, ticking), daemon=True
        ).start()

    seen_warnings = len(store.state.warnings)
    try:
        while True:
            console.begin()
            try:
                tag, payload = inbox.get()
            except KeyboardInterrupt:  # pragma: no cover - interactive only
                emit("stopped. everything is saved.")
                return 0

            if tag is EOF or tag == EOF:
                # Same argument as quitting: a pick already in the queue was
                # observed and is in hand, and dropping it would leave the
                # journal disagreeing with what ESPN plainly showed.
                _drain(inbox, store, emit)
                return 0

            if tag == ASSIST:
                # A read coming back. Recording and printing both happen here,
                # on the one thread that owns the ledger — see runner.py.
                _settle_assist(emit, assist, payload, bidding)
                continue

            if tag == BID:
                # **Nothing is dispatched and nothing is printed.** The price on
                # the board is not a fact the tool asserts — the only bid that
                # ever mattered is the one that won, and that arrives as a
                # `PlayerSold`. This updates one in-memory value and returns.
                bidding.observe(payload)
                continue

            if tag == TICK:
                # The clock, not an input. Whether there is anything worth
                # asking about is decided here, on the thread that can see the
                # store.
                _maybe_tick(assist, store, bidding)
                continue

            if tag == EVENT:
                try:
                    store.dispatch(payload)
                except Exception as exc:  # pragma: no cover - journal failures
                    emit(f"error: could not record an incoming pick: {exc}")
                    continue
                emit(f"#{store.events[-1].id} {render.describe(payload)}")
                seen_warnings = _emit_new_warnings(emit, store.state, seen_warnings)
                # The deterministic readout first, always. Only once it is on
                # screen does anything get asked of a model.
                _maybe_guidance(emit, store, book, [payload], precedent, seats,
                                strategy)
                _maybe_assist(assist, store, book, [payload], bidding,
                              precedent, seats, strategy)
                _maybe_strategist(assist, store, book, [payload], strategy)
                _maybe_narrator(assist, store, book, [payload], strategy)
                continue

            # **Handled before `parse_command`, and deliberately not a grammar
            # rule.** Every other command in that grammar records something or
            # views something. This records nothing, changes nothing and can be
            # wrong — putting it in the parser that produces journal events
            # would place a question to a model on the same footing as `sold`.
            asked = _asking(payload)
            if asked is not None:
                _maybe_analyst(emit, assist, store, asked)
                continue

            outcome = _handle_line(
                payload, store, emit, book, now_fn, seen_warnings, precedent,
                seats, strategy,
            )
            if outcome.exit_code is not None:
                _drain(inbox, store, emit)
                return outcome.exit_code
            seen_warnings = outcome.seen_warnings
    finally:
        # Before the summary, so a tick cannot post onto a queue nobody is
        # draining any more. The thread is a daemon and would die anyway; this
        # is about not spending a call on a draft that has ended.
        ticking.set()
        if assist is not None:
            emit(assist.summary())
        if source is not None:
            source.stop()
        # Hand the terminal back the way we found it, whatever happened. On
        # POSIX this is what stops a crash leaving the shell with echo off.
        console.close()


def _drain(inbox: "queue.Queue", store: DraftStore, emit) -> None:
    """Record whatever the source already posted before we quit.

    Only what is *already* queued — a blocking drain would hang against a
    source still producing. Without this, typing `quit` the instant after a
    sale lands throws that sale away: it was observed, it is in hand, and
    dropping it would leave the journal disagreeing with what ESPN showed.
    """
    recorded = 0
    while True:
        try:
            tag, payload = inbox.get_nowait()
        except queue.Empty:
            break
        if tag != EVENT:
            continue
        try:
            store.dispatch(payload)
            recorded += 1
        except Exception:  # pragma: no cover - journal failures only
            break
    if recorded:
        emit(f"recorded {recorded} pick(s) that arrived as you quit.")


def _run_source(source: EventSource, inbox: "queue.Queue", emit) -> None:
    """Drive one source on its own thread, reporting how it ends.

    A source that dies quietly is the worst outcome on draft day: the board
    would simply stop updating and look like a lull in the bidding.
    """
    try:
        source.start(_TaggingQueue(inbox, EVENT))
    except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
        emit(f"\n! {source.name} failed: {type(exc).__name__}: {exc}")
        _wake(inbox, emit)
        return

    status = source.status()
    if status.health is not SourceHealth.STOPPED or status.detail:
        emit(f"\n! {source.name} stopped: {status.detail or status.health.value}")
    _wake(inbox, emit)


def _wake(inbox: "queue.Queue", emit) -> None:
    """Unblock the main loop after the source is gone, whatever killed it.

    Found in a live rehearsal, and it is worse than it sounds. Only the *raise*
    path used to post here, so the ordinary ending — the reader giving up after
    its failure threshold, which is exactly what a closed draft-room tab looks
    like — printed its notice and left the main thread parked on `inbox.get()`
    forever. The tool looked alive, the prompt was on screen, and nothing typed
    into it was read, because nothing was reading. Ctrl-C was the only way out
    of a draft that was still perfectly recoverable by hand.

    A blank line rather than EOF, deliberately: the reader dying is not a reason
    to end the draft. Every manual command still works, and typing the rest of
    the picks yourself is the fallback the whole grammar exists for.
    """
    emit(
        "the live reader is gone, but this draft is not. Keep recording by hand "
        "-- `sold <player> <price> <team>` -- or `quit` to stop."
    )
    inbox.put((LINE, ""))


class _LineOutcome:
    __slots__ = ("exit_code", "seen_warnings")

    def __init__(self, exit_code: int | None, seen_warnings: int) -> None:
        self.exit_code = exit_code
        self.seen_warnings = seen_warnings


def _handle_line(
    line: str, store: DraftStore, emit, book, now_fn, seen_warnings: int,
    precedent=None, seats=None, strategy=None,
) -> _LineOutcome:
    """One typed command. Shared by both loops so they cannot drift."""
    try:
        command = parse_command(
            line,
            ParseContext(
                state=store.state, now=now_fn(), events=store.events, book=book
            ),
        )
    except CommandError as exc:
        emit(f"error: {exc}")
        return _LineOutcome(None, seen_warnings)

    if isinstance(command, NoopCommand):
        return _LineOutcome(None, seen_warnings)
    if isinstance(command, QuitCommand):
        emit("saved.")
        return _LineOutcome(0, seen_warnings)
    if isinstance(command, ViewCommand):
        emit(_view(store, command, book, precedent, seats, strategy))
        return _LineOutcome(None, seen_warnings)

    if isinstance(command, EmitCommand):
        try:
            for event in command.events:
                store.dispatch(event)
        except Exception as exc:  # pragma: no cover - journal failures only
            emit(f"error: could not record that: {exc}")
            return _LineOutcome(None, seen_warnings)
        emit(f"#{store.events[-1].id} {command.echo}")
        seen_warnings = _emit_new_warnings(emit, store.state, seen_warnings)
        _maybe_guidance(emit, store, book, command.events, precedent, seats,
                        strategy)

    return _LineOutcome(None, seen_warnings)


def _settle_assist(emit, assist, outcome, bidding=None) -> None:
    """One finished read: record it, print it if it is still current.

    Muting prints once and only once. A line every time the API is down would
    be its own denial of service against the readout that actually matters.
    """
    if assist is None:                  # pragma: no cover - defensive
        return
    was_muted = assist.runner.muted
    text = assist.runner.settle(outcome, log=assist.log)
    if text:
        emit(text)
    if assist.runner.muted and not was_muted:
        emit(f"! {assist.runner.muted_reason}")

    if bidding is None:
        return
    record = outcome.record
    if record.status != "ok" or record.player_key != bidding.player_key:
        # A read about the previous player must not become the thing the next
        # player's ticks are compared against.
        return

    if record.agent == "room":
        # The open read the ticks revise. It lands seconds after the first ticks
        # have already fired, which is why the tick prompt copes with it missing.
        bidding.opening_read = dict(record.payload or {})
    elif record.agent == "room_tick" and text:
        # **Only when it was shown.** A tick rendered as "" was suppressed for
        # saying nothing new; recording it anyway would make the next genuinely
        # new read look like a repeat of something nobody ever saw.
        bidding.last_tick = dict(record.payload or {})


def _asking(line: str) -> str | None:
    """The question in an `ask ...` line, or `None` if this is not one.

    Returns `""` for a bare `ask`, which is different from `None`: the caller
    answers "ask what?" rather than passing an empty line to the grammar, where
    it would come back as an unrecognised command and read like the word itself
    was wrong.
    """
    stripped = line.strip()
    if stripped.lower() == "ask":
        return ""
    if stripped[:4].lower() == "ask ":
        return stripped[4:].strip()
    return None


def _digest(assist) -> str:
    """The running state of the draft, as the Strategist last wrote it.

    Empty before the first sale, which is a normal state and not a missing value
    — every prompt that receives it is written to work without it.
    """
    from ffa.assist.agents import strategist

    return strategist.digest_of(assist.log.latest("strategist"))


def _maybe_assist(assist, store: DraftStore, book, events, bidding=None,
                  precedent=None, seats=None, strategy=None) -> None:
    """Ask the Room, once a player is on the block.

    Called *after* `_maybe_guidance`, which is the whole discipline: the
    ceilings and threats are already on screen and this can only ever add to
    them. It returns immediately — the call happens on a worker.

    Every failure here is swallowed on purpose. Building a payload is pure
    dict work and should not fail, but if it ever does, an assistant that
    cannot start is a missing paragraph and never a draft that stops.
    """
    if assist is None or assist.runner.muted:
        return
    if store.state.current_nomination is None:
        return
    if not any(isinstance(e, PlayerNominated) for e in events):
        return

    try:
        from ffa.assist.agents import room

        # A new player is up, so anything still in flight is now about the
        # previous one. Bumped before submitting, never after.
        assist.runner.advance()

        view = assist.build_view()
        player_key = (view.get("nomination") or {}).get("key", "")
        event_id = store.events[-1].id if store.events else 0

        # The live board resets here and nowhere else, so a price, an open read
        # or a shown tick can never outlive the auction it described.
        if bidding is not None:
            bidding.reset(player_key, event_id)

        spec = room.build(
            view,
            prior_reads=[r.payload for r in assist.log.for_player(player_key)],
            digest=_digest(assist),
            labels=assist.labels,
        )
        assist.runner.submit(
            spec["agent"], spec["payload"],
            moment=room.moment_key(spec["player_key"], event_id),
            event_id=event_id, player_key=spec["player_key"],
            schema=spec["schema"], check=spec["check"], show=spec["show"],
        )
    except Exception:  # noqa: BLE001 - never let inference stop a draft
        pass


def _maybe_tick(assist, store: DraftStore, bidding) -> None:
    """Ask the Room to revise itself, while the bidding is still running.

    Four gates before anything is spent, and each rules out a call that could
    only have produced noise:

    - nothing is on the block, so there is nothing to revise;
    - no price has been observed yet, so nothing has changed since the open
      read — this is what keeps a manual draft with no live source from ever
      firing the tick;
    - the store agrees a player is up;
    - the runner already has a tick in flight, which it refuses on its own.

    The board is snapshotted here and frozen into the payload, like every other
    agent. A worker that read `bidding` would describe whichever price happened
    to be current when it got scheduled rather than when it was asked.
    """
    if assist is None or assist.runner.muted:
        return
    if bidding is None or not bidding.live:
        return
    if bidding.price is None:
        return
    if store.state.current_nomination is None:
        return

    try:
        from ffa.assist.agents import room

        view = assist.build_view()
        # Arithmetic on the main thread, and only the *transition* travels: the
        # price being above a ceiling is a state that persists for the rest of
        # the auction, and letting that state override the repeat guard is what
        # made one rehearsal print the same sentence three times.
        crossed = bidding.newly_crossed(room.crossing(bidding.as_payload(), view))
        spec = room.build_tick(
            view,
            live_bid=bidding.as_payload(),
            opening_read=bidding.opening_read,
            last_tick=bidding.last_tick,
            crossed=crossed,
            digest=_digest(assist),
            labels=assist.labels,
        )
        assist.runner.submit(
            spec["agent"], spec["payload"],
            moment=room.tick_moment_key(
                spec["player_key"], bidding.event_id, bidding.price or 0),
            event_id=bidding.event_id, player_key=spec["player_key"],
            schema=spec["schema"], check=spec["check"], show=spec["show"],
        )
    except Exception:  # noqa: BLE001 - never let inference stop a draft
        pass


def _maybe_strategist(assist, store: DraftStore, book, events,
                      strategy=None) -> None:
    """Ask the Strategist, when a pick lands and the plan state has moved.

    **Gated on the transition, not on the sale.** Firing on every pick would be
    180 calls saying the plan is still fine, and the one that matters — the pick
    that pushed it from `on track` to `at risk` — would be buried among them.

    The transition test is arithmetic and is done here, once. The same value is
    put in the prompt and handed to the guard, so the thing the model is asked
    to echo and the thing its echo is checked against cannot drift apart.
    """
    if assist is None or assist.runner.muted:
        return
    if strategy is None:
        return
    sold = [e for e in events if isinstance(e, PlayerSold)]
    if not sold:
        return

    try:
        from ffa.advice.feed import plan_state
        from ffa.advice.strategy import strategy_read
        from ffa.assist.agents import strategist
        from ffa.assist.reconcile import compare

        now = plan_state(strategy_read(store.state, book, strategy))
        was = assist.plan_state
        assist.plan_state = now
        if now is None or now == was:
            return

        event = sold[-1]
        player_key = event.player.key
        price = getattr(event.price, "value", None)
        winner = getattr(event.team, "value", None)

        prior = assist.log.latest("room", player_key)
        spec = strategist.build(
            assist.build_view(),
            plan_state=now,
            sale={"player_key": player_key, "price": price,
                  "winner_team_id": winner},
            reconciled=compare(
                dict(getattr(prior, "payload", None) or {}) or None,
                price=price, winner_team_id=winner,
            ),
            digest=_digest(assist),
        )
        event_id = store.events[-1].id if store.events else 0
        assist.runner.submit(
            spec["agent"], spec["payload"],
            moment=strategist.moment_key(spec["player_key"], event_id),
            event_id=event_id, player_key=spec["player_key"],
            schema=spec["schema"], check=spec["check"], show=spec["show"],
        )
    except Exception:  # noqa: BLE001 - never let inference stop a draft
        pass


def _maybe_narrator(assist, store: DraftStore, book, events,
                    strategy=None) -> None:
    """Ask the Narrator, when the deterministic feed says something was notable.

    **It is never asked whether an event was worth mentioning.** `build_feed`
    already decided that, by arithmetic, and only a `high` priority entry reaches
    here. The model is asked one thing: what it meant. That split is why there is
    no "is this interesting" field anywhere in its schema.
    """
    if assist is None or assist.runner.muted:
        return
    sold = [e for e in events if isinstance(e, PlayerSold)]
    if not sold:
        return

    try:
        from ffa.advice.feed import build_feed
        from ffa.assist.agents import narrator
        from ffa.assist.reconcile import compare

        entries = build_feed(store.events, book, strategy=strategy,
                             draft_id=store.state.draft_id, limit=3)
        notable = next((e for e in entries if e.priority == "high"), None)
        if notable is None:
            return

        # `FeedEntry.player` is a display name; the join to an earlier read is
        # the key, so it is carried separately rather than normalised twice.
        player_key = sold[-1].player.key
        entry = notable.as_dict()
        entry["player_key"] = player_key

        prior_read = narrator.prior_read_of(assist.log.latest("room", player_key))
        price = getattr(sold[-1].price, "value", None)
        winner = getattr(sold[-1].team, "value", None)

        spec = narrator.build(
            assist.build_view(),
            entry=entry,
            reconciled=compare(prior_read or None, price=price,
                               winner_team_id=winner),
            prior_read=prior_read,
            digest=_digest(assist),
        )
        event_id = store.events[-1].id if store.events else 0
        assist.runner.submit(
            spec["agent"], spec["payload"],
            moment=narrator.moment_key(spec["player_key"], event_id),
            event_id=event_id, player_key=spec["player_key"],
            schema=spec["schema"], check=spec["check"], show=spec["show"],
        )
    except Exception:  # noqa: BLE001 - never let inference stop a draft
        pass


def _maybe_analyst(emit, assist, store: DraftStore, question: str) -> None:
    """Ask the Analyst, because a person typed a question.

    The only agent invoked on purpose, and so the only one that says anything
    when it cannot run. Everywhere else silence is correct — a missing paragraph
    nobody asked for. Here somebody is waiting for an answer, and leaving them
    waiting for one that is never coming is worse than saying the assistant is
    off.
    """
    if assist is None:
        emit("the assistant is not running — start the draft with --assist.")
        return
    if assist.runner.muted:
        emit(f"! {assist.runner.muted_reason}")
        return
    if not question.strip():
        emit("ask what?")
        return

    try:
        from ffa.assist.agents import analyst

        spec = analyst.build(
            assist.build_view(),
            question=question,
            prior_reads=[r.payload for r in assist.log.all()[-5:] if r.ok],
            digest=_digest(assist),
        )
        event_id = store.events[-1].id if store.events else 0
        started = assist.runner.submit(
            spec["agent"], spec["payload"],
            moment=analyst.moment_key(question, event_id),
            event_id=event_id, player_key="",
            schema=spec["schema"], check=spec["check"], show=spec["show"],
        )
        # The one place a dropped call is reported. Elsewhere it means a read
        # nobody requested did not happen; here it means a question went
        # unanswered, and that must not look like the tool ignoring you.
        emit("thinking..." if started else
             "the assistant is busy — ask again in a moment.")
    except Exception as exc:  # noqa: BLE001 - never let inference stop a draft
        emit(f"could not ask: {type(exc).__name__}: {exc}")


def _maybe_guidance(emit, store: DraftStore, book, events,
                    precedent=None, seats=None, strategy=None) -> None:
    """Capability 2: the bid readout auto-fires on nomination.

    That is the moment it is needed, and asking for it costs seconds you do not
    have while the auctioneer is counting.
    """
    if book is None or store.state.current_nomination is None:
        return
    if any(isinstance(e, PlayerNominated) for e in events):
        emit(
            render.render_guidance(
                advise(
                    store.state, book, precedent=precedent, seats=seats,
                    strategy=strategy,
                ).guidance
            )
        )
