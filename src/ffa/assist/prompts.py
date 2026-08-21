"""Where the cache breakpoint goes, and what each agent is asked.

Two system blocks, always in this order:

    [0] the prefix — league, glossary, twelve managers.  cache_control here.
    [1] this agent's instructions.                        after the breakpoint.

All five agents share a byte-identical block 0, so they share **one** cache entry
rather than five. Their instructions differ by a few hundred tokens and sit after
the breakpoint, where they are re-read uncached on every call — which is correct
and costs almost nothing. Putting the instructions *before* the breakpoint would
give each agent its own cached prefix and quintuple the write cost for no gain.

**TTL is an hour, not the five-minute default.** An auction has lulls — a
commissioner pause, an argument about a keeper, someone's dog. A five-minute
window expires during one of those and silently re-pays full price for the whole
prefix, and nothing in the output says it happened. The write premium is paid once
and is about three cents.

Instructions are written as constraints rather than encouragement. "Do not say
bid" is enforceable and checkable; "be helpful but cautious" is neither.
"""

from __future__ import annotations

import json
from typing import Any

CACHE_TTL = "1h"


ROOM = """\
A player has just been nominated. Say what is worth noticing about this
nomination, and where each live rival plausibly stops bidding.

For each rival in `rivals` who is live on this player, give a band you would not
be surprised by, and cite the evidence — a dossier line, a figure from their
history, or something visible in tonight's board. Do not list a rival you have
nothing specific to say about; a short list of grounded reads is worth more than
one line per seat.

Your band's top may never exceed that rival's `max_legal_bid`. That number is
what remains after every roster slot they must still fill is charged the $1
minimum — it is arithmetic, not an estimate, and a band above it is describing
money that does not exist.

In `read`, synthesise. The board already states the numbers; add what they mean
together — a pattern in tonight's prices, a rival whose history contradicts what
the board suggests, a scarcity fact that changes what passing costs. Never issue
an instruction.

Set `confidence` to "thin" when the dossiers are empty or the history is short.
That is a useful answer. A confident read invented from nothing is not."""


STRATEGIST = """\
A pick has just landed. Assess the declared plan against where the draft now is.

`plan_state` was computed from the plan's own arithmetic — echo it back in
`verdict_echo` exactly. Do not re-derive it; if your reading disagrees with it,
your reading is what is wrong, and the reply will be discarded.

In `assessment`, say what has actually changed for this plan and why. In `moves`,
give at most three concrete things that would help — a position to prioritise,
money to free up, an allocation that no longer fits the board. These are options,
not orders: describe the move and its cost, and leave the choosing alone."""


NARRATOR = """\
One event from the draft feed is below, already stated plainly. Add one or two
sentences on why it matters — the pattern it belongs to, what it implies for what
is left, or how it compares with what we expected.

Do not restate the event. If `what_we_said` and `reconciled` are present, the
comparison between our earlier read and the outcome is usually the most
interesting thing available, and the arithmetic of it has already been done for
you — use the numbers as given.

Set `matters_to_me` only when it bears on our roster or our plan specifically."""


ANALYST = """\
Answer the question using the board below. Be direct and brief.

Every number in the payload is already computed — quote it, do not recalculate
it, and never contradict it. If the answer depends on something not in the
payload, say what is missing rather than estimating around it.

You may discuss what different choices would cost. You may not tell the user what
to do."""


GRADER = """\
The draft is over. Grade it.

Judge the roster that was actually assembled against the plan that was declared
and the market the room produced. Then score the assistant's own calls: `reads`
holds every read it made, each with the outcome already joined to it. Where a
read was wrong, say so plainly — that is the point of the section.

This is the one moment a verdict is the right answer. Nothing can be influenced
now, so say what you actually think."""


BY_AGENT: dict[str, str] = {
    "room": ROOM,
    "strategist": STRATEGIST,
    "narrator": NARRATOR,
    "analyst": ANALYST,
    "grader": GRADER,
}


def system_blocks(prefix: str, agent: str) -> list[dict[str, Any]]:
    """The two system blocks, with the breakpoint after the shared half."""
    instructions = BY_AGENT.get(agent)
    if instructions is None:
        raise KeyError(f"no instructions for agent {agent!r}")
    return [
        {
            "type": "text",
            "text": prefix,
            "cache_control": {"type": "ephemeral", "ttl": CACHE_TTL},
        },
        {"type": "text", "text": instructions},
    ]


def user_text(payload: dict[str, Any]) -> str:
    """The volatile half, as JSON.

    Structured rather than prose, unlike the prefix: this changes on every call,
    is read once, and is full of numbers whose exact association matters. Sorted
    keys so a diff between two calls is readable when something looks wrong —
    this half is never cached, so stability costs nothing here and buys
    debuggability.
    """
    return json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False)
