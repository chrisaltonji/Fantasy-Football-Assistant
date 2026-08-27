"""Turn draft-room snapshots into events, through the normal ingest seam.

Satisfies the same `EventSource` Protocol as `ManualSource` and `SimSource`.
Nothing downstream — the store, the reducers, the projections, the advisory
layer — knows the events came from a browser rather than an API.

The diffing rule is deliberately the same one the ESPN REST adapter would have
used: **a pick is news when its player first appears**, not when the cell count
changes. ESPN pre-creates the whole board, so cell count never moves.
"""

from __future__ import annotations

import queue
from typing import Iterator, Mapping, Sequence

from ffa.domain.enums import Position, Provenance
from ffa.domain.events import BaseEvent, PlayerNominated, PlayerSold
from ffa.domain.ids import PlayerRef
from ffa.domain.sourced import known, unknown
from ffa.ingest.espn.draftroom import (
    DraftRoomReader,
    RoomNomination,
    RoomPick,
    RoomSnapshot,
)
from ffa.ingest.source import BidObservation, SourceHealth, SourceStatus
from ffa.util.clock import now_utc


def _position(raw: str | None) -> Position | None:
    if not raw:
        return None
    try:
        return Position.parse(raw)
    except Exception:  # noqa: BLE001 - an unknown position is not fatal
        return None


def diff_picks(
    previous: RoomSnapshot | None, current: RoomSnapshot
) -> tuple[RoomPick, ...]:
    """Picks present now that were not present before.

    Keyed on the normalized player key rather than the grid index: a board that
    re-renders or reorders must not replay every pick as new.
    """
    seen = {p.key for p in previous.picks} if previous else set()
    return tuple(p for p in current.picks if p.key not in seen)


def normalize_team_name(raw: str | None) -> str:
    """Fold a team name for matching. Case and punctuation are noise."""
    text = "".join(c.lower() for c in (raw or "") if c.isalnum() or c.isspace())
    return " ".join(text.split())


