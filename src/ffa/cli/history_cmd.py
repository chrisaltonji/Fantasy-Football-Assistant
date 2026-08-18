"""`ffa history` — what four prior auctions say about the twelve people in the room.

    ffa history fetch     pull every season ESPN still serves, and cache it
    ffa history report    the HTML report, plus a dossier draft to argue with

Split in two on purpose. Fetching is slow and hits the network; the analysis is
the part that gets iterated on, and it should never need ESPN to be reachable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ffa.config.identity import OwnerResolver, richest_name, seats
from ffa.config.loader import DEFAULT_CONFIG_PATH, load_config, load_credentials
from ffa.config.schema import ConfigError
from ffa.history.evidence import ROUNDS, assess
from ffa.history.fetch import DEFAULT_CACHE, build_history, download_season, load_cached
from ffa.history.metrics import build_profiles
from ffa.history.standing import finish_evidence
from ffa.history.precedent import (
    DEFAULT_PRECEDENT_PATH,
    build_precedent,
    save_precedent,
)
from ffa.history.report import render_report
from ffa.history.suggest import suggest_dossiers

# ESPN answers 202 for a season the league did not exist in, so walking back is
# cheap and self-terminating. Nothing before this is worth asking about.
EARLIEST = 2015


def _years(args, config) -> list[int]:
    if args.years:
        return sorted({int(y) for y in args.years})
    first = max(EARLIEST, args.since or (config.year - 8))
    return list(range(first, config.year))


def _managers(cache: Path, years, resolver=None) -> dict[str, str]:
    """Resolved owner id -> real name, pooled across every cached season.

    Pooled because somebody who left in 2023 is still a manager in the seasons
    they played, and their name only appears in those payloads.

    Where two accounts resolve to one person they also carry two names — ESPN
    has this league's Brian Cona as `Brian Cona` on one and `B C` on the other —
    so the most informative one wins rather than whichever season was read last.
    """
    candidates: dict[str, list[str]] = {}
    for year in years:
        raw = load_cached(year, cache) or {}
        for member in (raw.get("mTeam") or {}).get("members") or []:
            name = " ".join(
                str(p).strip()
                for p in (member.get("firstName"), member.get("lastName"))
                if p and str(p).strip()
            )
            if name and member.get("id"):
                key = (
                    resolver.resolve(str(member["id"]))
                    if resolver is not None
                    else str(member["id"])
                )
                candidates.setdefault(key, []).append(name)
    return {key: richest_name(names) for key, names in candidates.items()}


def cmd_history_fetch(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    creds = load_credentials()
    cookies = creds.as_cookies() if creds else None
    years = _years(args, config)

    print(f"reading league {config.league_id} — {years[0]} to {years[-1]}")
    for year in years:
        raw = download_season(config.league_id, year, cookies=cookies, cache=args.cache)
        status = raw.get("mDraftDetail_status")
        detail = (raw.get("mDraftDetail") or {}).get("draftDetail") or {}
        filled = sum(
            1 for p in (detail.get("picks") or []) if int(p.get("playerId", -1) or -1) != -1
        )
        print(f"  {year}: {status:<10} {filled} filled pick(s)")

    print(f"\ncached under {args.cache}/")
    print("Now:  ffa history report")
    return 0


def cmd_history_report(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    years = _years(args, config)
    resolver = OwnerResolver.from_config(config)
    history = build_history(
        years, cache=args.cache, league_name=config.name, league_id=config.league_id,
        resolver=resolver,
    )

    if not history.seasons:
        raise ConfigError(
            "no usable seasons in the cache. Run `ffa history fetch` first — and "
            "if it has already run, every season ESPN served was either not an "
            "auction or had no prices recorded."
        )

    managers = _managers(args.cache, years, resolver)
    profiles = build_profiles(history, managers)
    if resolver:
        print(f"{len(resolver)} alias(es) applied — second accounts merged")

    print(f"seasons used: {', '.join(str(y) for y in history.years)}")
    for gap in sorted(history.gaps, key=lambda g: g.year):
        print(f"  ! {gap.year} excluded — {gap.reason}", file=sys.stderr)
    print(f"{len(profiles)} manager(s) profiled\n")

    print(f"  {'manager':<22}{'shape':<18}{'pace':<13}{'chasing':<11}nominating")
    for profile in profiles:
        pooled = profile.pooled
        print(
            f"  {profile.manager:<22}{pooled.spend_shape or '—':<18}"
            f"{pooled.pace or '—':<13}{pooled.chases or '—':<11}"
            f"{pooled.nomination_style or '—'}"
        )

    # Recomputed on every run rather than quoted from a comment: a claim that
    # cannot be re-derived goes stale while still sounding authoritative.
    print("\nscoring every signal against its null...")
    scores = assess(history, rounds=args.rounds)
    # Not a draft statistic, so it carries its own null: shuffle who finished
    # where, within each season. It is the only measured answer to "how good is
    # this manager", which every other read is weighed against.
    scores["finishing position"] = finish_evidence(history, rounds=args.rounds * 5)
    for name, ev in sorted(scores.items(), key=lambda kv: -kv[1].z):
        mark = "  " if ev.survives else "! "
        print(f"  {mark}{name:<20}{ev.null:<12}z={ev.z:+5.1f}   {ev.verdict}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        render_report(history, profiles, generated=args.stamp or "", scores=scores),
        encoding="utf-8",
    )
    print(f"\nwrote {args.out}")

    precedent = build_precedent(history, profiles)
    save_precedent(precedent, args.precedent)
    print(f"wrote {args.precedent} — {len(precedent)} manager(s), "
          f"useful through {precedent.window:.0%} of the board")

    suggested = suggest_dossiers(profiles, seats(config))
    args.suggest.parent.mkdir(parents=True, exist_ok=True)
    args.suggest.write_text(json.dumps(suggested, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.suggest} — {len(suggested)} manager(s)")
    print(
        "\nThat file is a draft, not a finding. Read it, disagree with it, then:\n"
        f"  ffa dossier import {args.suggest} --dry-run"
    )
    return 0


def add_parser(sub) -> None:
    history = sub.add_parser(
        "history", help="what prior seasons say about how each manager drafts"
    )
    history_sub = history.add_subparsers(dest="history_command", required=True)

    def common(parser):
        parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
        parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
        parser.add_argument("--years", nargs="*", type=int, default=None,
                            help="explicit seasons; default walks back from this one")
        parser.add_argument("--since", type=int, default=None,
                            help="earliest season to try")
        return parser

    fetch = common(history_sub.add_parser("fetch", help="download and cache prior seasons"))
    fetch.set_defaults(func=cmd_history_fetch)

    report = common(history_sub.add_parser("report", help="analyse the cached seasons"))
    report.add_argument("--out", type=Path, default=Path("docs/draft_history.html"))
    report.add_argument("--suggest", type=Path,
                        default=Path("docs/dossier_suggested.json"))
    report.add_argument("--stamp", default="", help="text to print in the header")
    report.add_argument("--precedent", type=Path, default=DEFAULT_PRECEDENT_PATH,
                        help="where to write the spending scripts the draft loads")
    report.add_argument("--rounds", type=int, default=ROUNDS,
                        help="permutation rounds when scoring signals against chance")
    report.set_defaults(func=cmd_history_report)
