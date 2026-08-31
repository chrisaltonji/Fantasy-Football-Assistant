"""The Analyst: a direct answer to a direct question.

The only agent a person invokes on purpose, and the only one that answers in
prose. It has no schema — `schemas.BY_AGENT` maps it to `{}` deliberately, because
forcing a shape on an open question makes a model answer the shape instead of the
question.

**It is the one place the whole board is the right payload.** `analyst_payload`
hands over `build_view()` entire, ~15,200 tokens and about 15c a call. That is
defensible here and nowhere else: the question could be about any corner of the
draft, nothing is waiting on the answer, and a person is sitting there having
asked for it. Every other agent fires on a clock and gets a projection.

**The guard is looser here, on purpose.** "What would it cost me to chase him?" is
a question about arithmetic, and an answer to it has to be allowed to say chase —
the sentence-opener pattern that protects the Room would eat half of every
legitimate reply. So `check_analyst` checks only the phrases that cannot be
innocent in any context. The standing promise still holds: this tool does not tell
you what to do, and asking it to does not suspend that.
"""

from __future__ import annotations

from typing import Any

from ffa.assist.guard import check_analyst
from ffa.assist.projection import analyst_payload

AGENT = "analyst"

# How much of a question to put in the moment key. Enough to tell two questions
# apart when reading the sidecar back; short enough that the key stays a key.
KEY_CHARS = 40


def moment_key(question: str, event_id: int) -> str:
    """Keyed on the question, because there is no player and no trigger event.

    The event id still travels: it says what the board looked like when the
    question was asked, which is the only thing that makes an answer readable
    again tomorrow. The question is squashed to lowercase words so the same
    question typed twice with different spacing files as the same moment.
    """
    words = "-".join(str(question).lower().split())[:KEY_CHARS]
    return f"{AGENT}:{words or '-'}:{event_id}"


def render(parsed: dict[str, Any]) -> str:
    """The answer, marked as judgement.

    `[answer]` on the first line only, with the rest indented under it — the
    reply is prose and may run several lines, and a marker that appeared once at
    the top of a long block would leave the tail looking like tool output. The
    indent is what carries the attribution down the page.
    """
    text = (parsed.get("text") or "").strip()
    if not text:
        return ""

    lines = text.splitlines()
    out = [f"[answer] {lines[0]}"]
    out.extend(f"         {line}" for line in lines[1:])
    return "\n".join(out)


def build(view: dict[str, Any], *, question: str,
          prior_reads: list | None = None,
          digest: str = "") -> dict[str, Any]:
    """Everything `AssistRunner.submit` needs for one question.

    No `schema` key: `submit` passes `schema=None` through to `client.complete`,
    which is what selects the prose path. That is the same mechanism
    `schemas.schema_for` uses, expressed here as an absence rather than a flag.
    """
    return {
        "agent": AGENT,
        "payload": analyst_payload(
            view, question=question, prior_reads=prior_reads, digest=digest,
        ),
        # No player. The question may be about eleven of them or none.
        "player_key": "",
        "schema": None,
        "check": check_analyst,
        "show": render,
    }
