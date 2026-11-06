"""just-wiki: a shared, stewarded wiki for AI agents.

  just-wiki init <dir>                  a new wiki from the templates
  just-wiki serve                       the MCP server for agents
  just-wiki web                         the web UI for people
  just-wiki lint [--fix | --stale]      health check; --fix also rebuilds index.md
  just-wiki live [--check | --show X]   refresh the generated blocks
  just-wiki brief [--show | --check]    write the brief to its targets
  just-wiki steward [--force] [--only=notes,questions,stale,unchecked] [--print|--install]   run the steward now
  just-wiki steward --due               the agent check of unchecked pages, only if one waited long enough (for a timer)
  just-wiki unchecked [--json]          pages the pipeline steward changed from notes nobody checked yet
  just-wiki questions [--steward] [--json]   open questions with ids, owners and answers
  just-wiki nightly                     live, brief, lint, steward when needed, push, report
  just-wiki note <agent> <kind> <title> --source S… [--topic P]   write a note (body on stdin)
  just-wiki log <op> <text…>            append a line to log.md
  just-wiki usage [days] [--json]       who used the wiki
  just-wiki stats [days] [--json]       who contributes what and where, how the wiki is used (the Stats tab)
  just-wiki gaps [resolve …]            searches that found nothing and what the steward did about them
  just-wiki connect <agent>             connect an agent (claude-code, codex, hermes, generic)
  just-wiki memory <command>            optional: keep agents' own memory pointing at the wiki (just-wiki memory help)
  just-wiki units [--install]           systemd user units for serve, web and nightly
  just-wiki hook-read                   Claude Code PostToolUse hook: count file reads of pages
  just-wiki root                        print the wiki root

Global: --root DIR (else $JUST_WIKI_ROOT, else the nearest wiki.toml at or above the current directory).
"""
import argparse
import json
import sys

from . import store as w


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help", "help") or not argv:
        print(__doc__.strip())
        return 0
    root = None
    if "--root" in argv:
        i = argv.index("--root")
        root = argv[i + 1]
        del argv[i:i + 2]
    if root:
        w.set_root(root)
    cmd, args = argv[0], argv[1:]

    if cmd == "init":
        from . import setup
        return setup.init(args[0] if args else ".")
    if cmd == "root":
        print(w.require_root())
        return 0
    if cmd == "serve":
        from . import mcp_server
        return mcp_server.serve()
    if cmd == "web":
        from . import web
        p = argparse.ArgumentParser(prog="just-wiki web")
        p.add_argument("--port", type=int)
        p.add_argument("--host")
        a = p.parse_args(args)
        return web.serve(host=a.host, port=a.port)
    if cmd == "lint":
        from . import lint
        return lint.run(fix="--fix" in args, stale="--stale" in args)
    if cmd == "live":
        from . import live
        return live.run(check="--check" in args, show=args[args.index("--show") + 1] if "--show" in args else None)
    if cmd == "brief":
        from . import brief
        return brief.run(show="--show" in args, check="--check" in args)
    if cmd == "steward":
        from . import steward
        if "--print" in args:
            w.require_root()
            only = next((a.split("=", 1)[1].split(",") for a in args if a.startswith("--only=")), None)
            print(steward.prompt(only))
            return 0
        if "--install" in args:
            w.require_root()
            print(steward.install())
            return 0
        if "--due" in args:
            return steward.due()
        only = next((a.split("=", 1)[1].split(",") for a in args if a.startswith("--only=")), None)
        return steward.steward_once(force="--force" in args, only=only)
    if cmd == "unchecked":
        from . import verify
        w.require_root()
        entries = verify.reconcile()
        if "--json" in args:
            print(json.dumps(entries, indent=1, ensure_ascii=False))
        else:
            for pid, e in sorted(entries.items(), key=lambda x: x[1]["since"]):
                print(f"{e['since']}  {pid}  notes: {', '.join(e['notes'])}  ({'; '.join(e['why'])})")
        return 0
    if cmd == "questions":
        from . import questions
        import json as _json
        w.require_root()
        ans = questions.answers()
        rows = [q for q in questions.rows() if "--steward" not in args or questions.needs_steward(q) or q["id"] in ans]
        if "--json" in args:
            print(_json.dumps([{**q, "due": str(q["due"] or ""), "checked": str(q["checked"] or ""),
                                "answers": ans.get(q["id"], [])} for q in rows], ensure_ascii=False, default=str))
            return 0
        for q in rows:
            a = ans.get(q["id"])
            tag = (f"answered ({a[-1].get('action')}, {a[-1]['file']})" if a else
                   "steward" if questions.needs_steward(q) else "waiting")
            print(f"q-{q['id']}  [{q['kind']}, {q['owner']}, {tag}]  {q['text'][:110]}")
        return 0
    if cmd == "nightly":
        from . import steward
        return steward.nightly()
    if cmd == "note":
        from . import notes
        p = argparse.ArgumentParser(prog="just-wiki note")
        p.add_argument("agent")
        p.add_argument("kind")
        p.add_argument("title")
        p.add_argument("--source", action="append", default=[])
        p.add_argument("--topic", default="")
        a = p.parse_args(args)
        w.require_root()
        ok, msg = notes.submit(a.agent, a.kind, a.title, sys.stdin.read(), a.source, a.topic)
        print(msg)
        return 0 if ok else 1
    if cmd == "log":
        if len(args) < 2:
            raise SystemExit("usage: just-wiki log <op> <text…>")
        w.require_root()
        w.append_log(args[0], " ".join(args[1:]))
        return 0
    if cmd == "usage":
        from . import usage
        nums = [a for a in args if a.isdigit()]
        return usage.run(days=int(nums[0]) if nums else 7, as_json="--json" in args)
    if cmd == "gaps":
        from . import stats
        return stats.gaps_main(args)
    if cmd == "stats":
        from . import stats
        return stats.main(args)
    if cmd == "memory":
        from . import memory
        return memory.main(args)
    if cmd == "connect":
        from . import setup
        return setup.connect(args)
    if cmd == "units":
        from . import setup
        return setup.units(install="--install" in args)
    if cmd == "hook-read":
        from . import readlog
        return readlog.hook()
    print(f"just-wiki: unknown command {cmd!r}\n\n{__doc__.strip()}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
