#!/usr/bin/env python3
"""Dump and analyse raw ESPN Fantasy API payloads.

This is a throwaway diagnostic, NOT part of the application — nothing under
``src/ffa`` imports it. Its whole job is to answer the questions the build plan
can't answer from documentation:

  1. Does an auction draft's pick JSON actually carry the winning bid amount?
  2. What does a team object expose for remaining budget and roster?
  3. Does cookie auth work end-to-end against a private league?

Run it while an auction (ideally an ESPN "Practice Draft" in salary-cap mode)
is in progress or just finished. The payloads it writes become the offline
fixtures the real ESPN adapter is built and tested against.

    python tools/espn_probe.py --league-id 123456 --year 2026
    python tools/espn_probe.py --league-id 123456 --year 2026 --watch 5

Credentials come from .env / the environment, never the command line, so they
can't end up in your shell history.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Allow running straight from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import requests  # noqa: E402

from ffa.config.loader import load_credentials  # noqa: E402
from ffa.config.schema import ConfigError, EspnCredentials  # noqa: E402

# ESPN moved reads to this host; the legacy host still works and is kept as a
# fallback because undocumented infrastructure changes without notice.
HOSTS = (
    "https://lm-api-reads.fantasy.espn.com",
    "https://fantasy.espn.com",
)

DEFAULT_VIEWS = ("mDraftDetail", "mSettings", "mTeam", "mRoster")

# A browser-ish UA. ESPN sometimes 403s obviously-scripted clients.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class ProbeError(Exception):
    """A probe failure worth printing plainly and exiting on."""


def build_session(creds: EspnCredentials | None) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    if creds:
        session.cookies.update(creds.as_cookies())
    return session


def fetch_view(
    session: requests.Session, league_id: int, year: int, view: str, *, timeout: float = 15.0
) -> tuple[str, Any]:
    """Fetch one view, trying each host. Returns (url_used, parsed_json)."""
    last_error: str | None = None
    for host in HOSTS:
        url = (
            f"{host}/apis/v3/games/ffl/seasons/{year}"
            f"/segments/0/leagues/{league_id}?view={view}"
        )
        try:
            resp = session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            last_error = f"{host}: {type(exc).__name__}: {exc}"
            continue

        if resp.status_code == 401:
            raise ProbeError(
                "ESPN returned 401 Unauthorized.\n"
                "Your espn_s2 / SWID cookies are missing, expired, or belong to an "
                "account that can't see this league. Re-copy them from your browser "
                "(see README.md) and try again."
            )
        if resp.status_code == 404:
            raise ProbeError(
                f"ESPN returned 404 for league {league_id} in {year}.\n"
                "Check the league id and season year. A private league with no "
                "cookies also surfaces as 404 rather than 401."
            )
        if resp.status_code != 200:
            last_error = f"{host}: HTTP {resp.status_code}: {resp.text[:200]}"
            continue

        try:
            return url, resp.json()
        except ValueError:
            last_error = f"{host}: response was not JSON: {resp.text[:200]}"

    raise ProbeError(f"all hosts failed for view {view}. Last error: {last_error}")


def scrub(payload: Any, creds: EspnCredentials | None) -> Any:
    """Remove anything credential-shaped before the payload touches disk.

    ESPN's responses shouldn't echo cookies, but these files are meant to be
    committed as test fixtures, so this is a belt-and-braces pass.
    """
    text = json.dumps(payload)
    if creds:
        for secret in (creds.espn_s2, creds.swid, creds.swid.strip("{}")):
            if secret:
                text = text.replace(secret, "<redacted>")
    return json.loads(text)


# --------------------------------------------------------------------------
# Analysis — the actual point of the probe
# --------------------------------------------------------------------------


def _picks(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    detail = payload.get("draftDetail") or {}
    picks = detail.get("picks") or []
    return [p for p in picks if isinstance(p, dict)]


def _key_union(records: Iterable[dict[str, Any]]) -> list[str]:
    keys: set[str] = set()
    for record in records:
        keys.update(record.keys())
    return sorted(keys)


def analyse_draft(payload: Any) -> list[str]:
    """Answer question 1: is the winning bid amount in the pick JSON?"""
    out: list[str] = ["## Q1 — Does the pick JSON carry auction bid amounts?"]
    detail = payload.get("draftDetail") if isinstance(payload, dict) else None

    if not isinstance(detail, dict):
        out.append("  draftDetail is absent from this response — nothing to inspect.")
        return out

    out.append(f"  draftDetail.drafted   = {detail.get('drafted')}")
    out.append(f"  draftDetail.inProgress= {detail.get('inProgress')}")

    picks = _picks(payload)
    out.append(f"  pick count            = {len(picks)}")
    if not picks:
        out.append(
            "  No picks yet. Nominate and sell at least one player, then re-run."
        )
        return out

    out.append(f"  keys present on picks = {', '.join(_key_union(picks))}")

    # espn-api's BasePick already models bid_amount / nominatingTeamId, so the
    # question is whether ESPN populates them, not whether they're modelled.
    for field in ("bidAmount", "nominatingTeamId", "autoDraftTypeId", "keeper"):
        present = [p for p in picks if field in p]
        if not present:
            out.append(f"  {field:18s}: ABSENT from every pick")
            continue
        values = [p.get(field) for p in present]
        nonzero = [v for v in values if v not in (None, 0, False)]
        out.append(
            f"  {field:18s}: present on {len(present)}/{len(picks)} picks, "
            f"{len(nonzero)} non-zero, sample={values[:8]}"
        )

    # ESPN pre-creates the whole pick skeleton before anyone drafts, so
    # `len(picks)` says nothing about progress. A pick is *filled* when its
    # playerId stops being the -1 sentinel. Without this distinction an
    # untouched skeleton looks identical to "ESPN never populates prices" —
    # opposite conclusions from the same zeros.
    filled = [p for p in picks if p.get("playerId", -1) not in (-1, None)]
    bids = [p.get("bidAmount") for p in filled if p.get("bidAmount")]

    out.append(f"  filled picks          = {len(filled)}/{len(picks)} (playerId != -1)")
    out.append("")

    if not filled:
        out.append(
            "  VERDICT: INCONCLUSIVE — the pick skeleton exists but nothing has "
            "sold yet. This is the expected pre-draft baseline, not evidence "
            "either way. Sell 2-3 players and capture again."
        )
    elif bids:
        out.append(
            f"  VERDICT: bidAmount IS populated ({len(bids)}/{len(filled)} filled "
            f"picks, ${min(bids)}-${max(bids)}, total ${sum(bids)}). "
            "The live-price path is viable."
        )
    else:
        out.append(
            f"  VERDICT: {len(filled)} pick(s) are filled but every bidAmount is "
            "ZERO. ESPN gives us who won, not for how much. Prices need manual "
            "entry — already a first-class path, since price=None is a supported "
            "state and merge fills it in later."
        )

    out.append("")
    out.append("  First pick verbatim:")
    out.append("    " + json.dumps(picks[0], indent=2).replace("\n", "\n    "))
    return out


def analyse_settings(payload: Any) -> list[str]:
    """Confirm the league really is an auction and find the budget."""
    out: list[str] = ["## Draft settings (needed for `ffa config init`)"]
    settings = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings, dict):
        out.append("  settings absent — request the mSettings view.")
        return out

    draft = settings.get("draftSettings") or {}
    roster = settings.get("rosterSettings") or {}

    out.append(f"  league name           = {settings.get('name')}")
    out.append(f"  draftSettings.type    = {draft.get('type')}")
    out.append(f"  draftSettings.auctionBudget = {draft.get('auctionBudget')}")
    out.append(f"  draftSettings.keeperCount   = {draft.get('keeperCount')}")
    out.append(f"  size (team count)     = {settings.get('size')}")
    out.append(f"  scoringSettings.scoringType = "
               f"{(settings.get('scoringSettings') or {}).get('scoringType')}")
    out.append(f"  lineupSlotCounts      = {roster.get('lineupSlotCounts')}")
    out.append("")
    out.append(
        "  NOTE: espn-api's BaseSettings does NOT expose auctionBudget or draft "
        "type, so `ffa config init` must read these from the raw view."
    )
    return out


def analyse_teams(payload: Any) -> list[str]:
    """Answer question 2: what does a team expose for budget and roster?"""
    out: list[str] = ["## Q2 — What does a team object expose for budget/roster?"]
    teams = payload.get("teams") if isinstance(payload, dict) else None
    if not isinstance(teams, list) or not teams:
        out.append("  no teams array in this response.")
        return out

    out.append(f"  team count            = {len(teams)}")
    out.append(f"  keys present on teams = {', '.join(_key_union(teams))}")

    money_keys = [
        k for k in _key_union(teams)
        if any(tok in k.lower() for tok in ("budget", "bid", "cap", "salary", "spent", "acquisition"))
    ]
    out.append(f"  budget-ish keys       = {money_keys or 'NONE FOUND'}")

    first = teams[0]
    entries = ((first.get("roster") or {}).get("entries") or []) if isinstance(first, dict) else []
    out.append(f"  team[0].roster.entries= {len(entries)}")
    if entries:
        out.append(f"  roster entry keys     = {', '.join(_key_union(entries))}")
        bid_keys = [k for k in _key_union(entries) if "bid" in k.lower() or "value" in k.lower()]
        out.append(f"  entry value/bid keys  = {bid_keys or 'NONE FOUND'}")
        out.append("")
        out.append("  First roster entry verbatim (truncated):")
        sample = json.dumps(entries[0], indent=2)
        if len(sample) > 2000:
            sample = sample[:2000] + "\n... (truncated)"
        out.append("    " + sample.replace("\n", "\n    "))

    out.append("")
    out.append(
        "  If no per-team remaining-budget field exists, remaining budget is "
        "derived: budget - sum(bidAmount of that team's picks). The state layer "
        "computes it as a projection either way, so this is not a blocker."
    )
    return out


ANALYSERS = {
    "mDraftDetail": analyse_draft,
    "mSettings": analyse_settings,
    "mTeam": analyse_teams,
    "mRoster": analyse_teams,
}


def run_once(
    session: requests.Session,
    creds: EspnCredentials | None,
    league_id: int,
    year: int,
    views: tuple[str, ...],
    out_dir: Path,
    stamp: str,
) -> list[str]:
    report: list[str] = []
    for view in views:
        url, payload = fetch_view(session, league_id, year, view)
        clean = scrub(payload, creds)

        target = out_dir / f"{view}_{stamp}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(clean, indent=2), encoding="utf-8")

        report.append("")
        report.append("=" * 72)
        report.append(f"VIEW {view}  ->  {target}")
        report.append(f"  url: {url}")
        report.append("=" * 72)
        analyser = ANALYSERS.get(view)
        if analyser:
            report.extend(analyser(clean))
        else:
            report.append(f"  (no analyser for {view}; raw payload saved)")
    return report


def analyse_file(path: Path) -> list[str]:
    """Run the analysers over a payload someone already captured.

    The whole point: any machine that can't reach ESPN — this development
    sandbox included — can still get the full verdict from a payload pasted or
    saved out of a browser. The view is detected from the payload's shape
    rather than the filename, so a file called `paste.json` works fine.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"cannot read {path}: {exc}"]
    except ValueError as exc:
        return [f"{path} is not valid JSON: {exc}"]

    report = ["=" * 72, f"FILE {path}", "=" * 72]

    detail = payload.get("draftDetail") if isinstance(payload, dict) else None
    if isinstance(detail, dict) and "picks" in detail:
        report.extend(analyse_draft(payload))
    if isinstance(payload, dict) and payload.get("settings"):
        report.append("")
        report.extend(analyse_settings(payload))
    if isinstance(payload, dict) and payload.get("teams"):
        report.append("")
        report.extend(analyse_teams(payload))

    if len(report) == 3:
        report.append(
            "  Nothing recognizable here — expected a draftDetail, settings, or "
            "teams key. Is this the right view?"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--analyse", "--analyze", type=Path, nargs="+", dest="analyse", default=None,
        metavar="FILE",
        help="Analyse already-captured JSON instead of fetching. No network, no cookies.",
    )
    parser.add_argument("--league-id", type=int, default=None, help="ESPN league id (or set ESPN_LEAGUE_ID)")
    parser.add_argument("--year", type=int, default=None, help="Season year (or set ESPN_SEASON_YEAR)")
    parser.add_argument("--view", action="append", dest="views", help="View to fetch; repeatable. Default: all four.")
    parser.add_argument("--out", type=Path, default=Path("probe_out"), help="Directory for captured payloads")
    parser.add_argument("--public", action="store_true", help="Skip cookie auth (public leagues only)")
    parser.add_argument(
        "--watch",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Re-probe on an interval. Use during a live draft to catch picks as they land.",
    )
    args = parser.parse_args()

    # Offline mode runs before anything touches credentials or the network.
    if args.analyse:
        for path in args.analyse:
            print("\n".join(analyse_file(path)))
            print()
        return 0

    try:
        creds = None if args.public else load_credentials()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    from ffa.config.loader import load_dotenv

    env = load_dotenv()
    league_id = args.league_id or int(env.get("ESPN_LEAGUE_ID") or 0)
    year = args.year or int(env.get("ESPN_SEASON_YEAR") or 0)

    if not league_id or not year:
        print(
            "error: need --league-id and --year (or ESPN_LEAGUE_ID / "
            "ESPN_SEASON_YEAR in .env)",
            file=sys.stderr,
        )
        return 2

    views = tuple(args.views) if args.views else DEFAULT_VIEWS
    session = build_session(creds)

    print(f"probing league {league_id} season {year}")
    print(f"auth: {'cookies loaded' if creds else 'NONE (public mode)'}")
    print(f"views: {', '.join(views)}")

    try:
        while True:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            report = run_once(session, creds, league_id, year, views, args.out, stamp)
            print("\n".join(report))

            summary = args.out / f"report_{stamp}.txt"
            summary.write_text("\n".join(report), encoding="utf-8")
            print(f"\nreport written to {summary}")

            if args.watch is None:
                return 0
            print(f"\n--- sleeping {args.watch}s (Ctrl-C to stop) ---\n")
            time.sleep(args.watch)
    except ProbeError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
