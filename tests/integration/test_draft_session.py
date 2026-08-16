"""End-to-end draft sessions, driven from a transcript.

The REPL reads any `TextIO`, so a whole annotated session is a test input. No
pty, no subprocess, no mocking of the terminal.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from ffa.cli.repl import run_repl
from ffa.domain.codec import dump_event
from ffa.domain.projections import max_legal_bid, remaining_budget
from ffa.domain.reducers import replay
from ffa.state.journal import read_events
from ffa.state.store import DraftStore
from tests.conftest import at


def drive(store, script: str, *, now=None) -> str:
    out = io.StringIO()
    run_repl(store, stdin=io.StringIO(script), stdout=out, now_fn=now or (lambda: at(0)))
    return out.getvalue()


@pytest.fixture
def run_path(tmp_path: Path) -> Path:
    return tmp_path / "runs" / "2026-123456-20260816T190000"


def test_the_cp2_demo(run_path, init_event):
    """The exact demo from the build plan."""
    with DraftStore.create(run_path, init_event) as store:
        output = drive(store, "\n".join([
            "sold mahomes 45 t3",
            "sold barkley 62 t1",
            "undo",
            "budgets",
            "quit",
        ]))
        state = store.state

    assert "#2 mahomes -> Team 3 $45" in output
    assert "#3 barkley -> Team 1 $62" in output
    assert "#4 undid #3" in output

    # team3 keeps mahomes; team1's pick was undone.
    assert remaining_budget(state, 3) == 155
    assert max_legal_bid(state, 3) == 141
    assert remaining_budget(state, 1) == 200
    assert max_legal_bid(state, 1) == 185
    assert "$155" in output and "$141" in output


def test_resume_reproduces_state_exactly(run_path, init_event):
    """Frozen dataclasses all the way down give this a plain `==`."""
    with DraftStore.create(run_path, init_event) as store:
        drive(store, "sold mahomes 45 t3\nsold barkley 62 t1\nprice barkley 60\nquit\n")
        original = store.state

    with DraftStore.resume(run_path) as resumed:
        assert resumed.state == original
        assert remaining_budget(resumed.state, 1) == 140


def test_resume_is_byte_identical_on_rewrite(run_path, init_event):
    """The codec is a true bijection on the wire format.

    Without this, a resumed session could append lines in a subtly different
    format than the ones already in the file.
    """
    with DraftStore.create(run_path, init_event) as store:
        drive(store, "\n".join([
            "nominate ceedee lamb",
            "sold ceedee lamb 55 t2 wr",
            "sold mahomes ? t3",
            "price mahomes 45",
            "amend team:3 manager dave",
            "sold barkley 62 dave",
            "undo",
            "quit",
        ]))

    journal = store.journal_path
    original_bytes = journal.read_bytes()
    events, _ = read_events(journal)
    rewritten = ("\n".join(dump_event(e) for e in events) + "\n").encode("utf-8")
    assert rewritten == original_bytes


def test_partial_fallback_price_arrives_after_the_sale(run_path, init_event):
    """The flagship path, end to end: sale first, price later."""
    with DraftStore.create(run_path, init_event) as store:
        output = drive(store, "sold mahomes ? t3\nbudgets t3\nprice mahomes 45\nbudgets t3\nquit\n")
        state = store.state

    # Before the price lands, remaining is a floor, flagged with `+`.
    assert "$199+" in output
    assert remaining_budget(state, 3) == 155
    assert state.players["mahomes"].price.value == 45


def test_crash_mid_write_recovers_and_keeps_appending(run_path, init_event):
    with DraftStore.create(run_path, init_event) as store:
        drive(store, "sold mahomes 45 t3\nquit\n")

    journal = run_path / "events.jsonl"
    journal.write_text(journal.read_text() + '{"id": 3, "at": "2026-08-16T19:0', encoding="utf-8")

    with DraftStore.resume(run_path) as resumed:
        assert any("interrupted write" in w for w in resumed.load_warnings)
        drive(resumed, "sold barkley 62 t1\nquit\n")
        assert remaining_budget(resumed.state, 1) == 138

    events, warnings = read_events(journal)
    assert [e.id for e in events] == [1, 2, 3]
    assert warnings == []


def test_eof_exits_cleanly_with_everything_saved(run_path, init_event):
    with DraftStore.create(run_path, init_event) as store:
        code = run_repl(
            store, stdin=io.StringIO("sold mahomes 45 t3\n"),
            stdout=io.StringIO(), now_fn=lambda: at(0),
        )
        assert code == 0

    events, _ = read_events(run_path / "events.jsonl")
    assert len(events) == 2


def test_errors_do_not_write_anything(run_path, init_event):
    with DraftStore.create(run_path, init_event) as store:
        output = drive(store, "\n".join([
            "sould mahomes 45 t3",
            "sold barkley 62",
            "sold chase 55 nobody",
            "undo",
            "quit",
        ]))
        assert len(store.events) == 1  # only DraftInitialized

    assert "Did you mean 'sold'" in output
    assert "no team matches" in output
    assert "nothing to undo" in output


def test_a_transcript_with_comments_replays(run_path, init_event):
    with DraftStore.create(run_path, init_event) as store:
        drive(store, "\n".join([
            "# first round",
            "sold mahomes 45 t3   # overpaid",
            "",
            "sold barkley 62 t1",
            "quit",
        ]))
        assert len(store.events) == 3


def test_overspend_warns_but_records(run_path, init_event):
    with DraftStore.create(run_path, init_event) as store:
        output = drive(store, "sold superstar 250 t3\nquit\n")
        assert store.state.players["superstar"].sold

    assert "over budget" in output


def test_undo_then_redo_restores_the_pick(run_path, init_event):
    with DraftStore.create(run_path, init_event) as store:
        drive(store, "sold mahomes 45 t3\nundo\nredo\nquit\n")
        assert remaining_budget(store.state, 3) == 155


def test_log_shows_ids_and_marks_undone_events(run_path, init_event):
    with DraftStore.create(run_path, init_event) as store:
        output = drive(store, "sold mahomes 45 t3\nsold barkley 62 t1\nundo\nlog\nquit\n")

    assert "#2" in output and "#3" in output
    assert "sold mahomes -> team3 $45" in output


def test_state_view_shows_a_dangling_amendment(run_path, init_event):
    """Undo doesn't cascade, so the later amendment survives — visibly."""
    with DraftStore.create(run_path, init_event) as store:
        output = drive(store, "\n".join([
            "sold mahomes 45 t3",
            "price mahomes 50",
            "undo #2",
            "state",
            "quit",
        ]))

    assert "dangling" in output
    assert remaining_budget(store.state, 3) == 200


def test_incremental_state_always_matches_a_full_replay(run_path, init_event):
    with DraftStore.create(run_path, init_event) as store:
        drive(store, "\n".join([
            "sold mahomes 45 t3", "sold barkley 62 t1", "undo",
            "sold chase 30 t3", "price chase 33", "redo", "quit",
        ]))
        from_disk, _ = read_events(store.journal_path)
        assert store.state == replay(from_disk, store.state.draft_id)
