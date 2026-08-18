"""What we know about a manager, and the questions that get us there.

Everything else in this codebase is about **capacity** — can this rival afford
him, do they have a slot. That is arithmetic, and it is finished. A dossier is
the other half: **intent**. Will they actually bid, and how high, and on whom.

That half cannot be computed, only observed, which is why this file is a
question set rather than a model. The engine never scores a dossier. It records
what you know about twelve people and hands it to the layer whose job is
judgment; anything else would smuggle a guess into a payload the whole design
guarantees is deterministic.

**One declarative table drives everything.** `QUESTIONS` is the interview, the
printable form, the validator, and the readout. A question added here appears in
all four with no further work, and — more to the point — they cannot drift.

Design rules that are not negotiable:

- **Anchored on the owner's SWID.** Managers rename teams constantly and ESPN
  team ids shuffle between seasons; a dossier is about a *person* and outlives
  both. Nothing here keys on a name.
- **Every field is optional.** A half-filled dossier is the normal state and is
  strictly better than none. Nothing may block a draft because an answer is
  missing.
- **Unanswered and "no strong read" are different.** `None` means nobody has
  been asked; an explicit answer of `steady` is information. Collapsing them
  would make coverage meaningless and would quietly present a default as a
  finding.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import Any, Mapping

from ffa.domain.enums import Position


class DossierError(Exception):
    """A problem the user can fix. Shown without a traceback."""


# Re-exported, not reimplemented. Folding a SWID is an identity concern rather
# than a dossier one, and three modules had grown their own copy — one of which
# omitted the `.upper()`. `ffa.config.identity` is now the only definition.
from ffa.config.identity import fold_owner_id  # noqa: E402,F401


# --- the vocabulary -------------------------------------------------------------
#
# Deliberately coarse. "Sharp / solid / casual" is a read somebody can give in
# two seconds and be right about; a 1-10 rating is a number they would invent.


class Skill(str, Enum):
    SHARP = "sharp"
    SOLID = "solid"
    CASUAL = "casual"
    ERRATIC = "erratic"


class SpendShape(str, Enum):
    """How the $200 gets distributed across fifteen roster spots."""

    STARS_AND_SCRUBS = "stars_and_scrubs"
    BALANCED = "balanced"
    VALUE_HUNTER = "value_hunter"


class Pace(str, Enum):
    """When in the auction the money goes out."""

    FRONT_LOADS = "front_loads"
    STEADY = "steady"
    WAITS = "waits"


class Chases(str, Enum):
    """How often they go past a player's value to win him."""

    OFTEN = "often"
    SOMETIMES = "sometimes"
    RARELY = "rarely"


class NominationStyle(str, Enum):
    ENFORCER = "enforcer"
    TARGETS = "targets"
    BEST_AVAILABLE = "best_available"
    RANDOM = "random"


ENUMS: Mapping[str, type[Enum]] = {
    "skill": Skill,
    "spend_shape": SpendShape,
    "pace": Pace,
    "chases": Chases,
    "nomination_style": NominationStyle,
}


# --- the record ------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerDossier:
    """One manager. Keyed by SWID; everything else is optional."""

    owner_id: str

    # Display only, and recorded at the time of writing. `team_id` in particular
    # is *this season's* id — the config is the authority on it, and a stale one
    # here must never be used to attribute anything.
    team_id: int = 0
    label: str = ""
    real_name: str = ""

    seasons_in_league: int | None = None
    skill: Skill | None = None
    spend_shape: SpendShape | None = None
    pace: Pace | None = None
    chases: Chases | None = None
    nomination_style: NominationStyle | None = None

    overpays_at: tuple[Position, ...] = ()
    ignores: tuple[Position, ...] = ()
    homer_teams: tuple[str, ...] = ()
    avoids_teams: tuple[str, ...] = ()

    tells: str = ""
    notes: str = ""

    # Where any *derived* answers in this record came from. Deliberately not a
    # `Question`: it is never asked, never counted toward coverage, and never
    # overwritten by an interview. It lives here rather than in `notes` because
    # `notes` is the human catch-all — the one field explicitly for the things
    # only somebody who was in the room knows — and filling it with a generated
    # provenance string would take the most valuable field in the file and use
    # it for bookkeeping.
    derived_from: str = ""

    # ISO date. Staleness is a real signal — a read from three seasons ago is
    # worth less than one from last year, and the layer reading this should be
    # able to tell.
    updated_at: str = ""

    @property
    def answered(self) -> tuple[str, ...]:
        return tuple(q.field for q in QUESTIONS if q.is_answered(self))

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(q.field for q in QUESTIONS if not q.is_answered(self))

    @property
    def coverage(self) -> float:
        return len(self.answered) / len(QUESTIONS) if QUESTIONS else 0.0

    @property
    def is_empty(self) -> bool:
        return not self.answered


# --- the questions ---------------------------------------------------------------


@dataclass(frozen=True)
class Choice:
    key: str
    means: str


