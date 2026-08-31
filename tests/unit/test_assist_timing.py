"""What the instrumentation has to get right to be worth trusting.

Latency is the one property of this layer that no amount of unit testing could
establish — you have to call the real API to learn it. What *can* be pinned is
that the numbers are measured from the right endpoints, survive a round trip
through the sidecar, and exclude the samples that would lie.

The distinction the whole file turns on: `latency_ms` is the API call,
`elapsed_ms` is from asking to having something to print. The second is always
the larger and is the one that decides whether a read made the auction.
"""

from __future__ import annotations

import json
import queue
import time

import pytest

from ffa.assist.budget import SpendGuard
from ffa.assist.context import ReadLog, ReadRecord
from ffa.assist.runner import AssistRunner
from ffa.assist.session import TICK_BUDGET_MS, AssistSession


class Reply:
    def __init__(self, latency_ms=0):
        self.payload = {"changed": False}
        self.usage = {"input": 10, "output": 5}
        self.model = "claude-haiku-4-5"
        self.stop_reason = "end_turn"
        self.latency_ms = latency_ms


def runner(delay=0.0, latency_ms=0):
    inbox: queue.Queue = queue.Queue()

    def complete(*a, **kw):
        if delay:
            time.sleep(delay)
        return Reply(latency_ms=latency_ms)

    return AssistRunner(complete, inbox=inbox, prefix="p",
                        spend=SpendGuard(cap_dollars=99)), inbox


def wait(inbox, timeout=5.0):
    return inbox.get(timeout=timeout)[1]


# --- the two clocks ---------------------------------------------------------


def test_elapsed_covers_more_than_the_api_call():
    """`elapsed_ms` starts at submit and ends with something to print, so it
    includes the slot wait, thread start-up, the parse and the guard. A clock
    started inside the worker would time the API and call it the latency."""
    run, inbox = runner(delay=0.05, latency_ms=10)
    run.submit("room_tick", {}, moment="m")
    record = wait(inbox).record

    assert record.latency_ms == 10
    assert record.elapsed_ms >= 50
    assert record.elapsed_ms > record.latency_ms


def test_a_read_that_waited_for_a_slot_wears_the_wait():
    """The failure this measurement exists to catch. A tick that answers in 3s
    but lands at 9s because it queued behind another still missed the auction,
    and only the total says so."""
    run, inbox = runner(delay=0.15)
    assert run.submit("room_tick", {}, moment="first")
    # The tick has one slot, so this is refused rather than queued — the
    # refusal is what keeps the wait bounded. Pinned here because the day it
    # starts queueing, this measurement is how you would find out.
    assert not run.submit("room_tick", {}, moment="second")

    first = wait(inbox).record
    assert first.elapsed_ms >= 150


def test_a_failed_call_is_not_timed_as_though_it_were_fast():
    """A worker that raised has no meaningful latency, and letting a 0ms failure
    into the sample would drag every percentile toward zero."""
    def boom(*a, **kw):
        raise RuntimeError("no")

    inbox: queue.Queue = queue.Queue()
    run = AssistRunner(boom, inbox=inbox, prefix="p")
    run.submit("room", {}, moment="m")
    record = wait(inbox).record

    assert record.status == "failed"

    log = ReadLog()
    log.append(record)
    assert log.timings("room") == {}


# --- the statistics ---------------------------------------------------------


def _log(*pairs, agent="room_tick", status="ok"):
    log = ReadLog()
    for api, total in pairs:
        log.append(ReadRecord(agent=agent, moment="m", status=status,
                              latency_ms=api, elapsed_ms=total))
    return log


def test_percentiles_are_measured_values_and_never_interpolated():
    """With twenty samples an interpolating percentile invents numbers that were
    never observed. These are measurements or they are nothing."""
    log = _log(*[(i * 100, i * 100) for i in range(1, 11)])
    t = log.timings("room_tick")

    assert t["n"] == 10
    assert t["p50"] in {i * 100 for i in range(1, 11)}
    assert t["max"] == 1000


