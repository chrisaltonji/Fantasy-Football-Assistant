"""`ffa config alias` — declaring that two ESPN accounts are one person.

    ffa config alias --list
    ffa config alias --same 13 6            # by team id, nickname, name, or SWID
    ffa config alias --remove <swid>

**Discovery is the hard part**, which is why `--list` leads with orphans rather
than with the table. Brian Cona's second account was only found by reading a raw
`mTeam` payload and noticing that a name in `members` held no seat; nothing in
the tool would ever have mentioned it. So this walks every cached season and
reports accounts that played and are no longer seated, with the seasons and pick
counts that make the connection obvious to a human — and then stops. It never
proposes a link, because two similar names are not evidence and identity is the
one thing this codebase refuses to guess at.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from ffa.config.identity import OwnerResolver, fold_owner_id, richest_name
from ffa.config.loader import DEFAULT_CONFIG_PATH, load_config, write_config
from ffa.config.schema import ConfigError
from ffa.history.fetch import DEFAULT_CACHE, load_cached


def _accounts(cache: Path, years) -> dict[str, dict]:
    """Every ESPN account that appears in a cached season, with what it did."""
    out: dict[str, dict] = {}
    for year in years:
        raw = load_cached(year, cache) or {}
        teams = raw.get("mTeam") or {}
        for member in teams.get("members") or []:
            if not member.get("id"):
                continue
            key = fold_owner_id(str(member["id"]))
            entry = out.setdefault(
                key, {"names": [], "handles": set(), "seasons": set(), "seats": set()}
            )
            name = " ".join(
                str(p).strip()
                for p in (member.get("firstName"), member.get("lastName"))
                if p and str(p).strip()
            )
            if name:
                entry["names"].append(name)
            if member.get("displayName"):
                entry["handles"].add(str(member["displayName"]))
        for team in teams.get("teams") or []:
            owner = team.get("primaryOwner")
            if not owner:
                continue
            entry = out.setdefault(
                fold_owner_id(str(owner)),
                {"names": [], "handles": set(), "seasons": set(), "seats": set()},
            )
            entry["seasons"].add(year)
            entry["seats"].add(int(team["id"]))

    return out


def _picks_by_owner(cache: Path, years) -> dict[str, int]:
    counts: dict[str, int] = {}
    for year in years:
        raw = load_cached(year, cache) or {}
        owners = {
            int(t["id"]): fold_owner_id(str(t["primaryOwner"]))
            for t in ((raw.get("mTeam") or {}).get("teams") or [])
            if t.get("primaryOwner")
        }
        picks = ((raw.get("mDraftDetail") or {}).get("draftDetail") or {}).get("picks") or []
        for pick in picks:
            if int(pick.get("playerId", -1) or -1) == -1:
                continue
            owner = owners.get(int(pick.get("teamId") or 0))
            if owner:
                counts[owner] = counts.get(owner, 0) + 1
    return counts


def _resolve_account(token: str, config, accounts) -> str:
    """A team id, a nickname, a real name, or a raw SWID -> a folded SWID."""
    text = str(token).strip()
    lowered = text.lower().lstrip("t")

    if lowered.isdigit():
        team_id = int(lowered)
        if config.owners.get(team_id):
            return fold_owner_id(config.owners[team_id])
        # A seat that no longer exists — find whoever last held it.
        held = [
            swid for swid, info in accounts.items() if team_id in info["seats"]
        ]
        if len(held) == 1:
            return held[0]
        if held:
            raise ConfigError(
                f"team {team_id} was held by more than one account across the "
                "cached seasons; name the SWID directly"
            )
        raise ConfigError(f"no team {team_id} in this league or its history")

    folded = fold_owner_id(text)
    if folded in accounts or any(fold_owner_id(o) == folded for o in config.owners.values()):
        return folded

    matches = {
        swid
        for swid, info in accounts.items()
        if richest_name(info["names"]).lower().startswith(text.lower())
        or any(h.lower() == text.lower() for h in info["handles"])
    }
    for team_id, nickname in config.managers.items():
        if str(nickname).lower() == text.lower() and config.owners.get(team_id):
            matches.add(fold_owner_id(config.owners[team_id]))

    if len(matches) == 1:
        return next(iter(matches))
    if matches:
        raise ConfigError(f"{token!r} matches more than one account: {sorted(matches)}")
    raise ConfigError(
        f"no account matches {token!r}. Try a team id, a manager nickname, or a SWID."
    )


def cmd_config_alias(args: argparse.Namespace) -> int:
    config = load_config(args.path)
    years = list(range(config.year - 8, config.year + 1))
    accounts = _accounts(args.cache, years)
    picks = _picks_by_owner(args.cache, years)
    seated = {fold_owner_id(s): t for t, s in config.owners.items() if s}
    resolver = OwnerResolver.from_config(config)

    if args.same:
        if len(args.same) != 2:
            raise ConfigError("--same takes exactly two accounts")
        a = _resolve_account(args.same[0], config, accounts)
        b = _resolve_account(args.same[1], config, accounts)
        if a == b:
            raise ConfigError("those are already the same account")

        seated_members = [x for x in (a, b) if x in seated]
        if len(seated_members) == 2:
            raise ConfigError(
                f"both accounts hold a seat this season (team {seated[a]} and team "
                f"{seated[b]}). They cannot be one person — picks would be credited "
                "to the wrong manager."
            )
        primary = seated_members[0] if seated_members else sorted((a, b))[0]
        secondary = b if primary == a else a

        merged = dict(config.aliases)
        merged[secondary] = primary
        updated = replace(config, aliases=merged)
        updated.validate()
        write_config(updated, args.path)
        name = richest_name(
            accounts.get(secondary, {}).get("names", [])
            + accounts.get(primary, {}).get("names", [])
        )
        print(f"wrote {args.path}")
        print(f"  {secondary}\n    is the same person as\n  {primary}"
              + (f"   ({name})" if name else ""))
        print("\nRe-run `ffa history report` to merge their record.")
        return 0

    if args.remove:
        folded = fold_owner_id(args.remove)
        merged = {k: v for k, v in config.aliases.items() if fold_owner_id(k) != folded}
        if len(merged) == len(config.aliases):
            raise ConfigError(f"no alias declared for {args.remove}")
        write_config(replace(config, aliases=merged), args.path)
        print(f"removed the alias for {folded}")
        return 0

    # --- default: show the table, and lead with what you cannot see -----------

    for warning in resolver.warnings:
        print(f"! {warning}")

    orphans = [
        (swid, info)
        for swid, info in sorted(accounts.items())
        if swid not in seated and not resolver.is_alias(swid)
    ]
    if orphans:
        print("ESPN account(s) in your league history that hold no seat today:\n")
        for swid, info in orphans:
            name = richest_name(info["names"]) or "(no name)"
            handle = sorted(info["handles"])[0] if info["handles"] else "-"
            seasons = ", ".join(str(y) for y in sorted(info["seasons"])) or "no seat"
            print(f"  {swid}")
            print(f"    {name:<20} {handle:<20} {seasons}, {picks.get(swid, 0)} picks")
        print(
            "\nIf one of those is a second login for somebody still in the league,\n"
            "say so — it is never guessed from a name:\n"
            "  ffa config alias --same <team id> <that swid>\n"
        )
    else:
        print("every account in your league history holds a seat or is declared.\n")

    if not config.aliases:
        print("no aliases declared.")
        return 0

    print("declared aliases:")
    for secondary, primary in sorted(config.aliases.items()):
        team = seated.get(fold_owner_id(primary))
        name = richest_name(
            accounts.get(fold_owner_id(secondary), {}).get("names", [])
            + accounts.get(fold_owner_id(primary), {}).get("names", [])
        )
        print(f"  {secondary}  ->  {primary}"
              + (f"  (team {team})" if team else "")
              + (f"  {name}" if name else ""))
    return 0


def add_parser(config_sub) -> None:
    alias = config_sub.add_parser(
        "alias", help="declare that two ESPN accounts are the same person"
    )
    alias.add_argument("--path", type=Path, default=DEFAULT_CONFIG_PATH)
    alias.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    alias.add_argument("--same", nargs=2, metavar=("A", "B"), default=None,
                       help="two accounts, as team ids, nicknames, names or SWIDs")
    alias.add_argument("--remove", default=None, metavar="SWID")
    alias.set_defaults(func=cmd_config_alias)
