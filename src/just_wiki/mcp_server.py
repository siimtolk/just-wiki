"""The wiki over MCP (streamable HTTP, path /mcp) for every agent.

Read: get_index, get_status, search, read_page (+ list_tickets, read_ticket when [tickets] github_repo is set).
Write: submit_note only, which lands a new file in inbox/<agent>/ for the steward. Agents never edit curated pages, so
several can write at once without conflicts, and every change to a page goes through one reviewed step.

Clients on localhost need no auth; every other client needs `Authorization: Bearer <token>`, the token being in
.mcp_token at the wiki root (created on first start, 0600). Every call is logged in logs/usage.jsonl.
"""
import fcntl
import functools
import hmac
import json
import re
import secrets
import sqlite3
import threading

from . import issues, notes
from . import store as w

MAX_CHARS = 12000           # per read; the cut is always reported, never silent
LOCAL = ("127.0.0.1", "::1")
LOGGED_ARGS = ("agent", "kind", "title", "topic", "query", "limit", "include_inbox", "page", "section", "state", "number",
               "max_tokens")
DEFAULT_INSTRUCTIONS = (
    "Shared wiki: the one source for facts kept here ({folders}). "
    "For any question about it, look here BEFORE probing or guessing: get_status (current state and open "
    "questions), then search (matching sections) or search_text (their text), and read_page; get_index for "
    "the catalogue. Trust 'verified' pages; check 'draft'/'stale' ones before acting and say so. "
    "Write only with submit_note, in the same turn, before you reply, when " + notes.WRITE_WHEN + ". "
    "One fact per note, with numbers, dates, paths and a real source; none for chat, opinions or one-off errors. "
    "Never put secrets, tokens or passwords in a note.")
STOP = set("""a an and are as at be by can do does for from has have how i in is it its of on or
should the this to was what when where which who why will with""".split())

_lock = threading.Lock()
_db = {"sig": None, "conn": None}


def _corpus():
    files = [(p, "page") for p in w.pages()] + [(p, "inbox (unreviewed)") for p in w.inbox_notes()]
    return files, tuple((str(p), p.stat().st_mtime) for p, _ in files)


def sections(body):
    """[(heading, text)] of a page body at its '## ' headings; the part before the first one is the page's own
    section (heading ''). A section keeps its '### ' subsections."""
    parts = re.split(r"(?m)^(?=## )", body)
    out = []
    for p in parts:
        if not p.strip():
            continue
        if p.startswith("## "):
            out.append((p.splitlines()[0][3:].strip(), p.strip()))
        elif re.sub(r"(?m)^# .*$", "", p).strip():
            out.append(("", p.strip()))
    return out


def _index():
    """In-memory FTS5 over page sections + pending notes (one row each), rebuilt when any file changes. The search
    grain is the section: a question finds the part of a page that answers it, not the whole page."""
    files, sig = _corpus()
    with _lock:
        if sig != _db["sig"]:
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            conn.execute("CREATE VIRTUAL TABLE docs USING fts5(id, section, kind, status, updated, title, summary,"
                         " body, tokenize='unicode61 remove_diacritics 2')")
            for path, kind in files:
                meta, body = w.split(path.read_text(encoding="utf-8"))
                row = (w.page_id(path), kind, str(meta.get("status", meta.get("kind", ""))),
                       str(meta.get("updated", meta.get("created", ""))), str(meta.get("title", "")),
                       str(meta.get("summary", "")))
                parts = sections(body) if kind == "page" else [("", body)]
                for head, text in parts or [("", body)]:
                    conn.execute("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?)", (row[0], head, *row[1:], text))
            _db.update(sig=sig, conn=conn)
        return _db["conn"]


def _hits(query, limit, include_inbox):
    q = _fts_query(query)
    if not q:
        return None
    return _index().execute(
        "SELECT id, section, kind, status, updated, title, summary, snippet(docs, 7, '«', '»', ' … ', 24), body,"
        " bm25(docs, 0, 4.0, 0, 0, 0, 4.0, 2.0, 1.0)"
        " FROM docs WHERE docs MATCH ?" + ("" if include_inbox else " AND kind = 'page'")
        + " ORDER BY bm25(docs, 0, 4.0, 0, 0, 0, 4.0, 2.0, 1.0) LIMIT ?", (q, limit)).fetchall()


def _fts_query(text):
    """Keywords OR'ed, each as a prefix ('model' finds 'models'); question words dropped."""
    words = [x for x in re.findall(r"\w+", text.lower(), re.UNICODE) if len(x) > 1 and x not in STOP]
    return " OR ".join(f'"{x}"*' for x in words)


# ---------- the tools, as plain functions (tested without a server) ----------

def get_index() -> str:
    """The catalogue: every wiki page with a one-line summary. Read this first."""
    f = w.ROOT / "index.md"
    return f.read_text(encoding="utf-8") if f.exists() else "Empty wiki: no index.md yet (just-wiki lint --fix)."


def get_status() -> str:
    """Current state of the whole setup and open questions with owners."""
    f = w.ROOT / "status.md"
    return f.read_text(encoding="utf-8") if f.exists() else "No status.md yet."


