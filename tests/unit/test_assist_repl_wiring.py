"""The draft loop, with all four agents wired into it.

The agents and their guards are tested next door. What is tested here is the
*wiring* — the part that has no return value and fails by doing nothing at all:

- a `BidObservation` reaches the live board and never the store;
- a `TICK` asks the Room to revise itself, and only when there is something to
  revise;
- `ask` reaches the Analyst without passing through the grammar that writes
  events;
- a sale offers the Strategist and the Narrator a turn.

Everything runs against a fake `complete` callable. No key, no network — that
seam is the reason the whole package is testable, and this leans on it.
"""

from __future__ import annotations

import io
import json
import queue
import threading
import time

import pytest

from ffa.assist.budget import SpendGuard
from ffa.assist.context import ReadLog, ReadRecord
from ffa.assist.runner import AssistRunner
from ffa.assist.session import AssistSession
from ffa.cli.repl import (
    BID,
    EVENT,
    TICK,
    _asking,
    _Bidding,
    _maybe_analyst,
    _maybe_narrator,
    _maybe_strategist,
    _maybe_tick,
    _settle_assist,
    _TaggingQueue,
)
from ffa.domain.enums import Provenance
from ffa.domain.events import PlayerSold
from ffa.domain.ids import PlayerRef
from ffa.domain.sourced import known
from ffa.ingest.source import BidObservation
from tests.conftest import at


# --- a session driven by a fake API -----------------------------------------


class FakeReply:
    def __init__(self, payload):
        self.payload = payload
        self.usage = {"input": 10, "cache_read": 90}
        self.model = "fake"
        self.stop_reason = "end_turn"


def make_session(view, replies=None, *, log=None):
    """An `AssistSession` whose `complete` returns canned payloads."""
    calls: list[dict] = []
    replies = dict(replies or {})

    def complete(agent, system, user, *, schema=None, **kw):
        calls.append({"agent": agent, "payload": json.loads(user)})
        return FakeReply(replies.get(agent, {}))

    inbox: queue.Queue = queue.Queue()
    session = AssistSession(
        runner=AssistRunner(complete, inbox=inbox, prefix="p",
                            spend=SpendGuard(cap_dollars=99)),
        log=log or ReadLog(),
        build_view=lambda: view,
    )
    session.calls = calls
    return session


def settle_all(session, bidding=None, timeout=2.0):
    """Drain every in-flight read, returning what the loop would have printed."""
    shown: list[str] = []
    deadline = time.monotonic() + timeout
    while (session.runner.in_flight or not session.runner.inbox.empty()) \
            and time.monotonic() < deadline:
        try:
            tag, outcome = session.runner.inbox.get(timeout=0.5)
        except queue.Empty:
            break
        _settle_assist(shown.append, session, outcome, bidding)
    return shown


VIEW = {
    "nomination": {
        "key": "bijan-robinson",
        "player_key": "bijan-robinson",
        "name": "Bijan Robinson",
        "position": "RB",
        "guidance": {"max_advisable_bid": 41, "max_legal_bid": 88, "plan_cap": 45,
                     "threats": [{"team_id": 3, "is_live": True,
                                  "max_legal_bid": 52}]},
    },
    "me": {"team_id": 4}, "teams": [], "market": {}, "scarcity": {},
    "strategy": {}, "watchlist": [], "recent_sales": [], "feed": [],
}


class Store:
    """The two attributes the trigger functions actually read."""

    class _State:
        current_nomination = object()
        draft_id = "d"
        warnings = ()

    def __init__(self, nominated=True):
        self.state = Store._State()
        if not nominated:
            self.state.current_nomination = None
        self.events = []


# --- bids are routed by type, not by trust ----------------------------------


def test_a_bid_observation_is_tagged_apart_from_events():
    """The routing that keeps it out of `store.dispatch`. Done in the one place
    everything a source produces passes through, so a source cannot get it
    wrong and a new source cannot forget."""
    target: queue.Queue = queue.Queue()
    out = _TaggingQueue(target, EVENT)

    out.put(BidObservation("bijan-robinson", 30))
    out.put(PlayerSold(at=at(0), source="X", player=PlayerRef.from_raw("A"),
                       team=known(3, Provenance.ESPN_API, at(0)),
                       price=known(9, Provenance.ESPN_API, at(0))))

    assert target.get()[0] == BID
    assert target.get()[0] == EVENT


def test_the_live_board_ignores_a_price_for_another_player():
    """Two nominations can overlap on a slow poll. A price folded into the wrong
    one would have the tick revising an estimate about somebody else."""
    bidding = _Bidding()
    bidding.reset("bijan-robinson", 12)

    assert bidding.observe(BidObservation("bijan-robinson", 30))
    assert not bidding.observe(BidObservation("puka-nacua", 44))
    assert bidding.price == 30


