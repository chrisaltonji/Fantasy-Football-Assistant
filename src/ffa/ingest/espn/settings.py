"""Build a `LeagueConfig` from ESPN's raw settings and team views.

`config/league.toml` is gitignored — it holds the real league id, member SWIDs
and manager nicknames — so it is *expected* to go missing on a new machine or a
fresh checkout. Losing it should cost one command, not an afternoon of
hand-editing, which is what this exists for.

Everything here except `fetch_league_payloads` is pure and tested offline
against the committed fixtures. Two facts make the library route unusable and
force reading the raw views:

- `espn-api`'s `BaseSettings` drops both `auctionBudget` and the draft `type`.
- Team ids are not contiguous, so they have to be read rather than generated.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ffa.config.schema import ConfigError, LeagueConfig
from ffa.domain.enums import Position, RosterSlot

HOSTS = (
    "https://lm-api-reads.fantasy.espn.com",
    "https://fantasy.espn.com",
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# ESPN's lineup slot ids. Only the ones a football league can actually roster
# are mapped; anything else is reported rather than silently dropped, because a
# slot we ignore is a roster spot the budget arithmetic would never know about.
#
# 3 (RB/WR) and 5 (WR/TE) are ESPN's older restricted flex slots. They are
# folded into FLEX deliberately: this codebase models one flex with an explicit
# `flex_positions` list, and treating a restricted flex as a full one would
# overstate what fits there. Any league using them should check the result.
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


def roster_from_lineup_counts(
    counts: Mapping[str, Any]
) -> tuple[dict[RosterSlot, int], list[str]]:
    """`{"0": 1, "2": 2, ...}` -> `{QB: 1, RB: 2, ...}`, plus any warnings.

    Zero-count slots are dropped: ESPN sends every slot id it knows about, and
    keeping the zeros would put empty QB/DT/LB entries into the config.
    """
    roster: dict[RosterSlot, int] = {}
    warnings: list[str] = []

    for raw_id, raw_count in (counts or {}).items():
        try:
            slot_id, count = int(raw_id), int(raw_count)
        except (TypeError, ValueError):
            warnings.append(f"skipped unreadable lineup slot {raw_id!r}: {raw_count!r}")
            continue
        if count <= 0:
            continue

        slot = LINEUP_SLOT_IDS.get(slot_id)
        if slot is None:
            warnings.append(
                f"lineup slot id {slot_id} (count {count}) has no mapping — "
                "those roster spots are not counted in budget arithmetic"
            )
            continue
        roster[slot] = roster.get(slot, 0) + count

    return roster, warnings


def _members_by_id(payload: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for member in payload.get("members") or []:
        member_id = member.get("id")
        if not member_id:
            continue
        name = (
            member.get("displayName")
            or " ".join(
                p for p in (member.get("firstName"), member.get("lastName")) if p
            ).strip()
        )
        if name:
            out[str(member_id)] = str(name)
    return out


def teams_from_payload(
    payload: Mapping[str, Any]
) -> tuple[tuple[int, ...], dict[int, str], dict[int, str], dict[int, str]]:
    """Return (team_ids, owners, managers, team_names).

    Ids come back sorted and exactly as ESPN reports them. Never generate them:
    the real league runs 1-5 and 7-13, and `range(1, count + 1)` would invent a
    team 6 while dropping team 13.
    """
    members = _members_by_id(payload)
    team_ids: list[int] = []
    owners: dict[int, str] = {}
    managers: dict[int, str] = {}
    names: dict[int, str] = {}

    for team in payload.get("teams") or []:
        try:
            team_id = int(team["id"])
        except (KeyError, TypeError, ValueError):
            continue
        team_ids.append(team_id)

        name = (team.get("name") or "").strip()
        if name:
            names[team_id] = name

        owner = team.get("primaryOwner")
        if owner:
            owners[team_id] = str(owner)
            nickname = members.get(str(owner))
            if nickname:
                managers[team_id] = nickname

    return tuple(sorted(team_ids)), owners, managers, names


def my_team_id(owners: Mapping[int, str], swid: str | None) -> int:
    """Which team belongs to the SWID in `.env`.

    Returns 0 when it cannot be determined, which the caller must treat as a
    hard error before a draft: every projection keys off this, so guessing
    would produce confident advice about somebody else's team.
    """
    if not swid:
        return 0
    wanted = {swid, swid.strip("{}"), "{" + swid.strip("{}") + "}"}
    for team_id, owner in owners.items():
        if owner in wanted or owner.strip("{}") in {w.strip("{}") for w in wanted}:
            return team_id
    return 0


def config_from_payloads(
    settings_payload: Mapping[str, Any],
    teams_payload: Mapping[str, Any],
    *,
    league_id: int,
    year: int,
    swid: str | None = None,
    private: bool = True,
) -> tuple[LeagueConfig, list[str]]:
    """Assemble a `LeagueConfig`. Pure — no network, no files."""
    settings = settings_payload.get("settings")
    if not isinstance(settings, dict):
        raise ConfigError(
            "ESPN returned no `settings` block. Request the mSettings view for "
            "this league and season."
        )

    draft = settings.get("draftSettings") or {}
    roster_settings = settings.get("rosterSettings") or {}
    scoring = settings.get("scoringSettings") or {}

    roster, warnings = roster_from_lineup_counts(roster_settings.get("lineupSlotCounts") or {})
    if not roster:
        raise ConfigError(
            "ESPN's rosterSettings carried no usable lineup slots, so roster "
            "size and max-bid arithmetic cannot be derived."
        )

    team_ids, owners, managers, team_names = teams_from_payload(teams_payload)
    mine = my_team_id(owners, swid)
    if not mine:
        warnings.append(
            "could not match your SWID to a team — set [teams].my_team_id by "
            "hand before drafting, or advice will describe the wrong team"
        )

    size = settings.get("size")
    team_count = int(size) if size else len(team_ids)
    if team_ids and team_count != len(team_ids):
        warnings.append(
            f"ESPN reports size={team_count} but returned {len(team_ids)} teams; "
            "using the ids it actually returned"
        )
        team_count = len(team_ids)

    draft_type = str(draft.get("type") or "AUCTION").upper()
    if draft_type != "AUCTION":
        warnings.append(
            f"this league's draft type is {draft_type}, not AUCTION — "
            "budget and max-bid advice assumes an auction"
        )

    config = LeagueConfig(
        league_id=league_id,
        year=year,
        private=private,
        name=str(settings.get("name") or ""),
        draft_type=draft_type,
        budget=int(draft.get("auctionBudget") or 200),
        team_count=team_count,
        my_team_id=mine,
        team_ids=team_ids,
        roster=roster,
        flex_positions=(Position.RB, Position.WR, Position.TE),
        scoring_type=str(scoring.get("scoringType") or "PPR"),
        managers=managers,
        owners=owners,
        team_names=team_names,
    )
    return config, warnings


# --- the one impure function ------------------------------------------------


def fetch_league_payloads(
    league_id: int,
    year: int,
    *,
    cookies: Mapping[str, str] | None = None,
    timeout: float = 20.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch (mSettings, mTeam). Tries each host, like the probe does."""
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    if cookies:
        session.cookies.update(dict(cookies))

    def get(view: str) -> dict[str, Any]:
        last = "no hosts tried"
        for host in HOSTS:
            url = (
                f"{host}/apis/v3/games/ffl/seasons/{year}"
                f"/segments/0/leagues/{league_id}?view={view}"
            )
            try:
                response = session.get(url, timeout=timeout)
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
            if response.status_code == 401:
                raise ConfigError(
                    "ESPN returned 401. Your espn_s2 / SWID cookies are missing, "
                    "expired, or belong to an account that cannot see this league."
                )
            if response.status_code == 404:
                raise ConfigError(
                    f"ESPN returned 404 for league {league_id} in {year}. Check the "
                    "league id and season. A private league with no cookies also "
                    "surfaces as 404."
                )
            if response.status_code != 200:
                last = f"HTTP {response.status_code}"
                continue
            try:
                return response.json()
            except ValueError:
                last = "response was not JSON"
        raise ConfigError(f"could not fetch {view} for league {league_id}: {last}")

    return get("mSettings"), get("mTeam")
