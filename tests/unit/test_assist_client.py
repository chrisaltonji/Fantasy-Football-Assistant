"""The client, without the SDK, plus the one test that spends money.

Everything above the `live` marker runs on a machine that has never installed
`anthropic`: the SDK is faked in `sys.modules`, which works precisely because
`client.py` imports it inside a function. If someone moves that import to the top
of the file, `test_assist_boundary.py` fails first and these fail second.

The failure-translation tests are the point of the file. Every one of them is a
thing that will happen on draft night — a key with a typo, a schema the API
rejects, a request that outlives the player it was about — and each must arrive
as one sentence a person can act on rather than as a library traceback over the
top of a live auction.
"""

from __future__ import annotations

import json
import os
import sys
import types

import pytest

from ffa.assist.client import MODEL, PROFILES, ClaudeClient, Reply, FAST_MODEL
from ffa.assist.errors import AssistError


# --- a fake SDK -------------------------------------------------------------


class _Err(Exception):
    """Base for the fake SDK's exception tree."""

    def __init__(self, message: str = "", *, status_code: int = 0,
                 headers: dict | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = types.SimpleNamespace(headers=headers or {})


def _fake_anthropic(handler):
    """A module object shaped like the parts of the SDK `client.py` touches."""
    mod = types.ModuleType("anthropic")

    class APIStatusError(_Err): ...
    class AuthenticationError(APIStatusError): ...
    class PermissionDeniedError(APIStatusError): ...
    class BadRequestError(APIStatusError): ...
    class RateLimitError(APIStatusError): ...
    class APIConnectionError(_Err): ...
    class APITimeoutError(APIConnectionError): ...

    class Anthropic:
        def __init__(self, api_key=None, max_retries=None):
            self.api_key = api_key
            self.max_retries = max_retries
            self.messages = types.SimpleNamespace(create=handler)

    for name, obj in (
        ("Anthropic", Anthropic), ("APIStatusError", APIStatusError),
        ("AuthenticationError", AuthenticationError),
        ("PermissionDeniedError", PermissionDeniedError),
        ("BadRequestError", BadRequestError), ("RateLimitError", RateLimitError),
        ("APIConnectionError", APIConnectionError),
        ("APITimeoutError", APITimeoutError),
    ):
        setattr(mod, name, obj)
    return mod


def _message(text="{}", *, stop_reason="end_turn", usage=None, thinking=None):
    blocks = []
    if thinking:
        blocks.append(types.SimpleNamespace(type="thinking", thinking=thinking))
    blocks.append(types.SimpleNamespace(type="text", text=text))
    return types.SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=types.SimpleNamespace(**(usage or {
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 0,
        })),
    )


@pytest.fixture
def sdk(monkeypatch):
    """Install a fake `anthropic`, and hand back a way to set the response."""
    calls: list[dict] = []
    box: dict = {"result": _message()}

    def handler(**kwargs):
        calls.append(kwargs)
        result = box["result"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(handler))
    return types.SimpleNamespace(calls=calls, box=box)


def call(sdk, agent="room", schema=None, **kw):
    return ClaudeClient("sk-ant-test").complete(
        agent, [{"type": "text", "text": "sys"}], "payload", schema=schema, **kw
    )


# --- the request shape ------------------------------------------------------


