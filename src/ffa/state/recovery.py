"""Finding, naming, and repairing run directories.

A run directory is `runs/<draft_id>/` holding `events.jsonl` and a `.lock`.
`draft_id` is `<year>-<league_id>-<UTC timestamp>` — sortable, unique, and
readable, which a UUID is not. You will be reading these names under time
pressure.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ffa.state.journal import JOURNAL_NAME, read_events
from ffa.util.clock import now_utc

RUNS_DIR = Path("runs")


def make_draft_id(league_id: int, year: int, moment: datetime | None = None) -> str:
    stamp = (moment or now_utc()).strftime("%Y%m%dT%H%M%S")
    return f"{year}-{league_id}-{stamp}"


def run_dir(draft_id: str, base: Path = RUNS_DIR) -> Path:
    return base / draft_id


def journal_path(draft_id: str, base: Path = RUNS_DIR) -> Path:
    return run_dir(draft_id, base) / JOURNAL_NAME


def list_runs(base: Path = RUNS_DIR, *, league_id: int | None = None, year: int | None = None):
    """Run directories, newest first by journal mtime."""
    if not base.is_dir():
        return []

    prefix = f"{year}-{league_id}-" if (league_id is not None and year is not None) else ""
    candidates = [
        d for d in base.iterdir()
        if d.is_dir() and (d / JOURNAL_NAME).is_file() and d.name.startswith(prefix)
    ]
    return sorted(candidates, key=lambda d: (d / JOURNAL_NAME).stat().st_mtime, reverse=True)


def latest_run(base: Path = RUNS_DIR, *, league_id: int | None = None, year: int | None = None):
    runs = list_runs(base, league_id=league_id, year=year)
    return runs[0] if runs else None


def repair_torn_tail(path: Path) -> Path | None:
    """Truncate an incomplete trailing line, after backing the file up.

    Without this the fragment sits in the file re-warning on every future load,
    and — worse — the next append would land on the same line, welding a
    corrupt record to a good one. Returns the backup path if anything changed.
    """
    if not path.is_file():
        return None

    raw = path.read_bytes()
    if not raw or raw.endswith(b"\n"):
        return None

    keep = raw.rfind(b"\n")
    backup = path.with_suffix(path.suffix + f".corrupt-{now_utc().strftime('%Y%m%dT%H%M%S')}")
    backup.write_bytes(raw)
    path.write_bytes(raw[: keep + 1] if keep >= 0 else b"")
    return backup


def load_run(path: Path):
    """Read a run's journal, repairing a torn tail first. Returns (events, warnings)."""
    journal = path / JOURNAL_NAME if path.is_dir() else path
    warnings: list[str] = []

    backup = repair_torn_tail(journal)
    if backup is not None:
        warnings.append(
            f"recovered from an interrupted write; the original was copied to {backup}"
        )

    events, read_warnings = read_events(journal)
    return events, warnings + read_warnings
