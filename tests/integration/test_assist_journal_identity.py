"""The invariant the whole ephemeral-bid design exists to hold.

**A draft run with the assistant on and one run with it off must produce a
byte-identical `events.jsonl`.** That file is fsynced per line, replayed for undo
and crash-resume, and is the source of every number the tool asserts. The
assistant reads it and may never touch it.

The bid path is where that could quietly stop being true. `current_bid` is
scraped every two seconds, and the obvious way to get it to the tick agent — a
sixth event type — would have put tens of thousands of lines of derived
observation through ground truth. It would also have been invisible: the draft
would still replay, still resume, still total correctly. It would just be
carrying a record of things nobody claimed had happened.

So this runs the same seeded draft twice, once with the ladder and once without,
and compares the journals as bytes. Needs no API key and no network: the ladder
is produced by `SimSource`, and what is under test is the routing, not the model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ffa.ingest.source import BidObservation
from ffa.reference.playerbook import load_playerbook
from ffa.sim.engine import AuctionSim
from ffa.sim.source import SimSource
from ffa.state.store import DraftStore

SEED = 11
POOL = 60


SAMPLE = Path(__file__).resolve().parents[2] / "data" / "fixtures" / "sample_rankings.csv"


@pytest.fixture(scope="module")
def book():
    return load_playerbook(SAMPLE, teams=12, budget=200)


def _sim(init_event, book):
    return AuctionSim(league=init_event, book=book, seed=SEED, pool_size=POOL)


def _journal(directory, init_event, book, *, bids: bool) -> bytes:
    """Run one draft to completion and hand back the journal it wrote."""
    source = SimSource(_sim(init_event, book), bids=bids)
    stream = source.events_with_bids() if bids else source.events()

    store = DraftStore.create(directory, init_event)
    try:
        for item in stream:
            if isinstance(item, BidObservation):
                # Exactly what `_TaggingQueue` and the sim driver both do: it is
                # not an event, so it does not reach the store.
                continue
            store.dispatch(item)
    finally:
        store.close()

    return (directory / "events.jsonl").read_bytes()


def test_the_bid_ladder_leaves_the_journal_byte_identical(tmp_path, init_event, book):
    """The one that would fail silently and permanently if bids became events."""
    plain = _journal(tmp_path / "off", init_event, book, bids=False)
    with_bids = _journal(tmp_path / "on", init_event, book, bids=True)

    assert plain == with_bids


def test_the_ladder_actually_produced_something_to_be_identical_despite(
    init_event, book
):
    """Otherwise the test above passes for the wrong reason.

    A ladder that silently produced nothing — a signature change, an exception
    swallowed somewhere — would make the identity check trivially true and the
    real regression invisible."""
    source = SimSource(_sim(init_event, book), bids=True)
    observed = [x for x in source.events_with_bids() if isinstance(x, BidObservation)]

    assert len(observed) > 20


def test_no_observation_ever_carries_a_price_that_was_never_bid(init_event, book):
    """The ladder stops one step short of the sale.

    Emitting the winning number as an observation *and* as `PlayerSold` would put
    the same fact on the board in two provenances — exactly the blur the split
    exists to prevent."""
    from ffa.domain.events import PlayerNominated, PlayerSold

    source = SimSource(_sim(init_event, book), bids=True)
    stream = list(source.events_with_bids())

    sale_prices: dict[str, int] = {}
    for item in stream:
        if isinstance(item, PlayerSold):
            price = getattr(item.price, "value", None)
            if price is not None:
                sale_prices[item.player.key] = price

    for item in stream:
        if isinstance(item, BidObservation) and item.player_key in sale_prices:
            assert item.price < sale_prices[item.player_key]


def test_observations_only_ever_describe_a_player_who_is_up(init_event, book):
    """A price attributed to the wrong player would have the tick revising an
    estimate about someone who sold two nominations ago."""
    from ffa.domain.events import PlayerNominated

    source = SimSource(_sim(init_event, book), bids=True)
    current = None
    for item in source.events_with_bids():
        if isinstance(item, PlayerNominated):
            current = item.player.key
        elif isinstance(item, BidObservation):
            assert item.player_key == current