def test_a_late_read_is_kept_in_the_sample():
    """A read that arrived after the player sold is precisely the evidence you
    want when asking whether the cadence works. Dropping it would make the tick
    look punctual by excluding every time it was not."""
    log = _log((100, 200), status="late")
    assert log.timings("room_tick")["n"] == 1


def test_a_rejected_read_says_nothing_about_speed_but_is_still_timed():
    """Rejected means the guard refused the content, not that the call was slow.

    It stays out of the timing sample for the same reason a failure does: the
    question here is how fast the model answers, and a reply that was thrown
    away for saying `bid` answered exactly as fast as one that was not. Counting
    it would be harmless; excluding it keeps the sample answering one question."""
    log = _log((100, 200), status="rejected")
    assert log.timings("room_tick") == {}


def test_timings_are_per_agent_because_the_agents_are_not_alike():
    """A 25s open read averaged with a 2s tick describes neither."""
    log = ReadLog()
    log.append(ReadRecord(agent="room", moment="m", latency_ms=12000, elapsed_ms=13000))
    log.append(ReadRecord(agent="room_tick", moment="m", latency_ms=1500, elapsed_ms=1700))

    assert log.timings("room")["p50"] == 13000
    assert log.timings("room_tick")["p50"] == 1700
    assert log.agents() == ("room", "room_tick")


def test_no_samples_is_an_empty_dict_not_a_row_of_zeroes():
    """A surface that rendered zeroes would say the assistant was instant."""
    assert ReadLog().timings("room") == {}


# --- the sidecar ------------------------------------------------------------


def test_the_timings_survive_a_round_trip_through_the_sidecar(tmp_path):
    """`ffa` writes this file so tonight can be read back tomorrow. A field that
    serialises and does not load is a measurement you only have once."""
    path = tmp_path / "assist.jsonl"
    log = ReadLog(path)
    log.append(ReadRecord(agent="room_tick", moment="m", latency_ms=1400,
                          elapsed_ms=1600))

    reloaded = ReadLog.load(path)
    assert reloaded.timings("room_tick")["p50"] == 1600
    assert reloaded.all()[0].latency_ms == 1400


def test_an_older_sidecar_without_timings_still_loads(tmp_path):
    """Tolerant on the way in, like every other reader here. A record written
    before this existed must not break a run that reads it."""
    path = tmp_path / "assist.jsonl"
    path.write_text(json.dumps({"agent": "room", "moment": "m", "status": "ok"}) + "\n",
                    encoding="utf-8")

    log = ReadLog.load(path)
    assert log.all()[0].elapsed_ms == 0
    assert log.timings("room") == {}          # not zero — absent


# --- the verdict line -------------------------------------------------------


def _session(log):
    return AssistSession(runner=AssistRunner(lambda *a, **k: None,
                                             inbox=queue.Queue(), prefix="p"),
                         log=log, build_view=dict)


def test_the_report_says_whether_the_tick_made_the_cadence():
    """The one line in the table that is a verdict rather than a measurement.

    The tick is offered a turn every five seconds; one that habitually answers
    slower is not following the auction, it is describing a price that moved."""
    fast = _session(_log(*[(1200, 1400)] * 10)).timing_report()
    slow = _session(_log(*[(7000, 7200)] * 10)).timing_report()

    assert "inside" in fast
    assert "OVER" in slow
    assert f"{TICK_BUDGET_MS / 1000:.0f}s cadence" in fast


def test_the_report_is_empty_rather_than_a_header_with_nothing_under_it():
    assert _session(ReadLog()).timing_report() == ""


def test_the_report_names_every_agent_that_ran():
    log = ReadLog()
    log.append(ReadRecord(agent="room", moment="m", latency_ms=1, elapsed_ms=12000))
    log.append(ReadRecord(agent="strategist", moment="m", latency_ms=1, elapsed_ms=9000))
    report = _session(log).timing_report()

    assert "room" in report and "strategist" in report
    # No tick ran, so no verdict is offered about one.
    assert "cadence" not in report
