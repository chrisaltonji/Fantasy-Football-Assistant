"""Read a live ESPN auction draft out of the draft room's DOM.

Why this exists, and why it is not an HTTP client:

ESPN's draft room is driven by a Server-Sent Events stream at
`fantasydraft.espn.com/game-1/league-<id>/sse/JOIN`. Three facts, all observed
on 2026-08-17 against a live practice draft, rule out talking to it directly:

1. The JOIN token is minted at runtime by the page's own JavaScript and is
   **single-use** — replaying it is refused.
2. ESPN allows **one connection per team**. A second connection evicts the
   first with a "Duplicate Connection" notice. A headless client would kick you
   out of your own draft.
3. The REST view (`mDraftDetail`) is **not written while a draft runs**. Sixty
   polls over fifteen minutes of live drafting returned `0/180` filled picks
   with `inProgress: true` throughout.

So the only way to watch a draft without disrupting it is to read the page you
are already drafting in. This module attaches to a Chrome you started with
`--remote-debugging-port` and reads the rendered board. One connection — yours.

The DOM is a genuine structured feed, not a text scrape:

    <div class="draft-board-grid-pick-cell completedPick myTeam">
      <div class="pickCellTop">
        <div class="rosterSlot">RB</div>
        <div class="winningPrice">$76</div>
      </div>
      <div class="pickCellMiddle">
        <span class="playerFirstName">Jahmyr</span>
        <span class="playerLastName">Gibbs</span>
      </div>
      ...

`innerText` on those cells is empty — the layout is CSS-driven — so everything
here queries elements and never parses rendered text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

CDP_DEFAULT_PORT = 9222
DRAFT_URL_FRAGMENT = "fantasy.espn.com/football/draft"


class DraftRoomError(Exception):
    """A problem the user can act on, printed without a traceback."""


# --- what we read -----------------------------------------------------------


@dataclass(frozen=True)
class RoomTeam:
    """One team's row in the auction panel."""

    index: int
    name: str
    cash: int | None
    bid: int | None
    is_auto: bool = False
    is_me: bool = False
    is_nominating: bool = False


@dataclass(frozen=True)
class RoomPick:
    """One completed cell on the draft board."""

    grid_index: int
    team_index: int
    player: str
    price: int | None
    position: str | None
    roster_slot: str | None
    pro_team: str | None
    is_mine: bool = False

    @property
    def key(self) -> str:
        from ffa.domain.ids import normalize_player_key

        return normalize_player_key(self.player)


@dataclass(frozen=True)
class RoomNomination:
    """The player currently up for bid."""

    player: str
    position: str | None = None
    pro_team: str | None = None
    current_bid: int | None = None

    @property
    def key(self) -> str:
        from ffa.domain.ids import normalize_player_key

        return normalize_player_key(self.player)


@dataclass(frozen=True)
class RoomSnapshot:
    """Everything the board says at one instant."""

    teams: tuple[RoomTeam, ...] = ()
    picks: tuple[RoomPick, ...] = ()
    slots_per_team: int = 0
    pick_header: str | None = None
    clock: str | None = None
    nominated: "RoomNomination | None" = None

    @property
    def filled(self) -> int:
        return len(self.picks)

    def team_name(self, index: int) -> str:
        for team in self.teams:
            if team.index == index:
                return team.name
        return f"team{index + 1}"


# --- extraction -------------------------------------------------------------

