#!/usr/bin/env python3
"""Dump and analyse raw ESPN Fantasy API payloads.

A diagnostic, not part of the library — nothing under `src/espn_fantasy`
imports it. Its job is to answer the questions you cannot answer from
documentation, because there isn't any:

  1. Does an auction draft's pick JSON actually carry the winning bid amount?
  2. What does a team object expose for remaining budget and roster?
  3. Does cookie auth work end-to-end against this private league?

The payloads it writes are the offline fixtures the library is tested against.

    # The current season
    python tools/espn_probe.py --league-id 123456 --year 2026
    python tools/espn_probe.py --league-id 123456 --year 2026 --watch 5

Finding a real auction to read is its own problem, because a practice draft
does not write to `segments/0`. Four ways in, cheapest first:

    # 1. A prior season's completed auction — real bids, already on ESPN
    python tools/espn_probe.py --league-id 123456 --year 2025 --view mDraftDetail
    python tools/espn_probe.py --league-id 123456 --year 2025 --history

    # 2. Try several URL shapes at once; never aborts on a 404
    python tools/espn_probe.py --league-id 123456 --year 2026 --sweep

    # 3. Read what the draft room actually fetched — the only one that
    #    observes rather than guesses. Never commit the .har: it holds live
    #    cookies.
    python tools/espn_probe.py --har practice_draft.har

    # 4. Anything already saved out of a logged-in browser
    python tools/espn_probe.py --analyse saved.json

Credentials come from .env / the environment, never the command line, so they
cannot end up in your shell history.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Allow running straight from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from espn_fantasy.client import (  # noqa: E402
    HOSTS,
    EspnClient,
    normalize_payload,
    scrub,
)
from espn_fantasy.credentials import (  # noqa: E402
    EspnCredentials,
    load_credentials,
    load_dotenv,
)
from espn_fantasy.draft import UNFILLED, key_union  # noqa: E402
from espn_fantasy.errors import EspnApiError  # noqa: E402

DEFAULT_VIEWS = ("mDraftDetail", "mSettings", "mTeam", "mRoster")


# --------------------------------------------------------------------------
# Analysis — the actual point of the probe
# --------------------------------------------------------------------------


def _picks(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    detail = payload.get("draftDetail") or {}
    return [p for p in (detail.get("picks") or []) if isinstance(p, dict)]


def _filled(picks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Picks that actually sold. ESPN pre-creates the skeleton with playerId -1.

    Sweep and HAR modes share this with `analyse_draft`, so the answer paths
    can never disagree about what "filled" means.
    """
    return [p for p in picks if p.get("playerId", UNFILLED) not in (UNFILLED, None)]


def analyse_draft(payload: Any) -> list[str]:
    """Answer question 1: is the winning bid amount in the pick JSON?"""
    out: list[str] = ["## Q1 — Does the pick JSON carry auction bid amounts?"]
    detail = payload.get("draftDetail") if isinstance(payload, dict) else None

    if not isinstance(detail, dict):
        out.append("  draftDetail is absent from this response — nothing to inspect.")
        return out

    out.append(f"  draftDetail.drafted    = {detail.get('drafted')}")
    out.append(f"  draftDetail.inProgress = {detail.get('inProgress')}")

    picks = _picks(payload)
    out.append(f"  pick count             = {len(picks)}")
    if not picks:
        out.append("  No picks yet. Nominate and sell at least one player, then re-run.")
        return out

    out.append(f"  keys present on picks  = {', '.join(key_union(picks))}")

    for field in ("bidAmount", "nominatingTeamId", "autoDraftTypeId", "memberId", "keeper"):
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

    # ESPN pre-creates the whole pick skeleton before anyone bids, so
    # `len(picks)` says nothing about progress. A pick is *filled* when its
    # playerId stops being the -1 sentinel. Without that distinction an
    # untouched skeleton looks identical to "ESPN never populates prices" —
    # opposite conclusions drawn from the same zeros.
    filled = _filled(picks)
    bids = [p.get("bidAmount") for p in filled if p.get("bidAmount")]

    out.append(f"  filled picks           = {len(filled)}/{len(picks)} (playerId != -1)")
    out.append("")

    if not filled:
        out.append(
            "  VERDICT: INCONCLUSIVE — the pick skeleton exists but nothing has "
            "sold yet. This is the expected pre-draft baseline, not evidence "
            "either way. Note that a *running* draft returns this same payload."
        )
    elif bids:
        out.append(
            f"  VERDICT: bidAmount IS populated ({len(bids)}/{len(filled)} filled "
            f"picks, ${min(bids)}-${max(bids)}, total ${sum(bids)})."
        )
    else:
        out.append(
            f"  VERDICT: {len(filled)} pick(s) are filled but every bidAmount is "
            "ZERO. ESPN is reporting who won, not for how much."
        )

    out.append("")
    out.append("  First pick verbatim:")
    out.append("    " + json.dumps(picks[0], indent=2).replace("\n", "\n    "))
    return out


