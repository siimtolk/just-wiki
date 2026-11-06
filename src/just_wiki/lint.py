"""Deterministic wiki health check. No LLM.

  just-wiki lint          report only (exit 1 on errors)
  just-wiki lint --fix    also mark old verified pages stale, rebuild index.md, log a summary
  just-wiki lint --stale  list stale pages, oldest first (the steward re-checks these)

Errors: bad or missing frontmatter, type not matching its folder, verified page without sources, broken [[links]],
secrets anywhere, local source paths that no longer exist, a hand-edited brief.
Warnings: orphan pages, duplicate titles, stale pages, inbox notes waiting, malformed notes, old generated blocks.
Extra checks: each command in [lint] checks prints lines starting with ERROR or WARNING; they are added as they are.
"""
import hashlib
import json
import os
import re
import subprocess
from datetime import date
from pathlib import Path

from . import config
from . import store as w

REQUIRED = ("title", "type", "status", "summary", "updated")
LOCAL_PATH = re.compile(r"^(/|~/)")


def hidden_in_sandbox(s):
    """True when s is a host path a sandbox hides ($JUST_WIKI_SANDBOX_HIDDEN, colon-separated), so its absence
    proves nothing there."""
    def env(name):
        return os.environ.get(f"JUST_WIKI_{name}")
    if not env("SANDBOX"):
        return False
    hidden = env("SANDBOX_HIDDEN") or ""
    path = "/home/" + s[2:] if s.startswith("~/") else s
    return any(path == h or path.startswith(h.rstrip("/") + "/") for h in hidden.split(":") if h)


def norm(title):
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def memory_checks(warnings):
    """Optional ([memory] enabled): agents' memory files that hold a setup fact the wiki should hold."""
    if not w.cfg()["memory"].get("enabled"):
        return
    try:
        from .memory import fix
        warnings += fix.lint_lines()
    except Exception as e:  # noqa: BLE001 - a memory problem must not break the wiki's lint
        warnings.append(f"memory check failed: {e!r}")


def extra_checks(errors, warnings):
    for cmd in w.cfg()["lint"]["checks"]:
        argv = [config.expand(a, w.ROOT) for a in cmd]
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=300, cwd=w.ROOT).stdout
        except (OSError, subprocess.TimeoutExpired) as err:
            warnings.append(f"lint check {argv[0]} did not run: {err}")
            continue
        for line in out.splitlines():
            if line.startswith("ERROR"):
                errors.append(line[5:].strip())
            elif line.startswith("WARNING"):
                warnings.append(line[7:].strip())


