"""Capability 4 — the nomination order, and what to put up.

The first test in here is the load-bearing one: it asserts the *claim* the whole
module rests on against a real completed auction, so if ESPN ever stops repeating
`pickOrder` verbatim each round, a test fails rather than a readout lying.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ffa.advice.market import market_state
from ffa.advice.nomination import nomination_plan, turn_at, turns_taken
from ffa.cli import render
from ffa.config.schema import ConfigError, LeagueConfig, nomination_order_problem
from ffa.domain.enums import Position, Provenance, RosterSlot
from ffa.domain.events import DraftInitialized, PlayerNominated, PlayerSold, TeamSeed
from ffa.domain.ids import PlayerRef
from ffa.domain.models import LeagueSnapshot
from ffa.domain.reducers import replay
from ffa.domain.sourced import known
from ffa.ingest.espn.settings import (
    config_from_payloads,
    nomination_order_from_settings,
)
from ffa.reference.loader import ReferenceRow
from ffa.reference.playerbook import PlayerBook
from tests.conftest import at, sold

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "espn"

# The order this league actually drafted under in the completed-auction fixture.
ORDER = (12, 3, 2, 13, 9, 8, 4, 10, 5, 7, 1, 11)
SEATS = (1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13)


# --- the claim the module rests on ------------------------------------------------------


def test_the_nomination_order_repeats_verbatim_every_round():
    """The one fact everything else assumes, pinned against a real auction.

    `pickOrder` is twelve entries and the draft is 180 nominations, so the module
    treats it as a cycle and indexes it with a modulo. That is only sound if ESPN
    never snakes or rotates it — and this is a completed 15-round auction, every
    pick filled, so it is the strongest available evidence.
    """
    payload = json.loads(
        (FIXTURES / "mDraftDetail_completed_auction.json").read_text(encoding="utf-8")
    )
    picks = payload["draftDetail"]["picks"]
    order = tuple(payload["settings"]["draftSettings"]["pickOrder"])

    assert len(picks) == 180
    nominators = [p["nominatingTeamId"] for p in picks]
    assert nominators == list(order) * 15, (
        "ESPN no longer repeats pickOrder verbatim each round — `nominator_at` "
        "indexes it with a modulo and is now wrong"
    )


def test_no_espn_nomination_ever_goes_unsold():
    """Why sales can anchor the schedule at all.

    `turns_taken` counts sales and treats the Nth as the Nth nomination. That is
    only sound if every nomination resolves — and it does: the nominator opens
    the bidding themselves, so the floor is $1 and nothing is ever passed in.
    """
    picks = json.loads(
        (FIXTURES / "mDraftDetail_completed_auction.json").read_text(encoding="utf-8")
    )["draftDetail"]["picks"]

    assert all(p["playerId"] != -1 for p in picks)
    assert min(p["bidAmount"] for p in picks) >= 1


def test_every_pre_draft_skeleton_shows_the_same_cycle():
    """Readable in advance is the whole point, so check the unfilled payloads too."""
    for name in (
        "mDraftDetail_predraft",
        "mDraftDetail_practice_live",
        "mDraftDetail_inprogress",
    ):
        picks = json.loads(
            (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
        )["draftDetail"]["picks"]
        nominators = [p["nominatingTeamId"] for p in picks]
        first_round = nominators[:12]
        assert nominators == first_round * 15, f"{name} does not repeat its cycle"


# --- reading it off ESPN ---------------------------------------------------------------


def test_the_order_is_read_from_espn_settings():
    settings = json.loads((FIXTURES / "mSettings_auction.json").read_text(encoding="utf-8"))
    teams = json.loads((FIXTURES / "mTeam_predraft.json").read_text(encoding="utf-8"))

    config, _ = config_from_payloads(settings, teams, league_id=1, year=2026)

    assert config.nomination_order == (9, 11, 4, 3, 13, 5, 8, 10, 7, 2, 12, 1)
    assert sorted(config.nomination_order) == sorted(config.team_ids)


def test_a_missing_pick_order_is_reported_rather_than_invented():
    order, warnings = nomination_order_from_settings({}, SEATS)
    assert order == ()
    assert any("pickOrder" in w for w in warnings)


@pytest.mark.parametrize(
    "bad",
    [
        (12, 3, 2),                                   # short
        ORDER + (1,),                                 # long
        (99,) + ORDER[1:],                            # names a team not in the league
        (12, 12, 2, 13, 9, 8, 4, 10, 5, 7, 1, 11),    # one seat twice, another missing
    ],
)
def test_an_order_that_does_not_match_the_league_is_dropped(bad):
    """Never partially trusted. A short or duplicated order would still render,
    and it would name the wrong manager as being on the clock."""
    order, warnings = nomination_order_from_settings({"pickOrder": list(bad)}, SEATS)
    assert order == ()
    assert warnings


def test_what_init_writes_always_loads():
    """`config init` validates before writing, so the documented recovery command
    cannot produce a config that then refuses to load."""
    order, warnings = nomination_order_from_settings({"pickOrder": [99, 99]}, SEATS)
    assert order == () and warnings
    # And the same check is what `validate` applies, so nothing slips past.
    assert nomination_order_problem(order, SEATS) is None


def test_a_hand_edited_order_is_caught_on_load():
    config = LeagueConfig(
        league_id=1, year=2026, team_count=12, team_ids=SEATS,
        nomination_order=(99,) + ORDER[1:],
    )
    with pytest.raises(ConfigError, match="nomination order"):
        config.validate()


def test_an_absent_order_is_not_a_problem():
    assert nomination_order_problem((), SEATS) is None


# --- the schedule ---------------------------------------------------------------------


@pytest.fixture
def snapshot() -> LeagueSnapshot:
    return LeagueSnapshot(
        league_id=1,
        year=2026,
        name="Test",
        draft_type="AUCTION",
        budget=200,
        team_count=12,
        my_team_id=4,
        roster={
            RosterSlot.QB: 1, RosterSlot.RB: 2, RosterSlot.WR: 2, RosterSlot.TE: 1,
            RosterSlot.FLEX: 1, RosterSlot.K: 1, RosterSlot.DST: 1, RosterSlot.BE: 5,
            RosterSlot.IR: 1,
        },
        flex_positions=(Position.RB, Position.WR, Position.TE),
        nomination_order=ORDER,
    )


def state_for(snapshot: LeagueSnapshot, events=()):
    init = DraftInitialized(
        id=1, at=at(), draft_id="d", league=snapshot,
        teams=tuple(TeamSeed(team_id=i, manager=f"mgr{i}") for i in SEATS),
    )
    return replay((init, *events))


def sale(index: int, team_id: int, price: int = 5, position=Position.RB) -> PlayerSold:
    return sold(index + 2, f"Filler {index}", team_id, price, pos=position, seconds=index)


def test_the_whole_schedule_is_known_before_a_ball_is_snapped(snapshot):
    plan = nomination_plan(state_for(snapshot), None)

    assert plan.order_known is True
    assert plan.turns_taken == 0
    assert plan.total_nominations == 12 * 14  # 15 roster slots less IR
    assert plan.on_the_clock.team_id == 12
    assert plan.on_the_clock.round_number == 1
    # My seat is 4, which sits seventh in the order.
    assert plan.nominations_until_mine == 6
    assert plan.my_next.round_number == 1
    assert plan.my_turns_left == 14


def test_the_cycle_carries_into_later_rounds(snapshot):
    state = state_for(snapshot)
    assert turn_at(state, 0).team_id == turn_at(state, 12).team_id == 12
    assert turn_at(state, 12).round_number == 2
    assert turn_at(state, 13).round_number == 2


def test_nothing_is_promised_past_the_last_nomination(snapshot):
    state = state_for(snapshot)
    last = snapshot.total_nominations - 1
    assert turn_at(state, last) is not None
    assert turn_at(state, last + 1) is None


def test_sales_advance_the_clock(snapshot):
    """An auction resolves every nomination with a sale, so sales are the anchor."""
    state = state_for(snapshot, [sale(i, SEATS[i % 12]) for i in range(3)])
    assert turns_taken(state) == 3

    plan = nomination_plan(state, None)
    assert plan.turns_taken == 3
    assert plan.on_the_clock.team_id == ORDER[3]
    assert plan.nominations_until_mine == 3  # seat 4 is at index 6


def test_a_live_nomination_moves_the_clock_on_without_moving_the_credit(snapshot):
    """The seat that put this player up and the seat due to put up the next one
    are different, and conflating them tells you it is your turn while somebody
    else's player is on the block."""
    state = state_for(
        snapshot,
        [sale(i, SEATS[i % 12]) for i in range(6)]
        + [PlayerNominated(id=99, at=at(7), player=PlayerRef.from_raw("Bijan Robinson"))],
    )

    plan = nomination_plan(state, None)
    assert plan.current_nominator.team_id == ORDER[6] == 4   # mine, and already used
    assert plan.on_the_clock.team_id == ORDER[7]
    assert plan.is_my_turn is False
    # Eleven other seats go before mine comes round again — counted from the
    # nomination still to be made, not from the one already on the block.
    assert plan.nominations_until_mine == 11
    # My turn is spent for this round, so the next one is a full cycle away.
    assert plan.my_next.round_number == 2
    assert plan.my_turns_left == 13


