"""One entry point for the deterministic advisory layer.

Everything it returns is arithmetic over ground truth. The Claude layer reads
this and adds the soft half — who is likely to bid given their dossier, whether
a run is worth calling out, how a roster is shaping up. It never recomputes a
number that appears here.
"""

from __future__ import annotations

from ffa.advice.bidding import guidance_for
from ffa.advice.market import market_state
from ffa.advice.scarcity import scarcity_by_position
from ffa.advice.types import Advisory
from ffa.domain.models import DraftState
from ffa.reference.playerbook import PlayerBook


def advise(state: DraftState, book: PlayerBook, *, key: str | None = None) -> Advisory:
    """Current market and scarcity, plus bid guidance when a player is named.

    `key` defaults to whatever is currently nominated, so the common case —
    "a player just came up, what do I do" — needs no argument.
    """
    market = market_state(state, book)
    scarcity = scarcity_by_position(state, book)

    target = key
    if target is None and state.current_nomination is not None:
        target = state.current_nomination.ref.key

    guidance = None
    if target:
        name = None
        player = state.players.get(target)
        if player is not None:
            name = player.ref.raw
        guidance = guidance_for(state, book, target, market, name=name)

    return Advisory(market=market, scarcity=scarcity, guidance=guidance)
