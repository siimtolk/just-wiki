"""Optional: keep agents' own memory files pointing at the wiki.

Agents keep private memory (Claude Code's auto-memory, Hermes' MEMORY.md, notes in a folder). Setup facts copied there (an
address, a path, a unit name) drift away from the wiki and then beat it, because memory is loaded before the wiki is read.
This finds such copies, and with mode = "fix" replaces them by pointers ("(see wiki <page>)"). Off unless [memory] enabled.

  [memory]
  enabled = true
  mode = "report"                 # "report": warn only (lint, the Memory tab, `memory scan`); "fix": rewrite with backups
  nightly = true                  # the nightly run also does facts, audit and a sweep (false: you schedule `memory nightly`)
  presets = ["network", "paths"]  # network, paths, chat-ids, models; see patterns.py
  path_prefixes = ["/srv/"]       # for "paths"
  [[memory.sources]]
  agent = "claude"
  kind = "claude-code"            # claude-code | hermes | markdown-dir | file; path defaults to ~/.claude/projects/*/memory
  [[memory.sources]]
  agent = "hermes"
  kind = "hermes"
  path = "~/.hermes/memories"

  just-wiki memory scan                 the units holding a setup fact now (read-only)
  just-wiki memory fix [--apply]        what a fix would change; --apply does it (backups, lock, caps)
  just-wiki memory run                  one sweep as [memory] mode says (the timer and path unit call this)
  just-wiki memory facts [--dry-run]    list the setup values of the verified pages (needs an LLM)
  just-wiki memory audit                memory vs. wiki: contradicts or stale (needs an LLM; report only)
  just-wiki memory nightly              facts, audit and a sweep (the nightly run calls this)
  just-wiki memory report [--json]      the Memory tab as text or JSON
  just-wiki memory candidates           values an agent keeps writing into memory: material for a standing rule
  just-wiki memory status               settings, sources, counts
  just-wiki memory restore [DATE] [FILE] [--yes]   list or put back the backups of a day
  just-wiki memory line                 one line for a morning report
"""
import json
import sys

from .. import store as w
from . import audit, facts, fix, report, state


def off(out=print):
    out("memory is off: add [memory] enabled = true and at least one [[memory.sources]] to wiki.toml (see just-wiki memory help)")
    return 0


def nightly(out=print):
    """Facts, audit, then a sweep as the mode says. Never raises into the nightly run's other steps."""
    if not state.enabled():
        return 0
    rc = 0
    c = state.cfg()
    for name, fn in (("facts", lambda: facts.build(out=out) if c["facts"] else None),
                     ("audit", lambda: audit.run(out=out) if c["audit"] else None),
                     ("sweep", lambda: fix.run(c["mode"] == "fix", out))):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - one failing step must not stop the others
            out(f"memory {name} failed: {e!r}")
            rc = 1
    return rc


def main(args):
    w.require_root()
    cmd = args[0] if args else "help"
    rest = args[1:]
    c = state.cfg()
    if cmd in ("help", "-h", "--help"):
        print(__doc__.strip())
        return 0
    if cmd in ("run", "nightly") and not state.enabled():
        return 0
    if cmd not in ("status", "restore") and not c["sources"] and cmd not in ("facts",):
        return off()
    if cmd == "scan":
        hits = fix.scan()
        for h in hits:
            print(f"{h['where']}: {[t.text for t in h['tokens']]}\n   {h['line'][:160]}")
        for where, text in fix.stale_statuses():
            print(f"stale?  {where}: {text!r}")
        print(f"{len(hits)} unit(s) hold a setup fact")
        return 1 if hits else 0
    if cmd == "fix":
        return fix.run("--apply" in rest)
    if cmd == "run":
        return fix.run(c["mode"] == "fix")
    if cmd == "facts":
        facts.build(dry="--dry-run" in rest)
        return 0
    if cmd == "audit":
        audit.run()
        return 0
    if cmd == "nightly":
        return nightly()
    if cmd == "report":
        print(json.dumps(report.build(), ensure_ascii=False) if "--json" in rest else report.text())
        return 0
    if cmd == "candidates":
        items = report.candidates([r for r in state.rows()], report.rules_text())
        for i in items:
            print(f"{i['title']} [{i['tag']}]\n   {i['text']}")
        if not items:
            print("none yet: a value must be corrected twice (or a kind three times)")
        return 0
    if cmd == "line":
        s = report.line()
        if s:
            print(s)
        return 0
    if cmd == "status":
        print(f"memory: {'on' if state.enabled() else 'off'}, mode {c['mode']}, presets {c['presets']}")
        for s in fix.all_sources():
            print(f"  source {s.agent} ({s.kind}): {len(s.files())} file(s), {len(s.units())} unit(s) at {s.pattern}")
        f = state.read_facts()
        print(f"  listed values: {len(f.get('values', {}))} ({len(state.active_facts())} active), "
              f"retired {len(f.get('retired', {}))}")
        print(f"  corrections logged: {len(state.rows())}; backups in {state.backup_dir()}")
        return 0
    if cmd == "restore":
        pos = [a for a in rest if not a.startswith("--")]
        day = next((a for a in pos if len(a) == 10 and a[4] == "-"), None)
        name = next((a for a in pos if a != day), None)
        return fix.restore(day, name, "--yes" in rest)
    print(f"just-wiki memory: unknown command {cmd!r} (just-wiki memory help)", file=sys.stderr)
    return 2