def test_a_nomination_already_made_is_not_counted_as_one_ahead_of_you(snapshot):
    """The off-by-one a full dry run surfaced: with a player on the block it
    counted the live nomination among the ones still to come, and told you four
    when three people were ahead of you."""
    sales = [sale(i, SEATS[i % 12]) for i in range(3)]
    on_block = PlayerNominated(id=90, at=at(4), player=PlayerRef.from_raw("Bijan Robinson"))

    clear = nomination_plan(state_for(snapshot, sales), None)
    live = nomination_plan(state_for(snapshot, sales + [on_block]), None)

    # Seat 4 is at index 6 either way; the live board has one fewer to wait for.
    assert clear.nominations_until_mine == 3
    assert live.nominations_until_mine == 2


def test_it_is_my_turn_when_nothing_is_on_the_block(snapshot):
    state = state_for(snapshot, [sale(i, SEATS[i % 12]) for i in range(6)])
    plan = nomination_plan(state, None)

    assert plan.on_the_clock.team_id == 4
    assert plan.is_my_turn is True
    assert plan.nominations_until_mine == 0


def test_no_order_means_no_seat_named_rather_than_id_order(snapshot):
    """The tempting fallback is `effective_team_ids`, and it is wrong: ESPN's
    order is a shuffle, so id order names a specific manager and is confidently
    incorrect about something the user can see on their own screen."""
    plan = nomination_plan(state_for(replace(snapshot, nomination_order=())), None)

    assert plan.order_known is False
    assert plan.on_the_clock is None
    assert plan.my_next is None
    assert plan.nominations_until_mine is None


