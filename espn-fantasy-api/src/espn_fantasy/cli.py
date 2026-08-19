"""`espn-fantasy` — the package's capabilities from a terminal.

    espn-fantasy settings
    espn-fantasy teams
    espn-fantasy players --csv values.csv
    espn-fantasy draft --year 2025
    espn-fantasy raw mSettings --out capture.json
    espn-fantasy watch

Credentials come from `.env` or the environment, never from an argument, so
they cannot end up in shell history or a process listing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from espn_fantasy.client import EspnClient, scrub
from espn_fantasy.credentials import DEFAULT_ENV_PATH, load_credentials, load_dotenv
from espn_fantasy.draft import fetch_board
from espn_fantasy.errors import EspnApiError
from espn_fantasy.league import fetch_settings
from espn_fantasy.players import fetch_players, write_csv


def _client(args: argparse.Namespace) -> EspnClient:
    env = load_dotenv(args.env)
    league_id = args.league_id or int(env.get("ESPN_LEAGUE_ID") or 0)
    year = args.year or int(env.get("ESPN_SEASON_YEAR") or 0)

    if not league_id or not year:
        raise EspnApiError(
            "need --league-id and --year (or ESPN_LEAGUE_ID / ESPN_SEASON_YEAR "
            "in .env).\nYour league id is in the URL of any league page."
        )
    creds = None if args.public else load_credentials(args.env)
    return EspnClient(league_id, year, credentials=creds)


def _warn(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"  ! {warning}", file=sys.stderr)


# --- commands ---------------------------------------------------------------


def cmd_settings(args: argparse.Namespace) -> int:
    client = _client(args)
    settings, warnings = fetch_settings(client)

    print(f"{settings.name or '(unnamed league)'}  —  league {settings.league_id}, {settings.year}")
    print(f"  draft type      : {settings.draft_type or '(not reported)'}")
    if settings.auction_budget is not None:
        print(f"  auction budget  : ${settings.auction_budget}")
    print(f"  scoring         : {settings.scoring_type or '(not reported)'}")
    print(f"  teams           : {settings.team_count}")
    if settings.keeper_count:
        print(f"  keepers         : {settings.keeper_count}")
    if settings.roster:
        slots = ", ".join(f"{slot.value} x{n}" for slot, n in settings.roster.items())
        print(f"  roster ({settings.roster_size} deep, {settings.starter_count} starting): {slots}")
    _warn(warnings)
    return 0


def cmd_teams(args: argparse.Namespace) -> int:
    client = _client(args)
    settings, warnings = fetch_settings(client)
    directory = settings.teams

    if not directory.team_ids:
        print("no teams in this payload.")
        return 0

    width = max(len(directory.team_names.get(t, "")) for t in directory.team_ids) or 4
    print(f"{'id':>4}  {'team':<{width}}  manager")
    for team_id in directory.team_ids:
        print(
            f"{team_id:>4}  {directory.team_names.get(team_id, ''):<{width}}  "
            f"{directory.name_for(team_id)}"
        )

    # The gap is the point: ids are read, never generated.
    expected = set(range(min(directory.team_ids), max(directory.team_ids) + 1))
    missing = sorted(expected - set(directory.team_ids))
    if missing:
        print(
            f"\nnote: team id(s) {missing} do not exist in this league. Ids are "
            "not contiguous — never generate them with range()."
        )
    _warn(warnings)
    return 0


def cmd_players(args: argparse.Namespace) -> int:
    client = _client(args)
    players, warnings = fetch_players(client, limit=args.limit)

    if args.csv:
        count = write_csv(players, args.csv)
        print(f"wrote {count} player(s) to {args.csv}")
    else:
        for player in players[: args.top]:
            value = f"${player.auction_value:g}" if player.auction_value is not None else "-"
            adp = f"{player.adp:g}" if player.adp is not None else "-"
            print(
                f"  {value:>7}  {player.position:<4} {player.name:<26} "
                f"{player.nfl_team:<4} adp {adp}"
            )
        if len(players) > args.top:
            print(f"  ... and {len(players) - args.top} more (--top N, or --csv PATH)")

    total = sum(p.auction_value or 0 for p in players)
    print(f"\n{len(players)} priced player(s), ${total:,.0f} of value in total")
    _warn(warnings)
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    client = _client(args)
    board = fetch_board(client, history=args.history)

    print(board.describe())
    if board.pick_order:
        print(f"  nomination order: {list(board.pick_order)}")
    if board.has_prices:
        spend = board.spend_by_team()
        print(f"  spent by team   : {spend}")
        print(f"  total           : ${sum(spend.values()):,}")
        auto = [p for p in board.filled if p.autodrafted]
        if auto:
            print(f"  autodrafted     : {len(auto)} pick(s) — these carry no memberId")
    return 0


def cmd_raw(args: argparse.Namespace) -> int:
    client = _client(args)
    payload = client.get_view(args.view, segment=args.segment, history=args.history)
    clean = scrub(payload, client.credentials)
    text = json.dumps(clean, indent=2)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.view} to {args.out}")
        print(
            "note: this scrubs YOUR credentials only. Other members' ids are "
            "still in there — run tools/anonymize_capture.py before sharing it."
        )
    else:
        print(text)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Tail a live draft room. The only channel that works mid-auction."""
    from espn_fantasy.draftroom import DraftRoomError, DraftRoomReader
    from espn_fantasy.watch import DraftRoomWatcher, Health, Nominated, Sale, TeamResolver

    resolver = TeamResolver()
    if not args.no_league:
        try:
            settings, _ = fetch_settings(_client(args))
            resolver = TeamResolver(settings.teams.team_names, settings.teams.team_ids)
        except EspnApiError as exc:
            print(f"note: reading the board without league ids ({exc})", file=sys.stderr)

    reader = DraftRoomReader(port=args.port, tab_match=args.tab)
    watcher = DraftRoomWatcher(reader, interval=args.interval, resolver=resolver)

    try:
        reader.connect()
    except DraftRoomError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Pre-flight. A rename found now costs one command; found at pick 40 it
    # costs forty misattributed picks.
    snapshot = reader.snapshot()
    matched, unmatched = resolver.audit(snapshot)
    print(f"attached: {len(snapshot.teams)} team(s), {snapshot.filled} pick(s) on the board")
    if matched:
        print(f"  matched {len(matched)}/{len(snapshot.teams)} teams to league ids")
    if unmatched:
        print(f"  ! unmatched: {', '.join(unmatched)} — their picks go by column order")

    try:
        for event in watcher.events():
            if isinstance(event, Sale):
                price = f"${event.price}" if event.price is not None else "$?"
                print(
                    f"  SOLD  {event.player:<26} {price:>5}  -> team {event.team_id} "
                    f"({event.team_name})"
                )
            elif isinstance(event, Nominated):
                bid = f" at ${event.current_bid}" if event.current_bid else ""
                print(f"  UP    {event.player}{bid}")
            if watcher.status.health is not Health.OK:
                print(f"  ! {watcher.status.detail}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        reader.close()

    if watcher.status.health is Health.STOPPED:
        print(f"error: {watcher.status.detail}", file=sys.stderr)
        return 1
    return 0


# --- wiring -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="espn-fantasy",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--league-id", type=int, default=None,
                        help="ESPN league id (or set ESPN_LEAGUE_ID)")
    parser.add_argument("--year", type=int, default=None,
                        help="season year (or set ESPN_SEASON_YEAR)")
    parser.add_argument("--public", action="store_true",
                        help="skip cookie auth (public leagues only)")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV_PATH,
                        help="path to the .env holding ESPN_S2 / ESPN_SWID")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("settings", help="league settings: draft type, budget, roster").set_defaults(fn=cmd_settings)
    sub.add_parser("teams", help="team ids, names and managers").set_defaults(fn=cmd_teams)

    players = sub.add_parser("players", help="ESPN's auction values, ADP and projections")
    players.add_argument("--csv", type=Path, default=None, help="write to CSV instead of printing")
    players.add_argument("--limit", type=int, default=None, help="how many players to request")
    players.add_argument("--top", type=int, default=40, help="how many to print (default 40)")
    players.set_defaults(fn=cmd_players)

    draft = sub.add_parser("draft", help="the draft board (NOT live — see docs)")
    draft.add_argument("--history", action="store_true",
                       help="use the leagueHistory path, for a prior season")
    draft.set_defaults(fn=cmd_draft)

    raw = sub.add_parser("raw", help="dump a view's raw JSON")
    raw.add_argument("view", help="e.g. mSettings, mTeam, mRoster, mDraftDetail")
    raw.add_argument("--out", type=Path, default=None, help="write to a file")
    raw.add_argument("--segment", default="0", help="path segment id (default 0)")
    raw.add_argument("--history", action="store_true", help="use the leagueHistory path")
    raw.set_defaults(fn=cmd_raw)

    watch = sub.add_parser("watch", help="tail a live draft room over CDP")
    watch.add_argument("--port", type=int, default=9222, help="Chrome remote-debugging port")
    watch.add_argument("--tab", default="", help="substring of the draft tab's URL")
    watch.add_argument("--interval", type=float, default=2.0, help="seconds between polls")
    watch.add_argument("--no-league", action="store_true",
                       help="don't fetch mTeam; report board column order instead of team ids")
    watch.set_defaults(fn=cmd_watch)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except EspnApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
