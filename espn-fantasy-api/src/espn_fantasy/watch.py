"""Polling the draft room and reporting only what changed.

This is the layer between "a snapshot of the board" and "something happened".
It has no opinion about what you do with the news — it emits plain records, so
a consumer can feed a spreadsheet, a bot, a database, or nothing at all.

Three behaviours here are load-bearing on draft day and are the reason this
isn't a five-line `while True` loop:

- **Diffs are keyed on the player, not the grid index.** ESPN pre-creates the
  whole board, so the cell count never changes; and a board that re-renders or
  reorders must not replay all eighty sales as fresh news.
- **A failed snapshot degrades rather than raises.** A blip — a re-render, a
  tab briefly backgrounded — must not take the tool down mid-draft.
- **But it does not retry forever.** A reader that fails `max_failures` times
  running is not blipping, it is gone: the tab was closed, or Chrome exited. A
  loop that silently never yields again would hide exactly that.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Iterator, Mapping, Sequence

from espn_fantasy.draftroom import (
    DraftRoomReader,
    RoomNomination,
    RoomPick,
    RoomSnapshot,
)
from espn_fantasy.names import normalize_team_name


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Health(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class Status:
    health: Health = Health.OK
    detail: str = ""


@dataclass(frozen=True)
class Sale:
    """A player appeared on the board as sold.

    `price` is `None` when the board rendered the cell before its winning
    price settled. That is a real state and is reported as unknown rather than
    as zero, so a consumer can fill it in when the number lands.
    """

    player: str
    key: str
    team_id: int
    team_name: str
    price: int | None = None
    position: str | None = None
    roster_slot: str | None = None
    pro_team: str | None = None
    is_mine: bool = False
    at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class Nominated:
    """A player is up for bid.

    `current_bid` is a bid in progress, not an outcome. It is deliberately not
    reported as a price — the sale price comes from the completed pick, and
    conflating them means a later real number gets overwritten by one that was
    never paid.
    """

    player: str
    key: str
    position: str | None = None
    pro_team: str | None = None
    current_bid: int | None = None
    at: datetime = field(default_factory=_now)


Event = Sale | Nominated


def diff_picks(
    previous: RoomSnapshot | None, current: RoomSnapshot
) -> tuple[RoomPick, ...]:
    """Picks present now that were not present before."""
    seen = {p.key for p in previous.picks} if previous else set()
    return tuple(p for p in current.picks if p.key not in seen)


class TeamResolver:
    """Board column -> ESPN team id.

    This is the single most dangerous mapping in the live path, and the
    obvious implementation is wrong. The draft room's DOM carries **no team id
    anywhere**, and its column order is draft order, not id order — the league
    this was built against has its team 3 sitting in column 1. So `column + 1`
    credits picks to the wrong manager, and in a league with an id gap it
    invents a team 6 that does not exist while never mentioning team 13.

    The only join available is the team name, which ESPN reports identically
    in `mTeam` and on the board. Names are not identity, so a miss is never
    guessed around: it falls back to the ordered league ids and records a
    warning the caller is expected to surface.
    """

    def __init__(
        self,
        team_names: Mapping[int, str] | None = None,
        team_ids: Sequence[int] = (),
    ) -> None:
        self._by_name = {
            normalize_team_name(name): team_id
            for team_id, name in (team_names or {}).items()
            if normalize_team_name(name)
        }
        self._ids = tuple(team_ids)
        self.unmatched: set[str] = set()

    def resolve(self, snapshot: RoomSnapshot, pick: RoomPick) -> int:
        board_name = snapshot.team_name(pick.team_index)
        matched = self._by_name.get(normalize_team_name(board_name))
        if matched is not None:
            return matched

        if board_name:
            self.unmatched.add(board_name)
        if 0 <= pick.team_index < len(self._ids):
            return self._ids[pick.team_index]
        return pick.team_index + 1

    def audit(self, snapshot: RoomSnapshot) -> tuple[dict[str, int], list[str]]:
        """Check every board team against the league, before a draft starts.

        Discovering a rename at pick 40 is far worse than at pick 0: by then
        forty picks are credited to whoever happened to sit in that column.
        Run this at attach time, while the fix is still cheap.

        Deliberately does not mutate `unmatched` — a pre-flight report should
        not leave the resolver already in a warning state.
        """
        matched: dict[str, int] = {}
        unmatched: list[str] = []
        for team in snapshot.teams:
            team_id = self._by_name.get(normalize_team_name(team.name))
            if team_id is None:
                unmatched.append(team.name)
            else:
                matched[team.name] = team_id
        return matched, unmatched

    @property
    def warning(self) -> str | None:
        if not self.unmatched:
            return None
        if not self._by_name:
            # Nothing was ever supplied to match against, so "could not match"
            # would send someone hunting for a rename that never happened.
            return (
                "no league team data was supplied, so picks are credited by "
                "board column order. Column order is draft order, not id "
                "order — pass team names from mTeam to get real team ids."
            )
        return (
            "could not match these draft-room teams to a league team id: "
            + ", ".join(sorted(self.unmatched))
            + ". Their picks are credited by board position, which may be "
            "wrong — re-read mTeam if a team was renamed."
        )


class DraftRoomWatcher:
    """Polls the draft room and yields what changed."""

    def __init__(
        self,
        reader: DraftRoomReader,
        *,
        interval: float = 2.0,
        max_failures: int = 10,
        resolver: TeamResolver | None = None,
    ) -> None:
        self._reader = reader
        self._resolver = resolver or TeamResolver()
        self._interval = interval
        self._max_failures = max_failures
        self._failures = 0
        self._previous: RoomSnapshot | None = None
        self._nominated_key: str | None = None
        self._warned_unmatched = False
        self._status = Status()

    @property
    def status(self) -> Status:
        return self._status

    @property
    def snapshot(self) -> RoomSnapshot | None:
        """The most recent snapshot, for callers wanting board-wide state."""
        return self._previous

    def poll(self) -> list[Event]:
        """One read of the board. Returns the events it produced.

        Raises nothing: a failed read sets the status to DEGRADED (or STOPPED
        once the failures stop looking like a blip) and returns no events.
        """
        try:
            snapshot = self._reader.snapshot()
        except Exception as exc:  # noqa: BLE001 - degrade, never die
            self._failures += 1
            if self._failures >= self._max_failures:
                self._status = Status(
                    Health.STOPPED,
                    f"giving up after {self._failures} consecutive failures — is "
                    f"the draft room tab still open? Last error: "
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                self._status = Status(
                    Health.DEGRADED,
                    f"snapshot failed ({self._failures}/{self._max_failures}): "
                    f"{type(exc).__name__}: {exc}",
                )
            return []

        self._failures = 0
        self._status = Status(Health.OK, f"{snapshot.filled} pick(s) on the board")

        events: list[Event] = []

        # Nomination first: the board shows the new player on the block before
        # the previous sale clears, and the news is worth more the earlier it
        # lands.
        nomination = snapshot.nominated
        if nomination is not None and nomination.key != self._nominated_key:
            self._nominated_key = nomination.key
            events.append(_nominated(nomination))
        elif nomination is None:
            self._nominated_key = None

        for pick in diff_picks(self._previous, snapshot):
            events.append(_sale(pick, snapshot, self._resolver))
        self._previous = snapshot

        # Surface a bad team match once. Silently miscrediting picks is the
        # failure the resolver exists to prevent, so it must not stay quiet.
        if not self._warned_unmatched and self._resolver.warning:
            self._warned_unmatched = True
            self._status = Status(Health.DEGRADED, self._resolver.warning)

        return events

    def events(self, *, sleep=time.sleep) -> Iterator[Event]:
        """Poll on an interval, yielding events until the reader gives up."""
        while self._status.health is not Health.STOPPED:
            for event in self.poll():
                yield event
            if self._status.health is Health.STOPPED:
                return
            sleep(self._interval)

    def stop(self) -> None:
        self._status = Status(Health.STOPPED, "stopped by caller")


def _sale(pick: RoomPick, snapshot: RoomSnapshot, resolver: TeamResolver) -> Sale:
    return Sale(
        player=pick.player,
        key=pick.key,
        team_id=resolver.resolve(snapshot, pick),
        team_name=snapshot.team_name(pick.team_index),
        price=pick.price,
        position=pick.position,
        roster_slot=pick.roster_slot,
        pro_team=pick.pro_team,
        is_mine=pick.is_mine,
    )


def _nominated(nomination: RoomNomination) -> Nominated:
    return Nominated(
        player=nomination.player,
        key=nomination.key,
        position=nomination.position,
        pro_team=nomination.pro_team,
        current_bid=nomination.current_bid,
    )
