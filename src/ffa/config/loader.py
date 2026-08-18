"""Load and write config, and pull ESPN credentials from the environment.

Secrets never appear in the TOML file. They come from the process environment
or a gitignored ``.env``, so a config file can be shared or pasted into an
issue without leaking a session cookie.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from ffa.config.schema import ConfigError, EspnCredentials, LeagueConfig
from ffa.domain.enums import Position, RosterSlot

DEFAULT_CONFIG_PATH = Path("config/league.toml")
DEFAULT_ENV_PATH = Path(".env")


def load_dotenv(path: Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Minimal ``.env`` reader.

    Deliberately not python-dotenv: we need `KEY=value`, comments, and quote
    stripping, and nothing else. Real environment variables win over the file
    so CI and one-off overrides work without editing anything.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path}:{lineno}: expected KEY=value, got {raw!r}")
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_credentials(
    env_path: Path = DEFAULT_ENV_PATH, *, required: bool = False
) -> EspnCredentials | None:
    """Return ESPN cookies, or None if they aren't configured.

    Public leagues don't need these, so a missing pair is only an error when
    the caller says the league is private.
    """
    file_values = load_dotenv(env_path)

    def get(key: str) -> str:
        return (os.environ.get(key) or file_values.get(key) or "").strip()

    s2, swid = get("ESPN_S2"), get("ESPN_SWID")
    if not s2 or not swid:
        if required:
            missing = [k for k, v in (("ESPN_S2", s2), ("ESPN_SWID", swid)) if not v]
            raise ConfigError(
                f"private league needs {' and '.join(missing)}, but they are not set.\n"
                "Copy .env.example to .env and fill them in — see README.md for how "
                "to pull them out of your browser."
            )
        return None
    return EspnCredentials(espn_s2=s2, swid=swid)


def _require(table: dict[str, Any], section: str, key: str) -> Any:
    if key not in table:
        raise ConfigError(f"config is missing [{section}].{key}")
    return table[key]


def _nomination_order(raw: Any) -> tuple[int, ...]:
    """`[draft].nomination_order` -> team ids.

    A malformed entry is a hard error rather than a silent drop: this list
    decides who the readout says is on the clock, and quietly ignoring a typo
    would leave the tool blank with no explanation for why.
    """
    try:
        return tuple(int(i) for i in raw or ())
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "[draft].nomination_order must be a list of ESPN team ids, e.g. "
            "nomination_order = [9, 11, 4, 3, 13, 5, 8, 10, 7, 2, 12, 1]"
        ) from exc


def parse_config(data: dict[str, Any]) -> LeagueConfig:
    """Turn parsed TOML into a validated LeagueConfig."""
    league = data.get("league", {})
    draft = data.get("draft", {})
    teams = data.get("teams", {})
    roster_raw = dict(data.get("roster", {}))
    scoring = data.get("scoring", {})
    managers_raw = data.get("managers", {})
    owners_raw = data.get("owners", {})
    team_names_raw = data.get("team_names", {})
    real_names_raw = data.get("real_names", {})
    aliases_raw = data.get("aliases", {})
    reference = data.get("reference", {})
    polling = data.get("polling", {})

    flex_raw = roster_raw.pop("flex_positions", ["RB", "WR", "TE"])
    try:
        flex = tuple(Position.parse(p) for p in flex_raw)
    except ValueError as exc:
        raise ConfigError(f"[roster].flex_positions: {exc}") from exc

    roster: dict[RosterSlot, int] = {}
    for slot_name, count in roster_raw.items():
        try:
            slot = RosterSlot(str(slot_name).strip().upper())
        except ValueError as exc:
            raise ConfigError(
                f"[roster] has unknown slot {slot_name!r}. Valid slots: "
                + ", ".join(s.value for s in RosterSlot)
            ) from exc
        if not isinstance(count, int) or count < 0:
            raise ConfigError(f"[roster].{slot_name} must be a non-negative integer")
        if count:
            roster[slot] = count

    if not roster:
        raise ConfigError("[roster] is empty — the tool cannot compute roster needs")

    managers: dict[int, str] = {}
    for raw_id, nickname in managers_raw.items():
        # TOML keys are strings; team ids are ints everywhere else.
        try:
            managers[int(raw_id)] = str(nickname).strip()
        except ValueError as exc:
            raise ConfigError(
                f"[managers] keys must be ESPN team ids, got {raw_id!r}. "
                'Example: 3 = "dave"'
            ) from exc

    owners: dict[int, str] = {}
    for raw_id, swid in owners_raw.items():
        try:
            owners[int(raw_id)] = str(swid).strip()
        except ValueError as exc:
            raise ConfigError(
                f"[owners] keys must be ESPN team ids, got {raw_id!r}"
            ) from exc

    # Folded on the way in, so `{ABC}` in the file and `ABC` from a cookie are
    # the same key rather than two entries that shadow each other after a sort.
    from ffa.config.identity import fold_owner_id

    aliases: dict[str, str] = {}
    for raw_secondary, raw_primary in aliases_raw.items():
        secondary = fold_owner_id(str(raw_secondary))
        primary = fold_owner_id(str(raw_primary))
        if not secondary or not primary:
            raise ConfigError(
                f"[aliases] entries must both be ESPN member SWIDs, got "
                f"{raw_secondary!r} = {raw_primary!r}"
            )
        aliases[secondary] = primary

    config = LeagueConfig(
        league_id=int(_require(league, "league", "league_id")),
        year=int(_require(league, "league", "year")),
        private=bool(league.get("private", True)),
        name=str(league.get("name", "")),
        draft_type=str(draft.get("type", "AUCTION")).upper(),
        budget=int(draft.get("budget", 200)),
        nomination_order=_nomination_order(draft.get("nomination_order", ())),
        team_count=int(_require(teams, "teams", "count")),
        my_team_id=int(teams.get("my_team_id", 0)),
        team_ids=tuple(int(i) for i in teams.get("ids", ())),
        roster=roster,
        flex_positions=flex,
        scoring_type=str(scoring.get("type", "PPR")).upper(),
        managers=managers,
        owners=owners,
        team_names={int(k): str(v) for k, v in team_names_raw.items()},
        real_names={int(k): str(v) for k, v in real_names_raw.items()},
        aliases=aliases,
        reference_path=str(reference.get("path", "")),
        baseline_teams=int(reference.get("baseline_teams", 12)),
        baseline_budget=int(reference.get("baseline_budget", 200)),
        poll_interval_seconds=float(polling.get("interval_seconds", 3.0)),
        poll_failure_threshold=int(polling.get("failure_threshold", 4)),
    )
    config.validate()
    return config


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> LeagueConfig:
    if not path.is_file():
        raise ConfigError(
            f"no config at {path}.\n"
            "Run `ffa config init --league-id <id> --year <year>` to generate it "
            f"from ESPN, or copy {path.parent / 'league.example.toml'} and edit it."
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return parse_config(data)


def config_to_dict(config: LeagueConfig) -> dict[str, Any]:
    """Inverse of parse_config, for `ffa config init` to write out."""
    roster: dict[str, Any] = {
        slot.value: count for slot, count in sorted(config.roster.items(), key=lambda kv: kv[0].value)
    }
    roster["flex_positions"] = [p.value for p in config.flex_positions]
    return {
        "league": {
            "league_id": config.league_id,
            "year": config.year,
            "private": config.private,
            "name": config.name,
        },
        "draft": {
            "type": config.draft_type,
            "budget": config.budget,
            "nomination_order": list(config.nomination_order),
        },
        "teams": {
            "count": config.team_count,
            "my_team_id": config.my_team_id,
            "ids": list(config.team_ids),
        },
        "roster": roster,
        "scoring": {"type": config.scoring_type},
        "managers": {str(k): v for k, v in sorted(config.managers.items())},
        "owners": {str(k): v for k, v in sorted(config.owners.items())},
        "team_names": {str(k): v for k, v in sorted(config.team_names.items())},
        "real_names": {str(k): v for k, v in sorted(config.real_names.items())},
        "aliases": {str(k): v for k, v in sorted(config.aliases.items())},
        "reference": {
            "path": config.reference_path,
            "baseline_teams": config.baseline_teams,
            "baseline_budget": config.baseline_budget,
        },
        "polling": {
            "interval_seconds": config.poll_interval_seconds,
            "failure_threshold": config.poll_failure_threshold,
        },
    }


def write_config(config: LeagueConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tomli_w.dumps(config_to_dict(config)).encode("utf-8"))
