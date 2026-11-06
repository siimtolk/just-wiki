"""Claude Code PostToolUse hook on Read: log wiki page reads to logs/usage.jsonl, so file reads count like MCP reads.

Hook command: `just-wiki hook-read` (reads the hook JSON on stdin). Never fails the session.
Override the log path with JUST_WIKI_USAGE_LOG.
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import config
from . import store as w

TOP = ("index", "status")


def page_id(file_path, root):
    """folder/name (or index/status) when file_path is a page of the wiki at root, else None."""
    p = Path(file_path).resolve()
    if p.suffix != ".md":
        return None
    try:
        rel = p.relative_to(root).with_suffix("")
    except ValueError:
        return None
    if len(rel.parts) == 1 and rel.parts[0] in TOP:
        return rel.parts[0]
    if len(rel.parts) == 2 and rel.parts[0] in w.PAGE_DIRS:
        return rel.as_posix()
    return None


def log_read(file_path, root):
    page = page_id(file_path, root)
    if not page:
        return False
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M"), "ip": "claude-code-file",
           "tool": "read_page", "outcome": "ok", "page": page}
    log = Path(os.environ.get("JUST_WIKI_USAGE_LOG") or Path(root) / "logs" / "usage.jsonl")
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True


def hook():
    try:
        data = json.load(sys.stdin)
        path = data["tool_input"]["file_path"]
        root = w.ROOT
        for d in (Path(path).resolve().parent, *Path(path).resolve().parents):
            if (d / config.CONFIG_NAME).is_file():
                root = d
                break
        if root:
            log_read(path, Path(root).resolve())
    except Exception:
        pass
    return 0
