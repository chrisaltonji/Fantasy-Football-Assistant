from __future__ import annotations

from pathlib import Path

import pytest

from ffa.domain.codec import dump_event
from ffa.state.journal import Journal, JournalError, read_events
from ffa.state.recovery import make_draft_id, repair_torn_tail
from tests.conftest import sold


def write_journal(path: Path, events, *, trailing_newline: bool = True) -> Path:
    text = "\n".join(dump_event(e) for e in events)
    path.write_text(text + ("\n" if trailing_newline else ""), encoding="utf-8")
    return path


def test_the_journal_is_lf_on_every_platform(tmp_path: Path, init_event):
    """The wire format is LF, not the platform separator.

    Python's text mode rewrites "\\n" to "\\r\\n" on Windows, which broke the
    codec's byte-identical round-trip contract and made a journal written on
    Windows differ from the same draft written on Linux. The journal is the
    draft and is meant to be a portable audit artifact, so this is pinned.
    """
    path = tmp_path / "events.jsonl"
    with Journal.open(path) as journal:
        journal.append(init_event)
        journal.append(sold(0, "mahomes", 3, 45))

    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.count(b"\n") == 2
    assert raw.endswith(b"}\n")


def test_a_resumed_journal_round_trips_byte_identically(tmp_path: Path, init_event):
    """Reopening and appending must not switch line endings mid-file."""
    path = tmp_path / "events.jsonl"
    with Journal.open(path) as journal:
        journal.append(init_event)
    with Journal.open(path) as journal:
        journal.append(sold(0, "mahomes", 3, 45))

    events, warnings = read_events(path)
    assert warnings == []
    rebuilt = ("\n".join(dump_event(e) for e in events) + "\n").encode("utf-8")
    assert rebuilt == path.read_bytes()


def test_empty_and_missing_files_are_not_errors(tmp_path: Path):
    assert read_events(tmp_path / "nope.jsonl") == ([], [])
    empty = tmp_path / "events.jsonl"
    empty.write_text("")
    assert read_events(empty) == ([], [])


def test_append_stamps_dense_increasing_ids(tmp_path: Path, init_event):
    with Journal.open(tmp_path / "events.jsonl") as journal:
        first = journal.append(init_event)
        second = journal.append(sold(0, "mahomes", 3, 45))
        third = journal.append(sold(0, "barkley", 1, 62))
    assert [first.id, second.id, third.id] == [1, 2, 3]


def test_reopening_continues_the_id_sequence(tmp_path: Path, init_event):
    path = tmp_path / "events.jsonl"
    with Journal.open(path) as journal:
        journal.append(init_event)
        journal.append(sold(0, "mahomes", 3, 45))
    with Journal.open(path) as journal:
        assert journal.append(sold(0, "barkley", 1, 62)).id == 3


def test_append_stamps_a_timestamp_when_the_event_has_none(tmp_path: Path, init_event):
    with Journal.open(tmp_path / "events.jsonl") as journal:
        assert journal.append(type(init_event)(**{**init_event.__dict__, "at": None})).at


# --- the torn-tail / corrupt-middle asymmetry --------------------------------


def test_torn_trailing_line_is_dropped_with_a_warning(tmp_path: Path, init_event):
    path = write_journal(tmp_path / "events.jsonl", [init_event, sold(2, "mahomes", 3, 45)])
    path.write_text(path.read_text() + '{"id": 3, "at": "2026-08-16T19:0', encoding="utf-8")

    events, warnings = read_events(path)
    assert len(events) == 2
    assert any("incomplete final line" in w for w in warnings)


def test_corrupt_middle_line_raises_with_a_line_number(tmp_path: Path, init_event):
    """Silently skipping a middle event would yield wrong-but-plausible state,
    which is strictly worse than refusing to start."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        dump_event(init_event) + "\n" + "{ garbage }\n" + dump_event(sold(3, "x", 1, 5)) + "\n"
    )
    with pytest.raises(JournalError, match=r":2 is corrupt"):
        read_events(path)


def test_a_complete_but_unparseable_last_line_is_still_treated_as_torn(tmp_path: Path, init_event):
    path = tmp_path / "events.jsonl"
    path.write_text(dump_event(init_event) + "\n{ garbage }")
    events, warnings = read_events(path)
    assert len(events) == 1 and warnings


def test_repair_truncates_the_fragment_and_backs_it_up(tmp_path: Path, init_event):
    path = write_journal(tmp_path / "events.jsonl", [init_event])
    path.write_text(path.read_text() + '{"id": 2, "partial', encoding="utf-8")

    backup = repair_torn_tail(path)
    assert backup is not None and backup.exists()
    assert path.read_text().endswith("\n")
    assert read_events(path)[1] == []  # no lingering warning on the next load


def test_repair_is_a_noop_on_a_clean_file(tmp_path: Path, init_event):
    path = write_journal(tmp_path / "events.jsonl", [init_event])
    assert repair_torn_tail(path) is None


def test_next_append_after_repair_lands_on_a_clean_line(tmp_path: Path, init_event):
    path = write_journal(tmp_path / "events.jsonl", [init_event])
    path.write_text(path.read_text() + '{"id": 2, "partial', encoding="utf-8")
    repair_torn_tail(path)

    with Journal.open(path) as journal:
        journal.append(sold(0, "mahomes", 3, 45))
    events, _ = read_events(path)
    assert [e.id for e in events] == [1, 2]


# --- id sanity ---------------------------------------------------------------


def test_non_increasing_ids_are_rejected_as_two_writers(tmp_path: Path, init_event):
    path = tmp_path / "events.jsonl"
    path.write_text(
        dump_event(init_event) + "\n" + dump_event(sold(1, "dup", 1, 5)) + "\n"
    )
    with pytest.raises(JournalError, match="two processes"):
        read_events(path)


def test_draft_id_is_sortable_and_readable():
    from datetime import datetime, timezone

    moment = datetime(2026, 8, 16, 19, 4, 22, tzinfo=timezone.utc)
    assert make_draft_id(123456, 2026, moment) == "2026-123456-20260816T190422"
