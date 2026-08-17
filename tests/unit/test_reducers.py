from __future__ import annotations

import pytest

from ffa.domain import reducers
from ffa.domain.enums import Position, Provenance
from ffa.domain.events import EventUndone, FieldAmended, PlayerNominated, PlayerSold
from ffa.domain.ids import PlayerRef
from ffa.domain.models import DraftState
from ffa.domain.projections import remaining_budget
from ffa.domain.sourced import known, unknown
from tests.conftest import at, sold


def amend(event_id, entity, field_name, value, *, seconds=0, provenance=Provenance.MANUAL):
    return FieldAmended(
        id=event_id, at=at(seconds), entity=entity, field_name=field_name,
        value=known(value, provenance, at(seconds)),
    )


# --- the order-independence property -----------------------------------------


def test_amend_before_sale_and_after_sale_converge(init_event):
    """The justification for auto-vivifying entities, tested directly.

    ESPN can lag, so an amendment may arrive before the sale it corrects. Both
    orders must produce the same state, because `merge` decides who wins — not
    arrival order.
    """
    sale = sold(2, "mahomes", 3, None, seconds=1)
    correction = amend(3, "player:mahomes", "price", 45, seconds=1)

    forward = reducers.replay([init_event, sale, correction])
    # Same events, same observation timestamps, opposite order.
    backward = reducers.replay(
        [init_event, type(correction)(**{**correction.__dict__, "id": 2}),
         type(sale)(**{**sale.__dict__, "id": 3})]
    )
    assert forward.players["mahomes"].price.value == backward.players["mahomes"].price.value == 45


def test_amendment_for_an_unseen_player_creates_a_stub(init_event):
    state = reducers.replay([init_event, amend(2, "player:ghost", "price", 30)])
    assert state.players["ghost"].price.value == 30
    assert not state.players["ghost"].sold


def test_undone_sale_leaves_a_dangling_amendment(init_event):
    """Undo removes one event's contribution; it does not cascade."""
    state = reducers.replay([
        init_event,
        sold(2, "mahomes", 3, 45),
        amend(3, "player:mahomes", "price", 50),
        EventUndone(id=4, at=at(), target_id=2),
    ])
    player = state.players["mahomes"]
    assert player.is_dangling and not player.sold
    assert player.price.value == 50
    # A dangling player charges nobody.
    assert remaining_budget(state, 3) == 200


# --- merge behaviour through the reducer -------------------------------------


def test_espn_price_fills_in_a_manual_unknown(init_event):
    """The flagship partial-fallback path, end to end through replay."""
    state = reducers.replay([
        init_event,
        sold(2, "mahomes", 3, None, seconds=1),
        amend(3, "player:mahomes", "price", 45, seconds=2, provenance=Provenance.ESPN_API),
    ])
    assert state.players["mahomes"].price.value == 45


def test_a_manual_correction_survives_a_later_poll(init_event):
    state = reducers.replay([
        init_event,
        sold(2, "mahomes", 3, 45, seconds=1),
        amend(3, "player:mahomes", "price", 47, seconds=2, provenance=Provenance.ESPN_API),
    ])
    assert state.players["mahomes"].price.value == 45


def test_a_repeated_sale_merges_rather_than_duplicating(init_event):
    """CP5's poller will re-report picks; that must be harmless."""
    state = reducers.replay([init_event, sold(2, "mahomes", 3, 45), sold(3, "mahomes", 3, 45)])
    assert len(state.sold_players()) == 1
    assert remaining_budget(state, 3) == 155


# --- reducers are total ------------------------------------------------------


def test_overspend_is_recorded_not_rejected(init_event):
    state = reducers.replay([init_event, sold(2, "superstar", 3, 250)])
    assert state.players["superstar"].sold
    assert remaining_budget(state, 3) < 0
    assert any("over budget" in w for w in state.warnings)


def test_a_bad_entity_address_warns_instead_of_raising(init_event):
    state = reducers.replay([init_event, amend(2, "nonsense", "price", 10)])
    assert any("not an entity address" in w for w in state.warnings)


def test_an_unknown_field_warns_instead_of_raising(init_event):
    state = reducers.replay([init_event, amend(2, "player:mahomes", "hat_size", "L")])
    assert any("no field" in w for w in state.warnings)


def test_reducers_never_call_the_clock(init_event, monkeypatch):
    """Replay determinism depends on this. Timestamps come from events only."""
    def explode(*_a, **_kw):  # pragma: no cover - must never run
        raise AssertionError("a reducer read the wall clock")

    monkeypatch.setattr("ffa.util.clock.now_utc", explode)
    reducers.replay([init_event, sold(2, "mahomes", 3, 45), amend(3, "player:mahomes", "price", 50)])


def test_team_entity_stores_no_budget():
    """Structural pin on 'nothing computable is stored'."""
    from ffa.domain.models import TeamEntity

    assert not {f for f in TeamEntity.__dataclass_fields__} & {"budget", "remaining", "roster", "spent"}


# --- nominations --------------------------------------------------------------


def test_a_sale_clears_the_matching_nomination(init_event):
    state = reducers.replay([
        init_event,
        PlayerNominated(id=2, at=at(), player=PlayerRef.from_raw("mahomes")),
        sold(3, "mahomes", 3, 45),
    ])
    assert state.current_nomination is None


def test_a_sale_leaves_an_unrelated_nomination_standing(init_event):
    state = reducers.replay([
        init_event,
        PlayerNominated(id=2, at=at(), player=PlayerRef.from_raw("chase")),
        sold(3, "mahomes", 3, 45),
    ])
    assert state.current_nomination is not None
    assert state.current_nomination.ref.key == "chase"


# --- team amendments ----------------------------------------------------------


def test_naming_a_manager_changes_the_team_label(init_event):
    state = reducers.replay([init_event, amend(2, "team:3", "manager", "dave")])
    assert state.teams[3].label == "dave"


def test_label_never_uses_the_team_name(init_event):
    """Team names change constantly, including mid-draft.

    A readout that silently starts calling someone by a new team name — or
    worse, matches the wrong team — is a real failure mode. Falling back to
    `team3` is the honest answer.
    """
    state = reducers.replay([
        init_event,
        amend(2, "team:3", "name", "Gibbs Me Dat"),
        amend(3, "team:3", "manager", ""),
    ])
    assert state.teams[3].name.value == "Gibbs Me Dat"
    assert "Gibbs" not in state.teams[3].label


def test_label_falls_back_to_the_slot_when_no_manager_is_set(init_event):
    from ffa.domain.models import TeamEntity

    assert TeamEntity(team_id=7).label == "team7"
    assert DraftState.empty().teams == {}


def test_owner_id_is_the_stable_anchor(init_event):
    """Identity keys off the ESPN member SWID, not anything renameable."""
    state = reducers.replay([init_event])
    assert state.teams[3].owner_id.value == "{OWNER-03}"


def test_owner_id_is_amendable(init_event):
    state = reducers.replay([init_event, amend(2, "team:3", "owner_id", "{NEW-SWID}")])
    assert state.teams[3].owner_id.value == "{NEW-SWID}"


def test_team_ids_are_not_assumed_contiguous(init_event):
    """The real league has no team 6. Seeding range(1, count+1) would invent
    a phantom team and drop a real one."""
    assert 6 not in init_event.league.roster  # sanity: not confusing slots with ids
    state = reducers.replay([init_event])
    assert sorted(state.teams) == [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13]
    assert len(state.teams) == 12
