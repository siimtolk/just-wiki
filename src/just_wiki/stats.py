"""Statistics of the wiki for the Stats tab (and `just-wiki stats`): who contributes what and where, and how it is used.

Two sources, because neither is complete alone:
  contributions  the note files (inbox/<agent>/ waiting, inbox/_processed/<agent>/ merged): agent, kind, topic, date. Scripts and
                 tickets write notes without MCP, so the usage log under-counts them. The steward's lines in log.md
                 ("steward | merge <note> → <page>, <page>: …") say which pages a note ended up in.
  usage          logs/usage.jsonl (the MCP server and the file-read hook): calls per client and tool, what is searched for
                 and what comes back, which pages and sections are read, searches that found nothing.

build(days) returns the panel format of web/__init__.py (stat tiles, bars, tables, cards); days = 0 means all time.

  just-wiki stats [days]          the report as text (default 30 days)
  just-wiki stats --json [days]   the panel as JSON

Search gaps: searches that found nothing, grouped by query. The steward reviews the open ones (step 4c of its procedure) and
records an outcome with `just-wiki gaps resolve`; a reviewed gap stays closed unless it keeps coming back.

  just-wiki gaps [--days N] [--all] [--json]                          the open gaps (all: the reviewed ones too)
  just-wiki gaps resolve "<query>" covered|written|question|noise|answered [--page P] [--note TEXT]
"""
import collections
import json
import re
from datetime import date, datetime, timedelta

from . import store as w
from . import usage

ACTION = re.compile(r"^- \[(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}\] steward \| (merge|new|revise|discard|drop|fix|decide|question) "
                    r"(\S+) → ([^:\n]*)")
PAGE_ID = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9._-]+)*$")
UA_NAMES = {"claude-code": "claude-code", "curl": "curl (scripts)", "python-urllib": "python scripts (urllib)",
            "python-requests": "python scripts (requests)", "-": "file read hook"}


def client_name(rec, names):
    """A readable client label. The user agent comes first ([usage] names may map a user-agent product to a label), because every
    local client shares one ip; the ip (names, or the built-in label) is used only when there is no user agent."""
    product = (rec.get("ua") or "-").split("/")[0]
    if product in names:
        return names[product]
    if product.lower() in UA_NAMES and product != "-":
        return UA_NAMES[product.lower()]
    if product != "-":
        return product
    ip = rec.get("ip", "?")
    return names.get(ip) or usage.BUILTIN_NAMES.get(ip) or UA_NAMES["-"] if ip == "claude-code-file" else (names.get(ip) or ip)


def note_records():
    """[{agent, kind, title, created, id, state, topic}] of every note in the inbox, waiting or merged."""
    out = []
    for state, base in (("waiting", w.INBOX), ("merged", w.INBOX / "_processed")):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir() or (state == "waiting" and d.name.startswith("_")):
                continue
            for f in sorted(d.glob("*.md")):
                try:
                    meta, _ = w.split(f.read_text(encoding="utf-8"))
                except OSError:
                    continue
                created = str(meta.get("created") or "")[:16] or f.stem[:10]
                out.append({"agent": str(meta.get("agent") or d.name), "kind": str(meta.get("kind") or "?"),
                            "title": str(meta.get("title") or f.stem), "created": created, "id": f.stem,
                            "state": state, "topic": meta.get("topic") or None})
    return out


def steward_actions():
    """{note stem: (action, [pages])} from the steward's lines in log.md (the last line for a note wins)."""
    out = {}
    if not w.LOG.exists():
        return out
    for line in w.LOG.read_text(encoding="utf-8").splitlines():
        m = ACTION.match(line)
        if m:
            pages = [p.strip().removesuffix(".md") for p in m.group(4).split(",")]
            pages = [p for p in pages if p and p != "-" and PAGE_ID.match(p)]     # free text after the arrow is no page
            out[m.group(3).split("/")[-1]] = (m.group(2), pages)
    return out


