"""Taking dossier answers back from wherever the interview actually happened.

The interview is recall, and recall goes better spoken than typed — in a chat, by
voice, in a text file on a phone. Whatever the front end, the answers come back
as JSON, and this is the seam that accepts them.

Every value goes through the **same** `parse_answer` the terminal interview uses.
That is the point of the file: an import path with its own looser validation
would be a second way to get a bad value into the record, and the record is the
one thing here that cannot be regenerated.

Two deliberate refusals:

- **Absent means "not asked" and is left alone.** An import carries what was
  discussed, not the whole record, so a field that is not mentioned must survive
  untouched. Treating absence as a clear would make every partial pass
  destructive.
- **A bad value is dropped and named, never coerced.** Silently rounding
  `"pretty sharp"` to `sharp` would put a guess into the record wearing an
  observation's clothes, which is the one failure this whole subsystem is built
  to avoid.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, Mapping

from ffa.dossier.schema import (
    BY_FIELD,
    DossierError,
    OwnerDossier,
    fold_owner_id,
    parse_answer,
    seed,
)
from ffa.dossier.store import DossierBook

# A fenced ```json block, which is what a chat transcript actually contains.
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

# Values that mean "no answer" however the far end chose to say so. They are
# skipped rather than written, so the absent-means-unasked rule holds even when
# something helpfully fills the blanks in.
_NON_ANSWERS = {"", "-", "none", "null", "n/a", "na", "unknown", "not asked", "?"}


class ImportReport:
    """What landed, what did not, and why — per manager."""

    def __init__(self) -> None:
        self.applied: dict[int, list[str]] = {}
        self.rejected: list[str] = []
        self.skipped: list[str] = []

    @property
    def changed_teams(self) -> tuple[int, ...]:
        return tuple(sorted(t for t, fields in self.applied.items() if fields))

    @property
    def field_count(self) -> int:
        return sum(len(f) for f in self.applied.values())

    def __bool__(self) -> bool:
        return bool(self.field_count)


def extract_payload(text: str) -> dict[str, Any]:
    """The JSON object in whatever was handed over.

    Accepts a bare JSON file or a chat transcript with a fenced block in it,
    because both are things a person will realistically paste. The *last* fenced
    block wins: a conversation that produced a draft and then a correction ends
    with the correction.
    """
    stripped = (text or "").strip()
    if not stripped:
        raise DossierError("that file is empty")

    try:
        data = json.loads(stripped)
    except ValueError:
        blocks = _FENCE.findall(stripped)
        if not blocks:
            raise DossierError(
                "found no JSON in there. Paste the whole ```json block the "
                "interview produced, or a plain .json file."
            ) from None
        try:
            data = json.loads(blocks[-1])
        except ValueError as exc:
            raise DossierError(f"that JSON block does not parse: {exc}") from None

    if not isinstance(data, dict):
        raise DossierError("expected a JSON object keyed by team id")

    # Tolerate the on-disk shape too, so a hand-edited dossiers.json can be fed
    # straight back in.
    inner = data.get("owners")
    return inner if isinstance(inner, dict) else data


def _resolve_seat(key: str, seats: Mapping[int, str]) -> int | None:
    """`"3"` -> 3, and a SWID -> whichever seat holds it."""
    text = str(key).strip()
    if text.lstrip("t").isdigit():
        team_id = int(text.lstrip("t"))
        return team_id if team_id in seats else None

    folded = fold_owner_id(text)
    for team_id, swid in seats.items():
        if fold_owner_id(swid) == folded:
            return team_id
    return None


def _is_non_answer(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple)):
        return not value
    return str(value).strip().lower() in _NON_ANSWERS


def _as_text(value: Any) -> str:
    """Normalize to what `parse_answer` reads, so validation stays shared."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, bool):  # `true` is never a valid answer to anything here
        raise DossierError("expected a value, got a boolean")
    return str(value)


def apply_payload(
    book: DossierBook,
    payload: Mapping[str, Any],
    *,
    seats: Mapping[int, str],
    labels: Mapping[int, str] | None = None,
    today: str = "",
) -> tuple[DossierBook, ImportReport]:
    """Merge answers into the book. Pure; the caller decides whether to save."""
    labels = labels or {}
    report = ImportReport()

    for key, entry in payload.items():
        team_id = _resolve_seat(key, seats)
        if team_id is None:
            report.rejected.append(
                f"{key!r} is not a team in this league — skipped. "
                "Valid ids: " + ", ".join(str(t) for t in sorted(seats))
            )
            continue
        if not isinstance(entry, Mapping):
            report.rejected.append(f"team {team_id}: expected an object of answers")
            continue

        owner_id = seats[team_id]
        current = book.for_owner(owner_id) or seed(
            owner_id, team_id=team_id, label=labels.get(team_id, "")
        )
        updated, applied = _apply_one(current, entry, team_id, report)

        if applied:
            report.applied[team_id] = applied
            book = book.put(
                replace(
                    updated,
                    team_id=team_id,
                    label=labels.get(team_id, updated.label),
                    updated_at=today or updated.updated_at,
                )
            )

    return book, report


def _apply_one(
    dossier: OwnerDossier,
    entry: Mapping[str, Any],
    team_id: int,
    report: ImportReport,
) -> tuple[OwnerDossier, list[str]]:
    applied: list[str] = []

    for name, value in entry.items():
        question = BY_FIELD.get(str(name))
        if question is None:
            # `team_id` and `label` ride along in the on-disk shape; they are
            # display text the config owns, so ignoring them is correct.
            if str(name) == "derived_from":
                # Bookkeeping, not testimony. Applied, but it is not an answer
                # and must not count as one.
                dossier = replace(dossier, derived_from=str(value))
                continue
            if str(name) not in ("team_id", "label", "owner_id", "updated_at"):
                report.rejected.append(
                    f"team {team_id}: {name!r} is not a dossier field — ignored"
                )
            continue

        if _is_non_answer(value):
            # Absent *and* explicitly blank both mean "we did not get to this".
            report.skipped.append(f"team {team_id}: {name} left unanswered")
            continue

        try:
            parsed = parse_answer(question, _as_text(value))
        except DossierError as exc:
            report.rejected.append(f"team {team_id}: {name} — {exc}")
            continue

        dossier = replace(dossier, **{question.field: parsed})
        applied.append(question.field)

    return dossier, applied
