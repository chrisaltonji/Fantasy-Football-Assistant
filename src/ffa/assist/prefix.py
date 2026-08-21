"""The half of every prompt that never changes, so it can be cached once.

Five thousand tokens of league shape, vocabulary and twelve managers' histories
go into every agent call. Paid fresh on all ~180 nominations that is most of the
invoice; cached it is a tenth of the input rate, and the difference is roughly
$20 a draft.

**Built from the loaded sources, never from `build_view()`.** That payload stamps
`generated_at` on every call, and prompt caching is a byte-exact prefix match — one
timestamp in the cached half and the hit rate silently goes to zero while
everything still appears to work. `test_assist_prefix.py` builds it twice and
asserts the bytes are identical, because this is the failure that costs money
rather than raising.

**The glossary is the highest-leverage section in the system.** Without it a model
reasons from the names: it will treat `max_legal_bid` as a suggestion, miss that a
`+` means a floor rather than an estimate, and re-derive `is_live` incorrectly. A
few hundred cached tokens spent stating what the numbers mean buys correctness on
every call that follows, at a tenth of a cent.

Written as prose rather than JSON on purpose. This half is read once and reused;
prose is what the reader is good at, and the volatile half that follows the
breakpoint is where structure earns its keep.
"""

from __future__ import annotations

from typing import Any

# Below roughly this many tokens the API caches nothing at all, and says nothing
# about it. The fixed half — contract plus glossary — is about 700, so a prefix
# only clears the floor once the managers are in it. A league with no dossiers
# and no history can therefore fail to cache while looking completely healthy,
# which is exactly the kind of silence `cache_hit_rate` exists to break.
MIN_CACHEABLE_TOKENS = 1024

CONTRACT = """\
You are reading a live fantasy football auction draft and adding judgement to
numbers that have already been computed.

Everything you are shown below the line is arithmetic over what has actually
happened. It is not an estimate and it is not negotiable:

- Never recompute a number you have been given. If a ceiling says $58, it is $58.
- Never contradict one. An estimate above a rival's legal ceiling is not a bold
  read, it is a claim about money that does not exist.
- Never tell the user what to do. You do not say bid, pass, chase, avoid, take or
  walk away. You say what is worth noticing and let them decide. This is the one
  rule that is checked mechanically, and replies that break it are discarded.

Where the evidence is thin, say so. An empty dossier means you know nothing about
that person, and inventing a personality from a name is worse than answering
"thin"."""


GLOSSARY = """\
What the numbers mean. These definitions are exact; do not infer others.

- remaining — dollars a team has left. A trailing "+" (or remaining_is_floor)
  means at least one of their prices is unknown and was charged the $1 minimum,
  so the real figure is *at most* this. It over-states their ammunition on
  purpose: never be surprised by a bid they could make.
- max_legal_bid — the hard ceiling. Every other roster slot they must still fill
  is charged $1, so that money is unavailable for this player. Nobody can bid
  above their own max_legal_bid, ever.
- safe_legal_bid — our ceiling restated with our own unpriced picks charged at
  sheet value instead of $1. Lower than max_legal_bid and the honest one to plan
  against.
- max_advisable_bid — what the player is worth at tonight's prices given our
  roster needs. A judgement about value, not a limit. Capped by safe_legal_bid.
- reference_value / inflated_value — the sheet price, and that price restated at
  the room's current inflation ratio.
- is_live — a rival who can afford him *and* has an unfilled starting slot he
  could occupy. A team with $90 and no open running-back slot is not a threat on
  a running back, however rich they look. This is the single most important flag
  on the board.
- starter_gaps — unfilled *starting* slots. open_slots_by_pos includes the bench
  and is almost always non-zero, so it is not evidence of need.
- shortfall (plan) — dollars the declared plan still wants beyond what we hold.
  Positive means the plan is no longer affordable.

Provenance, which you must keep straight:

- Tonight's arithmetic — budgets, ceilings, scarcity, threats. Computed, exact.
- history — the same kind of arithmetic over *previous* drafts. A fact about what
  somebody did before and a bet about tonight. The season count travels with it;
  weigh it accordingly.
- dossier — recorded testimony. Somebody's opinion about a person, written down
  before the draft. Not measured, not computed, and sometimes wrong."""


