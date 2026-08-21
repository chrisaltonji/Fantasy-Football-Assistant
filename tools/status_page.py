"""Render `docs/status.json` to a single self-contained `docs/status.html`.

The point of this file is that there is one place to update. The tables that
kept getting retyped into chat now live in `docs/status.json`, and this turns
them into a page you can refresh instead of asking.

**Declared vs derived.** Anything that describes *judgement* — what is done,
what is next, what a rehearsal proved — is declared in the JSON, because nothing
can compute it. Anything that is a *fact about the repo* — test count, module
count, lines, commits, days until the draft — is derived here, at render time,
from the repo itself. Those are exactly the numbers that go stale in a
hand-maintained status doc, and the fix is to stop hand-maintaining them.

No dependencies, no build step, no network. `python tools/status_page.py` and
open the file.

    python tools/status_page.py                # writes docs/status.html
    python tools/status_page.py --check        # fail if the page is out of date
    python tools/status_page.py --open         # write it and open a browser
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "status.json"
TARGET = ROOT / "docs" / "status.html"


# --- derived facts ----------------------------------------------------------


def _run(args: list[str]) -> str:
    """Best-effort shell out. A missing tool degrades to empty, never raises.

    This page is a convenience; it must render on a machine with no git, no
    pytest, and no network rather than fail and tell you nothing at all.
    """
    try:
        out = subprocess.run(
            args, cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout or ""


def count_tests() -> int | None:
    """Collect-only, so it is fast and cannot be affected by a failing test."""
    text = _run([sys.executable, "-m", "pytest", "-q", "--collect-only",
                 "-p", "no:cacheprovider", "tests"])
    match = re.search(r"(\d+)\s+tests? collected", text)
    return int(match.group(1)) if match else None


def count_files(pattern: str, where: str) -> int:
    return len(list((ROOT / where).rglob(pattern)))


def count_lines(where: str, pattern: str = "*.py") -> int:
    total = 0
    for path in (ROOT / where).rglob(pattern):
        try:
            total += sum(1 for _ in path.open("r", encoding="utf-8", errors="ignore"))
        except OSError:  # pragma: no cover - unreadable file
            continue
    return total


def recent_commits(limit: int = 12) -> list[dict[str, str]]:
    text = _run(["git", "log", f"-{limit}", "--date=short",
                 "--pretty=format:%h\x1f%ad\x1f%s"])
    out: list[dict[str, str]] = []
    for line in text.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            out.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
    return out


def days_until(iso: str) -> int | None:
    try:
        target = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return (target - date.today()).days


# --- rendering helpers ------------------------------------------------------

# Every status word the JSON may use, and how it presents. Kept in one place so
# a typo in the data shows up as an obviously unstyled pill rather than as a
# silently mislabelled row.
STATES = {
    "done": ("done", "ok"),
    "partial": ("half", "warn"),
    "todo": ("not started", "todo"),
}


def pill(status: str, *, new: bool = False) -> str:
    label, tone = STATES.get(status, (status, "todo"))
    flag = '<span class="pill new">new</span>' if new else ""
    return f'<span class="pill {tone}">{escape(label)}</span>{flag}'


def e(value) -> str:
    return escape(str(value if value is not None else ""))


def section(title: str, body: str, *, sub: str = "", id_: str = "") -> str:
    anchor = f' id="{id_}"' if id_ else ""
    subtitle = f'<p class="sub">{e(sub)}</p>' if sub else ""
    return (
        f'<section{anchor}><h2>{e(title)}</h2>{subtitle}{body}</section>'
    )


def render(data: dict, *, fragment: bool = False) -> str:
    tests = count_tests()
    src_modules = count_files("*.py", "src")
    test_modules = len(list((ROOT / "tests").rglob("test_*.py")))
    src_lines = count_lines("src")
    commits = recent_commits()
    left = days_until(data.get("draft_day", ""))

    caps = data.get("capabilities", [])
    done = sum(1 for c in caps if c.get("status") == "done")
    partial = sum(1 for c in caps if c.get("status") == "partial")
    total = len(caps)
    pct = round(100 * (done + 0.5 * partial) / total) if total else 0

    verdict = data.get("verdict", {})

    # --- header ---------------------------------------------------------
    countdown = ""
    if left is not None:
        word = "day" if abs(left) == 1 else "days"
        if left > 0:
            countdown = f'<div class="count"><strong>{left}</strong> {word} to draft</div>'
        elif left == 0:
            countdown = '<div class="count urgent"><strong>Draft day</strong></div>'
        else:
            countdown = f'<div class="count"><strong>{abs(left)}</strong> {word} since draft</div>'

    stats = [
        ("tests", f"{tests:,}" if tests is not None else "—"),
        ("source modules", str(src_modules)),
        ("test modules", str(test_modules)),
        ("lines of source", f"{src_lines:,}"),
    ]
    stat_html = "".join(
        f'<div class="stat"><span class="figure">{e(v)}</span>'
        f'<span class="label">{e(k)}</span></div>'
        for k, v in stats
    )

    head = f"""
