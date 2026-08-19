"""ESPN session cookies, and where they come from.

Credentials are read from the environment or a `.env` file, never from a
command line argument, so they can't end up in shell history or a process
listing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from espn_fantasy.errors import EspnApiError

DEFAULT_ENV_PATH = Path(".env")


@dataclass(frozen=True)
class EspnCredentials:
    """The two cookies ESPN authenticates a private league with."""

    espn_s2: str
    swid: str

    def as_cookies(self) -> dict[str, str]:
        # ESPN expects SWID wrapped in braces. Users copying from devtools
        # sometimes strip them, so put them back rather than 401 mysteriously.
        swid = self.swid.strip()
        if swid and not swid.startswith("{"):
            swid = "{" + swid.strip("{}") + "}"
        return {"espn_s2": self.espn_s2.strip(), "SWID": swid}

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Never let credentials reach a log line or a traceback.
        return "EspnCredentials(espn_s2='<redacted>', swid='<redacted>')"


def load_dotenv(path: Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Minimal `.env` reader.

    Deliberately not python-dotenv: this needs `KEY=value`, comments, and
    quote stripping, and nothing else — which is not worth a dependency that
    every consumer then has to install. A missing file is not an error; plenty
    of setups pass real environment variables instead.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise EspnApiError(f"{path}:{lineno}: expected KEY=value, got {raw!r}")
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

    Real environment variables win over the file, so CI and one-off overrides
    work without editing anything. Public leagues don't need cookies at all,
    so a missing pair is only an error when the caller says it matters.
    """
    file_values = load_dotenv(env_path)

    def get(key: str) -> str:
        return (os.environ.get(key) or file_values.get(key) or "").strip()

    s2, swid = get("ESPN_S2"), get("ESPN_SWID")
    if not s2 or not swid:
        if required:
            missing = [k for k, v in (("ESPN_S2", s2), ("ESPN_SWID", swid)) if not v]
            raise EspnApiError(
                f"a private league needs {' and '.join(missing)}, but they are not "
                "set.\nCopy .env.example to .env and fill them in — see README.md "
                "for how to pull them out of your browser."
            )
        return None
    return EspnCredentials(espn_s2=s2, swid=swid)


def fold_owner_id(raw: str | None) -> str:
    """Fold a SWID / `primaryOwner` id so two spellings of it compare equal.

    ESPN writes owner ids as `{6D4D...}` in some payloads and `6D4D...` in
    others, and a SWID copied out of a browser's cookie jar can arrive in
    either case. The braces and the case are noise; folding them here is what
    keeps a hand-copied SWID from silently failing to match the identical id
    in `mTeam`.
    """
    return (raw or "").strip().strip("{}").upper()
