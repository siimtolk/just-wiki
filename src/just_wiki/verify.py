"""Cheap, deterministic checks of a note's claims against the host, and the list of pages that still need checking.

B — check(): what code can test without a model, read-only and fast:
  paths     every absolute path a note cites (in `sources` or the text) exists
  units     every systemd unit it names (x.service, .timer, .socket, .path) exists (user or system)
  commands  [steward.verify] commands: each gets the note's path, prints `OK …` / `FAIL …` lines (as lint checks do)
A note passes when at least one check ran and none failed. A page the pipeline changes stays `verified` only if every
note merged into it passed; otherwise it becomes `draft` and goes on the unchecked list.

C — the unchecked list (logs/unchecked.json): pages changed from notes nobody checked. The agent steward re-checks
them (scope `unchecked`): once a day, and as soon as one has waited `max_wait_min` (`just-wiki steward --due`).
An entry leaves the list when the agent logged `verify <page>` after it was listed, or the page changed after it was
listed and is `verified` again. An entry the agent tried but could not settle waits RETRY_HOURS before it is due
again, so `--due` does not restart the agent on it every few minutes. Every change to the list is locked.

  [steward.verify]
  enabled = true            # false: no checks, no list (e.g. a replay of another wiki's history)
  checks = ["paths", "units"]
  commands = []
  max_wait_min = 60         # an unchecked page this old makes the agent check run now (just-wiki steward --due)
  per_run = 10              # pages the agent re-checks per run
"""
import fcntl
import hashlib
import json
import re
import subprocess
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config
from . import store as w

PATH = re.compile(r"(?<![\w.:/-])(/(?:[\w.@+-]+/)*[\w.@+-]+/?)")
UNIT = re.compile(r"\b([\w@.-]+\.(?:service|timer|socket|path))\b")
DEFAULTS = {"enabled": True, "checks": ["paths", "units"], "commands": [], "max_wait_min": 60, "per_run": 10}
RETRY_HOURS = 24
SKIP_PATHS = ("/v1/", "/mcp", "/api/", "/health", "/status", "/send", "/chat/")      # URL paths, not files


def settings():
    return {**DEFAULTS, **(w.cfg()["steward"].get("verify") or {})}


def _unit_exists(unit):
    for scope in (["--user"], []):
        try:
            if subprocess.run(["systemctl", *scope, "cat", unit], capture_output=True, timeout=5).returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            return None                      # no systemd here: not checkable
    return False


def check(note_path, meta, body):
    """[{kind, target, ok (True/False/None = not checkable), detail}] for one note."""
    s, out = settings(), []
    if "paths" in s["checks"]:
        cited = [str(x).split(" ")[0].split("#")[0].rstrip(":,.") for x in (meta.get("sources") or [])]
        found = [m.group(1).rstrip(".,:)") for m in PATH.finditer(body)]
        for p in dict.fromkeys(cited + found):
            if not (p.startswith("/") or p.startswith("~/")) or any(k in p for k in SKIP_PATHS) or len(p) < 4:
                continue
            exists = Path(p).expanduser().exists()
            out.append({"kind": "path", "target": p, "ok": exists, "detail": "" if exists else "does not exist"})
    if "units" in s["checks"]:
        for u in dict.fromkeys(UNIT.findall(body)):
            ok = _unit_exists(u)
            if ok is not None:
                out.append({"kind": "unit", "target": u, "ok": ok, "detail": "" if ok else "no such unit"})
    for cmd in s["commands"]:
        try:
            r = subprocess.run([config.expand(a, w.ROOT) for a in cmd] + [str(note_path)], capture_output=True,
                               text=True, timeout=60, cwd=w.ROOT)
        except (OSError, subprocess.TimeoutExpired) as e:
            out.append({"kind": "command", "target": cmd[0], "ok": None, "detail": str(e)})
            continue
        for line in r.stdout.splitlines():
            if line.startswith(("OK ", "FAIL ")):
                out.append({"kind": "command", "target": cmd[0], "ok": line.startswith("OK "), "detail": line[:200]})
    return out


