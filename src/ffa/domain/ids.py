"""Stable identity for players, teams, and entity addresses.

The rule that makes journals durable: **a player's key derives only from what
the user typed, never from external data.** No rankings file, no ESPN player
id, nothing that might be absent or might change. A journal written today
replays identically forever, with or without a PlayerBook loaded.

CP3 attaches real player ids on top of this without touching the key, by
emitting a `FieldAmended(entity="player:<key>", field="player_id", ...)` with
`INFERRED` provenance. Resolution is just another observation — uncertain, and
overridable by the user because MANUAL outranks INFERRED. That's the payoff of
routing everything through `Sourced`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_STRIP = re.compile(r"[^a-z0-9 ]")
_SPACES = re.compile(r"\s+")


def normalize_player_key(raw: str) -> str:
    """`"Ja'Marr Chase"` -> `"jamarr-chase"`, `"mahomes"` -> `"mahomes"`.

    Deliberately does *not* strip generational suffixes (Jr, III). Those are
    real distinguishing information, and the key's job is stability and
    predictability, not matching — CP3's fuzzy resolver handles matching.
    """
    folded = unicodedata.normalize("NFKD", raw or "")
    ascii_only = folded.encode("ascii", "ignore").decode("ascii").lower()
    cleaned = _SPACES.sub(" ", _STRIP.sub("", ascii_only)).strip()
    return cleaned.replace(" ", "-")


@dataclass(frozen=True)
class PlayerRef:
    """How a player is named in the journal.

    `key` is identity, `raw` is what the user actually typed (kept for display
    and for post-mortems), `player_id` is the CP3 upgrade and is absent at CP2.
    """

    key: str
    raw: str
    player_id: str | None = None

    @classmethod
    def from_raw(cls, raw: str) -> "PlayerRef":
        return cls(key=normalize_player_key(raw), raw=raw.strip())

    def to_obj(self) -> dict[str, str | None]:
        obj: dict[str, str | None] = {"key": self.key, "raw": self.raw}
        if self.player_id is not None:
            obj["player_id"] = self.player_id
        return obj

    @classmethod
    def from_obj(cls, obj: dict) -> "PlayerRef":
        return cls(key=obj["key"], raw=obj.get("raw", obj["key"]), player_id=obj.get("player_id"))


# --- entity addresses --------------------------------------------------------
#
# `FieldAmended` targets an entity, never an event id. These strings are what
# appear in the journal and in error messages, so they're also what a human
# greps for.

PLAYER = "player"
TEAM = "team"
DRAFT = "draft"


class EntityAddressError(ValueError):
    """An address that names nothing the reducer knows how to write to."""


def format_entity_address(kind: str, ident: str | int | None = None) -> str:
    return kind if ident is None else f"{kind}:{ident}"


def parse_entity_address(address: str) -> tuple[str, str | int | None]:
    """`"player:mahomes"` -> `("player", "mahomes")`; `"team:3"` -> `("team", 3)`."""
    text = (address or "").strip()
    if text == DRAFT:
        return DRAFT, None

    kind, sep, ident = text.partition(":")
    if not sep or not ident:
        raise EntityAddressError(
            f"{address!r} is not an entity address. Expected 'player:<name>', "
            "'team:<id>', or 'draft'."
        )
    if kind == PLAYER:
        return PLAYER, ident
    if kind == TEAM:
        try:
            return TEAM, int(ident)
        except ValueError:
            raise EntityAddressError(f"team address needs a numeric id, got {ident!r}") from None
    raise EntityAddressError(
        f"unknown entity kind {kind!r} in {address!r}. Valid kinds: player, team, draft."
    )
