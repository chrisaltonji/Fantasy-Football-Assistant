"""Tests for the probe's analysis logic.

The probe is a diagnostic, but its *verdict* is what Checkpoint 1 delivers, so
the analysers are tested offline against synthetic payloads shaped like the
real ones. The HTTP layer is not tested here — it is exercised by hand against
ESPN.
"""

from __future__ import annotations

import importlib.util
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
