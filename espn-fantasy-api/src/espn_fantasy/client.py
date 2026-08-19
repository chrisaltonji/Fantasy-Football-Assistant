"""The HTTP layer: one place that knows how to ask ESPN for a view.

ESPN's fantasy API is undocumented and has no versioned contract, so most of
the value here is not the requests — it is the accumulated knowledge of how it
misbehaves:

- **Two hosts.** Reads moved to `lm-api-reads.fantasy.espn.com`; the legacy
  `fantasy.espn.com` still answers and is kept as a fallback, because
  undocumented infrastructure moves without notice.
- **A browser-ish User-Agent is not optional.** ESPN 403s obviously-scripted
  clients.
- **404 does not mean "no such league".** A private league with no cookies
  surfaces as 404, not 401, so the two have to be explained together.
- **Two URL shapes.** The seasonal path serves the current year; prior seasons
  come from `leagueHistory`, which returns a JSON *array* rather than an
  object. `normalize_payload` collapses that so nothing downstream has to care.
- **The player universe needs a header, not a query string.** Without
  `x-fantasy-filter` ESPN returns a handful of players and no amount of query
  string coaxing changes it.

Every method comes in two flavours: `get_*`, which raises `EspnApiError` with
an actionable message, and `try_*`, which never raises and hands back a
`Response` record. Sweeps need the second one — a 404 on one candidate URL is
a *result*, and must not abort the remaining candidates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from espn_fantasy.credentials import EspnCredentials
from espn_fantasy.errors import EspnApiError

HOSTS = (
    "https://lm-api-reads.fantasy.espn.com",
    "https://fantasy.espn.com",
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# The views worth knowing about. Others exist; these are the ones with a
# documented meaning in this package.
VIEW_SETTINGS = "mSettings"
VIEW_TEAM = "mTeam"
VIEW_ROSTER = "mRoster"
VIEW_DRAFT = "mDraftDetail"
VIEW_PLAYERS = "kona_player_info"

DEFAULT_PLAYER_LIMIT = 900


def build_url(
    host: str,
    league_id: int,
    year: int,
    view: str,
    *,
    segment: str = "0",
    history: bool = False,
) -> str:
    """Build one ESPN read URL.

    The seasonal shape is what every current-season call uses. The history
    shape is how ESPN serves prior seasons — and it answers with an array.
    """
    if history:
        return (
            f"{host}/apis/v3/games/ffl/leagueHistory/{league_id}"
            f"?seasonId={year}&view={view}"
        )
    return (
        f"{host}/apis/v3/games/ffl/seasons/{year}"
        f"/segments/{segment}/leagues/{league_id}?view={view}"
    )


def normalize_payload(payload: Any, year: int | None = None) -> Any:
    """Reduce a `leagueHistory` array to the single season object.

    Everything downstream assumes a top-level dict. Doing this in one place is
    what keeps the live path and the offline `--analyse` path from diverging
    on the one input shape that differs between them.
    """
    if not isinstance(payload, list):
        return payload
    if not payload:
        return {}
    if year is not None:
        for entry in payload:
            if isinstance(entry, dict) and entry.get("seasonId") == year:
                return entry
    return payload[-1] if isinstance(payload[-1], dict) else {}


def scrub(payload: Any, creds: EspnCredentials | None) -> Any:
    """Remove anything credential-shaped before a payload touches disk.

    ESPN's responses shouldn't echo cookies back, but captures get committed
    as fixtures, so this is belt and braces. It removes *your* credentials
    only — see `tools/anonymize_capture.py` for the other managers' ids.
    """
    text = json.dumps(payload)
    if creds:
        for secret in (creds.espn_s2, creds.swid, creds.swid.strip("{}")):
            if secret:
                text = text.replace(secret, "<redacted>")
    return json.loads(text)


@dataclass(frozen=True)
class Response:
    """One attempt at one URL. `status == -1` means the request never landed."""

    url: str
    status: int
    payload: Any = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.payload is not None


class EspnClient:
    """A read-only client for one league in one season.

    Nothing here writes to ESPN. There is no mutation surface in this package
    at all, which is deliberate: the whole point is to observe a league you
    are already in, without the risk of touching it.
    """

    def __init__(
        self,
        league_id: int,
        year: int,
        *,
        credentials: EspnCredentials | None = None,
        timeout: float = 20.0,
        hosts: tuple[str, ...] = HOSTS,
        user_agent: str = USER_AGENT,
        session: Any = None,
    ) -> None:
        self.league_id = int(league_id)
        self.year = int(year)
        self.credentials = credentials
        self.timeout = timeout
        self.hosts = tuple(hosts)

        if session is None:
            import requests

            session = requests.Session()
        self.session = session
        self.session.headers.update(
            {"User-Agent": user_agent, "Accept": "application/json"}
        )
        if credentials:
            self.session.cookies.update(credentials.as_cookies())

    # --- the raw layer ------------------------------------------------------

    def try_url(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Response:
        """Fetch one literal URL. Never raises."""
        import requests

        try:
            resp = self.session.get(
                url, headers=dict(headers or {}), timeout=timeout or self.timeout
            )
        except requests.RequestException as exc:
            return Response(url=url, status=-1, error=f"{type(exc).__name__}: {exc}")

        if resp.status_code != 200:
            return Response(url=url, status=resp.status_code, error=resp.text[:200])
        try:
            return Response(url=url, status=200, payload=resp.json())
        except ValueError:
            return Response(
                url=url,
                status=200,
                error=f"response was not JSON: {resp.text[:200]}",
            )

    def try_view(
        self,
        view: str,
        *,
        segment: str = "0",
        history: bool = False,
        league_id: int | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Response:
        """Fetch one view, trying each host. Never raises.

        Returns the first success, or the last failure — which carries the
        status the caller needs in order to say something useful.
        """
        last = Response(url="", status=-1, error="no hosts tried")
        for host in self.hosts:
            url = build_url(
                host,
                league_id if league_id is not None else self.league_id,
                self.year,
                view,
                segment=segment,
                history=history,
            )
            result = self.try_url(url, headers=headers, timeout=timeout)
            if result.ok:
                return result
            # 401 and 404 are answers about *this league*, not about this host.
            # Retrying the second host repeats the same rejection and buries
            # the real status behind a connection error from the fallback.
            if result.status in (401, 404):
                return result
            last = result
        return last

    def get_view(
        self,
        view: str,
        *,
        segment: str = "0",
        history: bool = False,
        league_id: int | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Fetch one view, or raise `EspnApiError` with an actionable message."""
        result = self.try_view(
            view,
            segment=segment,
            history=history,
            league_id=league_id,
            headers=headers,
            timeout=timeout,
        )
        if result.ok:
            return normalize_payload(result.payload, self.year)
        raise self._explain(result, view, league_id or self.league_id)

    def _explain(self, result: Response, view: str, league_id: int) -> EspnApiError:
        if result.status == 401:
            return EspnApiError(
                "ESPN returned 401 Unauthorized.\n"
                "Your espn_s2 / SWID cookies are missing, expired, or belong to "
                "an account that cannot see this league. Re-copy them from your "
                "browser — see README.md."
            )
        if result.status == 404:
            return EspnApiError(
                f"ESPN returned 404 for league {league_id} in {self.year}.\n"
                "Check the league id and the season. Note that a private league "
                "fetched with no cookies also surfaces as 404 rather than 401, "
                "so this may be an auth problem wearing a 404."
            )
        detail = result.error or f"HTTP {result.status}"
        return EspnApiError(f"could not fetch {view} for league {league_id}: {detail}")

    # --- the named views ----------------------------------------------------

    def settings(self, **kwargs: Any) -> dict[str, Any]:
        """League settings: draft type, auction budget, roster slots, scoring."""
        return self.get_view(VIEW_SETTINGS, **kwargs)

    def teams(self, **kwargs: Any) -> dict[str, Any]:
        """Teams and members. The only place owner identity is published."""
        return self.get_view(VIEW_TEAM, **kwargs)

    def rosters(self, **kwargs: Any) -> dict[str, Any]:
        """Teams with their roster entries attached."""
        return self.get_view(VIEW_ROSTER, **kwargs)

    def draft_detail(self, **kwargs: Any) -> dict[str, Any]:
        """The draft board.

        Read `docs/ESPN_DATA_ACCESS.md` before building on this: it is **not
        written while a draft is running**, and the payload you get mid-draft
        is byte-identical to the one you get before it starts.
        """
        return self.get_view(VIEW_DRAFT, **kwargs)

    def players(
        self,
        *,
        limit: int = DEFAULT_PLAYER_LIMIT,
        timeout: float = 45.0,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """The player universe, most-owned first.

        Carries ESPN's own consensus auction value, ADP and season projection
        per player. The `x-fantasy-filter` header is what makes this return
        more than a handful of players.
        """
        payload = self.get_view(
            VIEW_PLAYERS,
            headers={
                "x-fantasy-filter": json.dumps(
                    {
                        "players": {
                            "limit": int(limit),
                            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
                        }
                    }
                )
            },
            timeout=timeout,
            **kwargs,
        )
        if not isinstance(payload, dict):
            return []
        return [p for p in (payload.get("players") or []) if isinstance(p, dict)]
