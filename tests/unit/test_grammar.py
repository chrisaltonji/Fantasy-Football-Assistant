from __future__ import annotations

import pytest

from ffa.domain import reducers
from ffa.domain.enums import Position, Provenance
from ffa.domain.events import EventUndone, FieldAmended, PlayerNominated, PlayerSold
from ffa.domain.sourced import known
from ffa.ingest.manual.errors import CommandError
from ffa.ingest.manual.grammar import (
    VERBS,
    EmitCommand,
    NoopCommand,
    ParseContext,
    QuitCommand,
    ViewCommand,
    parse_command,
)
from ffa.ingest.source import EventSource
from tests.conftest import at, sold


@pytest.fixture
def ctx(init_event):
    """A draft where team 3 has been nicknamed `dave`."""
    name_dave = FieldAmended(
        id=2, at=at(), entity="team:3", field_name="manager",
        value=known("dave", Provenance.MANUAL, at()),
    )
    events = [init_event, name_dave]
    return ParseContext(state=reducers.replay(events), now=at(10), events=events)


def parse(line, ctx):
    return parse_command(line, ctx)


def only(command) -> object:
    assert isinstance(command, EmitCommand) and len(command.events) == 1
    return command.events[0]


# --- right-anchored parsing ---------------------------------------------------


@pytest.mark.parametrize(
    "line,expected_name",
    [
        ("sold mahomes 45 dave", "mahomes"),
        ("sold patrick mahomes 45 dave", "patrick mahomes"),
        ("sold ja'marr chase 78 t5", "ja'marr chase"),
        ("sold amon-ra st. brown 60 3", "amon-ra st. brown"),
    ],
)
def test_multiword_names_need_no_quoting(line, expected_name, ctx):
    event = only(parse(line, ctx))
    assert isinstance(event, PlayerSold)
    assert event.player.raw == expected_name


def test_trailing_position_is_recognized(ctx):
    event = only(parse("sold mahomes 45 dave qb", ctx))
    assert event.position.value is Position.QB
    assert event.player.raw == "mahomes"


def test_a_name_ending_in_a_teamlike_token_still_parses(ctx):
    """`dave` is the team here, not part of the name."""
    event = only(parse("sold patrick mahomes 45 dave", ctx))
    assert event.team.value == 3


@pytest.mark.parametrize("token,expected", [("45", 45), ("$45", 45), ("?", None)])
def test_price_token_forms(token, expected, ctx):
    assert only(parse(f"sold mahomes {token} dave", ctx)).price.value == expected


def test_unknown_price_is_recorded_as_an_explicit_unknown(ctx):
    price = only(parse("sold mahomes ? dave", ctx)).price
    assert price.value is None and not price.is_known


# --- team addressing ----------------------------------------------------------


@pytest.mark.parametrize("token", ["t3", "team3", "3", "dave", "dav"])
def test_team_token_forms_including_nickname_prefix(token, ctx):
    assert only(parse(f"sold mahomes 45 {token}", ctx)).team.value == 3


def test_slot_form_wins_over_a_confusing_nickname(ctx):
    """`3` must mean team 3 even if somebody nicknamed themselves oddly."""
    assert only(parse("sold mahomes 45 3", ctx)).team.value == 3


def test_unknown_team_lists_the_options(ctx):
    with pytest.raises(CommandError, match="no team matches"):
        parse("sold mahomes 45 nobody", ctx)


def test_out_of_range_slot_is_rejected(ctx):
    with pytest.raises(CommandError, match="no team 99"):
        parse("sold mahomes 45 t99", ctx)


# --- errors -------------------------------------------------------------------


def test_unknown_verb_suggests_the_closest(ctx):
    with pytest.raises(CommandError, match="Did you mean 'sold'"):
        parse("sould mahomes 45 dave", ctx)


def test_missing_argument_shows_usage_and_an_example(ctx):
    with pytest.raises(CommandError, match="sold <player...> <price|\\?> <team>"):
        parse("sold barkley 62", ctx)


def test_a_non_price_in_the_price_slot_is_named(ctx):
    with pytest.raises(CommandError, match="price second from the end"):
        parse("sold barkley sixty dave", ctx)


def test_selling_the_same_player_twice_is_refused_with_a_way_out(ctx):
    state = reducers.replay([*ctx.events, sold(3, "mahomes", 3, 45)])
    later = ParseContext(state=state, now=at(20), events=ctx.events)
    with pytest.raises(CommandError, match="already sold"):
        parse("sold mahomes 50 dave", later)


def test_price_for_an_unrecorded_player_says_to_sell_first(ctx):
    with pytest.raises(CommandError, match="Record the sale first"):
        parse("price mahomes 45", ctx)