# Runs inside the page. Returns raw strings only; all coercion happens in
# Python, where it is testable without a browser.
SNAPSHOT_JS = r"""
() => {
  const t = (el) => el ? (el.textContent || "").trim() : null;

  const headers = [...document.querySelectorAll(".draft-board-grid-header-cell")]
    .map(h => t(h));

  const teams = [...document.querySelectorAll(".auction-pick-component")].map((el, i) => ({
    index: i,
    name: t(el.querySelector(".team-name")),
    cash: t(el.querySelector(".cash")),
    bid: t(el.querySelector(".bid-amount")),
    auto: el.className.includes("autopick"),
    own: el.className.includes("--own"),
    selecting: el.className.includes("--selecting"),
  }));

  const cells = [...document.querySelectorAll(".draft-board-grid-pick-cell")];
  const picks = [];
  cells.forEach((c, i) => {
    const first = t(c.querySelector(".playerFirstName"));
    const last = t(c.querySelector(".playerLastName"));
    if (!first && !last) return;
    picks.push({
      gridIndex: i,
      player: [first, last].filter(Boolean).join(" "),
      price: t(c.querySelector(".winningPrice")),
      slot: t(c.querySelector(".rosterSlot")),
      position: t(c.querySelector(".positionPill")),
      proTeam: t(c.querySelector(".playerProTeam")),
      mine: c.className.includes("myTeam"),
    });
  });

  // The player currently up for bid. This is the moment bid guidance is
  // worth anything, so it is read every poll rather than inferred from sales.
  const nomName = t(document.querySelector(".player-selected .playerinfo__playername"));
  const nominated = nomName ? {
    player: nomName,
    position: t(document.querySelector(".player-selected .playerinfo__playerpos")),
    proTeam: t(document.querySelector(".player-selected .playerinfo__playerteam")),
    currentBid: t(document.querySelector(".current-amount")),
  } : null;

  const body = document.body ? document.body.innerText : "";
  const pickHeader = (body.match(/PK\s+\d+\s+OF\s+\d+/i) || [])[0] || null;
  const clock = (body.match(/\b\d{1,2}:\d{2}\b/) || [])[0] || null;

  return { headers, teams, picks, nominated, cellCount: cells.length, pickHeader, clock };
}
"""


def _money(raw: Any) -> int | None:
    """`"$76"` -> 76. `"$null"`, `""`, `None` -> None.

    ESPN renders an absent bid as the literal string `$null`, which is why this
    cannot simply strip non-digits and call `int` — that would turn "no bid"
    into a parse error, and later into a zero, which is a real number and would
    be merged as one.
    """
    if raw is None:
        return None
    text = str(raw).strip().lstrip("$").replace(",", "")
    if not text or text.lower() == "null":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _team_label(raw: str | None, index: int) -> str:
    """Panel names arrive as `"3. Fighting Finkelsteins"`. Drop the ordinal."""
    text = (raw or "").strip()
    if not text:
        return f"team{index + 1}"
    head, sep, tail = text.partition(".")
    if sep and head.strip().isdigit():
        return tail.strip() or f"team{index + 1}"
    return text


