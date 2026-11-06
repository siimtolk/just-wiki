"""Who uses the wiki and how, from logs/usage.jsonl (written by the MCP server and the file-read hook).

  just-wiki usage [days]          report (default 7 days)
  just-wiki usage --json [days]   the same as one JSON object

Shows calls per client and tool, searches that found nothing (gaps in the wiki), pages read most, and pages nobody
read. Client names come from [usage] names in wiki.toml (ip → label).
"""
import json
from collections import Counter
from datetime import datetime, timedelta

from . import store as w

BUILTIN_NAMES = {"127.0.0.1": "localhost", "::1": "localhost", "claude-code-file": "claude-code (file read)"}


def usage_log():
    return w.ROOT / "logs" / "usage.jsonl"


def filter_since(recs, days, now=None):
    """Keep records whose ts is within the last `days` days (string compare, minute precision)."""
    now = now or datetime.now()
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    return [r for r in recs if r.get("ts", "") >= since]


def records(days):
    f = usage_log()
    if not f.exists():
        return []
    raw = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            raw.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return filter_since(raw, days)


def summary(recs, days, page_ids, names=None):
    """One dict with everything the report and --json print. page_ids is passed in so tests need no wiki."""
    names = {**BUILTIN_NAMES, **(names or {})}
    reads = Counter(r["page"] for r in recs if r["tool"] == "read_page" and r["outcome"] in ("ok", "clipped") and r.get("page"))
    misses = [r["query"] for r in recs if r["tool"] == "search" and r["outcome"] == "miss"]
    notes = [r for r in recs if r["tool"] == "submit_note"]
    return {
        "days": days,
        "calls": len(recs),
        "by_client_tool": [{"client": c, "tool": t, "count": n} for (c, t), n in
                           Counter((names.get(r["ip"], r["ip"]), r["tool"]) for r in recs).most_common()],
        "notes": [{"ts": n["ts"], "agent": n.get("agent", "?"), "outcome": n.get("outcome", ""),
                   "title": n.get("title", "")} for n in notes],
        "notes_ok": sum(n["outcome"] == "ok" for n in notes),
        "notes_rejected": sum(n["outcome"] == "rejected" for n in notes),
        "empty_searches": [{"query": q, "count": n} for q, n in Counter(misses).most_common(15)],
        "empty_search_count": len(misses),
        "pages_read": sum(reads.values()),
        "most_read": [{"page": p, "count": n} for p, n in reads.most_common(10)],
        "unread": sorted(set(page_ids) - set(reads)),
    }


def current(days=7):
    return summary(records(days), days, {w.page_id(p) for p in w.pages()}, w.cfg()["usage"]["names"])


def run(days=7, as_json=False, out=print):
    w.require_root()
    s = current(days)
    if as_json:
        out(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    out(f"Wiki usage, last {days} days: {s['calls']} calls")
    out("\nBy client and tool:")
    for row in s["by_client_tool"]:
        out(f"  {row['count']:4}  {row['client']:28} {row['tool']}")
    out("\nNotes:")
    for n in s["notes"]:
        out(f"  {n['ts']}  {n['agent']:14} {n['outcome']:8} {n['title']}")
    out("\nSearches with no result (possible gaps):")
    for e in s["empty_searches"]:
        out(f"  {e['count']:4}  {e['query']}")
    out("\nMost read pages:")
    for m in s["most_read"]:
        out(f"  {m['count']:4}  {m['page']}")
    out(f"\nNot read in {days} days ({len(s['unread'])}): " + ", ".join(s["unread"]))
    return 0
