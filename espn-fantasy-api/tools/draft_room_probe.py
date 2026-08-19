#!/usr/bin/env python3
"""Attach to a running Chrome and catalogue what the ESPN draft room exposes.

Why a browser at all: ESPN's draft room is driven by a Server-Sent Events
stream whose JOIN token is minted at runtime and is single-use, and ESPN allows
**one connection per team** — a second connection evicts the first with a
"Duplicate Connection" notice. So a headless HTTP client cannot sit alongside
you on draft day; it would kick you out of your own draft. The tool therefore
reads the page *you* are drafting in, over the DevTools protocol, and never
opens a connection of its own.

Setup, once:

    python tools/draft_room_probe.py --launch

That starts Chrome with remote debugging on a dedicated profile (Chrome
refuses the debug port on your default profile). Log into ESPN in that window;
the profile persists, so you only do it once.

Then, with a draft room open in that Chrome:

    python tools/draft_room_probe.py --catalog

which writes a structural dump of everything the page exposes — the input to
building a parser against, and the answer to "what data do we have access to".
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CDP_PORT = 9222
PROFILE_DIR = Path.home() / ".espn-fantasy" / "chrome-profile"

CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
)

DRAFT_URL_FRAGMENT = "fantasy.espn.com/football/draft"


class ProbeError(Exception):
    """Something the user can fix, printed without a traceback."""


def find_chrome() -> Path:
    for path in CHROME_CANDIDATES:
        if path.is_file():
            return path
    raise ProbeError(
        "could not find chrome.exe in the usual places. Pass --chrome PATH."
    )


def launch(chrome: Path, port: int, profile: Path) -> int:
    """Start Chrome with the debug port on a dedicated profile.

    A dedicated profile is not a preference. Chrome refuses
    `--remote-debugging-port` when pointed at the default user-data-dir, so
    attaching to your everyday profile is not possible; this one persists, so
    logging into ESPN here is a one-time cost.
    """
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://fantasy.espn.com/football/",
    ]
    print(f"launching {chrome.name} on port {port}")
    print(f"  profile: {profile}")
    subprocess.Popen(args, close_fds=True)
    print(
        "\nChrome is starting. If this is the first time:\n"
        "  1. Log into ESPN in that window\n"
        "  2. Open your league, then the draft room\n"
        "  3. Re-run with --catalog\n"
    )
    return 0


def attach(port: int):
    """Connect to the already-running Chrome. Never launches one."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(f"http://localhost:{port}")
    except Exception as exc:  # noqa: BLE001 - surfaced as a plain message
        pw.stop()
        raise ProbeError(
            f"could not attach to Chrome on port {port}: {exc}\n"
            "Start it first with:  python tools/draft_room_probe.py --launch"
        ) from None
    return pw, browser


def find_draft_page(browser):
    """The draft room tab, wherever it is among the open contexts."""
    for context in browser.contexts:
        for page in context.pages:
            if DRAFT_URL_FRAGMENT in (page.url or ""):
                return page
    return None


# --- the catalogue ----------------------------------------------------------

CATALOG_JS = r"""
() => {
  const text = document.body ? document.body.innerText : "";
  const pick = (re) => { const m = text.match(re); return m ? m[0] : null; };

  // Class-name census: the draft room is a React app, so the stable handles
  // are class fragments rather than ids. This is what a parser keys off.
  const classCounts = {};
  for (const el of document.querySelectorAll("*")) {
    const cls = typeof el.className === "string" ? el.className : "";
    for (const c of cls.split(/\s+/)) {
      if (!c) continue;
      if (/^(jsx|css)-/.test(c)) continue;
      classCounts[c] = (classCounts[c] || 0) + 1;
    }
  }
  const classes = Object.entries(classCounts)
    .filter(([c, n]) => /draft|pick|bid|team|player|budget|nominat|clock|timer|roster|money|auction/i.test(c))
    .sort((a, b) => b[1] - a[1])
    .slice(0, 60);

  // Tables are where the board and the team list live.
  const tables = [...document.querySelectorAll("table")].slice(0, 12).map(t => ({
    rows: t.rows.length,
    cols: t.rows[0] ? t.rows[0].cells.length : 0,
    headers: t.rows[0] ? [...t.rows[0].cells].map(c => c.innerText.trim()).slice(0, 12) : [],
    firstDataRow: t.rows[1] ? [...t.rows[1].cells].map(c => c.innerText.trim()).slice(0, 12) : [],
    className: typeof t.className === "string" ? t.className : "",
  }));

  return {
    url: location.href,
    title: document.title,
    textLength: text.length,
    pickHeader: pick(/PK\s+\d+\s+OF\s+\d+/i),
    clock: pick(/\b\d{1,2}:\d{2}\b/),
    dollarCount: (text.match(/\$[\d,]+/g) || []).length,
    nullBudgets: (text.match(/\$null/g) || []).length,
    autoFlags: (text.match(/\bAUTO\b/g) || []).length,
    lines: text.split("\n").map(s => s.trim()).filter(Boolean),
    interestingClasses: classes,
    tables,
  };
}
"""


def catalog(port: int, out_dir: Path) -> int:
    pw, browser = attach(port)
    try:
        page = find_draft_page(browser)
        if page is None:
            open_urls = [
                p.url for c in browser.contexts for p in c.pages if p.url
            ]
            raise ProbeError(
                "no draft room tab found in that Chrome.\n"
                f"Looked for a URL containing {DRAFT_URL_FRAGMENT!r}.\n"
                + ("Open tabs:\n  " + "\n  ".join(open_urls) if open_urls else "No tabs open.")
            )

        data = page.evaluate(CATALOG_JS)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"draft_room_catalog_{stamp}.json"
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")

        print(f"attached to: {data['title']}")
        print(f"  {data['textLength']} chars of text, {len(data['lines'])} lines")
        print(f"  pick header : {data['pickHeader']}")
        print(f"  clock       : {data['clock']}")
        print(f"  $ amounts   : {data['dollarCount']}  ($null: {data['nullBudgets']}, AUTO: {data['autoFlags']})")
        print(f"  tables      : {len(data['tables'])}")
        print("\ntop draft-ish class names:")
        for name, count in data["interestingClasses"][:20]:
            print(f"  {count:>4}x  {name}")
        print(f"\nfull catalogue written to {target}")
        return 0
    finally:
        browser.close()
        pw.stop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--launch", action="store_true",
                        help="start Chrome with the debug port on a dedicated profile")
    parser.add_argument("--catalog", action="store_true",
                        help="dump what the open draft room exposes")
    parser.add_argument("--port", type=int, default=CDP_PORT)
    parser.add_argument("--chrome", type=Path, default=None)
    parser.add_argument("--profile", type=Path, default=PROFILE_DIR)
    parser.add_argument("--out", type=Path, default=Path("probe_out"))
    args = parser.parse_args()

    try:
        if args.launch:
            return launch(args.chrome or find_chrome(), args.port, args.profile)
        if args.catalog:
            return catalog(args.port, args.out)
        parser.print_help()
        return 2
    except ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
