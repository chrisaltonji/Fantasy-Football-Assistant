"""The committed sample payloads must never carry a real identity.

`docs/sample_state*.json` exist so a dashboard designer has something real to
build against, and "real" means real *shape*. The repo is public, and a
`build_view()` payload names twelve actual people: their ESPN member SWIDs, the
league id, the league name, and the nicknames they are known by.

This is a guard against a specific thing that has already happened once.
Regenerating the samples from the live league is the obvious way to refresh
them — `ffa export-state` does exactly that — and it silently replaces the
anonymised fixture with the real one. Nothing about the file looks different
at a glance. This test is what notices.

    python tools/anonymize_capture.py docs/sample_state.json --view \
        --out docs/sample_state.json
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

def _anonymizer():
    """Import `tools/anonymize_capture.py`, which is a script rather than a
    package. Re-declaring its constants here would defeat the point: the test
    exists to check the fixture matches what the anonymiser actually produces.
    """
    import importlib.util

    tool = Path(__file__).resolve().parents[2] / "tools" / "anonymize_capture.py"
    spec = importlib.util.spec_from_file_location("anonymize_capture", tool)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_TOOL = None


def _const(name):
    global _TOOL
    if _TOOL is None:
        _TOOL = _anonymizer()
    return getattr(_TOOL, name)

DOCS = Path(__file__).resolve().parents[2] / "docs"
SAMPLES = sorted(DOCS.glob("sample_state*.json"))

# Synthetic SWIDs are one hex character repeated, per `synthetic_swid`. A real
# ESPN member id will not match this, which is the whole point.
SYNTHETIC_SWID = re.compile(r"^\{([0-9A-F])\1{7}-0000-4000-8000-[0-9A-F]{12}\}$")


def test_there_are_samples_to_check():
    assert SAMPLES, "docs/sample_state*.json is what the design brief points at"


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_every_owner_id_is_synthetic(path):
    """The one that matters. A member SWID is an account identifier for a real
    person who is not you, and this repo is public."""
    payload = json.loads(path.read_text(encoding="utf-8"))

    owners = [t.get("owner_id") for t in payload.get("teams") or []]
    owners.append((payload.get("me") or {}).get("owner_id"))

    for owner in filter(None, owners):
        assert SYNTHETIC_SWID.match(owner), (
            f"{path.name} carries a real-looking SWID {owner}. Re-run: "
            f"python tools/anonymize_capture.py {path} --view --out {path}"
        )


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_the_league_is_the_example_one(path):
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["league"]["name"] == _const("SAMPLE_LEAGUE")
    # The draft id embeds the league id, which is enough to find the league.
    assert str(_const("SAMPLE_LEAGUE_ID")) in payload["draft_id"]


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_every_manager_is_an_alias(path):
    payload = json.loads(path.read_text(encoding="utf-8"))

    for team in payload.get("teams") or []:
        assert team["label"] in _const("SAMPLE_MANAGERS"), (
            f"{path.name} shows manager {team['label']!r}, which is not one of "
            "the sample aliases"
        )


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name)
def test_the_numbers_survive_anonymising(path):
    """Anonymising must cost nothing a designer needs. Prices, budgets,
    ceilings, scarcity and NFL player names are the entire reason the sample is
    worth committing."""
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["teams"], "no teams left"
    assert payload["me"]["max_legal_bid"] >= 0
    assert payload["recent_sales"], "no sales left"
    assert payload["scarcity"], "no scarcity left"
    assert any(t["roster"] for t in payload["teams"]), "no rosters left"