def test_a_nomination_wipes_the_previous_board():
    """Carrying a price, an open read or a shown tick across players is how a
    read about one auction ends up printed under another."""
    bidding = _Bidding()
    bidding.reset("bijan-robinson", 12)
    bidding.observe(BidObservation("bijan-robinson", 30))
    bidding.last_tick = {"note": "old"}
    bidding.opening_read = {"read": "old"}

    bidding.reset("puka-nacua", 44)

    assert bidding.price is None
    assert bidding.last_tick is None
    assert bidding.opening_read is None


# --- the tick -----------------------------------------------------------------


def test_the_tick_says_nothing_until_a_price_has_been_seen():
    """This is what keeps a manual draft with no live source from ever firing
    the tick: no observations, no calls, no bill."""
    session = make_session(VIEW)
    bidding = _Bidding()
    bidding.reset("bijan-robinson", 12)

    _maybe_tick(session, Store(), bidding)

    assert session.calls == []


def test_the_tick_fires_once_a_price_arrives():
    session = make_session(VIEW, {"room_tick": {"changed": False}})
    bidding = _Bidding()
    bidding.reset("bijan-robinson", 12)
    bidding.observe(BidObservation("bijan-robinson", 30))

    _maybe_tick(session, Store(), bidding)
    settle_all(session, bidding)

    assert [c["agent"] for c in session.calls] == ["room_tick"]
    assert session.calls[0]["payload"]["live_bid"]["price"] == 30


def test_the_tick_says_nothing_with_no_player_on_the_block():
    session = make_session(VIEW)
    bidding = _Bidding()
    bidding.reset("bijan-robinson", 12)
    bidding.observe(BidObservation("bijan-robinson", 30))

    _maybe_tick(session, Store(nominated=False), bidding)

    assert session.calls == []


def test_an_unchanged_tick_prints_nothing_and_is_not_remembered():
    """A tick rendered as "" was suppressed for saying nothing new. Recording it
    would make the next genuinely new read look like a repeat of something
    nobody ever saw."""
    session = make_session(VIEW, {"room_tick": {"changed": False}})
    bidding = _Bidding()
    bidding.reset("bijan-robinson", 12)
    bidding.observe(BidObservation("bijan-robinson", 30))

    _maybe_tick(session, Store(), bidding)
    shown = settle_all(session, bidding)

    assert shown == []
    assert bidding.last_tick is None


def test_a_shown_tick_becomes_what_the_next_one_is_compared_against():
    session = make_session(
        VIEW, {"room_tick": {"changed": True, "note": "Dave is still in."}})
    bidding = _Bidding()
    bidding.reset("bijan-robinson", 12)
    bidding.observe(BidObservation("bijan-robinson", 30))

    _maybe_tick(session, Store(), bidding)
    shown = settle_all(session, bidding)

    assert shown and "Dave is still in." in shown[0]
    assert bidding.last_tick["note"] == "Dave is still in."


def test_the_tick_carries_the_open_read_once_it_lands():
    """The open read arrives seconds after the first ticks have already fired,
    so `_settle_assist` is what closes the loop between them."""
    session = make_session(VIEW, {"room": {"read": "Dave is the one to beat",
                                           "confidence": "fair"}})
    bidding = _Bidding()
    bidding.reset("bijan-robinson", 12)

    session.runner.submit("room", {}, moment="m", player_key="bijan-robinson",
                          show=lambda p: "shown")
    settle_all(session, bidding)

    assert bidding.opening_read["read"] == "Dave is the one to beat"


def test_a_read_about_the_previous_player_never_lands_on_the_new_board():
    session = make_session(VIEW, {"room": {"read": "stale"}})
    bidding = _Bidding()
    bidding.reset("puka-nacua", 44)

    session.runner.submit("room", {}, moment="m", player_key="bijan-robinson",
                          show=lambda p: "shown")
    settle_all(session, bidding)

    assert bidding.opening_read is None


def test_a_crossing_is_announced_once_and_not_for_the_rest_of_the_auction():
    """**The defect a live practice room found.**

    `crossing` reports a state: the price is above our ceiling, and it stays
    above for the rest of the bidding. A crossing also overrides the repeat
    guard, so reporting the state every tick disabled suppression entirely from
    the moment the price passed - one auction printed the same sentence three
    times, twice differing only by the word "are".

    Latching, not comparing: a bid that dips back under and climbs again has not
    discovered anything new about the same number."""
    bidding = _Bidding()
    bidding.reset("bijan-robinson", 12)

    assert bidding.newly_crossed("") == ""
    assert bidding.newly_crossed("the advisable bid") == "the advisable bid"
    assert bidding.newly_crossed("the advisable bid") == ""
    assert bidding.newly_crossed("the advisable bid") == ""
    # A different, more serious ceiling is genuinely new.
    assert bidding.newly_crossed("your plan cap") == "your plan cap"
    assert bidding.newly_crossed("your plan cap") == ""


