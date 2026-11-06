"""The web UI for people: browse the wiki, see what changed, the schedule, what is open, and run the steward.

  just-wiki web [--host H] [--port P]     ([web] in wiki.toml; default 127.0.0.1:8094)

Rendered live from the files on each request (no build step). HTTP Basic auth on every request: [web] user, password
in [web] password_file (created on first start, 0600). Plain HTTP: keep it on a trusted network or behind a proxy.
Never serves inbox notes' raw files, raw/, logs/ or code; inbox notes are shown read-only on the Open tab.
Stdlib http.server + Python-Markdown; no JavaScript framework.

Panels: each `[[web.panels]]` in wiki.toml (name, command) is one more tab. The command prints JSON that the UI draws, so
a setup can show its own data (what its tools did, statistics) without the engine knowing it:

  {"title": "…", "updated": "…", "sections": [{"heading": "…", "text": "…", "fold": false,
      "stats": [{"label", "value", "note", "tone": "ok|warn|bad"}],
      "bars": [{"label", "value"}], "bars_title": "…",
      "table": {"columns": [...], "rows": [[cell, ...]]},       cell: text, or {"text", "href", "tag", "code"}, or a list of cells
      "items": [{"title", "text", "tag"}]}]}

Everything is escaped; hrefs must be a path on this site or http(s). The command runs on each view (cached 15 s, 60 s limit).
"""
import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

import markdown

from .. import config, edit, issues, lint, live, questions, steward
from .. import store as w
from .style import CSS

EXTRA = {"index": "Index", "AGENTS": "Schema", "STEWARD": "Steward rules"}
NESTED = re.compile(r"^ {2,3}(?=(?:[-*+]|\d+\.) )", re.M)   # Python-Markdown wants 4-space nesting
CODE = re.compile(r"(```.*?```|`[^`\n]+`)", re.S)
STATUS_TAG = {"verified": "ok", "draft": "warn", "stale": "bad"}
HASH_UNIT = re.compile(r"^[0-9a-f]{20,}")
TABS = (("/", "Pages"), ("/_changes", "Changes"), ("/_stats", "Stats"), ("/_schedule", "Schedule"), ("/_open", "Open"),
        ("/_steward", "Steward"), ("/_schema", "Schema"))
PANEL_CACHE = {}
e = html.escape


# ---------- pages ----------

def password():
    f = w.ROOT / w.cfg()["web"].get("password_file", ".web_password")
    if not f.exists():
        f.write_text(secrets.token_urlsafe(18) + "\n", encoding="utf-8")
        f.chmod(0o600)
    return f.read_text(encoding="utf-8").strip()


def load():
    """{page id: (meta, body)} for everything the site shows. Re-read per request: small files."""
    out = {}
    for path in w.pages() + [w.ROOT / f"{p}.md" for p in EXTRA]:
        if path.exists():
            meta, body = w.split(path.read_text(encoding="utf-8"))
            out[w.page_id(path)] = (meta or {}, body)
    return out


def url(pid):
    return "/" if pid == "index" else "/" + quote(pid)


def title(pid, pages):
    return str(pages[pid][0].get("title") or EXTRA.get(pid) or pid) if pid in pages else pid


def wikilinks(body, pages):
    def sub(m):
        target, _, label = m.group(1).partition("|")
        target, _, anchor = target.strip().removesuffix(".md").partition("#")
        if target not in pages:
            return f'<span class="broken" title="no such page">{e(label or target)}</span>'
        label = (label or title(target, pages)).strip()
        return f"[{label}]({url(target)}{'#' + anchor if anchor else ''})"
    parts = CODE.split(body)     # leave [[...]] inside code alone
    return "".join(p if i % 2 else re.sub(r"\[\[([^\]]+)\]\]", sub, p) for i, p in enumerate(parts))


def render(body, pages):
    body = live.BLOCK.sub(lambda m: f'<div class="live" markdown="1">\n\n{m.group(3)}\n</div>\n', body)
    return markdown.markdown(NESTED.sub("    ", wikilinks(body, pages)),
                             extensions=["tables", "fenced_code", "sane_lists", "toc", "md_in_html"])


def backlinks(pid, pages):
    return sorted(o for o, (_, body) in pages.items() if o not in (pid, "index") and pid in w.links(body))


def nav(current, pages):
    def item(pid, label=None):
        cls = ' class="here"' if pid == current else ""
        tag = pages[pid][0].get("status") if pid in pages else None
        dot = f'<i class="dot {STATUS_TAG.get(tag, "")}"></i>' if tag in ("draft", "stale") else ""
        return f'<li><a href="{url(pid)}"{cls}>{e(label or title(pid, pages))}{dot}</a></li>'
    out = ['<ul class="top">', item("status") if "status" in pages else "", item("index"), "</ul>"]
    for d in w.PAGE_DIRS:
        ids = sorted((p for p in pages if p.startswith(d + "/")), key=lambda p: title(p, pages).lower())
        if ids:
            out += [f"<h3>{d.capitalize()}</h3><ul>"] + [item(p) for p in ids] + ["</ul>"]
    out += ['<ul class="top">'] + [item(p) for p in ("AGENTS", "STEWARD") if p in pages] + ["</ul>"]
    return "\n".join(out)


def meta_bar(meta):
    if not meta.get("type") or meta.get("type") == "status":
        return ""
    status = str(meta.get("status", "?"))
    bits = [f'<span class="tag">{e(str(meta["type"]))}</span>',
            f'<span class="tag {STATUS_TAG.get(status, "")}">{e(status)}</span>']
    if meta.get("updated"):
        bits.append(f"updated {e(str(meta['updated']))} by {e(str(meta.get('updated_by', '?')))}")
    out = f'<div class="meta">{" · ".join(bits)}</div>'
    if meta.get("sources"):
        items = "".join(f"<li><code>{e(str(s))}</code></li>" if not str(s).startswith("http")
                        else f'<li><a href="{e(str(s))}">{e(str(s))}</a></li>' for s in meta["sources"])
        out += f'<details class="sources"><summary>Sources ({len(meta["sources"])})</summary><ul>{items}</ul></details>'
    return out


def page_view(pid, pages):
    meta, body = pages[pid]
    content = render(body, pages)
    head, sep, rest = content.partition("</h1>")
    actions = ""
    if edit.path_of(pid):
        btns = f"<a class='btn alt sm' href='/_edit/{e(pid)}'>Edit</a>" + (
            "" if pid in w.TOP_PAGES else f"<a class='btn alt sm danger' href='/_delete/{e(pid)}'>Remove</a>")
        actions = f"<div class='pageactions'>{btns}</div>"
    content = (f"<div class='pagehead'>{head}{sep}{actions}</div>" + meta_bar(meta) + rest) if sep else actions + meta_bar(meta) + content
    hist = git_log(n=8, path=f"{pid}.md")
    if hist:
        content += ('<section class="side"><h2>History</h2><ul class="plain">' + "".join(
            f'<li><a href="/_changes/{c["sha"]}">{e(c["when"])}</a> {e(c["subject"])}</li>' for c in hist)
            + "</ul></section>")
    refs = backlinks(pid, pages)
    if refs:
        content += ('<section class="side"><h2>Linked from</h2><ul class="plain">'
                    + "".join(f'<li><a href="{url(r)}">{e(title(r, pages))}</a></li>' for r in refs) + "</ul></section>")
    return content


def search_view(q, pages):
    q = q.strip().lower()
    hits = []
    for pid, (meta, body) in pages.items():
        if pid == "index":
            continue
        t, text = title(pid, pages).lower(), body.lower()
        score = 10 * (q in t) + 3 * (q in str(meta.get("summary", "")).lower()) + text.count(q)
        if score:
            i = text.find(q)
            snippet = body[max(0, i - 80): i + 120].replace("\n", " ") if i >= 0 else str(meta.get("summary", ""))
            hits.append((score, pid, snippet))
    hits.sort(key=lambda h: -h[0])
    rows = "".join(f'<li><a href="{url(pid)}">{e(title(pid, pages))}</a> <span class="id">{pid}</span>'
                   f'<p>…{e(snip)}…</p></li>' for _, pid, snip in hits)
    return f"<h1>Search: {e(q)}</h1>" + (f'<ul class="hits">{rows}</ul>' if hits else "<p>Nothing found.</p>")


# ---------- changes ----------

