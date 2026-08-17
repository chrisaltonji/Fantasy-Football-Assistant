"""Player identification tests.

R1 in the risk table: a wrong match wears a real player's price and fails
silently. The most important tests here are the ones asserting we *refuse* to
guess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ffa.domain.enums import Position
from ffa.reference.loader import ReferenceRow
from ffa.reference.playerbook import CONFIDENT, PlayerBook, load_playerbook

SAMPLE = Path(__file__).resolve().parents[2] / "data" / "fixtures" / "sample_rankings.csv"


def row(name, pos=Position.WR, value=40.0, rank=1, **kw) -> ReferenceRow:
    from ffa.domain.ids import normalize_player_key

    return ReferenceRow(
        key=normalize_player_key(name), name=name, position=pos,
        auction_value=value, overall_rank=rank, **kw,
    )


@pytest.fixture
def book() -> PlayerBook:
    return PlayerBook([
        row("Ja'Marr Chase", Position.WR, 60, 1),
        row("Bijan Robinson", Position.RB, 58, 2),
        row("Patrick Mahomes", Position.QB, 38, 3),
        row("Sam LaPorta", Position.TE, 28, 4),
        row("Marvin Harrison", Position.WR, 30, 5),
        row("Marvin Mims", Position.WR, 6, 60),
    ])


# --- resolution paths ---------------------------------------------------------


def test_exact_name(book):
    match = book.resolve("Ja'Marr Chase").match
    assert match.row.name == "Ja'Marr Chase" and match.confidence == 1.0


def test_punctuation_and_case_are_irrelevant(book):
    assert book.resolve("jamarr chase").match.row.name == "Ja'Marr Chase"
    assert book.resolve("JA'MARR CHASE").match.row.name == "Ja'Marr Chase"


def test_last_name_alone_resolves(book):
    """How people actually type mid-draft."""
    match = book.resolve("mahomes").match
    assert match.row.name == "Patrick Mahomes"
    assert match.how == "last name"
    assert match.confidence >= CONFIDENT


def test_prefix_resolves(book):
    assert book.resolve("bijan").match.row.name == "Bijan Robinson"


def test_tokens_in_any_completeness_resolve(book):
    assert book.resolve("sam lap").match.row.name == "Sam LaPorta"


# --- refusing to guess: the important half ------------------------------------


def test_an_ambiguous_first_name_is_refused_with_candidates(book):
    """Two Marvins. Picking one silently would be the worst outcome."""
    resolution = book.resolve("marvin")
    assert not resolution.resolved
    names = {m.row.name for m in resolution.candidates}
    assert names == {"Marvin Harrison", "Marvin Mims"}


def test_a_typo_is_not_auto_accepted(book):
    """`mahmoes` is *probably* Mahomes — probably isn't good enough."""
    resolution = book.resolve("mahmoes")
    assert not resolution.resolved
    assert resolution.candidates[0].row.name == "Patrick Mahomes"


def test_fuzzy_matches_stay_below_the_confidence_threshold(book):
    for match in book.resolve("mahmoes").candidates:
        assert match.confidence < CONFIDENT


def test_nonsense_yields_nothing_at_all(book):
    resolution = book.resolve("zzzzqqqq")
    assert not resolution.resolved and not resolution.candidates


def test_empty_input_resolves_to_nothing(book):
    assert not book.resolve("").resolved


def test_an_empty_book_never_resolves():
    assert not PlayerBook.empty().resolve("mahomes").resolved


def test_exclude_skips_already_taken_players(book):
    resolution = book.resolve("Ja'Marr Chase", exclude={"jamarr-chase"})
    assert not resolution.resolved


# --- values -------------------------------------------------------------------


def test_values_pass_through_at_matching_league_settings(book):
    assert book.value("jamarr-chase") == 60


def test_values_rescale_to_a_bigger_money_supply():
    """A 10-team $200 sheet in a 12-team $200 league is worth ~20% more."""
    from ffa.reference.loader import LoadReport

    report = LoadReport(source=Path("x"), rows=(row("Ja'Marr Chase", value=50.0),))
    scaled = PlayerBook.from_report(report, teams=12, budget=200, baseline_teams=10, baseline_budget=200)
    assert scaled.value("jamarr-chase") == 60


def test_values_are_whole_dollars_and_never_below_one():
    from ffa.reference.loader import LoadReport

    report = LoadReport(source=Path("x"), rows=(row("Scrub", value=0.4),))
    book = PlayerBook.from_report(report, teams=12, budget=200)
    assert book.value("scrub") == 1


def test_values_are_derived_from_rank_when_absent():
    from ffa.reference.loader import LoadReport

    rows = tuple(
        ReferenceRow(key=f"p{i}", name=f"P{i}", position=Position.WR, auction_value=None, overall_rank=i)
        for i in range(1, 21)
    )
    book = PlayerBook.from_report(LoadReport(source=Path("x"), rows=rows))
    assert book.value("p1") > book.value("p10") > book.value("p20")
    assert book.value("p20") >= 1


def test_unknown_key_has_no_value(book):
    assert book.value("nobody") is None


def test_by_position_filters(book):
    assert {r.name for r in book.by_position(Position.WR)} == {
        "Ja'Marr Chase", "Marvin Harrison", "Marvin Mims",
    }


# --- against the shipped fixture ----------------------------------------------


def test_the_sample_fixture_builds_a_usable_book():
    book = load_playerbook(SAMPLE, teams=12, budget=200, is_sample=True)
    assert len(book) > 200
    assert book.is_sample
    top = max(book.rows, key=lambda r: book.value(r.key) or 0)
    assert book.value(top.key) > 40


def test_every_fixture_surname_resolves_uniquely():
    """If the fixture had duplicate surnames every demo would hit disambiguation."""
    book = load_playerbook(SAMPLE)
    sample = list(book.rows)[:40]
    unresolved = [r.name for r in sample if not book.resolve(r.name.split()[-1]).resolved]
    assert unresolved == []
