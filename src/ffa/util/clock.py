"""Time, behind one indirection.

Everything that needs "now" calls `now_utc()` here rather than
`datetime.now()` directly. That gives tests a single place to freeze or forbid
the clock, which matters because replay determinism depends on reducers never
reading the wall clock — see `tests/unit/test_reducers.py`.

All timestamps are timezone-aware UTC and serialize with a trailing `Z`.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Serialize as ISO-8601 UTC with a `Z` suffix.

    Naive datetimes are assumed UTC rather than rejected: they only reach here
    from hand-written journal fixtures, and guessing UTC is strictly better
    than crashing a live draft over a missing offset.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def from_iso(raw: str) -> datetime:
    """Parse what `to_iso` writes, and also tolerate `+00:00` offsets.

    `fromisoformat` gained `Z` support in 3.11, but journals may be hand-edited
    or written by other tools, so normalize before parsing.
    """
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
