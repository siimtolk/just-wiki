"""Where the wiki is and how it is set up: `wiki.toml` at the wiki root.

The root is, in order: an explicit path (--root), $JUST_WIKI_ROOT, or the nearest folder at or above the current
directory that holds a `wiki.toml`. Every setting has a default, so an empty wiki.toml is a valid wiki.
"""
import os
import tomllib
from pathlib import Path

CONFIG_NAME = "wiki.toml"

# Page types: folder, frontmatter type, and one line on what belongs there (the router and the steward see it).
# SETUP_PAGES is the default, so a wiki made before page types were a setting keeps its folders; `init` writes
# GENERIC_PAGES into a new wiki's wiki.toml, for any user, subject or chat.
SETUP_PAGES = [
    {"folder": "systems", "type": "system", "about": "running things: services, hosts, models, network"},
    {"folder": "projects", "type": "project", "about": "things being built, with their parts and state"},
    {"folder": "decisions", "type": "decision", "about": "a decision someone made: context, who and when, alternatives"},
    {"folder": "runbooks", "type": "runbook", "about": "how to do a recurring task, step by step"},
    {"folder": "results", "type": "result", "about": "test results, measurements, reports"},
]
GENERIC_PAGES = [
    {"folder": "people", "type": "person", "about": "one person: who they are, background, relationships, situation"},
    {"folder": "preferences", "type": "preference",
     "about": "how someone wants things: tastes, standing instructions, how to answer or help them"},
    {"folder": "topics", "type": "topic",
     "about": "one subject, thing or piece of work (a plan, a product, a book series, a service): details, current state"},
    {"folder": "decisions", "type": "decision", "about": "a decision someone made: context, who and when, alternatives"},
    {"folder": "howtos", "type": "howto", "about": "how to do a recurring task, step by step"},
    {"folder": "results", "type": "result", "about": "outputs worth keeping: test results, measurements, reports"},
]

DEFAULTS = {
    "wiki": {"name": "JustWiki", "owner": "", "agent": "claude-code"},
    "mcp": {"host": "127.0.0.1", "port": 8093, "instructions": "", "outline_over": 0},
    "web": {"host": "127.0.0.1", "port": 8094, "user": "wiki", "title": "", "password_file": ".web_password",
            "on_decision": [], "explain_command": [], "panels": []},
    "tickets": {"github_repo": ""},
    "live": {"blocks": {}, "timeout": 120},
    "brief": {"max_chars": 9000, "head": "", "sections": ["howto"], "targets": []},
    "lint": {"stale_days": 60, "checks": []},
    "steward": {"command": [], "pending_commands": [], "after": [], "rules": "STEWARD.md", "timeout_min": 45,
                "mode": "agent", "rest": "agent", "llm": {}, "verify": {}},
    "nightly": {"report_command": [], "push": True, "remote": "origin", "branch": "main"},
    "usage": {"names": {}},
    "schedule": {"command": []},
    "pages": {"types": SETUP_PAGES},
    # Optional: keep agents' own memory files pointing at the wiki (see just_wiki/memory/__init__.py)
    "memory": {"enabled": False, "mode": "report", "nightly": True, "sources": [], "presets": ["network"], "path_prefixes": [],
               "model_names": [], "patterns": [], "max_fixes": 5, "backup_dir": "logs/memory-backups",
               "backup_days": 90, "interval_min": 15, "facts": True, "fact_folders": [],
               "fact_kinds": ["unit", "container", "job", "host_path", "model", "chat_id", "port", "address"],
               "deny_file": "memory-deny.txt", "fact_exclude": [], "audit": True, "rules_files": [], "llm": {},
               "fallback_command": []},
}


def find_root(explicit=None):
    """The wiki root as a Path, or None when none is found."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("JUST_WIKI_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / CONFIG_NAME).is_file():
            return d
    return None


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load(root):
    """The settings of the wiki at root: wiki.toml over DEFAULTS. A missing file gives the defaults."""
    path = Path(root) / CONFIG_NAME
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return _merge(DEFAULTS, data)


def expand(arg, root):
    """{root} in a configured command becomes the wiki root; ~ is expanded."""
    return os.path.expanduser(str(arg).replace("{root}", str(root)))
