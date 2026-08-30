"""The projections, the cached prefix, and where the breakpoint sits.

Two of these are about money rather than correctness, which is why they are here
rather than left to be noticed on an invoice: a prefix that is not byte-stable
silently stops caching, and a projection that carries the dossiers twice pays for
them twice on every one of ~180 nominations.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ffa.assist import projection, prompts
from ffa.assist.prefix import CONTRACT, GLOSSARY, build_prefix, likely_cacheable
from ffa.assist.schemas import ROOM_SCHEMA, schema_for

DOCS = Path(__file__).resolve().parents[2] / "docs"


@pytest.fixture(scope="module")
def view() -> dict:
    return json.loads((DOCS / "sample_state.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def idle() -> dict:
    return json.loads((DOCS / "sample_state_idle.json").read_text(encoding="utf-8"))


def approx_tokens(obj) -> int:
    text = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"))
    return len(text) // 4


# --- the projections --------------------------------------------------------


def test_the_room_sees_a_fraction_of_the_board(view):
    """~6,900 tokens down to under 2,500 — and the point is attention as much as
    cost: the nominated player should not be buried under eleven rosters."""
    assert approx_tokens(view) > 5_000
    assert approx_tokens(projection.room_payload(view)) < 2_500


def test_the_same_eleven_managers_are_not_described_twice(view):
    """`guidance.threats` and `teams` are the same rivals sharing five fields
    apiece — together 77% of this payload, measured. One thing in two shapes is
    also how a reply starts citing one and contradicting the other."""
    payload = projection.room_payload(view)

    assert "threats" not in payload["nomination"]["guidance"]
    assert payload["rivals"], "the rivals themselves must survive"


def test_the_threat_verdict_survives_on_the_rival_line(view):
    """Dropping the array must not drop `is_live`, which the glossary calls the
    single most important flag on the board."""
    payload = projection.room_payload(view)
    live = [r for r in payload["rivals"] if "is_live" in r]

    assert live, "no rival carries the per-player threat verdict"
    assert "open_at_position" in live[0]


def test_the_dossiers_are_not_shipped_twice(view):
    """The same testimony is already in the cached prefix, word for word.
    Sending both pays for it twice per nomination."""
    payload = projection.room_payload(view)
    assert "dossier" not in json.dumps(payload)


def test_a_field_the_model_is_told_to_ignore_is_not_sent(view):
    """The glossary says open_slots_by_pos includes the bench and is not
    evidence of need. Six keys per rival, per nomination, to be disregarded."""
    payload = projection.room_payload(view)
    assert "open_slots_by_pos" not in json.dumps(payload)


def test_rivals_carry_need_and_money_but_no_roster(view):
    payload = projection.room_payload(view)
    rival = payload["rivals"][0]

    assert {"max_legal_bid", "starter_gaps", "remaining"} <= set(rival)
    assert "roster" not in rival and "history" not in rival


def test_the_nominated_position_keeps_its_full_scarcity(view):
    payload = projection.room_payload(view)
    position = payload["nomination"]["position"]

    assert "elite" in payload["scarcity"][position]
    others = [p for p in payload["scarcity"] if p != position]
    assert others and "elite" not in payload["scarcity"][others[0]]


def test_the_room_is_not_asked_what_to_nominate(view):
    """Capability 4's candidate lists answer a different question, and including
    them invites the Room to answer that one instead."""
    payload = projection.room_payload(view)
    assert "bargains" not in payload["nomination_plan"]
    assert "out_of_reach" not in payload["nomination_plan"]


def test_the_narrator_gets_almost_nothing(view):
    """It writes one sentence. Handing it the board would turn that into a
    summary of the draft."""
    payload = projection.narrator_payload(
        view, entry={"text": "x", "position": "WR", "channel": "sale"}
    )
    assert approx_tokens(payload) < 400


def test_the_analyst_gets_everything(view):
    payload = projection.analyst_payload(view, question="am I short at WR?")
    assert payload["board"] == view


def test_projections_survive_an_idle_board(idle):
    """Nothing on the block is what the board shows for most of three hours."""
    assert idle["nomination"] is None
    payload = projection.room_payload(idle)
    assert payload["nomination"] == {}
    assert "guidance" not in payload.get("nomination", {})
    assert payload["rivals"]


def test_projections_never_mutate_the_view(view):
    before = json.dumps(view, sort_keys=True)
    projection.room_payload(view)
    projection.strategist_payload(view)
    assert json.dumps(view, sort_keys=True) == before


# --- the prefix -------------------------------------------------------------


class FakeLeague:
    team_count = 12
    budget = 200
    draftable_slots = 15
    scoring_type = "PPR"
    effective_team_ids = (1, 2, 3)
    managers = {1: "alex", 2: "blake", 3: "casey"}
    roster: dict = {}


def test_the_prefix_is_byte_stable():
    """Prompt caching is a byte-exact prefix match. One varying byte and the hit
    rate silently goes to zero while everything still appears to work."""
    assert build_prefix(league=FakeLeague()) == build_prefix(league=FakeLeague())


def test_the_prefix_carries_no_timestamp():
    """The reason it is built from the loaded sources and never from
    build_view(), which stamps generated_at on every call."""
    text = build_prefix(league=FakeLeague())
    assert "generated_at" not in text


def test_the_prefix_states_what_the_numbers_mean():
    """The highest-leverage tokens in the system — without them the model reasons
    from the field names and treats a hard ceiling as a suggestion."""
    text = build_prefix(league=FakeLeague())
    for term in ("max_legal_bid", "is_live", "starter_gaps", "remaining_is_floor"):
        assert term in text


def test_the_prefix_forbids_a_verdict_in_words():
    text = build_prefix(league=FakeLeague()).lower()
    assert "never tell the user what to do" in text


def test_a_seat_with_no_dossier_is_told_to_say_so():
    """A model handed an empty dossier will invent a personality unless refusing
    is offered as a legitimate answer."""
    assert "nothing recorded" in build_prefix(league=FakeLeague())


def test_the_floor_is_per_model_and_is_not_monotonic():
    """**The bug a live rehearsal found, pinned.**

    This was one constant while there was one model, and it went wrong the moment
    there were two. Haiku 4.5 will not cache a prefix under 4,096 tokens where
    Opus 5 caches from 512 — so a ~3,400-token prefix that caches perfectly on
    one silently caches nothing at all on the other. No error, no warning, just
    `cache_creation_input_tokens: 0` and full price on every call forever.

    The default for an unknown model is the *highest* floor, not the lowest: a
    model that turns out to cache is a pleasant surprise, one that does not is an
    invoice nobody reads until afterwards."""
    from ffa.assist.prefix import min_cacheable

    assert min_cacheable("claude-opus-5") == 512
    assert min_cacheable("claude-haiku-4-5") == 4096
    assert min_cacheable("some-model-shipped-next-year") == 4096

    prefix = build_prefix(league=FakeLeague())
    assert likely_cacheable(prefix, "claude-opus-5")
    assert not likely_cacheable(prefix, "claude-haiku-4-5")


def test_the_fixed_half_alone_barely_clears_the_lowest_floor():
    """Measured with `count_tokens`: the contract and glossary come to 995.

    That clears Opus 5's 512 and would not have cleared the 1,024 this once
    assumed — the old constant was Opus 4.8's floor applied to a model that
    halved it. A league with no managers, no dossiers and no history still caches
    almost nothing worth having, which is what the next test is about."""
    from ffa.assist.prefix import CONTRACT, GLOSSARY

    assert likely_cacheable(CONTRACT + GLOSSARY, "claude-opus-5")
    assert not likely_cacheable(CONTRACT + GLOSSARY, "claude-haiku-4-5")


def test_the_tick_carries_its_own_system_and_claims_no_cache():
    """It cannot cache on Haiku 4.5 at any size it would plausibly be, so it does
    not pretend to. A `cache_control` marker under the floor is not an error — it
    is a no-op that reads like a decision somebody made and verified."""
    from ffa.assist.prefix import estimate_tokens
    from ffa.assist.prompts import STANDALONE, system_blocks

    assert "room_tick" in STANDALONE
    blocks = system_blocks(build_prefix(league=FakeLeague()), "room_tick")

    assert len(blocks) == 1
    assert "cache_control" not in blocks[0]
    # Small enough that the uncached prefix is not worth caring about, and small
    # enough to answer inside a five-second cadence.
    assert estimate_tokens(blocks[0]["text"]) < 1200


def test_the_tick_is_never_handed_the_league_it_has_no_room_for():
    """The whole point of the standalone path: ~3,400 tokens of managers,
    dossiers and history on every one of ~900 calls, none of it cacheable."""
    from ffa.assist.prompts import system_blocks

    prefix = build_prefix(league=FakeLeague())
    text = system_blocks(prefix, "room_tick")[0]["text"]

    assert prefix not in text
    assert "nothing recorded" not in text


def test_a_real_prefix_clears_the_floor():
    """Three seats with no dossiers measures 1,125 — over the line, but only
    just. It is the dossiers that make caching worth having."""
    assert likely_cacheable(build_prefix(league=FakeLeague()))


def test_the_token_estimate_is_calibrated_against_the_real_tokenizer():
    """`chars // 4` is a rule of thumb for prose and reads this prefix as 2,467
    tokens when `count_tokens` says 3,782 — wrong by half, and it is the number
    that decides whether the cache warning fires. These are recorded
    measurements, so the test stays offline."""
    from ffa.assist.prefix import estimate_tokens

    for text, real in (
        (build_prefix(league=FakeLeague()), 1125),
        (CONTRACT + GLOSSARY, 995),
    ):
        estimate = estimate_tokens(text)
        assert 0.8 * real <= estimate <= 1.2 * real, (
            f"estimated {estimate} against a measured {real}"
        )


# --- the breakpoint ---------------------------------------------------------


def test_the_breakpoint_sits_after_the_shared_half():
    blocks = prompts.system_blocks("PREFIX", "room")

    assert len(blocks) == 2
    assert blocks[0]["text"] == "PREFIX"
    assert blocks[0]["cache_control"]["type"] == "ephemeral"
    assert "cache_control" not in blocks[1]


def test_every_agent_shares_one_cached_block():
    """Five agents, one cache entry. Instructions after the breakpoint cost a few
    hundred uncached tokens each; before it they would quintuple the write."""
    blocks = [prompts.system_blocks("PREFIX", a)[0]
              for a in ("room", "strategist", "narrator", "analyst", "grader")]
    assert all(b == blocks[0] for b in blocks)


def test_the_cache_survives_a_lull():
    """Five minutes expires during a commissioner pause and silently re-pays for
    the whole prefix."""
    assert prompts.system_blocks("P", "room")[0]["cache_control"]["ttl"] == "1h"


def test_an_unknown_agent_is_refused():
    with pytest.raises(KeyError):
        prompts.system_blocks("P", "nobody")


def test_the_volatile_half_is_stable_json(view):
    payload = projection.room_payload(view)
    assert prompts.user_text(payload) == prompts.user_text(payload)


# --- schemas ----------------------------------------------------------------


def test_the_room_schema_never_offers_a_field_that_invites_a_verdict():
    """The schema declining to ask and the guard checking anyway are one design.
    A field called recommendation would invite what the guard then rejects."""
    props = ROOM_SCHEMA["properties"]
    assert "read" in props and "watch_for" in props
    for banned in ("recommendation", "action", "advice", "verdict", "should_bid"):
        assert banned not in props


def test_thin_is_a_reachable_answer():
    assert "thin" in ROOM_SCHEMA["properties"]["confidence"]["enum"]


def test_every_structured_agent_has_a_schema_and_the_analyst_does_not():
    for agent in ("room", "strategist", "narrator", "grader"):
        schema = schema_for(agent)
        assert schema and schema["additionalProperties"] is False
        assert schema["required"]
    assert schema_for("analyst") is None


# --- keywords the API will not accept ---------------------------------------
#
# Found the hard way, on the first live sim run: a schema carrying `maxItems`
# comes back as a 400, and because a rejected schema is classified fatal the
# assistant mutes on its first nomination. Offline there is nothing to notice —
# the schema is valid JSON Schema and every unit test passes.
#
# Probed against the real API, one keyword per call:
#     rejected  maxItems, minimum, maximum
#     accepted  minItems, maxLength, enum, description, nested objects

REJECTED_KEYWORDS = ("maxItems", "minimum", "maximum")


def walk(node):
    """Every dict in a schema, at any depth."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


@pytest.mark.parametrize("agent", ["room", "strategist", "narrator", "grader"])
def test_no_schema_carries_a_keyword_the_api_rejects(agent):
    for node in walk(schema_for(agent)):
        for keyword in REJECTED_KEYWORDS:
            assert keyword not in node, (
                f"{agent}'s schema uses {keyword!r}, which the API rejects with "
                "a 400 — and a rejected schema mutes the assistant on its first "
                "nomination"
            )


def test_the_bounds_that_were_dropped_are_stated_in_words_instead():
    """A cap the model cannot see is not a cap.

    `maxItems` is a 400 from the API and a rejected schema is fatal, so the only
    place these bounds can live is the description. Removing one without saying
    the number in words would quietly buy a list of twelve — and the caps are now
    a latency budget, not a preference: at 15.9 ms per output token, two extra
    rivals is two extra seconds against a live auction."""
    rivals = ROOM_SCHEMA["properties"]["rivals"]
    watch = ROOM_SCHEMA["properties"]["watch_for"]

    assert "four" in rivals["description"]
    assert "two" in watch["description"]