def test_thinking_is_adaptive_and_carries_no_budget(sdk):
    """`budget_tokens` is the pre-4.6 shape and is a 400 on Opus 5. It is the
    single most likely thing to be wrong here, because it is what a model
    trained before the change reaches for."""
    call(sdk)
    request = sdk.calls[0]

    assert request["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in json.dumps(request["thinking"])


def test_the_schema_travels_in_output_config(sdk):
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    call(sdk, schema=schema)
    fmt = sdk.calls[0]["output_config"]["format"]

    assert fmt["type"] == "json_schema"
    assert fmt["schema"] is schema


def test_effort_is_per_agent_and_reaches_the_request(sdk):
    call(sdk, agent="grader", effort="high")
    assert sdk.calls[0]["output_config"]["effort"] == "high"


def test_the_clocks_match_what_each_agent_is_racing(sdk):
    """The Room fires with a player on the block; the Grader runs when the draft
    is over. 25s is measured, not chosen: a Room call takes 11-14s, and the
    first ceiling of 12s sat on the median and killed about half the reads on
    the first live run. Sitting on the median is the worst place for a timeout —
    a read that arrives too late is already handled, by supersede."""
    call(sdk, agent="room")
    call(sdk, agent="grader")

    assert sdk.calls[0]["timeout"] == 25.0
    assert sdk.calls[0]["timeout"] > 14.0, "under the measured worst case"
    assert sdk.calls[1]["timeout"] >= 300.0


# --- the per-agent profile --------------------------------------------------


def test_each_agent_is_dialled_without_being_told(sdk):
    """Model, effort and timeout all default from the agent name. Before this
    they were a constant, a dict and a default argument, so a caller could get
    one right and another wrong with nothing to notice it."""
    call(sdk, agent="room")
    call(sdk, agent="grader")
    room, grader = sdk.calls

    assert room["output_config"]["effort"] == "low"
    assert room["timeout"] == 25.0
    assert grader["output_config"]["effort"] == "high"
    assert grader["timeout"] == 300.0


def test_every_agent_but_the_tick_shares_one_model(sdk):
    """Caching is keyed on the model. A second model cannot read the prefix the
    others are paying to keep warm, and moving the small agents to Sonnet saves
    about $0.29 a draft against a $25 cap — not worth a second cache entry or a
    second set of behaviour to rehearse.

    `room_tick` is the deliberate exception and the same argument reversed: at a
    call every five seconds it keeps its own entry warm continuously, and Opus is
    disqualified on latency rather than cost. Pinned here so a *third* model
    cannot arrive without someone making that argument again."""
    assert {p.model for p in PROFILES.values()} == {MODEL, FAST_MODEL}
    assert PROFILES["room_tick"].model == FAST_MODEL


def test_the_fast_tier_sends_neither_effort_nor_thinking(sdk):
    """Haiku 4.5 rejects `output_config.effort` and has no adaptive thinking.

    Omitted, not defaulted — a key sent with a sensible value is still a 400,
    and this would surface on draft night rather than at import."""
    call(sdk, agent="room_tick")
    request = sdk.calls[0]

    assert "thinking" not in request
    assert "effort" not in request.get("output_config", {})
    assert request["model"] == FAST_MODEL
    assert request["timeout"] == 8.0


def test_the_fast_tier_still_gets_its_schema(sdk):
    """Dropping effort must not drop the structured output with it.

    `output_config` carries both, and the tick is the one agent where the dict
    would otherwise be empty — so the branch that omits an empty `output_config`
    has to not fire when a schema is present."""
    call(sdk, agent="room_tick", schema={"type": "object"})
    assert sdk.calls[0]["output_config"]["format"]["schema"] == {"type": "object"}
    assert "effort" not in sdk.calls[0]["output_config"]


def test_an_unknown_agent_still_gets_a_sane_call(sdk):
    """A typo in an agent name must not mean no timeout at all."""
    call(sdk, agent="nobody")
    assert sdk.calls[0]["timeout"] > 0
    assert sdk.calls[0]["output_config"]["effort"]


def test_one_model_override_moves_every_agent(sdk):
    """What a rehearsal on a cheaper model wants: one flag, not five edits."""
    c = ClaudeClient("sk-ant-test", model="claude-haiku-4-5")
    reply = c.complete("room", [], "x")

    assert sdk.calls[0]["model"] == "claude-haiku-4-5"
    # And the reply names what actually ran, because budget prices from it.
    assert reply.model == "claude-haiku-4-5"


def test_an_explicit_effort_still_wins(sdk):
    call(sdk, agent="room", effort="high")
    assert sdk.calls[0]["output_config"]["effort"] == "high"


def test_no_retries(sdk):
    """A retry after a timed-out Room call costs another 13 seconds and lands
    on a player who has already sold. Found on the first live sim run."""
    c = ClaudeClient("sk-ant-test")
    c.complete("room", [], "x")
    assert c._client.max_retries == 0


# --- parsing ----------------------------------------------------------------


def test_a_schema_reply_comes_back_parsed(sdk):
    sdk.box["result"] = _message('{"read": "thin room", "confidence": "fair"}')
    reply = call(sdk, schema={"type": "object"})

    assert reply.payload["read"] == "thin room"
    assert isinstance(reply, Reply)


def test_prose_agents_get_text(sdk):
    sdk.box["result"] = _message("Yes, you are short at WR.")
    reply = call(sdk, agent="analyst")
    assert reply.payload == {"text": "Yes, you are short at WR."}


def test_thinking_blocks_are_not_mistaken_for_the_answer(sdk):
    """They sit in `content` alongside the reply. Taking content[0] would read
    the model's reasoning and hand it on as the read."""
    sdk.box["result"] = _message('{"read": "ok"}', thinking="let me consider...")
    reply = call(sdk, schema={"type": "object"})
    assert reply.payload == {"read": "ok"}


def test_usage_is_flattened_for_pricing(sdk):
    reply = call(sdk)
    assert reply.usage == {
        "input": 100, "output": 50, "cache_read": 2000, "cache_creation": 0,
    }
    assert reply.cache_hit is True


def test_a_usage_field_that_vanishes_does_not_raise(sdk):
    """A renamed SDK field must make a call look cheap, never crash mid-draft."""
    sdk.box["result"] = _message(usage={"input_tokens": 10})
    assert call(sdk).usage["cache_read"] == 0


# --- failure translation ----------------------------------------------------


def test_a_bad_key_is_fatal_and_says_where_to_fix_it(sdk):
    import anthropic
    sdk.box["result"] = anthropic.AuthenticationError("401")

    with pytest.raises(AssistError) as caught:
        call(sdk)
    assert caught.value.fatal_to_assist
    assert "ANTHROPIC_API_KEY" in caught.value.message


def test_a_rejected_schema_is_fatal_and_keeps_the_api_wording(sdk):
    """Ours to fix, and the API's message names the field. Retrying 180 times
    would be 180 identical 400s."""
    import anthropic
    sdk.box["result"] = anthropic.BadRequestError("output_config.format invalid")

    with pytest.raises(AssistError) as caught:
        call(sdk)
    assert caught.value.fatal_to_assist
    assert "output_config.format invalid" in caught.value.message


def test_a_rate_limit_is_survivable_and_carries_its_wait(sdk):
    import anthropic
    sdk.box["result"] = anthropic.RateLimitError("429", headers={"retry-after": "3"})

    with pytest.raises(AssistError) as caught:
        call(sdk)
    assert not caught.value.fatal_to_assist
    assert caught.value.retry_after == 3.0


def test_an_unparseable_retry_after_does_not_become_a_second_failure(sdk):
    import anthropic
    sdk.box["result"] = anthropic.RateLimitError("429", headers={"retry-after": "soon"})

    with pytest.raises(AssistError) as caught:
        call(sdk)
    assert caught.value.retry_after is None


def test_a_timeout_names_the_agent_that_ran_long(sdk):
    import anthropic
    sdk.box["result"] = anthropic.APITimeoutError("slow")

    with pytest.raises(AssistError) as caught:
        call(sdk, agent="room")
    assert "room" in caught.value.message
    assert not caught.value.fatal_to_assist


def test_no_network_is_survivable(sdk):
    import anthropic
    sdk.box["result"] = anthropic.APIConnectionError("dns")

    with pytest.raises(AssistError) as caught:
        call(sdk)
    assert "could not reach" in caught.value.message
    assert not caught.value.fatal_to_assist


def test_a_refusal_is_named_rather_than_read_as_an_empty_reply(sdk):
    sdk.box["result"] = _message("", stop_reason="refusal")
    with pytest.raises(AssistError, match="refused"):
        call(sdk)


def test_truncation_is_named_when_the_json_will_not_parse(sdk):
    """With a schema this should be impossible. If it fires, structured output
    is not being honoured and every guard downstream is reading a shape it did
    not expect — so it says so instead of falling back to prose."""
    sdk.box["result"] = _message('{"read": "half a sen', stop_reason="max_tokens")

    with pytest.raises(AssistError) as caught:
        call(sdk, schema={"type": "object"})
    assert "max_tokens" in caught.value.message


def test_a_json_array_is_refused(sdk):
    sdk.box["result"] = _message("[1, 2, 3]")
    with pytest.raises(AssistError, match="not an object"):
        call(sdk, schema={"type": "object"})


def test_a_missing_package_explains_the_install(sdk, monkeypatch):
    monkeypatch.delitem(sys.modules, "anthropic")
    monkeypatch.setattr(
        "builtins.__import__",
        lambda name, *a, **k: (_ for _ in ()).throw(ImportError(name))
        if name == "anthropic" else __import__(name, *a, **k),
    )
    with pytest.raises(AssistError) as caught:
        call(sdk)
    assert "[assist]" in caught.value.message
    assert caught.value.fatal_to_assist


# --- secrets ----------------------------------------------------------------


def test_the_key_never_reaches_a_repr():
    """A key in a traceback is a key in a screenshot, and this repo is public."""
    assert "sk-ant-secret" not in repr(ClaudeClient("sk-ant-secret-value"))


# --- the live call ----------------------------------------------------------


@pytest.mark.live
def test_one_real_call():
    """The earliest possible moment to discover a schema the API rejects.

    Everything above this line proves the code does what it was written to do.
    Only this proves the API agrees — that `adaptive` thinking is accepted on
    this model, that `output_config.format` takes these schemas, and that a
    cached block is honoured. Run it deliberately: `pytest -m live`.

    **It calls twice on purpose.** One call can only ever report
    `cache_creation`; `cache_read` is zero on a cold prefix no matter how
    healthy the setup is, so a single-call test would pass identically whether
    caching worked or silently never engaged. The second call is the only one
    that can tell the difference, and that difference is roughly $16 across a
    draft. Two calls cost about three cents.
    """
    from ffa.assist.prompts import system_blocks
    from ffa.assist.schemas import NARRATOR_SCHEMA

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        from ffa.config.loader import load_dotenv
        key = load_dotenv().get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("no ANTHROPIC_API_KEY")

    # Long enough to clear the ~1024-token floor below which nothing is cached
    # at all, so this proves a cached block is honoured rather than only that a
    # call succeeds.
    prefix = "You are reading a fantasy football auction draft. " * 120
    client = ClaudeClient(key)

    def narrate(text):
        return client.complete(
            "narrator",
            system_blocks(prefix, "narrator"),
            json.dumps({"entry": {"text": text, "position": "RB"},
                        "market": {"inflation_ratio": 1.12}}),
            schema=NARRATOR_SCHEMA,
        )

    first = narrate("Bijan Robinson sold for $61")

    # The schema was honoured, and the reply is the shape the guard expects.
    assert set(first.payload) >= {"why", "matters_to_me"}
    assert isinstance(first.payload["matters_to_me"], bool)
    assert isinstance(first.payload["why"], str) and first.payload["why"]
    assert first.usage["output"] > 0
    assert first.stop_reason != "max_tokens", "2000 tokens was not enough"

    # The prefix engaged the cache — written if this run is the first inside the
    # hour, read if an earlier run already warmed it. Which one it is depends on
    # when you last ran this and is not the test's business; that *neither*
    # happened would mean the breakpoint never took and the whole prefix is
    # being paid for at full rate on every call.
    cached = first.usage["cache_creation"] + first.usage["cache_read"]
    assert cached > 0, "the prefix neither entered nor hit the cache"

    second = narrate("Jahmyr Gibbs sold for $54")

    assert second.usage["cache_read"] > 0, (
        "the prefix did not hit the cache on a second identical call — "
        "something is varying in the cached half"
    )
    assert second.usage["cache_read"] >= cached * 0.9, (
        "the second call cached less of the prefix than the first"
    )
    assert second.cache_hit

    print(f"\ncold {first.usage}\nwarm {second.usage}")


def test_the_grader_gets_room_to_finish_its_reply():
    """At the shared 2,000 it truncated on every run — its schema asks for a
    summary, a roster read, twenty scored calls and a what-was-missed section.
    With structured output that surfaces as "not the JSON its schema required",
    which reads like a schema fault and is not one."""
    assert PROFILES["grader"].max_tokens >= 8000
    assert PROFILES["narrator"].max_tokens < PROFILES["grader"].max_tokens


def test_max_tokens_is_per_agent_in_the_request(sdk):
    call(sdk, agent="narrator")
    call(sdk, agent="grader")
    assert sdk.calls[0]["max_tokens"] == PROFILES["narrator"].max_tokens
    assert sdk.calls[1]["max_tokens"] == PROFILES["grader"].max_tokens
