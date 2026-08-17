"""Tests for the probe's analysis logic.

The probe is a diagnostic, but its *verdict* is what Checkpoint 1 delivers, so
the analysers are tested offline against synthetic payloads shaped like the
real ones. The HTTP layer is not tested here — it is exercised by hand against
ESPN.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from ffa.config.schema import EspnCredentials

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "espn_probe.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("espn_probe", TOOLS)
    module = importlib.util.module_from_spec(spec)
    sys.modules["espn_probe"] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def draft_payload(*, bids: list[int]) -> dict:
    return {
        "draftDetail": {
            "drafted": True,
            "inProgress": False,
            "picks": [
                {
                    "id": i,
                    "playerId": 3000 + i,
                    "teamId": (i % 12) + 1,
                    "roundId": 1,
                    "roundPickNumber": i + 1,
                    "bidAmount": bid,
                    "nominatingTeamId": (i % 12) + 1,
                    "keeper": False,
                }
                for i, bid in enumerate(bids)
            ],
        }
    }


def test_populated_bids_produce_a_viable_verdict():
    report = "\n".join(probe.analyse_draft(draft_payload(bids=[45, 62, 1])))
    assert "bidAmount IS populated" in report
    assert "$1-$62" in report
    assert "total $108" in report
    assert "bidAmount" in report and "present on 3/3 picks" in report


def test_filled_picks_with_zero_bids_produce_a_fallback_verdict():
    report = "\n".join(probe.analyse_draft(draft_payload(bids=[0, 0, 0])))
    assert "every bidAmount is ZERO" in report
    assert "manual entry" in report


def test_an_untouched_skeleton_is_inconclusive_not_a_failure():
    """ESPN pre-creates all 180 picks, so zeros before the draft prove nothing.

    Without separating "nothing sold yet" from "sold but unpriced", the
    pre-draft baseline reads as evidence that ESPN never populates prices —
    the opposite conclusion from the same zeros.
    """
    payload = draft_payload(bids=[0, 0, 0])
    for pick in payload["draftDetail"]["picks"]:
        pick["playerId"] = -1  # the unfilled sentinel
    report = "\n".join(probe.analyse_draft(payload))
    assert "INCONCLUSIVE" in report
    assert "filled picks          = 0/3" in report


def test_only_filled_picks_count_toward_the_verdict():
    """One real sale among a skeleton of placeholders is still a positive."""
    payload = draft_payload(bids=[45, 0, 0])
    for pick in payload["draftDetail"]["picks"][1:]:
        pick["playerId"] = -1
    report = "\n".join(probe.analyse_draft(payload))
    assert "bidAmount IS populated (1/1 filled picks" in report


def test_missing_bid_field_is_reported_as_absent():
    payload = draft_payload(bids=[10])
    del payload["draftDetail"]["picks"][0]["bidAmount"]
    report = "\n".join(probe.analyse_draft(payload))
    assert "ABSENT from every pick" in report


def test_empty_draft_asks_the_user_to_sell_a_player():
    report = "\n".join(probe.analyse_draft({"draftDetail": {"picks": []}}))
    assert "No picks yet" in report


def test_missing_draft_detail_is_handled():
    report = "\n".join(probe.analyse_draft({"teams": []}))
    assert "draftDetail is absent" in report


def test_settings_analyser_surfaces_auction_budget():
    payload = {
        "settings": {
            "name": "Test League",
            "size": 12,
            "draftSettings": {"type": "AUCTION", "auctionBudget": 200, "keeperCount": 0},
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "2": 2}},
            "scoringSettings": {"scoringType": "PPR"},
        }
    }
    report = "\n".join(probe.analyse_settings(payload))
    assert "draftSettings.type    = AUCTION" in report
    assert "auctionBudget = 200" in report


def test_team_analyser_flags_budget_shaped_keys():
    payload = {
        "teams": [
            {
                "id": 1,
                "name": "Team One",
                "draftDayAcquisitionBudgetSpent": 140,
                "roster": {"entries": [{"playerId": 3000, "bidAmount": 45}]},
            }
        ]
    }
    report = "\n".join(probe.analyse_teams(payload))
    assert "draftDayAcquisitionBudgetSpent" in report
    assert "entry value/bid keys  = ['bidAmount']" in report


def test_team_analyser_says_so_when_no_budget_keys_exist():
    report = "\n".join(probe.analyse_teams({"teams": [{"id": 1, "name": "T"}]}))
    assert "budget-ish keys       = NONE FOUND" in report


# --- credential scrubbing: these payloads get committed as fixtures ---


def test_scrub_removes_credentials_anywhere_in_the_payload():
    creds = EspnCredentials(espn_s2="SECRET_S2", swid="{SECRET_SWID}")
    payload = {
        "nested": {"cookie": "SECRET_S2"},
        "list": ["prefix SECRET_SWID suffix"],
        "bare": "SECRET_SWID",
    }
    cleaned = probe.scrub(payload, creds)
    assert "SECRET_S2" not in str(cleaned)
    assert "SECRET_SWID" not in str(cleaned)
    assert cleaned["nested"]["cookie"] == "<redacted>"


def test_scrub_is_a_noop_without_credentials():
    payload = {"a": 1}
    assert probe.scrub(payload, None) == payload


@pytest.mark.parametrize("status,fragment", [(401, "401 Unauthorized"), (404, "404 for league")])
def test_http_errors_produce_actionable_messages(status, fragment, monkeypatch):
    class FakeResponse:
        status_code = status
        text = ""

    class FakeSession:
        def get(self, url, timeout):
            return FakeResponse()

    with pytest.raises(probe.ProbeError, match=fragment):
        probe.fetch_view(FakeSession(), 1, 2026, "mDraftDetail")


# --- URL construction ------------------------------------------------------


def test_default_url_shape_is_unchanged():
    """segments/0 is the only shape ever confirmed against ESPN. Lock it."""
    assert probe.build_url("https://h", 123, 2026, "mDraftDetail") == (
        "https://h/apis/v3/games/ffl/seasons/2026/segments/0/leagues/123?view=mDraftDetail"
    )


def test_segment_override_changes_only_the_segment():
    url = probe.build_url("https://h", 123, 2026, "mDraftDetail", segment="7")
    assert "/segments/7/leagues/123" in url


def test_history_uses_the_league_history_path():
    url = probe.build_url("https://h", 123, 2025, "mDraftDetail", history=True)
    assert url == (
        "https://h/apis/v3/games/ffl/leagueHistory/123?seasonId=2025&view=mDraftDetail"
    )
    assert "segments" not in url


# --- leagueHistory returns an array; every analyser wants a dict ------------


def test_normalize_picks_the_requested_season():
    payload = [{"seasonId": 2024, "n": "old"}, {"seasonId": 2025, "n": "want"}]
    assert probe.normalize_payload(payload, 2025)["n"] == "want"


def test_normalize_falls_back_to_the_last_entry():
    payload = [{"seasonId": 2024, "n": "old"}, {"seasonId": 2025, "n": "newest"}]
    assert probe.normalize_payload(payload, 1999)["n"] == "newest"
    assert probe.normalize_payload(payload)["n"] == "newest"


def test_normalize_leaves_a_dict_alone():
    payload = {"draftDetail": {"picks": []}}
    assert probe.normalize_payload(payload, 2025) is payload


def test_normalize_empty_array_is_an_empty_dict():
    assert probe.normalize_payload([], 2025) == {}


def test_history_array_flows_through_to_a_verdict():
    """The whole reason normalize exists: an array must still reach analyse."""
    payload = [{"seasonId": 2025, **draft_payload(bids=[45, 62])}]
    report = "\n".join(probe.analyse_payload(probe.normalize_payload(payload, 2025)))
    assert "bidAmount IS populated" in report


# --- fetch_url: the non-raising primitive a sweep needs --------------------


class _FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    """Maps url-substring -> response, so candidates can differ per URL."""

    def __init__(self, routes, default=None):
        self.routes = routes
        self.default = default or _FakeResponse(404, text="")
        self.seen = []

    def get(self, url, timeout=None):
        self.seen.append(url)
        for fragment, resp in self.routes.items():
            if fragment in url:
                return resp
        return self.default


def test_fetch_url_reports_a_404_instead_of_raising():
    status, payload, _ = probe.fetch_url(_FakeSession({}), "https://h/x")
    assert status == 404 and payload is None


def test_fetch_url_reports_a_transport_failure_as_minus_one():
    import requests

    class Boom:
        def get(self, url, timeout=None):
            raise requests.RequestException("down")

    status, payload, error = probe.fetch_url(Boom(), "https://h/x")
    assert status == -1 and payload is None and "down" in error


# --- sweep ----------------------------------------------------------------


def _filled_skeleton(filled_count, total=4):
    payload = draft_payload(bids=[10] * total)
    for pick in payload["draftDetail"]["picks"][filled_count:]:
        pick["playerId"] = -1
    return payload


def test_sweep_visits_every_candidate_despite_a_404(tmp_path):
    """A 404 on one shape is a result, not a reason to stop looking."""
    session = _FakeSession(
        {
            "/segments/1/": _FakeResponse(200, _filled_skeleton(0)),
            "/segments/2/": _FakeResponse(200, _filled_skeleton(2)),
        }
    )  # segment 0 and leagueHistory fall through to the default 404
    report = "\n".join(
        probe.sweep(session, None, 999, 2026, probe.DEFAULT_SWEEP_CANDIDATES, tmp_path, "S")
    )

    for label in ("segment0", "segment1", "segment2", "history"):
        assert label in report
    assert "HTTP 404" in report


def test_sweep_distinguishes_a_filled_candidate_from_an_empty_one(tmp_path):
    session = _FakeSession(
        {
            "/segments/1/": _FakeResponse(200, _filled_skeleton(0)),
            "/segments/2/": _FakeResponse(200, _filled_skeleton(2)),
        }
    )
    report = "\n".join(
        probe.sweep(session, None, 999, 2026, probe.DEFAULT_SWEEP_CANDIDATES, tmp_path, "S")
    )
    assert "0 filled / 4 picks" in report
    assert "2 filled / 4 picks" in report
    assert (tmp_path / "sweep_segment2_S.json").exists()


def test_sweep_honours_a_candidate_league_id_override(tmp_path):
    session = _FakeSession({})
    probe.sweep(
        session, None, 999, 2026,
        [probe.Candidate("shadow", league_id_override=555)],
        tmp_path, "S",
    )
    assert any("/leagues/555?" in url for url in session.seen)


# --- HAR ingestion --------------------------------------------------------


def _har(entries):
    return {"log": {"entries": entries}}


def _entry(url, payload=None, *, status=200, method="GET", encoding=None):
    content = {}
    if payload is not None:
        text = json.dumps(payload)
        if encoding == "base64":
            content = {
                "text": base64.b64encode(text.encode()).decode(),
                "encoding": "base64",
            }
        else:
            content = {"text": text}
    return {
        "request": {"url": url, "method": method, "cookies": [{"name": "espn_s2", "value": "SECRET"}]},
        "response": {"status": status, "content": content},
    }


ESPN_URL = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/x?view=mDraftDetail"


def test_har_names_the_url_carrying_filled_picks(tmp_path):
    path = tmp_path / "c.har"
    path.write_text(
        json.dumps(
            _har(
                [
                    _entry("https://fantasy.espn.com/apis/v3/empty", _filled_skeleton(0)),
                    _entry(ESPN_URL, _filled_skeleton(3)),
                ]
            )
        ),
        encoding="utf-8",
    )
    report = "\n".join(probe.analyse_har(path))
    assert "ANSWER: 3 filled pick(s) at" in report
    assert ESPN_URL in report
    assert "bidAmount IS populated" in report


def test_har_decodes_base64_bodies(tmp_path):
    path = tmp_path / "c.har"
    path.write_text(
        json.dumps(_har([_entry(ESPN_URL, _filled_skeleton(2), encoding="base64")])),
        encoding="utf-8",
    )
    assert "ANSWER: 2 filled pick(s) at" in "\n".join(probe.analyse_har(path))


def test_har_never_echoes_request_cookies(tmp_path):
    path = tmp_path / "c.har"
    path.write_text(
        json.dumps(_har([_entry(ESPN_URL, _filled_skeleton(1))])), encoding="utf-8"
    )
    assert "SECRET" not in "\n".join(probe.analyse_har(path))


def test_har_tolerates_entries_with_no_body(tmp_path):
    path = tmp_path / "c.har"
    path.write_text(
        json.dumps(
            _har(
                [
                    {"request": {"url": ESPN_URL, "method": "OPTIONS"}, "response": {"status": 204}},
                    _entry(ESPN_URL, _filled_skeleton(1)),
                ]
            )
        ),
        encoding="utf-8",
    )
    report = "\n".join(probe.analyse_har(path))
    assert "OPTIONS" in report and "ANSWER: 1 filled pick(s) at" in report


def test_har_ignores_non_espn_hosts(tmp_path):
    path = tmp_path / "c.har"
    path.write_text(
        json.dumps(_har([_entry("https://cdn.example.com/a.js", _filled_skeleton(3))])),
        encoding="utf-8",
    )
    report = "\n".join(probe.analyse_har(path))
    assert "No ESPN API calls found" in report
    assert "ANSWER" not in report


def test_har_says_so_when_nothing_was_filled(tmp_path):
    """Distinguish "wrong endpoint" from "nothing had sold yet"."""
    path = tmp_path / "c.har"
    path.write_text(
        json.dumps(_har([_entry(ESPN_URL, _filled_skeleton(0))])), encoding="utf-8"
    )
    report = "\n".join(probe.analyse_har(path))
    assert "ANSWER: none of these responses carried a filled pick" in report


# --- the load-bearing verdict must survive the refactor -------------------


def test_analyse_payload_still_reports_inconclusive_on_a_bare_skeleton():
    """Guards the same invariant as the analyse_draft tests, via the new entry
    point that --url, --sweep and --har all route through."""
    report = "\n".join(probe.analyse_payload(_filled_skeleton(0, total=3)))
    assert "INCONCLUSIVE" in report
    assert "filled picks          = 0/3" in report


def test_analyse_payload_reports_nothing_recognizable_for_junk():
    assert "Nothing recognizable" in "\n".join(probe.analyse_payload({"unrelated": 1}))