def test_a_board_that_disagrees_with_the_order_says_so(snapshot):
    state = state_for(
        snapshot,
        [
            sale(0, 12),
            PlayerNominated(
                id=98, at=at(2), player=PlayerRef.from_raw("Bijan Robinson"),
                # The order says seat 3 was up at nomination 2.
                nominated_by=known(9, Provenance.MANUAL, at(2)),
            ),
        ],
    )

    plan = nomination_plan(state, None)
    assert "mgr9" in plan.disagreement
    assert "mgr3" in plan.disagreement


def test_an_agreeing_board_is_silent(snapshot):
    state = state_for(
        snapshot,
        [
            sale(0, 12),
            PlayerNominated(
                id=98, at=at(2), player=PlayerRef.from_raw("Bijan Robinson"),
                nominated_by=known(ORDER[1], Provenance.MANUAL, at(2)),
            ),
        ],
    )
    assert nomination_plan(state, None).disagreement == ""


# --- surviving a resume ---------------------------------------------------------------


def test_the_order_survives_a_round_trip_through_the_journal(snapshot):
    init = DraftInitialized(id=1, at=at(), draft_id="d", league=snapshot)
    restored = DraftInitialized.from_payload(init.to_payload())
    assert restored.league.nomination_order == ORDER


def test_a_journal_written_before_this_existed_still_replays(snapshot):
    """Journals written today must replay under every future version, and the
    reverse: an old journal has no order, and unknown is the honest answer."""
    payload = DraftInitialized(id=1, at=at(), draft_id="d", league=snapshot).to_payload()
    del payload["league"]["nomination_order"]

    restored = DraftInitialized.from_payload(payload)
    assert restored.league.nomination_order == ()
    assert restored.league.nominator_at(0) is None


