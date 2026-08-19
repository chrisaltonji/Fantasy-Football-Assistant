"""The HTTP layer's behaviour, without an HTTP layer.

What's pinned here is not "does requests work" — it's the handful of decisions
that make the difference between a client that reports what happened and one
that misreports it.
"""

from __future__ import annotations

import json

import pytest
import requests

from espn_fantasy.client import (
    HOSTS,
    EspnClient,
    build_url,
    normalize_payload,
    scrub,
)
from espn_fantasy.credentials import EspnCredentials
from espn_fantasy.errors import EspnApiError
from tests.conftest import FakeResponse, FakeSession


def client(responses=None, **kwargs) -> EspnClient:
    session = FakeSession(responses)
    return EspnClient(123456, 2026, session=session, **kwargs)


# --- URL shapes -------------------------------------------------------------


def test_seasonal_and_history_urls_are_different_shapes():
    seasonal = build_url(HOSTS[0], 123456, 2026, "mTeam")
    history = build_url(HOSTS[0], 123456, 2025, "mTeam", history=True)

    assert "/seasons/2026/segments/0/leagues/123456" in seasonal
    assert "?view=mTeam" in seasonal
    # The history path puts the season in the query string, not the path.
    assert "/leagueHistory/123456" in history
    assert "seasonId=2025" in history


def test_segment_is_configurable_because_only_zero_is_confirmed():
    assert "/segments/2/" in build_url(HOSTS[0], 1, 2026, "mTeam", segment="2")


# --- the array that only leagueHistory returns ------------------------------


def test_history_arrays_collapse_to_the_requested_season():
    payload = [{"seasonId": 2024, "n": "old"}, {"seasonId": 2025, "n": "wanted"}]
    assert normalize_payload(payload, 2025)["n"] == "wanted"


def test_a_history_array_without_the_year_falls_back_to_the_last_entry():
    payload = [{"seasonId": 2024, "n": "old"}, {"seasonId": 2023, "n": "last"}]
    assert normalize_payload(payload, 2099)["n"] == "last"


def test_normalize_leaves_a_plain_object_alone():
    assert normalize_payload({"a": 1}, 2026) == {"a": 1}


def test_normalize_survives_an_empty_array():
    assert normalize_payload([], 2026) == {}


# --- host fallback ----------------------------------------------------------


def test_a_dead_first_host_falls_through_to_the_second():
    espn = client([FakeResponse(500, text="boom"), FakeResponse(200, {"ok": True})])
    assert espn.get_view("mTeam") == {"ok": True}
    assert [u.split("/apis")[0] for u in espn.session.urls] == list(HOSTS)


def test_a_connection_error_on_the_first_host_is_not_fatal():
    espn = client([requests.ConnectionError("no route"), FakeResponse(200, {"ok": 1})])
    assert espn.get_view("mTeam") == {"ok": 1}


def test_a_401_is_not_retried_on_the_second_host():
    """The status is an answer about the league, not about the host.

    Retrying buries it: the fallback host answers 401 too, and whichever
    error surfaces last is the one the user sees. It must be the 401.
    """
    espn = client([FakeResponse(401, text="Unauthorized")])
    with pytest.raises(EspnApiError, match="401"):
        espn.get_view("mTeam")
    assert len(espn.session.calls) == 1


def test_a_404_says_it_may_be_an_auth_problem_in_disguise():
    """The single most confusing thing about this API.

    A private league fetched without cookies returns 404, not 401. A message
    that only says "check the league id" sends people to debug the one thing
    that is fine.
    """
    espn = client([FakeResponse(404, text="Not Found")])
    with pytest.raises(EspnApiError) as exc:
        espn.get_view("mTeam")
    assert "404" in str(exc.value)
    assert "private league" in str(exc.value)
    assert len(espn.session.calls) == 1


def test_try_view_reports_failure_instead_of_raising():
    """Sweeps depend on this: a 404 on one candidate is a result, not an abort."""
    espn = client([FakeResponse(404), FakeResponse(404)])
    result = espn.try_view("mDraftDetail")
    assert result.ok is False
    assert result.status == 404


def test_a_200_that_is_not_json_is_a_failure_not_a_payload():
    espn = client([FakeResponse(200, None, text="<html>maintenance</html>")])
    result = espn.try_url("https://example.invalid/x")
    assert result.status == 200
    assert result.ok is False
    assert "not JSON" in result.error


# --- headers and cookies ----------------------------------------------------


def test_a_browser_user_agent_is_sent_because_espn_403s_scripts():
    espn = client()
    assert "Chrome" in espn.session.headers["User-Agent"]


def test_credentials_become_cookies_on_the_session():
    espn = client(credentials=EspnCredentials(espn_s2="S2VALUE", swid="ABC"))
    assert espn.session.cookies["espn_s2"] == "S2VALUE"
    assert espn.session.cookies["SWID"] == "{ABC}"


def test_the_player_view_sends_the_filter_header_that_makes_it_work():
    """Without `x-fantasy-filter` ESPN returns a handful of players.

    No query string coaxing substitutes for it, which is exactly the kind of
    thing that is impossible to guess and easy to lose in a refactor.
    """
    espn = client([FakeResponse(200, {"players": [{"player": {}}]})])
    espn.players(limit=50)

    sent = json.loads(espn.session.calls[0]["headers"]["x-fantasy-filter"])
    assert sent["players"]["limit"] == 50
    assert sent["players"]["sortPercOwned"]["sortAsc"] is False


def test_the_player_view_returns_the_players_array():
    espn = client([FakeResponse(200, {"players": [{"player": {"fullName": "X"}}]})])
    assert espn.players()[0]["player"]["fullName"] == "X"


def test_a_player_response_with_no_players_key_is_empty_not_an_error():
    espn = client([FakeResponse(200, {})])
    assert espn.players() == []


# --- scrubbing --------------------------------------------------------------


def test_scrub_removes_credentials_in_every_spelling_they_appear_in():
    creds = EspnCredentials(espn_s2="SECRET_S2", swid="{AAAA-BBBB}")
    payload = {"a": "SECRET_S2", "b": "{AAAA-BBBB}", "c": "AAAA-BBBB"}

    cleaned = json.dumps(scrub(payload, creds))
    assert "SECRET_S2" not in cleaned
    assert "AAAA-BBBB" not in cleaned


def test_scrub_is_a_no_op_without_credentials():
    assert scrub({"a": 1}, None) == {"a": 1}


def test_credentials_never_appear_in_a_repr():
    """Anything that reaches a traceback reaches a bug report."""
    text = repr(EspnCredentials(espn_s2="SECRET_S2", swid="SECRET_SWID"))
    assert "SECRET_S2" not in text
    assert "SECRET_SWID" not in text
