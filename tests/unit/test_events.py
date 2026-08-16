"""Codec tests. The on-disk format is a permanent commitment, so several of
these pin literal bytes rather than just asserting round-trip equality."""

from __future__ import annotations

import json

import pytest

from ffa.domain.codec import CodecError, dump_event, load_event
from ffa.domain.enums import Position, Provenance
from ffa.domain.events import (
    EVENT_TYPES,
    BaseEvent,
    EventUndone,
    FieldAmended,
    PlayerNominated,
    PlayerSold,
    UnknownEvent,
)
from ffa.domain.ids import PlayerRef
from ffa.domain.sourced import known, unknown
from tests.conftest import at, sold


def all_events(init_event):
    return [
        init_event,
        PlayerNominated(
            id=2, at=at(1), player=PlayerRef.from_raw("Ja'Marr Chase"),
            nominated_by=known(5, Provenance.MANUAL, at(1)),
        ),
        sold(3, "mahomes", 3, 45, pos=Position.QB, seconds=2),
        sold(4, "barkley", 1, None, seconds=3),
        FieldAmended(
            id=5, at=at(4), entity="player:barkley", field_name="price",
            value=known(62, Provenance.MANUAL, at(4)),
        ),
        EventUndone(id=6, at=at(5), target_id=4, reason="typo"),
    ]


def test_registry_covers_every_persistable_event_class():
    """Guards against 'added an event, forgot the codec'."""
    subclasses = {c for c in BaseEvent.__subclasses__() if c is not UnknownEvent}
    assert subclasses == set(EVENT_TYPES.values())


def test_every_event_type_round_trips(init_event):
    for event in all_events(init_event):
        assert load_event(dump_event(event)) == event


def test_round_trip_is_byte_identical(init_event):
    """The strong claim: the codec is a true bijection on the wire format.

    Without this, a resumed session could keep appending in a subtly different
    format than the lines already in the file.
    """
    for event in all_events(init_event):
        line = dump_event(event)
        assert dump_event(load_event(line)) == line


def test_sold_with_unknown_price_serializes_an_explicit_null():
    """Pinned literally. An omitted key would be indistinguishable from an
    older schema that lacked the field."""
    payload = json.loads(dump_event(sold(7, "mahomes", 3, None)))
    assert payload["price"] == {
        "value": None,
        "provenance": "UNKNOWN",
        "observed_at": "2026-08-16T19:00:00Z",
        "confidence": 0.0,
    }


def test_envelope_keys_lead_the_line(init_event):
    """So you can scan the left edge of the journal by eye."""
    assert list(json.loads(dump_event(sold(2, "x", 1, 5))))[:4] == ["id", "at", "type", "source"]


def test_dump_is_always_a_single_line():
    event = sold(2, "weird\nname", 1, 5)
    assert "\n" not in dump_event(event)


def test_note_survives_the_round_trip():
    event = sold(2, "mahomes", 3, 45)
    event = type(event)(**{**event.__dict__, "note": "sold mahomes 45 t3"})
    assert load_event(dump_event(event)).note == "sold mahomes 45 t3"


# --- forward compatibility ---------------------------------------------------


def test_unknown_event_type_degrades_instead_of_raising():
    line = json.dumps({"id": 9, "at": "2026-08-16T19:00:00Z", "type": "PlayerKept", "x": 1})
    event = load_event(line)
    assert isinstance(event, UnknownEvent)
    assert event.type_name == "PlayerKept"
    assert event.id == 9


def test_unknown_event_round_trips_verbatim():
    """An older binary must not destroy data written by a newer one."""
    line = json.dumps({"id": 9, "at": "2026-08-16T19:00:00Z", "type": "PlayerKept", "x": 1})
    assert dump_event(load_event(line)) == line


def test_extra_unknown_fields_on_a_known_type_are_ignored():
    line = json.loads(dump_event(sold(2, "mahomes", 3, 45)))
    line["some_future_field"] = {"nested": True}
    assert load_event(json.dumps(line)).player.key == "mahomes"


# --- corruption --------------------------------------------------------------


def test_missing_required_field_is_corruption():
    line = json.loads(dump_event(sold(2, "mahomes", 3, 45)))
    del line["team"]
    with pytest.raises(CodecError, match="missing required field"):
        load_event(json.dumps(line))


@pytest.mark.parametrize(
    "line,fragment",
    [
        ("{not json", "not valid JSON"),
        ("[1,2,3]", "expected a JSON object"),
        ('{"type":"EventUndone","target_id":1}', "no usable id"),
        ('{"id":1,"type":"PlayerSold"}', "missing required field"),
    ],
)
def test_malformed_lines_raise_codec_error(line, fragment):
    with pytest.raises(CodecError, match=fragment):
        load_event(line)


def test_unparseable_timestamp_raises():
    line = json.loads(dump_event(sold(2, "mahomes", 3, 45)))
    line["at"] = "not-a-time"
    with pytest.raises(CodecError, match="unparseable timestamp"):
        load_event(json.dumps(line))


def test_unregistered_event_class_cannot_be_persisted():
    class Rogue(BaseEvent):
        pass

    with pytest.raises(CodecError, match="not in EVENT_TYPES"):
        dump_event(Rogue(id=1, at=at()))
