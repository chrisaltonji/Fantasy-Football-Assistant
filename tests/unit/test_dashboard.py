"""The dashboard's read-only window onto a running draft.

The visual half is verified by looking at it. What is tested here is the half
that can regress silently: that this never becomes a second writer, that it
degrades instead of failing, and that it serves the same payload every other
surface reads.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from ffa.dashboard.server import StateReader, serve
from ffa.state.store import DraftStore
from tests.conftest import sold


@pytest.fixture
def run(tmp_path, init_event):
    """A real journal, written the way the REPL writes one, with the writer then
    closed so a test is free to reopen it and append."""
    directory = tmp_path / "run"
    store = DraftStore.create(directory, init_event)
    store.dispatch(sold(2, "Bijan Robinson", 4, 60, seconds=1))
    store.close()
    return directory


def append(directory, event):
    """Write one more pick, the way the live draft would."""
    store = DraftStore.resume(directory)
    try:
        store.dispatch(event)
    finally:
        store.close()


# --- the property the whole design rests on ---------------------------------------------


def test_reading_a_draft_never_becomes_a_second_writer(run, init_event, tmp_path):
    """The engine's crash-safety and undo both come from one writer. A dashboard
    that took the lock would either block the draft or corrupt journal ordering,
    so it goes nowhere near `DraftStore` — and the proof is that the real writer
    can still open the same run while the reader is working."""
    reader = StateReader(run)
    reader.view()

    # The lock is still free: a writer can take it, which it could not do if the
    # dashboard had opened a store of its own.
    append(run, sold(3, "Ja'Marr Chase", 5, 55, seconds=2))

    assert len(reader.view()["teams"]) == len(init_event.teams)


def test_it_sees_picks_that_land_after_it_started(run):
    """A dashboard that cached the board forever would quietly stop being a
    dashboard, and look identical while doing it."""
    reader = StateReader(run)
    before = len(reader.view()["recent_sales"])

    append(run, sold(3, "Ja'Marr Chase", 5, 55, seconds=2))

    assert len(reader.view()["recent_sales"]) == before + 1


def test_an_unchanged_journal_is_not_replayed_again(run, monkeypatch):
    """~40 ms of advisory work per poll is nothing against a nomination clock
    and pure waste once a second on an idle draft."""
    reader = StateReader(run)
    reader.view()

    from ffa.domain import reducers

    calls = {"n": 0}
    real = reducers.replay

    def counted(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(reducers, "replay", counted)
    reader.view()
    reader.view()

    assert calls["n"] == 0, "the journal did not change; nothing should be replayed"


# --- degrading rather than failing ------------------------------------------------------


def test_no_journal_yet_is_a_state_not_an_error(tmp_path):
    """`ffa dashboard` before `ffa draft` is an ordinary thing to do, and the
    page has a rendering for it."""
    view = StateReader(tmp_path / "nothing-here").view()

    assert view["initialized"] is False
    assert view["warnings"]


def test_a_torn_trailing_line_does_not_take_the_board_down(run):
    """The exact shape of a concurrent read: the writer is mid-append. The
    journal's tolerance for this exists for crash-resume, and it is the same
    tolerance that makes reading a live file sound."""
    journal = run / "events.jsonl"
    with journal.open("a", encoding="utf-8") as fh:
        fh.write('{"id": 99, "type": "PlayerSold", "pla')

    view = StateReader(run).view()

    assert view["initialized"] is True
    assert any("line" in w.lower() or "trailing" in w.lower() for w in view["warnings"])


# --- the HTTP surface -------------------------------------------------------------------


@pytest.fixture
def server(run):
    reader = StateReader(run)
    srv = serve(reader, port=0, interval=1.0)
    yield srv, srv.server_address[1]
    srv.shutdown()


def get(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as res:
        return res.status, res.read()


def test_it_serves_the_page_and_the_same_payload_every_surface_reads(server):
    _, port = server

    status, body = get(port, "/")
    assert status == 200 and b"<!doctype html>" in body[:40].lower()

    status, body = get(port, "/state.json")
    view = json.loads(body)
    assert status == 200
    # The contract, not a bespoke shape: this is `build_view` verbatim.
    for key in ("schema_version", "league", "me", "teams", "market", "scarcity"):
        assert key in view


def test_the_poll_interval_is_told_to_the_page_rather_than_hardcoded_twice(server):
    _, port = server
    status, body = get(port, "/meta.json")
    assert status == 200
    assert json.loads(body)["poll_interval"] == 1.0


def test_an_unknown_path_is_a_plain_404(server):
    _, port = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(port, "/../../etc/passwd")
    assert excinfo.value.code == 404


def test_a_failing_read_still_answers(run, monkeypatch):
    """A blank dashboard mid-draft is worse than a stale one, so a read that
    blows up becomes a payload the page can render rather than a dead socket."""
    reader = StateReader(run)

    def boom():
        raise RuntimeError("disk went away")

    monkeypatch.setattr(reader, "view", boom)
    srv = serve(reader, port=0, interval=1.0)
    try:
        status, body = get(srv.server_address[1], "/state.json")
        payload = json.loads(body)
        assert status == 200
        assert payload["initialized"] is False
        assert any("disk went away" in w for w in payload["warnings"])
    finally:
        srv.shutdown()
