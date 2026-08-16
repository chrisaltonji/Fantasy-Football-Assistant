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
