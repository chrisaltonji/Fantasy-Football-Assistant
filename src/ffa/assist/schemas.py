"""The shapes each agent must answer in.

Structured output via `output_config={"format": {...}}` — raw JSON Schema rather
than `messages.parse`, because that wants a Pydantic model and this codebase has
no Pydantic in it and a house style of hand-written dataclasses. Adding an ORM's
worth of dependency to describe five objects would be the wrong trade.

**Field names are part of the guarantee, not decoration.** The Room answers with
`read` and `watch_for`, never `recommendation` or `action`. A schema that offers a
field called `recommendation` is asking for a verdict however carefully the prompt
words it, and `guard.py` would then spend the draft rejecting replies the schema
invited. The two work together: the schema declines to ask, the guard checks
anyway.

`additionalProperties: false` and explicit `required` throughout — with
`strict: true` on the tool the API guarantees the shape, and every caller can
stop writing defensive `.get()` chains.
"""

from __future__ import annotations

from typing import Any

POSITIONS = ["QB", "RB", "WR", "TE", "K", "DST"]


ROOM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["player_key", "rivals", "read", "watch_for", "confidence"],
    "properties": {
        "player_key": {
            "type": "string",
            "description": "The key of the player this is about, echoed back.",
        },
        "rivals": {
            "type": "array",
            "maxItems": 6,
            "description": (
                "Where each live rival plausibly stops bidding. Only rivals who "
                "can both afford him and start him. Omit anyone you have nothing "
                "to say about — a shorter list is better than a padded one."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["team_id", "lo", "hi", "rationale"],
                "properties": {
                    "team_id": {"type": "integer"},
                    "lo": {"type": "integer", "minimum": 1},
                    "hi": {"type": "integer", "minimum": 1},
                    "rationale": {
                        "type": "string",
                        "maxLength": 160,
                        "description": (
                            "The dossier line or precedent figure this rests on. "
                            "Cite the evidence, not a feeling."
                        ),
                    },
                },
            },
        },
        # `read`, not `recommendation`. The name is the first line of defence.
        "read": {
            "type": "string",
            "maxLength": 400,
            "description": (
                "What is worth noticing about this nomination. Never an "
                "instruction: do not tell the user to bid, pass, chase or avoid."
            ),
        },
        "watch_for": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string", "maxLength": 100},
            "description": "Specific things that would change the picture.",
        },
        # "thin" must be reachable and the prompt must say so. A model handed an
        # empty dossier will invent a personality unless refusing is offered as a
        # legitimate answer.
        "confidence": {"type": "string", "enum": ["thin", "fair", "strong"]},
    },
}


STRATEGIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict_echo", "assessment", "moves"],
    "properties": {
        # Exists solely so `guard.verdict_matches` can check it. The state is
        # computed by `advice/strategy.py`; a model that disagrees with the
        # arithmetic about the one thing arithmetic decides is not worth reading
        # on the prose either.
        "verdict_echo": {
            "type": "string",
            "enum": ["on track", "at risk", "time to move"],
            "description": "Echo the plan state you were given. Do not re-derive it.",
        },
        "assessment": {"type": "string", "maxLength": 600},
        "moves": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["what", "why"],
                "properties": {
                    "what": {"type": "string", "maxLength": 120},
                    "why": {"type": "string", "maxLength": 200},
                },
            },
        },
        "positions_at_risk": {
            "type": "array",
            "items": {"type": "string", "enum": POSITIONS},
        },
    },
}


NARRATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["why", "matters_to_me"],
    "properties": {
        "why": {
            "type": "string",
            "maxLength": 240,
            "description": (
                "One or two sentences on why this event matters. The event "
                "itself is already stated — add what it means, not a restatement."
            ),
        },
        "matters_to_me": {
            "type": "boolean",
            "description": "Does this bear on our roster or our plan specifically.",
        },
    },
}


GRADER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["grade", "summary", "roster_read", "calls"],
    "properties": {
        "grade": {
            "type": "string",
            "enum": ["A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D", "F"],
        },
        "summary": {"type": "string", "maxLength": 1200},
        "roster_read": {"type": "string", "maxLength": 1200},
        # The assistant scoring its own calls against what actually happened.
        # `reconcile.compare` supplies the arithmetic; this supplies the verdict
        # on it, which is the one place a verdict is the right answer — the draft
        # is over and nothing can be influenced.
        "calls": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["seq", "verdict", "note"],
                "properties": {
                    "seq": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["good", "fair", "poor"]},
                    "note": {"type": "string", "maxLength": 200},
                },
            },
        },
        "what_the_assistant_missed": {"type": "string", "maxLength": 800},
    },
}


BY_AGENT: dict[str, dict[str, Any]] = {
    "room": ROOM_SCHEMA,
    "strategist": STRATEGIST_SCHEMA,
    "narrator": NARRATOR_SCHEMA,
    "grader": GRADER_SCHEMA,
    # The Analyst answers in prose — it is a conversation, and forcing a shape on
    # an open question would make it answer the schema instead of the question.
    "analyst": {},
}


def schema_for(agent: str) -> dict[str, Any] | None:
    """The schema an agent must answer in, or `None` for free prose."""
    schema = BY_AGENT.get(agent)
    return schema or None
