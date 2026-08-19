#!/usr/bin/env python3
"""Turn a raw probe capture into a committable fixture.

`espn_fantasy.client.scrub` removes *your* credentials. It does not touch the
other managers' member SWIDs, which ride along in `mDraftDetail` picks as
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

    `<redacted>` is included deliberately. `client.scrub` has already
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

    # ONE pass, via a callback — not a chain of `str.replace`.
    #
    # A sequential chain re-reads text it has already written: replace A -> B,
    # and a later replace of B -> C silently rewrites the value that stood in
    # for A. That needs the synthetic namespace to overlap the source one,
    # which never happens on a real capture (ESPN's SWIDs are random UUIDs)
    # and happens immediately when re-running this on a committed fixture.
    # A single pass cannot substitute its own output.
    text = SWID_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)
    if "<redacted>" in mapping:
        text = text.replace("<redacted>", mapping["<redacted>"])

    leftover = [s for s in SWID_RE.findall(text) if s not in mapping.values()]
    if leftover:
        raise SystemExit(f"refusing to write: {len(leftover)} SWID(s) survived replacement")

    counter = {"n": 0}
    payload = blank_names(json.loads(text), counter)
    return payload, mapping, counter["n"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="Raw capture from tools/espn_probe.py")
    parser.add_argument("--out", type=Path, required=True, help="Where to write the fixture")
    args = parser.parse_args()

    raw = args.source.read_text(encoding="utf-8")
    payload, mapping, renamed = anonymize(raw)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"{args.source} -> {args.out}")
    print(f"  {len(mapping)} SWID(s) replaced, {renamed} name field(s) blanked")

    # Belt and braces: re-read what we actually wrote and assert it's clean.
    #
    # Ids that are *also* replacement values are skipped. Re-running this on an
    # already-anonymized file draws its synthetic ids from the same namespace
    # it assigns from, so a value can legitimately appear in the output as
    # somebody else's replacement. Flagging that reports a leak that isn't one.
    written = args.out.read_text(encoding="utf-8")
    fakes = set(mapping.values())
    for real in mapping:
        if real not in fakes and real in written:
            print(f"  FATAL: {real} survived into the fixture", file=sys.stderr)
            return 1
    print("  verified: no source identifier survives in the written file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