def page_table():
    """{page id: {status, updated, folder}}."""
    out = {}
    for p in w.pages():
        meta, _ = w.split(p.read_text(encoding="utf-8"))
        pid = w.page_id(p)
        out[pid] = {"status": str(meta.get("status") or ""), "updated": str(w.as_date(meta.get("updated")) or ""),
                    "folder": pid.split("/")[0], "title": str(meta.get("title") or pid)}
    return out


OUTCOMES = ("covered", "written", "question", "noise", "answered")   # answered: a person answered it in the web UI
REOPEN_AFTER = 2          # a reviewed gap with this many new empty searches since the review is open again


def norm(query):
    return " ".join((query or "").lower().split())


def gaps_file():
    return w.ROOT / "logs" / "gaps-reviewed.json"


def reviewed():
    try:
        return json.loads(gaps_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gaps(days=14):
    """[{query, count, first, last, review}] of the searches that found nothing, most frequent first. review is the steward's
    record (outcome, page, note, at) or None."""
    found = {}
    for r in usage.records(days if days else 36500):
        if r["tool"] == "search" and r.get("outcome") == "miss" and norm(r.get("query")):
            g = found.setdefault(norm(r["query"]), {"query": r["query"].strip(), "times": []})
            g["times"].append(r["ts"])
    done = reviewed()
    out = []
    for key, g in found.items():
        rev = done.get(key)
        out.append({"query": g["query"], "count": len(g["times"]), "first": min(g["times"]), "last": max(g["times"]),
                    "review": rev, "since_review": sum(1 for t in g["times"] if rev and t > rev.get("at", "")) if rev else 0})
    return sorted(out, key=lambda g: (-g["count"], g["query"]))


def is_open(g):
    return g["review"] is None or g["since_review"] >= REOPEN_AFTER


def open_gaps(days=14):
    return [g for g in gaps(days) if is_open(g)]


def resolve(query, outcome, page="", note=""):
    """Record the steward's decision on a gap. Returns (ok, message)."""
    if outcome not in OUTCOMES:
        return False, f"the outcome must be one of {', '.join(OUTCOMES)}"
    if not norm(query):
        return False, "give the query as it appears in `just-wiki gaps`"
    if outcome in ("covered", "written") and page and not w.resolve(page):
        return False, f"{page} is not a page of this wiki"
    data = reviewed()
    data[norm(query)] = {"outcome": outcome, "page": page, "note": " ".join(note.split())[:300], "at": w.now()}
    f = gaps_file()
    f.parent.mkdir(exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)
    return True, f"{query!r}: {outcome}"


def gaps_main(args):
    w.require_root()
    if args and args[0] == "resolve":
        pos = [a for a in args[1:] if not a.startswith("--")]
        opt = {a: args[args.index(a) + 1] for a in ("--page", "--note") if a in args and args.index(a) + 1 < len(args)}
        pos = [a for a in pos if a not in opt.values()]
        if len(pos) < 2:
            print('usage: just-wiki gaps resolve "<query>" covered|written|question|noise|answered [--page P] [--note TEXT]')
            return 2
        ok, msg = resolve(pos[0], pos[1], opt.get("--page", ""), opt.get("--note", ""))
        print(msg)
        return 0 if ok else 1
    days = int(args[args.index("--days") + 1]) if "--days" in args else 14
    rows = gaps(days) if "--all" in args else open_gaps(days)
    if "--json" in args:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    for g in rows:
        rev = g["review"]
        state = f"  [{rev['outcome']}{' → ' + rev['page'] if rev['page'] else ''}, {rev['at'][:10]}]" if rev else ""
        print(f"{g['count']:3}x  {g['query']}   (last {g['last']}){state}")
    if not rows:
        print(f"no open search gaps in the last {days} days")
    return 0


def tile(label, value, note="", tone=""):
    return {"label": label, "value": str(value), "note": note, "tone": tone}


def link(text, page):
    return {"text": text, "href": "/" + page} if page else text


def build(days=30):
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M") if days else ""
    names = {**w.cfg()["usage"]["names"]}
    recs = [r for r in usage.records(36500 if not days else days)]
    notes = [n for n in note_records() if n["created"] >= since]
    actions = steward_actions()
    pages = page_table()
    folders = sorted({p["folder"] for p in pages.values()})
    where = f"last {days} days" if days else "all time"

    # ---- usage numbers
    by_client = collections.defaultdict(collections.Counter)
    for r in recs:
        by_client[client_name(r, names)][r["tool"]] += 1
        if r["tool"] == "search" and r.get("outcome") == "miss":
            by_client[client_name(r, names)]["_miss"] += 1
    searches = [r for r in recs if r["tool"] in ("search", "search_text")]
    reads = [r for r in recs if r["tool"] == "read_page" and r.get("outcome") in ("ok", "clipped")]
    misses = [r for r in recs if r["tool"] == "search" and r.get("outcome") == "miss"]
    read_count = collections.Counter(r["page"] for r in reads if r.get("page"))
    found_count = collections.Counter(p for r in searches for p in r.get("hits", []))
    merged_into = collections.Counter()
    for n in notes:
        act = actions.get(n["id"])
        if act and act[0] in ("merge", "new", "revise"):
            merged_into.update(act[1])
        elif n["topic"] and n["state"] == "merged" and not act:
            merged_into[n["topic"]] += 1
    state_count = collections.Counter(("discarded" if actions.get(n["id"], ("",))[0] in ("discard", "drop") else n["state"])
                                      for n in notes)

    sections = []
    sections.append({"heading": f"Overview ({where})", "stats": [
        tile("MCP calls", len(recs), f"{len(by_client)} client(s)"),
        tile("notes written", len(notes), f"{state_count['merged']} merged, {state_count['waiting']} waiting, "
                                           f"{state_count['discarded']} discarded", "warn" if state_count["waiting"] else ""),
        tile("pages read", len(read_count), f"of {len(pages)} pages, {len(reads)} reads"),
        tile("searches", len(searches), f"{len(misses)} found nothing", "warn" if misses else ""),
        tile("pages", len(pages), f"{sum(1 for p in pages.values() if p['status'] == 'verified')} verified, "
                                  f"{sum(1 for p in pages.values() if p['status'] != 'verified')} draft or stale"),
        tile("rejected notes", sum(1 for r in recs if r["tool"] == "submit_note" and r.get("outcome") == "rejected"),
             "sources missing, too short, or a secret")]})

    # ---- who contributes
    by_agent = collections.defaultdict(lambda: {"n": 0, "kinds": collections.Counter(), "merged": 0, "waiting": 0,
                                                "discarded": 0, "last": ""})
    for n in notes:
        e = by_agent[n["agent"]]
        e["n"] += 1
        e["kinds"][n["kind"]] += 1
        act = actions.get(n["id"], ("",))[0]
        e["discarded" if act in ("discard", "drop") else "merged" if n["state"] == "merged" else "waiting"] += 1
        e["last"] = max(e["last"], n["created"])
    rows = [[a, str(e["n"]), ", ".join(f"{k} {c}" for k, c in e["kinds"].most_common()), str(e["merged"]), str(e["waiting"]),
             str(e["discarded"]), e["last"][5:]] for a, e in sorted(by_agent.items(), key=lambda kv: -kv[1]["n"])]
    sections.append({"heading": "Who contributes", "bars_title": "Notes per agent",
                     "bars": [{"label": a, "value": e["n"]} for a, e in sorted(by_agent.items(), key=lambda kv: -kv[1]["n"])[:10]],
                     "text": "Every note file counts, also those scripts and tickets write without MCP. 'Discarded' = the steward "
                             "found the wiki already right or the note not worth keeping."
                             + ("" if rows else " No notes in this period."),
                     "table": {"columns": ["Agent", "Notes", "Kinds", "Merged", "Waiting", "Discarded", "Last note"], "rows": rows}
                     if rows else None})

    # ---- to which topics
    by_folder = collections.defaultdict(collections.Counter)
    for n in notes:
        for p in (actions.get(n["id"], ("", []))[1] or ([n["topic"]] if n["topic"] else [])):
            by_folder[p.split("/")[0]][n["agent"]] += 1
    matrix_agents = [a for a, _ in sorted(by_agent.items(), key=lambda kv: -kv[1]["n"])[:6]]
    frows = [[f, str(sum(c.values())), *[str(c.get(a, 0)) for a in matrix_agents]]
             for f, c in sorted(by_folder.items(), key=lambda kv: -sum(kv[1].values()))]
    sections.append({"heading": "Where the notes go",
                     "text": "Pages the steward merged each note into (from log.md); a note without a log line counts under its "
                             "topic. Rows are page folders, columns are the busiest agents.",
                     "bars_title": "Most changed pages (notes merged in)",
                     "bars": [{"label": p, "value": c} for p, c in merged_into.most_common(10)],
                     "table": {"columns": ["Folder", "Notes", *matrix_agents], "rows": frows} if frows else None})

    # ---- over time
    span = min(days or 30, 30)
    day_list = [(date.today() - timedelta(days=i)).isoformat() for i in range(span - 1, -1, -1)]
    notes_day = collections.Counter(n["created"][:10] for n in notes)
    calls_day = collections.Counter(r["ts"][:10] for r in recs)
    sections.append({"heading": "Over time", "bars_title": f"MCP calls per day (last {span} days)",
                     "bars_style": "columns", "bars": [{"label": d[5:], "value": calls_day[d]} for d in day_list]})
    sections.append({"heading": "", "bars_title": f"Notes per day (last {span} days)",
                     "bars_style": "columns", "bars": [{"label": d[5:], "value": notes_day[d]} for d in day_list]})

    # ---- how agents use it
    crow = []
    for c, t in sorted(by_client.items(), key=lambda kv: -sum(v for k, v in kv[1].items() if k != "_miss")):
        total = sum(v for k, v in t.items() if k != "_miss")
        s, rd = t["search"] + t["search_text"], t["read_page"]
        crow.append([c, str(total), str(s), str(rd), str(t["get_status"] + t["get_index"]), str(t["submit_note"]),
                     {"text": str(t["_miss"]), "tag": "warn" if t["_miss"] else ""} if t["_miss"] else "0"])
    sections.append({"heading": "How the wiki is used",
                     "text": "Clients are told apart by their user agent (name them in [usage] names in wiki.toml: a user-agent "
                             "product or an ip → a label). Notes here are only those sent over MCP."
                             + ("" if crow else " No calls in this period."),
                     "table": {"columns": ["Client", "Calls", "Searches", "Page reads", "Status/index", "Notes", "Empty searches"],
                               "rows": crow} if crow else None})

    # ---- what they look for
    queries = collections.Counter(r.get("query", "").strip().lower() for r in searches if r.get("query"))
    sec_read = collections.Counter((r["page"], r["section"]) for r in reads if r.get("section") and r.get("page"))
    sections.append({"heading": "What agents look for",
                     "bars_title": "Most searched terms", "bars": [{"label": q[:40], "value": c} for q, c in queries.most_common(10)],
                     "text": "Queries as agents typed them; 'Pages found by search' counts how often a page was among the hits."})
    prow = [[link(p, p), str(found_count.get(p, 0)), str(read_count.get(p, 0)), str(merged_into.get(p, 0)),
             pages.get(p, {}).get("status", ""), pages.get(p, {}).get("updated", "")]
            for p in sorted(set(found_count) | set(read_count), key=lambda p: (-(read_count.get(p, 0) + found_count.get(p, 0)), p))[:15]
            if p in pages]
    sections.append({"heading": "Most used pages", "table": {
        "columns": ["Page", "Found by search", "Read", "Notes merged in", "Status", "Updated"], "rows": prow} if prow else None,
        "text": "" if prow else "No page reads or searches in this period."})
    if sec_read:
        sections.append({"heading": "Most read sections", "table": {"columns": ["Page", "Section", "Reads"], "rows": [
            [link(p, p) if p in pages else p, s, str(c)] for (p, s), c in sec_read.most_common(10)]}})

    # ---- by folder
    frow = []
    for f in folders:
        ps = [p for p, m in pages.items() if m["folder"] == f]
        frow.append([f, str(len(ps)), str(sum(read_count.get(p, 0) for p in ps)), str(sum(found_count.get(p, 0) for p in ps)),
                     str(sum(merged_into.get(p, 0) for p in ps)), str(sum(1 for p in ps if not read_count.get(p)))])
    sections.append({"heading": "By folder", "table": {"columns": ["Folder", "Pages", "Reads", "Found by search", "Notes merged in",
                                                                    "Never read"], "rows": frow}})

    # ---- search gaps and what the steward did about them
    all_gaps = gaps(days)
    grows = []
    for g in all_gaps[:30]:
        rev = g["review"]
        status = ({"text": rev["outcome"], "tag": "ok" if rev["outcome"] in ("covered", "written", "answered") else ""} if rev and not is_open(g)
                  else {"text": "open" if not rev else "back again", "tag": "warn"})
        grows.append([g["query"], str(g["count"]), g["last"], status,
                      link(rev["page"], rev["page"]) if rev and rev.get("page") else "", (rev or {}).get("note", "")])
    sections.append({"heading": "Search gaps",
                     "text": "Searches that found nothing. The steward reviews the open ones (adds the term to a page, writes the answer "
                             "from a source, asks a person, or ignores noise) and records it; a reviewed gap that keeps coming back "
                             "opens again." + ("" if grows else " None in this period."),
                     "table": {"columns": ["Query", "Times", "Last", "Status", "Page", "Note"], "rows": grows} if grows else None})

    # ---- worth a look
    look = []
    for g in [g for g in all_gaps if is_open(g)][:5]:
        look.append({"title": f"Searched {g['count']}x and found nothing: {g['query']}", "text": "The steward reviews this on its next "
                     "run: a missing page, or the term is worded differently from how agents ask.", "tag": "gap"})
    for p, c in read_count.most_common(30):
        if pages.get(p, {}).get("status") in ("draft", "stale") and c >= 2:
            look.append({"title": f"{p}: read {c} times but {pages[p]['status']}", "text": "A page agents rely on should be verified "
                         "or refreshed.", "tag": pages[p]["status"]})
    for a, e in by_agent.items():
        if e["waiting"] >= 5:
            look.append({"title": f"{e['waiting']} notes from {a} are waiting", "text": "The steward has not merged them yet.",
                         "tag": "waiting"})
    sections.append({"heading": "Worth a look", "items": look[:10], "text": "" if look else "Nothing stands out in this period."})
    for s in sections:
        if s.get("table") is None:
            s.pop("table", None)
    return {"title": "Stats", "updated": datetime.now().strftime("%Y-%m-%d %H:%M"), "sections": sections}


def text(p):
    out = [f"{p['title']} ({p['updated']})"]
    for s in p["sections"]:
        if s.get("heading"):
            out.append(f"\n## {s['heading']}")
        out += [f"  {t['label']}: {t['value']} ({t['note']})" for t in s.get("stats", [])]
        out += [f"  {b['value']:>5}  {b['label']}" for b in s.get("bars", [])]
        for row in (s.get("table") or {}).get("rows", [])[:12]:
            out.append("  " + " | ".join(c if isinstance(c, str) else (c.get("text", "") if isinstance(c, dict) else "") for c in row))
        out += [f"  - {i['title']}" for i in s.get("items", [])]
    return "\n".join(out)


def main(args):
    w.require_root()
    nums = [a for a in args if a.isdigit()]
    p = build(int(nums[0]) if nums else 30)
    print(json.dumps(p, ensure_ascii=False) if "--json" in args else text(p))
    return 0
