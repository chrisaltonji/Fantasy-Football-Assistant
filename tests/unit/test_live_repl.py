"""The live draft loop — two producers, one writer.

The manual REPL blocks on `readline()`, so a pick landing while you are not
typing would not be seen until you pressed Enter. The live loop moves both
stdin and the event source onto threads that post to one queue, and the main
thread stays the only thing that ever calls `store.dispatch`.

That single-writer property is the thing worth testing. The architecture buys
crash-safety and undo from it, and a second writer would corrupt journal
ordering rather than fail loudly.
"""

from __future__ import annotations

import io
import queue
import threading
import time

from ffa.cli.repl import EVENT, _TaggingQueue, run_repl
from ffa.domain.enums import Provenance
from ffa.domain.events import PlayerSold
from ffa.domain.ids import PlayerRef
from ffa.domain.sourced import known
from ffa.ingest.source import SourceHealth, SourceStatus
from ffa.state.store import DraftStore
from tests.conftest import at


def sale(name: str, team: int, price: int, seconds: int = 0) -> PlayerSold:
    moment = at(seconds)
    return PlayerSold(
        at=moment,
        source=Provenance.ESPN_API.value,
        player=PlayerRef.from_raw(name),
        team=known(team, Provenance.ESPN_API, moment),
        price=known(price, Provenance.ESPN_API, moment),
    )


class FakeSource:
    """An `EventSource` that emits a fixed list, then signals completion."""

    name = "fake"

    def __init__(self, events, *, delay: float = 0.0):
        self._events = list(events)
        self._delay = delay
        self._health = SourceHealth.OK
        self.stopped = False
        self.done = threading.Event()

    def start(self, out) -> None:
        try:
            for event in self._events:
                if self.stopped:
                    return
                if self._delay:
                    time.sleep(self._delay)
                out.put(event)
        finally:
            self.done.set()

    def stop(self) -> None:
        self.stopped = True
        self._health = SourceHealth.STOPPED

    def status(self) -> SourceStatus:
        return SourceStatus(name=self.name, health=self._health)


class GatedStdin:
    """Holds each typed line back until the source has finished emitting.

    A `StringIO` returns "quit" instantly, so the loop would race the source
    thread and every assertion would be a coin toss. Gating makes the ordering
    deterministic without sprinkling `sleep` through the tests.
    """

    def __init__(self, script: str, gate: threading.Event, timeout: float = 5.0):
        self._lines = script.splitlines(keepends=True)
        self._gate = gate
        self._timeout = timeout

    def readline(self) -> str:
        self._gate.wait(self._timeout)
        return self._lines.pop(0) if self._lines else ""


def drive(store, source, script: str = "quit\n"):
    out = io.StringIO()
    code = run_repl(
        store, stdin=GatedStdin(script, source.done), stdout=out,
        book=None, source=source,
    )
    return code, out.getvalue()


# --- the tagging queue ------------------------------------------------------


def test_the_source_sees_a_plain_queue_and_never_learns_it_is_multiplexed():
    """Wrapping the queue is what keeps the `EventSource` Protocol honest.

    `ManualSource`, `SimSource` and `DraftRoomSource` all put bare events; none
    of them should need to know another producer exists.
    """
    inbox: queue.Queue = queue.Queue()
    event = sale("mahomes", 3, 45)

    _TaggingQueue(inbox, EVENT).put(event)

    assert inbox.get() == (EVENT, event)


# --- events land without typing --------------------------------------------


def test_a_pick_is_recorded_without_the_user_typing_anything(tmp_path, init_event):
    store = DraftStore.create(tmp_path / "run", init_event)
    try:
        code, out = drive(store, FakeSource([sale("mahomes", 3, 45)]))
        assert code == 0
        assert len(store.state.sold_players()) == 1
        assert "mahomes" in out
    finally:
        store.close()


def test_incoming_picks_are_echoed_with_the_id_undo_takes(tmp_path, init_event):
    """`undo #7` is what you type under pressure, so the id has to be visible."""
    store = DraftStore.create(tmp_path / "run", init_event)
    try:
        _, out = drive(store, FakeSource([sale("mahomes", 3, 45)]))
        assert f"#{store.events[-1].id}" in out
    finally:
        store.close()


def test_several_picks_all_land(tmp_path, init_event):
    events = [sale("mahomes", 3, 45, 0), sale("barkley", 1, 62, 1),
              sale("kelce", 5, 30, 2)]
    store = DraftStore.create(tmp_path / "run", init_event)
    try:
        drive(store, FakeSource(events))
        assert len(store.state.sold_players()) == 3
    finally:
        store.close()


def test_typing_still_works_alongside_the_source(tmp_path, init_event):
    """Manual entry is the correction path; it must not be crowded out."""
    store = DraftStore.create(tmp_path / "run", init_event)
    try:
        drive(store, FakeSource([sale("mahomes", 3, 45)]),
              script="sold barkley 62 t1\nquit\n")
        keys = {p.ref.key for p in store.state.sold_players()}
        assert keys == {"mahomes", "barkley"}
    finally:
        store.close()


def test_a_pick_queued_at_the_moment_you_quit_is_still_recorded(tmp_path, init_event):
    """Observed, in hand, and thrown away would be the worst outcome.

    The journal would disagree with what ESPN plainly showed, and nothing would
    say so.
    """
    store = DraftStore.create(tmp_path / "run", init_event)
    source = FakeSource([sale("mahomes", 3, 45)])
    try:
        # Release the gate first, so `quit` and the pick are both queued.
        source.done.set()
        _, out = drive(store, source)
        assert len(store.state.sold_players()) == 1
    finally:
        store.close()


