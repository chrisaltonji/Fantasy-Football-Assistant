"""Loading, holding and saving `data/dossiers.json`.

Kept separate from the schema for the reason the dashboard spec gives: dossiers
"persist and mutate on a different cadence" from everything else. They outlive a
season, a config rebuild, and a league renumbering, because they are about
people rather than about this draft.

Two properties this file exists to guarantee:

- **Lookup is by SWID, and the config is the authority on which SWID sits in
  which seat.** The `team_id` written into a dossier is display text from
  whenever it was recorded. Trusting it would attribute a read to the wrong
  manager the first season somebody leaves the league — the exact failure the
  SWID anchor exists to prevent.
- **A broken file costs advice, never the draft.** `load_dossiers` degrades to
  an empty book with warnings, the same way `load_book` falls back rather than
  refusing to start.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Iterator, Mapping

from ffa.dossier.schema import (
    DossierError,
    OwnerDossier,
    fold_owner_id,
    from_dict,
    to_dict,
)

DEFAULT_DOSSIER_PATH = Path("data/dossiers.json")
DOSSIER_SCHEMA_VERSION = 1


class DossierBook:
    """Every dossier we have, addressable by owner or by seat.

    `owners` is the config's `team_id -> SWID` map. Passing it in is what makes
    `for_team` authoritative rather than a guess off stale display text; a book
    built without it simply cannot answer by seat, which is the honest outcome.
    """

    def __init__(
        self,
        by_owner: Mapping[str, OwnerDossier] | None = None,
        *,
        owners: Mapping[int, str] | None = None,
        source: Path | None = None,
        warnings: tuple[str, ...] = (),
        resolver=None,
    ) -> None:
        self._resolver = resolver
        extra: list[str] = []

        def key(swid):
            return resolver.resolve(swid) if resolver is not None else fold_owner_id(swid)

        # Two accounts for one person means two entries on disk. Fold them, but
        # **never merge answers silently**: a dossier is a record of what
        # somebody told us, and quietly picking one of two contradictory
        # answers would fabricate an observation. The primary wins and the
        # collision is named.
        self._by_owner: dict[str, OwnerDossier] = {}
        for owner_id, dossier in (by_owner or {}).items():
            folded = key(owner_id)
            existing = self._by_owner.get(folded)
            if existing is None:
                self._by_owner[folded] = dossier
                continue
            keep, drop = (existing, dossier) if key(existing.owner_id) == fold_owner_id(
                existing.owner_id
            ) else (dossier, existing)
            clashes = [
                f for f in drop.answered
                if f in keep.answered and getattr(keep, f) != getattr(drop, f)
            ]
            self._by_owner[folded] = keep
            if clashes:
                extra.append(
                    f"two aliased accounts both answer {', '.join(sorted(clashes))} "
                    f"for {folded} — kept the primary's answers"
                )

        self._owners = {
            int(team_id): key(swid) for team_id, swid in (owners or {}).items()
        }
        self.source = source
        self.warnings = tuple(warnings) + tuple(extra)

    # -- lookup ---------------------------------------------------------------

    def for_owner(self, owner_id: str | None) -> OwnerDossier | None:
        if not owner_id:
            return None
        if self._resolver is not None:
            return self._by_owner.get(self._resolver.resolve(owner_id))
        return self._by_owner.get(fold_owner_id(owner_id))

    def for_team(self, team_id: int) -> OwnerDossier | None:
        return self.for_owner(self._owners.get(int(team_id)))

    def __len__(self) -> int:
        return len(self._by_owner)

    def __iter__(self) -> Iterator[OwnerDossier]:
        return iter(sorted(self._by_owner.values(), key=lambda d: (d.team_id, d.owner_id)))

    def __contains__(self, owner_id: object) -> bool:
        return fold_owner_id(str(owner_id)) in self._by_owner

    @property
    def filled(self) -> tuple[OwnerDossier, ...]:
        return tuple(d for d in self if not d.is_empty)

    def coverage(self, owners: Mapping[int, str] | None = None) -> float:
        """Fraction of the league we have any read on at all."""
        seats = owners or self._owners
        if not seats:
            return 0.0
        known = sum(
            1
            for swid in seats.values()
            if (d := self.for_owner(swid)) is not None and not d.is_empty
        )
        return known / len(seats)

    # -- mutation -------------------------------------------------------------

    def put(self, dossier: OwnerDossier) -> "DossierBook":
        """Returns a new book. Dossiers are frozen; so is this, by convention."""
        merged = dict(self._by_owner)
        merged[fold_owner_id(dossier.owner_id)] = dossier
        return DossierBook(
            merged, owners=self._owners, source=self.source, warnings=self.warnings,
            resolver=self._resolver,
        )

    def with_seats(self, owners: Mapping[int, str], labels: Mapping[int, str] | None = None):
        """Re-point the book at this season's seating.

        Also refreshes each dossier's display `team_id`/`label`, so a file read
        back next season shows where people actually sit now rather than where
        they sat when the note was written.
        """
        labels = labels or {}
        updated = dict(self._by_owner)
        for team_id, swid in owners.items():
            key = fold_owner_id(swid)
            existing = updated.get(key)
            if existing is None:
                continue
            updated[key] = replace(
                existing,
                team_id=int(team_id),
                label=labels.get(int(team_id), existing.label),
            )
        return DossierBook(
            updated, owners=owners, source=self.source, warnings=self.warnings
        )

    @classmethod
    def empty(cls) -> "DossierBook":
        return cls({})


# --- disk -------------------------------------------------------------------------


def load_dossiers(
    path: Path = DEFAULT_DOSSIER_PATH,
    *,
    owners: Mapping[int, str] | None = None,
    resolver=None,
) -> DossierBook:
    """Read the file. A missing or broken one is an empty book, never an error.

    Refusing to start a draft because a notes file has a typo in it would be the
    wrong trade by a wide margin — the arithmetic works perfectly without any of
    this.
    """
    if not path.is_file():
        return DossierBook({}, owners=owners, source=path)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return DossierBook(
            {},
            owners=owners,
            source=path,
            warnings=(f"could not read {path}: {exc}",),
        )

    entries = data.get("owners") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return DossierBook(
            {},
            owners=owners,
            source=path,
            warnings=(f"{path} has no `owners` object — treating it as empty",),
        )

    book: dict[str, OwnerDossier] = {}
    warnings: list[str] = []
    for key, raw in entries.items():
        if not isinstance(raw, dict):
            warnings.append(f"{path}: entry {key!r} is not an object — skipped")
            continue
        try:
            dossier = from_dict({"owner_id": key, **raw})
        except DossierError as exc:
            warnings.append(f"{path}: {exc}")
            continue
        book[dossier.owner_id] = dossier

    return DossierBook(
        book, owners=owners, source=path, warnings=tuple(warnings), resolver=resolver
    )


def save_dossiers(
    book: DossierBook, path: Path = DEFAULT_DOSSIER_PATH, *, league_id: int = 0
) -> None:
    """Write the whole file, sorted, human-readable.

    Sorted and indented because this file is meant to be opened and hand-edited
    — fixing a typo should not require re-running an interview — and because a
    stable ordering keeps its diffs readable across a season.
    """
    payload = {
        "schema_version": DOSSIER_SCHEMA_VERSION,
        "league_id": league_id,
        "owners": {
            dossier.owner_id: {
                k: v for k, v in to_dict(dossier).items() if k != "owner_id"
            }
            for dossier in book
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