<header>
  <div class="head-main">
    <h1>{e(data.get('project', 'Build status'))}</h1>
    <p class="sub">{e(data.get('subtitle', ''))}</p>
    <p class="draft-line">Draft day
      <strong>{e(data.get('draft_day', ''))}</strong>
      {e(data.get('draft_time', ''))}</p>
  </div>
  {countdown}
</header>

<div class="verdict {e(verdict.get('state', ''))}">
  <p class="headline">{e(verdict.get('headline', ''))}</p>
  <p>{e(verdict.get('detail', ''))}</p>
</div>

<div class="stats">{stat_html}</div>

<div class="progress-wrap">
  <div class="progress-head">
    <span>Advisory capabilities</span>
    <span><strong>{done}</strong> done · {partial} half · {total - done - partial} not started</span>
  </div>
  <div class="bar"><div class="fill" style="width:{pct}%"></div></div>
</div>
"""

    # --- checkpoints ----------------------------------------------------
    rows = "".join(
        f'<tr><td class="mono">{e(c["id"])}</td><td>{e(c["scope"])}</td>'
        f'<td>{pill(c["status"])}</td></tr>'
        for c in data.get("checkpoints", [])
    )
    checkpoints = section(
        "Engine checkpoints",
        f'<table><thead><tr><th>#</th><th>Scope</th><th>Status</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
        f'<p class="note">{e(data.get("checkpoints_note", ""))}</p>',
        id_="checkpoints",
    )

    # --- capabilities ---------------------------------------------------
    cap_rows = ""
    for c in caps:
        num = c["n"] if c.get("n") is not None else "—"
        cap_rows += (
            f'<tr class="{e(c.get("status"))}">'
            f'<td class="mono num">{e(num)}</td>'
            f'<td class="capname">{e(c["name"])}</td>'
            f'<td><span class="tier t-{e(c.get("tier","")).replace(".","-")}">'
            f'{e(c.get("tier",""))}</span></td>'
            f'<td>{pill(c["status"], new=bool(c.get("new")))}</td>'
            f'<td class="where">{e(c.get("where",""))}</td>'
            f"</tr>"
        )
    capabilities = section(
        "The 12 advisory capabilities",
        f'<table class="caps"><thead><tr><th>#</th><th>Capability</th><th>Tier</th>'
        f'<th>Status</th><th>Where / why</th></tr></thead>'
        f'<tbody>{cap_rows}</tbody></table>'
        f'<p class="note">{e(data.get("capabilities_note",""))}</p>',
        id_="capabilities",
    )

    # --- the tracked build ----------------------------------------------
    # A capability table says "LLM layer: not started" for weeks at a time. This
    # is the same work at the granularity it is actually done in, so progress is
    # visible while it is happening rather than only when it lands.
    build = data.get("build") or {}
    build_html = ""
    if build:
        steps = build.get("steps") or []
        items = [i for s in steps for i in (s.get("items") or [])]
        done_n = sum(1 for i in items if i.get("status") == "done")
        pct = round(100 * done_n / len(items)) if items else 0

        rows = ""
        for step in steps:
            its = step.get("items") or []
            sdone = sum(1 for i in its if i.get("status") == "done")
            state = ("done" if its and sdone == len(its)
                     else "partial" if sdone else "todo")
            lis = "".join(
                f'<li class="{e(i.get("status"))}"><span class="dot"></span>'
                f'<span class="itemname">{e(i["name"])}</span>'
                f'{pill(i.get("status", "todo"))}</li>'
                for i in its
            )
            detail = (f'<p class="blurb">{e(step["detail"])}</p>'
                      if step.get("detail") else "")
            rows += (
                f'<article class="card">'
                f'<div class="card-head"><h3>{step.get("n")}. {e(step["name"])}</h3>'
                f'<span class="ratio">{sdone}/{len(its)}</span></div>'
                f'{detail}{pill(state)}<ul class="items">{lis}</ul></article>'
            )

        build_html = section(
            build.get("name", "Build"),
            f'<p class="sub">{e(build.get("blurb", ""))}</p>'
            f'<div class="progress-wrap"><div class="progress-head">'
            f'<span>modules</span><span><strong>{done_n}</strong> of {len(items)}'
            f'</span></div><div class="bar"><div class="fill" '
            f'style="width:{pct}%"></div></div></div>'
            f'<div class="cards">{rows}</div>',
            id_="build",
        )

    # --- layers ---------------------------------------------------------
    cards = ""
    for layer in data.get("layers", []):
        items = ""
        for item in layer.get("items", []):
            note = (
                f'<span class="itemnote">{e(item["note"])}</span>'
                if item.get("note") else ""
            )
            items += (
                f'<li class="{e(item.get("status"))}">'
                f'<span class="dot"></span>'
                f'<span class="itemname">{e(item["name"])}{note}</span>'
                f'{pill(item["status"], new=bool(item.get("new")))}</li>'
            )
        layer_items = layer.get("items", [])
        layer_done = sum(1 for i in layer_items if i.get("status") == "done")
        cards += (
            f'<article class="card">'
            f'<div class="card-head"><h3>{e(layer["name"])}</h3>'
            f'<span class="ratio">{layer_done}/{len(layer_items)}</span></div>'
            f'<p class="blurb">{e(layer.get("blurb",""))}</p>'
            f'<ul class="items">{items}</ul></article>'
        )
    layers = section("Layer by layer", f'<div class="cards">{cards}</div>', id_="layers")

    # --- blocking -------------------------------------------------------
    block_rows = "".join(
        f'<tr><td class="mono">{e(b["id"])}</td><td>{e(b["item"])}</td>'
        f'<td>{pill(b["status"])}</td><td class="where">{e(b.get("note",""))}</td></tr>'
        for b in data.get("blocking", [])
    )
    blocking = section(
        "Blocking items",
        f'<table><thead><tr><th>#</th><th>Item</th><th>Status</th><th>Note</th>'
        f'</tr></thead><tbody>{block_rows}</tbody></table>',
        id_="blocking",
    )

    # --- next -----------------------------------------------------------
    next_html = ""
    for item in data.get("next", []):
        cmd = (
            f'<pre class="cmd">{e(item["command"])}</pre>'
            if item.get("command") else ""
        )
        owner = e(item.get("owner", ""))
        next_html += (
            f'<article class="next-item owner-{owner}">'
            f'<div class="next-head">'
            f'<h3>{e(item["title"])}</h3>'
            f'<span class="urgency">{e(item.get("urgency",""))}</span></div>'
            f'<p>{e(item.get("detail",""))}</p>{cmd}</article>'
        )
    nxt = section("What is next", f'<div class="next-list">{next_html}</div>', id_="next")

    # --- rehearsals -----------------------------------------------------
    reh = ""
    for r in data.get("rehearsals", []):
        def bullets(key: str, css: str) -> str:
            entries = r.get(key) or []
            if not entries:
                return ""
            lis = "".join(f"<li>{e(x)}</li>" for x in entries)
            heading = {
                "confirmed": "Confirmed live",
                "found": "Bugs found",
                "artefacts": "Rehearsal artefacts, not defects",
            }[key]
            return f'<div class="reh-group {css}"><h4>{heading}</h4><ul>{lis}</ul></div>'

        reh += (
            f'<article class="card rehearsal">'
            f'<div class="card-head"><h3>{e(r["title"])}</h3>'
            f'<span class="ratio">{e(r["date"])}</span></div>'
            f'{bullets("confirmed", "good")}'
            f'{bullets("found", "bad")}'
            f'{bullets("artefacts", "neutral")}'
            f"</article>"
        )
    rehearsals = section("Live rehearsals", f'<div class="cards">{reh}</div>', id_="rehearsals")

    # --- open questions -------------------------------------------------
    q_html = ""
    for q in data.get("open_questions", []):
        tag = (
            '<span class="pill warn">blocking</span>' if q.get("blocking")
            else '<span class="pill todo">not blocking</span>'
        )
        q_html += (
            f"<details><summary>{e(q['q'])} {tag}</summary>"
            f'<p><strong>Why it matters.</strong> {e(q.get("why",""))}</p>'
            f'<p><strong>How it gets answered.</strong> {e(q.get("how",""))}</p>'
            f"</details>"
        )
    for n in data.get("notes", []):
        q_html += (
            f"<details><summary>{e(n['title'])} "
            f'<span class="pill todo">note</span></summary>'
            f"<p>{e(n.get('body',''))}</p></details>"
        )
    questions = section("Open questions", q_html, id_="questions")

    # --- commits --------------------------------------------------------
    commit_rows = "".join(
        f'<tr><td class="mono">{e(c["hash"])}</td><td class="mono">{e(c["date"])}</td>'
        f'<td>{e(c["subject"])}</td></tr>'
        for c in commits
    )
    history = section(
        "Recent work",
        f'<table class="commits"><tbody>{commit_rows}</tbody></table>'
        if commit_rows else '<p class="note">No git history available.</p>',
        id_="history",
    )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    nav = "".join(
        f'<a href="#{i}">{label}</a>'
        for i, label in (
            ("build", "Build"),
            ("checkpoints", "Checkpoints"),
            ("capabilities", "Capabilities"),
            ("layers", "Layers"),
            ("blocking", "Blocking"),
            ("next", "Next"),
            ("rehearsals", "Rehearsals"),
            ("questions", "Questions"),
            ("history", "History"),
        )
    )

    # The page has its own name, separate from the project's. In a gallery of
    # artifacts "Draft Day Readiness" says what this page is; the project name
    # would only say which repo it came from.
    return (FRAGMENT if fragment else TEMPLATE).format(
        css=CSS,
        title=e(data.get("page_title") or data.get("project", "Build status")),
        nav=nav,
        head=head,
        body="".join([
            build_html, checkpoints, capabilities, layers, blocking, nxt,
            rehearsals, questions, history,
        ]),
        generated=generated,
        as_of=e(data.get("as_of", "")),
    )


CSS = """
/* Palette grounded in the subject rather than defaulted: an auction ledger.
   Warm neutrals, oxblood accent. The semantic three — done / half / not
   started — are deliberately a separate hue family from the accent, so a
   status never reads as "branded" and the accent never reads as a status.

   Three theme states, not two. A viewer who has chosen stamps data-theme on
   the root; a viewer on the default "system" setting stamps nothing, and only
   prefers-color-scheme separates them. So: :root carries the whole light
   palette, the media query redefines tokens for the unstamped dark case, and
   the [data-theme] blocks let an explicit choice win either way. Nothing below
   declares a colour anywhere except through these tokens. */
