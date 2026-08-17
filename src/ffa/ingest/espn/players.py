"""Pull auction values straight from ESPN, instead of exporting them by hand.

ESPN publishes its own consensus auction value per player as
`player.ownership.auctionValueAverage`, alongside ADP and projections. That
removes the manual FantasyPros export the build had been waiting on — and it is
arguably the better source for *this* league, because the league drafts on
ESPN. ESPN's number is what ESPN drafters actually pay; a third-party sheet is
what a different population thinks they should.

Two caveats worth knowing rather than discovering mid-draft:

- ESPN returns values for ~355 players, summing to roughly 79% of the money in
  a 12-team $200 league. That is not a bug and must not be "corrected" by
  scaling up: the remaining money genuinely goes on $1 fliers outside the
  valued set. Only 180 players get drafted, so coverage is not the issue.
- The values are a season-long average, not a live market read. Inflation
  during the draft is `advice.market`'s job, and it needs an unscaled baseline
  to measure against.

Output is a CSV with the loader's canonical headers, so it goes through exactly
the same tolerant loader, `PlayerBook`, and `ffa data validate` as a
hand-exported sheet. Nothing downstream learns where it came from.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ffa.config.schema import ConfigError
from ffa.ingest.espn.settings import HOSTS, USER_AGENT

# ESPN's `defaultPositionId`. Only fantasy-relevant positions are mapped;
# anything else is a defensive player or a coach and is dropped.
POSITION_IDS: Mapping[int, str] = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    16: "DST",
}

# ESPN's `proTeamId`. 0 is free agency. Display text only — nothing keys off
# it — but a wrong team on screen mid-draft costs a second of doubt at exactly
# the wrong moment.
PRO_TEAM_IDS: Mapping[int, str] = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
    7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV",
    14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ",
    21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB",
    28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}

# The loader's canonical headers, so the output needs no alias resolution.
COLUMNS = (
    "player_name", "position", "nfl_team", "auction_value",
    "adp", "projected_points", "espn_player_id",
)

DEFAULT_LIMIT = 900


def _projected_points(player: Mapping[str, Any], year: int) -> float | None:
    """Season projection, if ESPN shipped one.

    `statSourceId == 1` is the projection (0 is actuals) and
    `statSplitTypeId == 0` is the full season. Best effort: a missing
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


def rows_from_payload(
    players: Sequence[Mapping[str, Any]], *, year: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn ESPN's player list into loader-ready rows, plus warnings.

    Players with no auction value are dropped rather than written as `0`. A
    zero is a *known* value and would tell the advisory layer the player is
    worth nothing, which is a different claim from "ESPN did not price him".
    """
    rows: list[dict[str, Any]] = []
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
        value = ownership.get("auctionValueAverage")
        if not isinstance(value, (int, float)) or value <= 0:
            unpriced += 1
            continue

        position_id = player.get("defaultPositionId")
        position = POSITION_IDS.get(position_id) if isinstance(position_id, int) else None
        if position is None:
            if isinstance(position_id, int):
                unmapped_positions.add(position_id)
            continue

        adp = ownership.get("averageDraftPosition")
        rows.append({
            "player_name": name,
            "position": position,
            "nfl_team": PRO_TEAM_IDS.get(player.get("proTeamId"), ""),
            # Whole dollars downstream, but keep two places here: rounding 355
            # values to int loses the ordering between $4.4 and $4.6 players,
            # and the loader is happy with floats.
            "auction_value": round(float(value), 2),
            "adp": round(float(adp), 2) if isinstance(adp, (int, float)) and adp else "",
            "projected_points": _projected_points(player, year) or "",
            "espn_player_id": player.get("id", ""),
        })

    rows.sort(key=lambda r: (-r["auction_value"], r["player_name"]))

    if unmapped_positions:
        warnings.append(
            f"skipped players at unmapped position ids {sorted(unmapped_positions)} "
            "(defensive players / coaches are not draftable here)"
        )
    if unpriced:
        warnings.append(
            f"{unpriced} player(s) had no ESPN auction value and were dropped "
            "rather than written as $0"
        )
    return rows, warnings


def write_rows(rows: Iterable[Mapping[str, Any]], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in COLUMNS})
            written += 1
    return written


def fetch_players(
    league_id: int,
    year: int,
    *,
    cookies: Mapping[str, str] | None = None,
    limit: int = DEFAULT_LIMIT,
    timeout: float = 45.0,
) -> list[dict[str, Any]]:
    """Fetch the player universe, most-owned first.

    The `x-fantasy-filter` header is not optional — without it ESPN returns a
    handful of players and no amount of query string coaxing changes that.
    """
    import requests

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "x-fantasy-filter": json.dumps({
            "players": {
                "limit": int(limit),
                "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
            }
        }),
    })
    if cookies:
        session.cookies.update(dict(cookies))

    last = "no hosts tried"
    for host in HOSTS:
        url = (
            f"{host}/apis/v3/games/ffl/seasons/{year}"
            f"/segments/0/leagues/{league_id}?view=kona_player_info"
        )
        try:
            response = session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue
        if response.status_code == 401:
            raise ConfigError(
                "ESPN returned 401 fetching players. Re-copy espn_s2 / SWID."
            )
        if response.status_code != 200:
            last = f"HTTP {response.status_code}"
            continue
        try:
            return list(response.json().get("players") or [])
        except ValueError:
            last = "response was not JSON"

    raise ConfigError(f"could not fetch players for league {league_id}: {last}")
