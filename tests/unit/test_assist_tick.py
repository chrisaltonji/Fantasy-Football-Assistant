"""The Room's second gear, and the plumbing that lets it fire every five seconds.

Three things are being pinned here, and they fail in different ways:

- **The payload stays small.** It runs on a fast model against a 5s clock, and
  both only work while the question is small. Nothing enforces that but a test.
- **Silence is the default.** A line per tick is forty lines per player, which
  scrolls the arithmetic off the screen at the worst possible moment.
- **Bids never reach the journal.** That is the invariant the whole ephemeral
  path exists to hold, and it would fail silently and permanently.
"""

from __future__ import annotations

import json
import queue

import pytest

from ffa.assist.agents import room
from ffa.assist.budget import SpendGuard
from ffa.assist.client import PROFILES
from ffa.assist.runner import TICK_AGENTS, AssistRunner
from ffa.domain.events import EVENT_TYPES
from ffa.ingest.source import BidObservation

VIEW = {
    "nomination": {
        "player_key": "bijan-robinson",
        "name": "Bijan Robinson",
        "position": "RB",
        "guidance": {
            "max_advisable_bid": 41,
            "max_legal_bid": 88,
            "plan_cap": 45,
            "threats": [
                {"team_id": 3, "is_live": True, "max_legal_bid": 52,
                 "dossier": "x" * 800},
                {"team_id": 9, "is_live": False, "max_legal_bid": 30,
                 "dossier": "y" * 800},
            ],
        },
    },
    "me": {"team_id": 4, "remaining": 120},
    "teams": [{"team_id": 3, "label": "dave", "is_me": False}],
    "market": {"inflation_ratio": 1.1},
    "scarcity": {"RB": {"danger": True}},
    "watchlist": [{"key": "a"}] * 20,
    "recent_sales": [{"key": "b"}] * 20,
}


# --- the payload -------------------------------------------------------------


def test_the_tick_payload_is_a_fraction_of_the_open_one():
    """Not a style preference. This is the input to a 5s cadence on a small
    model, and the open payload is ~3,200 tokens at the first nomination."""
    tick = json.dumps(room.build_tick(VIEW, live_bid={"price": 30})["payload"])
    full = json.dumps(room.build(VIEW)["payload"])

    assert len(tick) < len(full) / 3


def test_the_tick_drops_rivals_who_cannot_bid():
    """A rival who is not live is not a threat, and costs six keys to say so."""
    payload = room.build_tick(VIEW)["payload"]
    assert [r["team_id"] for r in payload["rivals"]] == [3]


def test_the_tick_strips_the_dossier_like_the_open_read_does():
    """The same testimony is in the cached prefix word for word. Shipping it
    again pays twice and shows the model one thing in two shapes."""
    payload = room.build_tick(VIEW)["payload"]
    assert all("dossier" not in r for r in payload["rivals"])


def test_the_tick_still_sees_our_own_ceilings():
    """Leaving these out would have the model revising a band with no idea where
    our ceiling sits — which is the one comparison the read is for."""
    ceiling = room.build_tick(VIEW)["payload"]["our_ceiling"]
    assert ceiling == {"max_advisable_bid": 41, "max_legal_bid": 88, "plan_cap": 45}


def test_the_tick_carries_what_it_is_revising():
    """Its whole premise is that it is answering back to the open read. Without
    this it is a fresh analysis on a small model, which is a different and much
    worse thing."""
    payload = room.build_tick(VIEW, opening_read={"read": "Dave is the one to beat"})["payload"]
    assert payload["opening_read"]["read"] == "Dave is the one to beat"


def test_the_tick_is_told_what_an_empty_opening_read_means():
    """**Measured live: 44% of ticks fire before the open read lands.**

    The open read takes ~10s and the first bid arrives well inside that, so the
    earliest ticks of every auction have nothing to revise. The prompt used to
    say "`opening_read` is what you said", which for those calls is a false
    premise — and telling a model it said something it did not is how a read
    starts citing an estimate nobody made.
    """
    from ffa.assist.prompts import ROOM_TICK

    assert "empty" in ROOM_TICK
    assert "not an error" in ROOM_TICK


