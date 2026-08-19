"""Closed vocabularies, and the id tables ESPN encodes them with.

Kept dependency-free: everything else in the package imports from here.

The id maps are the part that is genuinely hard-won. They are not published
anywhere by ESPN, they are not guessable, and a wrong entry is invisible — a
mis-mapped lineup slot is a roster spot your arithmetic never knows about.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping


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
        """Bench and IR don't score; they still cost roster space."""
        return self not in (RosterSlot.BE, RosterSlot.IR)


# ESPN's `lineupSlotCounts` keys. Only the slots a football league can actually
# roster are mapped; anything else is reported by the caller rather than
# silently dropped.
#
# 3 (RB/WR) and 5 (WR/TE) are ESPN's older *restricted* flex slots. They fold
# into FLEX here, which is a lossy simplification: a restricted flex accepts
# fewer positions than a full one. A league using them should check the result.
LINEUP_SLOT_IDS: Mapping[int, RosterSlot] = {
    0: RosterSlot.QB,
    2: RosterSlot.RB,
    3: RosterSlot.FLEX,
    4: RosterSlot.WR,
    5: RosterSlot.FLEX,
    6: RosterSlot.TE,
    16: RosterSlot.DST,
    17: RosterSlot.K,
    20: RosterSlot.BE,
    21: RosterSlot.IR,
    23: RosterSlot.FLEX,
}

# ESPN's `defaultPositionId`. Unmapped ids are defensive players or coaches.
POSITION_IDS: Mapping[int, str] = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    16: "DST",
}

# ESPN's `proTeamId`. 0 is free agency.
PRO_TEAM_IDS: Mapping[int, str] = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
    7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV",
    14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ",
    21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB",
    28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}
