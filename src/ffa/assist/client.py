"""The only module that talks to the API, and the only one that imports the SDK.

Everything else in `assist/` is pure: projections, prompts, schemas, the guard,
the ledger. They are testable with no key and no network, and they stay that way
because the import lives *inside* `complete()` rather than at module scope. The
same lazy-import trick as the `playwright` reader — the suite exercises this whole
package on a machine that has never installed `anthropic`, and there is a test
asserting the module is absent from `sys.modules` afterwards.

**The seam is a callable, not this class.** `runner.py` takes anything matching
`complete(agent, system, user, *, schema, effort, timeout) -> Reply` — with the
last two defaulting per agent — so tests
inject a fake and never discover whether a key exists. That is the same shape as
`EventSource` and `RawConsole(keys=...)`, and it is the reason the assistant can
be exercised end to end offline.

**Every SDK exception becomes an `AssistError`.** A library traceback surfacing
mid-auction is the failure mode this package exists to prevent — you are three
seconds into a bidding war and the screen is telling you about a connection pool.
Failures divide in two: the ones worth retrying (overloaded, timeout, a 500) and
the ones where retrying 180 times is pure waste (a bad key, a schema the API
rejects). The second kind sets `fatal_to_assist`, and the runner mutes.

**Model parameters.** `thinking={"type": "adaptive"}` — notably *not*
`budget_tokens`, which is the pre-4.6 shape and is rejected with a 400 on Opus 5.
Which model, how much effort and how long each agent gets are one table,
`PROFILES`; the comment above it is where the "why not a cheaper model for the
small agents" question is answered with numbers.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ffa.assist.errors import AssistError

MODEL = "claude-opus-5"

# Fallback for an agent with no profile. Real ceilings are per agent below.
MAX_TOKENS = 2000


@dataclass(frozen=True)
class AgentProfile:
    """How one agent is dialled: which model, how hard it thinks, how long it gets.

    One table rather than three mechanisms. These three settings were previously
    a constant, a dict and a default argument, which meant the caller could get
    the Room's timeout right and its effort wrong without anything noticing.
    `complete()` resolves all three from the agent name, so a caller passes an
    override only when it has a reason to.
    """

    model: str
    # `None` means the model does not take an effort setting, and the key is
    # left off the request entirely. This is not a "use the default" — Haiku 4.5
    # **rejects** `output_config.effort`, so sending it with any value is a 400.
    effort: str | None
    timeout: float
    max_tokens: int = MAX_TOKENS
    # **Three states, not two, and the difference is not cosmetic.**
    #
    #   "adaptive"  -> {"type": "adaptive"}
    #   "disabled"  -> {"type": "disabled"}
    #   None        -> the key is not sent at all
    #
    # `None` is for Haiku 4.5, which has never heard of adaptive thinking: the
    # request that is not made cannot be rejected. It is **not** a way to turn
    # thinking off on a current model — omitting the field on Sonnet 5 runs
    # adaptive anyway, so a bool that omitted the key would have looked like it
    # disabled thinking and changed nothing at all. Off has to be said out loud.
    thinking: str | None = "adaptive"


# **Which agent runs where, and every boundary is a measured number.**
#
# Latency here is almost purely output size. Measured over a live practice room,
# Opus 5 at `low` effort held **17-19 ms per output token**, near-constant across
# ten calls, and the Room emits 700-880 tokens - so it took 12-17s and one read
# arrived after the player had already sold. Haiku 4.5 ran the same arithmetic at
# **4 ms per output token**. The tier is the lever; effort was already at `low`.
#
# **The middle tier — the three that run against the clock.** The Room, the
# Strategist and the Narrator all fire while a draft is moving. Sonnet 5 is
# faster per token than Opus and its cache floor is 1,024, so the ~3,400-token
# shared prefix still caches and all three read **one warm entry**. That last
# part is why they move together: caching is keyed on the model, and splitting
# them across tiers would buy a second prefix write for no gain.
#
# **The top tier — the Analyst, alone.** It is the one agent invoked on purpose,
# with a person sitting there waiting for the answer and a 60s ceiling. There is
# no clock to race, so the argument that moved the others does not reach it.
# Being alone on Opus does mean its cache entry goes cold between questions and
# it pays a ~3.4c prefix write on most calls. For an agent asked a handful of
# times that is the right trade, and it is written down here so it does not read
# like an oversight later.
#
# **The fast tier — the tick.** Opus was disqualified on latency, not cost: an
# 11-14s read cannot follow a live auction. See the note on `room_tick` below for
# why it reads no shared prefix at all.
#
# Effort and timeout are where the agents differ within a tier, and those numbers
# are measured too. The Room's first ceiling was 12s, right on its own median,
# which killed about half the reads on the first live run. It gets 25s and
# `supersede` does the job the short timeout was trying to do: a read that arrives
# after the player sold is marked `late` and never printed, which is correct and
# does not also discard the reads that would have been in time. Those ceilings
# stay where they are after the move to Sonnet - being faster is a reason to keep
# headroom, not to remove it.
# The middle tier carries every agent that runs against the draft clock; the top
# tier is the one agent a person waits on deliberately; the fast tier is the tick,
# which has five seconds and a small question. See the note above for the
# measurements behind each boundary.
LIVE_MODEL = "claude-sonnet-5"
FAST_MODEL = "claude-haiku-4-5"

PROFILES: dict[str, AgentProfile] = {
    # **Thinking off, and it is the single largest latency lever here.**
    #
    # With adaptive thinking on, the visible reply was identical every time -
    # four rivals, two watch lines, a ~330-character read - while output ranged
    # 423 to 965 tokens and latency tracked it exactly, 7.0s to 13.9s. That
    # spread was thinking, which bills and times as output.
    #
    # Off: 398-496 tokens, p50 10.4s -> 7.6s, p90 13.8s -> 8.8s. The spread
    # collapsed from 542 tokens to 98, which matters more than the median: a read
    # that is reliably eight seconds is usable under a clock in a way that one
    # averaging ten and sometimes taking fourteen is not.
    #
    # Checked rather than assumed, because disabled thinking is what makes a
    # model write its reasoning into the visible response: zero leaks across 13
    # replies, structure intact, evidence still cited from the dossiers. The
    # documented failure modes for this are specific to Opus 5; this is Sonnet.
    #
    # `"disabled"` is said out loud rather than omitted. Omitting the field runs
    # adaptive on Sonnet 5 - a profile that left it off would have looked like it
    # disabled thinking and changed nothing.
    "room": AgentProfile(LIVE_MODEL, "low", 25.0, 2000, thinking="disabled"),
    # **8s, which is longer than the 5s cadence on purpose.** The ticker skips
    # rather than queues while one is in flight, so a slow tick costs the next
    # slot and nothing else. A ceiling *below* the cadence would instead kill
    # reads that were about to land — the same mistake the Room's first 12s
    # ceiling made, at a tenth of the scale but ten times as often.
    # No effort and no thinking: Haiku 4.5 rejects the first and does not offer
    # the second. 600 tokens because the whole reply is a line and a few bands.
    "room_tick": AgentProfile(FAST_MODEL, None, 8.0, 600, thinking=None),
    "strategist": AgentProfile(LIVE_MODEL, "medium", 30.0, 2000),
    # One or two sentences. A thousand is already generous, and a ceiling is the
    # cheapest defence against an agent that decides to summarise the draft.
    "narrator": AgentProfile(LIVE_MODEL, "low", 20.0, 1000),
    # The only agent still on the top tier, and the only one with a person
    # waiting on it rather than a clock. It therefore owns its cache entry alone
    # and pays a prefix write on most calls; see the note above.
    "analyst": AgentProfile(MODEL, "high", 60.0, 4000),
    # **8,000, and measured rather than chosen.** At the shared 2,000 the Grader
    # truncated every time: its schema asks for a summary, a roster read, up to
    # twenty scored calls and a what-was-missed section, and the reply stopped
    # mid-object. With structured output that surfaces as "the reply was not the
    # JSON its schema required", which reads like a schema fault and is not one.
    # It runs once, after the draft, so the ceiling costs nothing to raise.
    "grader": AgentProfile(MODEL, "high", 300.0, 8000),
}

DEFAULT_PROFILE = AgentProfile(MODEL, "medium", 30.0)


def profile_for(agent: str) -> AgentProfile:
    return PROFILES.get(agent, DEFAULT_PROFILE)


def prefix_models() -> tuple[str, ...]:
    """Every model that reads the shared prefix, in table order.

    Derived from the table rather than listed separately, so an agent moved
    between tiers cannot leave a hardcoded list behind. `prompts.STANDALONE`
    names the agents that read no prefix at all and they are excluded here —
    the tick carries its own system prompt precisely because it could not cache
    this one.

    The caller wants this to decide whether the prefix is long enough to cache,
    and the floor is per model, so "which models" is the question that has to be
    answered before "is it long enough".
    """
    from ffa.assist.prompts import STANDALONE

    seen: dict[str, None] = {}
    for agent, profile in PROFILES.items():
        if agent not in STANDALONE:
            seen.setdefault(profile.model, None)
    return tuple(seen)


@dataclass(frozen=True)
class Reply:
    """One completed call, already parsed and priced.

    `payload` is the structured object for agents that have a schema, and
    `{"text": ...}` for the Analyst, which answers in prose. Callers never see a
    `Message`, a content block or a `stop_reason` — those are SDK shapes, and
    letting them past this module would put the SDK's vocabulary in the runner.
    """

    payload: dict[str, Any]
    usage: dict[str, int] = field(default_factory=dict)
    model: str = MODEL
    stop_reason: str = ""
    # Wall clock for the API call alone, in milliseconds. Measured rather than
    # inferred: the tick is the one agent with a deadline it can actually miss,
    # and "does Haiku answer 2,880 tokens inside five seconds" is not a question
    # a token count can answer.
    latency_ms: int = 0

    @property
    def cache_hit(self) -> bool:
        return bool(self.usage.get("cache_read"))


def _usage_dict(usage: Any) -> dict[str, int]:
    """SDK usage object to the flat shape `budget.cost_cents` prices.

    `getattr` with a default throughout: a field the SDK adds or renames must
    make a call look cheap, never raise. Underreporting cost is survivable;
    crashing at pick 40 is not.
    """
    return {
        "input": int(getattr(usage, "input_tokens", 0) or 0),
        "output": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "cache_creation": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    }


def _text_of(message: Any) -> str:
    """The text blocks of a reply, concatenated.

    Thinking blocks are in `content` too and are not the answer. Selecting on
    `type == "text"` rather than taking `content[0]` is the difference between
    reading the reply and reading the model's reasoning about it.
    """
    parts = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts).strip()


class ClaudeClient:
    """A thin, synchronous wrapper. One draft, one instance, many threads.

    The SDK client is thread-safe and holds a connection pool, so the workers
    share one. Nothing here mutates state after construction except `_client`,
    which is built once under the GIL and is idempotent if it races.
    """

    def __init__(self, api_key: str, *, model: str | None = None,
                 max_retries: int = 0) -> None:
        self._api_key = api_key
        # None means "each agent uses its own profile". A value here overrides
        # every profile at once, which is what a rehearsal on a cheaper model
        # wants — one flag, not five edits.
        self.model = model
        # No retries, against the SDK's default of two. A retry after a
        # timed-out Room call costs another 13 seconds and lands on a player
        # who has already sold — and on the first live run it was what turned a
        # 12s ceiling into 25s of dead wall-clock per failure. Failures surface
        # immediately instead; three in a row mutes, which is the behaviour that
        # was designed for this.
        self._max_retries = max_retries
        self._client: Any = None

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"ClaudeClient(model={self.model or 'per-agent'!r}, api_key='<redacted>')"

    def _ensure(self) -> Any:
        """Build the SDK client on first use, importing here and not above."""
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:
            raise AssistError(
                "the assistant needs the anthropic package: "
                "pip install -e .[assist]",
                fatal_to_assist=True,
            ) from exc
        self._client = anthropic.Anthropic(
            api_key=self._api_key, max_retries=self._max_retries
        )
        return self._client

    def complete(self, agent: str, system: list[dict[str, Any]], user: str, *,
                 schema: dict[str, Any] | None = None,
                 effort: str | None = None,
                 timeout: float | None = None) -> Reply:
        """One call. Returns a `Reply`, or raises `AssistError`. Never anything else.

        Model, effort and timeout all default from the agent's profile. Callers
        pass an override only deliberately — the Room asking for `high` effort
        should be a visible choice at the call site, not something that happens
        because an argument was left off.
        """
        client = self._ensure()
        import anthropic  # already imported by _ensure; bound here for the excepts

        profile = profile_for(agent)
        model = self.model or profile.model

        request: dict[str, Any] = {
            "model": model,
            "max_tokens": profile.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {},
            "timeout": timeout if timeout is not None else profile.timeout,
        }

        # Both of these are omitted rather than defaulted when the profile says
        # so, because the fast tier does not merely prefer their absence — Haiku
        # 4.5 rejects `effort` outright, and adaptive thinking is not one of its
        # modes. A key sent with a sensible value is still a 400 there.
        resolved_effort = effort or profile.effort
        if resolved_effort:
            request["output_config"]["effort"] = resolved_effort
        if profile.thinking is not None:
            request["thinking"] = {"type": profile.thinking}

        if schema:
            request["output_config"]["format"] = {
                "type": "json_schema",
                "schema": schema,
            }
        if not request["output_config"]:
            # An empty `output_config` is not the same as no `output_config`.
            # Send the parameter only when it carries something.
            request.pop("output_config")

        started = time.monotonic()
        try:
            message = client.messages.create(**request)
        except anthropic.AuthenticationError as exc:
            raise AssistError(
                "the API rejected the key. Check ANTHROPIC_API_KEY in .env.",
                fatal_to_assist=True,
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise AssistError(
                "this key is not permitted to use " + self.model + ".",
                fatal_to_assist=True,
            ) from exc
        except anthropic.BadRequestError as exc:
            # Our fault, every time: a malformed schema, a parameter this model
            # rejects. Retrying it 180 times would be 180 identical 400s, so it
            # is fatal, and the SDK's message is worth keeping — it names the
            # field.
            raise AssistError(
                f"the API rejected the {agent} request: {exc}",
                fatal_to_assist=True,
            ) from exc
        except anthropic.RateLimitError as exc:
            retry_after = getattr(getattr(exc, "response", None), "headers", {})
            wait = None
            try:
                wait = float(retry_after.get("retry-after", "")) if retry_after else None
            except (TypeError, ValueError):
                wait = None
            raise AssistError("rate limited", retry_after=wait) from exc
        except anthropic.APITimeoutError as exc:
            elapsed = time.monotonic() - started
            raise AssistError(
                f"the {agent} call passed {elapsed:.0f}s and was dropped"
            ) from exc
        except anthropic.APIStatusError as exc:
            raise AssistError(
                f"the API returned {getattr(exc, 'status_code', '?')} "
                f"for the {agent} call"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise AssistError("could not reach the API") from exc

        return self._parse(agent, message, schema, model,
                           latency_ms=int((time.monotonic() - started) * 1000))

    def _parse(self, agent: str, message: Any, schema: dict | None,
               model: str, *, latency_ms: int = 0) -> Reply:
        """Reply object to `Reply`. Refusals and truncation are named, not guessed."""
        stop = getattr(message, "stop_reason", "") or ""
        usage = _usage_dict(getattr(message, "usage", None))

        # Checked before reading content, because a refusal's content is empty
        # and "returned nothing" would be a misleading way to describe it.
        if stop == "refusal":
            raise AssistError(f"the {agent} call was refused by the model")

        text = _text_of(message)
        if not text:
            raise AssistError(f"the {agent} call returned nothing")

        if not schema:
            return Reply(payload={"text": text}, usage=usage, model=model,
                         stop_reason=stop, latency_ms=latency_ms)

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            # With a schema this should be impossible, which is why it is worth
            # saying so rather than silently falling back to prose: if it ever
            # fires, the structured-output request is not being honoured and
            # every guard downstream is reading a shape it did not expect.
            detail = "truncated at max_tokens" if stop == "max_tokens" else str(exc)
            raise AssistError(
                f"the {agent} reply was not the JSON its schema required ({detail})"
            ) from exc
        if not isinstance(payload, dict):
            raise AssistError(f"the {agent} reply was not an object")

        return Reply(payload=payload, usage=usage, model=model,
                     stop_reason=stop, latency_ms=latency_ms)
