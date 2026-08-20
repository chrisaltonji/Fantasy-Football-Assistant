#!/usr/bin/env python3
"""Turn a raw probe capture into a committable fixture.

`espn_probe.scrub` removes *your* credentials. It does not touch the other
eleven managers' member SWIDs, which ride along in `mDraftDetail` picks as
`memberId` and in `mTeam` as `members[].id`. Those are real account
identifiers for real people and must never land in a public repo.

This maps every SWID-shaped string to a synthetic one, stably and
consistently, so relationships between picks and members survive while the
identities do not. It also blanks display names.

    python tools/anonymize_capture.py probe_out/prior_season/mDraftDetail_*.json \
        --out tests/fixtures/espn/mDraftDetail_completed_auction.json

Player ids and bid amounts are deliberately left alone — they are public
reference data and the whole reason the capture is worth keeping.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# ESPN SWIDs are brace-wrapped UUIDs. Match loosely: the point is to catch
# every one, and a false positive on some other UUID-shaped id is harmless
# because the replacement is still stable and reversible within one file.
SWID_RE = re.compile(r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}")

# Free-text fields managers control. Names get placeholders that look obviously
# wrong, so anything keying off them fails a test instead of failing live.
NAME_FIELDS = ("displayName", "firstName", "lastName", "nickname", "name", "abbrev")


def synthetic_swid(index: int) -> str:
    """Match the style already used by the committed fixtures.

    One hex character repeated, with the same character as the suffix — so
    team 11 reads `{BBBBBBBB-...-00000000000B}`, exactly as mTeam_predraft
    does. Hex rather than decimal because `str(11 % 10) * 8` collides with
    index 1, which is confusing in a file you read by eye.
    """
    char = "0123456789ABCDEF"[index % 16]
    return "{%s-0000-4000-8000-%012X}" % (char * 8, index)


def build_swid_map(text: str) -> dict[str, str]:
    """Stable mapping, ordered by first appearance so diffs stay readable.

    `<redacted>` is included deliberately. `espn_probe.scrub` has already
    replaced *your own* SWID with that literal by the time a capture hits
    disk, so without this your team is the one team in the fixture whose
    member id is not SWID-shaped — and anything asserting on shape trips over
    it. It stands for a real identity, so it gets a synthetic one like the
    rest.
    """
    mapping: dict[str, str] = {}
    for match in SWID_RE.findall(text):
        if match not in mapping:
            mapping[match] = synthetic_swid(len(mapping) + 1)
    if "<redacted>" in text:
        mapping["<redacted>"] = synthetic_swid(len(mapping) + 1)
    return mapping


def blank_names(node: Any, counter: dict[str, int]) -> Any:
    """Replace manager-controlled free text in place, recursively."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in NAME_FIELDS and isinstance(value, str) and value:
                counter["n"] += 1
                node[key] = f"Volatile Team Name {counter['n']}"
            else:
                blank_names(value, counter)
    elif isinstance(node, list):
        for item in node:
            blank_names(item, counter)
    return node


def anonymize(text: str) -> tuple[Any, dict[str, str], int]:
    mapping = build_swid_map(text)
    for real, fake in mapping.items():
        text = text.replace(real, fake)

    leftover = [s for s in SWID_RE.findall(text) if s not in mapping.values()]
    if leftover:
        raise SystemExit(f"refusing to write: {len(leftover)} SWID(s) survived replacement")

    counter = {"n": 0}
    payload = blank_names(json.loads(text), counter)
    return payload, mapping, counter["n"]


# --- the view payload ------------------------------------------------------
#
# `build_view()` output carries identity under different keys than a raw ESPN
# capture, and `docs/sample_state*.json` are committed so a dashboard designer
# has something real to build against. "Real" has to mean real *shape*: the
# league name, the league id, the manager nicknames and every `owner_id` are
# twelve actual people, and the repo is public.

