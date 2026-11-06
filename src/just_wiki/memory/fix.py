"""Find setup facts in agents' memories and move them out: scan (report), fix (rewrite), restore (undo).

A unit holding a fact and no pointer is a drift. A fix rewrites it by rule first: every value becomes
"(see wiki <page that holds that value>)", nothing else in the line changes (the punctuation after a value stays). Only
when the rule's result fails the checks, and the line holds a built-in or user pattern (not just a listed value), a model
may rewrite the entry: [memory.llm] or the steward's LLM, then [memory] fallback_command. Every result is checked in
code: no fact left, a pointer present, every pointed-to page exists, not much longer, 80% of the other words kept.
Old text is backed up first (the day's first copy of each file), and `memory restore` puts it back.
"""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

from .. import config
from .. import store as w
from . import sources as src
from . import state
from .patterns import LISTED, STALE, has_pointer

PROMPT = """Rewrite one entry of an AI agent's private memory. The memory may keep only how to work and the user's \
preferences, never facts about the setup (ports, addresses, paths, times, ids, names of services and models). Those \
live in the shared wiki.

Keep every instruction and preference in the entry. Replace each setup value with a pointer to the wiki page given \
below, in the form (see wiki <page>); if several values point to the same page, one pointer is enough. Do not add \
anything else. Write the entry in its original language and style. Output only the rewritten entry, no quotes, no \
explanation, exactly as many lines as the entry has.

Wiki page for these facts: {page}
Entry:
{entry}"""
MERGE_PTR = re.compile(r"(\(see wiki[^)]*\))(?:(?:[\s,/:;&-]|\b(?:and|or|ja|ning)\b)+\1)+")
CODE_PTR = re.compile(r"`(\(see wiki[^)]*\))`")
WORD = re.compile(r"[^\W\d_]{4,}")


# ---------------------------------------------------------------- checks

def words(text, pats):
    return {x.lower() for x in WORD.findall(pats.sub(text, lambda t: " "))} - {"wiki"}


def valid(old, new, pats):
    """'' when the rewrite is usable, else why not."""
    if not new.strip() or len(new) > len(old) + 80:
        return "empty or much longer"
    if len([x for x in new.splitlines() if x.strip()]) > max(1, len([x for x in old.splitlines() if x.strip()])):
        return "more lines than the original (an explanation or a second version)"
    left = pats.facts_in(new)
    if left:
        return f"fact left: {left[0][1].text}"
    if not has_pointer(new):
        return "no wiki pointer"
    if not state.pointers_ok(new):
        return "pointer to a page that does not exist"
    keep = words(old, pats)
    if keep and len(keep & words(new, pats)) < 0.8 * len(keep):
        return "dropped words of the original (instructions lost?)"
    return ""


def rule_rewrite(entry, pats, page=None, lookup=None):
    """The entry with every value in a fact line replaced by a pointer, or None when that is not usable."""
    lookup = lookup or state.fact_page

    def ptr(t):
        pg = lookup(t.text) or page
        return f"(see wiki {pg})" if pg else "(see wiki)"

    lines = []
    for line in entry.split("\n"):
        if pats.fact(line):
            line = CODE_PTR.sub(r"\1", MERGE_PTR.sub(r"\1", pats.sub(line, ptr)))
        lines.append(line)
    new = "\n".join(lines)
    return new if new != entry and not valid(entry, new, pats) else None


def has_builtin(entry, pats):
    """True when the entry holds a value of a preset or user pattern (not only a listed value): a model may help."""
    return any(t.name != LISTED for line in entry.splitlines() if not has_pointer(line) for t in pats.tokens(line))


def llm_config():
    c = state.cfg().get("llm") or {}
    if not (c.get("base_url") and c.get("model")):
        c = (w.cfg()["steward"].get("llm") or {})
    return c if c.get("base_url") and c.get("model") else None


def ask_model(prompt, max_tokens=800):
    """The configured LLM's answer, or None; fallback_command ({prompt} in its arguments) if the LLM fails."""
    c = llm_config()
    if c:
        try:
            from ..pipeline import LLM
            return LLM({**c, "max_tokens": max_tokens}).chat("You rewrite memory entries exactly as told.", prompt, kind="memory")
        except Exception:  # noqa: BLE001 - the model is down or answered badly: try the next way
            pass
    cmd = state.cfg().get("fallback_command") or []
    if cmd:
        try:
            r = subprocess.run([config.expand(a, w.ROOT).replace("{prompt}", prompt) for a in cmd], capture_output=True,
                               text=True, timeout=300, stdin=subprocess.DEVNULL, cwd=tempfile.gettempdir())
            return r.stdout if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    return None


