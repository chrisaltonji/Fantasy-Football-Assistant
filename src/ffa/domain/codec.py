"""The wire format: events <-> JSON lines.

Split from `events.py` on purpose. That module is about *meaning*; this one is
about *bytes*. The separation gives the byte-identity test a single target, and
keeps the envelope encoded in exactly one place so it can't drift between
event types.

Conventions, all deliberate:
- One JSON object per line, exactly one trailing newline.
- `id`, `at`, `type`, `source` always lead, so you can scan the left edge of
  the file with your eyes.
- Default separators, no `sort_keys`, no indenting. Round-tripping must be
  byte-identical or a resumed session would rewrite the file in a subtly
  different format.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from ffa.domain.events import EVENT_TYPES, BaseEvent, UnknownEvent
from ffa.util.clock import from_iso, to_iso

_ENVELOPE_KEYS = ("id", "at", "type", "source", "note")


class CodecError(Exception):
    """A line that is structurally not an event we can read."""


def dump_event(event: BaseEvent) -> str:
    """Serialize to a single line, without the trailing newline."""
    if isinstance(event, UnknownEvent):
        # Preserve an unrecognized event verbatim so a round-trip through an
        # older version doesn't destroy data written by a newer one.
        return json.dumps(dict(event.raw), ensure_ascii=False)

    type_name = _type_name(event)
    obj: dict[str, Any] = {
        "id": event.id,
        "at": to_iso(event.at) if event.at is not None else None,
        "type": type_name,
        "source": event.source,
    }
    obj.update(event.to_payload())
    if event.note is not None:
        obj["note"] = event.note
    return json.dumps(obj, ensure_ascii=False)


def load_event(line: str) -> BaseEvent:
    """Parse one line. Unknown *types* degrade; malformed *structure* raises."""
    try:
        obj = json.loads(line)
    except ValueError as exc:
        raise CodecError(f"not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise CodecError(f"expected a JSON object, got {type(obj).__name__}")

    type_name = obj.get("type")
    cls = EVENT_TYPES.get(type_name) if isinstance(type_name, str) else None
    if cls is None:
        return UnknownEvent(
            id=_envelope_id(obj),
            at=_envelope_at(obj),
            source=obj.get("source", ""),
            note=obj.get("note"),
            type_name=str(type_name),
            raw=obj,
        )

    try:
        event = cls.from_payload(obj)
    except KeyError as exc:
        # A known type missing a required field is corruption, not evolution.
        raise CodecError(f"{type_name} is missing required field {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise CodecError(f"{type_name} has a malformed field: {exc}") from exc

    return replace(
        event,
        id=_envelope_id(obj),
        at=_envelope_at(obj),
        source=obj.get("source", "MANUAL"),
        note=obj.get("note"),
    )


def _type_name(event: BaseEvent) -> str:
    for name, cls in EVENT_TYPES.items():
        if type(event) is cls:
            return name
    raise CodecError(
        f"{type(event).__name__} is not in EVENT_TYPES — add it to the registry "
        "in events.py, or it cannot be persisted."
    )


def _envelope_id(obj: dict[str, Any]) -> int:
    try:
        return int(obj["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CodecError(f"event has no usable id: {obj.get('id')!r}") from exc


def _envelope_at(obj: dict[str, Any]):
    raw = obj.get("at")
    if raw is None:
        return None
    try:
        return from_iso(raw)
    except ValueError as exc:
        raise CodecError(f"unparseable timestamp {raw!r}") from exc
