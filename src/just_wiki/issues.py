"""Work tickets as GitHub issues ([tickets] github_repo in wiki.toml), read-only, through the `gh` CLI.

A label `state:<x>` gives a ticket's state; other `key:value` labels are shown as they are.
"""
import json
import re
import subprocess
import time

from . import store as w

_cache = {"at": 0.0, "rows": [], "repo": None}


def repo():
    return w.cfg()["tickets"]["github_repo"]


def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300] or f"gh exited {r.returncode}")
    return r.stdout


def label_values(names):
    out = {}
    for n in names:
        k, sep, v = n.partition(":")
        if sep:
            out.setdefault(k, v)
    return out


def open_issues(max_age=60):
    """Open issues, newest first: [{number, title, url, state, labels{key: value}}], cached for max_age seconds."""
    if not repo():
        return []
    if time.time() - _cache["at"] > max_age or _cache["repo"] != repo():
        rows = json.loads(gh("issue", "list", "--repo", repo(), "--limit", "200", "--json",
                             "number,title,labels,url,updatedAt") or "[]")
        for i in rows:
            i["labels"] = label_values(lbl["name"] for lbl in i["labels"])
            i["state"] = i["labels"].get("state", "open")
        _cache.update(at=time.time(), rows=rows, repo=repo())
    return _cache["rows"]


def view(number):
    out = gh("issue", "view", str(int(number)), "--repo", repo(), "--comments")
    return re.sub(r"<!-- chat: \S+ -->", "", out)
