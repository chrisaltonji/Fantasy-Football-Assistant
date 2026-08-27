"""The three agents that were scaffolded for months and never built.

These tests are about the seams, not the prose. Each agent is a thin adapter —
pick a payload, pick a guard, render — and what can go wrong is picking the wrong
one, dropping a field on the way through, or letting a verdict out. The prompts
and schemas they use were already covered by `test_assist_prompts.py`.
"""

from __future__ import annotations

import pytest

from ffa.assist.agents import analyst, narrator, room, strategist
from ffa.assist.context import ReadLog, ReadRecord

# A board with just enough in it for a payload builder to select from.
VIEW = {
    "nomination": {},
    "me": {"team_id": 4, "remaining": 120},
    "teams": [{"team_id": 3, "label": "dave", "remaining": 52, "is_me": False}],
    "market": {"inflation_ratio": 1.1},
    "scarcity": {"RB": {"danger": True}},
    "strategy": {"archetype": "balanced"},
    "watchlist": [],
    "recent_sales": [],
    "feed": [],
}


# --- what each agent is wired to --------------------------------------------


@pytest.mark.parametrize("spec, expected", [
    (lambda: strategist.build(VIEW, plan_state="at risk", sale={"player_key": "x"}),
     "strategist"),
    (lambda: narrator.build(VIEW, entry={"player_key": "x"}), "narrator"),
    (lambda: analyst.build(VIEW, question="how much?"), "analyst"),
])
def test_each_agent_names_itself_the_way_its_profile_is_keyed(spec, expected):
    """`submit` looks up the profile, the prompt and the schema by this string.

    A typo here does not fail — it silently falls through to `DEFAULT_PROFILE`
    and `system_blocks` raises somewhere else entirely."""
    assert spec()["agent"] == expected


def test_the_analyst_asks_for_no_schema():
    """It answers in prose, and `schema=None` is what selects that path.

    Expressed as an absence rather than a flag, so it cannot disagree with
    `schemas.schema_for`, which says the same thing a different way."""
    assert analyst.build(VIEW, question="q")["schema"] is None


def test_the_others_all_carry_one():
    for spec in (strategist.build(VIEW, plan_state="on track"),
                 narrator.build(VIEW, entry={})):
        assert spec["schema"]["type"] == "object"


def test_the_digest_reaches_every_payload():
    """The whole point of the digest is that the other agents read it.

    It travels in the volatile user block rather than the cached prefix, which is
    why it can change on every sale without costing the cache — so the thing to
    pin is that it actually arrives."""
    assert strategist.build(VIEW, plan_state="on track", digest="D")["payload"]["digest"] == "D"
    assert narrator.build(VIEW, entry={}, digest="D")["payload"]["digest"] == "D"
    assert analyst.build(VIEW, question="q", digest="D")["payload"]["digest"] == "D"
    assert room.build(VIEW, digest="D")["payload"]["digest"] == "D"
    assert room.build_tick(VIEW, digest="D")["payload"]["digest"] == "D"


# --- the Strategist, and the verdict it may not re-derive --------------------


def test_a_strategist_that_disagrees_with_the_arithmetic_is_rejected_whole():
    """Not blanked — rejected. The plan state is computed by `advice/strategy.py`
    from shortfall and the bench reserve. A model that disagrees about the one
    thing arithmetic decides has lost the thread, and its prose is not worth
    reading either. `{}` is what makes the runner record it as `rejected`."""
    spec = strategist.build(VIEW, plan_state="time to move")
    cleaned, violations = spec["check"]({
        "verdict_echo": "on track", "assessment": "All fine.", "moves": [],
    })

    assert cleaned == {}
    assert "disagrees" in violations[0]


def test_a_strategist_that_agrees_keeps_its_prose():
    spec = strategist.build(VIEW, plan_state="at risk")
    cleaned, violations = spec["check"]({
        "verdict_echo": "at risk", "assessment": "RB is thinning fast.",
        "moves": [{"what": "free up $12", "why": "tight end is cheap late"}],
        "digest": "Prices running 10% hot.",
    })

    assert violations == []
    assert cleaned["assessment"] == "RB is thinning fast."
    assert cleaned["digest"] == "Prices running 10% hot."


def test_a_verdict_smuggled_into_the_digest_is_stripped():
    """This is the one that matters most, and it is not obvious.

    The digest is the only string that rides in *every* other agent's payload. An
    instruction left in it would be laundered into the tick's prompt and could
    come back out on screen under a different byline, having passed no guard on
    the way."""
    spec = strategist.build(VIEW, plan_state="on track")
    cleaned, violations = spec["check"]({
        "verdict_echo": "on track", "assessment": "Fine.", "moves": [],
        "digest": "Bid up to $40 on the next back.",
    })

    assert cleaned["digest"] == ""
    assert any("digest" in v for v in violations)