# --- the candidate lists --------------------------------------------------------------


def book_for(rows) -> PlayerBook:
    return PlayerBook(tuple(rows), source=Path("test.csv"))


def row(name: str, position: Position, value: float, rank: int) -> ReferenceRow:
    from ffa.domain.ids import normalize_player_key

    return ReferenceRow(
        key=normalize_player_key(name), name=name, position=position,
        auction_value=value, overall_rank=rank,
    )


@pytest.fixture
def book() -> PlayerBook:
    return book_for(
        [
            row("Bijan Robinson", Position.RB, 60, 1),
            row("Ja'Marr Chase", Position.WR, 55, 2),
            row("Brock Bowers", Position.TE, 30, 3),
            row("Josh Allen", Position.QB, 25, 4),
            row("Bench Body", Position.RB, 3, 90),
        ]
    )


def test_nothing_is_singled_out_at_the_start_of_a_draft(snapshot, book):
    """Every seat shares one ceiling and every seat needs everything, so no
    player is uncontested and none is out of reach. An empty answer here is the
    correct one, and inventing a pick would be the whole failure mode."""
    state = state_for(snapshot)
    plan = nomination_plan(state, book, market_state(state, book))

    assert plan.bargains == ()
    assert plan.out_of_reach == ()


def fill_every_rival_starter(snapshot, *, skip=()) -> list:
    """Sell every rival a full set of starters, so no gap is left anywhere.

    Six players each: three RB, two WR, one TE. The third RB lands in FLEX, which
    is the point — see the test below for why filling TE alone is not enough.
    """
    events = []
    index = 0
    for team_id in SEATS:
        if team_id == snapshot.my_team_id or team_id in skip:
            continue
        for slot, position in enumerate(
            (Position.RB, Position.RB, Position.RB, Position.WR, Position.WR, Position.TE)
        ):
            events.append(
                sold(index + 2, f"Filler {team_id}-{slot}", team_id, 1,
                     pos=position, seconds=index)
            )
            index += 1
    return events


def test_filling_every_rival_te_is_not_enough_because_flex_stays_open(snapshot, book):
    """Non-obvious, and it is why `bargains` is quiet for most of a draft.

    TE is flex-eligible in this league, so a rival with their TE slot filled and
    their FLEX open still has somewhere to start another one — and remains a live
    threat. `has_starter_gap` was already right about this; a nomination readout
    that assumed otherwise would promise a $1 tight end and be wrong.
    """
    events = [
        sold(index + 2, f"Their TE {team_id}", team_id, 1, pos=Position.TE, seconds=index)
        for index, team_id in enumerate(t for t in SEATS if t != snapshot.my_team_id)
    ]
    state = state_for(snapshot, events)
    plan = nomination_plan(state, book, market_state(state, book))

    assert plan.bargains == ()


def test_a_player_nobody_can_start_is_flagged_as_free_money(snapshot, book):
    """The sharp case, and pure arithmetic: we still need a TE, every rival has
    filled every starting slot one could go in, so the auction has nobody to bid
    it up."""
    state = state_for(snapshot, fill_every_rival_starter(snapshot))
    plan = nomination_plan(state, book, market_state(state, book))

    names = [c.name for c in plan.bargains]
    assert "Brock Bowers" in names
    bowers = next(c for c in plan.bargains if c.name == "Brock Bowers")
    assert bowers.live_rivals == 0
    assert bowers.fills_my_starter_gap is True