@dataclass(frozen=True)
class Question:
    """One thing to ask, and why it is worth the ten seconds.

    `why` is not decoration. An interview across twelve managers goes fast and
    shallow unless the person answering knows what the answer is *for*; and the
    inference layer reading these later needs the same context to weigh them.
    """

    field: str
    prompt: str
    why: str
    kind: str  # choice | text | int | positions | nfl_teams
    choices: tuple[Choice, ...] = ()

    def is_answered(self, dossier: "OwnerDossier") -> bool:
        value = getattr(dossier, self.field, None)
        if value is None:
            return False
        if isinstance(value, (str, tuple)):
            return bool(value)
        return True


QUESTIONS: tuple[Question, ...] = (
    Question(
        field="real_name",
        prompt="What do you actually call them?",
        why="ESPN already gives us the name on the account — 'Michael Curley'. "
            "This is the one you would use out loud, which is what a readout "
            "should say when four seconds matter. It overrides ESPN's version "
            "wherever a name is shown.",
        kind="text",
    ),
    Question(
        field="seasons_in_league",
        prompt="How many seasons have they been in this league?",
        why="A first-timer in an auction behaves differently from someone on "
            "their eighth: they misjudge the early market and run out of money.",
        kind="int",
    ),
    Question(
        field="skill",
        prompt="How good are they?",
        why="Sets how much to trust every other read. A sharp manager's odd bid "
            "usually means they know something; a casual one's usually doesn't.",
        kind="choice",
        choices=(
            Choice("sharp", "prepared, knows values, rarely wastes money"),
            Choice("solid", "competent, roughly follows the consensus"),
            Choice("casual", "shows up, drafts off gut and name recognition"),
            Choice("erratic", "unpredictable — capable of anything, either way"),
        ),
    ),
    Question(
        field="spend_shape",
        prompt="How do they spend the $200?",
        why="The single most useful thing to know. Stars-and-scrubs money is "
            "gone by pick 40 and they stop being a threat on anyone; a value "
            "hunter is dangerous all night and never at the top of the bidding.",
        kind="choice",
        choices=(
            Choice("stars_and_scrubs", "two or three big names, then $1 bodies"),
            Choice("balanced", "spreads it, ends with a roster of solid pieces"),
            Choice("value_hunter", "sits out the top, hunts the middle for value"),
        ),
    ),
    Question(
        field="pace",
        prompt="When does their money go out?",
        why="Tells you *when* their ceiling is real. A front-loader's $150 in "
            "the first half hour is genuinely spendable; the same $150 held by "
            "someone who waits will still be there at pick 100.",
        kind="choice",
        choices=(
            Choice("front_loads", "spends early and hard"),
            Choice("steady", "roughly even across the draft"),
            Choice("waits", "hoards, then buys the late market"),
        ),
    ),
    Question(
        field="chases",
        prompt="How often do they bid past a player's value to win him?",
        why="This is what turns a $40 player into a $55 player. A room with "
            "three chasers in it prices differently from one with none.",
        kind="choice",
        choices=(
            Choice("often", "gets into it and does not back down"),
            Choice("sometimes", "will overpay for players they want"),
            Choice("rarely", "disciplined, drops out at their number"),
        ),
    ),
    Question(
        field="nomination_style",
        prompt="What do they nominate?",
        why="Nomination order is known in advance, so knowing what somebody "
            "does with their turn is a forecast rather than a guess. An "
            "enforcer's nomination is a trap; a targeter's tells you who they "
            "want.",
        kind="choice",
        choices=(
            Choice("enforcer", "players they don't want, to drain other budgets"),
            Choice("targets", "players they actually want"),
            Choice("best_available", "straight down the board"),
            Choice("random", "no discernible pattern"),
        ),
    ),
    Question(
        field="overpays_at",
        prompt="Which positions do they reliably pay up for?",
        why="Position bias is stable across seasons and is the cheapest "
            "possible edge: knowing somebody always takes a QB early tells you "
            "which bidding wars to skip.",
        kind="positions",
    ),
    Question(
        field="ignores",
        prompt="Which positions do they let go cheap?",
        why="The other half. A position nobody in the room wants is where your "
            "money goes furthest.",
        kind="positions",
    ),
    Question(
        field="homer_teams",
        prompt="Which NFL teams do they overpay for?",
        why="Homer bidding is the most reliable irrationality in any league, "
            "and it is invisible to every value sheet ever made.",
        kind="nfl_teams",
    ),
    Question(
        field="avoids_teams",
        prompt="Which NFL teams will they not roster?",
        why="Same effect, opposite sign — and it means a genuinely good player "
            "goes cheap in this specific room.",
        kind="nfl_teams",
    ),
    Question(
        field="tells",
        prompt="Any tells? What do they do when they are about to bid, or done?",
        why="The things that only somebody who has drafted with them knows. "
            "This is the field most likely to be worth more than everything "
            "above it.",
        kind="text",
    ),
    Question(
        field="notes",
        prompt="Anything else worth knowing?",
        why="Rivalries, grudges, who they copy, whether they show up late. "
            "The catch-all, on purpose.",
        kind="text",
    ),
)