def test_a_move_may_describe_an_action_because_that_is_what_a_move_is():
    """"free up $12 by passing on a second tight end" is the shape being asked
    for, and it necessarily contains words the opener pattern fires on. Only
    `why` is checked — that is where an instruction would be aimed at the user
    rather than at the board."""
    spec = strategist.build(VIEW, plan_state="on track")
    cleaned, violations = spec["check"]({
        "verdict_echo": "on track", "assessment": "ok",
        "moves": [{"what": "pass on a second tight end",
                   "why": "the position is deep for another two rounds"}],
    })

    assert violations == []
    assert len(cleaned["moves"]) == 1


def test_the_strategist_does_not_reprint_the_verdict():
    """The deterministic surface already showed it. Printing it again here would
    put the same word on screen twice in two voices, and the second would read
    as a second opinion about something that is not a matter of opinion."""
    shown = strategist.render({
        "verdict_echo": "at risk", "assessment": "RB is thinning.", "moves": [],
    })

    assert "at risk" not in shown
    assert "RB is thinning." in shown


def test_the_digest_is_written_for_agents_and_never_shown():
    shown = strategist.render({"assessment": "a", "digest": "internal state"})
    assert "internal state" not in shown


def test_digest_of_reads_the_stored_record_and_tolerates_the_first_sale():
    """Before the first sale there is no digest, and that is a normal state."""
    log = ReadLog()
    assert strategist.digest_of(log.latest("strategist")) == ""

    log.append(ReadRecord(agent="strategist", moment="m",
                          payload={"digest": "Prices are hot."}))
    assert strategist.digest_of(log.latest("strategist")) == "Prices are hot."


# --- the Narrator ------------------------------------------------------------


def test_the_narrator_marks_what_bears_on_us():
    assert narrator.render({"why": "Third back over $40.", "matters_to_me": True}) \
        == "[note] * Third back over $40."
    assert narrator.render({"why": "Third back over $40.", "matters_to_me": False}) \
        == "[note] Third back over $40."


def test_a_narrator_with_nothing_left_renders_nothing():
    """An empty `[note]` looks like the assistant losing its train of thought."""
    assert narrator.render({"why": "", "matters_to_me": True}) == ""


def test_the_narrator_is_guarded_like_everything_else():
    """It speaks about a settled fact, so it has the least room to do damage —
    but it prints beside deterministic lines, which is the adjacency the rule
    exists for."""
    cleaned, violations = narrator.build(VIEW, entry={})["check"](
        {"why": "Pass on the next one.", "matters_to_me": False}
    )
    assert cleaned["why"] == ""
    assert violations


def test_prior_read_of_refuses_nothing_gracefully():
    """`ReadLog.latest` returns `None` before the Room has said anything, and it
    already refuses to hand back a rejected read — passing one on would launder
    exactly the output the guard refused."""
    assert narrator.prior_read_of(None) == {}
    assert narrator.prior_read_of(
        ReadRecord(agent="room", moment="m", payload={"read": "x"})
    ) == {"read": "x"}


# --- the Analyst -------------------------------------------------------------


def test_the_analyst_may_use_the_words_a_question_requires():
    """"What would it cost me to chase him?" is a question about arithmetic, and
    the answer has to be allowed to say chase. The Room's sentence-opener pattern
    would eat half of every legitimate reply here."""
    cleaned, violations = analyst.check_analyst({
        "text": "Chasing him past $40 would leave you $12 short at tight end."
    })

    assert violations == []
    assert cleaned["text"].startswith("Chasing")


@pytest.mark.parametrize("text", [
    "You should bid $40.",
    "I recommend the cheaper option.",
    "Go up to $38 and stop.",
])
def test_the_analyst_still_refuses_an_actual_instruction(text):
    """Asking a direct question does not suspend the standing promise."""
    cleaned, violations = analyst.check_analyst({"text": text})
    assert cleaned == {}
    assert violations


def test_the_analyst_indents_a_multi_line_answer():
    """A marker that appeared once at the top of a long block would leave the
    tail looking like tool output. The indent carries attribution down the page."""
    shown = analyst.render({"text": "First line.\nSecond line."})
    assert shown.splitlines() == ["[answer] First line.", "         Second line."]


def test_the_analyst_key_survives_the_same_question_typed_twice():
    a = analyst.moment_key("What  would it COST?", 12)
    b = analyst.moment_key("what would it cost?", 12)
    assert a == b


def test_the_analyst_key_separates_two_questions_on_the_same_board():
    assert analyst.moment_key("who is live", 12) != analyst.moment_key("what next", 12)
