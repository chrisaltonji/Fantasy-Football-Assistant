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

from ffa.assist.client import MODEL, PROFILES, ClaudeClient, Reply
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


def test_the_room_gets_the_tightest_clock(sdk):
    """It fires with a player on the block. A read that lands after the gavel is
    evidence, not advice — so the ceiling is short on purpose."""
    call(sdk, agent="room")
    call(sdk, agent="grader")

    assert sdk.calls[0]["timeout"] == 12.0
    assert sdk.calls[1]["timeout"] > sdk.calls[0]["timeout"]


# --- the per-agent profile --------------------------------------------------


def test_each_agent_is_dialled_without_being_told(sdk):
    """Model, effort and timeout all default from the agent name. Before this
    they were a constant, a dict and a default argument, so a caller could get
    one right and another wrong with nothing to notice it."""
    call(sdk, agent="room")
    call(sdk, agent="grader")
    room, grader = sdk.calls

    assert room["output_config"]["effort"] == "low"
    assert room["timeout"] == 12.0
    assert grader["output_config"]["effort"] == "high"
    assert grader["timeout"] == 300.0


def test_every_agent_shares_one_model_so_they_share_one_cache(sdk):
    """Caching is keyed on the model. A second model cannot read the prefix the
    others are paying to keep warm, and moving the three small agents to Sonnet
    saves about $0.29 a draft against a $25 cap — which is not worth a second
    cache entry or a second set of behaviour to rehearse."""
    assert {p.model for p in PROFILES.values()} == {MODEL}


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


def test_one_retry_not_two(sdk):
    """A nomination lasts seconds. A third attempt lands after the sale."""
    c = ClaudeClient("sk-ant-test")
    c.complete("room", [], "x")
    assert c._client.max_retries == 1


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
def test_one_real_call(tmp_path):
    """The earliest possible moment to discover a schema the API rejects.

    Everything above this line proves the code does what it was written to do.
    Only this proves the API agrees — that `adaptive` thinking is accepted on
    this model, that `output_config.format` takes these schemas, that a cached
    block is honoured. Run it deliberately: `pytest -m live`.
    """
    from ffa.assist.prompts import system_blocks
    from ffa.assist.schemas import NARRATOR_SCHEMA

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        from ffa.config.loader import load_dotenv
        key = load_dotenv().get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("no ANTHROPIC_API_KEY")

    # Long enough to clear the cache floor, so this also proves a cached block
    # is accepted rather than only that a call succeeds.
    prefix = ("You are reading a fantasy football auction draft. " * 120)
    reply = ClaudeClient(key).complete(
        "narrator",
        system_blocks(prefix, "narrator"),
        json.dumps({"entry": {"text": "Bijan Robinson sold for $61", "position": "RB"},
                    "market": {"inflation_ratio": 1.12}}),
        schema=NARRATOR_SCHEMA,
        effort="low",
    )

    assert set(reply.payload) >= {"why", "matters_to_me"}
    assert isinstance(reply.payload["matters_to_me"], bool)
    assert reply.usage["output"] > 0
    print(f"\nlive: {reply.usage} stop={reply.stop_reason}")


# --- the request shape against the real SDK ---------------------------------
#
# These need `anthropic` installed but no key and no network. They are the
# cheap half of what the live test proves: the live call shows the *API*
# accepts the request, and these show the *SDK* declares the shape we send.
# An upgrade that renames a parameter fails here, in a second, rather than at
# pick 12 on draft night.


def _sdk():
    return pytest.importorskip("anthropic", reason="assist extra not installed")


def test_every_parameter_we_send_exists_on_the_real_sdk():
    import inspect

    anthropic = _sdk()
    sig = inspect.signature(anthropic.Anthropic(api_key="x").messages.create)
    for name in ("model", "max_tokens", "system", "messages",
                 "thinking", "output_config", "timeout"):
        assert name in sig.parameters, f"{name} is not a messages.create parameter"


def test_adaptive_is_a_declared_thinking_type_and_budget_tokens_is_not():
    """The 400 this prevents is the single likeliest mistake in the file."""
    import typing

    _sdk()
    from anthropic.types.thinking_config_param import ThinkingConfigAdaptiveParam

    hints = typing.get_type_hints(ThinkingConfigAdaptiveParam)
    assert hints["type"] == typing.Literal["adaptive"]
    assert "budget_tokens" not in hints


def test_output_config_declares_effort_and_a_json_schema_format():
    import typing

    _sdk()
    from anthropic.types import JSONOutputFormatParam, OutputConfigParam

    assert set(typing.get_type_hints(OutputConfigParam)) >= {"effort", "format"}
    fmt = typing.get_type_hints(JSONOutputFormatParam)
    assert fmt["type"] == typing.Literal["json_schema"]
    assert "schema" in fmt