BY_FIELD: Mapping[str, Question] = {q.field: q for q in QUESTIONS}


# --- coercion -------------------------------------------------------------------


def parse_answer(question: Question, raw: str) -> Any:
    """Turn typed text into a stored value, or raise `DossierError` saying why."""
    text = (raw or "").strip()
    if not text:
        raise DossierError("empty answer")

    if question.kind == "text":
        return text

    if question.kind == "int":
        try:
            value = int(text)
        except ValueError:
            raise DossierError(f"{text!r} is not a whole number") from None
        if value < 0:
            raise DossierError("that cannot be negative")
        return value

    if question.kind == "choice":
        wanted = text.lower()
        keys = [c.key for c in question.choices]
        if wanted in keys:
            return ENUMS[question.field](wanted)
        # Numbered, because that is how the choices are shown.
        if wanted.isdigit() and 1 <= int(wanted) <= len(keys):
            return ENUMS[question.field](keys[int(wanted) - 1])
        matches = [k for k in keys if k.startswith(wanted)]
        if len(matches) == 1:
            return ENUMS[question.field](matches[0])
        raise DossierError(
            f"{text!r} is not one of: " + ", ".join(keys)
        )

    if question.kind == "positions":
        out: list[Position] = []
        for token in _split(text):
            try:
                position = Position.parse(token)
            except ValueError:
                raise DossierError(
                    f"{token!r} is not a position. Valid: "
                    + ", ".join(p.value for p in Position)
                ) from None
            if position not in out:
                out.append(position)
        return tuple(out)

    if question.kind == "nfl_teams":
        # Not validated against a roster of NFL teams on purpose. The point is
        # to record what you said; refusing "Niners" because the canonical
        # abbreviation is SF would cost more than the tidiness is worth.
        return tuple(dict.fromkeys(t.upper() for t in _split(text)))

    raise DossierError(f"unknown question kind {question.kind!r}")  # pragma: no cover


def _split(text: str) -> list[str]:
    return [t.strip() for t in text.replace(",", " ").split() if t.strip()]


def render_answer(question: Question, value: Any) -> str:
    """How a stored value reads back. `-` for never asked."""
    if value is None or value == () or value == "":
        return "-"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, tuple):
        return ", ".join(
            v.value if isinstance(v, Enum) else str(v) for v in value
        )
    return str(value)


# --- serialization ---------------------------------------------------------------

_FIELD_NAMES = tuple(f.name for f in fields(OwnerDossier))


def to_dict(dossier: OwnerDossier) -> dict[str, Any]:
    """Plain JSON. Unanswered fields are omitted rather than written as null.

    A file of nulls reads as "asked and refused"; an absent key reads as "not
    asked". They are different states and the file should say which.
    """
    out: dict[str, Any] = {}
    for name in _FIELD_NAMES:
        value = getattr(dossier, name)
        if value is None or value == () or value == "" or (name == "team_id" and not value):
            if name != "owner_id":
                continue
        if isinstance(value, Enum):
            out[name] = value.value
        elif isinstance(value, tuple):
            out[name] = [v.value if isinstance(v, Enum) else v for v in value]
        else:
            out[name] = value
    return out


def from_dict(data: Mapping[str, Any]) -> OwnerDossier:
    """The inverse. Tolerant: an unreadable field is dropped, never fatal.

    A dossier file is hand-editable by design — it is faster to fix a typo in
    JSON than to re-run an interview — so it will be hand-edited, and a strict
    loader would turn one bad line into a draft that will not start.
    """
    owner_id = fold_owner_id(str(data.get("owner_id") or ""))
    if not owner_id:
        raise DossierError("a dossier entry has no owner_id (the ESPN member SWID)")

    kwargs: dict[str, Any] = {"owner_id": owner_id}
    for name in _FIELD_NAMES:
        if name == "owner_id" or name not in data:
            continue
        raw = data[name]
        try:
            kwargs[name] = _coerce(name, raw)
        except (ValueError, TypeError, KeyError):
            continue
    return OwnerDossier(**kwargs)


def _coerce(name: str, raw: Any) -> Any:
    if name in ENUMS:
        return ENUMS[name](str(raw))
    if name in ("overpays_at", "ignores"):
        return tuple(Position.parse(str(v)) for v in raw)
    if name in ("homer_teams", "avoids_teams"):
        return tuple(str(v).upper() for v in raw)
    if name == "seasons_in_league":
        return int(raw)
    if name == "team_id":
        return int(raw)
    return str(raw)


def seed(owner_id: str, *, team_id: int = 0, label: str = "") -> OwnerDossier:
    """An empty dossier for someone we have not asked about yet."""
    return OwnerDossier(
        owner_id=fold_owner_id(owner_id), team_id=team_id, label=label
    )