def test_a_tick_with_no_opening_read_still_carries_what_it_needs():
    """It is not blind, only un-anchored: the price, our ceilings and the live
    rivals are all still there, which is what those early reads actually used."""
    payload = room.build_tick(VIEW, live_bid={"price": 30})["payload"]

    assert payload["opening_read"] == {}
    assert payload["live_bid"]["price"] == 30
    assert payload["our_ceiling"]["max_advisable_bid"] == 41
    assert payload["rivals"], "the live rivals are the anchor when the read is not"


# --- silence -----------------------------------------------------------------


def test_nothing_changed_renders_nothing():
    """The common case, by design. The schema makes `changed` the only required
    field precisely so that saying nothing is representable."""
    assert room.render_tick({"changed": False, "note": "ignored"}) == ""


def test_repeating_itself_renders_nothing():
    """Happens whenever the price moves inside a band already given."""
    last = {"note": "Dave is still in.", "rivals": [{"team_id": 3, "lo": 40, "hi": 50}]}
    shown = room.render_tick(
        {"changed": True, "note": "Dave is still in.",
         "rivals": [{"team_id": 3, "lo": 40, "hi": 50}]},
        last=last,
    )
    assert shown == ""


def test_a_changed_band_is_news_even_with_the_same_sentence():
    last = {"note": "Dave is still in.", "rivals": [{"team_id": 3, "lo": 40, "hi": 50}]}
    shown = room.render_tick(
        {"changed": True, "note": "Dave is still in.",
         "rivals": [{"team_id": 3, "lo": 46, "hi": 52}]},
        last=last, labels={3: "dave"},
    )
    assert "dave" in shown


def test_crossing_a_ceiling_overrides_suppression():
    """The same sentence means something different on the other side of a number
    the tool computed."""
    last = {"note": "Dave is still in.", "rivals": []}
    shown = room.render_tick(
        {"changed": True, "note": "Dave is still in."},
        last=last, crossed="your plan cap",
    )
    assert "past your plan cap" in shown


def test_a_reply_whose_content_the_guard_stripped_renders_nothing():
    """`check_room_tick` forces `changed` false when nothing survives, so an
    empty `[read]` never reaches the screen."""
    cleaned, violations = room.build_tick(VIEW)["check"](
        {"changed": True, "note": "Pass on him.", "rivals": []}
    )
    assert violations
    assert room.render_tick(cleaned) == ""


def test_the_tick_clamps_against_the_same_arithmetic_as_the_open_read():
    """An estimate above `max_legal_bid` describes money that does not exist."""
    cleaned, violations = room.build_tick(VIEW)["check"](
        {"changed": True, "note": "ok", "rivals": [{"team_id": 3, "lo": 50, "hi": 99}]}
    )
    assert cleaned["rivals"] == []
    assert "above their $52 ceiling" in violations[0]


# --- crossing is arithmetic, not judgement -----------------------------------


@pytest.mark.parametrize("price, expected", [
    (30, ""),
    (42, "the advisable bid"),   # past max_advisable_bid (41), under plan_cap
    (46, "your plan cap"),       # past both; names the one that binds
])
def test_crossing_is_decided_here_and_never_asked_of_the_model(price, expected):
    assert room.crossing({"price": price}, VIEW) == expected


def test_crossing_says_nothing_when_there_is_no_price():
    assert room.crossing(None, VIEW) == ""
    assert room.crossing({"price": None}, VIEW) == ""


# --- the moment key ----------------------------------------------------------


def test_two_ticks_at_different_prices_are_different_moments():
    a = room.tick_moment_key("bijan", 143, 38)
    b = room.tick_moment_key("bijan", 143, 44)
    assert a != b


