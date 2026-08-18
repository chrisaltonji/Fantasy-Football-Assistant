"""A fill-in questionnaire, for doing the interview away from a terminal.

Twelve managers is twenty to thirty minutes of recall, and most of that recall
happens somewhere other than at a prompt — on the couch, in a group chat,
arguing about last year's draft. So the same `QUESTIONS` table that drives
`ffa dossier interview` also renders a document you can fill in at leisure and
type back in one pass.

Markdown rather than HTML by the project's own rule: this is machine-adjacent,
frequently edited, long-lived, and needs no charts.
"""

from __future__ import annotations

from typing import Mapping

from ffa.dossier.schema import QUESTIONS, OwnerDossier, render_answer
from ffa.dossier.store import DossierBook


def seat_heading(
    team_id: int,
    labels: Mapping[int, str],
    team_names: Mapping[int, str],
    dossier: "OwnerDossier | None" = None,
    real_names: Mapping[int, str] | None = None,
) -> str:
    """How one seat is announced to a person: id, team name, username.

    All three, because none of them alone is enough. The team name is what you
    actually recognise — "Fighting Finkelsteins", not `macurl1392` — but it is
    also the one thing here that changes mid-season, so it can never stand on
    its own. The id is stable and meaningless; the username is stable and
    unrecognisable. Printing the set is the only version somebody can act on.

    Note this is display only. `label` still resolves to the manager nickname
    and never to a team name, and the interchange is still keyed by id —
    nothing downstream may key off what this returns.
    """
    username = labels.get(team_id) or (dossier.label if dossier else "") or ""
    # A dossier answer wins over ESPN's record: you know what people are
    # actually called better than their account does.
    real_name = (dossier.real_name if dossier else "") or (real_names or {}).get(
        team_id, ""
    )
    team_name = team_names.get(team_id, "")

    who = real_name or team_name or username or f"team{team_id}"
    trailing = [p for p in (team_name if real_name else "", username) if p and p != who]
    suffix = f"  ({', '.join(trailing)})" if trailing else ""
    return f"team {team_id} — {who}{suffix}"


HEADER = """# Owner dossiers

One page per manager. Fill in what you know and leave the rest blank — a
half-filled dossier is the normal state and is worth far more than none.

**Why this exists.** Everything the tool does today is *capacity*: can this
rival afford him, do they have a slot for him. That is arithmetic and it is
finished. This is the other half — *intent*. Will they actually bid, how high,
and on whom. It cannot be computed, only observed, and you are the only person
who has observed it.

**Answer format.** For the multiple-choice questions, write one of the keys
shown. For positions, list them (`RB WR`). For NFL teams, use whatever you call
them. For the free-text ones, write a sentence.

Type it back in with:

```
ffa dossier interview --team 3
```

or edit `data/dossiers.json` directly — it is plain JSON and meant to be opened.

---

## The questions

"""


def render_form(
    book: DossierBook,
    *,
    seats: Mapping[int, str],
    labels: Mapping[int, str] | None = None,
    team_names: Mapping[int, str] | None = None,
    real_names: Mapping[int, str] | None = None,
) -> str:
    """One page per seat, pre-filled with whatever is already known."""
    labels = labels or {}
    team_names = team_names or {}
    real_names = real_names or {}
    out = [HEADER]

    for question in QUESTIONS:
        out.append(f"**{question.prompt}**  \n_{question.why}_\n")
        if question.choices:
            for choice in question.choices:
                out.append(f"- `{choice.key}` — {choice.means}")
            out.append("")
    out.append("---\n")

    for team_id in sorted(seats):
        owner_id = seats[team_id]
        dossier = book.for_owner(owner_id) or OwnerDossier(owner_id=owner_id)
        out.append(
            f"## {seat_heading(team_id, labels, team_names, dossier, real_names)}\n"
        )
        for question in QUESTIONS:
            current = render_answer(question, getattr(dossier, question.field, None))
            out.append(f"- **{question.field}**: {current}")
        out.append("")

    return "\n".join(out).rstrip() + "\n"
