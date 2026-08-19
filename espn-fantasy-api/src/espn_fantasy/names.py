"""Folding names into stable keys.

Two different jobs, deliberately not shared: player names come off ESPN's
board and out of its payloads and need to compare equal across both, and team
names are the only join available between the draft room's DOM and the REST
views.
"""

from __future__ import annotations

import re
import unicodedata

_STRIP = re.compile(r"[^a-z0-9 ]")
_SPACES = re.compile(r"\s+")


def normalize_player_key(raw: str) -> str:
    """`"Ja'Marr Chase"` -> `"jamarr-chase"`.

    Deliberately does *not* strip generational suffixes (Jr, III). Those are
    real distinguishing information — Odell Beckham Jr. is not Odell Beckham —
    and this key's job is stability, not fuzzy matching.
    """
    folded = unicodedata.normalize("NFKD", raw or "")
    ascii_only = folded.encode("ascii", "ignore").decode("ascii").lower()
    cleaned = _SPACES.sub(" ", _STRIP.sub("", ascii_only)).strip()
    return cleaned.replace(" ", "-")


def normalize_team_name(raw: str | None) -> str:
    """Fold a team name for matching. Case and punctuation are noise.

    Unlike the player key this keeps spaces, because team names are matched
    whole rather than used as identifiers.
    """
    text = "".join(c.lower() for c in (raw or "") if c.isalnum() or c.isspace())
    return " ".join(text.split())
