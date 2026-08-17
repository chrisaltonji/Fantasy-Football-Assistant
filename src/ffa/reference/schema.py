"""What a rankings/auction-value file is allowed to look like.

Written against the FantasyPros auction-values calculator, which is the source
in use — but nothing here is FantasyPros-specific in a way that would reject
another sheet. Header names are matched through an alias table, and the awkward
bits of that particular export (a title row above the real header, a single
cell holding name + team + position + positional rank) are handled as
*tolerated shapes* rather than assumptions.

The governing rule: a file the user exported at 11pm the night before the draft
should load, and anything we can't understand should be reported by row rather
than aborting the load.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ffa.domain.enums import Position

# --- canonical columns --------------------------------------------------------

PLAYER_NAME = "player_name"
POSITION = "position"
NFL_TEAM = "nfl_team"
AUCTION_VALUE = "auction_value"
OVERALL_RANK = "overall_rank"
POSITIONAL_RANK = "positional_rank"
ADP = "adp"
PROJECTED_POINTS = "projected_points"
TIER = "tier"
BYE_WEEK = "bye_week"
ESPN_PLAYER_ID = "espn_player_id"

# A row needs a name, and needs *either* a value or a rank we can derive one
# from. Everything else is a bonus.
REQUIRED = (PLAYER_NAME,)
VALUE_OR_RANK = (AUCTION_VALUE, OVERALL_RANK)

# Lowercased, punctuation-stripped header text -> canonical column.
ALIASES: dict[str, str] = {
    # name
    "player": PLAYER_NAME, "player name": PLAYER_NAME, "name": PLAYER_NAME,
    "players": PLAYER_NAME, "player names": PLAYER_NAME, "full name": PLAYER_NAME,
    # position
    "pos": POSITION, "position": POSITION, "player position": POSITION,
    # nfl team
    "team": NFL_TEAM, "tm": NFL_TEAM, "nfl team": NFL_TEAM, "pro team": NFL_TEAM,
    # auction value — the column FantasyPros calls "Value"
    "value": AUCTION_VALUE, "auction value": AUCTION_VALUE, "auction": AUCTION_VALUE,
    "aav": AUCTION_VALUE, "avg value": AUCTION_VALUE, "average value": AUCTION_VALUE,
    "price": AUCTION_VALUE, "cost": AUCTION_VALUE, "salary": AUCTION_VALUE,
    "$": AUCTION_VALUE, "dollars": AUCTION_VALUE, "your value": AUCTION_VALUE,
    # ranks
    "rank": OVERALL_RANK, "overall rank": OVERALL_RANK, "ovr": OVERALL_RANK,
    "rk": OVERALL_RANK, "#": OVERALL_RANK, "ecr": OVERALL_RANK,
    "pos rank": POSITIONAL_RANK, "positional rank": POSITIONAL_RANK,
    "position rank": POSITIONAL_RANK,
    # extras
    "adp": ADP, "average draft position": ADP,
    "proj": PROJECTED_POINTS, "projected points": PROJECTED_POINTS,
    "points": PROJECTED_POINTS, "fpts": PROJECTED_POINTS, "projection": PROJECTED_POINTS,
    "tier": TIER, "tiers": TIER,
    "bye": BYE_WEEK, "bye week": BYE_WEEK,
    "espn id": ESPN_PLAYER_ID, "espn player id": ESPN_PLAYER_ID,
    "player id": ESPN_PLAYER_ID, "id": ESPN_PLAYER_ID,
}

NUMERIC = {
    AUCTION_VALUE: float,
    OVERALL_RANK: int,
    POSITIONAL_RANK: int,
    ADP: float,
    PROJECTED_POINTS: float,
    TIER: int,
    BYE_WEEK: int,
}

_PUNCT = re.compile(r"[^a-z0-9$# ]+")
_SPACES = re.compile(r"\s+")


def normalize_header(raw: str) -> str:
    text = _SPACES.sub(" ", _PUNCT.sub(" ", str(raw or "").strip().lower())).strip()
    return text


def canonical_column(raw: str) -> str | None:
    """Map one header cell to a canonical column, or None if unrecognized."""
    return ALIASES.get(normalize_header(raw))


def map_headers(cells) -> dict[int, str]:
    """Column index -> canonical name, for the cells we recognize."""
    mapping: dict[int, str] = {}
    for index, cell in enumerate(cells):
        column = canonical_column(cell)
        # First occurrence wins: FantasyPros repeats "Value"-ish headers for
        # its own value vs. consensus value, and the leftmost is the one the
        # user configured.
        if column and column not in mapping.values():
            mapping[index] = column
    return mapping


def looks_like_header(cells) -> bool:
    """Is this row the header?

    Requires a name column plus one other recognized column. A single match is
    too weak — a data row containing a player literally named "Value" would
    otherwise be mistaken for the header.
    """
    mapping = map_headers(cells)
    return PLAYER_NAME in mapping.values() and len(mapping) >= 2


# --- combined player cells ----------------------------------------------------
#
# FantasyPros renders a player as one cell carrying up to four facts. Several
# spellings are in circulation depending on where you copy from, so match a few
# and fall back to "the whole cell is the name".

_COMBINED = (
    # Ja'Marr Chase CIN (WR1)
    re.compile(r"^(?P<name>.+?)\s+(?P<team>[A-Za-z]{2,4})\s*\(\s*(?P<pos>[A-Za-z]{1,3})\s*(?P<rank>\d+)?\s*\)$"),
    # Ja'Marr Chase (CIN - WR)  /  Ja'Marr Chase (CIN, WR)
    re.compile(r"^(?P<name>.+?)\s*\(\s*(?P<team>[A-Za-z]{2,4})\s*[-,/]\s*(?P<pos>[A-Za-z]{1,3})\s*\)$"),
    # Ja'Marr Chase (WR - CIN)
    re.compile(r"^(?P<name>.+?)\s*\(\s*(?P<pos>QB|RB|WR|TE|K|DST|D/ST|DEF)\s*[-,/]\s*(?P<team>[A-Za-z]{2,4})\s*\)$", re.I),
    # Ja'Marr Chase, CIN WR
    re.compile(r"^(?P<name>.+?)\s*,\s*(?P<team>[A-Za-z]{2,4})\s+(?P<pos>[A-Za-z]{1,3})$"),
)


@dataclass(frozen=True)
class PlayerCell:
    name: str
    nfl_team: str | None = None
    position: Position | None = None
    positional_rank: int | None = None


def parse_player_cell(raw: str) -> PlayerCell:
    """Pull name/team/position/positional-rank out of a single cell.

    Falls back to treating the whole cell as the name, which is correct for
    sheets that keep these in separate columns.
    """
    text = _SPACES.sub(" ", str(raw or "").strip())
    if not text:
        return PlayerCell(name="")

    for pattern in _COMBINED:
        match = pattern.match(text)
        if not match:
            continue
        groups = match.groupdict()
        position = _maybe_position(groups.get("pos"))
        if position is None:
            # A trailing parenthetical that isn't a position — a nickname, a
            # note — means this isn't a combined cell after all.
            continue
        rank = groups.get("rank")
        return PlayerCell(
            name=groups["name"].strip(),
            nfl_team=(groups.get("team") or "").upper() or None,
            position=position,
            positional_rank=int(rank) if rank else None,
        )

    return PlayerCell(name=text)


def _maybe_position(raw: str | None) -> Position | None:
    if not raw:
        return None
    try:
        return Position.parse(raw)
    except ValueError:
        return None


def parse_money(raw) -> float | None:
    """`$45`, `45`, `45.0`, `$1` -> float. Blanks and dashes -> None."""
    text = str(raw or "").strip().replace("$", "").replace(",", "")
    if not text or text in {"-", "--", "n/a", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None