:root{
  --bg:#faf8f6; --panel:#fff; --ink:#1a1614; --muted:#6b625d; --line:#e8e2dd;
  --ok:#1f7a44; --ok-bg:#e8f3ec; --warn:#8a5209; --warn-bg:#fbf0dc;
  --todo:#6b625d; --todo-bg:#efebe7; --new:#8c2f39; --new-bg:#f7e9ea;
  --accent:#8c2f39;
  --shadow:0 1px 2px rgba(26,22,20,.05),0 4px 16px rgba(26,22,20,.05);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#14110f; --panel:#1c1917; --ink:#ede8e4; --muted:#a09590; --line:#2b2521;
    --ok:#5fd08a; --ok-bg:#12281c; --warn:#e8b463; --warn-bg:#2b210f;
    --todo:#a09590; --todo-bg:#241f1c; --new:#e79aa3; --new-bg:#2e1a1d;
    --accent:#e79aa3;
    --shadow:0 1px 2px rgba(0,0,0,.35),0 4px 20px rgba(0,0,0,.28);
  }
}
:root[data-theme="dark"]{
  --bg:#14110f; --panel:#1c1917; --ink:#ede8e4; --muted:#a09590; --line:#2b2521;
  --ok:#5fd08a; --ok-bg:#12281c; --warn:#e8b463; --warn-bg:#2b210f;
  --todo:#a09590; --todo-bg:#241f1c; --new:#e79aa3; --new-bg:#2e1a1d;
  --accent:#e79aa3;
  --shadow:0 1px 2px rgba(0,0,0,.35),0 4px 20px rgba(0,0,0,.28);
}
*{box-sizing:border-box}
body{
  margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:26px;line-height:1.2;margin:0 0 4px;letter-spacing:-.02em;text-wrap:balance}
