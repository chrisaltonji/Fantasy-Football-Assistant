"""Everything the assistant needs, assembled once, before the draft starts.

The runner takes a callable, the agents take a view, the prefix takes the loaded
sources. This is the one place they meet, so `repl.py` holds a single object and
never learns what any of it is made of — the loop's whole knowledge of the
inference layer is "call `_maybe_assist`, then settle what comes back".

**Assembled at startup, deliberately.** Building the prefix means reading every
dossier and every precedent record, which is exactly the work you do not want
happening for the first time while a player is on the block. If any of it is
going to fail, it fails here, before the first nomination, with the draft not yet
running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ffa.assist.budget import SpendGuard
from ffa.assist.client import MODEL
from ffa.assist.context import SIDECAR_NAME, ReadLog
from ffa.assist.prefix import build_prefix, estimate_tokens, likely_cacheable
from ffa.assist.runner import AssistRunner


@dataclass
class AssistSession:
    """The assistant, ready to run. One per draft."""

    runner: AssistRunner
    log: ReadLog
    build_view: Callable[[], dict[str, Any]]
    labels: dict[int, str] = field(default_factory=dict)
    prefix_tokens: int = 0
    cacheable: bool = True
    # The model this prefix is cached against, so the warning can name the
    # floor it was actually checked against. The floor is per model and is
    # not monotonic across generations; a general number would be wrong.
    prefix_model: str = ""
    # The last plan state the Strategist was told about, so its trigger can fire
    # on the transition rather than on every sale. Main thread only, like the
    # rest of the loop state; the runner never sees it.
    #
    # `None` before a plan exists and after one is cleared, which is why the
    # trigger tests for it separately: a draft with no plan is a normal draft,
    # not a plan that is failing.
    plan_state: str | None = None

    def banner(self) -> str:
        """What to say at startup — including the one warning worth making loud.

        A prefix under the cache floor still works perfectly and costs about ten
        times as much, silently. It is the only failure in this layer that never
        surfaces on its own.
        """
        # Two decimals under a dollar: a 40c cap rendered as "$0" reads as no
        # budget at all, which is the opposite of what it means.
        cap = self.runner.spend.cap_dollars
        cap_text = f"${cap:.0f}" if cap >= 1 else f"${cap:.2f}"
        line = (f"assistant on — reads are judgement, never a number. "
                f"~{self.prefix_tokens} cached tokens, {cap_text} cap.")
        if not self.cacheable:
            from ffa.assist.prefix import min_cacheable

            floor = min_cacheable(self.prefix_model)
            line += (
                f"\n! the shared prefix is ~{self.prefix_tokens} tokens and "
                f"{self.prefix_model or 'this model'} does not cache under "
                f"{floor}, so every call pays full price for it. Recording "
                "dossiers would fix it."
            )
        return line

    def summary(self) -> str:
        """The closing line: what it did, what it cost, whether caching worked."""
        counts = self.log.counts()
        if not counts:
            return "assistant: no reads."
        parts = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
        return (
            f"assistant: {parts}. ${self.log.spend_dollars():.2f} spent, "
            f"{self.log.cache_hit_rate():.0%} of input tokens came from cache."
        )


def build_session(
    *, complete: Callable[..., Any], inbox, league, build_view,
    run_dir: Path | None = None, dossiers=None, precedent=None, seats=None,
    reference=None, cap_dollars: float = 25.0,
) -> AssistSession:
    """Wire one up. Pure assembly — nothing here calls an API."""
    labels = dict(getattr(league, "managers", {}) or {})
    prefix = build_prefix(
        league=league, dossiers=dossiers, precedent=precedent, seats=seats,
        labels=labels, reference=reference,
    )

    # The sidecar sits beside events.jsonl and is deliberately not it. A missing
    # or corrupt one is a shrug; see context.py.
    log = ReadLog(path=(run_dir / SIDECAR_NAME) if run_dir else None)

    return AssistSession(
        runner=AssistRunner(
            complete, inbox=inbox, prefix=prefix,
            spend=SpendGuard(cap_dollars=cap_dollars),
        ),
        log=log,
        build_view=build_view,
        labels=labels,
        prefix_tokens=estimate_tokens(prefix),
        # Checked against the model that actually reads this prefix. The tick
        # runs on another one and carries its own; see `prompts.STANDALONE`.
        cacheable=likely_cacheable(prefix, MODEL),
        prefix_model=MODEL,
    )
