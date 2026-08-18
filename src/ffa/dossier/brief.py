"""A briefing you paste into a chat, so the interview can be a conversation.

Thirteen questions across twelve managers is 156 prompts, and answering them at
a terminal is the wrong shape for the task. The content is recall — arguing with
yourself about what somebody did in the third round two years ago — and that
goes far better spoken than typed.

So this renders the whole interview as a prompt: the questions, the vocabulary,
the roster, and an explicit output contract. Paste it into a chat, talk, and
`ffa dossier import` takes the answers back.

**The interchange is keyed by team id, not SWID.** A SWID is opaque and easy to
garble in a transcript, and one wrong character silently files a read against
the wrong person. Team ids are short, meaningful, and already printed beside
every nickname — and `import` resolves them through the config, which is the
authority on who sits where.

The instruction that matters most is *don't guess*. An empty dossier is honest
and costs nothing; an invented one is read as observation by a layer whose whole
job is to trust it.
"""

from __future__ import annotations

from typing import Mapping

from ffa.dossier.schema import QUESTIONS, OwnerDossier, render_answer
from ffa.dossier.store import DossierBook

PREAMBLE = """# Owner dossier interview

You are helping me record what I know about the managers in my fantasy football
league, ahead of a 12-team, $200 ESPN auction draft.

## What this is for

My draft tool already computes **capacity** perfectly — whether a rival can
afford a player and whether they have a roster slot for him. That part is
arithmetic and it is finished.

What it cannot compute is **intent**: whether they will actually bid, how high,
and on whom. That can only be observed, and I am the only person who has
observed it. That is what we are collecting.

## How to run this

Interview me one manager at a time. Ask conversationally — follow up, jog my
memory, let me ramble — rather than reading the questions out like a form. Move
on when I have nothing more for that person.

**Three rules, in order of importance:**

1. **Never invent an answer, and never smooth a vague one into a confident
   one.** Leave it out. An empty field is honest and costs nothing; a plausible
   guess gets read as observation by a layer whose entire job is to trust it.
   If I say "I think maybe he goes early sometimes?", that is not an answer —
   ask, or leave it blank.
2. **Every field is optional.** A half-filled dossier is the normal state and is
   far better than none. Do not push for completeness.
3. **Free text is worth more than the categories.** `tells` and `notes` are
   where the real edge lives, because they hold the things no value sheet
   knows. Spend time there.

If I want to stop early, output what we have so far.

## The questions

"""

OUTPUT_CONTRACT = """
## What to output

When we are done — or whenever I ask — output **one fenced `json` block** and
nothing else inside it, keyed by **team id** (the numbers in the roster below):

```json
{
  "3": {
    "real_name": "Dave",
    "seasons_in_league": 8,
    "skill": "sharp",
    "spend_shape": "stars_and_scrubs",
    "pace": "front_loads",
    "chases": "often",
    "nomination_style": "enforcer",
    "overpays_at": ["QB"],
    "ignores": ["TE"],
    "homer_teams": ["KC"],
    "tells": "goes quiet the moment he is out of money",
    "notes": "will bid up team 11 out of spite, every year"
  }
}
```

Rules for the block:

- **Omit any field I did not actually answer.** Do not write `null`, `""`, or
  `"unknown"` — an absent key means "not asked", and that distinction is load
  bearing.
- Omit any manager we did not get to.
- For the multiple-choice fields, use exactly one of the keys listed above.
- `overpays_at` / `ignores` are position codes (`QB RB WR TE K DST`).
- `homer_teams` / `avoids_teams` are NFL teams — whatever I call them is fine.
- `seasons_in_league` is a whole number.

I will save that block and run `ffa dossier import` on it.

## The roster

These are the twelve seats. The team id is what the JSON above is keyed by.

"""


def render_brief(
    book: DossierBook,
    *,
    seats: Mapping[int, str],
    labels: Mapping[int, str] | None = None,
    team_names: Mapping[int, str] | None = None,
    league_name: str = "",
) -> str:
    """The whole interview as one pasteable prompt."""
    labels = labels or {}
    team_names = team_names or {}
    out = [PREAMBLE]

    for question in QUESTIONS:
        out.append(f"**`{question.field}`** — {question.prompt}")
        out.append(f"_Why it matters: {question.why}_")
        if question.choices:
            for choice in question.choices:
                out.append(f"- `{choice.key}` — {choice.means}")
        out.append("")

    out.append(OUTPUT_CONTRACT.rstrip())
    out.append("")
    if league_name:
        out.append(f"League: **{league_name}**\n")

    out.append("| team id | team name | ESPN username | already recorded |")
    out.append("|---|---|---|---|")
    for team_id in sorted(seats):
        dossier = book.for_team(team_id) or OwnerDossier(owner_id=seats[team_id])
        username = labels.get(team_id) or dossier.label or "-"
        team_name = team_names.get(team_id) or "-"
        out.append(
            f"| {team_id} | {team_name} | {username} | {_summarize(dossier)} |"
        )

    out.append("")
    out.append(
        "Refer to people by **team name** — that is what I will recognise. The "
        "ESPN username is there so you can tell two similar teams apart, and the "
        "team id is only for keying the JSON. A team name can change mid-season, "
        "so never treat it as an identity."
    )
    out.append("")
    out.append(
        "Start with whichever manager I name. If I do not name one, start at the "
        "top of that table and work down."
    )
    return "\n".join(out) + "\n"


def _summarize(dossier: OwnerDossier) -> str:
    """What we already have, so a second pass is about the gaps."""
    if dossier.is_empty:
        return "nothing yet"
    parts = [
        f"{q.field}={render_answer(q, getattr(dossier, q.field, None))}"
        for q in QUESTIONS
        if q.is_answered(dossier)
    ]
    return "; ".join(parts)