def rewrite(entry, pats, page):
    """(new text, who) or (None, why)."""
    new = rule_rewrite(entry, pats, page)
    if new:
        return new, "rule"
    if not has_builtin(entry, pats):
        return None, "listed value only: the rule could not rewrite it, no model for these"
    prompt = PROMPT.format(page=page or "(no page yet; write: see wiki)", entry=entry)
    answer = ask_model(prompt)
    if answer is None:
        return None, "no model configured or none answered"
    cand = answer.strip().strip('"').strip()
    n = max(1, len([x for x in entry.splitlines() if x.strip()]))
    tail = "\n".join([x for x in cand.splitlines() if x.strip()][-n:])   # a model that corrects itself: the last version
    why = ""
    for c in (cand, tail):
        why = valid(entry, c, pats)
        if not why:
            return c, "model"
    return None, f"model: {why}"


# ---------------------------------------------------------------- backups

def backup(path):
    d = state.backup_dir() / str(date.today())
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(state.backup_dir(), 0o700)
    name = f"{path.name}.{hashlib.md5(str(path).encode()).hexdigest()[:6]}"
    f = d / name
    if not f.exists():                       # the day's first (original) copy; never overwrite it
        f.write_bytes(path.read_bytes())
        idx = d / "index.json"
        data = json.loads(idx.read_text()) if idx.exists() else {}
        data[name] = str(path)
        idx.write_text(json.dumps(data, indent=1))


def prune_backups():
    base = state.backup_dir()
    if not base.is_dir():
        return
    cut = time.time() - int(state.cfg()["backup_days"]) * 86400
    for d in base.iterdir():
        try:
            if d.is_dir() and datetime.strptime(d.name[:10], "%Y-%m-%d").timestamp() < cut:
                for f in d.iterdir():
                    f.unlink()
                d.rmdir()
        except (ValueError, OSError):
            continue


def restore(day=None, name=None, yes=False, out=print):
    """List (or with yes=True put back) the backups of a day, newest-day default. The current file is kept beside them."""
    base = state.backup_dir()
    days = sorted(d.name for d in base.iterdir() if d.is_dir()) if base.is_dir() else []
    day = day or (days[-1] if days else None)
    idx = base / day / "index.json" if day else None
    if not idx or not idx.exists():
        out("no backups" + (f" for {day}" if day else ""))
        return 1
    index = json.loads(idx.read_text())
    todo = {n: p for n, p in index.items() if not name or name in n or name in p}
    for n, p in sorted(todo.items()):
        old, cur = (base / day / n).read_text(encoding="utf-8"), Path(p).read_text(encoding="utf-8") if Path(p).exists() else ""
        out(f"{'restore' if yes else 'would restore'} {p}: {len(cur)} → {len(old)} chars ({day})")
        if yes:
            (base / day / (n + ".before-restore")).write_text(cur, encoding="utf-8")
            lock = Path(p).with_suffix(Path(p).suffix + ".lock")
            with open(lock, "a") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                tmp = Path(p).with_name(".just-wiki_" + Path(p).name)
                tmp.write_text(old, encoding="utf-8")
                os.replace(tmp, p)
                fcntl.flock(lk, fcntl.LOCK_UN)
            state.log_row({"where": f"restored {Path(p).name}", "values": [], "by": "restore", "restored": p})
    if not yes and todo:
        out("add --yes to restore these (the current files are kept as <name>.before-restore in the backup folder)")
    return 0


# ---------------------------------------------------------------- scan and fix

def all_sources():
    return src.load(state.cfg())


def scan(pats=None):
    """[{where, label, line, tokens}] of the units holding a setup fact now."""
    pats = pats or state.patterns()
    hits = []
    for s in all_sources():
        for u in s.units():
            toks = [t for line in u.text.splitlines() if not has_pointer(line) for t in pats.tokens(line)]
            if toks:
                hits.append({"where": u.where, "label": u.label, "line": u.text[:200], "tokens": toks, "unit": u, "source": s})
    return hits


