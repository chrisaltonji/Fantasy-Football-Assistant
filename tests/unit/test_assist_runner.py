"""The runner, driven end to end with a fake client.

No key, no network, no SDK. `complete` is a callable, so every one of these
drives the real threading, the real semaphore and the real settle path — the
only thing replaced is the API call itself.

The tests that matter most are the ones about what *cannot* happen: a worker
that raises must not kill the draft, a slow call must not block anything, and a
read about a player who already sold must be recorded and never printed.
"""

from __future__ import annotations

import queue
import threading
import time

from ffa.assist.budget import SpendGuard
from ffa.assist.context import ReadLog
from ffa.assist.errors import AssistError
from ffa.assist.runner import ASSIST, AssistRunner


class FakeReply:
    def __init__(self, payload, usage=None, model="claude-opus-5"):
        self.payload = payload
        self.usage = usage or {"input": 100, "output": 50,
                               "cache_read": 2000, "cache_creation": 0}
        self.model = model
        self.stop_reason = "end_turn"


def runner(complete, **kw):
    inbox: queue.Queue = queue.Queue()
    return AssistRunner(complete, inbox=inbox, prefix="PREFIX", **kw), inbox


def wait(inbox, timeout=5.0):
    tag, outcome = inbox.get(timeout=timeout)
    assert tag == ASSIST
    return outcome


def ok(payload=None):
    return lambda *a, **k: FakeReply(payload or {"read": "a thin room"})


# --- the thing that must never happen ---------------------------------------


def test_a_worker_that_raises_becomes_a_record_not_a_crash():
    """The last line of defence. A traceback on a daemon thread prints straight
    over a live auction and there is no scrolling it back."""
    def boom(*a, **k):
        raise RuntimeError("something nobody predicted")

    run, inbox = runner(boom)
    run.submit("room", {}, moment="m")
    outcome = wait(inbox)

    assert outcome.record.status == "failed"
    assert "RuntimeError" in outcome.record.detail


def test_a_slow_call_never_blocks_the_caller():
    """submit() returns immediately or the whole design is pointless."""
    started = threading.Event()

    def slow(*a, **k):
        started.set()
        time.sleep(0.4)
        return FakeReply({"read": "late"})

    run, inbox = runner(slow)
    before = time.monotonic()
    run.submit("room", {}, moment="m")
    elapsed = time.monotonic() - before

    assert elapsed < 0.2, "submit blocked on the call"
    assert started.wait(2.0)
    wait(inbox)


def test_the_slot_is_released_even_when_the_worker_dies():
    """Otherwise two unlucky failures mute the assistant by exhausting slots,
    and nothing would say why."""
    def boom(*a, **k):
        raise RuntimeError("nope")

    run, inbox = runner(boom, max_in_flight=1)
    for _ in range(3):
        assert run.submit("room", {}, moment="m") is True
        wait(inbox)


def test_workers_are_daemons_so_quit_is_never_held_up():
    """A ThreadPoolExecutor joins its workers at exit; a 30s request in flight
    would swallow a Ctrl-C. That is the reason this file uses raw threads."""
    gate = threading.Event()
    seen: list[threading.Thread] = []

    def blocked(*a, **k):
        seen.append(threading.current_thread())
        gate.wait(3.0)
        return FakeReply({"read": "x"})

    run, inbox = runner(blocked)
    run.submit("room", {}, moment="m")
    for _ in range(200):
        if seen:
            break
        time.sleep(0.01)

    assert seen and seen[0].daemon is True
    gate.set()
    wait(inbox)


# --- back-pressure ----------------------------------------------------------


def test_calls_beyond_the_limit_are_dropped_rather_than_queued():
    """A queued call lands after the player sold, and an unbounded backlog is
    how a slow API becomes a bill for reads nobody ever saw."""
    gate = threading.Event()

    def blocked(*a, **k):
        gate.wait(3.0)
        return FakeReply({"read": "x"})

    run, inbox = runner(blocked, max_in_flight=2)
    assert run.submit("room", {}, moment="a") is True
    assert run.submit("room", {}, moment="b") is True
    assert run.submit("room", {}, moment="c") is False   # dropped, not queued

    gate.set()
    wait(inbox)
    wait(inbox)


# --- supersede --------------------------------------------------------------


def test_a_read_about_a_player_who_already_sold_is_recorded_but_not_shown():
    """The auction moved. On screen it is noise; in the ledger it is the
    evidence the Grader scores the assistant with."""
    run, inbox = runner(ok())
    log = ReadLog()

    run.submit("room", {}, moment="bijan", show=lambda p: "SHOWN")
    outcome = wait(inbox)
    run.advance()                      # a new player is up

    text = run.settle(outcome, log=log)

    assert text == "", "a stale read reached the screen"
    assert log.latest("room") is None, "a late read must not be read as current"
    assert log.all()[-1].status == "late"