h2{font-size:18px;margin:0 0 14px;letter-spacing:-.01em}
h3{font-size:15px;margin:0}
h4{font-size:12px;margin:0 0 6px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
p{margin:0 0 10px}
a{color:var(--accent)}
.sub{color:var(--muted);margin:0 0 6px}
.note{color:var(--muted);font-size:13.5px;margin-top:12px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}

header{display:flex;gap:20px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;margin-bottom:20px}
.draft-line{color:var(--muted);font-size:13.5px;margin:6px 0 0}
.count{
  background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:12px 18px;text-align:center;box-shadow:var(--shadow);white-space:nowrap;
}
.count strong{display:block;font-size:30px;line-height:1;letter-spacing:-.03em;font-variant-numeric:tabular-nums}
.count.urgent{color:var(--warn);border-color:var(--warn)}

.verdict{border-radius:12px;padding:16px 18px;margin-bottom:18px;border:1px solid var(--line);background:var(--panel);box-shadow:var(--shadow)}
.verdict.ready{border-left:4px solid var(--ok)}
.verdict .headline{font-weight:650;font-size:16px;margin-bottom:4px}
.verdict p:last-child{margin:0;color:var(--muted)}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:18px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;box-shadow:var(--shadow)}
.stat .figure{display:block;font-size:22px;font-weight:640;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat .label{display:block;color:var(--muted);font-size:12px;margin-top:2px}

.progress-wrap{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:26px;box-shadow:var(--shadow)}
.progress-head{display:flex;justify-content:space-between;gap:12px;font-size:13px;color:var(--muted);margin-bottom:8px;flex-wrap:wrap}
.progress-head strong{color:var(--ink)}
.bar{height:8px;border-radius:99px;background:var(--todo-bg);overflow:hidden}
.fill{height:100%;border-radius:99px;background:var(--ok)}

nav{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0;margin-bottom:8px;border-bottom:1px solid var(--line);display:flex;gap:14px;flex-wrap:wrap}
nav a{color:var(--muted);text-decoration:none;font-size:13px}
nav a:hover{color:var(--accent)}

section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px;box-shadow:var(--shadow)}