def test_a_crossing_does_not_survive_the_next_nomination():
    """Every player has his own ceilings. Carrying the latch would silence the
    first genuine crossing on the next one."""
    bidding = _Bidding()
    bidding.reset("bijan-robinson", 12)
    bidding.newly_crossed("your plan cap")

    bidding.reset("puka-nacua", 44)
    assert bidding.newly_crossed("your plan cap") == "your plan cap"


def test_the_tick_only_wears_the_crossing_it_was_handed():
    """`build_tick` no longer computes it. The caller owns the memory of what
    has already been announced, which keeps the builder a pure function of its
    arguments like everything else in that module."""
    from ffa.assist.agents import room

    view = {"nomination": {"player_key": "bijan", "guidance":
            {"max_advisable_bid": 40, "plan_cap": 45, "threats": []}}}
    # The price is well past both, so `crossing` would report one - but nothing
    # was passed in, so nothing is shown.
    spec = room.build_tick(view, live_bid={"price": 90})
    shown = spec["show"]({"changed": True, "note": "climbing"})

    assert "past" not in shown
    assert room.crossing({"price": 90}, view) == "your plan cap"


# --- the Strategist fires on a transition, not on a sale ---------------------


def test_the_strategist_is_silent_without_a_plan():
    """A draft with no plan is a normal draft, not a plan that is failing."""
    session = make_session(VIEW)
    _maybe_strategist(session, Store(), None, [_sale()], None)
    assert session.calls == []


def _sale(name="Bijan Robinson", team=3, price=41):
    moment = at(0)
    return PlayerSold(at=moment, source=Provenance.ESPN_API.value,
                      player=PlayerRef.from_raw(name),
                      team=known(team, Provenance.ESPN_API, moment),
                      price=known(price, Provenance.ESPN_API, moment))


def test_the_strategist_is_silent_when_nothing_landed():
    session = make_session(VIEW)
    _maybe_strategist(session, Store(), None, [], object())
    assert session.calls == []


# --- the Narrator ------------------------------------------------------------


def test_the_narrator_is_silent_when_nothing_landed():
    session = make_session(VIEW)
    _maybe_narrator(session, Store(), None, [], None)
    assert session.calls == []


# --- `ask` -------------------------------------------------------------------


@pytest.mark.parametrize("line, expected", [
    ("ask who is live on him", "who is live on him"),
    ("  ASK  what would it cost  ", "what would it cost"),
    ("ask", ""),
    ("Ask ", ""),
])
def test_ask_is_recognised_before_the_grammar_sees_it(line, expected):
    """Every other command in that grammar records or views something. This
    records nothing and can be wrong, so it never reaches the parser that
    produces journal events."""
    assert _asking(line) == expected


@pytest.mark.parametrize("line", [
    "sold barkley 40 t3", "asking price", "askew", "", "undo", "task",
])
def test_an_ordinary_command_is_not_mistaken_for_a_question(line):
    assert _asking(line) is None


def test_asking_with_no_assistant_says_so_rather_than_going_quiet():
    """The only agent invoked on purpose, so the only one that reports it cannot
    run. Silence here would look like the tool ignoring you."""
    said: list[str] = []
    _maybe_analyst(said.append, None, Store(), "who is live")
    assert "--assist" in said[0]


def test_asking_nothing_asks_what():
    said: list[str] = []
    _maybe_analyst(said.append, make_session(VIEW), Store(), "   ")
    assert said == ["ask what?"]


def test_a_question_reaches_the_analyst_with_the_whole_board():
    session = make_session(VIEW, {"analyst": {"text": "About $14 over."}})
    said: list[str] = []

    _maybe_analyst(said.append, session, Store(), "what would it cost")
    shown = settle_all(session)

    assert said == ["thinking..."]
    assert session.calls[0]["agent"] == "analyst"
    assert session.calls[0]["payload"]["question"] == "what would it cost"
    assert shown and "About $14 over." in shown[0]


def test_a_muted_assistant_says_why_instead_of_answering():
    session = make_session(VIEW)
    session.runner.mute("out of budget")
    said: list[str] = []

    _maybe_analyst(said.append, session, Store(), "anything")

    assert said == ["! out of budget"]
    assert session.calls == []


# --- the digest --------------------------------------------------------------