def passed(results):
    return bool(results) and all(r["ok"] for r in results if r["ok"] is not None) and any(r["ok"] for r in results)


def summary(results):
    if not results:
        return "nothing checkable"
    bad = [r for r in results if r["ok"] is False]
    good = sum(1 for r in results if r["ok"])
    return f"{good} checks ok" + (", failed: " + "; ".join(f"{r['target']} {r['detail']}" for r in bad[:5]) if bad else "")


# ---------------------------------------------------------------- the unchecked list (C)

def _file():
    return w.ROOT / "logs" / "unchecked.json"


@contextmanager
def locked():
    """One writer of the unchecked list at a time (pipeline, agent runs, the web UI's reconcile)."""
    (w.ROOT / "logs").mkdir(exist_ok=True)
    with open(w.ROOT / "logs" / "unchecked.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load():
    try:
        return json.loads(_file().read_text())
    except (OSError, ValueError):
        return {}


def save(entries):
    _file().parent.mkdir(exist_ok=True)
    tmp = _file().with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(_file())


def page_hash(pid):
    path = w.resolve(pid)
    return hashlib.sha1(path.read_bytes()).hexdigest()[:12] if path else ""


def add(page_id, note_ids, why):
    with locked():
        entries = load()
        e = entries.setdefault(page_id, {"since": w.now(), "notes": [], "why": []})
        e["notes"] = sorted(set(e["notes"]) | set(note_ids))
        e["why"] = (e["why"] + [why])[-5:]
        e["hash"] = page_hash(page_id)            # the page as it was when listed (after the pipeline's write)
        e.pop("tried", None)                      # new reasons: due again
        save(entries)


def mark_tried(page_ids):
    """The agent check ran on these and left them listed: wait RETRY_HOURS before they are due again."""
    with locked():
        entries = load()
        for pid in page_ids:
            if pid in entries:
                entries[pid]["tried"] = w.now()
        save(entries)


def _verified_by_log(pid, since):
    if not w.LOG.exists():
        return False
    for line in w.LOG.read_text(encoding="utf-8").splitlines()[::-1]:
        m = re.match(r"^- \[([\d-]+ [\d:]+)\] steward \| verify (\S+?):?\s", line)
        if m and m.group(2) == pid:
            return m.group(1) >= since[:16]
    return False


def reconcile():
    """Drop entries that are settled (the agent logged `verify <page>` after the entry, or the page changed since it
    was listed and is verified) or whose page is gone. Returns what is left."""
    with locked():
        entries, keep = load(), {}
        for pid, e in entries.items():
            path = w.resolve(pid)
            if not path:
                continue
            meta = w.split(path.read_text(encoding="utf-8"))[0]
            changed = page_hash(pid) != e.get("hash")
            if _verified_by_log(pid, e["since"]) or (changed and meta.get("status") == "verified"):
                continue
            keep[pid] = e
        if keep != entries:
            save(keep)
        return keep


def _age(stamp, now):
    return (now - datetime.strptime(stamp[:16], "%Y-%m-%d %H:%M")).total_seconds()


def due():
    """Entries waiting at least max_wait_min, except those the agent tried in the last RETRY_HOURS."""
    limit = float(settings()["max_wait_min"]) * 60
    now = datetime.strptime(w.now(), "%Y-%m-%d %H:%M")
    return {pid: e for pid, e in reconcile().items()
            if _age(e["since"], now) >= limit and (not e.get("tried") or _age(e["tried"], now) >= RETRY_HOURS * 3600)}


def backlog_warnings():
    """For lint: a list growing faster than the agent clears it."""
    if not settings()["enabled"]:
        return []
    entries = load()
    if not entries:
        return []
    now = datetime.strptime(w.now(), "%Y-%m-%d %H:%M")
    oldest = max(_age(e["since"], now) for e in entries.values()) / 86400
    out = []
    if len(entries) > 3 * int(settings()["per_run"]):
        out.append(f"{len(entries)} unchecked pages (more than 3 agent runs' worth): just-wiki unchecked")
    if oldest > 7:
        out.append(f"an unchecked page has waited {oldest:.0f} days: just-wiki unchecked")
    return out