def test_a_tick_key_never_collides_with_an_open_read_key():
    assert room.tick_moment_key("bijan", 143, 38) != room.moment_key("bijan", 143)


# --- the transport -----------------------------------------------------------


def _runner(inbox=None):
    def never_returns(*a, **kw):
        import time
        time.sleep(30)
    return AssistRunner(never_returns, inbox=inbox or queue.Queue(), prefix="p",
                        spend=SpendGuard(cap_dollars=99))


def test_the_tick_has_its_own_slot_and_a_busy_room_cannot_starve_it():
    """Sharing the pool would have starved it outright: a 25s open read plus one
    Strategist holds both shared slots for most of a nomination, which is exactly
    the window the tick exists to cover."""
    r = _runner()
    assert r.submit("room", {}, moment="m1")
    assert r.submit("strategist", {}, moment="m2")
    assert not r.submit("narrator", {}, moment="m3")   # shared pool is full
    assert r.submit("room_tick", {}, moment="m4")      # its own slot is not


def test_a_second_tick_is_skipped_rather_than_queued():
    """A tick that waits for a slot lands describing a price nobody is bidding."""
    r = _runner()
    assert r.submit("room_tick", {}, moment="m1")
    assert not r.submit("room_tick", {}, moment="m2")


def test_the_tick_agent_set_matches_the_profile_table():
    """These are two lists of agent names that must not drift apart: one decides
    which semaphore is taken, the other which model is called."""
    assert TICK_AGENTS <= set(PROFILES)
    assert room.TICK_AGENT in TICK_AGENTS


# --- bids are not events -----------------------------------------------------


def test_a_bid_observation_is_not_a_journal_event():
    """The invariant the whole ephemeral path exists to hold. A `BidObservation`
    that became an event would put tens of thousands of derived lines through an
    fsynced file that undo and crash-resume replay."""
    assert "BidObservation" not in EVENT_TYPES
    assert not isinstance(BidObservation("x"), tuple(EVENT_TYPES.values()))


def test_a_bid_observation_carries_no_event_id():
    """It has no place in the ordering that ground truth is ordered by."""
    assert not hasattr(BidObservation("x", 30), "id")


class _Reader:
    """Hands back a scripted sequence of board snapshots, then stops the source."""

    def __init__(self, snapshots, source_box):
        self._snapshots = list(snapshots)
        self._box = source_box

    def snapshot(self):
        if not self._snapshots:
            self._box[0].stop()
            raise RuntimeError("exhausted")
        return self._snapshots.pop(0)


def _board(price):
    from ffa.ingest.espn.draftroom import RoomNomination, RoomSnapshot
    return RoomSnapshot(
        teams=(), picks=(), slots_per_team=16,
        nominated=RoomNomination(player="Bijan Robinson", position="RB",
                                 current_bid=price),
    )


def _drain(prices):
    from ffa.ingest.espn.source import DraftRoomSource
    box = []
    source = DraftRoomSource(_Reader([_board(p) for p in prices], box), interval=0)
    box.append(source)
    out = []
    try:
        for item in source.events():
            out.append(item)
    except RuntimeError:
        pass
    return out


def test_the_poller_emits_one_nomination_and_then_only_price_moves():
    """Until now this branch did not exist: the gate fires once per player, so
    every raise after the opening bid was scraped, parsed and thrown away."""
    from ffa.domain.events import PlayerNominated

    out = _drain([2, 2, 8, 8, 14])

    assert isinstance(out[0], PlayerNominated)
    assert [type(x) for x in out[1:]] == [BidObservation, BidObservation]
    assert [x.price for x in out[1:]] == [8, 14]


def test_an_unchanged_price_says_nothing():
    """It polls every two seconds. Re-emitting the same number would wake the
    tick on nothing and spend a call to say so."""
    out = _drain([5, 5, 5, 5])
    assert len(out) == 1  # the nomination, and nothing else