def parse_snapshot(raw: dict[str, Any]) -> RoomSnapshot:
    """Turn the page's raw dict into a typed snapshot. Pure; no browser."""
    raw_teams: Sequence[dict[str, Any]] = raw.get("teams") or []
    headers: Sequence[str] = [h for h in (raw.get("headers") or []) if h]

    teams = tuple(
        RoomTeam(
            index=int(entry.get("index", i)),
            name=_team_label(entry.get("name") or (headers[i] if i < len(headers) else None), i),
            cash=_money(entry.get("cash")),
            bid=_money(entry.get("bid")),
            is_auto=bool(entry.get("auto")),
            is_me=bool(entry.get("own")),
            is_nominating=bool(entry.get("selecting")),
        )
        for i, entry in enumerate(raw_teams)
    )

    # The board is one flat grid of `team_count * slots` cells, laid out a whole
    # team at a time. Deriving the stride rather than hardcoding 15 keeps this
    # correct for any roster size.
    cell_count = int(raw.get("cellCount") or 0)
    slots = (cell_count // len(teams)) if teams else 0

    picks = tuple(
        RoomPick(
            grid_index=int(entry["gridIndex"]),
            team_index=(int(entry["gridIndex"]) // slots) if slots else 0,
            player=str(entry.get("player") or "").strip(),
            price=_money(entry.get("price")),
            position=(entry.get("position") or None),
            roster_slot=(entry.get("slot") or None),
            pro_team=(entry.get("proTeam") or None),
            is_mine=bool(entry.get("mine")),
        )
        for entry in (raw.get("picks") or [])
        if str(entry.get("player") or "").strip()
    )

    raw_nom = raw.get("nominated") or None
    nominated = None
    if isinstance(raw_nom, dict) and str(raw_nom.get("player") or "").strip():
        nominated = RoomNomination(
            player=str(raw_nom["player"]).strip(),
            position=(raw_nom.get("position") or None),
            pro_team=(raw_nom.get("proTeam") or None),
            # "Current offer: $36" -> 36. Absent before the first bid lands.
            current_bid=_money(raw_nom.get("currentBid", "").split("$")[-1]
                               if isinstance(raw_nom.get("currentBid"), str) else None),
        )

    return RoomSnapshot(
        teams=teams,
        picks=picks,
        slots_per_team=slots,
        pick_header=raw.get("pickHeader"),
        clock=raw.get("clock"),
        nominated=nominated,
    )


# --- the browser side -------------------------------------------------------


@dataclass
class DraftRoomReader:
    """Attaches to a running Chrome and snapshots the draft room on demand.

    Deliberately never launches a browser and never navigates. It attaches to
    the page you are drafting in and reads it; anything else risks the
    duplicate-connection eviction described in the module docstring.
    """

    port: int = CDP_DEFAULT_PORT

    # Substring of the tab's URL, for the two-draft-rooms case below. Empty
    # means "there had better be exactly one".
    tab_match: str = ""

    _pw: Any = field(default=None, repr=False)
    _browser: Any = field(default=None, repr=False)
    _page: Any = field(default=None, repr=False)

    def connect(self) -> "DraftRoomReader":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:  # pragma: no cover - depends on the environment
            raise DraftRoomError(
                "playwright is not installed. Run:  python -m pip install playwright"
            ) from None

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(
                f"http://localhost:{self.port}"
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a plain message
            self._pw.stop()
            self._pw = None
            raise DraftRoomError(
                f"could not attach to Chrome on port {self.port}: {exc}\n"
                "Start it with:  python tools/draft_room_probe.py --launch"
            ) from None

        try:
            self._page = self._find_page()
        except DraftRoomError:
            self.close()
            raise
        if self._page is None:
            self.close()
            raise DraftRoomError(
                "attached, but no draft room tab is open in that Chrome.\n"
                f"Looked for a URL containing {DRAFT_URL_FRAGMENT!r}"
                + (f" and {self.tab_match!r}." if self.tab_match else ".")
            )
        return self

    def draft_tabs(self) -> tuple[str, ...]:
        """Every open tab that looks like a draft room, in browser order."""
        urls: list[str] = []
        for context in self._browser.contexts:
            for page in context.pages:
                url = page.url or ""
                if DRAFT_URL_FRAGMENT in url:
                    urls.append(url)
        return tuple(urls)

    def _find_page(self):
        """The one draft-room tab, or a refusal.

        Returning the *first* match is the tempting version and it is wrong.
        With two draft rooms open — the real league beside yesterday's practice
        draft, or a stale tab left over from a reload — the tab that gets read
        is whichever Chrome happens to list first. That failure is invisible:
        the attach succeeds, the pre-flight reports `matched 12/12` because
        both rooms belong to the same league, and the board simply never moves.
        It was hit during the rehearsal.

        So ambiguity is an error, never a coin flip. `tab_match` narrows it when
        you do want a specific one.
        """
        candidates = []
        for context in self._browser.contexts:
            for page in context.pages:
                url = page.url or ""
                if DRAFT_URL_FRAGMENT not in url:
                    continue
                if self.tab_match and self.tab_match.lower() not in url.lower():
                    continue
                candidates.append(page)

        if not candidates:
            return None
        if len(candidates) > 1:
            listed = "\n".join(f"  {p.url}" for p in candidates)
            raise DraftRoomError(
                f"{len(candidates)} draft-room tabs are open in that Chrome, "
                "and reading the wrong one looks exactly like a draft that "
                "never starts:\n"
                f"{listed}\n"
                "Close the ones you are not drafting in, or single one out with "
                "--tab <text from its URL>."
            )
        return candidates[0]

    def snapshot_raw(self) -> dict[str, Any]:
        """The untouched dict `SNAPSHOT_JS` returned.

        Exposed so a caller wanting both the raw and the parsed view can derive
        them from *one* evaluate. Two evaluates would sample the board at two
        different instants, and a pick landing between them would look like a
        raw/parsed disagreement that is not real.

        Connects on first use if it has to. **Playwright's sync API is
        greenlet-based and not thread-safe**: a connection made on one thread
        raises on every call from another ("Current/Expected greenlet"). The
        live draft loop polls from a worker thread, so the connection has to be
        established *there*, which is what this lazy connect is for. Connecting
        eagerly on the main thread and handing the object over looks fine and
        fails on every single poll.
        """
        if self._page is None:
            self.connect()
        return self._page.evaluate(SNAPSHOT_JS)

    def snapshot(self) -> RoomSnapshot:
        return parse_snapshot(self.snapshot_raw())

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
        self._page = None

    def __enter__(self) -> "DraftRoomReader":
        return self.connect()

    def __exit__(self, *_exc) -> None:
        self.close()