# --- other verbs --------------------------------------------------------------


def test_nominate_without_a_team(ctx):
    event = only(parse("nominate ceedee lamb", ctx))
    assert isinstance(event, PlayerNominated)
    assert event.player.key == "ceedee-lamb" and event.nominated_by is None


def test_nominate_with_a_nominating_team(ctx):
    assert only(parse("nom ceedee dave", ctx)).nominated_by.value == 3


def test_price_emits_an_amendment(ctx):
    state = reducers.replay([*ctx.events, sold(3, "mahomes", 3, None)])
    later = ParseContext(state=state, now=at(20), events=ctx.events)
    event = only(parse("price mahomes 45", later))
    assert isinstance(event, FieldAmended)
    assert event.entity == "player:mahomes" and event.value.value == 45


def test_amend_rejects_a_field_that_nothing_reads(ctx):
    with pytest.raises(CommandError, match="no amendable field"):
        parse("amend team:3 budget 500", ctx)


def test_amend_rejects_a_malformed_address(ctx):
    with pytest.raises(CommandError, match="not an entity address"):
        parse("amend nonsense manager dave", ctx)


def test_undo_targets_the_last_event(ctx):
    events = [*ctx.events, sold(3, "mahomes", 3, 45)]
    later = ParseContext(state=reducers.replay(events), now=at(20), events=events)
    assert only(parse("undo", later)).target_id == 3


def test_undo_accepts_an_explicit_id(ctx):
    events = [*ctx.events, sold(3, "mahomes", 3, 45)]
    later = ParseContext(state=reducers.replay(events), now=at(20), events=events)
    assert only(parse("undo #2", later)).target_id == 2


def test_undo_of_initialization_is_refused(ctx):
    with pytest.raises(CommandError, match="cannot undo draft initialization"):
        parse("undo #1", ctx)


def test_undo_with_nothing_to_undo(init_event):
    empty = ParseContext(state=reducers.replay([init_event]), now=at(), events=[init_event])
    with pytest.raises(CommandError, match="nothing to undo"):
        parse("undo", empty)


def test_redo_finds_the_last_active_undo(ctx):
    events = [*ctx.events, sold(3, "mahomes", 3, 45), EventUndone(id=4, at=at(), target_id=3)]
    later = ParseContext(state=reducers.replay(events), now=at(20), events=events)
    assert only(parse("redo", later)).target_id == 4


def test_redo_with_nothing_to_redo(ctx):
    with pytest.raises(CommandError, match="nothing to redo"):
        parse("redo", ctx)


@pytest.mark.parametrize("line,kind", [("budgets", "budgets"), ("b", "budgets"),
                                       ("state", "state"), ("log 5", "log"), ("help sold", "help")])
def test_view_verbs(line, kind, ctx):
    command = parse(line, ctx)
    assert isinstance(command, ViewCommand) and command.kind == kind


@pytest.mark.parametrize("line", ["quit", "q", "exit"])
def test_quit_aliases(line, ctx):
    assert isinstance(parse(line, ctx), QuitCommand)


# --- comments and blanks ------------------------------------------------------


@pytest.mark.parametrize("line", ["", "   ", "# a note", "  # indented note"])
def test_blank_and_comment_lines_are_noops(line, ctx):
    assert isinstance(parse(line, ctx), NoopCommand)


def test_trailing_comments_are_stripped(ctx):
    assert only(parse("sold mahomes 45 dave  # steal", ctx)).price.value == 45


def test_comment_stripping_does_not_eat_an_undo_id(ctx):
    """`undo #7` and `# comment` share a character; the id must survive."""
    events = [*ctx.events, sold(3, "mahomes", 3, 45)]
    later = ParseContext(state=reducers.replay(events), now=at(20), events=events)
    assert only(parse("undo #3", later)).target_id == 3


# --- structural ----------------------------------------------------------------


def test_parser_returns_unstamped_events(ctx):
    """Ids and timestamps belong to the journal, not the parser."""
    event = only(parse("sold mahomes 45 dave", ctx))
    assert event.id == 0
    assert event.at is None


def test_parser_does_not_read_the_clock(ctx, monkeypatch):
    def explode(*_a, **_kw):  # pragma: no cover - must never run
        raise AssertionError("the parser read the wall clock")

    monkeypatch.setattr("ffa.util.clock.now_utc", explode)
    parse("sold mahomes 45 dave", ctx)


def test_every_verb_has_usage_and_help():
    """Prevents an undocumented verb shipping."""
    assert all(v.usage and v.help for v in VERBS)


def test_manual_source_satisfies_the_event_source_protocol():
    from ffa.ingest.manual.source import ManualSource

    assert isinstance(ManualSource(iter([]), lambda: None), EventSource)