def test_the_opening_bid_is_not_repeated_as_an_observation():
    """It already travelled on `PlayerNominated.opening_bid`. Emitting it twice
    would have the tick fire immediately on a board the open read is still
    describing."""
    out = _drain([7, 7])
    assert not any(isinstance(x, BidObservation) for x in out)


def test_the_price_resets_with_the_player():
    """Carrying it would silence the first raise on the next nomination whenever
    the two happened to open at the same number."""
    from ffa.domain.events import PlayerNominated
    from ffa.ingest.espn.draftroom import RoomNomination, RoomSnapshot

    def board(name, price):
        return RoomSnapshot(teams=(), picks=(), slots_per_team=16,
                            nominated=RoomNomination(player=name, current_bid=price))

    from ffa.ingest.espn.source import DraftRoomSource
    box = []
    source = DraftRoomSource(
        _Reader([board("Bijan Robinson", 9), board("Puka Nacua", 9),
                 board("Puka Nacua", 12)], box), interval=0)
    box.append(source)
    out = []
    try:
        for item in source.events():
            out.append(item)
    except RuntimeError:
        pass

    assert [type(x) for x in out] == [PlayerNominated, PlayerNominated, BidObservation]
    assert out[-1].price == 12


# --- the simulator can exercise all of it with no ESPN ----------------------


def test_the_sim_source_stays_silent_unless_asked_for_bids():
    """A `BidObservation` is not an event. A source that produced them unasked
    would change what every existing sim test sees on the queue."""
    import inspect

    from ffa.sim.source import SimSource
    assert inspect.signature(SimSource.__init__).parameters["bids"].default is False


def test_the_sim_ladder_climbs_to_the_sale_without_reaching_it():
    """The winning number arrives as `PlayerSold` like it always did. Emitting it
    twice — once as an observation and once as ground truth — is exactly the blur
    the split exists to prevent."""
    from datetime import datetime, timezone

    from ffa.domain.enums import Provenance
    from ffa.domain.events import PlayerNominated, PlayerSold
    from ffa.domain.ids import PlayerRef
    from ffa.domain.sourced import known
    from ffa.sim.source import SimSource

    now = datetime(2026, 8, 16, 19, 0, tzinfo=timezone.utc)
    player = PlayerRef.from_raw("Bijan Robinson")
    nom = PlayerNominated(at=now, source="SIM", player=player,
                          opening_bid=known(1, Provenance.SIM, now))
    sold = PlayerSold(at=now, source="SIM", player=player,
                      team=known(3, Provenance.SIM, now),
                      price=known(41, Provenance.SIM, now))

    class FakeSim:
        seed = 1
        def events(self):
            return iter([nom, sold])

    out = list(SimSource(FakeSim(), bids=True).events_with_bids())
    prices = [x.price for x in out if isinstance(x, BidObservation)]

    assert out[0] is nom and out[-1] is sold
    assert prices == sorted(prices)
    assert all(1 < p < 41 for p in prices)


def test_the_sim_ladder_skips_an_auction_with_no_room_in_it():
    """A player who goes for $1 has no ladder, and inventing one would put a
    price on the board that nobody ever bid."""
    from datetime import datetime, timezone

    from ffa.domain.enums import Provenance
    from ffa.domain.events import PlayerNominated, PlayerSold
    from ffa.domain.ids import PlayerRef
    from ffa.domain.sourced import known
    from ffa.sim.source import SimSource

    now = datetime(2026, 8, 16, 19, 0, tzinfo=timezone.utc)
    player = PlayerRef.from_raw("Some Guy")
    nom = PlayerNominated(at=now, source="SIM", player=player,
                          opening_bid=known(1, Provenance.SIM, now))
    sold = PlayerSold(at=now, source="SIM", player=player,
                      team=known(3, Provenance.SIM, now),
                      price=known(1, Provenance.SIM, now))

    class FakeSim:
        seed = 1
        def events(self):
            return iter([nom, sold])

    out = list(SimSource(FakeSim(), bids=True).events_with_bids())
    assert not any(isinstance(x, BidObservation) for x in out)
