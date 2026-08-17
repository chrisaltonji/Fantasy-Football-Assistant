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


# --- with reference data loaded -----------------------------------------------


@pytest.fixture
def book():
    from ffa.reference.playerbook import load_playerbook

    fixture = Path(__file__).resolve().parents[2] / "data" / "fixtures" / "sample_rankings.csv"
    return load_playerbook(fixture, teams=12, budget=200, is_sample=True)


def drive_with_book(store, script: str, book) -> str:
    out = io.StringIO()
    run_repl(store, stdin=io.StringIO(script), stdout=out, now_fn=lambda: at(0), book=book)
    return out.getvalue()


def top_players(book, n=3):
    return sorted(book.rows, key=lambda r: r.overall_rank or 9999)[:n]


def test_sample_data_is_announced_loudly(run_path, init_event, book):
    """Invented values are worse than no values if you don't know they're invented."""
    with DraftStore.create(run_path, init_event) as store:
        output = drive_with_book(store, "quit\n", book)
    assert "USING SAMPLE DATA" in output and "INVENTED" in output


def test_a_surname_resolves_to_the_full_player(run_path, init_event, book):
    player = top_players(book, 1)[0]
    surname = player.name.split()[-1]
    with DraftStore.create(run_path, init_event) as store:
        output = drive_with_book(store, f"sold {surname} 45 t3\nquit\n", book)
        assert store.state.players[player.key].sold

    # The readout shows the canonical name, not the two letters that were typed.
    assert player.name in output


def test_the_literal_command_survives_in_the_journal(run_path, init_event, book):
    """`raw` becomes the canonical name, so `note` is what preserves the input."""
    player = top_players(book, 1)[0]
    surname = player.name.split()[-1]
    with DraftStore.create(run_path, init_event) as store:
        drive_with_book(store, f"sold {surname} 45 t3\nquit\n", book)
        assert store.events[-1].note == f"sold {surname} 45 t3"


def test_position_is_filled_in_from_the_sheet(run_path, init_event, book):
    player = top_players(book, 1)[0]
    with DraftStore.create(run_path, init_event) as store:
        drive_with_book(store, f"sold {player.name} 45 t3\nquit\n", book)
        assert store.state.players[player.key].position.value is player.position


def test_an_ambiguous_name_is_refused_and_nothing_is_written(run_path, init_event, book):
    with DraftStore.create(run_path, init_event) as store:
        output = drive_with_book(store, "sold bram 45 t3\nquit\n", book)
        assert len(store.events) == 1  # only DraftInitialized

    assert "could be more than one player" in output
    assert "Type more of the name" in output


def test_a_player_absent_from_the_sheet_is_still_recordable(run_path, init_event, book):
    """Deep sleepers and late adds are normal; the tool must not block."""
    with DraftStore.create(run_path, init_event) as store:
        drive_with_book(store, "sold Some Undrafted Rookie 2 t3\nquit\n", book)
        assert len(store.state.sold_players()) == 1


def test_nomination_auto_fires_the_bid_readout(run_path, init_event, book):
    """Capability 2: it fires on nomination, not on request."""
    player = top_players(book, 1)[0]
    with DraftStore.create(run_path, init_event) as store:
        output = drive_with_book(store, f"nominate {player.name}\nquit\n", book)

    assert "sheet value" in output
    assert "bid up to" in output
    assert "who can take him" in output


def test_advice_works_for_a_player_who_is_not_nominated(run_path, init_event, book):
    player = top_players(book, 2)[1]
    with DraftStore.create(run_path, init_event) as store:
        output = drive_with_book(store, f"advice {player.name}\nquit\n", book)
    assert player.name in output and "legal max" in output


def test_scarcity_and_market_views(run_path, init_event, book):
    with DraftStore.create(run_path, init_event) as store:
        output = drive_with_book(store, "scarcity\nmarket\nquit\n", book)
    assert "ELITE" in output and "STARTABLE" in output
    assert "not enough priced sales yet" in output


def test_inflation_appears_once_enough_has_sold(run_path, init_event, book):
    players = top_players(book, 8)
    script = "\n".join(
        f"sold {p.name} {round((book.value(p.key) or 1) * 1.2)} t{(i % 12) + 1}"
        for i, p in enumerate(players)
    )
    with DraftStore.create(run_path, init_event) as store:
        output = drive_with_book(store, script + "\nmarket\nquit\n", book)
    assert "inflated" in output


def test_advice_degrades_cleanly_with_no_reference_data(run_path, init_event):
    with DraftStore.create(run_path, init_event) as store:
        output = drive_with_book(store, "scarcity\nquit\n", None)
    assert "no reference data" in output


def test_incremental_state_always_matches_a_full_replay(run_path, init_event):
    with DraftStore.create(run_path, init_event) as store:
        drive(store, "\n".join([
            "sold mahomes 45 t3", "sold barkley 62 t1", "undo",
            "sold chase 30 t3", "price chase 33", "redo", "quit",
        ]))
        from_disk, _ = read_events(store.journal_path)
        assert store.state == replay(from_disk, store.state.draft_id)
