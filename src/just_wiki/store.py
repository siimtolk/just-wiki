"""The wiki on disk: page parsing, links, secrets, the log. Stdlib + PyYAML.

Module globals (ROOT, INBOX, LOG) point at the current wiki; set_root() moves them. Code that wants another wiki
(tests, a second wiki) calls set_root() first.
"""
import fcntl
import re
from datetime import date, datetime
from pathlib import Path

import yaml

from . import config

TOP_PAGES = ("status",)                      # curated pages outside the page dirs
# The page folders, their types and what belongs in each: [pages] types in wiki.toml, set by set_root().
PAGE_DIRS = tuple(t["folder"] for t in config.SETUP_PAGES)
TYPES = {t["folder"]: t["type"] for t in config.SETUP_PAGES}
ABOUT = {t["folder"]: t.get("about", "") for t in config.SETUP_PAGES}
STATUSES = ("draft", "verified", "stale")
NOTE_KINDS = ("finding", "result", "decision", "question", "correction")
HIDDEN = ("tools", ".git", ".claude", "logs", "inbox", "raw")   # never served as pages

ROOT = INBOX = LOG = None
_cfg = {"root": None, "data": None}

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
SECRET = re.compile(
    r"(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_|xox[bp]-|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY|hf_[A-Za-z0-9]{30,}|refresh_token[\"' ]*[:=]"
    r"|(api[_-]?key|token|password|secret)[\"' ]*[:=] *[\"']?[A-Za-z0-9_\-]{16,})",
    re.IGNORECASE)


def set_root(path):
    """Point the module at the wiki in path."""
    global ROOT, INBOX, LOG
    ROOT = Path(path).resolve()
    INBOX = ROOT / "inbox"
    LOG = ROOT / "log.md"
    _cfg.update(root=None, data=None)
    _set_page_types()
    return ROOT


_types_mtime = [None]


def _types_changed():
    """True once after wiki.toml changed (a page folder added or removed in the web UI): long-running servers pick it up."""
    try:
        m = (ROOT / "wiki.toml").stat().st_mtime
    except OSError:
        m = None
    changed = m != _types_mtime[0]
    _types_mtime[0] = m
    return changed


def _set_page_types():
    global PAGE_DIRS, TYPES, ABOUT
    _types_changed()
    # read directly, so the settings cache stays lazy (it fills on the first cfg() call, after set_root)
    types = [t for t in config.load(ROOT)["pages"]["types"] if t.get("folder") and t.get("type")]
    PAGE_DIRS = tuple(t["folder"] for t in types)
    TYPES = {t["folder"]: t["type"] for t in types}
    ABOUT = {t["folder"]: t.get("about", "") for t in types}


def folders_text():
    """'people = one person: …; topics = …' for prompts."""
    return "; ".join(f"{d} = {ABOUT[d]}" if ABOUT.get(d) else d for d in PAGE_DIRS)


def require_root():
    if ROOT is None:
        raise SystemExit("just-wiki: no wiki found. Run inside a wiki (a folder with wiki.toml), "
                         "set JUST_WIKI_ROOT, or pass --root.")
    return ROOT


def cfg():
    """Settings of the current wiki (wiki.toml over defaults), re-read when the root changes."""
    if _cfg["root"] != ROOT:
        _cfg.update(root=ROOT, data=config.load(require_root()))
    return _cfg["data"]


def split(text):
    """(frontmatter dict, body). A file without frontmatter gives ({}, text)."""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            try:
                meta = yaml.safe_load(text[4:end]) or {}
            except yaml.YAMLError:
                meta = {"_error": "frontmatter is not valid YAML"}
            if not isinstance(meta, dict):
                meta = {"_error": "frontmatter is not a mapping"}
            return meta, text[end + 4:].lstrip("\n")
    return {}, text


def dump(meta, body):
    head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=None)
    return f"---\n{head}---\n\n{body.rstrip()}\n"


def page_id(path):
    """systems/hermes for ROOT/systems/hermes.md."""
    return Path(path).relative_to(ROOT).with_suffix("").as_posix()


def pages():
    """Curated pages: every .md in the page dirs, plus status.md."""
    if _types_changed():
        _cfg.update(root=None, data=None)
        _set_page_types()
    out = [ROOT / f"{p}.md" for p in TOP_PAGES if (ROOT / f"{p}.md").exists()]
    for d in PAGE_DIRS:
        out += sorted((ROOT / d).rglob("*.md"))
    return out


def inbox_notes():
    """Notes waiting for the steward (not yet in inbox/_processed)."""
    return sorted(p for p in INBOX.rglob("*.md") if "_processed" not in p.parts) if INBOX.is_dir() else []


def links(body):
    return [m.group(1).strip().removesuffix(".md") for m in WIKILINK.finditer(body)]


def resolve(ref):
    """A page id, path or file name → absolute path inside ROOT, or None. Inbox notes resolve too (search hits)."""
    ref = ref.strip().lstrip("/").removesuffix(".md")
    path = (ROOT / f"{ref}.md").resolve()
    if ROOT not in path.parents or not path.is_file():
        return None
    if path.relative_to(ROOT).parts[0] in ("tools", ".git", ".claude"):
        return None
    return path


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def now():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def append_log(op, text):
    """One line per event: `- [YYYY-MM-DD HH:MM] op | text`. Locked, so agents can't interleave."""
    line = f"- [{now()}] {op} | {' '.join(text.split())}\n"
    with open(LOG, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)
        fcntl.flock(f, fcntl.LOCK_UN)


_found = config.find_root()
if _found:
    set_root(_found)
