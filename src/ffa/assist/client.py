"""The only module that talks to the API, and the only one that imports the SDK.

Everything else in `assist/` is pure: projections, prompts, schemas, the guard,
the ledger. They are testable with no key and no network, and they stay that way
because the import lives *inside* `complete()` rather than at module scope. The
same lazy-import trick as the `playwright` reader — the suite exercises this whole
package on a machine that has never installed `anthropic`, and there is a test
asserting the module is absent from `sys.modules` afterwards.

**The seam is a callable, not this class.** `runner.py` takes anything matching
`complete(agent, system, user, *, schema, effort, timeout) -> Reply`, so tests
inject a fake and never discover whether a key exists. That is the same shape as
`EventSource` and `RawConsole(keys=...)`, and it is the reason the assistant can
be exercised end to end offline.

**Every SDK exception becomes an `AssistError`.** A library traceback surfacing
mid-auction is the failure mode this package exists to prevent — you are three
seconds into a bidding war and the screen is telling you about a connection pool.
Failures divide in two: the ones worth retrying (overloaded, timeout, a 500) and
the ones where retrying 180 times is pure waste (a bad key, a schema the API
rejects). The second kind sets `fatal_to_assist`, and the runner mutes.

**Model parameters.** `claude-opus-5` with `thinking={"type": "adaptive"}`.
Notably *not* `budget_tokens` — that is the pre-4.6 shape and it is rejected with
a 400 on Opus 5. Effort travels per agent through `output_config`, because the
Room under a bidding clock and the Grader after the draft want opposite ends of
it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ffa.assist.errors import AssistError

MODEL = "claude-opus-5"

# Enough for a read plus its rationale, and low enough that a model which
# decides to write an essay is cut off rather than billed for one.
MAX_TOKENS = 2000

# Per-agent ceilings. The Room's is the one that matters: it fires while a player
# is on the block, and a read that lands after the gavel is evidence rather than
# advice. The others are not racing anything.
TIMEOUTS = {
    "room": 12.0,
    "strategist": 30.0,
    "narrator": 20.0,
    "analyst": 60.0,
    "grader": 300.0,
}


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

    def __init__(self, api_key: str, *, model: str = MODEL,
                 max_retries: int = 1) -> None:
        self._api_key = api_key
        self.model = model
        # One retry, not the SDK's default of two. A nomination lasts seconds;
        # a third attempt would land well after the player sold, and the Room's
        # own timeout is the honest ceiling.
        self._max_retries = max_retries
        self._client: Any = None

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"ClaudeClient(model={self.model!r}, api_key='<redacted>')"

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
                 effort: str = "medium",
                 timeout: float | None = None) -> Reply:
        """One call. Returns a `Reply`, or raises `AssistError`. Never anything else."""
        client = self._ensure()
        import anthropic  # already imported by _ensure; bound here for the excepts

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
            "timeout": timeout if timeout is not None else TIMEOUTS.get(agent, 30.0),
        }
        if schema:
            request["output_config"]["format"] = {
                "type": "json_schema",
                "schema": schema,
            }

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

        return self._parse(agent, message, schema)

    def _parse(self, agent: str, message: Any, schema: dict | None) -> Reply:
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
            return Reply(payload={"text": text}, usage=usage,
                         model=self.model, stop_reason=stop)

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

        return Reply(payload=payload, usage=usage, model=self.model, stop_reason=stop)
