"""The anonymizer, because it is the one tool where a bug leaks real identities.

`client.scrub` removes *your* credentials. It does nothing about the other
managers' member SWIDs, which ride along in `mDraftDetail` picks as `memberId`
and in `mTeam` as `members[].id`. Those are real account identifiers for real
people, and a capture is worthless as a fixture until they are gone.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "anonymize_capture", TOOLS / "anonymize_capture.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


anonymize_capture = _load_tool()


REAL_A = "{6D4DEADB-EEFC-4F1A-9B2C-A1B2C3D4E5F6}"
REAL_B = "{0A1B2C3D-4E5F-6071-8293-A4B5C6D7E8F9}"


def sample() -> str:
    return json.dumps({
        "teams": [
            {"id": 1, "primaryOwner": REAL_A, "name": "Real Team Name"},
            {"id": 3, "primaryOwner": REAL_B, "name": "Another Real Name"},
        ],
        "members": [
            {"id": REAL_A, "displayName": "realhandle92",
             "firstName": "Michael", "lastName": "Curley"},
            {"id": REAL_B, "displayName": "otherguy"},
        ],
        "draftDetail": {"picks": [{"memberId": REAL_A, "playerId": 4429795,
                                   "bidAmount": 76}]},
    })


def test_every_real_swid_is_replaced():
    payload, mapping, _ = anonymize_capture.anonymize(sample())
    text = json.dumps(payload)

    assert REAL_A not in text
    assert REAL_B not in text
    assert len(mapping) == 2


def test_manager_controlled_names_are_blanked():
    payload, _, renamed = anonymize_capture.anonymize(sample())
    text = json.dumps(payload)

    assert "Michael" not in text
    assert "Curley" not in text
    assert "realhandle92" not in text
    assert renamed > 0


def test_relationships_survive_so_the_fixture_is_still_useful():
    """The same member must stay the same member across teams, members, picks.

    An anonymizer that assigns randomly per occurrence destroys exactly the
    structure a fixture exists to pin.
    """
    payload, _, _ = anonymize_capture.anonymize(sample())

    owner = payload["teams"][0]["primaryOwner"]
    assert payload["members"][0]["id"] == owner
    assert payload["draftDetail"]["picks"][0]["memberId"] == owner
    assert payload["teams"][1]["primaryOwner"] != owner


def test_public_reference_data_is_deliberately_left_alone():
    """Player ids and bid amounts are the whole reason the capture is kept."""
    payload, _, _ = anonymize_capture.anonymize(sample())
    pick = payload["draftDetail"]["picks"][0]
    assert pick["playerId"] == 4429795
    assert pick["bidAmount"] == 76


def test_your_own_redacted_credential_gets_a_synthetic_id_too():
    """`scrub` has already turned your SWID into the literal `<redacted>`.

    Without handling it, your team is the one team whose member id is not
    SWID-shaped, and anything asserting on shape trips over it.
    """
    text = json.dumps({"members": [{"id": "<redacted>"}, {"id": REAL_A}]})
    payload, mapping, _ = anonymize_capture.anonymize(text)

    assert "<redacted>" not in json.dumps(payload)
    assert len(mapping) == 2


def test_a_single_pass_never_substitutes_its_own_output():
    """Ids drawn from the synthetic namespace must not be re-mapped.

    A chain of `str.replace` rewrites values it has already written the moment
    the two namespaces overlap — which is exactly what re-running this on a
    committed fixture does.
    """
    synthetic = [anonymize_capture.synthetic_swid(i) for i in (1, 2, 3)]
    text = json.dumps({"members": [{"id": s} for s in reversed(synthetic)]})

    payload, _, _ = anonymize_capture.anonymize(text)
    ids = [m["id"] for m in payload["members"]]

    assert len(set(ids)) == 3, "distinct ids must stay distinct"
    assert set(ids) <= set(synthetic)


def test_running_it_twice_is_stable():
    once, _, _ = anonymize_capture.anonymize(sample())
    twice, _, _ = anonymize_capture.anonymize(json.dumps(once))
    assert {m["id"] for m in twice["members"]} == {m["id"] for m in once["members"]}


def test_it_refuses_to_write_if_an_id_would_survive(monkeypatch):
    """A leak must stop the run, not warn about it."""
    monkeypatch.setattr(anonymize_capture, "build_swid_map", lambda _text: {})
    with pytest.raises(SystemExit):
        anonymize_capture.anonymize(sample())