def test_one_rival_with_a_gap_is_enough_to_disqualify_a_bargain(snapshot, book):
    """"No live rival" means no live rival, not few. One team with room and a
    dollar is the whole difference between free money and an auction."""
    state = state_for(snapshot, fill_every_rival_starter(snapshot, skip=(9,)))
    plan = nomination_plan(state, book, market_state(state, book))

    assert "Brock Bowers" not in [c.name for c in plan.bargains]


def test_a_player_out_of_our_reach_is_separated_from_one_we_can_win(snapshot, book):
    """We have spent almost everything; one rival has not. A player they can
    outbid us on outright cannot cost us somebody we could have won."""
    events = [sold(2, "Our Expensive Guy", 4, 190, pos=Position.RB, seconds=1)]
    state = state_for(snapshot, events)
    plan = nomination_plan(state, book, market_state(state, book))

    assert plan.out_of_reach, "a rival with a full budget outbids our $9 ceiling"
    top = plan.out_of_reach[0]
    assert top.contested_ceiling > top.my_ceiling
    assert top.live_rivals > 0
    # Ranked by dollars actually extractable, so the priciest reachable name leads.
    assert top.name == "Bijan Robinson"


def test_a_player_already_on_the_block_is_not_a_name_to_put_up(snapshot, book):
    state = state_for(
        snapshot,
        [PlayerNominated(id=99, at=at(1), player=PlayerRef.from_raw("Bijan Robinson"))],
    )
    plan = nomination_plan(state, book, market_state(state, book))

    every = [c.key for c in plan.bargains + plan.out_of_reach]
    assert state.current_nomination.ref.key not in every


def test_a_player_with_no_reference_row_is_left_out_rather_than_priced(snapshot):
    """Every test in the candidate split is a comparison against a price, and
    inventing one is what this codebase refuses everywhere else."""
    state = state_for(snapshot)
    plan = nomination_plan(state, book_for([]), market_state(state, book_for([])))
    assert plan.bargains == () and plan.out_of_reach == ()


def test_the_truncated_lists_carry_their_own_totals(snapshot):
    """A list that stops at five and says nothing reads as 'there are five'."""
    big = book_for([row(f"Tight End {i}", Position.TE, 20 - i, i + 1) for i in range(9)])

    state = state_for(snapshot, fill_every_rival_starter(snapshot))
    plan = nomination_plan(state, big, market_state(state, big))

    assert len(plan.bargains) == 5
    assert plan.bargains_total == 9


# --- the readout ----------------------------------------------------------------------


def test_the_readout_leads_with_your_own_turn(snapshot):
    text = render.render_nomination_plan(nomination_plan(state_for(snapshot), None))
    assert "your turn" in text
    assert "mgr12" in text


def test_the_readout_refuses_to_name_a_seat_without_an_order(snapshot):
    plan = nomination_plan(state_for(replace(snapshot, nomination_order=())), None)
    text = render.render_nomination_plan(plan)

    assert "no draft order" in text or "no nomination order" in text
    for team_id in SEATS:
        assert f"mgr{team_id}" not in text


def test_the_readout_puts_a_disagreement_first(snapshot):
    state = state_for(
        snapshot,
        [
            sale(0, 12),
            PlayerNominated(
                id=98, at=at(2), player=PlayerRef.from_raw("Bijan Robinson"),
                nominated_by=known(9, Provenance.MANUAL, at(2)),
            ),
        ],
    )
    text = render.render_nomination_plan(nomination_plan(state, None))
    assert text.splitlines()[0].startswith("!")


def test_no_reference_data_does_not_become_a_claim_about_the_board(snapshot):
    """The schedule works with no auction values at all, and must not pay for
    that by asserting there is nothing worth putting up. Two empty lists with no
    prices loaded mean we did not look."""
    plan = nomination_plan(state_for(snapshot), None)
    text = render.render_nomination_plan(plan)

    assert plan.board_read is False
    assert "no reference data" in text
    assert "priced past your ceiling" not in text
    # The half that never needed a sheet is still there.
    assert "your turn" in text


def test_an_uninitialized_draft_renders_nothing_confident():
    assert "not initialized" in render.render_nomination_plan(None)