def test_an_agent_that_is_not_about_the_current_player_is_never_superseded():
    """**The bug a live practice room found, and it silenced an agent outright.**

    Supersede judged every read against the current nomination. That is right for
    the Room, whose whole subject is the player on the block. The Strategist
    comments on a sale that has *already happened*, and ESPN nominates the next
    player within seconds of one — so a 14-20s Strategist read was structurally
    guaranteed to be marked late and never printed. Live: it fired once, was
    marked late, showed nothing. It was not slow; it was being asked the wrong
    question about whether it still mattered.
    """
    run, inbox = runner(ok())
    log = ReadLog()

    run.submit("strategist", {}, moment="sale", show=lambda p: "SHOWN")
    outcome = wait(inbox)
    run.advance()                      # the next player goes up, as it always does

    text = run.settle(outcome, log=log)

    assert text == "SHOWN"
    assert log.all()[-1].status == "ok"


def test_the_narrator_survives_the_nomination_that_follows_the_sale():
    """Same argument. It narrates an event that is already in the journal."""
    run, inbox = runner(ok())
    run.submit("narrator", {}, moment="sale", show=lambda p: "NOTE")
    outcome = wait(inbox)
    run.advance()

    assert run.settle(outcome) == "NOTE"


def test_the_answer_to_a_question_is_not_thrown_away_by_a_nomination():
    """The Analyst is the one agent a person invoked on purpose. Discarding its
    answer because the board moved would look like the tool ignoring them."""
    run, inbox = runner(ok())
    run.submit("analyst", {}, moment="q", show=lambda p: "ANSWER")
    outcome = wait(inbox)
    run.advance()

    assert run.settle(outcome) == "ANSWER"


def test_the_tick_is_still_superseded_because_it_is_about_the_block():
    """The other half of the rule. A revision of a price nobody is bidding any
    more is exactly the noise the whole mechanism exists to keep off the screen."""
    run, inbox = runner(ok())
    log = ReadLog()

    run.submit("room_tick", {}, moment="t", show=lambda p: "STALE")
    outcome = wait(inbox)
    run.advance()

    assert run.settle(outcome, log=log) == ""
    assert log.all()[-1].status == "late"


def test_the_superseding_set_names_only_agents_that_have_a_profile():
    """Two lists of agent names that must not drift apart: one decides what goes
    stale, the other what model is called. A typo here fails open — the agent
    would simply never be superseded, silently."""
    from ffa.assist.client import PROFILES
    from ffa.assist.runner import SUPERSEDING

    assert SUPERSEDING <= set(PROFILES)
    assert SUPERSEDING == {"room", "room_tick"}


def test_a_current_read_is_shown():
    run, inbox = runner(ok())
    run.submit("room", {}, moment="bijan", show=lambda p: "SHOWN")
    assert run.settle(wait(inbox)) == "SHOWN"


# --- the guard, wired in ----------------------------------------------------


def test_a_reply_the_guard_empties_is_kept_and_not_shown():
    """Discarding it would hide that it happened, and how often it happens is
    what you would want to know before trusting any of it."""
    run, inbox = runner(ok({"read": "Bid up to $40"}))
    log = ReadLog()

    run.submit("room", {}, moment="m",
               check=lambda p: ({}, ["read contains a verdict"]),
               show=lambda p: "SHOWN")
    text = run.settle(wait(inbox), log=log)

    assert text == ""
    assert log.all()[-1].status == "rejected"
    assert "verdict" in log.all()[-1].detail


def test_a_partly_clamped_reply_still_shows_what_survived():
    """Dropping one impossible estimate must not throw away three good ones."""
    run, inbox = runner(ok({"read": "ok", "rivals": [1, 2, 3]}))
    run.submit("room", {}, moment="m",
               check=lambda p: ({"read": "ok", "rivals": [1, 2]}, ["dropped 3"]),
               show=lambda p: f"rivals={len(p['rivals'])}")

    assert run.settle(wait(inbox)) == "rivals=2"


# --- muting -----------------------------------------------------------------


def test_three_failures_in_a_row_mute_with_a_reason():
    run, inbox = runner(lambda *a, **k: (_ for _ in ()).throw(AssistError("boom")))
    for _ in range(3):
        run.submit("room", {}, moment="m")
        run.settle(wait(inbox))

    assert run.muted
    assert "arithmetic" in run.muted_reason, "muting must say the draft is fine"
    assert run.submit("room", {}, moment="m") is False


