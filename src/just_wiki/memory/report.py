"""The memory report: the data of the web UI's Memory tab, a plain-text view and a one-line summary.

build() returns the panel format of web/__init__.py (sections of stats, bars, tables and cards), so the same JSON could
also be shown by a [[web.panels]] command. Sources: logs/memory.jsonl (every correction), memory-facts.json,
memory-audit.json, the memories themselves (read-only) and [memory] rules_files (to see if a suggestion is already there).
"""
import collections
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import fix, state

KIND_LABEL = {"unit": "systemd unit / timer", "job": "scheduled job", "container": "container", "host_path": "absolute host path",
              "model": "model name", "chat_id": "chat id", "port": "port", "address": "address", "url": "URL",
              "listed": "listed from the wiki", "other": "other"}


def tile(label, value, note="", tone=""):
    return {"label": label, "value": str(value), "note": note, "tone": tone}


def link(text, page):
    return {"text": text, "href": "/" + page} if page else text


def replaced_of(row):
    """[(value, kind, page)] of a correction (older rows only have the short matches)."""
    if row.get("replaced"):
        return [(x["value"], x.get("kind") or "other", x.get("page")) for x in row["replaced"]]
    return [(v, "other", row.get("page")) for v in row.get("values", [])]


def agent_of(where):
    return where.split()[0]


def candidates(rows, rules_text):
    """Values and kinds an agent keeps writing into memory: material for a standing rule in its instructions file."""
    by_value = collections.defaultdict(lambda: {"n": 0, "agents": set(), "page": None, "kind": "other"})
    by_kind = collections.defaultdict(int)
    for r in rows:
        if r.get("failed") or r.get("by") == "restore":
            continue
        for v, kind, page in replaced_of(r):
            e = by_value[v]
            e["n"] += 1
            e["agents"].add(agent_of(r["where"]))
            e["page"] = page or e["page"]
            e["kind"] = kind
            by_kind[(agent_of(r["where"]), kind)] += 1
    items = []
    for v, e in sorted(by_value.items(), key=lambda kv: -kv[1]["n"]):
        if e["n"] >= 2:
            ag = "/".join(sorted(e["agents"]))
            ptr = f" (see wiki {e['page']})" if e["page"] else ""
            items.append({"title": f"{v}: written to memory {e['n']} times ({ag})",
                          "text": f"Suggestion for {ag}'s instructions: \"Don't save {v} in memory; it is a "
                                  f"{KIND_LABEL.get(e['kind'], e['kind'])}, look it up in the wiki{ptr}.\"",
                          "tag": "already in the rules" if v in rules_text else "new"})
    for (ag, kind), n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        if n >= 3:
            items.append({"title": f"{ag} keeps saving {KIND_LABEL.get(kind, kind)}s ({n} corrections)",
                          "text": f"A general line may help: \"Never store {KIND_LABEL.get(kind, kind)}s in memory; the wiki holds them.\"",
                          "tag": "new"})
    return items[:10]