def analyse_settings(payload: Any) -> list[str]:
    """Confirm what kind of draft this is, and find the budget."""
    out: list[str] = ["## League settings"]
    settings = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings, dict):
        out.append("  settings absent — request the mSettings view.")
        return out

    draft = settings.get("draftSettings") or {}
    roster = settings.get("rosterSettings") or {}

    out.append(f"  league name                 = {settings.get('name')}")
    out.append(f"  draftSettings.type          = {draft.get('type')}")
    out.append(f"  draftSettings.auctionBudget = {draft.get('auctionBudget')}")
    out.append(f"  draftSettings.keeperCount   = {draft.get('keeperCount')}")
    out.append(f"  draftSettings.pickOrder     = {draft.get('pickOrder')}")
    out.append(f"  size (team count)           = {settings.get('size')}")
    out.append(
        f"  scoringSettings.scoringType = "
        f"{(settings.get('scoringSettings') or {}).get('scoringType')}"
    )
    out.append(f"  lineupSlotCounts            = {roster.get('lineupSlotCounts')}")
    out.append("")
    out.append(
        "  NOTE: the espn-api library's BaseSettings exposes neither "
        "auctionBudget nor draft type, which is why this reads the raw view."
    )
    return out


def analyse_teams(payload: Any) -> list[str]:
    """Answer question 2: what does a team expose for budget and roster?"""
    out: list[str] = ["## Q2 — What does a team object expose for budget/roster?"]
    teams = payload.get("teams") if isinstance(payload, dict) else None
    if not isinstance(teams, list) or not teams:
        out.append("  no teams array in this response.")
        return out

    out.append(f"  team count             = {len(teams)}")
    out.append(f"  keys present on teams  = {', '.join(key_union(teams))}")

    ids = [t.get("id") for t in teams if isinstance(t, dict)]
    out.append(f"  team ids               = {ids}")
    numeric = [i for i in ids if isinstance(i, int)]
    if numeric and sorted(numeric) != list(range(min(numeric), max(numeric) + 1)):
        out.append(
            "  ^ NOTE: these ids are NOT contiguous. Never generate them with "
            "range() — doing so invents a team that does not exist and drops "
            "one that does."
        )

    money_keys = [
        k for k in key_union(teams)
        if any(tok in k.lower() for tok in
               ("budget", "bid", "cap", "salary", "spent", "acquisition"))
    ]
    out.append(f"  budget-ish keys        = {money_keys or 'NONE FOUND'}")

    first = teams[0]
    entries = (
        ((first.get("roster") or {}).get("entries") or [])
        if isinstance(first, dict) else []
    )
    out.append(f"  team[0].roster.entries = {len(entries)}")
    if entries:
        out.append(f"  roster entry keys      = {', '.join(key_union(entries))}")
        bid_keys = [
            k for k in key_union(entries) if "bid" in k.lower() or "value" in k.lower()
        ]
        out.append(f"  entry value/bid keys   = {bid_keys or 'NONE FOUND'}")
        out.append("")
        out.append("  First roster entry verbatim (truncated):")
        sample = json.dumps(entries[0], indent=2)
        if len(sample) > 2000:
            sample = sample[:2000] + "\n... (truncated)"
        out.append("    " + sample.replace("\n", "\n    "))

    out.append("")
    out.append(
        "  If no per-team remaining-budget field exists, remaining budget has "
        "to be derived: budget - sum(bidAmount of that team's picks). Note "
        "that transactionCounter.acquisitionBudgetSpent is the FAAB waiver "
        "budget — a different pot entirely, and an easy thing to misread."
    )
    return out


