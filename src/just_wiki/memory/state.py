"""Shared state of the memory feature: settings, the event log, the fact list on disk, the wiki's pages."""
import json
import os
import time
from datetime import date, datetime
from pathlib import Path

from .. import store as w
from .patterns import Patterns

_pages = {}


def cfg():
    return w.cfg()["memory"]


def enabled():
    return bool(cfg().get("enabled"))


def logs():
    d = w.ROOT / "logs"
    d.mkdir(exist_ok=True)
    return d


def log_file():
    return logs() / "memory.jsonl"


def facts_file():
    return logs() / "memory-facts.json"


def audit_file():
    return logs() / "memory-audit.json"


def deny_file():
    return w.ROOT / cfg()["deny_file"]


def backup_dir():
    p = Path(os.path.expanduser(str(cfg()["backup_dir"])))
    return p if p.is_absolute() else w.ROOT / p


def reset():
    _pages.clear()


def page_texts():
    """{page id: text} of every page (read once per run)."""
    if not _pages:
        _pages.update({w.page_id(p): p.read_text(encoding="utf-8") for p in w.pages()})
    return _pages


def page_for(values):
    """The page that holds most of the values (substring search), or None."""
    hits = {}
    for v in values:
        for pid, t in page_texts().items():
            if v in t:
                hits[pid] = hits.get(pid, 0) + 1
    return max(hits, key=lambda k: (hits[k], k)) if hits else None


def pointers_ok(text):
    """False when a "(see wiki <folder>/<page>)" or [[page]] names a page that does not exist."""
    import re
    for m in re.finditer(r"\(see wiki ([a-z]+/[a-z0-9-]+)\)|\[\[([^\]|#]+)", text):
        if (m.group(1) or m.group(2)) not in page_texts():
            return False
    return True


# ---- the fact list written by facts.py

def read_facts():
    f = facts_file()
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except (OSError, ValueError):
        return {}


def active_facts(include_pending=None):
    """{value: page} of the listed values that are active: from the night after they were first listed."""
    pending = bool(os.environ.get("JUST_WIKI_MEMORY_PENDING")) if include_pending is None else include_pending
    today = date.today().isoformat()
    return {v: m.get("page") for v, m in read_facts().get("values", {}).items()
            if pending or m.get("first_seen", today) < today}


def retired_facts():
    return {v: m.get("page") for v, m in read_facts().get("retired", {}).items()}


def patterns(include_pending=None):
    facts = read_facts().get("values", {})
    active = active_facts(include_pending)
    return Patterns(cfg(), active, {v: facts[v].get("kind") for v in active if v in facts})


def fact_page(value):
    return active_facts().get(value) or page_for([value])


# ---- the event log

def log_row(row):
    with log_file().open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": w.now(), **row}, ensure_ascii=False) + "\n")


def rows(since_days=None):
    f = log_file()
    if not f.exists():
        return []
    cut = time.time() - since_days * 86400 if since_days else 0
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            if datetime.strptime(r["at"], "%Y-%m-%d %H:%M").timestamp() >= cut:
                out.append(r)
        except (ValueError, KeyError):
            continue
    return out
