"""Loader and schema tests.

Weighted toward the awkward shapes a real export actually arrives in, because
a loader that only handles tidy CSVs is a loader that fails the night before
the draft.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ffa.domain.enums import Position
from ffa.reference import schema
from ffa.reference.loader import ReferenceError, load_reference

SAMPLE = Path(__file__).resolve().parents[2] / "data" / "fixtures" / "sample_rankings.csv"


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- header aliasing ----------------------------------------------------------


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Player", schema.PLAYER_NAME), ("PLAYER NAME", schema.PLAYER_NAME),
        ("Name", schema.PLAYER_NAME),
        ("Value", schema.AUCTION_VALUE), ("Auction Value", schema.AUCTION_VALUE),
        ("$", schema.AUCTION_VALUE), ("AAV", schema.AUCTION_VALUE),
        ("Price", schema.AUCTION_VALUE),
        ("Pos", schema.POSITION), ("Team", schema.NFL_TEAM),
        ("Rank", schema.OVERALL_RANK), ("Bye Week", schema.BYE_WEEK),
        ("nonsense", None),
    ],
)
def test_header_aliases(header, expected):
    assert schema.canonical_column(header) == expected


def test_header_detection_needs_more_than_one_hit():
    """A data row containing a player named "Value" must not look like a header."""
    assert not schema.looks_like_header(["Value"])
    assert schema.looks_like_header(["Player", "Value"])


def test_first_matching_column_wins():
    """Exports repeat value-ish columns; the leftmost is the configured one."""
    mapping = schema.map_headers(["Player", "Value", "Auction Value"])
    assert list(mapping.items())[1] == (1, schema.AUCTION_VALUE)
    assert 2 not in mapping


# --- combined player cells ----------------------------------------------------


@pytest.mark.parametrize(
    "cell,name,team,pos,rank",
    [
        ("Ja'Marr Chase CIN (WR1)", "Ja'Marr Chase", "CIN", Position.WR, 1),
        ("Bijan Robinson ATL (RB2)", "Bijan Robinson", "ATL", Position.RB, 2),
        ("Sam LaPorta (DET - TE)", "Sam LaPorta", "DET", Position.TE, None),
        ("Josh Allen (QB - BUF)", "Josh Allen", "BUF", Position.QB, None),
        ("Travis Kelce, KC TE", "Travis Kelce", "KC", Position.TE, None),
    ],
)
def test_combined_cells_split(cell, name, team, pos, rank):
    parsed = schema.parse_player_cell(cell)
    assert (parsed.name, parsed.nfl_team, parsed.position, parsed.positional_rank) == (
        name, team, pos, rank,
    )


def test_a_plain_name_stays_whole():
    parsed = schema.parse_player_cell("Patrick Mahomes")
    assert parsed.name == "Patrick Mahomes" and parsed.position is None


def test_a_non_position_parenthetical_is_not_treated_as_a_combined_cell():
    """`(Jr)` or a note must not be mistaken for a position."""
    assert schema.parse_player_cell("Odell Beckham (Jr)").name == "Odell Beckham (Jr)"


@pytest.mark.parametrize(
    "raw,expected", [("$45", 45.0), ("45", 45.0), ("1,200", 1200.0), ("", None),
                     ("-", None), ("n/a", None), ("garbage", None)],
)
def test_money_parsing(raw, expected):
    assert schema.parse_money(raw) == expected


# --- the shipped fixture ------------------------------------------------------


def test_sample_fixture_loads_past_its_title_rows():
    """The fixture has two title lines above the header on purpose."""
    report = load_reference(SAMPLE)
    assert report.header_line == 3
    assert report.ok > 200
    assert not report.dropped


def test_sample_fixture_has_unique_keys():
    report = load_reference(SAMPLE)
    keys = [r.key for r in report.rows]
    assert len(keys) == len(set(keys))


# --- formats ------------------------------------------------------------------


def test_tab_separated_loads(tmp_path: Path):
    path = write(tmp_path, "v.tsv", "Player\tPos\tValue\nJa'Marr Chase\tWR\t$60\n")
    assert load_reference(path).rows[0].auction_value == 60


def test_pasted_whitespace_aligned_text_loads(tmp_path: Path):
    """Copy-paste is a first-class path: CSV export is behind a paywall."""
    path = write(
        tmp_path, "v.txt",
        "Player            Pos   Value\n"
        "Ja'Marr Chase     WR    $60\n"
        "Bijan Robinson    RB    $58\n",
    )
    report = load_reference(path)
    assert [r.name for r in report.rows] == ["Ja'Marr Chase", "Bijan Robinson"]


def test_semicolon_delimited_loads(tmp_path: Path):
    path = write(tmp_path, "v.csv", "Player;Pos;Value\nJa'Marr Chase;WR;60\n")
    assert load_reference(path).rows[0].position is Position.WR


def test_a_bom_does_not_break_the_header(tmp_path: Path):
    path = tmp_path / "v.csv"
    path.write_text("Player,Pos,Value\nJa'Marr Chase,WR,60\n", encoding="utf-8-sig")
    assert load_reference(path).ok == 1


# --- tolerance: rows drop, loads continue -------------------------------------


def test_a_bad_row_is_dropped_with_a_reason_not_raised(tmp_path: Path):
    path = write(
        tmp_path, "v.csv",
        "Player,Pos,Value\n"
        "Ja'Marr Chase,WR,60\n"
        ",,\n"                       # blank — skipped silently
        "Mystery Man,,25\n"          # no position — dropped with a reason
        "Bijan Robinson,RB,58\n",
    )
    report = load_reference(path)
    assert [r.name for r in report.rows] == ["Ja'Marr Chase", "Bijan Robinson"]
    assert len(report.dropped) == 1
    assert "no position" in report.dropped[0][1]


def test_duplicates_are_dropped_and_point_at_the_original(tmp_path: Path):
    path = write(
        tmp_path, "v.csv",
        "Player,Pos,Value\nJa'Marr Chase,WR,60\nJa'Marr Chase,WR,55\n",
    )
    report = load_reference(path)
    assert report.ok == 1
    assert "duplicate" in report.dropped[0][1]


def test_metadata_lines_above_the_header_are_skipped(tmp_path: Path):
    path = write(
        tmp_path, "v.csv",
        "My 2026 Auction Values\n"
        "Generated for 12 teams / $200 / PPR\n"
        "\n"
        "Player,Pos,Value\n"
        "Ja'Marr Chase,WR,60\n",
    )
    report = load_reference(path)
    assert report.header_line == 4 and report.ok == 1


def test_missing_value_column_warns_rather_than_failing(tmp_path: Path):
    path = write(tmp_path, "v.csv", "Player,Pos,Rank\nJa'Marr Chase,WR,1\n")
    report = load_reference(path)
    assert report.ok == 1
    assert any("derived from rank" in w for w in report.warnings)


def test_mostly_unpriced_rows_warn(tmp_path: Path):
    rows = "\n".join(f"Player {i},WR," for i in range(10))
    path = write(tmp_path, "v.csv", f"Player,Pos,Value\nJa'Marr Chase,WR,60\n{rows}\n")
    assert any("have a value" in w for w in load_reference(path).warnings)


# --- hard errors --------------------------------------------------------------


def test_missing_file_says_what_to_do(tmp_path: Path):
    with pytest.raises(ReferenceError, match="drop the file in data/reference"):
        load_reference(tmp_path / "nope.csv")


def test_no_header_row_shows_what_it_looked_for(tmp_path: Path):
    path = write(tmp_path, "v.csv", "just,some,junk\nmore,junk,here\n")
    with pytest.raises(ReferenceError, match="could not find a header row"):
        load_reference(path)


def test_header_but_no_rows_is_an_error(tmp_path: Path):
    path = write(tmp_path, "v.csv", "Player,Pos,Value\n")
    with pytest.raises(ReferenceError, match="no usable rows"):
        load_reference(path)


def test_empty_file_is_an_error(tmp_path: Path):
    with pytest.raises(ReferenceError, match="is empty"):
        load_reference(write(tmp_path, "v.csv", "   \n"))


def test_report_summary_is_human_readable(tmp_path: Path):
    path = write(tmp_path, "v.csv", "Player,Pos,Value\nJa'Marr Chase,WR,60\nBad Row,,\n")
    summary = load_reference(path).summary()
    assert "1 row(s) ok, 1 dropped" in summary
    assert "header found on line 1" in summary
