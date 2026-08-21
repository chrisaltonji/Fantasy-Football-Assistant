"""The shared context store — and the promise that it is not the journal.

It lives in the run directory beside `events.jsonl`, which is exactly how a
future reader talks themselves into trusting it. The load-bearing test here is
the last one: the same journal replays byte-identically with the sidecar present,
absent, and full of garbage. Losing inference costs nothing. That asymmetry is
the design, and it only holds while something checks it.
"""

from __future__ import annotations

import json

import pytest

from ffa.assist.context import (
    ReadLog,
    ReadRecord,
    moment_key,
    records_for_prompt,
)
from ffa.domain.enums import Position
from ffa.domain.reducers import replay
from ffa.state.store import DraftStore
from tests.conftest import sold


def read(agent="room", player="bijan-robinson", event_id=7, **kw) -> ReadRecord:
    return ReadRecord(
        agent=agent, moment=moment_key(agent, player, event_id),
        event_id=event_id, player_key=player, at="2026-08-31T20:04:00Z",
        payload=kw.pop("payload", {"read": "…"}), **kw,
    )


# --- append and look up -----------------------------------------------------


def test_append_assigns_a_dense_sequence():
    log = ReadLog()
    first = log.append(read(player="a"))
    second = log.append(read(player="b"))

    assert (first.seq, second.seq) == (1, 2)
    assert len(log.all()) == 2


def test_a_later_agent_can_find_an_earlier_read():
    """The whole reason this exists: the Narrator needs what the Room said."""
    log = ReadLog()
    log.append(read(player="bijan-robinson", payload={"rivals": [{"lo": 31, "hi": 39}]}))

    found = log.latest("room", "bijan-robinson")
    assert found is not None
    assert found.payload["rivals"][0]["hi"] == 39


def test_lookup_ignores_failures_and_rejections():
    """Handing on a rejected read would launder exactly what the guard refused."""
    log = ReadLog()
    log.append(read(payload={"read": "good"}))
    log.append(read(status="rejected", payload={"read": "Bid up to $40"}))
    log.append(read(status="failed", detail="timed out"))

    assert log.latest("room", "bijan-robinson").payload["read"] == "good"


def test_lookup_is_scoped_by_agent_and_player():
    log = ReadLog()
    log.append(read(agent="room", player="a"))
    log.append(read(agent="narrator", player="a"))

    assert log.latest("room", "a").agent == "room"
    assert log.latest("room", "b") is None
    assert len(log.for_player("a")) == 2
    assert len(log.for_player("a", agent="room")) == 1


def test_since_returns_only_what_is_new():
    log = ReadLog()
    log.append(read(player="a"))
    mark = log.all()[-1].seq
    log.append(read(player="b"))

    assert [r.player_key for r in log.since(mark)] == ["b"]


# --- accounting -------------------------------------------------------------


def test_spend_and_counts_are_summed_from_the_records():
    log = ReadLog()
    log.append(read(usage={"cost_cents": 3}))
    log.append(read(usage={"cost_cents": 4}, status="late"))
    log.append(read(status="failed"))

    assert log.spend_cents() == 7
    assert log.counts() == {"ok": 1, "late": 1, "failed": 1}


def test_cache_hit_rate_is_worth_surfacing():
    """Zero after a few calls means something volatile leaked into the prefix and
    every nomination is paying full price for tokens that should be free."""
    log = ReadLog()
    log.append(read(usage={"cache_read": 0, "input": 5000}))
    assert log.cache_hit_rate() == 0.0

    log.append(read(usage={"cache_read": 5000, "input": 1800}))
    assert 0.4 < log.cache_hit_rate() < 0.6


def test_an_empty_log_has_no_opinion_about_its_hit_rate():
    assert ReadLog().cache_hit_rate() == 0.0


# --- the sidecar ------------------------------------------------------------


def test_a_sidecar_round_trips(tmp_path):
    path = tmp_path / "assist.jsonl"
    log = ReadLog(path)
    log.append(read(player="a", payload={"read": "one"}))
    log.append(read(player="b", payload={"read": "two"}))

    back = ReadLog.load(path)
    assert [r.player_key for r in back.all()] == ["a", "b"]
    assert back.all()[1].payload["read"] == "two"


def test_a_corrupt_line_is_skipped_and_named_rather_than_raising(tmp_path):
    """Deliberately unlike `read_events`, which raises on a bad middle line
    because a wrong-but-plausible draft state is worse than refusing to start.
    Nothing here is load-bearing, so salvaging what parses is the useful answer."""
    path = tmp_path / "assist.jsonl"
    log = ReadLog(path)
    log.append(read(player="a"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    log2 = ReadLog(path)
    log2.append(read(player="c"))

    back = ReadLog.load(path)
    assert [r.player_key for r in back.all()] == ["a", "c"]
    assert any("not JSON" in w for w in back.warnings)


def test_a_missing_sidecar_is_an_empty_log_not_an_error(tmp_path):
    back = ReadLog.load(tmp_path / "nothing-here.jsonl")
    assert back.all() == ()
    assert back.warnings == []


def test_an_unwritable_path_costs_one_warning_and_nothing_else(tmp_path):
    """A failing disk must not print a line per nomination while you are trying
    to read a bid."""
    blocked = tmp_path / "assist.jsonl"
    blocked.mkdir()                       # a directory where a file should be

    log = ReadLog(blocked)
    for _ in range(5):
        log.append(read())

    assert len(log.all()) == 5            # the draft's copy is unaffected
    assert len(log.warnings) == 1


def test_unknown_fields_do_not_break_an_older_reader(tmp_path):
    path = tmp_path / "assist.jsonl"
    path.write_text(json.dumps({
        "agent": "room", "moment": "room:x:1", "event_id": 1,
        "player_key": "x", "payload": {}, "something_new": 42,
    }) + "\n", encoding="utf-8")

    assert ReadLog.load(path).all()[0].agent == "room"


# --- the promise ------------------------------------------------------------


@pytest.mark.parametrize("sidecar", ["absent", "present", "garbage"])
def test_the_draft_replays_identically_whatever_the_sidecar_says(
    tmp_path, init_event, sidecar
):
    """The load-bearing test. `events.jsonl` is ground truth; `assist.jsonl` is
    opinions living next door. Nothing in state/ or domain/ may read it, and the
    proof is that the draft cannot tell the difference."""
    run = tmp_path / "run"
    store = DraftStore.create(run, init_event)
    store.dispatch(sold(2, "Bijan Robinson", 4, 60, pos=Position.RB, seconds=1))
    events = list(store.events)
    store.close()

    path = run / "assist.jsonl"
    if sidecar == "present":
        ReadLog(path).append(read())
    elif sidecar == "garbage":
        path.write_text("\x00\x00 not even close\n" * 20, encoding="utf-8")

    state = replay(events, run.name)
    assert len(state.sold_players()) == 1
    assert state.players["bijan-robinson"].price.value == 60


# --- what gets handed to another agent --------------------------------------


def test_prior_reads_are_trimmed_before_they_reach_a_prompt():
    log = ReadLog()
    for i in range(15):
        log.append(read(player=f"p{i}"))
    log.append(read(player="bad", status="failed"))

    trimmed = records_for_prompt(log.all(), limit=5)

    assert len(trimmed) == 5
    assert all(set(t) == {"agent", "at", "player", "said"} for t in trimmed)
    assert trimmed[-1]["player"] == "p14"     # newest last, in reading order