def search(query: str, limit: int = 5, include_inbox: bool = True) -> str:
    """Full-text search over wiki pages and not-yet-merged inbox notes.

    The wiki is written in English: search with English keywords and names, even when the user wrote in another
    language. include_inbox: False searches only the curated pages (no unchecked inbox notes); use it when you will
    act on the facts. Returns page ids with status, date and a snippet; open one with read_page(page).
    """
    rows = _hits(query, max(1, min(int(limit), 15)), include_inbox)
    if rows is None:
        return "Empty query: give a few keywords."
    if not rows:
        return f"No matches for {query!r}. Try other words, or get_index."
    return "\n\n".join(f"{i}. {pid}" + (f" › {sec}" if sec else "") + f" — {title} [{kind}, {status}, {updated}]\n"
                       f"   {snip}" for i, (pid, sec, kind, status, updated, title, _s, snip, _b, _sc) in enumerate(rows, 1))


DISAGREE = "Disagreements & open questions"


def _disagreements(pids, seen):
    """The 'Disagreements & open questions' section of each page, for the pages a search answer draws on: a change or
    a contradiction is usually recorded there, not in the section that matched the question's words."""
    out = []
    for pid in pids:
        row = _index().execute("SELECT body FROM docs WHERE id = ? AND section LIKE ? LIMIT 1",
                               (pid, DISAGREE[:12] + "%")).fetchone()
        if row and row[0].strip() and row[0].strip() not in seen:
            seen.add(row[0].strip())
            out.append(f"### {DISAGREE} ({pid})\n{row[0].strip()[:1500]}\n")
    return out


def search_text(query: str, max_tokens: int = 0, include_inbox: bool = True) -> str:
    """Full-text search that returns the text itself: the best-matching page sections (and inbox notes), best
    first. Each comes with its page id, title and summary. Use it to answer a question in one step.

    max_tokens: 0 (default) gives a focused answer of about 3,000 tokens: the sections that match the question, no
    repeats, and the open disagreements recorded on the same pages (where changes and contradictions are noted). A
    number (500-12000) fills that budget with the best matches instead.
    """
    focused = not max_tokens
    max_tokens = 3000 if focused else max(500, min(int(max_tokens), 12000))
    rows = _hits(query, 40, include_inbox)
    if rows is None:
        return "Empty query: give a few keywords."
    if not rows:
        return f"No matches for {query!r}. Try other words, or get_index."
    best = rows[0][9]                                   # bm25 is negative: more negative = better
    out, used, seen, pages = [], 0, set(), []
    for n, (pid, sec, kind, status, updated, title, summary, _snip, body, score) in enumerate(rows):
        if focused and n >= 3 and score > best * 0.25:
            break                                       # the rest match much worse than the best
        if body.strip() in seen:
            continue
        seen.add(body.strip())
        block = (f"### {title}" + (f" › {sec}" if sec else "") + f" ({pid}; {kind}, {status}, {updated})\n"
                 + (f"{summary}\n\n" if summary and not sec else "") + body.strip() + "\n")
        cost = len(block) // 4
        if used + cost > max_tokens:
            if not out and cost > max_tokens:       # one huge section: give its start
                out.append(block[:max_tokens * 4] + "\n[CLIPPED: read_page for the rest]\n")
            continue
        out.append(block)
        used += cost
        if kind == "page" and pid not in pages:
            pages.append(pid)
    if focused:
        out += _disagreements(pages, seen)
    return "\n".join(out)