def check(fix):
    errors, warnings = [], []
    stale_days = int(w.cfg()["lint"]["stale_days"])
    ids = {w.page_id(p) for p in w.pages()}
    inbound = {i: 0 for i in ids}
    titles = {}
    today = date.today()

    for path in w.pages():
        pid = w.page_id(path)
        text = path.read_text(encoding="utf-8")
        meta, body = w.split(text)
        if "_error" in meta:
            errors.append(f"{pid}: {meta['_error']}")
            continue
        missing = [k for k in REQUIRED if not meta.get(k)]
        if missing:
            errors.append(f"{pid}: missing frontmatter {', '.join(missing)}")
        folder = path.relative_to(w.ROOT).parts[0]
        if folder in w.TYPES and meta.get("type") != w.TYPES[folder]:
            errors.append(f"{pid}: type '{meta.get('type')}' but folder {folder}/ wants '{w.TYPES[folder]}'")
        status = meta.get("status")
        if status not in w.STATUSES:
            errors.append(f"{pid}: status '{status}' not one of {', '.join(w.STATUSES)}")
        sources = meta.get("sources") or []
        if status == "verified" and not sources:
            errors.append(f"{pid}: verified but has no sources")
        for src in sources:
            s = str(src).split(" ")[0].split("#")[0]
            if LOCAL_PATH.match(s) and not Path(s).expanduser().exists():
                if hidden_in_sandbox(s):
                    warnings.append(f"{pid}: source not checkable in sandbox: {s}")
                    continue
                errors.append(f"{pid}: source path no longer exists: {s}")
        updated = w.as_date(meta.get("updated"))
        if meta.get("updated") and not updated:
            errors.append(f"{pid}: updated '{meta.get('updated')}' is not YYYY-MM-DD")
        if status == "verified" and updated and (today - updated).days > stale_days:
            warnings.append(f"{pid}: verified {updated}, older than {stale_days} days → stale")
            if fix:
                meta["status"] = "stale"
                path.write_text(w.dump(meta, body), encoding="utf-8")
        elif status == "stale":
            warnings.append(f"{pid}: stale, needs re-checking")
        for ref in w.links(body):
            if ref in inbound:
                if ref != pid:
                    inbound[ref] += 1
            elif not w.resolve(ref):
                errors.append(f"{pid}: broken link [[{ref}]]")
        if meta.get("title"):
            titles.setdefault(norm(str(meta["title"])), []).append(pid)

    live_state = w.ROOT / "logs/live.json"   # just-wiki live: generated blocks must be fresh
    if any("<!-- live:" in p.read_text(encoding="utf-8") for p in w.pages()):
        try:
            run = date.fromisoformat(json.loads(live_state.read_text())["run"][:10])
            if (today - run).days > 2:
                warnings.append(f"generated blocks last refreshed {run}: run just-wiki live")
        except (OSError, ValueError, KeyError):
            warnings.append("generated blocks never refreshed: run just-wiki live")
    undescribed = set()
    for path in w.pages():
        for row in re.findall(r"^\|.*\| \*\*none\*\* \|$", path.read_text(encoding="utf-8"), re.M):
            name = re.search(r"`([^`]+)`", row) or re.search(r"^\| ([^|]+?) \|", row)
            undescribed.add(name.group(1))
    if undescribed:
        warnings.append(f"no page describes: {', '.join(sorted(undescribed))} (generated tables)")

    for pid, n in sorted(inbound.items()):
        if n == 0 and pid not in w.TOP_PAGES:
            warnings.append(f"{pid}: orphan (no page links to it)")
    for same in titles.values():
        if len(same) > 1:
            warnings.append(f"duplicate titles: {', '.join(same)} — merge them")

    for path in sorted(w.ROOT.rglob("*")):
        rel = path.relative_to(w.ROOT)
        # raw/ is scanned by the steward on ingest; tools/ (if any) holds code, not prose.
        if not path.is_file() or rel.parts[0] in (".git", "raw", "tools") or \
                path.suffix not in (".md", ".json", ".yaml", ".txt", ".toml"):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if w.SECRET.search(line):
                errors.append(f"{rel}:{n}: looks like a secret")

    brief, state = w.ROOT / "brief.md", w.ROOT / "logs/brief.json"
    if brief.exists() and state.exists():
        if hashlib.sha256(brief.read_bytes()).hexdigest() != json.loads(state.read_text())["sha256"]:
            errors.append("brief.md was edited by hand: it is generated, run just-wiki brief")

    extra_checks(errors, warnings)
    memory_checks(warnings)
    from . import verify
    warnings += verify.backlog_warnings()

    notes = w.inbox_notes()
    if notes:
        warnings.append(f"{len(notes)} inbox note(s) waiting for the steward")
    for note in notes:
        meta, _ = w.split(note.read_text(encoding="utf-8"))
        if meta.get("kind") not in w.NOTE_KINDS or not meta.get("sources"):
            warnings.append(f"{note.relative_to(w.ROOT)}: malformed note (kind or sources)")
    return errors, warnings


def build_index():
    """index.md is generated from frontmatter, never hand-edited: it can't drift from the pages."""
    lines = ["# Index", "",
             "Generated by `just-wiki lint --fix` from each page's frontmatter. Do not edit by hand.",
             "Start with [[status]] for what is going on now.", ""]
    for d in w.PAGE_DIRS:
        rows = []
        for path in sorted((w.ROOT / d).rglob("*.md")):
            meta, _ = w.split(path.read_text(encoding="utf-8"))
            flag = "" if meta.get("status") == "verified" else f" *({meta.get('status', '?')})*"
            rows.append(f"- [[{w.page_id(path)}]] — {meta.get('summary', '').strip()}{flag}")
        if rows:
            lines += [f"## {d.capitalize()}", ""] + rows + [""]
    (w.ROOT / "index.md").write_text("\n".join(lines), encoding="utf-8")


def stale_pages():
    """'page-id updated' for pages with status: stale, oldest first."""
    rows = []
    for path in w.pages():
        meta, _ = w.split(path.read_text(encoding="utf-8"))
        if meta.get("status") == "stale":
            rows.append((str(w.as_date(meta.get("updated")) or ""), w.page_id(path)))
    return [f"{pid} {d}" for d, pid in sorted(rows)]


def run(fix=False, stale=False, out=print):
    """The CLI. Returns the exit code."""
    w.require_root()
    if stale:
        for line in stale_pages():
            out(line)
        return 0
    errors, warnings = check(fix)
    if fix:
        build_index()
    for e in errors:
        out(f"ERROR   {e}")
    for x in warnings:
        out(f"WARNING {x}")
    out(f"{len(errors)} error(s), {len(warnings)} warning(s), {len(w.pages())} page(s)")
    if fix:
        text = f"{len(errors)} errors, {len(warnings)} warnings" + (f"; first error: {errors[0]}" if errors else "")
        # the nightly run lints twice; log only when the result differs from the last lint line
        log = w.LOG.read_text(encoding="utf-8").splitlines() if w.LOG.exists() else []
        last = [line for line in log if "] lint | " in line]
        if not last or not last[-1].endswith("] lint | " + text):
            w.append_log("lint", text)
    return 1 if errors else 0