def rules_text():
    out = []
    for f in state.cfg().get("rules_files") or []:
        try:
            out.append(Path(f).expanduser().read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n".join(out)


def build():
    state.reset()
    pats = state.patterns(include_pending=False)
    rows = state.rows()
    fixes = [r for r in rows if not r.get("failed") and r.get("by") != "restore"]
    fails = {}
    for r in rows:                                   # a failure counts until a later fix of the same entry
        k = (r["where"], tuple(r["values"]))
        fails.pop(k, None) if not r.get("failed") else fails.setdefault(k, r)
    today = date.today().isoformat()
    facts = state.read_facts()
    fvals, retired = facts.get("values", {}), facts.get("retired", {})
    pending = {v: m for v, m in fvals.items() if m.get("first_seen") == today}
    audit = json.loads(state.audit_file().read_text(encoding="utf-8")) if state.audit_file().exists() else {}
    findings = audit.get("findings", [])
    docs = [d for s in fix.all_sources() for d in s.documents()]
    flagged_now = {u.where for u in (h["unit"] for h in fix.scan())}
    cfg = state.cfg()
    sections = []

    def since(days):
        cut = time.time() - days * 86400
        return [r for r in fixes if datetime.strptime(r["at"], "%Y-%m-%d %H:%M").timestamp() >= cut]

    # 1 overview
    repeats = sum(1 for r in fixes if r.get("repeat"))
    mode_note = "reports only: set mode = \"fix\" in [memory] to let it rewrite" if cfg["mode"] == "report" else "rewrites by rule, with backups"
    sections.append({"heading": "Overview", "stats": [
        tile("corrections, 7 days", len(since(7)), f"{len(since(30))} in 30 days, {len(fixes)} in all"),
        tile("holding a fact now", len(flagged_now), "memory files with an unfixed setup fact", "warn" if flagged_now else "ok"),
        tile("stuck", len(fails), "a rewrite that failed and was not fixed since", "bad" if fails else "ok"),
        tile("repeats", repeats, "the same value came back after a fix", "warn" if repeats else "ok"),
        tile("wiki-listed values", len(fvals), f"{len(pending)} new tonight"),
        tile("audit findings", len(findings), "memory vs. wiki", "warn" if findings else "ok"),
        tile("memories watched", len(docs), f"{len(cfg['sources'])} source(s)")],
        "text": f"Memory should hold how to work with you, never setup facts. Mode: {cfg['mode']} ({mode_note})."})

    # 2 patterns
    cnt = collections.Counter(v for r in fixes for v, _, _ in replaced_of(r))
    by_pat = collections.Counter()
    for r in fixes:
        for _v, kind, _page in replaced_of(r):
            by_pat[kind] += 1
    trows = []
    own = {str(p.get("name")) for p in cfg.get("patterns") or []}
    for name, kind in pats.names():
        if kind != "listed":
            trows.append([name, KIND_LABEL.get(kind, kind), "yours" if name in own else "preset", str(by_pat.get(kind, 0))])
    by_kind = collections.Counter(m.get("kind") for m in fvals.values())
    for kind, n in sorted(by_kind.items()):
        ex = next((v for v, m in sorted(fvals.items()) if m.get("kind") == kind), "")
        trows.append([f"{KIND_LABEL.get(kind, kind)} (listed from the wiki)", {"text": f"{n} values, e.g. {ex}", "code": True},
                      "wiki list", str(by_pat.get(kind, 0))])
    sections.append({"heading": "Patterns being matched",
                     "text": "A line holding one of these, without a pointer to the wiki, is a memory drift. Each value becomes "
                             "(see wiki <page>); a model only helps with the preset and own patterns when the rule can't.",
                     "table": {"columns": ["Pattern", "Kind", "Source", "Replaced"], "rows": trows}})

    # 3 nightly fact-list run
    run = (facts.get("runs") or [{}])[-1]
    sec3 = {"heading": "Fact-list run", "stats": [
        tile("last run", run.get("at", "never")[11:] or "never", f"{run.get('at', '')[:10]}: {run.get('pages', 0)} pages, {run.get('asked', 0)} asked the model"),
        tile("new tonight", len(pending), "active from tomorrow", "warn" if pending else "ok"),
        tile("retired", sum(1 for m in retired.values() if m.get("retired") == today), f"{len(retired)} in the last 60 days")],
        "text": "just-wiki memory facts: the LLM lists the setup values of the verified pages; code filters them (the deny file "
                f"{cfg['deny_file']} keeps tools and project names out). New values wait one night before they can rewrite a memory."}
    if retired:
        sec3["items"] = [{"title": f"Left the wiki: {v}", "text": f"was on {m.get('page')}, retired {m.get('retired')}; memories that "
                          "still hold it are reported as stale.", "tag": "retired"} for v, m in sorted(retired.items())[:8]]
    sections.append(sec3)
    if pending:
        sections.append({"heading": f"New values, active tomorrow ({len(pending)})", "fold": len(pending) > 12, "table": {
            "columns": ["Value", "Kind", "Wiki page"],
            "rows": [[{"text": v, "code": True}, KIND_LABEL.get(m.get("kind"), m.get("kind", "")), link(m.get("page") or "", m.get("page"))]
                     for v, m in sorted(pending.items())[:80]]}})
    if fvals:
        cnts = {v: cnt.get(v, 0) for v in fvals}
        sections.append({"heading": f"All listed values ({len(fvals)})", "fold": True, "table": {
            "columns": ["Value", "Kind", "Wiki page", "First seen", "State", "Replaced"],
            "rows": [[v, KIND_LABEL.get(m.get("kind"), m.get("kind", "")), link(m.get("page") or "", m.get("page")), m.get("first_seen", ""),
                      {"text": "pending" if m.get("first_seen") == today else "active", "tag": "warn" if m.get("first_seen") == today else "ok"},
                      str(cnts.get(v, 0))] for v, m in sorted(fvals.items(), key=lambda kv: (kv[1].get("kind", ""), kv[0]))]}})

    # 4 corrections
    days = [(date.today() - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    per_day = collections.Counter(r["at"][:10] for r in fixes)
    by_method = collections.Counter(r.get("by", "?") for r in fixes)
    by_agent = collections.defaultdict(lambda: [0, 0, 0])
    for r in fixes:
        e = by_agent[r["where"]]
        e[0] += 1
        e[1] += 1 if r.get("repeat") else 0
    for (where, _v) in fails:
        by_agent[where][2] += 1
    sections.append({
        "heading": "Corrections", "bars_title": "Per day, last 14 days", "bars_style": "columns", "bars": [{"label": d[5:], "value": per_day[d]} for d in days],
        "text": "Method: " + (", ".join(f"{k} {n}" for k, n in by_method.most_common()) or "none yet") +
                ". 'rule' is a plain value-for-pointer swap; 'model' is an LLM rewrite (preset and own patterns only).",
        "table": {"columns": ["Agent / memory", "Corrections", "Repeats", "Stuck"],
                  "rows": [[k, str(v[0]), str(v[1]), str(v[2])] for k, v in sorted(by_agent.items(), key=lambda kv: -kv[1][0])]}})
    top = collections.defaultdict(lambda: {"n": 0, "agents": set(), "last": "", "page": None, "kind": "other"})
    for r in fixes:
        for v, kind, page in replaced_of(r):
            e = top[v]
            e["n"] += 1
            e["agents"].add(agent_of(r["where"]))
            e["last"] = max(e["last"], r["at"])
            e["page"] = page or e["page"]
            e["kind"] = kind
    sections.append({"heading": "Top replaced values", **({"table": {
        "columns": ["Value", "Kind", "Agent", "Times", "Last", "Now points to"],
        "rows": [[{"text": v, "code": True}, KIND_LABEL.get(e["kind"], e["kind"]), "/".join(sorted(e["agents"])), str(e["n"]), e["last"],
                  link(e["page"] or "", e["page"])] for v, e in sorted(top.items(), key=lambda kv: (-kv[1]["n"], kv[0]))[:15]]}}
        if top else {"text": "No corrections yet."})})

    # 5 candidates for standing rules
    cand = candidates(fixes, rules_text())
    sections.append({"heading": "Candidates for standing rules",
                     "text": "Values an agent writes into memory again and again are better stopped at the source: a line in its "
                             "instructions file (Hermes' SOUL.md, a CLAUDE.md, a memory rule). Shown when a value was corrected twice "
                             "or a kind three times. You decide; nothing is written automatically." + ("" if cand else " None yet."),
                     "items": cand})

    # 6 audit
    sections.append({"heading": "Memory audit (memory vs. wiki)",
                     "text": f"Last run {audit.get('at', 'never')}; {audit.get('asked', 0)} entries asked the model (unchanged ones are "
                             "cached). The model only reports; nothing is rewritten." + ("" if findings else " No findings."),
                     "items": [{"title": f["where"], "text": f["reason"], "tag": f["verdict"]} for f in findings]})

    # 7 inventory
    fix_by_where = collections.Counter(r["where"] for r in fixes)
    irows, pointed = [], collections.Counter()
    for d in docs:
        ptrs = sorted(set(re.findall(r"\(see wiki ([a-z]+/[a-z0-9-]+)\)", d.text)) | set(re.findall(r"wiki `?([a-z]+/[a-z0-9-]+)", d.text)))
        ptrs = [p for p in ptrs if p in state.page_texts()]
        pointed.update(ptrs)
        flagged = d.where in flagged_now
        irows.append([d.agent, d.label, str(len(d.text)), datetime.fromtimestamp(d.path.stat().st_mtime).strftime("%d.%m %H:%M"),
                      {"text": "holds a fact now" if flagged else "clean", "tag": "bad" if flagged else "ok"},
                      str(fix_by_where.get(d.where, 0)), [link(p, p) for p in ptrs] or ""])
    sections.append({"heading": "Memories and their wiki links",
                     "text": "Every memory file or entry the sources list. 'Wiki links' are the pointers a memory now holds to wiki pages.",
                     "table": {"columns": ["Agent", "Memory", "Chars", "Changed", "State", "Fixed", "Wiki links"], "rows": irows}})
    if pointed:
        sections.append({"heading": "Most linked wiki pages", "bars": [{"label": p, "value": n} for p, n in pointed.most_common(8)]})

    # 8 timeline
    tl = []
    for r in reversed(rows[-25:]):
        if r.get("failed"):
            tl.append([r["at"], r["where"], {"text": "failed", "tag": "bad"}, r["failed"][:80], ""])
        elif r.get("by") == "restore":
            tl.append([r["at"], r["where"], {"text": "restore", "tag": "warn"}, "", ""])
        else:
            vals = ", ".join(v for v, _, _ in replaced_of(r))[:60]
            tl.append([r["at"], r["where"], {"text": r.get("by", "?"), "tag": "ok"}, {"text": vals, "code": True},
                       link(r.get("page") or "", r.get("page")) if not r.get("repeat") else {"text": f"repeat #{r['repeat'] + 1}", "tag": "warn"}])
    sections.append({"heading": "Recent events", "fold": True, "table": {
        "columns": ["When", "Memory", "Method", "Replaced", "Now points to"], "rows": tl}} if tl
        else {"heading": "Recent events", "text": "Nothing yet."})
    return {"title": "Agent memory", "updated": time.strftime("%Y-%m-%d %H:%M"), "sections": sections}


def text(p=None):
    p = p or build()
    out = [f"{p['title']} ({p['updated']})"]
    for s in p["sections"]:
        out.append(f"\n## {s['heading']}")
        out += [f"  {t['label']}: {t['value']} ({t['note']})" for t in s.get("stats", [])]
        if s.get("table"):
            for row in s["table"]["rows"][:8]:
                out.append("  " + " | ".join(c if isinstance(c, str) else (c.get("text", "") if isinstance(c, dict) else "") for c in row))
        out += [f"  - {i['title']}: {i['text']}" for i in s.get("items", [])]
    return "\n".join(out)


def line():
    """One line for a morning report: the last 24 h of corrections, failures, repeats, plus audit findings and new values."""
    rows = state.rows(1)
    done = [r for r in rows if not r.get("failed") and r.get("by") != "restore"]
    stuck = {}
    for r in rows:
        k = (r["where"], tuple(r["values"]))
        stuck.pop(k, None) if not r.get("failed") else stuck.setdefault(k, True)
    parts = []
    if done or stuck:
        by = collections.Counter(r.get("by") for r in done)
        parts.append(f"memory corrections: {len(done)} (rule {by['rule']}, model {by['model']})")
        if stuck:
            parts.append(f"stuck: {len(stuck)} ({', '.join(sorted({k[0] for k in stuck}))})")
        again = sorted({r["where"] for r in done if r.get("repeat")})
        if again:
            parts.append(f"repeats: {', '.join(again)}")
    today = date.today().isoformat()
    facts = state.read_facts()
    new = sorted(v for v, m in facts.get("values", {}).items() if m.get("first_seen") == today)
    gone = sorted(v for v, m in facts.get("retired", {}).items() if m.get("retired") == today)
    if new:
        parts.append(f"{len(new)} new listed values (active tomorrow): {', '.join(new[:5])}" + (" …" if len(new) > 5 else ""))
    if gone:
        parts.append(f"left the wiki: {', '.join(gone[:5])}")
    f = state.audit_file()
    if f.exists():
        fnd = json.loads(f.read_text(encoding="utf-8")).get("findings", [])
        if fnd:
            parts.append(f"memory audit: {len(fnd)} finding(s): " + ", ".join(f"{x['where']} ({x['verdict']})" for x in fnd[:3]))
    return "; ".join(parts)
