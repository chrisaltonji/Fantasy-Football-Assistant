"""The Strategist: the declared plan, against where the draft actually is.

The only agent whose subject is us. Everything else here reads the room; this
reads our own roster and the plan we wrote down before the draft, and says what
the last pick did to the distance between them.

**It never re-derives the verdict.** `advice/strategy.py` computes the plan state
from shortfall and the bench reserve — three words, `on track` / `at risk` /
`time to move` — and this agent is asked to echo it back so `check_strategist`
can compare. That is not a formality. A model that disagrees with the arithmetic
about the one thing the arithmetic decides has lost the thread, and the whole
reply is rejected rather than half-shown: its prose is not worth reading either.

**It also writes the digest.** The running state of the draft that every other
agent reads sits in this reply, and it is here rather than anywhere else for two
reasons. It fires on sales — roughly twenty-five times a night, not every five
seconds — so it is cheap to keep current. And summarising where the night stands
is already what it is doing; the digest is that work addressed to the other
agents instead of to us.

One writer, low frequency, everyone reads. Same shape as the rest of the codebase.
"""

from __future__ import annotations

from typing import Any

from ffa.assist.guard import check_strategist
from ffa.assist.projection import strategist_payload
from ffa.assist.schemas import STRATEGIST_SCHEMA

AGENT = "strategist"


def moment_key(player_key: str, event_id: int) -> str:
    """Keyed on the sale that triggered it, not on a plan revision number.

    The plan does not have versions — it is declared once. What changes is the
    board, so the join to truth is the event that changed it.
    """
    return f"{AGENT}:{player_key or '-'}:{event_id}"


def digest_of(record: Any) -> str:
    """The running digest out of a stored Strategist read, or "".

    Takes a `ReadRecord` (or `None`) rather than a payload so every caller does
    the same `.latest("strategist")` lookup and gets the same answer, including
    the empty one. Before the first sale there is no digest, and that is a normal
    state rather than a missing value to work around.
    """
    if record is None:
        return ""
    return str((getattr(record, "payload", None) or {}).get("digest", "") or "")


def render(parsed: dict[str, Any]) -> str:
    """The assessment, as it appears on the terminal.

    The verdict is deliberately **not** reprinted. It was computed by
    `advice/strategy.py` and the deterministic surface has already shown it;
    repeating it here under the `[plan]` header would put the same word on screen
    twice in two different voices, and the second one would look like a second
    opinion about a thing that is not a matter of opinion.

    The digest is not printed either. It is written for the other agents, not for
    the person drafting, and showing it would be a summary of a draft they are
    currently watching.
    """
    lines: list[str] = []

    assessment = (parsed.get("assessment") or "").strip()
    if assessment:
        lines.append(f"[plan] {assessment}")

    for move in parsed.get("moves") or []:
        if not isinstance(move, dict):
            continue
        what = (move.get("what") or "").strip()
        why = (move.get("why") or "").strip()
        if not what:
            continue
        lines.append(f"       - {what}")
        if why:
            lines.append(f"         {why}")

    at_risk = [p for p in (parsed.get("positions_at_risk") or []) if str(p).strip()]
    if at_risk:
        lines.append(f"       at risk: {', '.join(at_risk)}")

    return "\n".join(lines)


def build(view: dict[str, Any], *, plan_state: str | None = None,
          sale: dict[str, Any] | None = None,
          reconciled: dict[str, Any] | None = None,
          digest: str = "") -> dict[str, Any]:
    """Everything `AssistRunner.submit` needs for one landed pick.

    `plan_state` arrives from `advice/strategy.py` rather than being read out of
    the view here, so there is one computation of it and the guard checks the
    reply against the same value the prompt was given. Passing the view and
    letting each side dig the verdict out separately is how the two quietly come
    to disagree.
    """
    sale = sale or {}
    player_key = sale.get("player_key") or sale.get("key") or ""

    return {
        "agent": AGENT,
        "payload": strategist_payload(
            view, sale=sale, reconciled=reconciled, plan_state=plan_state,
            digest=digest,
        ),
        "player_key": player_key,
        "schema": STRATEGIST_SCHEMA,
        "check": lambda parsed: check_strategist(parsed, plan_state),
        "show": render,
    }
