"""Shared fixtures.

Every payload here was captured from a real 12-team salary-cap league and then
anonymized. The tests are the only contract this package has — ESPN publishes
no schema, and there is no staging environment to check against — so a test
that stops matching reality is the only warning anyone gets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "espn"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def settings_payload() -> dict:
    return load("mSettings_auction.json")


@pytest.fixture
def teams_payload() -> dict:
    return load("mTeam_predraft.json")


@pytest.fixture
def predraft() -> dict:
    return load("mDraftDetail_predraft.json")


@pytest.fixture
def inprogress() -> dict:
    return load("mDraftDetail_inprogress.json")


@pytest.fixture
def completed() -> dict:
    return load("mDraftDetail_completed_auction.json")


@pytest.fixture
def practice_live() -> dict:
    return load("mDraftDetail_practice_live.json")


@pytest.fixture
def player_slice() -> dict:
    return load("kona_player_info_slice.json")


@pytest.fixture
def room_snapshot() -> dict:
    return load("draftroom_snapshot_live.json")


class FakeResponse:
    def __init__(self, status: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload if payload is not None else {})

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class FakeSession:
    """Stands in for `requests.Session` so client tests never touch a network.

    Records every call, because *which host was tried, in what order, with
    which headers* is exactly what the client's behaviour is made of.
    """

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, headers: Any = None, timeout: Any = None) -> FakeResponse:
        self.calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        if not self.responses:
            return FakeResponse(200, {})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]
