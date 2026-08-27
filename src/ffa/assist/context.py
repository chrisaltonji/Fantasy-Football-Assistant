"""What the agents said, so they can read each other.

Five agents, one assistant. The Narrator needs the Room's earlier estimate to say
"he went $47, eight over what we expected"; the Grader needs every read against
its outcome so it can score the assistant itself. That shared memory lives here.

**This is not the journal, and its address is a trap.**

It sits in `runs/<draft_id>/` because that is where the draft lives, and that
adjacency is exactly how a future reader talks themselves into trusting it. So,
plainly: `events.jsonl` is ground truth, fsynced, replayed, and the source of
every number the tool asserts. `assist.jsonl` is opinions. Nothing in `state/`,
`domain/` or `advice/` may read it. A missing, truncated or corrupt sidecar is a
shrug — `replay()` produces a byte-identical draft without it, and there is a test
that runs the same journal with the file present, absent and full of garbage.

That asymmetry is the whole design. Losing inference costs nothing; losing the
journal is the end of the world. Storing them side by side is safe only while the
difference is written down.

**One writer, no lock.** `append()` is called only from the main thread, in the
`ASSIST` branch of the draft loop. Workers never touch this object — they are
handed prior reads as plain dicts at spawn time. Same structural argument that
makes `DraftStore` safe, and it is why there is no `threading.Lock` here to
forget to take.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

SIDECAR_NAME = "assist.jsonl"

# Every status a call can end in. `late` and `rejected` are recorded rather than
# discarded on purpose: a read that arrived after the player sold is still
# evidence about the assistant, and the Grader wants it.
STATUSES = ("ok", "failed", "skipped", "late", "rejected", "muted")


@dataclass(frozen=True)
class ReadRecord:
    """One agent call, and what became of it."""

    agent: str
    moment: str                 # the lookup key: "room:bijan-robinson:143"
    event_id: int = 0           # journal id at request time — the join to truth
    seq: int = 0                # assigned by the log, monotonic
    at: str = ""
    player_key: str = ""
    status: str = "ok"
    payload: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq, "at": self.at, "agent": self.agent,
            "moment": self.moment, "event_id": self.event_id,
            "player_key": self.player_key, "status": self.status,
            "payload": self.payload, "detail": self.detail,
            "usage": self.usage, "model": self.model,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ReadRecord":
        """Tolerant on the way in, like `OwnerDossier.from_dict`.

        A record written by a newer version with extra keys must not break an
        older reader, and a field that has gone missing is not worth losing the
        rest of the line over.
        """
        return cls(
            agent=str(raw.get("agent", "")),
            moment=str(raw.get("moment", "")),
            event_id=int(raw.get("event_id", 0) or 0),
            seq=int(raw.get("seq", 0) or 0),
            at=str(raw.get("at", "")),
            player_key=str(raw.get("player_key", "")),
            status=str(raw.get("status", "ok")),
            payload=raw.get("payload") or {},
            detail=str(raw.get("detail", "")),
            usage=raw.get("usage") or {},
            model=str(raw.get("model", "")),
        )

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class ReadLog:
    """Append-only, in memory, optionally mirrored to a sidecar.

    The in-memory list is the real store; the file is a convenience so `ffa
    grade` can run tomorrow against a draft that finished tonight. A write
    failure costs one warning and nothing else — see `append`.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._records: list[ReadRecord] = []
        self.warnings: list[str] = []
        self._write_failed = False

    # --- writing ------------------------------------------------------------

    def append(self, record: ReadRecord) -> ReadRecord:
        """Record one call. Main thread only. Returns the record with its seq."""
        stored = replace(record, seq=len(self._records) + 1)
        self._records.append(stored)
        self._persist(stored)
        return stored

    def _persist(self, record: ReadRecord) -> None:
        if self.path is None or self._write_failed:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")
                # Flushed but deliberately not fsynced. The journal earns that
                # cost because losing an event loses the draft; this is derived
                # prose, and paying journal-grade durability for it under a
                # clock would be the wrong trade.
                fh.flush()
        except OSError as exc:
            # Said once, then never again — a failing disk must not print a
            # line per nomination while you are trying to read a bid.
            self._write_failed = True
            self.warnings.append(
                f"could not write {self.path}: {exc}. The assistant still works; "
                "its history just will not survive this session."
            )

    # --- reading ------------------------------------------------------------

    def all(self) -> tuple[ReadRecord, ...]:
        return tuple(self._records)

    def since(self, seq: int) -> tuple[ReadRecord, ...]:
        return tuple(r for r in self._records if r.seq > seq)

    def for_player(self, player_key: str, agent: str = "") -> tuple[ReadRecord, ...]:
        return tuple(
            r for r in self._records
            if r.player_key == player_key and (not agent or r.agent == agent)
        )

    def latest(self, agent: str, player_key: str = "") -> ReadRecord | None:
        """The most recent successful read, which is what a later agent wants.

        Failures and rejections are kept in the log for the Grader but are never
        handed to another agent as context — passing on a rejected read would
        launder exactly the output the guard refused.
        """
        for record in reversed(self._records):
            if record.agent != agent or not record.ok:
                continue
            if player_key and record.player_key != player_key:
                continue
            return record
        return None

    # --- accounting ---------------------------------------------------------

    # The key the runner writes. It was `cost_cents` here and `cents` there, so
    # this returned 0 no matter what had been spent — invisible, because the
    # spend *cap* is fed separately by `settle()` and kept working. Only the
    # reported total was wrong. `test_the_ledger_reads_the_key_the_runner_writes`
    # pins the two together.
    # Micros, not cents, and for the same reason `SpendGuard` counts them: the
    # tick costs a third of a cent and is the most numerous call, so a ledger
    # summed in whole cents reported half again as much as was actually spent.
    COST_KEY = "micros"

    def spend_micros(self) -> int:
        return sum(int(r.usage.get(self.COST_KEY, 0) or 0) for r in self._records)

    def spend_dollars(self) -> float:
        return self.spend_micros() / 1_000_000

    def spend_cents(self) -> int:
        """Whole cents, for display. Never sum this — see `COST_KEY`."""
        return int(self.spend_micros() / 10_000)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for record in self._records:
            out[record.status] = out.get(record.status, 0) + 1
        return out

    def cache_hit_rate(self) -> float:
        """Share of input tokens served from cache.

        Worth surfacing rather than assuming: if this is zero after a few calls,
        something volatile has leaked into the prefix and every nomination is
        paying full price for five thousand tokens that were supposed to be free.
        """
        read = fresh = 0
        for record in self._records:
            read += int(record.usage.get("cache_read", 0) or 0)
            fresh += int(record.usage.get("input", 0) or 0)
        total = read + fresh
        return (read / total) if total else 0.0

    # --- loading ------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "ReadLog":
        """Read a sidecar back. Never raises — a bad line is skipped and named.

        Deliberately unlike `read_events`, which raises on a corrupt middle line
        because a wrong-but-plausible draft state is worse than refusing to
        start. Nothing here is load-bearing, so the useful behaviour is to
        salvage what parses and say what did not.
        """
        log = cls(path)
        if not path.is_file():
            return log
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.warnings.append(f"could not read {path}: {exc}")
            return log

        for number, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                log.warnings.append(f"{path}:{number} is not JSON; skipped")
                continue
            if not isinstance(raw, dict):
                log.warnings.append(f"{path}:{number} is not an object; skipped")
                continue
            log._records.append(ReadRecord.from_dict(raw))

        # Renumber so `seq` stays dense even if the file lost a line. The seq is
        # a position in the stream, not an identifier anything joins on — the
        # join to ground truth is `event_id`.
        log._records = [replace(r, seq=i) for i, r in enumerate(log._records, start=1)]
        return log


def moment_key(agent: str, player_key: str, event_id: int) -> str:
    """The lookup key. Stable, readable in the file, unique per call."""
    return f"{agent}:{player_key or '-'}:{event_id}"


def records_for_prompt(records: Iterable[ReadRecord], limit: int = 10) -> list[dict]:
    """Prior reads, trimmed to what another agent can use.

    Only successful ones, only the fields that carry meaning, newest last so the
    model reads them in the order they happened.
    """
    usable = [r for r in records if r.ok][-limit:]
    return [
        {"agent": r.agent, "at": r.at, "player": r.player_key, "said": r.payload}
        for r in usable
    ]