def git_log(n=60, path=None):
    if not (w.ROOT / ".git").exists():
        return []
    args = ["git", "-C", str(w.ROOT), "log", f"-n{n}", "--date=format:%Y-%m-%d %H:%M",
            "--pretty=format:%x1e%h%x1f%ad%x1f%an%x1f%s", "--name-only"]
    out = subprocess.run(args + (["--", path] if path else []), capture_output=True, text=True).stdout
    commits = []
    for chunk in out.split("\x1e")[1:]:
        head, _, files = chunk.partition("\n")
        sha, when, who, subject = (head.split("\x1f") + ["", "", "", ""])[:4]
        commits.append({"sha": sha, "when": when, "who": who, "subject": subject,
                        "files": [f for f in files.splitlines() if f.strip()]})
    return commits


def page_link(f, pages):
    pid = f.removesuffix(".md")
    return f'<a href="{url(pid)}">{e(title(pid, pages))}</a>' if f.endswith(".md") and pid in pages else \
        f'<span class="id">{e(f)}</span>'


def changes_view(query, pages):
    kind = query.get("show", ["pages"])[0]
    commits = git_log(150)
    if kind == "pages":   # what a reader cares about: commits that changed curated pages
        commits = [c for c in commits if any(f.removesuffix(".md") in pages for f in c["files"])]
    tabs = " ".join(f'<a href="?show={k}"{" class=here" if k == kind else ""}>{lbl}</a>'
                      for k, lbl in (("pages", "Page changes"), ("all", "All commits"), ("log", "log.md")))
    out = [f"<h1>Changes</h1><p class='subnav'>{tabs}</p>"]
    if kind == "log":
        lines = w.LOG.read_text(encoding="utf-8").splitlines()[-200:] if w.LOG.exists() else []
        ops = sorted({m.group(1) for line in lines if (m := re.match(r"^- \[[^]]*\] (\w+)", line))})
        op = query.get("op", [""])[0]
        links = [f'<a href="?show=log"{" class=here" if not op else ""}>all</a>'] + [
            f'<a href="?show=log&op={o}"{" class=here" if o == op else ""}>{o}</a>' for o in ops]
        out.append("<p class='subnav'>" + " ".join(links) + "</p>")
        rows = []
        for line in reversed(lines):
            m = re.match(r"^- \[([^]]*)\] (\w+) \| (.*)$", line)
            if m and (not op or m.group(2) == op):
                rows.append(f"<tr><td class='nowrap'>{e(m.group(1))}</td><td><span class='tag'>{e(m.group(2))}</span>"
                            f"</td><td>{wikilinks_html(m.group(3), pages)}</td></tr>")
        out.append("<table class='list'><tr><th>When</th><th>Op</th><th>What</th></tr>" + "".join(rows) + "</table>")
        return "\n".join(out)
    if not commits:
        return "\n".join(out + ["<p>No commits (is the wiki a git repo?).</p>"])
    day = None
    for c in commits:
        d = c["when"][:10]
        if d != day:
            out.append(f"<h2>{e(d)}</h2>")
            day = d
        touched = [f for f in c["files"] if not f.startswith("inbox/")]
        shown = ", ".join(page_link(f, pages) for f in touched[:6]) + (f" +{len(touched) - 6}" if len(touched) > 6
                                                                        else "")
        notes = sum(f.startswith("inbox/") for f in c["files"])
        out.append(f'<div class="commit"><a href="/_changes/{c["sha"]}" class="sha">{e(c["sha"])}</a> '
                   f'<span class="when">{e(c["when"][11:])}</span> {e(c["subject"])}'
                   f'<div class="files">{shown}{f" · {notes} note file(s)" if notes else ""}</div></div>')
    return "\n".join(out)


def wikilinks_html(text, pages):
    def sub(m):
        pid = m.group(1)
        return f'<a href="{url(pid)}">{e(pid)}</a>' if pid in pages else e(pid)
    return re.sub(r"\b((?:systems|projects|decisions|runbooks|results)/[\w./-]+?|status)(?=[\s:,;)]|$)", sub,
                  e(text))


def commit_view(sha):
    if not re.fullmatch(r"[0-9a-f]{4,40}", sha):
        return "<h1>Bad commit id</h1>"
    r = subprocess.run(["git", "-C", str(w.ROOT), "show", "--stat", "--patch", "--format=%h %ad %an%n%n%s%n%n%b",
                        "--date=format:%Y-%m-%d %H:%M", sha, "--", "*.md", "wiki.toml"],
                       capture_output=True, text=True)
    text = r.stdout[:200_000] or r.stderr
    lines = []
    for line in text.splitlines():
        cls = ("add" if line.startswith("+") and not line.startswith("+++") else
               "del" if line.startswith("-") and not line.startswith("---") else
               "hunk" if line.startswith("@@") else "file" if line.startswith("diff --git") else "")
        lines.append(f'<span class="{cls}">{e(line)}</span>' if cls else e(line))
    return f'<h1>Commit {e(sha)}</h1><p><a href="/_changes">← all changes</a></p><pre class="diff">' + \
        "\n".join(lines) + "</pre>"


# ---------- schedule ----------

def fmt_us(us):
    return datetime.fromtimestamp(us / 1e6).strftime("%a %d.%m %H:%M") if us else "—"


