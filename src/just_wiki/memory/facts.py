"""The fact list: setup values the wiki says change with the setup, so memory checks know what to look for.

The presets and patterns catch values by their shape. The wiki knows more: names of units, containers and jobs, files of
this setup. This reads the verified pages (all page folders, or [memory] fact_folders), a chunk at a time, and asks the
LLM ([memory.llm] or the steward's) to list the values that would change if the setup changed. Code then checks every value:
it must stand verbatim in the page; chat commands, tool names, document names, flags, placeholders, plain words, relative
paths and anything in the deny file (memory-deny.txt: one value per line) or matching a regex in [memory] fact_exclude are
dropped; only the kinds in [memory] fact_kinds count. The result is logs/memory-facts.json:

  values   {value: {page, kind, first_seen}}   a value is active from the night after first_seen, so a bad extraction
                                               shows in the report before it can rewrite a memory
  retired  {value: {page, retired}}            no longer anywhere in the wiki text (a code check, not the model's word):
                                               a memory that still holds one is probably stale
  runs     one line per night (pages, asked, values, new, retired)

Unchanged pages are not asked again (logs/memory-facts-cache.json). Without an LLM nothing happens.
"""
import hashlib
import json
import re
from datetime import date, timedelta

from .. import store as w
from . import fix, state
from .patterns import has_pointer

CHUNK = 7000
RETIRED_KEEP_DAYS = 60
KINDS = ("port", "address", "unit", "container", "job", "script", "host_path", "model", "chat_id", "id")
DENY = [re.compile(p) for p in (
    r"^/[A-Za-z0-9_-]+$",                    # a chat command: /todo
    r"^--?[A-Za-z]",                         # a command-line flag
    r"^[A-Za-z_.-]+\.md$",                   # a document name: AGENTS.md
    r"^/proc/|^/dev/",                       # system paths
    r"[<>{}()\[\]*]|\s",                     # a placeholder, a pattern, a phrase
    r"^[a-z_]+$",                            # a plain word or tool name (submit_note, search)
)]
BAD_PATHS = ("/tmp", "/etc", "/usr", "/var", "/dev", "/proc", "/path")   # examples; more via [memory] fact_exclude
PROMPT = """You list the setup-specific values in a page of a wiki about a computer setup. These are values that would have \
to be corrected if the setup changed: names of systemd units, timers, containers and scheduled jobs, host paths, ports, \
addresses, chat ids, model names. Copy each value EXACTLY as it is written in the text, without quotes or backticks.

Do NOT list: chat commands (like /todo), tool or function names (like submit_note), names of documentation files \
(AGENTS.md), command-line flags, placeholders in <angle brackets>, scripts and commands a person or agent runs, shell \
aliases, relative paths (tools/x.py), repository, project and folder names, public web sites, ordinary words, or generic \
software names.

Answer with one JSON array and nothing else: [{{"value": "...", "kind": "<one of: {kinds}>"}}, ...]. Use [] if there are \
none.

Page {page}:
{text}"""


def deny_list():
    try:
        return {x.strip() for x in state.deny_file().read_text(encoding="utf-8").splitlines()
                if x.strip() and not x.startswith("#")}
    except OSError:
        return set()


def source_pages():
    """{page id: text} of the verified pages the list is built from."""
    folders = state.cfg().get("fact_folders") or []
    out = {}
    for pid, t in state.page_texts().items():
        if (not folders or pid.split("/")[0] in folders) and re.search(r"^status:\s*verified\s*$", t[:600], re.M):
            out[pid] = t
    return out


def chunks(text):
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > CHUNK and cur:
            out.append(cur)
            cur = ""
        cur += line
    return out + ([cur] if cur.strip() else [])


def keep(value, text, pats):
    """True when the value stands verbatim in the page text and is a setup value, not a command, word or placeholder."""
    if not (5 <= len(value) <= 90) or value not in text:
        return False
    if not re.search(r"[-.:/_0-9]", value) or any(d.search(value) for d in DENY):
        return False
    if has_pointer(value) or pats.tokens(value):
        return False                           # the patterns already catch it (or it is the wiki's own folder)
    return True


