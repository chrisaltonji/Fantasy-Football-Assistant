"""Reading a rankings file without being precious about its shape.

The contract, which matters more than the implementation:

- **Row-level problems never raise.** A bad row is dropped, the reason is
  recorded, and the load continues. You find out about it from
  `ffa data validate`, days before the draft, not from a crash during it.
- **Hard errors are only:** the file can't be read, no header row can be found,
  or a required column is missing after aliasing.
- **The header is searched for, not assumed.** Exports routinely carry a title
  or settings line above the real header.

Formats: `.csv`, `.tsv`, `.txt` (including pasted, whitespace-aligned text) and
`.xlsx`/`.xlsm`. Copy-paste is a first-class input because CSV export sits
behind a paid tier on the source in use.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ffa.domain.enums import Position
from ffa.domain.ids import normalize_player_key
from ffa.reference import schema
from ffa.reference.schema import (
    ADP,
    AUCTION_VALUE,
    BYE_WEEK,
    ESPN_PLAYER_ID,
    NFL_TEAM,
    OVERALL_RANK,
    PLAYER_NAME,
    POSITION,
    POSITIONAL_RANK,
    PROJECTED_POINTS,
    TIER,
)

HEADER_SEARCH_LINES = 15
_MULTISPACE = re.compile(r"\s{2,}")


class ReferenceError(Exception):
    """The file as a whole is unusable. Row problems never reach here."""


@dataclass(frozen=True)
class ReferenceRow:
    key: str
    name: str
    position: Position
    nfl_team: str | None = None
    auction_value: float | None = None
    overall_rank: int | None = None
    positional_rank: int | None = None
    adp: float | None = None
    projected_points: float | None = None
    tier: int | None = None
    bye_week: int | None = None
    espn_player_id: str | None = None


@dataclass(frozen=True)
class LoadReport:
    source: Path
    rows: tuple[ReferenceRow, ...] = ()
    dropped: tuple[tuple[int, str], ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    header_line: int = 0
    columns: tuple[str, ...] = ()

    @property
    def ok(self) -> int:
        return len(self.rows)

    def summary(self) -> str:
        lines = [
            f"{self.source}",
            f"  header found on line {self.header_line}",
            f"  columns: {', '.join(self.columns) or '(none)'}",
            f"  {self.ok} row(s) ok, {len(self.dropped)} dropped",
        ]
        for lineno, reason in self.dropped[:20]:
            lines.append(f"    line {lineno}: {reason}")
        if len(self.dropped) > 20:
            lines.append(f"    ... and {len(self.dropped) - 20} more")
        lines.extend(f"  ! {w}" for w in self.warnings)
        return "\n".join(lines)


# --- format readers -----------------------------------------------------------


def _read_grid(path: Path) -> list[list[str]]:
    """Everything becomes a list of rows of strings before we look at it."""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return _read_xlsx(path)
    return _read_text(path)


def _read_xlsx(path: Path) -> list[list[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - openpyxl is a hard dep
        raise ReferenceError(f"cannot read {path}: openpyxl is not installed") from exc

    try:
        book = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise ReferenceError(f"cannot read {path}: {exc}") from exc

    sheet = book[book.sheetnames[0]]
    return [["" if cell is None else str(cell) for cell in row] for row in sheet.iter_rows(values_only=True)]


def _read_text(path: Path) -> list[list[str]]:
    try:
        # utf-8-sig strips the BOM Excel likes to add.
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ReferenceError(f"cannot read {path}: {exc}") from exc
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")

    if not text.strip():
        raise ReferenceError(f"{path} is empty")

    delimiter = _sniff_delimiter(text)
    if delimiter == "WHITESPACE":
        # Pasted, column-aligned text: split on runs of 2+ spaces so single
        # spaces inside "Ja'Marr Chase" survive.
        return [_MULTISPACE.split(line.strip()) for line in text.splitlines() if line.strip()]

    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:HEADER_SEARCH_LINES + 5])
    for candidate in ("\t", ",", ";", "|"):
        if any(candidate in line for line in sample.splitlines()):
            try:
                return csv.Sniffer().sniff(sample, delimiters=candidate).delimiter
            except csv.Error:
                return candidate
    return "WHITESPACE"


# --- the load -----------------------------------------------------------------


def load_reference(path: Path) -> LoadReport:
    path = Path(path)
    if not path.is_file():
        raise ReferenceError(
            f"no such file: {path}\n"
            "Export your auction values and drop the file in data/reference/."
        )

    grid = _read_grid(path)
    header_index = _find_header(grid)
    if header_index is None:
        raise ReferenceError(
            f"{path}: could not find a header row in the first {HEADER_SEARCH_LINES} lines.\n"
            "Expected a row containing a player-name column (Player / Name) plus at "
            "least one of: Value, Auction Value, $, Rank, Pos, Team.\n"
            f"First line seen: {_preview(grid)}"
        )

    mapping = schema.map_headers(grid[header_index])
    columns = tuple(sorted(set(mapping.values())))

    missing = [c for c in schema.REQUIRED if c not in columns]
    if missing:  # pragma: no cover - _find_header already requires a name column
        raise ReferenceError(f"{path}: missing required column(s): {', '.join(missing)}")

    rows: list[ReferenceRow] = []
    dropped: list[tuple[int, str]] = []
    seen: dict[str, int] = {}
    warnings: list[str] = []

    for offset, raw_row in enumerate(grid[header_index + 1:]):
        lineno = header_index + 2 + offset
        if not any(str(cell).strip() for cell in raw_row):
            continue
        try:
            row = _build_row(raw_row, mapping)
        except _RowProblem as problem:
            dropped.append((lineno, str(problem)))
            continue

        if row.key in seen:
            dropped.append((lineno, f"duplicate of {row.name!r} on line {seen[row.key]}"))
            continue
        seen[row.key] = lineno
        rows.append(row)

    if not rows:
        raise ReferenceError(
            f"{path}: found a header on line {header_index + 1} but no usable rows below it."
        )

    if AUCTION_VALUE not in columns:
        warnings.append(
            "no auction-value column found; values will be derived from rank, which "
            "is much rougher. Export with a Value/$ column if you can."
        )
    priced = sum(1 for r in rows if r.auction_value is not None)
    if priced and priced < len(rows) * 0.5:
        warnings.append(f"only {priced}/{len(rows)} rows have a value — check the export settings")

    return LoadReport(
        source=path,
        rows=tuple(rows),
        dropped=tuple(dropped),
        warnings=tuple(warnings),
        header_line=header_index + 1,
        columns=columns,
    )


class _RowProblem(Exception):
    """Internal: this one row is unusable. Never escapes the loader."""


def _find_header(grid: Sequence[Sequence[str]]) -> int | None:
    for index, row in enumerate(grid[:HEADER_SEARCH_LINES]):
        if schema.looks_like_header(row):
            return index
    return None


def _build_row(raw_row: Sequence[Any], mapping: dict[int, str]) -> ReferenceRow:
    values: dict[str, str] = {}
    for index, column in mapping.items():
        if index < len(raw_row):
            values[column] = str(raw_row[index] or "").strip()

    cell = schema.parse_player_cell(values.get(PLAYER_NAME, ""))
    if not cell.name:
        raise _RowProblem("no player name")

    # Explicit columns win over anything parsed out of a combined cell — if the
    # sheet bothered to give us a Pos column, that's the authoritative one.
    position = _position(values.get(POSITION)) or cell.position
    if position is None:
        raise _RowProblem(f"no position for {cell.name!r}")

    return ReferenceRow(
        key=normalize_player_key(cell.name),
        name=cell.name,
        position=position,
        nfl_team=(values.get(NFL_TEAM) or cell.nfl_team or "").upper() or None,
        auction_value=schema.parse_money(values.get(AUCTION_VALUE)),
        overall_rank=_int(values.get(OVERALL_RANK)),
        positional_rank=_int(values.get(POSITIONAL_RANK)) or cell.positional_rank,
        adp=_float(values.get(ADP)),
        projected_points=_float(values.get(PROJECTED_POINTS)),
        tier=_int(values.get(TIER)),
        bye_week=_int(values.get(BYE_WEEK)),
        espn_player_id=values.get(ESPN_PLAYER_ID) or None,
    )


def _position(raw: str | None) -> Position | None:
    if not raw:
        return None
    try:
        return Position.parse(raw)
    except ValueError:
        return None


def _int(raw) -> int | None:
    value = schema.parse_money(raw)
    return int(value) if value is not None else None


def _float(raw) -> float | None:
    return schema.parse_money(raw)


def _preview(grid: Sequence[Sequence[str]]) -> str:
    return " | ".join(str(c) for c in (grid[0] if grid else []))[:120] or "(file is empty)"
