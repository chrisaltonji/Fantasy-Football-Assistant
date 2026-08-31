"""The Narrator: one sentence on why an event mattered.

The smallest agent here, and the one with the least room to do damage — it speaks
about something that has already happened, on a board nobody is bidding into. But
it prints into the ambient feed beside deterministic lines, which is precisely the
adjacency the no-verdict rule exists for, so it is guarded like everything else.

**Its best line is a comparison, and the arithmetic is already done.**
"He went $47, eight over what we expected" — the subtraction is
`reconcile.compare`, computed on the main thread and handed over as a fact. The
Room's earlier estimate comes out of the `ReadLog` the same way. Asking a model to
recompute a delta it could have been given is how a system starts disagreeing
with itself in prose.

**The payload is deliberately tiny.** `narrator_payload` is ~400 tokens: the one
event, the market line, the scarcity for that position, and what we said. Handing
it the board would dilute one sentence about one pick into a summary of the draft,
which is the failure mode the deterministic feed was already built to avoid.
"""

from __future__ import annotations

from typing import Any

from ffa.assist.guard import check_narrator
from ffa.assist.projection import narrator_payload
from ffa.assist.schemas import NARRATOR_SCHEMA

AGENT = "narrator"


def moment_key(player_key: str, event_id: int) -> str:
    """Keyed on the event it is narrating. One line per event, at most."""
    return f"{AGENT}:{player_key or '-'}:{event_id}"


def prior_read_of(record: Any) -> dict[str, Any]:
    """The Room read this event resolves, as a plain dict, or `{}`.

    Takes a `ReadRecord` because that is what `ReadLog.latest` returns, and that
    method already refuses to hand back anything but a successful read — passing
    on a rejected one would launder exactly the output the guard refused into a
    second agent under a new byline.
    """
    if record is None:
        return {}
    return dict(getattr(record, "payload", None) or {})


def render(parsed: dict[str, Any]) -> str:
    """One line in the feed, or nothing.

    `[note]` rather than `[read]`: this is commentary on a settled fact, not an
    estimate about an open one, and the two should not look alike on a screen
    where one of them is about to inform a bid.

    `matters_to_me` is a marker rather than a sentence of its own — the model is
    asked to set it only when the event bears on our roster or our plan
    specifically, and running that into prose would make every line about us.
    """
    why = (parsed.get("why") or "").strip()
    if not why:
        return ""

    mark = " *" if parsed.get("matters_to_me") else ""
    return f"[note]{mark} {why}"


def build(view: dict[str, Any], *, entry: dict[str, Any],
          reconciled: dict[str, Any] | None = None,
          prior_read: dict[str, Any] | None = None,
          digest: str = "") -> dict[str, Any]:
    """Everything `AssistRunner.submit` needs for one notable event.

    `entry` is a feed entry from `advice/feed.py` — already filtered for
    notability by arithmetic, so this agent is never asked whether something was
    worth mentioning, only what it meant. That split is the same one everywhere
    else in the package, and it is why there is no "is this interesting" field
    anywhere in the schema.
    """
    player_key = entry.get("player_key") or entry.get("key") or ""

    return {
        "agent": AGENT,
        "payload": narrator_payload(
            view, entry=entry, reconciled=reconciled, prior_read=prior_read,
            digest=digest,
        ),
        "player_key": player_key,
        "schema": NARRATOR_SCHEMA,
        "check": check_narrator,
        "show": render,
    }