def _league_section(league: Any) -> str:
    roster = getattr(league, "roster", {}) or {}
    slots = ", ".join(
        f"{slot.value}×{n}" for slot, n in sorted(roster.items(), key=lambda kv: kv[0].value)
    )
    return (
        f"League: {getattr(league, 'team_count', 0)} teams, "
        f"${getattr(league, 'budget', 0)} each, "
        f"{getattr(league, 'draftable_slots', 0)} draftable slots.\n"
        f"Roster: {slots}\n"
        f"Scoring: {getattr(league, 'scoring_type', '')}"
    )


def _manager_section(dossiers: Any, precedent: Any, seats: dict[int, str] | None,
                     team_ids: tuple[int, ...], labels: dict[int, str]) -> str:
    """One block per seat: who they are, what is recorded, what the record shows.

    Sorted by team id so the bytes are stable — a dict iteration order that
    varied would silently halve the cache hit rate.
    """
    blocks: list[str] = []
    for team_id in sorted(team_ids):
        lines = [f"[{team_id}] {labels.get(team_id, f'team{team_id}')}"]

        dossier = None
        if dossiers is not None:
            try:
                dossier = dossiers.for_team(team_id)
            except Exception:      # noqa: BLE001 - a bad book must not stop a draft
                dossier = None
        if dossier is not None and not getattr(dossier, "is_empty", True):
            from ffa.dossier.schema import QUESTIONS, render_answer

            for question in QUESTIONS:
                # `render_answer(question, value)` — the question first, so
                # enums and tuples come back as the strings the interview wrote.
                value = render_answer(question, getattr(dossier, question.field, None))
                if value and value != "-":
                    lines.append(f"  {question.field}: {value}")
        else:
            lines.append("  dossier: nothing recorded — say so rather than guessing")

        record = None
        if precedent is not None:
            try:
                record = precedent.for_team(team_id, seats or {})
            except Exception:      # noqa: BLE001
                record = None
        if record is not None:
            lines.append(
                f"  history: {record.season_count} season(s)"
                + (" (thin)" if record.is_thin else "")
                + f", spend_shape {record.spend_shape}, pace {record.pace}"
                + f", nomination_premium {record.nomination_premium:.2f}"
                + f", te_share {record.te_share:.2f}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_prefix(*, league: Any, dossiers: Any = None, precedent: Any = None,
                 seats: dict[int, str] | None = None,
                 labels: dict[int, str] | None = None,
                 reference: Any = None) -> str:
    """The cached half. Deterministic — same inputs, same bytes, always."""
    team_ids = tuple(getattr(league, "effective_team_ids", ()) or ())
    labels = labels or dict(getattr(league, "managers", {}) or {})

    reference_line = ""
    if reference is not None:
        count = len(reference) if hasattr(reference, "__len__") else 0
        sample = getattr(reference, "is_sample", False)
        reference_line = (
            f"\nAuction values: {count} players"
            + (" — SAMPLE DATA, these are invented" if sample else "")
        )

    return "\n\n".join([
        CONTRACT,
        "---",
        _league_section(league) + reference_line,
        GLOSSARY,
        "The managers in this room:",
        _manager_section(dossiers, precedent, seats, team_ids, labels),
    ])


def likely_cacheable(prefix: str) -> bool:
    """Is this long enough for the API to cache it at all?

    A rough character-count proxy, because a real token count needs the network
    and this is checked at startup. Wrong by a few percent and still worth having:
    the failure it catches is silent, and the fix — record some dossiers — is not
    something you want to discover from an invoice.
    """
    return (len(prefix) // 4) >= MIN_CACHEABLE_TOKENS