table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font-size:11.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600;padding:0 10px 8px 0;border-bottom:1px solid var(--line)}
td{padding:9px 10px 9px 0;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
.where{color:var(--muted);font-size:13px}
.capname{font-weight:560}
.num{color:var(--muted)}
tr.todo .capname{color:var(--muted);font-weight:500}
.commits td{padding:7px 10px 7px 0}

.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:11.5px;font-weight:600;white-space:nowrap;letter-spacing:.01em}
.pill.ok{background:var(--ok-bg);color:var(--ok)}
.pill.warn{background:var(--warn-bg);color:var(--warn)}
.pill.todo{background:var(--todo-bg);color:var(--todo)}
.pill.new{background:var(--new-bg);color:var(--new);margin-left:5px}
.tier{font-size:11.5px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.card{border:1px solid var(--line);border-radius:10px;padding:16px;background:var(--bg)}
.card-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:6px}
.ratio{font-size:12px;color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:nowrap}
.blurb{color:var(--muted);font-size:13px;margin-bottom:12px}
.items{list-style:none;margin:0;padding:0}
.items li{display:flex;align-items:flex-start;gap:9px;padding:6px 0;border-top:1px solid var(--line);font-size:13.5px}
.items li:first-child{border-top:none}
.itemname{flex:1;min-width:0}
.itemnote{display:block;color:var(--muted);font-size:12px;margin-top:1px}
.dot{width:7px;height:7px;border-radius:99px;background:var(--todo);margin-top:7px;flex:none}
.items li.done .dot{background:var(--ok)}
.items li.partial .dot{background:var(--warn)}
.items li.todo .itemname{color:var(--muted)}

