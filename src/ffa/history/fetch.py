"""Pulling prior seasons out of ESPN, and saying what came back.

Three views per season, and each is load-bearing:

- `mDraftDetail` — the picks, with `bidAmount` and, crucially,
  `nominatingTeamId`. Who *put a player up* is half the read on a manager and
  is not recoverable from anywhere else.
- `mTeam` — which SWID held which seat that year. Team ids happen to be stable
  in this league, but a profile keyed on the seat blends two people the first
  time somebody leaves, so the join is always made through the owner.
- `kona_player_info` — names, positions, pro teams, and the season's price
  reference.

**The price reference is not the same field every year**, which is the single
most surprising thing here and the reason the source is recorded per season
rather than assumed:

| season | `ownership.auctionValueAverage` | `draftRanksByRankType.PPR.auctionValue` |
|---|---|---|
| 2022 | 348 players | none |
| 2023 | 365 | 160 |
| 2024 | 314 | 160 |
| 2025 | **all zero** | 160 |

So 2025 would silently produce "everybody chases everything" if the first field
were trusted blindly. Whatever source is used is scaled to that season's actual
money before anything is compared, so a season covered by 160 editorial values
and one covered by 348 consensus averages can sit in the same table.

Raw payloads are cached on disk. The analysis is the part that can be wrong and
it should be iterable without re-hitting ESPN twelve times.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ffa.config.schema import ConfigError
from ffa.history.schema import DraftPick, History, SeasonDraft, SeasonGap
from ffa.ingest.espn.players import POSITION_IDS, PRO_TEAM_IDS
from ffa.ingest.espn.settings import HOSTS, USER_AGENT

DEFAULT_CACHE = Path("probe_out/history")

# Enough to cover every drafted player and then some; the drafted set has come
# back 100% covered at this limit for every season tested.
PLAYER_LIMIT = 1500

# Below this, a "reference" is noise — a $1 flier priced at $0.4 is not a chase.
MIN_REFERENCE = 1.0


# --- the wire ------------------------------------------------------------------


def _session(cookies: Mapping[str, str] | None):
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    if cookies:
        session.cookies.update(dict(cookies))
    return session


def fetch_view(
    session, league_id: int, year: int, view: str, *, headers=None, timeout: float = 60.0
) -> tuple[dict[str, Any] | None, str]:
    """One view for one season. Returns (payload, status) — never raises on 404.

    A season that is simply not there is an ordinary answer, not an error: this
    walks back through years that may predate the league.
    """
    import requests

    last = "no hosts tried"
    for host in HOSTS:
        url = (
            f"{host}/apis/v3/games/ffl/seasons/{year}"
            f"/segments/0/leagues/{league_id}?view={view}"
        )
        try:
            response = session.get(url, timeout=timeout, headers=headers or {})
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            continue
        if response.status_code == 401:
            raise ConfigError(
                "ESPN returned 401 reading draft history. Re-copy espn_s2 / SWID."
            )
        if response.status_code != 200:
            last = f"HTTP {response.status_code}"
            continue
        try:
            return response.json(), "ok"
        except ValueError:
            last = "response was not JSON"
    return None, last


def download_season(
    league_id: int, year: int, *, cookies=None, cache: Path = DEFAULT_CACHE
) -> dict[str, Any]:
    """Fetch the three views for one season and cache them."""
    session = _session(cookies)
    cache.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {"year": year, "league_id": league_id}

    for view, headers in (
        ("mSettings", None),
        ("mTeam", None),
        ("mDraftDetail", None),
        (
            "kona_player_info",
            {
                "x-fantasy-filter": json.dumps({
                    "players": {
                        "limit": PLAYER_LIMIT,
                        "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
                    }
                })
            },
        ),
    ):
        payload, status = fetch_view(session, league_id, year, view, headers=headers)
        out[view] = payload
        out[f"{view}_status"] = status

    (cache / f"{year}.json").write_text(json.dumps(out), encoding="utf-8")
    return out


def load_cached(year: int, cache: Path = DEFAULT_CACHE) -> dict[str, Any] | None:
    path = cache / f"{year}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


# --- turning a payload into a season -------------------------------------------


def price_reference(player_payload: Mapping[str, Any] | None) -> tuple[dict[int, float], str]:
    """`player_id -> consensus price`, and which field it came from.

    Preference is by coverage, not by name: `auctionValueAverage` reaches ~350
    players where it exists at all, ESPN's editorial `auctionValue` reaches the
    top 160. Falling through matters — 2025 returns the first field as a column
    of zeroes, and a zero consensus would make every single purchase look like
    an overpay.
    """
    players = (player_payload or {}).get("players") or []

    average: dict[int, float] = {}
    editorial: dict[int, float] = {}
    for entry in players:
        player = entry.get("player") or {}
        player_id = entry.get("id") or player.get("id")
        if player_id is None:
            continue
        value = (player.get("ownership") or {}).get("auctionValueAverage") or 0
        if value and float(value) >= MIN_REFERENCE:
            average[int(player_id)] = float(value)
        ranks = player.get("draftRanksByRankType") or {}
        rank = ranks.get("PPR") or ranks.get("STANDARD") or {}
        editorial_value = rank.get("auctionValue") or 0
        if editorial_value and float(editorial_value) >= MIN_REFERENCE:
            editorial[int(player_id)] = float(editorial_value)

    # Merged rather than one-or-the-other. They disagree on coverage in both
    # directions — 2022 has 348 consensus averages and no editorial values,
    # 2025 has 160 editorial values and a column of zeroes — and a player
    # excluded for want of a reference is a player whose purchase cannot be
    # judged at all. Consensus wins where both exist: it is an average of what
    # drafters actually paid, where the editorial number is one desk's opinion.
    merged = dict(editorial)
    merged.update(average)

    if not merged:
        return {}, "none available"
    if average and editorial:
        source = (
            f"ESPN consensus for {len(average)} players, editorial value for "
            f"{len(set(editorial) - set(average))} more"
        )
    elif average:
        source = f"ESPN consensus (ownership.auctionValueAverage), {len(average)} players"
    else:
        source = f"ESPN editorial (draftRanks.auctionValue), {len(editorial)} players"
    return merged, source


def player_index(player_payload: Mapping[str, Any] | None) -> dict[int, dict[str, str]]:
    """`player_id -> {name, position, pro_team}`."""
    out: dict[int, dict[str, str]] = {}
    for entry in (player_payload or {}).get("players") or []:
        player = entry.get("player") or {}
        player_id = entry.get("id") or player.get("id")
        if player_id is None:
            continue
        out[int(player_id)] = {
            "name": str(player.get("fullName") or "").strip(),
            "position": POSITION_IDS.get(int(player.get("defaultPositionId") or 0), ""),
            "pro_team": PRO_TEAM_IDS.get(int(player.get("proTeamId") or 0), ""),
        }
    return out


def _owners(team_payload: Mapping[str, Any] | None) -> tuple[dict[int, str], dict[int, str]]:
    owners: dict[int, str] = {}
    names: dict[int, str] = {}
    for team in (team_payload or {}).get("teams") or []:
        try:
            team_id = int(team["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if team.get("primaryOwner"):
            owners[team_id] = str(team["primaryOwner"])
        name = (team.get("name") or "").strip()
        if name:
            names[team_id] = name
    return owners, names


def season_from_payload(raw: Mapping[str, Any]) -> SeasonDraft | SeasonGap:
    """One cached season -> a usable draft, or a stated reason it is not one."""
    year = int(raw.get("year") or 0)
    settings_payload = raw.get("mSettings")
    if not settings_payload:
        return SeasonGap(year, f"ESPN did not serve this season ({raw.get('mSettings_status')})")

    settings = settings_payload.get("settings") or {}
    draft_settings = settings.get("draftSettings") or {}
    draft_type = str(draft_settings.get("type") or "").upper()
    if draft_type != "AUCTION":
        return SeasonGap(
            year,
            f"draft type was {draft_type or 'unknown'}, not AUCTION — no prices "
            "and no nomination order to read",
        )

    detail = (raw.get("mDraftDetail") or {}).get("draftDetail") or {}
    all_picks = detail.get("picks") or []
    filled = [p for p in all_picks if int(p.get("playerId", -1) or -1) != -1]
    if not filled:
        return SeasonGap(year, "the board is an unfilled skeleton — no picks recorded")

    priced = [p for p in filled if p.get("bidAmount")]
    if not priced:
        return SeasonGap(
            year,
            f"{len(filled)} picks recorded but every bidAmount is zero — prices "
            "were never written",
        )

    index = player_index(raw.get("kona_player_info"))
    reference, source = price_reference(raw.get("kona_player_info"))
    owners, team_names = _owners(raw.get("mTeam"))

    # Scale the reference to the money actually spent this season. Without it a
    # season covered by 160 editorial values and one covered by 350 consensus
    # averages are not comparable, and every cross-season number would be an
    # artefact of which field ESPN happened to populate.
    covered = [p for p in filled if int(p.get("playerId")) in reference]
    ref_total = sum(reference[int(p["playerId"])] for p in covered)
    actual_total = sum(int(p.get("bidAmount") or 0) for p in covered)
    scale = (actual_total / ref_total) if ref_total > 0 else 1.0

    picks = tuple(
        DraftPick(
            overall_pick=int(p.get("overallPickNumber") or 0),
            team_id=int(p.get("teamId") or 0),
            player_id=int(p.get("playerId")),
            price=int(p.get("bidAmount") or 0),
            player_name=index.get(int(p["playerId"]), {}).get("name", ""),
            position=index.get(int(p["playerId"]), {}).get("position", ""),
            pro_team=index.get(int(p["playerId"]), {}).get("pro_team", ""),
            nominating_team_id=(
                int(p["nominatingTeamId"]) if p.get("nominatingTeamId") else None
            ),
            is_keeper=bool(p.get("keeper")),
            auto_drafted=bool(p.get("autoDraftTypeId")),
            reference=(
                round(reference[int(p["playerId"])] * scale, 2)
                if int(p["playerId"]) in reference
                else None
            ),
        )
        for p in sorted(filled, key=lambda p: int(p.get("overallPickNumber") or 0))
    )

    return SeasonDraft(
        year=year,
        league_id=int(raw.get("league_id") or 0),
        team_count=int(settings.get("size") or len(owners)),
        budget=int(draft_settings.get("auctionBudget") or 200),
        picks=picks,
        owners=owners,
        team_names=team_names,
        reference_source=source,
        reference_coverage=(len(covered) / len(filled)) if filled else 0.0,
    )


def build_history(
    years: "list[int]", *, cache: Path = DEFAULT_CACHE, league_name: str = "",
    league_id: int = 0,
) -> History:
    """Assemble every cached season into one record, gaps included."""
    seasons: list[SeasonDraft] = []
    gaps: list[SeasonGap] = []

    for year in sorted(years):
        raw = load_cached(year, cache)
        if raw is None:
            gaps.append(SeasonGap(year, "not fetched — no cached payload"))
            continue
        result = season_from_payload(raw)
        if isinstance(result, SeasonGap):
            gaps.append(result)
        else:
            seasons.append(result)

    return History(
        seasons=tuple(seasons),
        gaps=tuple(gaps),
        league_name=league_name,
        league_id=league_id,
    )