def read_page(page: str = "", section: str = "", id: str = "", page_id: str = "", name: str = "") -> str:
    """Read one page by id, e.g. 'topics/reading-plan' (folder/name, as in the index). Give it as `page`. Optional
    section: a heading's text, to read only that part. (`id`, `page_id` and `name` are accepted as the same thing:
    agents often guess them, and a rejected call costs a whole turn.)"""
    page = page or id or page_id or name
    if not page:
        return "Give the page id as `page`, e.g. read_page(page='systems/host'). Use get_index for the ids."
    path = w.resolve(page)
    if not path:
        return f"No page {page!r}. Use get_index or search for the right id."
    text = path.read_text(encoding="utf-8")
    over = int(w.cfg()["mcp"].get("outline_over") or 0)
    if over and not section and len(text) > over:        # a long page: its outline and first section, not all of it
        meta, body = w.split(text)
        parts = sections(body)
        if len(parts) > 1:
            lines = [f"{h or '(intro)'} — {len(t):,} chars" for h, t in parts]
            return (f"(status: {meta.get('status')}, updated: {meta.get('updated')}) This page is long "
                    f"({len(text):,} characters), so here are its sections and the first one. Read another with "
                    f"read_page(page={page!r}, section='<heading>'), or find the right one with search_text.\n\n"
                    "Sections:\n- " + "\n- ".join(lines) + "\n\n" + parts[0][1][:over // 2])
    if section:
        meta, body = w.split(text)
        parts = re.split(r"(?m)^(?=#{1,4} )", body)
        hit = [s for s in parts if s.startswith("#") and section.lower() in s.splitlines()[0].lower()]
        if not hit:
            heads = [s.splitlines()[0] for s in parts if s.startswith("#")]
            return f"No section {section!r} in {page}. Headings: " + "; ".join(heads)
        text = f"(status: {meta.get('status')}, updated: {meta.get('updated')})\n\n" + "\n".join(hit)
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + (f"\n\n[CLIPPED: showed {MAX_CHARS} of {len(text)} characters. "
                                   "Ask for one section with read_page(page, section=...).]")
    return text


def list_tickets(state: str = "") -> str:
    """Open work tickets (GitHub issues): number, state, other labels, title.

    state: a ticket state (the issue's state:<x> label), or empty for all open tickets. Read-only.
    """
    try:
        rows = issues.open_issues()
    except (RuntimeError, OSError) as err:
        return f"Tickets unavailable: {err}"
    rows = [i for i in rows if not state or i["state"] == state.strip()]
    if not rows:
        return "No open tickets" + (f" in state {state!r}." if state else ".")
    return "\n".join(f"#{i['number']} [{', '.join([i['state']] + [v for k, v in i['labels'].items() if k != 'state'])}]"
                     f" {i['title']}" for i in rows)


def read_ticket(number: int) -> str:
    """One ticket in full: its description and comment thread."""
    try:
        return issues.view(number)[:MAX_CHARS]
    except (RuntimeError, OSError) as err:
        return f"Ticket #{number} unavailable: {err}"


def submit_note(agent: str, kind: str, title: str, body: str, sources: list[str], topic: str = "") -> str:
    """Contribute to the wiki. The note goes to the inbox; the steward merges it into the right page.

    agent:   your name, lowercase, e.g. 'hermes'
    kind:    finding | result | decision | question | correction
    title:   one line saying what this is
    body:    the content, in Markdown; facts with numbers, dates and paths, not chat
    sources: where it comes from: file paths, URLs, commands run, log names. At least one.
    topic:   the page it belongs to, a page id from the index (folder/name), or empty if new or unsure
    """
    return notes.submit(agent, kind, title, body, sources, topic)[1]


# ---------- the server ----------

def _record(rec):
    f = w.ROOT / "logs" / "usage.jsonl"
    f.parent.mkdir(exist_ok=True)
    with open(f, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fcntl.flock(fh, fcntl.LOCK_UN)


def usage_record(tool, kwargs, out, ip, ua):
    """One line of logs/usage.jsonl. read_page takes its page as page, id, page_id or name: it is logged as `page` whichever
    was used, so the statistics never miss a read."""
    outcome = ("rejected" if out.startswith("REJECTED") else "miss" if out.startswith(("No ", "Empty"))
               else "clipped" if "[CLIPPED:" in out else "ok")
    rec = {"ts": w.now(), "ip": ip, "ua": ua, "tool": tool, "outcome": outcome, "chars": len(out),
           **{k: v for k, v in kwargs.items() if k in LOGGED_ARGS and v not in ("", None)}}
    if tool == "read_page" and not rec.get("page"):
        page = kwargs.get("id") or kwargs.get("page_id") or kwargs.get("name")
        if page:
            rec["page"] = page
    if tool == "search":
        rec["hits"] = re.findall(r"^\d+\. (\S+) —", out, re.M)
    return rec


def build(mcp_cls=None):
    """The FastMCP app with every tool registered and logged."""
    from mcp.server.fastmcp import FastMCP
    c = w.cfg()["mcp"]
    mcp = (mcp_cls or FastMCP)("wiki", host=c["host"], port=int(c["port"]), stateless_http=True,
                               instructions=c["instructions"] or DEFAULT_INSTRUCTIONS.format(folders=", ".join(w.PAGE_DIRS)))

    def client():
        try:
            req = mcp.get_context().request_context.request
            return req.client.host, req.headers.get("user-agent", "")[:60]
        except Exception:
            return "?", ""

    def logged(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            out = fn(*args, **kwargs)
            ip, ua = client()
            _record(usage_record(fn.__name__, kwargs, out, ip, ua))
            return out
        return inner

    tools = [get_index, get_status, search, search_text, read_page, submit_note]
    if issues.repo():
        tools[5:5] = [list_tickets, read_ticket]
    for fn in tools:
        mcp.tool(structured_output=False)(logged(fn))     # text only: the structured copy doubled every result
    return mcp


def token():
    f = w.ROOT / ".mcp_token"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(32) + "\n")
        f.chmod(0o600)
    return f.read_text().strip()


def require_token_off_localhost(app, tok):
    """ASGI wrapper: localhost passes, every other client needs the bearer token."""
    want = f"Bearer {tok}".encode()

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and (scope.get("client") or ("",))[0] not in LOCAL:
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, want):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"bearer token required off localhost"})
                return
        await app(scope, receive, send)
    return guarded


def serve():
    import uvicorn
    w.require_root()
    c = w.cfg()["mcp"]
    mcp = build()
    uvicorn.run(require_token_off_localhost(mcp.streamable_http_app(), token()),
                host=c["host"], port=int(c["port"]), log_level="info")
    return 0