.next-list{display:flex;flex-direction:column;gap:12px}
.next-item{border:1px solid var(--line);border-left:3px solid var(--todo);border-radius:10px;padding:14px 16px;background:var(--bg)}
.next-item.owner-you{border-left-color:var(--warn)}
.next-item.owner-build{border-left-color:var(--accent)}
.next-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:6px;flex-wrap:wrap}
.urgency{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);white-space:nowrap}
.next-item p{color:var(--muted);font-size:13.5px;margin:0}
pre.cmd{
  background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:10px 12px;margin:10px 0 0;overflow-x:auto;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;color:var(--ink);
}

.rehearsal .reh-group{margin-top:12px}
.reh-group ul{margin:0;padding-left:18px;font-size:13.5px;color:var(--muted)}
.reh-group li{margin-bottom:4px}
.reh-group.good h4{color:var(--ok)}
.reh-group.bad h4{color:var(--warn)}

details{border-top:1px solid var(--line);padding:12px 0}
details:first-of-type{border-top:none;padding-top:0}
summary{cursor:pointer;font-weight:560;font-size:14px;list-style:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸";color:var(--muted);display:inline-block;width:16px}
details[open] summary::before{content:"▾"}
details p{color:var(--muted);font-size:13.5px;margin:8px 0 0 16px}

a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:3px}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

footer{color:var(--muted);font-size:12.5px;text-align:center;margin-top:26px;line-height:1.7}

@media (max-width:640px){
  .wrap{padding:20px 14px 60px}
  h1{font-size:21px}
  header{flex-direction:column}
  .count{align-self:stretch}
  section{padding:16px}
  .where{display:none}
  th:last-child,td:last-child{padding-right:0}
}
"""

# Body-only. A published Artifact is wrapped in its own
# `<!doctype html><head>…</head><body>` skeleton, so a full document here would
# nest one inside another. The `<title>` is kept because that is what names the
# page in the gallery and the browser tab.
FRAGMENT = """<title>{title}</title>
<style>{css}</style>
<div class="wrap">
{head}
<nav>{nav}</nav>
{body}
<footer>
  Generated {generated} from <code>docs/status.json</code> ·
  status as of {as_of}<br>
  Regenerate with <code>python tools/status_page.py</code>
</footer>
</div>
"""

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%238c2f39'/%3E%3Cpath d='M8 21V13M14 21V9M20 21V16M26 21V11' stroke='white' stroke-width='3' stroke-linecap='round'/%3E%3C/svg%3E">
<meta name="color-scheme" content="light dark">
<style>{css}</style>
</head>
<body>
<div class="wrap">
{head}
<nav>{nav}</nav>
{body}
<footer>
  Generated {generated} from <code>docs/status.json</code> ·
  status as of {as_of}<br>
  Regenerate with <code>python tools/status_page.py</code>
</footer>
</div>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, default=TARGET)
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the page would change (for CI)")
    parser.add_argument("--open", dest="open_it", action="store_true",
                        help="open the page in a browser afterwards")
    parser.add_argument("--fragment", action="store_true",
                        help="emit body-only HTML, for publishing where the "
                             "host supplies the page skeleton")
    args = parser.parse_args(argv)

    if not args.source.is_file():
        print(f"error: no status source at {args.source}", file=sys.stderr)
        return 2

    try:
        data = json.loads(args.source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"error: {args.source} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    html = render(data, fragment=args.fragment)

    if args.check:
        # The timestamp changes every run, so compare everything else. Without
        # this the check could never pass.
        def strip(text: str) -> str:
            return re.sub(r"Generated [^<·]+", "", text)

        current = args.out.read_text(encoding="utf-8") if args.out.is_file() else ""
        if strip(current) != strip(html):
            print(f"{args.out} is out of date — run: python tools/status_page.py",
                  file=sys.stderr)
            return 1
        print(f"{args.out} is up to date")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding="utf-8")
    print(f"wrote {args.out}  ({len(html):,} bytes)")

    if args.open_it:  # pragma: no cover - interactive
        import webbrowser

        webbrowser.open(args.out.resolve().as_uri())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
