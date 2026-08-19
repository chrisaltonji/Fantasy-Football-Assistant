"""A read-only client for ESPN's undocumented Fantasy Football API.

    from espn_fantasy import EspnClient, load_credentials, fetch_settings

    client = EspnClient(123456, 2026, credentials=load_credentials())
    settings, warnings = fetch_settings(client)

Two independent ways in, because ESPN gives you different things through each:

- **REST** (`client`, `league`, `players`, `draft`) — league settings, teams,
  the player universe with ESPN's own auction values, and completed drafts.
- **The draft room DOM** (`draftroom`, `watch`) — the only channel that is
  live *during* an auction. See `docs/ESPN_DATA_ACCESS.md` for why.

Nothing in this package writes to ESPN.
"""

from espn_fantasy.client import (
    HOSTS,
    USER_AGENT,
    EspnClient,
    Response,
    build_url,
    normalize_payload,
    scrub,
)
from espn_fantasy.credentials import (
    EspnCredentials,
    fold_owner_id,
    load_credentials,
    load_dotenv,
)
from espn_fantasy.draft import (
    DraftBoard,
    DraftPick,
    board_from_payload,
    fetch_board,
    new_picks,
)
from espn_fantasy.enums import (
    LINEUP_SLOT_IDS,
    POSITION_IDS,
    PRO_TEAM_IDS,
    Position,
    RosterSlot,
)
from espn_fantasy.errors import EspnApiError
from espn_fantasy.league import (
    LeagueSettings,
    Member,
    TeamDirectory,
    fetch_settings,
    my_team_id,
    roster_from_lineup_counts,
    settings_from_payloads,
    teams_from_payload,
)
from espn_fantasy.names import normalize_player_key, normalize_team_name
from espn_fantasy.players import (
    PlayerValue,
    fetch_players,
    players_from_payload,
    write_csv,
)

__version__ = "0.1.0"

__all__ = [
    "EspnApiError",
    "EspnClient",
    "EspnCredentials",
    "DraftBoard",
    "DraftPick",
    "HOSTS",
    "LINEUP_SLOT_IDS",
    "LeagueSettings",
    "Member",
    "POSITION_IDS",
    "PRO_TEAM_IDS",
    "PlayerValue",
    "Position",
    "Response",
    "RosterSlot",
    "TeamDirectory",
    "USER_AGENT",
    "__version__",
    "board_from_payload",
    "build_url",
    "fetch_board",
    "fetch_players",
    "fetch_settings",
    "fold_owner_id",
    "load_credentials",
    "load_dotenv",
    "my_team_id",
    "new_picks",
    "normalize_payload",
    "normalize_player_key",
    "normalize_team_name",
    "players_from_payload",
    "roster_from_lineup_counts",
    "scrub",
    "settings_from_payloads",
    "teams_from_payload",
    "write_csv",
]


def __getattr__(name: str):
    """Expose the draft-room names lazily.

    `draftroom` and `watch` are importable without playwright installed — the
    import only fails at `connect()` — but keeping them out of the eager
    import list means the REST half of this package has no optional-dependency
    surface at all.
    """
    if name in ("DraftRoomReader", "DraftRoomError", "RoomSnapshot", "parse_snapshot"):
        from espn_fantasy import draftroom

        return getattr(draftroom, name)
    if name in ("DraftRoomWatcher", "TeamResolver", "Sale", "Nominated"):
        from espn_fantasy import watch

        return getattr(watch, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