# --- the single-writer property --------------------------------------------


def test_every_write_happens_on_the_main_thread(tmp_path, init_event):
    """The whole concurrency story in one assertion.

    Producers hand events over; they never touch the store. A second writing
    thread would interleave journal appends and desynchronise state from disk
    on resume — silently, and only visible after a crash.
    """
    store = DraftStore.create(tmp_path / "run", init_event)
    writers: set[int] = set()
    store.subscribe(lambda state, event: writers.add(threading.get_ident()))
    try:
        drive(store, FakeSource([sale("mahomes", 3, 45), sale("barkley", 1, 62)]),
              script="sold kelce 30 t5\nquit\n")
        assert writers == {threading.get_ident()}
    finally:
        store.close()


def test_the_journal_stays_replayable_after_mixed_input(tmp_path, init_event):
    """Folded-forward state and a from-scratch replay must agree."""
    from ffa.domain.reducers import replay

    store = DraftStore.create(tmp_path / "run", init_event)
    try:
        drive(store, FakeSource([sale("mahomes", 3, 45)]),
              script="sold barkley 62 t1\nquit\n")
        live_state, events = store.state, store.events
    finally:
        store.close()

    assert replay(events, live_state.draft_id).players == live_state.players


# --- lifecycle --------------------------------------------------------------


def test_quitting_stops_the_source(tmp_path, init_event):
    """A thread left polling a browser after the draft ends is a leak."""
    source = FakeSource([])
    store = DraftStore.create(tmp_path / "run", init_event)
    try:
        drive(store, source)
        assert source.stopped is True
    finally:
        store.close()


def test_eof_on_stdin_ends_the_loop(tmp_path, init_event):
    store = DraftStore.create(tmp_path / "run", init_event)
    try:
        code, _ = drive(store, FakeSource([]), script="")
        assert code == 0
    finally:
        store.close()


def test_a_source_that_raises_is_reported_not_swallowed(tmp_path, init_event):
    """A source that dies quietly looks exactly like a lull in the bidding."""

    class Exploding(FakeSource):
        def start(self, out):
            self.done.set()
            raise RuntimeError("browser went away")

    store = DraftStore.create(tmp_path / "run", init_event)
    try:
        _, out = drive(store, Exploding([]))
        assert "browser went away" in out
        assert "fake failed" in out
    finally:
        store.close()


def test_the_live_banner_says_where_picks_come_from(tmp_path, init_event):
    store = DraftStore.create(tmp_path / "run", init_event)
    try:
        _, out = drive(store, FakeSource([]))
        assert "live source: fake" in out
    finally:
        store.close()


# --- typing while picks land ------------------------------------------------


class HeldSource(FakeSource):
    """Emits nothing until released, so the test controls when a pick lands."""

    def __init__(self, events):
        super().__init__(events)
        self.release = threading.Event()

    def start(self, out) -> None:
        self.release.wait(5.0)
        super().start(out)


def test_a_pick_landing_mid_command_does_not_garble_what_you_typed(
    tmp_path, init_event, monkeypatch
):
    """The pick-14 failure, through the real loop rather than the console alone.

    Before this, the sale printed wherever the cursor happened to be and the
    half-typed command was left split across two lines — still in the terminal's
    buffer, no longer under its prompt. The command has to survive both the
    redraw and the submit.
    """
    import io as _io

    from ffa.cli.console import RawConsole
    from tests.unit.test_console import BlockingKeys, _wait_for

    keys = BlockingKeys()
    screen = _io.StringIO()
    monkeypatch.setattr(
        "ffa.cli.repl.make_console",
        lambda stdin, stdout, prompt: RawConsole(stdin, screen, prompt, keys=keys),
    )

    source = HeldSource([sale("mahomes", 3, 45)])
    store = DraftStore.create(tmp_path / "run", init_event)
    finished: list[int] = []
    loop = threading.Thread(
        target=lambda: finished.append(
            run_repl(store, stdin=_io.StringIO(), stdout=_io.StringIO(),
                     book=None, source=source)
        ),
        daemon=True,
    )
    loop.start()
    try:
        keys.type("sold barkl")
        _wait_for(screen, "> sold barkl")

        source.release.set()
        deadline = time.monotonic() + 5.0
        while "mahomes" not in screen.getvalue() and time.monotonic() < deadline:
            time.sleep(0.005)

        # The sale printed, and the half-typed command is back under it.
        assert "mahomes" in screen.getvalue()
        assert screen.getvalue().endswith("> sold barkl")

        keys.type("ey 62 t1\r")
        keys.type("quit\r")
        loop.join(timeout=5)

        assert finished == [0]
        assert {p.ref.key for p in store.state.sold_players()} == {"mahomes", "barkley"}
    finally:
        source.stop()
        store.close()


# --- the manual path is untouched -------------------------------------------


def test_no_source_means_the_original_blocking_loop(tmp_path, init_event):
    """Every integration test drives this path; it must not have changed."""
    store = DraftStore.create(tmp_path / "run", init_event)
    out = io.StringIO()
    try:
        code = run_repl(store, stdin=io.StringIO("sold mahomes 45 t3\nquit\n"),
                        stdout=out, book=None)
        assert code == 0
        assert len(store.state.sold_players()) == 1
        assert "live source" not in out.getvalue()
    finally:
        store.close()
