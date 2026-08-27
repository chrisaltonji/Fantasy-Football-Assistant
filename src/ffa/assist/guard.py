"""Mechanical enforcement of the two promises the prompt cannot keep.

Everything else in this codebase makes its guarantees structurally: undo is a
replay filter, the dashboard cannot write because it never opens a store, the
nomination readout cannot name the wrong seat because it refuses to guess an
order. The inference layer breaks that pattern — its output is generated text,
and a system prompt is a request, not a constraint.

So the two properties that actually matter are checked here, after parsing, on
the main thread:

**It never issues a verdict.** "Bid up to $40" sitting beside `max_advisable_bid`
would be read as the same kind of number, and the whole engine has refused that
blur everywhere else. Rejection is anchored rather than keyword-based, because
the naive version fires on "a pass-catching back" and "the room passed on three
tight ends" — both perfectly good sentences that a keyword list would eat. The
false-positive tests are the important half of `test_assist_guard.py`.

**It never contradicts arithmetic that shipped in the same request.** An estimate
above a rival's `max_legal_bid` is not a bold read, it is a claim about money that
does not exist. Those are dropped and logged — never rewritten, because a
corrected estimate is a fabrication wearing the model's byline.

Both return what survived plus what did not, and the caller records the
violations. A rejection is a prompt bug, and rehearsal is when you want to see it.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

# Sentence-initial second-person instructions. Anchored to a sentence boundary so
# the verb has to be the thing being said, not a word appearing in a description.
_VERDICT_OPENERS = re.compile(
    r"(?:^|(?<=[.!?]\s)|(?<=[.!?]\s\s))\s*"
    r"(bid|pass|buy|take|grab|chase|stretch|walk away|let him go|"
    r"don't|do not|avoid|skip)\b",
    re.IGNORECASE,
)

# Phrases that are a recommendation however they are placed. Kept short and
# specific; every entry here is one that cannot be innocent.
_VERDICT_PHRASES = (
    "you should bid",
    "you should pass",
    "you should take",
    "i recommend",
    "my recommendation",
    "i'd bid",
    "i would bid",
    "i'd pass",
    "i would pass",
    "recommend bidding",
    "recommend passing",
    "go to $",
    "go up to $",
)


def reject_verdict(text: str) -> str | None:
    """The offending fragment if this reads as an instruction, else `None`.

    Two independent tests, because they catch different failures: an opener
    catches "Pass on him — the room is thin", a phrase catches "…, so I'd bid up
    to $40" buried mid-sentence.
    """
    if not text:
        return None

    lowered = text.lower()
    for phrase in _VERDICT_PHRASES:
        if phrase in lowered:
            return phrase

    match = _VERDICT_OPENERS.search(text)
    if match:
        return match.group(1)
    return None


def clamp_rivals(
    rivals: Iterable[dict[str, Any]],
    threats: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop estimates that arithmetic already rules out. Never rewrite one.

    Three ways an estimate can be impossible rather than merely wrong:

    - it names a team that is not in this player's threat list at all
    - its band is inverted, `lo` above `hi`
    - its top is above what that team could legally spend

    The third is the one that matters. `max_legal_bid` is not an opinion about a
    rival; it is what is left after every other roster slot is charged the $1
    minimum. An estimate above it describes money that cannot be bid, and
    presenting it next to the real ceiling would make both look like guesses.
    """
    ceilings = {
        int(t["team_id"]): int(t.get("max_legal_bid", 0) or 0)
        for t in threats if "team_id" in t
    }

    kept: list[dict[str, Any]] = []
    dropped: list[str] = []

    for rival in rivals:
        try:
            team_id = int(rival["team_id"])
            lo = int(rival["lo"])
            hi = int(rival["hi"])
        except (KeyError, TypeError, ValueError):
            dropped.append(f"unreadable estimate {rival!r}")
            continue

        if team_id not in ceilings:
            dropped.append(f"team {team_id} is not a threat on this player")
            continue
        if lo > hi:
            dropped.append(f"team {team_id}: lo ${lo} above hi ${hi}")
            continue
        if hi > ceilings[team_id]:
            dropped.append(
                f"team {team_id}: ${hi} is above their ${ceilings[team_id]} ceiling"
            )
            continue

        kept.append(rival)

    return kept, dropped


def verdict_matches(echoed: str, deterministic: str | None) -> bool:
    """Does the Strategist agree with the arithmetic about the plan's state?

    The three-state verdict is computed, not judged — `advice/strategy.py` decides
    it from `shortfall` and the bench reserve. The Strategist is asked to echo it
    back purely so this check exists. A model that disagrees with the arithmetic
    about the one thing the arithmetic decides has lost the thread, and its prose
    is not worth reading either.
    """
    if deterministic is None:
        return True                     # nothing to disagree with
    return (echoed or "").strip().lower() == deterministic.strip().lower()