# Deliberately the names the committed samples already used, so a regenerated
# fixture is a small diff rather than a rewrite.
SAMPLE_MANAGERS = (
    "alex", "blake", "casey", "drew", "evan", "frankie",
    "gray", "harper", "indigo", "jordan", "kai", "lane",
)
SAMPLE_LEAGUE = "Example Auction League"
SAMPLE_LEAGUE_ID = 123456


def anonymize_view(payload: Any) -> tuple[Any, int]:
    """Strip identity from a `build_view()` payload, keeping every number.

    Prices, budgets, ceilings, scarcity and player names all survive untouched
    — they are the entire reason the sample is worth committing, and NFL player
    names are public. What goes is who the twelve people are.
    """
    text = json.dumps(payload)
    mapping = build_swid_map(text)
    for real, fake in mapping.items():
        text = text.replace(real, fake)
    payload = json.loads(text)

    # Nicknames are assigned by first appearance in `teams`, so the same person
    # keeps the same alias everywhere they are referenced.
    labels: dict[int, str] = {}
    for index, team in enumerate(payload.get("teams") or []):
        labels[team.get("team_id")] = SAMPLE_MANAGERS[index % len(SAMPLE_MANAGERS)]

    def relabel(team: Any) -> None:
        if not isinstance(team, dict):
            return
        alias = labels.get(team.get("team_id"))
        if alias:
            team["label"] = alias
            if team.get("manager"):
                team["manager"] = alias
        # The team's own name is manager-controlled free text.
        if team.get("name"):
            team["name"] = "Volatile Team Name"

    for team in payload.get("teams") or []:
        relabel(team)
    relabel(payload.get("me"))

    # Labels also ride along inside the advisory blocks.
    guidance = (payload.get("nomination") or {}).get("guidance") or {}
    for block in ("threats", "pace_reads"):
        for row in guidance.get(block) or []:
            if isinstance(row, dict) and row.get("team_id") in labels:
                row["label"] = labels[row["team_id"]]
    for key in ("on_the_clock", "current_nominator", "my_next"):
        turn = (payload.get("nomination_plan") or {}).get(key)
        if isinstance(turn, dict) and turn.get("team_id") in labels:
            turn["label"] = labels[turn["team_id"]]
    for turn in (payload.get("nomination_plan") or {}).get("upcoming") or []:
        if isinstance(turn, dict) and turn.get("team_id") in labels:
            turn["label"] = labels[turn["team_id"]]

    league = payload.get("league")
    if isinstance(league, dict):
        league["name"] = SAMPLE_LEAGUE

    # The draft id embeds the real league id.
    if payload.get("draft_id"):
        payload["draft_id"] = f"{SAMPLE_LEAGUE_ID}-sample"

    return payload, len(mapping)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="Raw capture from tools/espn_probe.py")
    parser.add_argument("--out", type=Path, required=True, help="Where to write the fixture")
    parser.add_argument("--view", action="store_true",
                        help="source is a build_view() payload (ffa export-state), "
                             "not a raw ESPN capture")
    args = parser.parse_args()

    raw = args.source.read_text(encoding="utf-8")
    if args.view:
        payload, swids = anonymize_view(json.loads(raw))
        mapping, renamed = {}, 0
        print(f"{args.source} -> {args.out}")
        print(f"  {swids} SWID(s) replaced, league/draft id and 12 label(s) aliased")
    else:
        payload, mapping, renamed = anonymize(raw)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if not args.view:
        print(f"{args.source} -> {args.out}")
        print(f"  {len(mapping)} SWID(s) replaced, {renamed} name field(s) blanked")

    # Belt and braces: re-read what we actually wrote and assert it's clean.
    written = args.out.read_text(encoding="utf-8")
    for real in mapping:
        if real in written:
            print(f"  FATAL: {real} survived into the fixture", file=sys.stderr)
            return 1
    print("  verified: no source identifier survives in the written file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