def stale_statuses():
    """[(where, text)]: documents holding a dated status ("connected ... 2026-09-01") that may no longer be true."""
    out = []
    for s in all_sources():
        for u in s.documents():
            for line in u.text.splitlines():
                m = STALE.search(line)
                if m and not has_pointer(line):
                    out.append((f"{u.agent} {u.label}", m.group(0)[:60]))
                    break
    return out


def lint_lines():
    """WARNING lines for just-wiki lint (one per file)."""
    seen, out = set(), []
    for h in scan():
        if h["where"] not in seen:
            seen.add(h["where"])
            out.append(f"memory drift: {h['where']} holds a setup fact ({h['tokens'][0].text!r}); "
                       "just-wiki memory fix moves it to a wiki pointer")
    out += [f"memory stale?: {where} holds a dated status ({text!r}); check it is still true or point to the wiki"
            for where, text in stale_statuses()]
    return out


def sweep(apply=False, out=print):
    """Rewrite what can be rewritten (apply=True) or show what would be. Returns (fixed, found, failed)."""
    state.reset()
    pats = state.patterns()
    c = state.cfg()
    fixed = found = failed = 0
    rows = state.rows()
    recent_fail = {(r["where"], tuple(r["values"])) for r in state.rows(1) if r.get("failed")}
    for s in all_sources():
        for u in s.units():
            hits = pats.facts_in(u.text)
            if not hits:
                continue
            found += 1
            if apply and fixed >= int(c["max_fixes"]):
                out(f"stopping after {c['max_fixes']} fixes (a safety cap); the next sweep continues")
                return fixed, found, failed
            values = [t.text for _, t in hits]
            page = state.page_for(values)
            new, who = rewrite(u.text, pats, page)
            if not apply:
                verdict = who if new else "WOULD FAIL: " + who
                out(f"{u.where}: {values} → wiki {page or '(new fact)'} [{verdict}]\n   {u.text[:160]}"
                    + (f"\n → {new[:160]}" if new else ""))
                continue
            if new is None:
                failed += 1
                out(f"{u.where}: could not rewrite ({who}); left for the report")
                if (u.where, tuple(values)) not in recent_fail:
                    state.log_row({"where": u.where, "values": values, "page": page, "failed": who})
                continue
            backup(u.path)
            if not s.replace(u, new):
                continue
            replaced = [{"value": t.text, "kind": t.kind, "page": state.fact_page(t.text) or page}
                        for line, _ in hits for t in pats.tokens(line)]
            before = next((o for o, n in zip(u.text.splitlines(), new.splitlines(), strict=False) if o != n), u.text)[:160]
            after = next((n for o, n in zip(u.text.splitlines(), new.splitlines(), strict=False) if o != n), new)[:160]
            repeat = sum(1 for r in rows if r.get("where") == u.where and r.get("values") == values and not r.get("failed"))
            if not page:
                note_new_fact(u, values)
            fixed += 1
            target = page or "(new fact, note in inbox/memory)"
            w.append_log("memory", f"fixed {u.where}: {', '.join(values)} → wiki {target} ({who})"
                         + (f"; repeat #{repeat + 1}" if repeat else ""))
            state.log_row({"where": u.where, "values": values, "page": page, "by": who, "repeat": repeat,
                           "replaced": replaced, "before": before, "after": after})
            out(f"fixed {u.where} ({who}): {values} → {page}" + (f" (repeat #{repeat + 1})" if repeat else ""))
    return fixed, found, failed


def note_new_fact(unit, values):
    """A value that no wiki page holds: leave it as a note for the steward to check and merge."""
    d = w.ROOT / "inbox/memory"
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    (d / f"{stamp}-memory-fact.md").write_text(
        f"---\ntitle: Setup fact found in an agent's memory, not in the wiki\nkind: finding\nagent: memory\n"
        f"created: {w.now()}\nsources: [{unit.path}]\n---\nThe memory sweeper moved this out of {unit.where}; no wiki page "
        f"holds its values ({', '.join(values)}). Check it and merge it if it is true (the memory now says \"see wiki\"):\n\n"
        f"> {unit.text[:300]}\n", encoding="utf-8")


def run(apply, out=print):
    """One sweep under the lock; the timer and the path unit both call this."""
    lock = state.logs() / "memory.lock"
    with open(lock, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            out("memory: another sweep is running")
            return 0
        fixed, found, failed = sweep(apply, out)
        if apply:
            prune_backups()
        out(f"memory: found {found}, fixed {fixed}, failed {failed}" if apply else f"memory: found {found} (report only)")
    return 0