def rel(us):
    if not us:
        return ""
    s = us / 1e6 - time.time()
    mins = abs(int(s // 60))
    txt = f"{mins} min" if mins < 90 else f"{mins // 60} h" if mins < 2880 else f"{mins // 1440} d"
    return f"in {txt}" if s > 0 else f"{txt} ago"


def schedule_rows():
    """[{unit, activates, next, last, description, result, active}] from [schedule] command or systemd user timers."""
    cmd = w.cfg()["schedule"]["command"]
    if cmd:
        try:
            return json.loads(subprocess.run([config.expand(a, w.ROOT) for a in cmd], capture_output=True,
                                             text=True, timeout=30).stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return []
    if not shutil.which("systemctl"):
        return []
    try:
        timers = json.loads(subprocess.run(["systemctl", "--user", "list-timers", "--all", "--output=json"],
                                           capture_output=True, text=True, timeout=15).stdout or "[]")
    except ValueError:
        return []
    rows = []
    for t in timers:
        if HASH_UNIT.match(t["unit"]):
            continue
        svc = t.get("activates") or t["unit"].replace(".timer", ".service")
        props = subprocess.run(["systemctl", "--user", "show", svc, "-p", "Description,Result,ActiveState,"
                                "ExecMainStatus"], capture_output=True, text=True).stdout
        p = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
        enabled = subprocess.run(["systemctl", "--user", "is-active", t["unit"]], capture_output=True,
                                 text=True).stdout.strip()
        rows.append({"unit": t["unit"], "activates": svc, "next": t.get("next") or 0, "last": t.get("last") or 0,
                     "description": p.get("Description", ""), "result": p.get("Result", ""),
                     "status": p.get("ExecMainStatus", ""), "active": p.get("ActiveState", ""), "timer": enabled})
    rows.sort(key=lambda r: (r["next"] == 0, r["next"]))
    return rows


def schedule_view():
    rows = schedule_rows()
    out = ["<h1>Schedule</h1>"]
    night = w.ROOT / "logs/nightly.json"
    if night.exists():
        try:
            n = json.loads(night.read_text())
        except ValueError:
            n = {}
        acts = ", ".join(f"{v} {k}" for k, v in n.get("acts", {}).items()) or "no steward actions"
        push = "ok" if n.get("pushed") else "off" if n.get("pushed") is None else "FAILED"
        out.append(f"<div class='card'><b>Last nightly run</b> {e(n.get('date', '?'))}: {e(acts)}; "
                   f"{e(n.get('lint', ''))}; push {push}"
                   + "".join(f"<div class='bad'>{e(p)}</div>" for p in n.get("problems", [])) + "</div>")
    if not rows:
        return "\n".join(out + ["<p>No timers found (configure [schedule] command in wiki.toml).</p>"])
    body = []
    for r in rows:
        ok = r.get("result") in ("success", "") and r.get("status") in ("0", "", None)
        state = ("running" if r.get("active") == "activating" else "paused" if r.get("timer") == "inactive"
                 else "ok" if ok else "failed")
        cls = {"ok": "ok", "failed": "bad", "paused": "warn", "running": "warn"}[state]
        body.append(f"<tr><td><b>{e(r['activates'].removesuffix('.service'))}</b><div class='id'>"
                    f"{e(r.get('description', ''))}</div></td><td class='nowrap'>{fmt_us(r['next'])}<div class='id'>"
                    f"{rel(r['next'])}</div></td><td class='nowrap'>{fmt_us(r['last'])}<div class='id'>{rel(r['last'])}"
                    f"</div></td><td><span class='tag {cls}'>{state}</span></td></tr>")
    out.append("<div class='tablewrap'><table class='list'><thead><tr><th>Job</th><th>Next</th><th>Last</th><th>State</th></tr></thead><tbody>"
               + "".join(body) + "</tbody></table></div>")
    return "\n".join(out)


# ---------- open items ----------

KIND_LABEL = {"mine": "yours", "agent": "agent", "reminder": "reminder"}


_filling = {}


def explain(q):
    """The question's card from [web] explain_command: {title, what, why, options: [{answer, effect}]}.

    The command is called as `<cmd> --cached <id>` (must be fast; prints {} when there is no card yet). A missing card
    is made in the background with `<cmd> <id>` (at most once per question every 10 minutes), so the page never waits
    for a model. Without the setting there are no cards."""
    cmd = w.cfg()["web"].get("explain_command") or []
    if not cmd:
        return {}
    argv = [config.expand(a, w.ROOT) for a in cmd]
    try:
        r = subprocess.run(argv + ["--cached", q["id"]], capture_output=True, text=True, timeout=10)
        c = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        c = {}
    if not c and time.time() - _filling.get(q["id"], 0) > 600:
        _filling[q["id"]] = time.time()
        try:
            subprocess.Popen(argv + [q["id"]], cwd=w.ROOT, start_new_session=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            pass
        return {"pending": True}
    if c and isinstance(c.get("options"), list):
        c["options"] = [o if isinstance(o, dict) else {"answer": str(o), "effect": ""} for o in c["options"]][:3]
    return c if isinstance(c, dict) else {}


def ago(d):
    if not d:
        return ""
    n = (datetime.now().date() - d).days
    return "today" if n == 0 else "yesterday" if n == 1 else f"{n} days ago"


def open_counts():
    """(waiting for the owner, waiting for the steward) for the tab badge."""
    rows, ans = questions.rows(), questions.answers()
    mine = sum(1 for q in rows if q["kind"] == "mine" and q["id"] not in ans)
    stew = sum(1 for q in rows if q["id"] in ans or questions.needs_steward(q))
    return mine, stew


def question_card(q, ans, pages, tok):
    a = ans.get(q["id"])
    due = q["due"]
    badges = [f"<span class='tag'>{KIND_LABEL[q['kind']]}</span>", f"<span class='id'>q-{q['id']}</span>",
              f"<span class='id'>owner {e(q['owner'])}</span>"]
    if due:
        badges.append(f"<span class='tag {'bad' if due <= datetime.now().date() else ''}'>due {due}</span>")
    if q["checked"]:
        badges.append(f"<span class='tag warn'>steward checked {q['checked']}</span>")
    if a:
        last = a[-1]
        badges.append(f"<span class='tag ok'>answered: {e(str(last.get('action')))}, waiting for the steward</span>")
    elif questions.needs_steward(q):
        badges.append("<span class='tag warn'>the steward settles it</span>")
    asked = questions.asked(q)
    if asked:
        badges.append(f"<span class='id'>asked {asked} ({ago(asked)})</span>")
    c = {} if a else explain(q)
    summary = ""
    if c.get("what") or c.get("why"):
        summary = (f"<div class='qsum'><b>{e(c.get('title', ''))}</b>"
                   + (f"<div><span class='lbl'>What</span> {e(c['what'])}</div>" if c.get("what") else "")
                   + (f"<div><span class='lbl'>Why</span> {e(c['why'])}</div>" if c.get("why") else "") + "</div>")
    elif c.get("pending"):
        summary = "<div class='qsum id'>A short summary and suggested answers are being written; reload in a moment.</div>"
    out = [f"<div class='q{' mine' if q['kind'] == 'mine' and not a else ''}' id='q-{q['id']}'>"
           f"<div class='qhead'>{' '.join(badges)}</div>{summary}"
           f"<details class='full'{'' if summary else ' open'}><summary>Full question</summary>"
           f"<div class='qtext'>{render(q['text'], pages)}</div></details>"]
    if a:
        for n in a:
            out.append(f"<div class='id'>→ {e(str(n.get('title', '')))} ({e(str(n.get('agent', '')))}, "
                       f"{e(str(n.get('created', '')))})</div>")
        out.append("</div>")
        return "".join(out)
    hidden = f"<input type='hidden' name='t' value='{tok}'>"
    opts = "".join(f"<label class='opt sugg'><input type='radio' name='option' value='{e(o['answer'])}'> "
                   f"<span><b>{e(o['answer'])}</b>{'<br><span class=id>' + e(o['effect']) + '</span>' if o.get('effect') else ''}"
                   f"</span></label>" for o in c.get("options", []))
    ph = "Or write your own (or add to the one you picked)" if opts else "Your decision"
    out.append(
        f"<details class='actions'{' open' if q['kind'] == 'mine' and opts else ''}><summary>Answer…</summary>"
        f"<form method='post' action='/_q/{q['id']}'>{hidden}<input type='hidden' name='action' value='decide'>"
        f"{opts}<textarea name='text' rows='2' placeholder='{ph}'></textarea>"
        "<button>Decide</button></form>"
        f"<form method='post' action='/_q/{q['id']}' class='row'>{hidden}<input type='hidden' name='action' "
        "value='investigate'><input name='text' placeholder='Hint (optional)'><button class='alt'>Steward, find out"
        "</button></form>"
        f"<form method='post' action='/_q/{q['id']}' class='row'>{hidden}<input type='hidden' name='action' value='owner'>"
        "<input name='text' placeholder='New owner, e.g. hermes'><button class='alt'>Change owner</button></form>"
        f"<form method='post' action='/_q/{q['id']}' class='row'>{hidden}<input type='hidden' name='action' "
        "value='postpone'><input name='text' type='date'><button class='alt'>Postpone</button></form>"
        f"<form method='post' action='/_q/{q['id']}' class='row'>{hidden}<input type='hidden' name='action' value='drop'>"
        "<input name='text' placeholder='Reason (optional)'><button class='alt danger'>Drop</button></form>"
        "</details></div>")
    return "".join(out)


def answered_line(q, a, tok):
    """One compact line for a question that has an answer waiting for the steward."""
    last = a[-1]
    c = explain(q) if w.cfg()["web"].get("explain_command") else {}
    name = c.get("title") or re.sub(r"[*`\[\]]", "", questions.REMINDER.sub("", questions.base_text(q["text"])))[:80]
    said = f": {e(last['said'])}" if last.get("said") else ""
    me = re.sub(r"[^a-z0-9-]", "", (w.cfg()["wiki"]["owner"] or w.cfg()["web"]["user"]).lower())
    undo = (f"<form method='post' action='/_q/{q['id']}' class='inline'><input type='hidden' name='t' value='{tok}'>"
            "<input type='hidden' name='action' value='withdraw'><button class='link'>withdraw</button></form>"
            if any(str(x.get("agent", "")).lower() == me for x in a) else "")
    return (f"<li id='q-{q['id']}'><span class='tag ok'>{e(str(last.get('action')))}</span> <b>{e(name)}</b>{said} "
            f"<span class='id'>q-{q['id']} · {e(str(last.get('agent', '')))} {e(str(last.get('created', '')))}</span> "
            f"{undo}</li>")


def gaps_section(tok):
    """Open search gaps on the Open tab: answer one (a note for the steward) or dismiss noise, without waiting for the steward."""
    from .. import stats
    gaps = stats.open_gaps()
    if not gaps:
        return ""
    cards = []
    for g in gaps[:20]:
        qv = e(g["query"], quote=True)
        cards.append(
            f"<div class='q'><div class='qhead'><span class='tag warn'>gap</span> <b>{e(g['query'])}</b> "
            f"<span class='id'>searched {g['count']}x, last {e(g['last'])}</span></div>"
            "<details class='actions'><summary>Answer or dismiss</summary>"
            f"<form method='post' action='/_gap/answer'><input type='hidden' name='t' value='{tok}'>"
            f"<input type='hidden' name='query' value='{qv}'>"
            "<textarea name='text' rows='3' required minlength='20' placeholder='What should the wiki say about this? It "
            "goes to the steward as a note, with your word as the source.'></textarea>"
            "<button>Send as a note</button></form>"
            f"<form method='post' action='/_gap/noise' class='inline'><input type='hidden' name='t' value='{tok}'>"
            f"<input type='hidden' name='query' value='{qv}'><button class='alt'>Not relevant: ignore</button></form>"
            "</details></div>")
    return (f"<div class='secthead'><h2 id='gaps'>Search gaps <span class='count'>{len(gaps)}</span></h2>"
            f"<form method='post' action='/_steward/run' class='inline'><input type='hidden' name='t' value='{tok}'>"
            "<input type='hidden' name='only' value='gaps'><button class='alt sm'>Let the steward review these now</button></form></div>"
            "<p class='id'>Agents searched for these and the wiki found nothing. The steward reviews them on its next run (or now, "
            "with the button: it uses Claude); if you know the answer, say it here.</p>"
            + "".join(cards) + (f"<p class='id'>… and {len(gaps) - 20} more (Stats tab).</p>" if len(gaps) > 20 else ""))


def open_view(pages, query, tok, flash=""):
    owner = w.cfg()["wiki"]["owner"]
    rows, ans = questions.rows(), questions.answers()
    today = datetime.now().date()
    groups = {"you": [], "steward": [], "later": [], "answered": []}
    for q in rows:
        if q["id"] in ans:
            groups["answered"].append(q)
        elif q["kind"] == "mine":
            groups["you"].append(q)
        elif questions.needs_steward(q, today):
            groups["steward"].append(q)
        else:
            groups["later"].append(q)
    out = ["<h1>Open</h1>"]
    if flash:
        out.append(notice(flash))
    out.append(f"<p class='id'>Answers become notes for the steward{f' (as {e(owner)})' if owner else ''}; it carries "
               "them out and edits status.md. Agent-owned questions and due reminders the steward settles by itself.</p>")
    head = {"you": "Waiting for you", "steward": "The steward settles these on its next run",
            "later": "Later: reminders not due yet, questions the steward checked this week"}
    for k in ("you", "steward", "later"):
        if groups[k] or k == "you":
            out.append(f"<h2>{head[k]} <span class='count'>{len(groups[k])}</span></h2>")
            out.append("".join(question_card(q, ans, pages, tok) for q in groups[k])
                       or "<p class='id'>Nothing waits for you. 🎉</p>")
    out.append(gaps_section(tok))
    if groups["answered"]:
        out.append(f"<details class='fold'><summary><h2>Answered, waiting for the steward <span class='count'>"
                   f"{len(groups['answered'])}</span></h2></summary><ul class='plain answered'>"
                   + "".join(answered_line(q, ans[q["id"]], tok) for q in groups["answered"]) + "</ul></details>")
    done = questions.settled(14)
    if done:
        out.append(f"<details class='fold'><summary><h2>Steward actions on questions, last 14 days "
                   f"<span class='count'>{len(done)}"
                   "</span></h2></summary><ul class='plain'>" + "".join(
                       f"<li><span class='when'>{e(d['date'])}</span> <span class='tag'>{e(d['action'])}</span> "
                       f"<span class='id'>q-{e(d['id'])}</span> {wikilinks_html(d['text'], pages)}</li>" for d in done)
                   + "</ul></details>")
    if issues.repo():
        try:
            tix = issues.open_issues()
            out.append(f"<h2>Tickets <span class='count'>{len(tix)}</span> <span class='id'>{e(issues.repo())}</span></h2>")
            out.append("<table class='list'>" + "".join(
                f"<tr><td class='nowrap'><a href='{e(i['url'])}'>#{i['number']}</a></td><td>{e(i['title'])}</td>"
                f"<td><span class='tag'>{e(i['state'])}</span></td></tr>" for i in tix) + "</table>"
                if tix else "<p class='id'>No open tickets.</p>")
        except (RuntimeError, OSError) as err:
            out.append(f"<h2>Tickets</h2><p class='bad'>unavailable: {e(str(err))}</p>")
    notes = w.inbox_notes()
    out.append(f"<h2>Inbox <span class='count'>{len(notes)}</span> <span class='id'>notes waiting for the steward"
               "</span></h2>")
    cards = []
    for n in reversed(notes):
        meta, body = w.split(n.read_text(encoding="utf-8"))
        qa = f" · answers q-{e(str(meta['answers']))}" if meta.get("answers") else ""
        cards.append(f"<details class='note'><summary><span class='tag'>{e(str(meta.get('kind', '?')))}</span> "
                     f"{e(str(meta.get('title', n.stem)))} <span class='id'>{e(str(meta.get('agent', '?')))} · "
                     f"{e(str(meta.get('created', '')))}{' · → ' + e(str(meta['topic'])) if meta.get('topic') else ''}"
                     f"{qa}</span></summary>{render(body, pages)}</details>")
    out.append("".join(cards) or "<p class='id'>Empty.</p>")
    errors, warnings = lint.check(fix=False)
    out.append(f"<h2>Lint <span class='count'>{len(errors)} / {len(warnings)}</span> <span class='id'>errors / "
               "warnings</span></h2><ul class='plain'>" + "".join(f"<li class='bad'>{e(x)}</li>" for x in errors)
               + "".join(f"<li>{e(x)}</li>" for x in warnings if "inbox note" not in x) + "</ul>")
    return "\n".join(out)


def answer(i, form, agent):
    q = questions.find(i)
    if not q:
        return "That question is no longer in status.md (the steward may have settled it)."
    action = form.get("action", [""])[0]
    if action == "withdraw":
        n = questions.withdraw(i, agent)
        return f"Withdrew {n} answer(s) for q-{i}: it is open again." if n else "Nothing to withdraw."
    text = form.get("text", [""])[0].strip() or form.get("option", [""])[0].strip()
    if action == "decide" and form.get("option", [""])[0].strip() and form.get("text", [""])[0].strip():
        text = f"{form['option'][0].strip()} — {form['text'][0].strip()}"
    ok, msg = questions.answer_note(q, action, text, agent)
    if not ok:
        return f"Not saved: {msg}"
    extra = ""
    cmd = w.cfg()["web"].get("on_decision") or []
    if cmd and action == "decide":   # e.g. hand a decision that needs code or host work to a build pipeline
        try:
            r = subprocess.run([config.expand(a, w.ROOT) for a in cmd] + [q["id"], text], capture_output=True,
                               text=True, timeout=120, cwd=w.ROOT)
            extra = " " + (r.stdout.strip() or r.stderr.strip())[-400:]
        except (OSError, subprocess.TimeoutExpired) as err:
            extra = f" (on_decision failed: {err})"
    return f"Saved for q-{q['id']}: the steward carries it out on its next run.{extra}"


# ---------- steward ----------

def form_token(pw):
    return hmac.new(pw.encode(), b"just-wiki-form", hashlib.sha256).hexdigest()[:32]


def plan_items(pages):
    """What a steward run would touch now: {scope: [(label, link)]}."""
    items = {"notes": [], "questions": [], "stale": [], "unchecked": [], "gaps": []}
    for n in w.inbox_notes():
        meta, _ = w.split(n.read_text(encoding="utf-8"))
        scope = "questions" if meta.get("answers") else "notes"
        items[scope].append((f"{meta.get('kind', '?')}: {meta.get('title', n.stem)} ({meta.get('agent', '?')})",
                             f"/_open#q-{meta['answers']}" if meta.get("answers") else "/_open"))
    for q in questions.rows():
        if questions.needs_steward(q):
            items["questions"].append((f"{'due reminder' if q['kind'] == 'reminder' else 'agent question'} q-{q['id']}: "
                                       f"{re.sub(r'[*`]', '', q['text'])[:90]}", f"/_open#q-{q['id']}"))
    for line in lint.stale_pages():
        pid = line.split()[0]
        items["stale"].append((f"stale page {title(pid, pages)}", url(pid)))
    from .. import stats, verify
    for g in stats.open_gaps():
        items["gaps"].append((f"searched {g['count']}x, found nothing: {g['query']}", "/_stats"))
    if verify.settings()["enabled"]:
        for pid, ent in sorted(verify.reconcile().items(), key=lambda x: x[1]["since"]):
            items["unchecked"].append((f"unchecked since {ent['since']}: {title(pid, pages)} "
                                       f"({len(ent['notes'])} note(s))", url(pid)))
    return items


def progress(s, pages):
    """Commits and log lines since the run started (the steward commits once per note, page and row)."""
    out = []
    if s.get("head"):
        r = subprocess.run(["git", "-C", str(w.ROOT), "log", "--format=%h %ad %s", "--date=format:%H:%M",
                            f"{s['head']}..HEAD"], capture_output=True, text=True)
        commits = [line for line in r.stdout.splitlines() if line]
        out.append(f"<h3>Commits so far <span class='count'>{len(commits)}</span></h3><ul class='plain'>" + "".join(
            f"<li><a href='/_changes/{c.split()[0]}' class='sha'>{e(c.split()[0])}</a> <span class='when'>"
            f"{e(c.split()[1])}</span> {e(' '.join(c.split()[2:]))}</li>" for c in reversed(commits)) + "</ul>")
    lines = (w.LOG.read_text(encoding="utf-8").splitlines() if w.LOG.exists() else [])[s.get("log_lines", 0):]
    acts = [x for x in lines if "] steward | " in x]
    if acts:
        out.append("<h3>Log</h3><ul class='plain'>" + "".join(f"<li>{wikilinks_html(a[2:], pages)}</li>"
                                                               for a in reversed(acts)) + "</ul>")
    err = w.ROOT / "logs" / f"steward-{s.get('tag', '')}.err"
    if err.exists() and err.read_text().strip():
        out.append(f"<details><summary>stderr so far</summary><pre>{e(err.read_text()[-3000:])}</pre></details>")
    return "".join(out)


def steward_view(pw, flash=""):
    pages = load()
    s = steward.running()
    out = ["<h1>Steward</h1>"]
    if flash:
        out.append(notice(flash))
    if s.get("running"):
        out.append(f"<div class='card'><b>Running</b> since {e(s.get('started', '?'))}"
                   f"{' (' + e(s['scope']) + ')' if s.get('scope') else ''}. This page refreshes every 5 seconds.</div>")
        out.append(progress(s, pages))
        return "\n".join(out)
    if s.get("started"):
        out.append(f"<p><b>Last run:</b> {e(s.get('started'))} → {e(s.get('ended', '?'))}, exit "
                   f"{e(str(s.get('rc', '?')))}{' (' + e(s['scope']) + ')' if s.get('scope') else ''}</p>")
    items = plan_items(pages)
    names = {"notes": "Inbox notes", "questions": "Open questions", "stale": "Stale pages",
             "unchecked": "Unchecked pages", "gaps": "Search gaps"}
    boxes = "".join(f"<label class='opt'><input type='checkbox' name='only' value='{k}' checked> {names[k]} "
                    f"<span class='count'>{len(items[k])}</span></label>" for k in steward.SCOPES)
    out.append(f"<section class='panel'><h2>Run the steward</h2><form method='post' action='/_steward/run'>"
               f"<input type='hidden' name='t' value='{form_token(pw)}'><div class='scopes'><b>Run on</b> {boxes}</div>"
               "<div class='row' style='margin-top:12px'><button>Run the steward now</button><label class='opt'>"
               "<input type='checkbox' name='force' value='1'> even if nothing waits</label></div></form>"
               "<p class='id' style='margin-bottom:0'>A run uses Claude (or the model set in wiki.toml) and commits what it changes.</p></section>")
    for k in steward.SCOPES:
        if items[k]:
            out.append(f"<div class='secthead'><h2>{names[k]} <span class='count'>{len(items[k])}</span></h2>"
                       f"<form method='post' action='/_steward/run' class='inline'><input type='hidden' name='t' "
                       f"value='{form_token(pw)}'><input type='hidden' name='only' value='{k}'>"
                       f"<button class='alt sm'>Run only this</button></form></div><ul class='plain'>" + "".join(
                           f"<li><a href='{e(link)}'>{e(label)}</a></li>" for label, link in items[k]) + "</ul>")
    if not any(items.values()):
        out.append("<p class='id'>Nothing waits for the steward.</p>")
    logs = sorted((p for p in (w.ROOT / "logs").glob("steward-*.json") if p.name != "steward-run.json"),
                  key=lambda p: p.stat().st_mtime) if (w.ROOT / "logs").is_dir() else []
    if logs:
        last = logs[-1]
        try:
            res = json.loads(last.read_text())
            summary = res.get("result", "") if isinstance(res, dict) else ""
            cost = res.get("total_cost_usd") if isinstance(res, dict) else None
        except ValueError:
            summary, cost = last.read_text()[-3000:], None
        out.append(f"<h2>Last report <span class='id'>{e(last.name)}{f' · ${cost:.2f}' if cost else ''}</span></h2>"
                   + render(summary or "(no summary)", pages))
        err = last.with_suffix(".err")
        if err.exists() and err.read_text().strip():
            out.append(f"<details><summary>stderr</summary><pre>{e(err.read_text()[-4000:])}</pre></details>")
    acts = [line for line in (w.LOG.read_text(encoding="utf-8").splitlines() if w.LOG.exists() else [])
            if "] steward | " in line][-25:]
    if acts:
        out.append("<h2>Recent steward actions</h2><ul class='plain'>" + "".join(
            f"<li>{wikilinks_html(a[2:], pages)}</li>" for a in reversed(acts)) + "</ul>")
    return "\n".join(out)


def start_steward(force, only):
    exe = shutil.which("just-wiki")
    cmd = ([exe] if exe else [sys.executable, "-m", "just_wiki.cli"]) + ["--root", str(w.ROOT), "steward"] + \
        (["--force"] if force else []) + ([f"--only={','.join(only)}"] if only else [])
    if shutil.which("systemd-run"):   # its own unit: survives a restart of the web UI, shows in the journal
        r = subprocess.run(["systemd-run", "--user", "--collect", "--quiet",
                            f"--unit=just-wiki-steward-{int(time.time())}", f"--working-directory={w.ROOT}", *cmd],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return "Steward started."
    subprocess.Popen(cmd, cwd=w.ROOT, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "Steward started."


# ---------- panels ----------

def panels():
    """[(slug, name, command)] of the [[web.panels]] in wiki.toml."""
    out = []
    for pn in w.cfg()["web"].get("panels") or []:
        name, cmd = str(pn.get("name", "")).strip(), pn.get("command") or []
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if slug and cmd:
            out.append((slug, name, cmd))
    return out


def all_tabs():
    mem = (("/_memory", "Memory"),) if w.cfg()["memory"].get("enabled") else ()
    return TABS + mem + tuple((f"/_p/{slug}", name) for slug, name, _ in panels())


def safe_href(h):
    h = str(h or "")
    return h if (h.startswith("/") and not h.startswith("//")) or h.startswith(("http://", "https://")) else ""


def cell_html(c):
    if isinstance(c, list):
        return ", ".join(cell_html(x) for x in c)
    if not isinstance(c, dict):
        return e(str(c))
    txt = e(str(c.get("text", "")))
    if c.get("code") and txt:
        txt = f"<code>{txt}</code>"
    href = safe_href(c.get("href"))
    if href and txt:
        txt = f'<a href="{e(href, quote=True)}">{txt}</a>'
    tag = str(c.get("tag", ""))
    if tag and txt:
        cls = {"ok": "ok", "warn": "warn", "bad": "bad"}.get(tag, "")
        txt = f"<span class='tag {cls}'>{txt}</span>"
    return txt


def plain(c):
    if isinstance(c, list):
        return " ".join(plain(x) for x in c)
    return str(c.get("text", "")) if isinstance(c, dict) else str(c)


def table_html(t):
    cols = "".join(f"<th>{e(str(c))}</th>" for c in t.get("columns", []))
    rows = "".join("<tr>" + "".join(f"<td{' class=nowrap' if len(plain(c)) <= 22 else ''}>{cell_html(c)}</td>" for c in row) + "</tr>"
                   for row in t.get("rows", []))
    return f"<div class='tablewrap'><table class='list'><thead><tr>{cols}</tr></thead><tbody>{rows}</tbody></table></div>"


def bars_html(s):
    vals = [float(b.get("value") or 0) for b in s["bars"]]
    top = max(vals + [1.0])
    title = f"<div class='bars-title'>{e(str(s['bars_title']))}</div>" if s.get("bars_title") else ""
    if s.get("bars_style") == "columns":              # a series over time: one column per entry, labelled at both ends
        cols = "".join(f"<div class='c' style='height:{max(2.0, v / top * 100):.0f}%' "
                       f"title='{e(str(b.get('label', '')), quote=True)}: {e(str(b.get('value', 0)))}'></div>" for b, v in zip(s["bars"], vals, strict=False))
        return (title + f"<div class='colchart'>{cols}</div><div class='colaxis'><span>{e(str(s['bars'][0].get('label', '')))}</span>"
                f"<span>peak {top:.0f}</span><span>{e(str(s['bars'][-1].get('label', '')))}</span></div>")
    bars = "".join(f"<div class='bar'><span class='bl' title='{e(str(b.get('label', '')), quote=True)}'>{e(str(b.get('label', '')))}</span>"
                   f"<span class='bt'><i style='width:{min(100.0, v / top * 100):.0f}%'></i></span>"
                   f"<span class='bv'>{e(str(b.get('value', 0)))}</span></div>" for b, v in zip(s["bars"], vals, strict=False))
    return title + f"<div class='bars'>{bars}</div>"


def section_parts(s):
    """(heading, body, fold) of one panel section."""
    out = []
    if s.get("text"):
        out.append(f"<p class='meta'>{e(str(s['text']))}</p>")
    if s.get("stats"):
        tiles = "".join(
            f"<div class='stat {e(str(t.get('tone', '')) if t.get('tone') in ('ok', 'warn', 'bad') else '')}'>"
            f"<div class='sv'>{e(str(t.get('value', '')))}</div><div class='sl'>{e(str(t.get('label', '')))}</div>"
            f"<div class='sn'>{e(str(t.get('note', '')))}</div></div>" for t in s["stats"])
        out.append(f"<div class='stats'>{tiles}</div>")
    bars = bars_html(s) if s.get("bars") else ""
    table = table_html(s["table"]) if s.get("table") else ""
    out.append(f"<div class='cols'><div>{bars}</div><div>{table}</div></div>" if bars and table else bars + table)
    if s.get("items"):
        out.append("<div class='cards'>" + "".join(
            f"<div class='card'><b>{e(str(i.get('title', '')))}</b>"
            + (f" <span class='tag'>{e(str(i['tag']))}</span>" if i.get("tag") else "")
            + f"<div class='meta'>{e(str(i.get('text', '')))}</div></div>" for i in s["items"]) + "</div>")
    return e(str(s.get("heading", ""))), "".join(out), bool(s.get("fold"))


def section_html(s):
    head, body, fold = section_parts(s)
    if not head:
        return body
    if fold:
        return f"<section class='panel'><details class='fold'><summary><h2>{head}</h2></summary>{body}</details></section>"
    return f"<section class='panel'><h2>{head}</h2>{body}</section>"


def panel_html(data):
    parts = []                                    # a section without a heading continues the one before it
    for s in data.get("sections", []):
        if isinstance(s, dict):
            head, body, fold = section_parts(s)
            if not head and parts:
                parts[-1][1] += body
            else:
                parts.append([head, body, fold])
    sections = "".join(f"<section class='panel'><details class='fold'><summary><h2>{h}</h2></summary>{b}</details></section>" if f
                       else f"<section class='panel'>{'<h2>' + h + '</h2>' if h else ''}{b}</section>" for h, b, f in parts)
    upd = f"<p class='meta'>Updated {e(str(data['updated']))}</p>" if data.get("updated") else ""
    return f"<h1>{e(str(data.get('title', 'Panel')))}</h1>{upd}{sections}"


# ---------- editing: pages and the page folders ----------

def web_user():
    return re.sub(r"[^a-z0-9-]", "", (w.cfg()["wiki"]["owner"] or w.cfg()["web"]["user"]).lower()) or "web"


def notice(msg, kind=""):
    """A message box: red when it starts with "Not ", green otherwise, unless a kind (ok, bad, warn) is given."""
    if not msg:
        return ""
    kind = kind or ("bad" if msg.startswith("Not ") else "ok")
    return f"<div class='notice {kind}'>{e(msg)}</div>"


EDITOR_JS = """<script>
(function(){var f=document.querySelector('form.editor');if(!f)return;var dirty=false,flag=document.getElementById('dirty');
f.addEventListener('input',function(){dirty=true;if(flag)flag.hidden=false});
f.addEventListener('submit',function(){dirty=false});
window.addEventListener('beforeunload',function(e){if(dirty){e.preventDefault();e.returnValue=''}});
document.addEventListener('keydown',function(e){if((e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();
var b=f.querySelector('button[value=save]');if(b)b.click()}});
var t=f.querySelector('[name=title]'),s=f.querySelector('[name=slug]');
if(t&&s){var touched=!!s.value;s.addEventListener('input',function(){touched=true});
t.addEventListener('input',function(){if(!touched)s.value=t.value.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,60)})}
var sel=f.querySelector('[name=folder]'),about=document.getElementById('folder-about');
if(sel&&about){var show=function(){about.textContent=sel.options[sel.selectedIndex].getAttribute('data-about')||''};sel.addEventListener('change',show);show()}
})();
</script>"""
STATUS_HINT = "draft: not checked yet · verified: checked against its sources · stale: needs a re-check"


def page_form(action, tok, f, base="", create_folder=None, slug="", cancel="/", msg="", info="", preview="", remove=None):
    """The editor: the title and text on the left, the properties (status, summary, sources) in a card on the right, and a bar with
    Save, Preview and Cancel that stays in view."""
    status = "".join(f"<option{' selected' if s == f['status'] else ''}>{s}</option>" for s in edit.STATUSES)
    where = ""
    if create_folder is not None:
        opts = "".join(f"<option value='{e(d)}' data-about='{e(w.ABOUT.get(d, ''), quote=True)}'"
                       f"{' selected' if d == create_folder else ''}>{e(d)}/</option>" for d in w.PAGE_DIRS)
        where = (f"<label class='field'><span class='lbl'>Folder</span><select name='folder'>{opts}</select>"
                 "<span class='hint' id='folder-about'></span></label>"
                 f"<label class='field'><span class='lbl'>File name <span class='req'>*</span></span><input name='slug' value='{e(slug)}' "
                 "placeholder='my-new-page' pattern='[a-z0-9][a-z0-9-]*' required>"
                 "<span class='hint'>Lowercase letters, digits and hyphens; it becomes the page id.</span></label>")
    live = ("<div class='notice warn'>Blocks between <code>&lt;!-- live:name --&gt;</code> markers are regenerated from the host by "
            "<code>just-wiki live</code>: change their command in wiki.toml, not their text here.</div>" if "<!-- live:" in f["body"] else "")
    return f"""{notice(msg)}{preview}<form method="post" action="{e(action)}" class="editor" autocomplete="off">
<input type="hidden" name="t" value="{tok}"><input type="hidden" name="base" value="{e(base)}">
<div class="editor-grid"><div class="form">
<label class="field"><span class="lbl">Title <span class="req">*</span></span><input name="title" value="{e(f['title'])}" required maxlength="150"></label>
<label class="field"><span class="lbl">Text</span><span class="hint">Markdown. Link other pages with [[folder/page]]. Ctrl+S saves.</span>
<textarea class="code" name="body" rows="30" spellcheck="false">{e(f['body'])}</textarea></label>{live}</div>
<aside class="sidecard">{where}
<label class="field"><span class="lbl">Status</span><select name="status">{status}</select><span class="hint">{STATUS_HINT}</span></label>
<label class="field"><span class="lbl">Summary</span><input name="summary" value="{e(f['summary'])}" maxlength="300">
<span class="hint">One line, shown in the index and in search results.</span></label>
<label class="field"><span class="lbl">Sources</span><textarea name="sources" rows="5">{e(f['sources'])}</textarea>
<span class="hint">One per line: a file, URL, command or note. A verified page needs at least one.</span></label>{info}</aside></div>
<div class="actionbar"><button type="submit" name="do" value="save">Save</button><button type="submit" name="do" value="preview" class="alt">Preview</button>
<a class="btn alt" href="{e(cancel)}">Cancel</a><span class="grow"></span><span class="id" id="dirty" hidden>Unsaved changes</span>{remove or ""}</div></form>{EDITOR_JS}"""


def preview_html(f):
    """The text as it will look, shown above the editor. Raw HTML is shown as text and comments dropped: a preview runs nothing."""
    body = re.sub(r"<!--.*?-->", "", edit.clean(f["body"]), flags=re.S).replace("<", "&lt;")
    return ("<div class='preview'><span class='tag warn'>Preview: not saved yet</span>"
            + render(body, load()) + "</div>")


def crumbs(*items):
    return "<p class='crumbs'>" + " / ".join(f"<a href='{e(h)}'>{e(t)}</a>" if h else e(t) for t, h in items) + "</p>"


def edit_view(pid, tok, flash="", form=None, base="", preview=""):
    page = edit.load(pid)
    if not page:
        return None
    f = form or page["fields"]
    warn = notice(f"The frontmatter of this page is not valid YAML ({page['error']}); saving rewrites it from the fields below.", "warn") \
        if page["error"] else ""
    info = f"<div class='id'>Type <b>{e(page['type'] or '?')}</b> · {e(pid)}<br>Saved as one git commit; undo it from the Changes tab.</div>"
    remove = f"<a class='btn danger' href='/_delete/{e(pid)}'>Remove page</a>"
    return (crumbs(("Pages", "/"), (pid.split("/")[0], ""), (f["title"] or pid, url(pid)), ("Edit", "")) + f"<h1>Edit “{e(page['fields']['title'] or pid)}”</h1>{warn}"
            + page_form(f"/_edit/{pid}", tok, f, base or page["base"], cancel=url(pid), msg=flash, info=info, preview=preview, remove=remove))


def new_view(tok, flash="", form=None, folder="", slug="", preview=""):
    f = form or {"title": "", "summary": "", "status": "draft", "sources": "", "body": ""}
    return (crumbs(("Pages", "/"), ("New page", "")) + "<h1>New page</h1>"
            + page_form("/_new", tok, f, create_folder=folder or (w.PAGE_DIRS[0] if w.PAGE_DIRS else ""), slug=slug, cancel="/",
                        msg=flash, preview=preview))


def delete_view(pid, tok, pages):
    if not edit.path_of(pid) or pid not in pages or pid in w.TOP_PAGES:
        return None
    refs = edit.linked_from(pid)
    links = (notice("These pages link here and will show a broken link afterwards:", "warn") + "<ul class='plain'>"
             + "".join(f"<li><a href='{url(r)}'>{e(title(r, pages))}</a></li>" for r in refs) + "</ul>") if refs else \
        "<p class='meta'>No other page links here.</p>"
    return (crumbs(("Pages", "/"), (title(pid, pages), url(pid)), ("Remove", "")) + f"<h1>Remove “{e(title(pid, pages))}”?</h1>"
            f"<p class='meta'>{e(pid)}</p>{links}"
            f"<form method='post' action='/_delete/{e(pid)}'><input type='hidden' name='t' value='{tok}'>"
            "<p>The page is removed in one git commit; you can undo that from the Changes tab.</p>"
            "<div class='row'><button class='danger' type='submit' name='confirm' value='1'>Remove the page</button> "
            f"<a class='btn alt' href='{url(pid)}'>Cancel</a></div></form>")


def schema_view(tok, flash=""):
    forms, rows = [], []
    for r in edit.folders():
        f = e(r["folder"])
        forms.append(f"<form id='u-{f}' method='post' action='/_schema/update'><input type='hidden' name='t' value='{tok}'>"
                     f"<input type='hidden' name='folder' value='{f}'></form>")
        rows.append(
            f"<tr><td class='nowrap'><b>{f}/</b></td>"
            f"<td><input form='u-{f}' name='type' value='{e(r['type'])}' size='9' aria-label='type of {f}'></td>"
            f"<td><input form='u-{f}' name='about' value='{e(r['about'])}' aria-label='what belongs in {f}'></td>"
            f"<td>{r['pages']}</td><td><div class='acts'><button form='u-{f}' class='alt sm'>Save</button>"
            f"<details class='actions'><summary>Rename</summary>"
            f"<form method='post' action='/_schema/rename' class='row'><input type='hidden' name='t' value='{tok}'>"
            f"<input type='hidden' name='folder' value='{f}'><input name='new' placeholder='new name' size='12' aria-label='new name'>"
            f"<button class='alt sm'>Rename</button></form></details>"
            f"<a class='danger' href='/_schema/remove/{f}'>Remove…</a></div></td></tr>")
    return (f"<h1>Schema</h1>{notice(flash)}<p class='meta'>The page folders of this wiki. Each has a type (written into the frontmatter "
            "of its pages) and a line saying what belongs there, which the steward uses to route notes. Stored in "
            "<code>wiki.toml</code> under <code>[pages]</code>; every change is one git commit you can undo.</p>"
            + "".join(forms) + "<section class='panel'><h2>Page folders</h2><div class='tablewrap'><table class='list'><thead><tr>"
            "<th>Folder</th><th>Type</th><th>What belongs here</th><th>Pages</th><th></th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>"
            "<p class='id'>Renaming moves the folder's pages and updates every [[link]] to them. Removing asks what happens to the pages.</p></section>"
            f"<section class='panel'><h2>Add a folder</h2><form method='post' action='/_schema/add' class='row'>"
            f"<input type='hidden' name='t' value='{tok}'><input name='folder' placeholder='folder' size='14' required>"
            "<input name='type' placeholder='type' size='10' required><input name='about' placeholder='what belongs here'>"
            "<button>Add folder</button></form></section>")


def remove_folder_view(folder, tok):
    if folder not in w.PAGE_DIRS:
        return None
    plan = edit.removal_plan(folder)
    n, others = len(plan["pages"]), [d for d in w.PAGE_DIRS if d != folder]
    crumb = crumbs(("Schema", "/_schema"), (f"{folder}/", ""), ("Remove", ""))
    if len(w.PAGE_DIRS) == 1:
        return crumb + f"<h1>Remove {e(folder)}/</h1>" + notice("This is the only page folder: a wiki needs at least one.", "warn") + \
            "<p><a class='btn alt' href='/_schema'>Back</a></p>"
    refs = (notice("These pages in other folders link into it:", "warn") + "<ul class='plain'>"
            + "".join(f"<li><code>{e(r)}</code></li>" for r in plan["linked_from"][:15]) + "</ul>") if plan["linked_from"] else ""
    head = (crumb + f"<h1>Remove {e(folder)}/</h1><form method='post' action='/_schema/remove'>"
            f"<input type='hidden' name='t' value='{tok}'><input type='hidden' name='folder' value='{e(folder)}'>")
    if not n:
        return head + ("<p class='meta'>The folder holds no pages.</p><div class='row'><button class='danger' name='action' value='empty'>"
                       "Remove the folder</button> <a class='btn alt' href='/_schema'>Cancel</a></div></form>")
    opts = "".join(f"<option value='{e(d)}'>{e(d)}/ ({e(w.TYPES[d])})</option>" for d in others)
    listing = "".join(f"<li><code>{e(x)}</code></li>" for x in plan["pages"][:15]) + (f"<li>… and {n - 15} more</li>" if n > 15 else "")
    return (head + f"<p>It holds <b>{n}</b> page(s):</p><ul class='plain'>{listing}</ul>{refs}"
            "<section class='panel'><h2>What happens to the pages?</h2>"
            f"<p><label class='opt'><input type='radio' name='action' value='move' checked> Move them to <select name='target'>{opts}</select></label>"
            "<br><span class='id'>Their type changes to that folder's and every [[link]] to them is updated.</span></p>"
            f"<p><label class='opt'><input type='radio' name='action' value='delete'> Delete them with the folder. Type "
            f"<code>{e(folder)}</code> to confirm:</label> <input name='confirm_name' size='16' autocomplete='off' style='width:auto'></p>"
            "<p class='id'>One git commit; undo it from the Changes tab.</p></section>"
            "<div class='row'><button class='danger' type='submit'>Remove the folder</button> <a class='btn alt' href='/_schema'>Cancel</a></div></form>")


def stats_view(q):
    """The Stats tab: who contributes what and where, how the wiki is used (just_wiki/stats.py), for a period."""
    from .. import stats
    raw = (q.get("days") or ["30"])[0]
    days = int(raw) if raw.isdigit() else 30
    periods = ((7, "7 days"), (30, "30 days"), (90, "90 days"), (0, "all time"))
    nav = "<p class='subnav'>" + " ".join(f'<a href="/_stats?days={d}"{" class=here" if d == days else ""}>{label}</a>'
                                          for d, label in periods) + "</p>"
    return nav + panel_html(stats.build(days))


def memory_view():
    """The built-in Memory tab ([memory] enabled): see just_wiki/memory/report.py."""
    from ..memory import report
    return panel_html(report.build())


def panel_view(slug):
    found = next((p for p in panels() if p[0] == slug), None)
    if not found:
        return None
    _, name, cmd = found
    hit = PANEL_CACHE.get(slug)
    if hit and time.time() - hit[0] < 15:
        return hit[1]
    try:
        r = subprocess.run([config.expand(a, w.ROOT) for a in cmd], capture_output=True, text=True, timeout=60,
                           cwd=w.ROOT, stdin=subprocess.DEVNULL)
        if r.returncode == 0:
            html_ = panel_html(json.loads(r.stdout))
        else:
            html_ = (f"<h1>{e(name)}</h1><div class='card bad'>The panel command failed (exit {r.returncode})."
                     f"<pre>{e(r.stderr[-800:])}</pre></div>")
    except (OSError, ValueError, subprocess.TimeoutExpired) as err:
        html_ = f"<h1>{e(name)}</h1><div class='card bad'>The panel command gave no JSON: {e(repr(err))}</div>"
    PANEL_CACHE[slug] = (time.time(), html_)
    return html_


# ---------- server ----------

def shell(current_tab, current_pid, heading, content, pages, refresh=False, side=None):
    name = w.cfg()["web"]["title"] or w.cfg()["wiki"]["name"]
    try:
        mine, stew = open_counts()
    except Exception:
        mine, stew = 0, 0
    badge = {"/_open": f" <span class='badge'>{mine}</span>" if mine else "",
             "/_steward": " <span class='badge dot-run'>●</span>" if steward.running().get("running")
             else f" <span class='badge soft'>{stew}</span>" if stew else ""}
    tabs = "".join(f'<a href="{href}"{" class=here" if href == current_tab else ""}>{lbl}{badge.get(href, "")}</a>'
                   for href, lbl in all_tabs())
    side = (f'<nav><p class="newpage"><a href="/_new">＋ New page</a></p><details open><summary>Pages</summary>{nav(current_pid, pages)}'
            "</details></nav>" if (current_tab == "/" if side is None else side) else "")
    meta = '<meta http-equiv="refresh" content="5">' if refresh else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}
<title>{e(heading)} · {e(name)}</title>
<style>{CSS}</style></head>
<body><header><a class="brand" href="/">{e(name)}</a><div class="tabs">{tabs}</div>
<form action="/_search"><input name="q" type="search" placeholder="Search" aria-label="Search"></form></header>
<div class="wrap{' wide' if not side else ''}">{side}<main>{content}</main></div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def authed(self):
        expected = "Basic " + base64.b64encode(f"{w.cfg()['web']['user']}:{self.server.password}".encode()).decode()
        if hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="wiki", charset="UTF-8"')
        self.end_headers()
        return False

    def do_GET(self):
        if not self.authed():
            return
        pages = load()
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)
        tok = form_token(self.server.password)
        try:
            if path == "/_search":
                term = q.get("q", [""])[0]
                return self.reply(200, shell("/", "", "Search", search_view(term, pages) if term.strip()
                                             else "<p>Type a word.</p>", pages))
            if path == "/_changes":
                return self.reply(200, shell("/_changes", "", "Changes", changes_view(q, pages), pages))
            if path.startswith("/_changes/"):
                return self.reply(200, shell("/_changes", "", "Commit", commit_view(path.split("/")[-1]), pages))
            if path == "/_schedule":
                return self.reply(200, shell("/_schedule", "", "Schedule", schedule_view(), pages))
            if path == "/_open":
                return self.reply(200, shell("/_open", "", "Open", open_view(pages, q, tok, q.get("m", [""])[0]), pages))
            if path.startswith("/_edit/"):
                v = edit_view(path[7:], tok, q.get("m", [""])[0])
                if v is not None:
                    return self.reply(200, shell("/", path[7:], "Edit", v, pages, side=False))
            if path == "/_new":
                return self.reply(200, shell("/", "", "New page", new_view(tok, q.get("m", [""])[0],
                                                                           folder=q.get("folder", [""])[0]), pages, side=False))
            if path.startswith("/_delete/"):
                v = delete_view(path[9:], tok, pages)
                if v is not None:
                    return self.reply(200, shell("/", path[9:], "Remove", v, pages, side=False))
            if path.startswith("/_schema/remove/"):
                v = remove_folder_view(path[16:], tok)
                if v is not None:
                    return self.reply(200, shell("/_schema", "", "Remove folder", v, pages))
            if path == "/_schema":
                return self.reply(200, shell("/_schema", "", "Schema", schema_view(tok, q.get("m", [""])[0]), pages))
            if path == "/_stats":
                return self.reply(200, shell("/_stats", "", "Stats", stats_view(q), pages))
            if path == "/_memory" and w.cfg()["memory"].get("enabled"):
                return self.reply(200, shell("/_memory", "", "Memory", memory_view(), pages))
            if path.startswith("/_p/"):
                body = panel_view(path[4:].strip("/"))
                if body is not None:
                    return self.reply(200, shell(path.rstrip("/"), "", "Panel", body, pages))
            if path == "/_steward":
                return self.reply(200, shell("/_steward", "", "Steward",
                                             steward_view(self.server.password, q.get("m", [""])[0]), pages,
                                             refresh=bool(steward.running().get("running"))))
        except Exception as err:   # one broken view must not take the site down
            return self.reply(500, shell("", "", "Error", f"<h1>Error</h1><pre>{e(repr(err))}</pre>", pages))
        pid = path.strip("/").removesuffix(".md") or "index"
        if pid not in pages:
            return self.reply(404, shell("/", "", "Not found", f"<h1>No page {e(pid)}</h1>", pages))
        self.reply(200, shell("/", pid, title(pid, pages), notice(q.get("m", [""])[0]) + page_view(pid, pages), pages))

    def do_POST(self):
        if not self.authed():
            return
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(min(n, 600_000)).decode())
        if not hmac.compare_digest(form.get("t", [""])[0], form_token(self.server.password)):
            return self.reply(403, "bad form token")
        if path == "/_steward/run":
            only = [x for x in form.get("only", []) if x in steward.SCOPES]
            if not only:
                msg = "Pick at least one of notes, questions, stale pages."
            elif steward.running().get("running"):
                msg = "A steward run is already going."
            else:
                msg = start_steward(form.get("force", [""])[0] == "1", None if set(only) == set(steward.SCOPES) else only)
            return self.redirect("/_steward?m=" + quote(msg))
        one = lambda k: form.get(k, [""])[0]  # noqa: E731
        fields = {k: one(k) for k in ("title", "summary", "status", "sources", "body")}
        if path.startswith("/_edit/"):
            pid, tok = path[7:], form_token(self.server.password)
            if one("do") == "preview":
                v = edit_view(pid, tok, "", fields, one("base"), preview_html(fields))
                return self.reply(200, shell("/", pid, "Edit", v or "<h1>Page not found</h1>", load(), side=False))
            ok, msg = edit.save(pid, fields, one("base"), web_user())
            if ok:
                return self.redirect(url(pid) + "?m=" + quote(msg))
            return self.reply(200, shell("/", pid, "Edit", edit_view(pid, tok, msg, fields, one("base")) or "<h1>Page not found</h1>",
                                         load(), side=False))
        if path == "/_new":
            tok = form_token(self.server.password)
            if one("do") == "preview":
                return self.reply(200, shell("/", "", "New page", new_view(tok, "", fields, one("folder"), one("slug"), preview_html(fields)),
                                             load(), side=False))
            ok, msg, pid = edit.create(one("folder"), fields, one("slug"), web_user())
            if ok:
                return self.redirect(url(pid) + "?m=" + quote(msg))
            return self.reply(200, shell("/", "", "New page", new_view(tok, msg, fields, one("folder"), one("slug")), load(), side=False))
        if path.startswith("/_delete/"):
            pid = path[9:]
            if one("confirm") != "1":
                return self.redirect(f"/_delete/{pid}")
            ok, msg = edit.delete(pid, web_user())
            return self.redirect("/_changes" if ok else url(pid) + "?m=" + quote(msg))
        if path in ("/_gap/answer", "/_gap/noise"):
            from .. import notes as note_store
            from .. import stats
            user, query = web_user(), one("query").strip()
            if path.endswith("noise"):
                ok, msg = stats.resolve(query, "noise", note=f"dismissed by {user} in the web UI")
                msg = f"Ignored: {query}" if ok else msg
            else:
                ok, msg = note_store.submit(user, "finding", f'What the wiki should say about "{query[:80]}"', one("text").strip(),
                                            [f"answer given in the web UI by {user} to the search gap '{query[:100]}'"])
                if ok:
                    stats.resolve(query, "answered", note=f"answered by {user}; the note is waiting for the steward")
                    msg = f"Sent to the steward as a note. {msg}"
            return self.redirect("/_open?m=" + quote(msg if ok else "Not saved: " + msg) + "#gaps")
        if path in ("/_schema/add", "/_schema/update", "/_schema/remove", "/_schema/rename"):
            user = web_user()
            if path.endswith("add"):
                ok, msg = edit.add_folder(one("folder"), one("type"), one("about"), user)
            elif path.endswith("update"):
                ok, msg = edit.set_about(one("folder"), one("type"), one("about"), user)
            elif path.endswith("rename"):
                ok, msg = edit.rename_folder(one("folder"), one("new"), user)
            elif one("action") == "delete" and one("confirm_name") != one("folder"):
                ok, msg = False, "type the folder's name to confirm deleting its pages"
            else:
                ok, msg = edit.remove_folder(one("folder"), user, one("action") or "empty", one("target"))
            return self.redirect("/_schema?m=" + quote(msg if ok else "Not saved: " + msg))
        m = re.fullmatch(r"/_q/([0-9a-f]{6})", path)
        if m:
            agent = re.sub(r"[^a-z0-9-]", "", (w.cfg()["wiki"]["owner"] or w.cfg()["web"]["user"]).lower()) or "web"
            msg = answer(m.group(1), form, agent)
            return self.redirect("/_open?m=" + quote(msg) + f"#q-{m.group(1)}")
        return self.reply(404, "not found")

    def redirect(self, where):
        self.send_response(303)
        self.send_header("Location", where)
        self.end_headers()

    def reply(self, code, body):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def serve(host=None, port=None):
    w.require_root()
    c = w.cfg()["web"]
    host, port = host or c["host"], int(port or c["port"])
    server = ThreadingHTTPServer((host, port), Handler)
    server.password = password()
    print(f"just-wiki web on http://{host}:{port} (user {c['user']}, password in "
          f"{w.ROOT / c.get('password_file', '.web_password')})", flush=True)
    server.serve_forever()
    return 0