def test_a_success_resets_the_failure_count():
    """Two blips an hour apart are not a broken setup."""
    replies = [AssistError("a"), AssistError("b"), None, AssistError("c")]

    def flaky(*a, **k):
        item = replies.pop(0)
        if item:
            raise item
        return FakeReply({"read": "fine"})

    run, inbox = runner(flaky)
    for _ in range(4):
        run.submit("room", {}, moment="m")
        run.settle(wait(inbox))

    assert not run.muted


def test_a_fatal_failure_mutes_at_once_without_waiting_for_three():
    """A bad key will not fix itself, and rediscovering it 180 times is waste."""
    run, inbox = runner(
        lambda *a, **k: (_ for _ in ()).throw(
            AssistError("the API rejected the key", fatal_to_assist=True))
    )
    run.submit("room", {}, moment="m")
    run.settle(wait(inbox))

    assert run.muted
    assert "rejected the key" in run.muted_reason


def test_the_spend_cap_mutes_and_says_how_to_raise_it():
    # One fake call prices at 2,750 micros ($0.00275) on Opus 5, so this cap is
    # under a single read. It used to be 0.01 and passed only because whole-cent
    # rounding turned 0.275c into a full penny.
    run, inbox = runner(
        ok(),
        spend=SpendGuard(cap_dollars=0.002),
    )
    run.submit("room", {}, moment="m")
    run.settle(wait(inbox))

    assert run.muted
    assert "--assist-budget" in run.muted_reason
    assert run.submit("room", {}, moment="m") is False


def test_spend_accumulates_from_real_usage():
    run, inbox = runner(ok())
    for _ in range(3):
        run.submit("room", {}, moment="m")
        run.settle(wait(inbox))

    assert run.spend.spent_micros > 0


# --- the ledger -------------------------------------------------------------


def test_every_outcome_is_counted_by_status():
    run, inbox = runner(ok())
    run.submit("room", {}, moment="m")
    run.settle(wait(inbox))
    assert run.counts() == {"ok": 1}


def test_usage_carries_a_price_the_ledger_can_read_back():
    run, inbox = runner(ok())
    log = ReadLog()
    run.submit("room", {}, moment="m")
    run.settle(wait(inbox), log=log)

    assert log.all()[-1].usage["cents"] >= 0
    assert log.all()[-1].model == "claude-opus-5"


def test_nothing_is_submitted_once_muted():
    run, inbox = runner(ok())
    run.mute("stopped for a reason")
    assert run.submit("room", {}, moment="m") is False
    assert inbox.empty()


def test_the_first_mute_reason_is_the_one_kept():
    """The first cause is the useful one; later failures are its consequences."""
    run, _ = runner(ok())
    run.mute("first")
    run.mute("second")
    assert run.muted_reason == "first"


# --- what the workers may touch ---------------------------------------------


def test_the_worker_never_sees_live_state():
    """Everything is captured at submit time. A worker reading live state would
    describe a board that had already moved on."""
    payload = {"nomination": {"name": "Bijan"}}
    seen: list[str] = []

    def capture(agent, system, user, **k):
        seen.append(user)
        return FakeReply({"read": "x"})

    run, inbox = runner(capture)
    run.submit("room", payload, moment="m")
    payload["nomination"]["name"] = "Someone else"    # the board moves on
    wait(inbox)

    assert "Bijan" in seen[0]


def test_the_prefix_is_the_cached_first_block():
    blocks: list = []

    def capture(agent, system, user, **k):
        blocks.append(system)
        return FakeReply({"read": "x"})

    run, inbox = runner(capture)
    run.submit("room", {}, moment="m")
    wait(inbox)

    assert blocks[0][0]["text"] == "PREFIX"
    assert blocks[0][0]["cache_control"]["type"] == "ephemeral"


def test_the_ledger_reads_the_key_the_runner_writes():
    """These were `cents` and `cost_cents` and disagreed silently. The spend cap
    is fed separately by settle(), so it kept working and only the reported
    total was wrong — a number on screen that was always $0.00.

    Written against the runner's real output rather than a literal, so renaming
    the key in one place breaks this instead of the summary line."""
    run, inbox = runner(ok())
    log = ReadLog()
    run.submit("room", {}, moment="m")
    run.settle(wait(inbox), log=log)

    written = log.all()[-1].usage
    assert ReadLog.COST_KEY in written
    assert log.spend_micros() == written[ReadLog.COST_KEY] > 0
    assert log.spend_micros() == run.spend.spent_micros
    # Both units are written, and they are not the same number. `cents` is for
    # the one line a person reads; summing it is how the cap read 1.4x high.
    assert "cents" in written
