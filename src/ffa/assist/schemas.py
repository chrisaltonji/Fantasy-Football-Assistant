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

**Not every JSON Schema keyword is accepted here, and the rejection is a 400.**
Probed against the real API rather than assumed: `maxItems`, `minimum` and
`maximum` are refused outright; `minItems`, `maxLength`, `enum`, `description`
and nested objects are fine. So the bounds that used to be `maxItems` are stated
in the `description`, which is where the model reads them anyway, and the bounds
that actually matter — a rival estimate above a hard ceiling — were never the
schema's job in the first place. `guard.clamp_rivals` enforces those against
arithmetic, which is the only thing that can.

`test_assist_prompts.py` walks every schema and fails on a rejected keyword,
because the alternative is finding out at pick one.
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
        # **Four, and the number is a latency budget rather than a taste.**
        #
        # Latency here is output size: 15.9 ms per output token measured on
        # Sonnet 5, near-constant, so a 700-token reply takes eleven seconds and
        # races the auction it is describing. This block was 56% of that reply —
        # six rivals at ~120 characters of evidence apiece — and it is the only
        # part big enough to be worth cutting.
        #
        # It also makes the schema agree with the instructions for the first
        # time. The prompt has always said a short list of grounded reads beats
        # one line per seat, and then offered six slots, which a model reads as
        # a target. It returned six or seven on every one of ten live calls.
        "rivals": {
            "type": "array",
            "description": (
                "Where each live rival plausibly stops bidding. **At most four.** "
                "Only rivals who can both afford him and start him, and only the "
                "ones that matter most — the reader has seconds. Omit anyone you "
                "have nothing specific to say about; four grounded reads beat a "
                "padded list, and a short one is a real answer."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["team_id", "lo", "hi", "rationale"],
                "properties": {
                    "team_id": {"type": "integer"},
                    # No `minimum`: the API rejects it. A bid below $1 is
                    # impossible anyway, and `clamp_rivals` drops anything the
                    # arithmetic rules out rather than trusting the schema.
                    "lo": {"type": "integer", "description": "dollars, at least 1"},
                    "hi": {"type": "integer", "description": "dollars, at least lo"},
                    "rationale": {
                        "type": "string",
                        # 100, from 160. The evidence is a citation, not an
                        # argument — the live replies that ran to 160 were
                        # spending the tail on a second clause the reader does
                        # not have time for.
                        "maxLength": 100,
                        "description": (
                            "The dossier line or precedent figure this rests on, "
                            "in one clause. Cite the evidence, not a feeling."
                        ),
                    },
                },
            },
        },
        # `read`, not `recommendation`. The name is the first line of defence.
        "read": {
            "type": "string",
            # 280, from 400. This lands beside a bid ceiling while someone is
            # deciding whether to raise; the last third of a 400-character
            # paragraph is not read under that clock, and it costs a second.
            "maxLength": 280,
            "description": (
                "What is worth noticing about this nomination, in two or three "
                "sentences. Never an instruction: do not tell the user to bid, "
                "pass, chase or avoid."
            ),
        },
        "watch_for": {
            "type": "array",
            "items": {"type": "string", "maxLength": 100},
            "description": (
                "At most two specific things that would change the picture. The "
                "third was always the weakest."
            ),
        },
        # "thin" must be reachable and the prompt must say so. A model handed an
        # empty dossier will invent a personality unless refusing is offered as a
        # legitimate answer.
        "confidence": {"type": "string", "enum": ["thin", "fair", "strong"]},
    },
}


ROOM_TICK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["changed"],
    "properties": {
        # **The only required field, and it is the one that licenses silence.**
        # Nothing having changed is the common case on a five-second cadence, and
        # a schema whose every field were required would make "nothing to add"
        # unrepresentable — so the model would invent something to fill it. This
        # is also what `render_tick` keys off to print nothing.
        "changed": {
            "type": "boolean",
            "description": (
                "Whether anything material has changed since the opening read. "
                "False is the expected answer most of the time."
            ),
        },
        # A quarter of the Room's 400. This is read at a glance between bids.
        "note": {
            "type": "string",
            "maxLength": 120,
            "description": (
                "One short sentence, only if `changed`. What moved and why it "
                "matters. Never an instruction: not bid, pass, stop or stay in."
            ),
        },
        "rivals": {
            "type": "array",
            "description": (
                "Only rivals whose band has actually moved. At most three. Do "
                "not restate an estimate the price has not contradicted."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["team_id", "lo", "hi"],
                "properties": {
                    "team_id": {"type": "integer"},
                    "lo": {"type": "integer", "description": "dollars, at least 1"},
                    "hi": {"type": "integer", "description": "dollars, at least lo"},
                    # Half the Room's 160 and optional. Under a bidding clock the
                    # band is the message; the reasoning is a luxury.
                    "rationale": {"type": "string", "maxLength": 80},
                },
            },
        },
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
            "description": "At most three.",
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
        # **The shared memory every other agent reads, and the only field here
        # that is not about the plan.** Capped hard at 400 rather than left to
        # the prompt, because this is the one string that rides in *every* other
        # agent's payload — including the tick's, which is ~500 tokens total and
        # fires every five seconds. An unbounded digest is a slow leak straight
        # into the one payload that must stay small, and nothing in the output
        # would say it was happening.
        "digest": {
            "type": "string",
            "maxLength": 400,
            "description": (
                "Two or three sentences on where the draft now stands, for the "
                "other agents to read. Not about the plan. Written fresh each "
                "time, under 60 words."
            ),
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
            "description": "At most twenty, the ones worth commenting on.",
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
    "room_tick": ROOM_TICK_SCHEMA,
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