ANALYSERS = {
    "mDraftDetail": analyse_draft,
    "mSettings": analyse_settings,
    "mTeam": analyse_teams,
    "mRoster": analyse_teams,
}


def analyse_payload(payload: Any) -> list[str]:
    """Run whichever analysers the payload's shape calls for.

    Detection is by shape, not by filename or view name, so this works on a
    browser-saved file, a raw `--url` response, or a body pulled out of a HAR.
    """
    report: list[str] = []

    detail = payload.get("draftDetail") if isinstance(payload, dict) else None
    if isinstance(detail, dict) and "picks" in detail:
        report.extend(analyse_draft(payload))
    if isinstance(payload, dict) and payload.get("settings"):
        report.append("")
        report.extend(analyse_settings(payload))
    if isinstance(payload, dict) and payload.get("teams"):
        report.append("")
        report.extend(analyse_teams(payload))

    if not report:
        report.append(
            "  Nothing recognizable here — expected a draftDetail, settings, or "
            "teams key. Is this the right view?"
        )
    return report


def analyse_file(path: Path) -> list[str]:
    """Run the analysers over a payload someone already captured.

    The whole point: a machine that cannot reach ESPN — a CI runner, a
    sandbox, a locked-down laptop — still gets the full verdict from a payload
    saved out of a browser.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"cannot read {path}: {exc}"]
    except ValueError as exc:
        return [f"{path} is not valid JSON: {exc}"]

    report = ["=" * 72, f"FILE {path}", "=" * 72]
    report.extend(analyse_payload(normalize_payload(payload)))
    return report


# --------------------------------------------------------------------------
# HAR ingestion — read what the draft room actually fetched
# --------------------------------------------------------------------------

ESPN_HOST_FRAGMENTS = ("fantasy.espn.com", "lm-api-reads.fantasy.espn.com")


def _har_body(entry: dict[str, Any]) -> Any:
    """Decode one HAR entry's response body to JSON, or None.

    Bodies may be absent (redirects, preflights, bodies the browser dropped)
    or base64-encoded. Neither is an error worth stopping for.
    """
    content = (entry.get("response") or {}).get("content") or {}
    text = content.get("text")
    if not isinstance(text, str) or not text:
        return None
    if content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def analyse_har(path: Path) -> list[str]:
    """Report every ESPN API call in a browser capture, and which carry picks.

    This is the one method that observes rather than guesses: whatever the
    draft room fetched to render those picks is in here by construction.

    Request cookies and headers are never read or echoed — a HAR carries live
    espn_s2 / SWID, which is also why `*.har` is gitignored.
    """
    try:
        har = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"cannot read {path}: {exc}"]
    except ValueError as exc:
        return [f"{path} is not valid HAR JSON: {exc}"]

    entries = ((har.get("log") or {}).get("entries") or []) if isinstance(har, dict) else []
    report = ["=" * 72, f"HAR {path}", "=" * 72]

    rows: list[tuple[str, int, int, int, str]] = []
    best: tuple[int, str, Any] | None = None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") or {}
        url = request.get("url")
        if not isinstance(url, str) or not any(f in url for f in ESPN_HOST_FRAGMENTS):
            continue

        method = str(request.get("method", "?"))
        status = int((entry.get("response") or {}).get("status") or 0)
        payload = normalize_payload(_har_body(entry))
        picks = _picks(payload) if payload is not None else []
        filled = _filled(picks)

        rows.append((method, status, len(filled), len(picks), url))
        if filled and (best is None or len(filled) > best[0]):
            best = (len(filled), url, payload)

    if not rows:
        report.append(
            f"  No ESPN API calls found among {len(entries)} entries. Was the "
            "capture taken with the draft room open, and saved *with content*?"
        )
        return report

    report.append(f"  {len(rows)} ESPN call(s) found in {len(entries)} entries.")
    report.append("")
    for method, status, filled_n, pick_n, url in rows:
        marker = " <<<" if filled_n else ""
        picks_col = f"{filled_n} filled / {pick_n} picks" if pick_n else "no picks"
        report.append(f"  {method:<5} {status:<4} {picks_col:<24} {url}{marker}")

    report.append("")
    if best is None:
        report.append(
            "  ANSWER: none of these responses carried a filled pick. Either no "
            "player had sold when the capture was taken, or the draft room is "
            "driven by a channel this HAR did not record (an SSE stream or a "
            "websocket is not an API call). Sell 2-3 players and re-capture."
        )
    else:
        count, url, payload = best
        report.append(f"  ANSWER: {count} filled pick(s) at")
        report.append(f"    {url}")
        report.append("")
        report.extend(analyse_payload(payload))
    return report


# --------------------------------------------------------------------------
# Fetch modes
# --------------------------------------------------------------------------


def run_once(
    client: EspnClient,
    views: tuple[str, ...],
    out_dir: Path,
    stamp: str,
    *,
    segment: str = "0",
    history: bool = False,
    tag: str = "",
) -> list[str]:
    report: list[str] = []
    for view in views:
        result = client.try_view(view, segment=segment, history=history)
        report.append("")
        report.append("=" * 72)
        report.append(f"VIEW {view}")
        report.append(f"  url: {result.url}")
        report.append("=" * 72)

        if not result.ok:
            report.append(f"  HTTP {result.status}: {result.error}")
            continue

        clean = scrub(normalize_payload(result.payload, client.year), client.credentials)

        suffix = f"_{tag}" if tag else ""
        target = out_dir / f"{view}{suffix}_{stamp}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(clean, indent=2), encoding="utf-8")
        report.append(f"  saved to {target}")

        analyser = ANALYSERS.get(view)
        if analyser:
            report.extend(analyser(clean))
        else:
            report.append(f"  (no analyser for {view}; raw payload saved)")
    return report


@dataclass(frozen=True)
class Candidate:
    """One URL shape worth trying when hunting for practice-draft picks."""

    label: str
    segment: str = "0"
    history: bool = False
    view: str = "mDraftDetail"
    league_id_override: int | None = None


# `segments/0` is the only shape ever confirmed against this API. The other
# segment ids are a guess with no evidence behind them — they are here because
# the search space is small and cheap, not because ESPN is known to use them.
# The HAR path is the one that actually answers the question.
DEFAULT_SWEEP_CANDIDATES: tuple[Candidate, ...] = (
    Candidate("segment0", segment="0"),
    Candidate("segment1", segment="1"),
    Candidate("segment2", segment="2"),
    Candidate("history", history=True),
)


def sweep(
    client: EspnClient,
    candidates: Iterable[Candidate],
    out_dir: Path,
    stamp: str,
) -> list[str]:
    """Try every candidate URL shape and report what each returned.

    Deliberately has no exception path: a 404 on one candidate is a *result*,
    not a failure, and must never stop the remaining candidates.
    """
    report: list[str] = ["## Sweep — which URL shape carries draft picks?", ""]
    rows: list[tuple[str, str, str]] = []

    for cand in candidates:
        target_league = cand.league_id_override or client.league_id
        result = client.try_view(
            cand.view,
            segment=cand.segment,
            history=cand.history,
            league_id=target_league,
        )

        if not result.ok:
            outcome = (
                f"HTTP {result.status}" if result.status != -1
                else f"request failed: {result.error}"
            )
        else:
            clean = scrub(
                normalize_payload(result.payload, client.year), client.credentials
            )
            picks = _picks(clean)
            filled = _filled(picks)

            path = out_dir / f"sweep_{cand.label}_{stamp}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(clean, indent=2), encoding="utf-8")

            outcome = (
                "200, no draftDetail.picks" if not picks
                else f"200, {len(filled)} filled / {len(picks)} picks"
            )
            outcome += f"  -> {path.name}"

        rows.append((cand.label, str(target_league), outcome))

    width = max(len(r[0]) for r in rows)
    for label, target_league, outcome in rows:
        report.append(f"  {label:<{width}}  league {target_league}  {outcome}")

    report.append("")
    report.append(
        "  Any row with a non-zero filled count is where the picks live. All "
        "zeros means this candidate set missed — run --har next, which "
        "observes the real endpoint instead of guessing at it."
    )
    return report


def run_urls(
    client: EspnClient, urls: Iterable[str], out_dir: Path, stamp: str
) -> list[str]:
    """Fetch literal URLs with cookies attached and analyse whatever comes back."""
    report: list[str] = []
    for i, url in enumerate(urls):
        result = client.try_url(url)
        report.append("")
        report.append("=" * 72)
        report.append(f"URL {url}")
        report.append("=" * 72)

        if not result.ok:
            report.append(f"  HTTP {result.status}: {result.error}")
            continue

        clean = scrub(normalize_payload(result.payload), client.credentials)
        target = out_dir / f"url{i}_{stamp}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(clean, indent=2), encoding="utf-8")
        report.append(f"  saved to {target}")
        report.extend(analyse_payload(clean))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--analyse", "--analyze", type=Path, nargs="+", dest="analyse", default=None,
        metavar="FILE",
        help="Analyse already-captured JSON instead of fetching. No network, no cookies.",
    )
    parser.add_argument(
        "--har", type=Path, nargs="+", default=None, metavar="FILE",
        help="Analyse a DevTools HAR export. No network, no cookies. Never commit the .har.",
    )
    parser.add_argument("--league-id", type=int, default=None,
                        help="ESPN league id (or set ESPN_LEAGUE_ID)")
    parser.add_argument("--year", type=int, default=None,
                        help="Season year (or set ESPN_SEASON_YEAR)")
    parser.add_argument("--view", action="append", dest="views",
                        help="View to fetch; repeatable. Default: all four.")
    parser.add_argument("--segment", default="0",
                        help="Path segment id. Default 0 — the only shape confirmed so far.")
    parser.add_argument("--history", action="store_true",
                        help="Use the leagueHistory path instead of the seasonal one.")
    parser.add_argument("--sweep", action="store_true",
                        help="Try several URL shapes and report which carries picks.")
    parser.add_argument("--candidate-league-id", type=int, action="append",
                        dest="candidate_league_ids",
                        help="Extra league id to sweep, e.g. a shadow id from a draft-room URL.")
    parser.add_argument("--url", action="append", dest="urls",
                        help="Fetch a literal URL with cookies attached. Repeatable.")
    parser.add_argument("--out", type=Path, default=Path("probe_out"),
                        help="Directory for captured payloads")
    parser.add_argument("--env", type=Path, default=Path(".env"),
                        help="Path to the .env holding ESPN_S2 / ESPN_SWID")
    parser.add_argument("--public", action="store_true",
                        help="Skip cookie auth (public leagues only)")
    parser.add_argument("--watch", type=float, default=None, metavar="SECONDS",
                        help="Re-probe on an interval.")
    args = parser.parse_args()

    # Offline modes run before anything touches credentials or the network.
    if args.analyse:
        for path in args.analyse:
            print("\n".join(analyse_file(path)))
            print()
        return 0

    if args.har:
        for path in args.har:
            print("\n".join(analyse_har(path)))
            print()
        return 0

    try:
        creds: EspnCredentials | None = None if args.public else load_credentials(args.env)
        env = load_dotenv(args.env)
    except EspnApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

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
    client = EspnClient(league_id, year, credentials=creds)

    print(f"probing league {league_id} season {year}")
    print(f"auth: {'cookies loaded' if creds else 'NONE (public mode)'}")
    print(f"views: {', '.join(views)}")
    print(f"hosts: {', '.join(HOSTS)}")

    candidates = DEFAULT_SWEEP_CANDIDATES + tuple(
        Candidate(f"league{lid}", league_id_override=lid)
        for lid in (args.candidate_league_ids or ())
    )

    try:
        while True:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            if args.urls:
                report = run_urls(client, args.urls, args.out, stamp)
            elif args.sweep:
                report = sweep(client, candidates, args.out, stamp)
            else:
                tag = (
                    "history" if args.history
                    else f"seg{args.segment}" if args.segment != "0"
                    else ""
                )
                report = run_once(
                    client, views, args.out, stamp,
                    segment=args.segment, history=args.history, tag=tag,
                )
            print("\n".join(report))

            summary = args.out / f"report_{stamp}.txt"
            summary.parent.mkdir(parents=True, exist_ok=True)
            summary.write_text("\n".join(report), encoding="utf-8")
            print(f"\nreport written to {summary}")
            print(
                "\nreminder: captures scrub YOUR credentials only. Run "
                "tools/anonymize_capture.py before committing or sharing any "
                "of them — other members' ids are still in there."
            )

            if args.watch is None:
                return 0
            print(f"\n--- sleeping {args.watch}s (Ctrl-C to stop) ---\n")
            time.sleep(args.watch)
    except EspnApiError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
