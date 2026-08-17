from __future__ import annotations

import pytest

from ffa.cli import render
from ffa.domain import reducers
from ffa.domain.enums import Position
from ffa.domain.events import EventUndone
from ffa.domain.models import DraftState
from tests.conftest import at, sold


def state_of(init_event, *events):
    return reducers.replay([init_event, *events])


@pytest.mark.parametrize(
    "amount,unknowns,expected",
    [(155, 0, "$155"), (142, 3, "$142+"), (0, 0, "$0"), (-14, 0, "-$14 OVER")],
)
def test_money_formatting(amount, unknowns, expected):
    assert render.money(amount, unknowns=unknowns) == expected


def test_budgets_marks_my_team(init_event):
    out = render.render_budgets(state_of(init_event))
    assert "*mgr4" in out


def test_budgets_shows_the_floor_marker_and_explains_it(init_event):
    out = render.render_budgets(state_of(init_event, sold(2, "mahomes", 3, None)))
    assert "$199+" in out
    assert "some prices unknown" in out


def test_budgets_omits_the_footnote_when_everything_is_known(init_event):
    out = render.render_budgets(state_of(init_event, sold(2, "mahomes", 3, 45)))
    assert "some prices unknown" not in out


def test_budgets_can_be_filtered_to_one_team(init_event):
    out = render.render_budgets(state_of(init_event, sold(2, "mahomes", 3, 45)), only_team=3)
    assert "mgr3" in out and "mgr5" not in out


def test_starter_gaps_render_compactly(init_event):
    out = render.render_budgets(state_of(init_event))
    assert "RBx2" in out and "QB" in out


def test_a_filled_position_drops_out_of_needs(init_event):
    out = render.render_budgets(state_of(init_event, sold(2, "mahomes", 3, 45, pos=Position.QB)), only_team=3)
    assert "RBx2" in out
    assert " QB " not in f" {out} "


def test_state_lists_rosters_by_team(init_event):
    out = render.render_state(state_of(init_event, sold(2, "mahomes", 3, 45)))
    assert "mgr3: mahomes $45" in out
    assert "1 player(s) sold" in out


def test_state_shows_unknown_price_as_a_question_mark(init_event):
    out = render.render_state(state_of(init_event, sold(2, "mahomes", 3, None)))
    assert "mahomes $?" in out


def test_state_flags_dangling_players(init_event):
    state = state_of(
        init_event,
        sold(2, "mahomes", 3, 45),
        EventUndone(id=3, at=at(), target_id=2),
    )
    # A sale with nothing amended after it leaves no trace at all.
    assert "dangling" not in render.render_state(state)


def test_state_reports_the_current_nomination(init_event):
    from ffa.domain.events import PlayerNominated
    from ffa.domain.ids import PlayerRef

    state = state_of(init_event, PlayerNominated(id=2, at=at(), player=PlayerRef.from_raw("ceedee")))
    assert "up for bid: ceedee" in render.render_state(state)


def test_log_marks_undone_events(init_event):
    events = [init_event, sold(2, "mahomes", 3, 45), EventUndone(id=3, at=at(), target_id=2)]
    out = render.render_log(events)
    assert "#2" in out and "#3" in out
    lines = [line for line in out.splitlines() if line.startswith("#2")]
    assert "x" in lines[0]


def test_log_respects_a_limit(init_event):
    events = [init_event] + [sold(i + 2, f"p{i}", 1, 1) for i in range(10)]
    assert len(render.render_log(events, 3).splitlines()) == 4  # header + 3


def test_help_lists_every_verb():
    from ffa.ingest.manual.grammar import VERBS

    out = render.render_help()
    assert all(v.name in out for v in VERBS)


def test_help_for_one_verb_shows_aliases():
    out = render.render_help("sold")
    assert "aliases: s, won" in out


def test_help_for_an_unknown_verb():
    assert "no such command" in render.render_help("nope")


def test_warnings_render_with_a_marker():
    assert render.render_warnings(["a", "b"]) == "! a\n! b"
    assert render.render_warnings([]) == ""


def test_renderers_are_safe_on_uninitialized_state():
    assert render.render_budgets(DraftState.empty()) == "draft not initialized"
    assert render.render_state(DraftState.empty()) == "draft not initialized"