def usable(value, kind):
    """Only values that change with the setup count; scripts, commands, relative paths and project names stay in memories
    as how-to vocabulary (applied at build time, so the filter can change without asking the model again)."""
    if value in deny_list() or kind not in state.cfg()["fact_kinds"]:
        return False
    if any(re.search(p, value) for p in state.cfg()["fact_exclude"]):      # e.g. an agent's own container paths
        return False
    if kind in ("unit", "container", "job"):
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*", value)) and not value.endswith((".py", ".sh", ".md"))
    if kind == "host_path":
        return value.startswith(("/", "~/")) and value.count("/") >= 2 and not value.startswith(BAD_PATHS)
    if kind == "address":
        return bool(re.match(r"\d{1,3}(\.\d{1,3}){3}", value))
    return kind in ("model", "chat_id", "port")


def parse(answer):
    m = re.search(r"\[.*\]", answer, re.S)
    try:
        items = json.loads(m.group(0)) if m else []
    except ValueError:
        return None
    return [(str(i.get("value", "")).strip(), i.get("kind", "")) for i in items if isinstance(i, dict)]


def extract(pid, text, pats):
    """[(value, kind)] for one page, or None when the model could not be asked."""
    c = fix.llm_config()
    from ..pipeline import LLM
    llm = LLM({**c, "max_tokens": 1500})
    found = {}
    for ch in chunks(text):
        try:
            items = parse(llm.chat("You extract setup values from wiki pages exactly as written.",
                                   PROMPT.format(page=pid, text=ch, kinds=", ".join(KINDS)), kind="memory"))
        except Exception as e:  # noqa: BLE001 - model down: keep the old list
            print(f"memory facts: the LLM is unavailable ({type(e).__name__}); keeping the old list")
            return None
        for v, kind in items or []:
            if keep(v, text, pats):
                found.setdefault(v, kind if kind in KINDS else "id")
    return sorted(found.items())


def build(dry=False, out=print):
    state.reset()
    if not fix.llm_config():
        out("memory facts: no LLM configured ([memory.llm] or [steward.llm]); nothing to do")
        return None
    pages = source_pages()
    cache_f = state.logs() / "memory-facts-cache.json"
    cache = json.loads(cache_f.read_text()) if cache_f.exists() else {}
    old = state.read_facts()
    base_pats = state.patterns(include_pending=False)
    asked = 0
    for pid, text in pages.items():
        sha = hashlib.sha256(text.encode()).hexdigest()
        if cache.get(pid, {}).get("sha") == sha:
            continue
        if dry:
            out(f"would ask: {pid} ({len(chunks(text))} chunk(s))")
            continue
        vals = extract(pid, text, base_pats)
        if vals is None:
            return None
        cache[pid] = {"sha": sha, "values": vals}
        asked += 1
    if dry:
        return None
    cache = {k: v for k, v in cache.items() if k in pages}
    today = date.today().isoformat()
    now_values = {}
    for pid in sorted(cache):                 # the first page (alphabetical) that lists a value is its page
        for v, kind in cache[pid]["values"]:
            if usable(v, kind):
                now_values.setdefault(v, {"page": pid, "kind": kind})
    wiki_text = "\n".join(state.page_texts().values())
    prev, retired = old.get("values", {}), dict(old.get("retired", {}))
    values = {}
    for v, m in now_values.items():
        values[v] = {**m, "first_seen": prev.get(v, {}).get("first_seen", today)}
        retired.pop(v, None)
    for v, m in prev.items():
        if v not in values:
            if v in wiki_text and usable(v, m.get("kind", "")):     # the model missed it this time; it is still there
                values[v] = m
            elif v not in wiki_text:
                retired[v] = {"page": m.get("page"), "retired": today}
    cut = (date.today() - timedelta(days=RETIRED_KEEP_DAYS)).isoformat()
    retired = {v: m for v, m in retired.items() if m.get("retired", today) >= cut}
    new = sorted(v for v, m in values.items() if m["first_seen"] == today)
    gone = sorted(v for v, m in retired.items() if m.get("retired") == today)
    runs = [r for r in old.get("runs", []) if r.get("date") != today][-29:]
    runs.append({"date": today, "at": w.now(), "pages": len(pages), "asked": asked, "values": len(values),
                 "new": len(new), "retired": len(gone)})
    state.facts_file().write_text(json.dumps({"at": w.now(), "values": values, "retired": retired, "runs": runs}, indent=1,
                                             ensure_ascii=False), encoding="utf-8")
    cache_f.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    out(f"memory facts: {len(pages)} pages, {asked} asked, {len(values)} values ({len(new)} new, active tomorrow), "
        f"{len(gone)} retired tonight")
    return values
