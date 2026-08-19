"""The player universe: ESPN's own auction values, ADP and projections.

ESPN publishes a consensus auction value per player as
`player.ownership.auctionValueAverage`, alongside average draft position and a
season projection. For a league that drafts on ESPN this is arguably the best
available source, because it is what ESPN drafters actually pay — a
third-party sheet is what a different population thinks they should.

Two caveats worth knowing rather than discovering mid-draft:

- ESPN prices roughly 355 players, summing to about 79% of the money in a
  12-team $200 league. That is not a bug and must not be "corrected" by
  scaling up: the rest of the money genuinely goes on $1 fliers outside the
  priced set. Only 180 players get drafted, so coverage is not the problem.
- The values are a season-long average, not a live market read. Anything
  modelling in-draft inflation needs this unscaled, as a baseline to measure
  against.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from espn_fantasy.enums import POSITION_IDS, PRO_TEAM_IDS

COLUMNS = (
    "player_name", "position", "nfl_team", "auction_value",
    "adp", "projected_points", "espn_player_id",
)


@dataclass(frozen=True)
class PlayerValue:
    """One priced player, as ESPN sees them."""

    name: str
    position: str
    nfl_team: str = ""
    auction_value: float | None = None
    adp: float | None = None
    projected_points: float | None = None
    espn_player_id: int | None = None

    def as_row(self) -> dict[str, Any]:
        """A flat dict with the CSV's column names, blanks for unknowns."""
        return {
            "player_name": self.name,
            "position": self.position,
            "nfl_team": self.nfl_team,
            "auction_value": self.auction_value if self.auction_value is not None else "",
            "adp": self.adp if self.adp is not None else "",
            "projected_points": (
                self.projected_points if self.projected_points is not None else ""
            ),
            "espn_player_id": self.espn_player_id if self.espn_player_id is not None else "",
        }


def _projected_points(player: Mapping[str, Any], year: int) -> float | None:
    """Season projection, if ESPN shipped one.

    `statSourceId == 1` is the projection (0 is actuals) and
    `statSplitTypeId == 0` is the full season. Best effort — a missing
    projection is not worth failing an import over.
    """
    for entry in player.get("stats") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("statSourceId") != 1 or entry.get("statSplitTypeId") != 0:
            continue
        if entry.get("seasonId") not in (year, str(year), None):
            continue
        total = entry.get("appliedTotal")
        if isinstance(total, (int, float)):
            return round(float(total), 1)
    return None


def players_from_payload(
    players: Sequence[Mapping[str, Any]],
    *,
    year: int,
    priced_only: bool = True,
) -> tuple[list[PlayerValue], list[str]]:
    """Turn ESPN's player list into typed records, plus warnings.

    With `priced_only` (the default), players ESPN did not price are dropped
    rather than emitted with a value of `0`. That distinction matters more
    than it looks: zero is a *known* value and says the player is worth
    nothing, which is a different claim from "ESPN did not price him".
    """
    out: list[PlayerValue] = []
    warnings: list[str] = []
    unmapped_positions: set[int] = set()
    unpriced = 0

    for entry in players:
        player = entry.get("player") if isinstance(entry, dict) else None
        if not isinstance(player, dict):
            continue

        name = (player.get("fullName") or "").strip()
        if not name:
            continue

        ownership = player.get("ownership") or {}
        raw_value = ownership.get("auctionValueAverage")
        priced = isinstance(raw_value, (int, float)) and raw_value > 0
        if not priced:
            unpriced += 1
            if priced_only:
                continue

        position_id = player.get("defaultPositionId")
        position = POSITION_IDS.get(position_id) if isinstance(position_id, int) else None
        if position is None:
            if isinstance(position_id, int):
                unmapped_positions.add(position_id)
            continue

        adp = ownership.get("averageDraftPosition")
        player_id = player.get("id")
        out.append(
            PlayerValue(
                name=name,
                position=position,
                nfl_team=PRO_TEAM_IDS.get(player.get("proTeamId"), ""),
                # Whole dollars is what gets used downstream, but two places
                # are kept here: rounding 355 values to int loses the ordering
                # between a $4.4 and a $4.6 player.
                auction_value=round(float(raw_value), 2) if priced else None,
                adp=round(float(adp), 2) if isinstance(adp, (int, float)) and adp else None,
                projected_points=_projected_points(player, year),
                espn_player_id=int(player_id) if isinstance(player_id, int) else None,
            )
        )

    # Priced players first, most expensive first; unpriced ones trail in name
    # order rather than being scattered through the list by a `None` sort key.
    out.sort(key=lambda p: (p.auction_value is None, -(p.auction_value or 0), p.name))

    if unmapped_positions:
        warnings.append(
            f"skipped players at unmapped position ids {sorted(unmapped_positions)} "
            "(defensive players and coaches are not draftable in a standard league)"
        )
    if unpriced:
        verb = "dropped" if priced_only else "kept with auction_value unset"
        warnings.append(
            f"{unpriced} player(s) had no ESPN auction value and were {verb} "
            "rather than recorded as $0"
        )
    return out, warnings


def write_csv(players: Iterable[PlayerValue], path: Path) -> int:
    """Write players to CSV. Returns the row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        for player in players:
            writer.writerow(player.as_row())
            written += 1
    return written


def fetch_players(
    client: Any, *, limit: int | None = None, priced_only: bool = True
) -> tuple[list[PlayerValue], list[str]]:
    """Fetch and parse the player universe in one call."""
    kwargs = {} if limit is None else {"limit": limit}
    raw = client.players(**kwargs)
    return players_from_payload(raw, year=client.year, priced_only=priced_only)
