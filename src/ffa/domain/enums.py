"""Closed vocabularies shared across the whole app.

Kept deliberately small and dependency-free: config, ingest, reference, and
advice all import from here, so anything with real logic belongs elsewhere.
"""

from __future__ import annotations

from enum import Enum


class Position(str, Enum):
    """A player's actual NFL position."""

    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    K = "K"
    DST = "DST"

    @classmethod
    def parse(cls, raw: str) -> "Position":
        """Normalize the many spellings these show up as in the wild.

        ESPN, FantasyPros, and hand-built spreadsheets all disagree about
        defenses in particular (D/ST, DEF, DST, D).
        """
        key = (raw or "").strip().upper().replace("/", "").replace(" ", "")
        aliases = {
            "PK": cls.K,
            "KICKER": cls.K,
            "DEF": cls.DST,
            "DST": cls.DST,
            "D": cls.DST,
            "DEFENSE": cls.DST,
            "TEAMDEFENSE": cls.DST,
            "QUARTERBACK": cls.QB,
            "RUNNINGBACK": cls.RB,
            "WIDERECEIVER": cls.WR,
            "TIGHTEND": cls.TE,
        }
        if key in aliases:
            return aliases[key]
        try:
            return cls(key)
        except ValueError as exc:
            raise ValueError(f"unrecognized position: {raw!r}") from exc


class Provenance(str, Enum):
    """Where a single observed value came from.

    Ordering matters: `rank` decides which of two competing observations of the
    same field wins. See `ffa.domain.sourced.merge` — but note that provenance
    is only consulted when *both* values are known. Knowledge beats ignorance
    regardless of rank.
    """

    UNKNOWN = "UNKNOWN"
    INFERRED = "INFERRED"
    SIM = "SIM"
    ESPN_API = "ESPN_API"
    MANUAL = "MANUAL"

    @property
    def rank(self) -> int:
        return _PROVENANCE_RANK[self]

    @classmethod
    def parse(cls, raw: str) -> "Provenance":
        """Unknown provenance demotes to UNKNOWN rather than raising.

        A journal written by a newer version may carry a provenance we don't
        know. Demoting is safer than crashing (loses the draft) and safer than
        promoting (would let an unknown source outrank a manual correction).
        """
        try:
            return cls(str(raw).strip().upper())
        except ValueError:
            return cls.UNKNOWN


# Explicit table rather than declaration order, so reordering the enum for
# readability can never silently change merge behaviour.
_PROVENANCE_RANK = {
    Provenance.UNKNOWN: 0,
    Provenance.INFERRED: 1,
    Provenance.SIM: 2,
    Provenance.ESPN_API: 3,
    Provenance.MANUAL: 4,
}


class RosterSlot(str, Enum):
    """A slot on a fantasy roster. Distinct from Position because of FLEX."""

    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    FLEX = "FLEX"
    K = "K"
    DST = "DST"
    BE = "BE"
    IR = "IR"

    @property
    def is_starter(self) -> bool:
        """Bench and IR don't contribute points; they still cost roster space."""
        return self not in (RosterSlot.BE, RosterSlot.IR)
