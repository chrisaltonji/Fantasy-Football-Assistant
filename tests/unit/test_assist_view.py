"""The `assist` key, and the dashboard's read of the sidecar.

Two properties are load-bearing here and everything else is detail:

- With the assistant off, the view payload is **byte-identical** to what every
  existing surface already reads.
- Only `ok` records reach a surface. `rejected` and `late` stay in the ledger
  because the Grader scores the assistant on them, but neither is something to
  put on screen as though it had stood.
"""

from __future__ import annotations

import json

from ffa.assist.context import ReadLog, ReadRecord
from ffa.assist.view import assist_view
from ffa.domain import reducers
from ffa.view.model import build_view


def record(**kw):
    base = dict(agent="room", moment="m", status="ok", player_key="bijan",
                payload={"read": "a thin room"},
                usage={"micros": 40_000, "cents": 4, "cache_read": 2000, "input": 100})
    base.update(kw)
    return ReadRecord(**base)


def log_with(*records) -> ReadLog:
    log = ReadLog()
    for r in records:
        log.append(r)
    return log


# --- the key itself ---------------------------------------------------------


def state_of(init_event):
    return reducers.replay([init_event])


def test_the_key_is_absent_entirely_when_the_assistant_is_off(init_event):
    """Not `{}` — absent. An empty object would change every existing
    consumer's payload the day this shipped, and would read as "the assistant
    had nothing to say" rather than "there is no assistant"."""
    assert "assist" not in build_view(state_of(init_event))


def test_turning_it_off_leaves_the_payload_byte_identical(init_event):
    state = state_of(init_event)
    without = build_view(state)
    also_without = build_view(state, assist={})

    without.pop("generated_at", None)
    also_without.pop("generated_at", None)
    assert json.dumps(without, sort_keys=True) == json.dumps(also_without, sort_keys=True)


def test_the_key_is_top_level_and_never_merged_into_a_team(init_event):
    """A read sitting inside a team object would inherit that object's
    authority. The whole point of the provenance split is that it cannot."""
    view = build_view(state_of(init_event),
                      assist={"current": {"payload": {"read": "x"}}})

    assert view["assist"]["current"]["payload"]["read"] == "x"
    for team in view.get("teams") or []:
        assert "assist" not in team and "read" not in team


# --- what a surface is allowed to see ---------------------------------------


def test_only_ok_records_reach_a_surface():
    log = log_with(
        record(status="ok", payload={"read": "stood"}),
        record(status="rejected", payload={"read": "Bid up to $40"}),
        record(status="late", payload={"read": "arrived after the gavel"}),
        record(status="failed"),
    )
    shown = json.dumps(assist_view(log)["recent"])

    assert "stood" in shown
    assert "Bid up to $40" not in shown
    assert "after the gavel" not in shown


def test_rejected_and_late_still_appear_in_the_counts():
    """They are hidden from the screen, not from the record. How often the guard
    fires is what you would want to know before trusting any of it."""
    log = log_with(record(status="ok"), record(status="rejected"),
                   record(status="late"))
    counts = assist_view(log)["counts"]

    assert counts == {"ok": 1, "rejected": 1, "late": 1}


def test_every_entry_is_labelled_as_inference():
    """It renders beside max_advisable_bid, which is arithmetic. Anything
    printed next to a computed number inherits its authority unless something
    actively prevents that."""
    view = assist_view(log_with(record()))
    assert all(e["is_inference"] is True for e in view["recent"])


def test_the_current_read_is_found_by_player_not_by_recency():
    """A read about the previous player must never be shown as though it were
    about whoever is on the block now."""
    log = log_with(record(player_key="bijan", payload={"read": "about bijan"}),
                   record(player_key="gibbs", payload={"read": "about gibbs"}))

    assert assist_view(log, player_key="bijan")["current"]["payload"]["read"] \
        == "about bijan"
    assert assist_view(log, player_key="nobody")["current"] is None


def test_a_late_read_is_never_the_current_one():
    log = log_with(record(player_key="bijan", status="late"))
    assert assist_view(log, player_key="bijan")["current"] is None


def test_no_log_means_no_key():
    assert assist_view(None) == {}


def test_recent_is_bounded():
    """180 entries is a column nobody scrolls."""
    log = log_with(*[record() for _ in range(40)])
    assert len(assist_view(log, recent=12)["recent"]) == 12


def test_muting_is_surfaced_rather_than_hidden():
    """A muted assistant that looks identical to a quiet one is the failure this
    whole layer is trying not to have."""
    class Runner:
        muted = True
        muted_reason = "spend cap reached"
        spend = type("S", (), {"cap_dollars": 25.0})()

    view = assist_view(log_with(record()), runner=Runner())
    assert view["muted"] is True
    assert "spend cap" in view["muted_reason"]


def test_spend_and_cache_rate_travel_with_it():
    """`cache_hit_rate` is the share of input *tokens* served from cache, not
    the share of calls — 2,000 cached against 100 fresh is 0.95, not 1.0. The
    summary line said "of calls" and meant tokens."""
    view = assist_view(log_with(record(), record()))

    assert view["spend_cents"] == 8
    assert view["spend_dollars"] == 0.08
    assert view["cache_hit_rate"] == round(2000 / 2100, 3)


# --- the dashboard's read of the sidecar ------------------------------------


def reader_over(tmp_path, records):
    """A StateReader over a run directory with a journal and a sidecar."""
    import json as _json

    from ffa.dashboard.server import StateReader

    run = tmp_path / "run"
    run.mkdir()
    (run / "events.jsonl").write_text("", encoding="utf-8")
    (run / "assist.jsonl").write_text(
        "".join(_json.dumps(r.as_dict()) + "\n" for r in records), encoding="utf-8")
    return StateReader(run), run


def test_a_read_landing_without_a_new_pick_is_noticed(tmp_path):
    """The sidecar grows on its own clock — a read lands seconds after the pick
    that triggered it. Keying the cache on the journal alone would leave every
    read invisible until the next pick happened to invalidate it."""
    import json as _json

    reader, run = reader_over(tmp_path, [record()])
    before = reader.view()

    with (run / "assist.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(_json.dumps(record(seq=2).as_dict()) + "\n")
    after = reader.view()

    assert after is not before, "the cache did not invalidate on a new read"


def test_a_corrupt_sidecar_does_not_take_the_board_down(tmp_path):
    """Nothing here is load-bearing. The arithmetic does not depend on any of
    it, so the board must render exactly as it would with no assistant."""
    reader, run = reader_over(tmp_path, [record()])
    (run / "assist.jsonl").write_text("{not json at all\n", encoding="utf-8")

    view = reader.view()
    assert "assist" not in view
    # Renders exactly as it would with no assistant at all — same keys, no
    # error, nothing about the sidecar leaking into the board's own vocabulary.
    assert view["schema_version"] == 1 and "initialized" in view


def test_a_missing_sidecar_is_a_shrug(tmp_path):
    reader, run = reader_over(tmp_path, [])
    (run / "assist.jsonl").unlink()
    assert "assist" not in reader.view()
