"""The dev watcher.

A diagnostic, but one whose whole job is to be believable — if it renders
something other than what the production reader sees, watching it proves
nothing. So these tests pin that it goes through the same `parse_snapshot`, and
that it degrades rather than blanking when the browser goes away.

All offline: fixture mode needs no Chrome.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from ffa.ingest.espn.draftroom import DraftRoomError

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "draft_watch.py"
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures" / "espn" / "draftroom_snapshot_live.json"
)


def _load():
    spec = importlib.util.spec_from_file_location("draft_watch", TOOLS)
    module = importlib.util.module_from_spec(spec)
    sys.modules["draft_watch"] = module
    spec.loader.exec_module(module)
    return module


watch = _load()


@pytest.fixture
def watcher():
    return watch.Watcher(fixture=FIXTURE)


# --- the payload ------------------------------------------------------------


def test_a_read_carries_both_raw_and_parsed(watcher):
    """Both panels come from one read, so they cannot show different instants."""
    data = watcher.read()
    assert data["ok"] is True
    assert data["error"] is None
    assert data["raw"] is not None
    assert data["parsed"] is not None


def test_the_parsed_side_matches_the_production_parser(watcher):
    """The whole point: no private copy of the parsing logic.

    If the page rendered through its own parser, a convincing demo would say
    nothing about what the real reader does.
    """
    from ffa.ingest.espn.draftroom import parse_snapshot

    data = watcher.read()
    expected = parse_snapshot(json.loads(FIXTURE.read_text(encoding="utf-8")))

    assert data["parsed"]["filled"] == expected.filled
    assert data["parsed"]["slots_per_team"] == expected.slots_per_team
    assert len(data["parsed"]["teams"]) == len(expected.teams)
    assert [p["key"] for p in data["parsed"]["picks"]] == [p.key for p in expected.picks]


def test_the_payload_is_json_serializable(watcher):
    """It goes over HTTP; a dataclass that slipped through would 500 the poll."""
    json.dumps(watcher.read())


def test_absent_money_is_null_not_zero(watcher):
    """The `$null` trap, made visible rather than silently zeroed."""
    parsed = watcher.read()["parsed"]
    assert parsed["null_bids"] > 0, "fixture should contain $null bids"
    for team in parsed["teams"]:
        assert team["bid"] is None or team["bid"] > 0


def test_the_null_counters_are_surfaced(watcher):
    parsed = watcher.read()["parsed"]
    assert "null_bids" in parsed
    assert "unpriced_picks" in parsed


def test_picks_carry_the_key_the_engine_merges_on(watcher):
    parsed = watcher.read()["parsed"]
    pick = parsed["picks"][0]
    assert pick["key"] == pick["key"].lower()
    assert " " not in pick["key"]


# --- degradation ------------------------------------------------------------


class _Boom:
    """A reader that has lost the browser."""

    def __init__(self, exc=None):
        self.exc = exc or DraftRoomError("no draft room tab is open")
        self.closed = False

    def snapshot_raw(self):
        raise self.exc

    def close(self):
        self.closed = True


def test_a_failed_read_never_raises_at_the_caller():
    """The page must keep rendering; a traceback here is a blank screen there."""
    w = watch.Watcher()
    w._reader = _Boom()
    data = w.read()
    assert data["ok"] is False
    assert "draft room" in data["error"]


def test_the_last_good_data_survives_a_failure(watcher):
    """Blanking mid-draft is indistinguishable from "nothing has happened"."""
    good = watcher.read()
    assert good["ok"] is True

    watcher._fixture = None          # force the live path
    watcher._reader = _Boom()
    degraded = watcher.read()

    assert degraded["ok"] is False
    assert degraded["parsed"] == good["parsed"], "kept the last good board"
    assert degraded["stale_for"] is not None


def test_a_failure_drops_the_reader_so_the_next_poll_reconnects():
    """A cached page handle never recovers from a closed-and-reopened tab."""
    w = watch.Watcher()
    w._reader = _Boom()
    w.read()
    assert w._reader is None


def test_the_first_read_can_fail_without_any_prior_good_state():
    w = watch.Watcher()
    w._reader = _Boom()
    data = w.read()
    assert data["ok"] is False
    assert data["raw"] is None
    assert data["stale_for"] is None


def test_a_non_draftroom_error_is_reported_with_its_type():
    w = watch.Watcher()
    w._reader = _Boom(RuntimeError("target closed"))
    assert "RuntimeError: target closed" in w.read()["error"]


# --- the page ---------------------------------------------------------------


def test_the_page_is_self_contained():
    """A strict-offline diagnostic must not fetch anything."""
    assert 'src="http' not in watch.PAGE
    assert 'href="http' not in watch.PAGE
    assert "cdn" not in watch.PAGE.lower()


def test_the_page_declares_both_panels():
    assert "SNAPSHOT_JS" in watch.PAGE      # the raw panel's heading
    assert "RoomSnapshot" in watch.PAGE     # the parsed panel's heading


def test_the_poll_interval_is_substituted_not_left_as_a_token():
    rendered = watch.PAGE.replace("POLL_MS", "1000")
    assert "POLL_MS" not in rendered
    assert "setInterval(tick, 1000)" in rendered


def test_the_page_escapes_rendered_text():
    """Team names are user-controlled free text and land in innerHTML."""
    assert "function esc(" in watch.PAGE
    assert "&lt;" in watch.PAGE