class TeamResolver:
    """Board column -> ESPN team id.

    This is the single most dangerous mapping in the live path, and the obvious
    implementation is wrong. The draft room's DOM carries **no team id
    anywhere**, and its column order is draft order, not id order — the real
    league's team 3 sits in column 1. So `column + 1` credits picks to the
    wrong manager, and in a league with an id gap it invents a team 6 that does
    not exist while never mentioning team 13.

    The only join available is the team name, which ESPN reports identically in
    `mTeam` and on the board. Names are explicitly not identity in this
    codebase, so a miss is never guessed around: it falls back to the ordered
    league ids and records a warning the caller must surface.
    """

    def __init__(self, team_names: Mapping[int, str] | None = None,
                 team_ids: Sequence[int] = ()) -> None:
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
        """Check every board team against the config, before a draft starts.

        Discovering a rename at pick 40 is far worse than at pick 0: by then
        forty picks are attributed to whoever happened to sit in that column,
        and unpicking it means undoing and retyping under the clock. This runs
        at attach time, while the fix is still one command.

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
        return (
            "could not match these draft-room teams to a league team id: "
            + ", ".join(sorted(self.unmatched))
            + ". Picks for them are attributed by board position, which may be "
            "wrong — check with `budgets`, and re-run `ffa config init` if a "
            "team was renamed."
        )


def events_for(
    pick: RoomPick,
    snapshot: RoomSnapshot,
    resolver: "TeamResolver | None" = None,
) -> BaseEvent:
    """One completed cell becomes one `PlayerSold`.

    A missing price is recorded as an explicit unknown rather than skipped or
    zeroed. The board can render a cell before its price settles, and `merge`
    is built to let the real number land later — whereas a zero would be a
    known value and would win.
    """
    at = now_utc()
    price = (
        known(pick.price, Provenance.ESPN_API, at)
        if pick.price is not None
        else unknown(at, note="draft room showed no winning price")
    )
    position = _position(pick.position)
    resolver = resolver or TeamResolver()
    return PlayerSold(
        at=at,
        source=Provenance.ESPN_API.value,
        player=PlayerRef.from_raw(pick.player),
        team=known(resolver.resolve(snapshot, pick), Provenance.ESPN_API, at),
        price=price,
        position=known(position, Provenance.ESPN_API, at) if position else None,
    )


def nomination_event(nomination: RoomNomination) -> BaseEvent:
    """The player on the block becomes a `PlayerNominated`.

    This is what makes bid guidance fire at the moment it is worth anything.
    Without it the engine only ever learns about *completed* sales, and
    `advice` with no argument has nothing to talk about — which is exactly how
    it behaved before this existed.

    The current bid is deliberately *not* recorded as the sale price. It is a
    bid in progress, not an outcome, and the price event has to come from the
    completed pick or a later observation would be overwritten by a number
    that was never paid.

    **`nominated_by` is deliberately left unset here, and wiring it is not the
    obvious one-liner it looks like.** The board does carry a `--selecting`
    modifier and `RoomTeam.is_nominating` already parses it, so the temptation is
    to hand that straight over. What is *not* established is what the modifier
    means while bidding is running: the seat that put this player up, or the seat
    due to put the next one up. Those are different teams, and picking wrong
    would make `NominationPlan.disagreement` — which exists to catch a stale
    draft order — fire on almost every pick and cry wolf about a correct one.

    The manual path sets `nominated_by` because the user typed a name, and that
    is a real observation. This one stays silent until a rehearsal settles it:
    `tools/draft_watch.py` already flashes `is_nominating` on change, so watching
    one nomination land answers it with no new code.
    """
    at = now_utc()
    return PlayerNominated(
        at=at,
        source=Provenance.ESPN_API.value,
        player=PlayerRef.from_raw(nomination.player),
        opening_bid=(
            known(nomination.current_bid, Provenance.ESPN_API, at)
            if nomination.current_bid is not None
            else None
        ),
    )


class DraftRoomSource:
    """Polls the draft room and emits what changed."""

    name = "espn-draftroom"

    def __init__(self, reader: DraftRoomReader, *, interval: float = 2.0,
                 max_failures: int = 10,
                 resolver: "TeamResolver | None" = None) -> None:
        self._reader = reader
        self._resolver = resolver or TeamResolver()
        self._warned_unmatched = False
        self._interval = interval
        self._max_failures = max_failures
        self._failures = 0
        self._previous: RoomSnapshot | None = None
        self._nominated_key: str | None = None
        # The last price seen on the current nomination, so a re-poll that shows
        # the same number stays silent. Reset with the nomination, not carried.
        self._last_bid: int | None = None
        self._health = SourceHealth.OK
        self._detail = ""
        self._emitted = 0

    def start(self, out: "queue.Queue[BaseEvent]") -> None:
        for event in self.events():
            out.put(event)

    def events(self) -> Iterator[BaseEvent]:
        """Yield events until stopped.

        A failed snapshot degrades rather than raising: on draft day a blip —
        a re-render, a tab briefly backgrounded — must not take the tool down.
        The status goes DEGRADED so the surface can say so, and polling
        continues.

        But it does not retry forever. A reader that fails `max_failures` times
        running is not blipping, it is gone — the tab was closed, or Chrome
        exited — and a loop that never yields would hang the caller and hide
        the fact. It stops and says which.
        """
        import time

        while self._health is not SourceHealth.STOPPED:
            try:
                snapshot = self._reader.snapshot()
                self._health = SourceHealth.OK
                self._failures = 0
                self._detail = f"{snapshot.filled} pick(s) on the board"
            except Exception as exc:  # noqa: BLE001 - degrade, never die
                self._failures += 1
                self._detail = (
                    f"snapshot failed ({self._failures}/{self._max_failures}): "
                    f"{type(exc).__name__}: {exc}"
                )
                if self._failures >= self._max_failures:
                    self._health = SourceHealth.STOPPED
                    self._detail = (
                        f"giving up after {self._failures} consecutive failures — "
                        f"is the draft room tab still open? Last error: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    return
                self._health = SourceHealth.DEGRADED
                time.sleep(self._interval)
                continue

            # Nomination first: the board shows the new player on the block
            # before the previous sale clears, and guidance is worth more the
            # earlier it lands.
            nomination = snapshot.nominated
            if nomination is not None and nomination.key != self._nominated_key:
                self._nominated_key = nomination.key
                self._last_bid = nomination.current_bid
                self._emitted += 1
                yield nomination_event(nomination)
            elif nomination is None:
                self._nominated_key = None
                self._last_bid = None
            elif nomination.current_bid != self._last_bid:
                # **Same player, higher price.** Until now this branch did not
                # exist and the number was simply dropped: the gate above fires
                # once per player, so every raise after the opening bid was
                # scraped, parsed, and thrown away.
                #
                # It is yielded as a `BidObservation`, not an event, and the
                # distinction is the whole design — see that class. Nothing
                # downstream folds it, `_emitted` does not count it, and a draft
                # run with the assistant off never sees it at all.
                self._last_bid = nomination.current_bid
                yield BidObservation(
                    player_key=nomination.key,
                    price=nomination.current_bid,
                    at=now_utc().isoformat(),
                )

            for pick in diff_picks(self._previous, snapshot):
                self._emitted += 1
                yield events_for(pick, snapshot, self._resolver)
            self._previous = snapshot

            # Surface a bad team match once, through the status line the REPL
            # already shows. Silently misattributing picks is the failure this
            # whole resolver exists to prevent, so it must not stay quiet.
            if not self._warned_unmatched and self._resolver.warning:
                self._warned_unmatched = True
                self._detail = self._resolver.warning
                self._health = SourceHealth.DEGRADED

            if self._health is SourceHealth.STOPPED:
                return
            time.sleep(self._interval)

    def stop(self) -> None:
        self._health = SourceHealth.STOPPED

    def status(self) -> SourceStatus:
        return SourceStatus(name=self.name, health=self._health, detail=self._detail)