def check_room(parsed: dict[str, Any], threats: Sequence[dict[str, Any]]
               ) -> tuple[dict[str, Any], list[str]]:
    """Everything the Room's reply has to survive. Returns (cleaned, violations)."""
    violations: list[str] = []
    cleaned = dict(parsed)

    offending = reject_verdict(str(parsed.get("read", "")))
    if offending:
        violations.append(f"read contains a verdict: {offending!r}")
        cleaned["read"] = ""

    watch = []
    for line in parsed.get("watch_for") or []:
        found = reject_verdict(str(line))
        if found:
            violations.append(f"watch_for contains a verdict: {found!r}")
            continue
        watch.append(line)
    cleaned["watch_for"] = watch

    kept, dropped = clamp_rivals(parsed.get("rivals") or [], threats)
    cleaned["rivals"] = kept
    violations.extend(dropped)

    return cleaned, violations


def check_room_tick(parsed: dict[str, Any], threats: Sequence[dict[str, Any]]
                    ) -> tuple[dict[str, Any], list[str]]:
    """The same two rules as `check_room`, over the tick's smaller reply.

    Not folded into `check_room` with a flag: the tick's `note` is a different
    field from the Room's `read`, it has no `watch_for`, and its `changed` flag
    has to be cleared when the sentence behind it is stripped. A shared function
    with two shapes would be the kind of branch that quietly checks the wrong key.

    **`changed` is forced false when nothing survives.** Otherwise a reply whose
    only content was rejected still says something changed, and `render_tick`
    would print an empty line under a live auction — which reads like the
    assistant losing its train of thought rather than like it saying nothing.
    """
    violations: list[str] = []
    cleaned = dict(parsed)

    offending = reject_verdict(str(parsed.get("note", "")))
    if offending:
        violations.append(f"note contains a verdict: {offending!r}")
        cleaned["note"] = ""

    kept, dropped = clamp_rivals(parsed.get("rivals") or [], threats)
    cleaned["rivals"] = kept
    violations.extend(dropped)

    if not (cleaned.get("note") or "").strip() and not kept:
        cleaned["changed"] = False

    return cleaned, violations


def check_strategist(parsed: dict[str, Any], plan_state: str | None
                     ) -> tuple[dict[str, Any], list[str]]:
    """Everything the Strategist's reply has to survive.

    Unlike the Room, a failed check here rejects the **whole** reply rather than
    blanking a field. `verdict_matches` is not a style rule — it asks whether the
    model agrees with the arithmetic about the one thing the arithmetic decides.
    A reply that disagrees has lost the thread, and its prose is not worth reading
    either. Returning `{}` is what makes `runner._call` record it as `rejected`.

    The digest gets the same verdict check as everything else, and it matters more
    here than anywhere: it is the one string that will be read by every other
    agent, so an instruction smuggled into it would launder itself into the tick's
    payload and out onto the screen under a different byline.
    """
    if not verdict_matches(str(parsed.get("verdict_echo", "")), plan_state):
        return {}, [
            f"verdict_echo {parsed.get('verdict_echo')!r} disagrees with the "
            f"computed plan state {plan_state!r}"
        ]

    violations: list[str] = []
    cleaned = dict(parsed)

    for field in ("assessment", "digest"):
        offending = reject_verdict(str(parsed.get(field, "")))
        if offending:
            violations.append(f"{field} contains a verdict: {offending!r}")
            cleaned[field] = ""

    # A move is a described option with a cost — "free up $12 by passing on a
    # second tight end" is the shape that is wanted, and it necessarily contains
    # words the opener pattern fires on. So `what` is left alone and only `why`
    # is checked, which is where an actual instruction would be phrased at the
    # user rather than about the board.
    moves = []
    for move in parsed.get("moves") or []:
        if not isinstance(move, dict):
            violations.append(f"unreadable move {move!r}")
            continue
        found = reject_verdict(str(move.get("why", "")))
        if found:
            violations.append(f"move rationale contains a verdict: {found!r}")
            continue
        moves.append(move)
    cleaned["moves"] = moves

    return cleaned, violations


def check_narrator(parsed: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """One field, one rule.

    The Narrator writes a sentence about an event that has already happened, so
    it has the least room to do damage of any agent here — but it prints in the
    ambient feed beside deterministic lines, which is exactly the adjacency the
    no-verdict rule exists for.
    """
    violations: list[str] = []
    cleaned = dict(parsed)

    offending = reject_verdict(str(parsed.get("why", "")))
    if offending:
        violations.append(f"why contains a verdict: {offending!r}")
        cleaned["why"] = ""

    return cleaned, violations


def check_analyst(parsed: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Prose, and the one place the rule is loosened on purpose.

    The Analyst answers a direct question. "What would it cost me to chase him?"
    is a question about arithmetic, and an answer to it must be allowed to use the
    word chase — the `_VERDICT_OPENERS` pattern would eat half of every legitimate
    reply here.

    So only the unambiguous phrases are checked, the ones that cannot be innocent
    in any context: "you should bid", "I recommend", "go up to $". Those are still
    refused, because the standing promise is that this tool never tells you what
    to do, and a question does not suspend it.
    """
    text = str(parsed.get("text", ""))
    lowered = text.lower()

    for phrase in _VERDICT_PHRASES:
        if phrase in lowered:
            # The whole reply, not the phrase: unlike the structured agents there
            # is no field to blank here, and half an answer with a hole in it is
            # worse than the plain sentence saying it was refused.
            return {}, [f"answer contains a verdict: {phrase!r}"]

    return dict(parsed), []