def test_the_digest_reaches_the_tick_from_the_strategist_read():
    """The whole shared-memory claim, end to end: one agent writes it, another
    reads it, through the log and nothing else."""
    log = ReadLog()
    log.append(ReadRecord(agent="strategist", moment="m",
                          payload={"digest": "Prices running hot."}))
    session = make_session(VIEW, {"room_tick": {"changed": False}}, log=log)
    bidding = _Bidding()
    bidding.reset("bijan-robinson", 12)
    bidding.observe(BidObservation("bijan-robinson", 30))

    _maybe_tick(session, Store(), bidding)
    settle_all(session, bidding)

    assert session.calls[0]["payload"]["digest"] == "Prices running hot."


def test_a_rejected_strategist_read_never_becomes_the_digest():
    """`ReadLog.latest` skips non-ok records. Passing one on would launder
    exactly the output the guard refused into every other prompt."""
    log = ReadLog()
    log.append(ReadRecord(agent="strategist", moment="m", status="rejected",
                          payload={"digest": "Bid up to $40."}))
    session = make_session(VIEW, {"room_tick": {"changed": False}}, log=log)
    bidding = _Bidding()
    bidding.reset("bijan-robinson", 12)
    bidding.observe(BidObservation("bijan-robinson", 30))

    _maybe_tick(session, Store(), bidding)
    settle_all(session, bidding)

    assert session.calls[0]["payload"]["digest"] == ""


# --- the loop itself ---------------------------------------------------------


class _Source:
    """Emits a fixed list of items — events and observations alike — then stops."""

    name = "fake"

    def __init__(self, items):
        self._items = list(items)
        self.done = threading.Event()
        self.stopped = False

    def start(self, out) -> None:
        try:
            for item in self._items:
                if self.stopped:
                    return
                out.put(item)
        finally:
            self.done.set()

    def stop(self) -> None:
        self.stopped = True

    def status(self):
        from ffa.ingest.source import SourceHealth, SourceStatus
        return SourceStatus(name=self.name, health=SourceHealth.OK)


class _Stdin:
    """Holds the typed line back until the source has finished emitting.

    A bare `StringIO` returns "quit" instantly, so the loop would race the source
    thread and every assertion would be a coin toss.
    """

    def __init__(self, script, gate, timeout=5.0):
        self._lines = script.splitlines(keepends=True)
        self._gate = gate
        self._timeout = timeout

    def readline(self) -> str:
        self._gate.wait(self._timeout)
        return self._lines.pop(0) if self._lines else ""


def test_the_loop_routes_a_bid_to_the_board_and_never_to_the_journal(
    tmp_path, init_event
):
    """The end-to-end version of the routing test above, through `run_repl`.

    A `BidObservation` arriving on the same queue as events must update the live
    board and leave the journal alone. Were it ever dispatched, the store would
    either raise or record something with no event type — and the draft would
    carry a line nobody ever claimed had happened.
    """
    from ffa.cli.repl import run_repl
    from ffa.state.store import DraftStore

    store = DraftStore.create(tmp_path / "run", init_event)
    before = len(store.events)

    source = _Source([BidObservation("bijan-robinson", 30),
                      BidObservation("bijan-robinson", 34)])
    out = io.StringIO()
    try:
        code = run_repl(store, stdin=_Stdin("quit\n", source.done), stdout=out,
                        book=None, source=source)
    finally:
        store.close()

    assert code == 0
    assert len(store.events) == before
    assert "error" not in out.getvalue().lower()


def test_the_loop_survives_a_bid_for_a_player_nobody_nominated(tmp_path, init_event):
    """A price can arrive before the nomination that explains it, or after the
    sale that ended it. Both are shrugs, not errors."""
    from ffa.cli.repl import run_repl
    from ffa.state.store import DraftStore

    store = DraftStore.create(tmp_path / "run", init_event)
    source = _Source([BidObservation("", None), BidObservation("ghost", 9)])
    out = io.StringIO()
    try:
        code = run_repl(store, stdin=_Stdin("quit\n", source.done), stdout=out,
                        book=None, source=source)
    finally:
        store.close()

    assert code == 0
    assert "Traceback" not in out.getvalue()


def test_no_ticker_runs_when_the_assistant_is_off(tmp_path, init_event):
    """With the assistant off nothing posts a `TICK`, nothing routes a `BID` into
    a read, and the loop is the loop that shipped."""
    from ffa.cli import repl as repl_mod
    from ffa.cli.repl import run_repl
    from ffa.state.store import DraftStore

    started: list[object] = []
    original = repl_mod._run_ticker

    def spy(inbox, stop):
        started.append(inbox)
        return original(inbox, stop)

    repl_mod._run_ticker = spy
    try:
        store = DraftStore.create(tmp_path / "run", init_event)
        source = _Source([])
        try:
            run_repl(store, stdin=_Stdin("quit\n", source.done),
                     stdout=io.StringIO(), book=None, source=source)
        finally:
            store.close()
    finally:
        repl_mod._run_ticker = original

    assert started == []
