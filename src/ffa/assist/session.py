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
from ffa.assist.client import prefix_models
from ffa.assist.context import SIDECAR_NAME, ReadLog
from ffa.assist.prefix import (
    build_prefix,
    estimate_tokens,
    likely_cacheable,
    strictest,
)
from ffa.assist.runner import AssistRunner

# What the tick is measured against. Not a timeout — the profile's 8s ceiling is
# that — but the cadence it is offered a turn on, and therefore the number that
# decides whether a read describes the board it was asked about.
TICK_BUDGET_MS = 5000


@dataclass
class AssistSession:
    """The assistant, ready to run. One per draft."""

    runner: AssistRunner
    log: ReadLog
    build_view: Callable[[], dict[str, Any]]
    labels: dict[int, str] = field(default_factory=dict)
    prefix_tokens: int = 0
    cacheable: bool = True
    # The *strictest* model that reads this prefix, so the warning names the
    # floor that actually binds. More than one tier reads this block and the
    # floor is per model — checking any single one of them is how a whole tier
    # caches nothing while the banner says it is fine.
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

    def timing_report(self) -> str:
        """How long each agent took, measured, per agent.

        Separate from `summary` because it is a table and because it answers a
        different question. The summary asks what this cost; this asks whether
        it arrived in time, which is the only question the tick can fail.

        **`total` is the column to read**, not `api`. It starts when the read was
        asked for and ends when there is something to print, so it includes
        waiting for a slot. A read that answers in 3s but lands at 9s because it
        queued behind another still missed the auction.
        """
        rows = []
        for agent in self.log.agents():
            t = self.log.timings(agent)
            if t:
                rows.append((agent, t))
        if not rows:
            return ""

        out = [f"  {'agent':<12}{'n':>4}{'api p50':>10}{'total p50':>11}"
               f"{'total p90':>11}{'max':>9}"]
        for agent, t in rows:
            out.append(
                f"  {agent:<12}{t['n']:>4}{t['api_p50'] / 1000:>9.1f}s"
                f"{t['p50'] / 1000:>10.1f}s{t['p90'] / 1000:>10.1f}s"
                f"{t['max'] / 1000:>8.1f}s"
            )

        # The one line that is a verdict rather than a measurement. The tick has
        # a deadline the others do not: it is offered a turn every five seconds,
        # and one that habitually answers slower than that is not following the
        # auction, it is describing a price that has already moved.
        tick = dict(rows).get("room_tick")
        if tick:
            budget = TICK_BUDGET_MS
            verdict = "inside" if tick["p90"] <= budget else "OVER"
            out.append(
                f"  tick vs the {budget / 1000:.0f}s cadence: p90 "
                f"{tick['p90'] / 1000:.1f}s — {verdict}"
            )
        return "\n".join(out)


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

    # The prefix is read by more than one tier now, and the cache floor is per
    # model. It has to clear the worst of them: checking any single reader is
    # how an entire tier caches nothing while the banner reports it as fine.
    strict_model, _ = strictest(prefix_models())

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
        cacheable=likely_cacheable(prefix, strict_model),
        prefix_model=strict_model,
    )
