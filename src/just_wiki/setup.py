"""Setting a wiki up: `init` (a new wiki), `connect` (an agent), `units` (systemd user services)."""
import json
from datetime import date
import os
import re
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from . import notes
from . import store as w

TEMPLATE = resources.files("just_wiki").joinpath("templates/wiki")
DIRS = ("raw", "inbox", "logs")            # plus the page folders of the new wiki.toml ([pages] types)


def init(target):
    root = Path(target).expanduser().resolve()
    if (root / "wiki.toml").exists():
        raise SystemExit(f"{root} already has a wiki.toml")
    root.mkdir(parents=True, exist_ok=True)
    for d in DIRS:
        (root / d).mkdir(exist_ok=True)
    today = date.today().isoformat()
    for name in ("AGENTS.md", "status.md", "wiki.toml", "STEWARD.md"):
        if not (root / name).exists():
            text = TEMPLATE.joinpath(name).read_text(encoding="utf-8")
            (root / name).write_text(text.replace("updated: 2026-01-01", f"updated: {today}"), encoding="utf-8")
    if not (root / ".gitignore").exists():
        (root / ".gitignore").write_text(TEMPLATE.joinpath("gitignore").read_text(encoding="utf-8"))
    if not (root / "CLAUDE.md").exists():
        (root / "CLAUDE.md").symlink_to("AGENTS.md")
    (root / "log.md").touch()
    for d in ("raw", "inbox"):
        (root / d / ".gitkeep").touch()
    w.set_root(root)
    for d in w.PAGE_DIRS:
        (root / d).mkdir(exist_ok=True)
    from . import lint
    lint.build_index()
    w.append_log("init", "wiki created by just-wiki init")
    if not (root / ".git").exists() and shutil.which("git"):
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)])
        for key, val in (("user.name", "just-wiki"), ("user.email", "just-wiki@localhost")):   # the steward commits
            if not subprocess.run(["git", "-C", str(root), "config", key], capture_output=True, text=True).stdout.strip():
                subprocess.run(["git", "-C", str(root), "config", key, val])
        subprocess.run(["git", "-C", str(root), "add", "-A"])
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "just-wiki init"])
    print(f"New wiki in {root}.\nNext: cd {root} && just-wiki serve   (MCP for agents)\n"
          f"      just-wiki web     (web UI)\n      just-wiki connect claude-code")
    return 0


def mcp_url():
    c = w.cfg()["mcp"]
    host = "127.0.0.1" if c["host"] in ("0.0.0.0", "::") else c["host"]
    return f"http://{host}:{c['port']}/mcp"


PROMPT_LINE = (
    'Shared wiki (MCP "{name}"): the source of truth for this setup.\n'
    "- Before working on anything it covers, call get_status, then search / read_page (get_index lists every page). "
    "Trust 'verified' pages; check 'draft' and 'stale' ones first and say so. Results marked 'inbox (unreviewed)' are other "
    "agents' unchecked notes: search with include_inbox=false when you will act on the facts.\n"
    '- Write only with submit_note(agent="{agent}", kind, title, body, sources), in the same turn, before you reply, when '
    + notes.WRITE_WHEN + ". One fact per note, with numbers, dates, paths and a real source; none for chat, opinions or "
    "one-off errors.\n"
    "- Never put secrets, tokens or passwords in a note or in your memory: say where they are stored.")


def managed_block(path, key, text):
    """Put text between <!-- just-wiki:key --> markers in path (created if missing). True when the file changed."""
    path = Path(path).expanduser()
    begin, end = f"<!-- just-wiki:{key} -->", f"<!-- /just-wiki:{key} -->"
    block = f"{begin}\n{text.rstrip()}\n{end}\n"
    orig = path.read_text(encoding="utf-8") if path.exists() else ""
    pat = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\n?", re.S)
    new = pat.sub(lambda m: block, orig) if pat.search(orig) else (orig.rstrip("\n") + "\n\n" if orig else "") + block
    if new != orig:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
        return True
    return False


def add_memory_source(agent, kind, path):
    """--memory: add a [[memory.sources]] (and a report-only [memory] section if there is none) to wiki.toml."""
    toml = w.ROOT / "wiki.toml"
    text = toml.read_text(encoding="utf-8")
    if re.search(r"^\[memory\]", text, re.M) is None:
        text = (text.rstrip("\n") + "\n\n[memory]            # the agents' memory pointing at the wiki (just-wiki memory help)\n"
                'enabled = true\nmode = "report"       # "fix" lets it rewrite, with backups\npresets = ["network"]\n')
    if not re.search(r'(?m)^path[ \t]*=[ \t]*"' + re.escape(str(path)) + '"', text):      # a commented example does not count
        text = text.rstrip("\n") + f'\n\n[[memory.sources]]\nagent = "{agent}"\nkind = "{kind}"\npath = "{path}"\n'
    toml.write_text(text, encoding="utf-8")
    print(f"wiki.toml: memory source {agent} ({kind}) at {path}; try: just-wiki memory status, just-wiki memory scan")


def allow_wiki_tools(name):
    """Let Claude Code use the wiki's tools without asking each time: add "mcp__<name>" to permissions.allow in
    ~/.claude/settings.json. Without it Claude Code asks before every MCP call, and its auto mode blocks the write that records a
    note ("External System Writes"), so notes silently do not get written. Other settings are kept; a file that is not valid
    JSON is left alone. Returns what was done, as a sentence."""
    f = Path("~/.claude/settings.json").expanduser()
    rule = f"mcp__{name}"
    manual = f'add "{rule}" to permissions.allow in {f} yourself'
    try:
        data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except ValueError:
        return f"{f} is not valid JSON, so it was not changed; {manual}"
    perms = data.get("permissions", {}) if isinstance(data, dict) else None
    if perms is None or not isinstance(perms, dict) or not isinstance(perms.get("allow", []), list):
        return f"{f} has an unexpected shape, so it was not changed; {manual}"
    allow = perms.setdefault("allow", [])
    if rule in allow:
        return f"{rule} is already allowed in {f}"
    allow.append(rule)
    data["permissions"] = perms
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(f)
    return f"added {rule!r} to permissions.allow in {f}"


def connect(args):
    w.require_root()
    agent = args[0] if args else "generic"
    name = next((a.split("=", 1)[1] for a in args if a.startswith("--name=")), "wiki")
    url = mcp_url()
    key = re.sub(r"[^a-z0-9]+", "-", str(w.ROOT).lower()).strip("-")
    if agent == "claude-code":
        claude = shutil.which("claude")
        if not claude:
            raise SystemExit("claude not found on PATH")
        have = subprocess.run([claude, "mcp", "get", name], capture_output=True, text=True).returncode == 0
        if have:
            print(f"MCP server {name!r} already configured for Claude Code (claude mcp get {name})")
        else:
            subprocess.run([claude, "mcp", "add", "--scope", "user", "--transport", "http", name, url], check=True)
            print(f"added MCP server {name!r} → {url} (user scope: every Claude Code session)")
        if "--no-brief" not in args:
            text = (f"# {w.cfg()['wiki']['name']} ({w.ROOT})\n\n"
                    + PROMPT_LINE.format(name=name, agent="claude-code") + f"\n\n@{w.ROOT / 'brief.md'}")
            changed = managed_block("~/.claude/CLAUDE.md", key, text)
            print("~/.claude/CLAUDE.md: " + ("brief import added" if changed else "already up to date"))
        if "--no-allow" in args:
            print("Claude Code will ask before each use of the wiki's tools and may refuse the note-writing one in auto mode:\n"
                  f'allow "mcp__{name}" in permissions.allow of ~/.claude/settings.json to avoid that.')
        else:
            print("Claude Code permissions: " + allow_wiki_tools(name) + "  (--no-allow skips this)")
        if "--memory" in args:
            add_memory_source("claude", "claude-code", "~/.claude/projects/*/memory")
        return 0
    if agent == "codex":
        print(f"Add to ~/.codex/config.toml:\n\n[mcp_servers.{name}]\nurl = \"{url}\"\n\n"
              f"and to ~/.codex/AGENTS.md:\n\n{PROMPT_LINE.format(name=name, agent='codex')}")
        return 0
    if agent == "hermes":
        hdir = next((a.split("=", 1)[1] for a in args if a.startswith("--dir=")), "")
        print(f"1. Hermes config.yaml (restart Hermes afterwards):\n\nmcp_servers:\n  {name}:\n    url: {url}\n")
        print("   Hermes on the host, or in Docker with host networking (network_mode: host): that URL works as it is,\n"
              "   no token. In a container on a Docker bridge network 127.0.0.1 is the container itself: use host\n"
              "   networking, or set [mcp] host = \"0.0.0.0\" in wiki.toml, restart `just-wiki serve`, use\n"
              f"   http://<the host's address on that network>:{w.cfg()['mcp']['port']}/mcp and send the bearer token\n"
              f"   from {w.ROOT}/.mcp_token (never paste it into a note or chat; check your Hermes version for how an\n"
              "   MCP server takes a request header).\n")
        rule = PROMPT_LINE.format(name=name, agent="hermes")
        if hdir:
            home = Path(hdir).expanduser()
            target = str(home / "HERMES.md")
            soul = home / "SOUL.md"
            toml = w.ROOT / "wiki.toml"
            text = toml.read_text(encoding="utf-8")
            with_rule = not soul.exists()           # no SOUL.md: the rule rides on top of the brief in HERMES.md
            if target not in text:
                prefix = f"prefix = '''\n# Shared wiki rules\n\n{rule}\n\n'''\n" if with_rule else ""
                toml.write_text(text.rstrip("\n") + f'\n\n[[brief.targets]]                # Hermes loads HERMES.md '
                                f'from its working git repo\npath = "{target}"\n{prefix}', encoding="utf-8")
            print(f"2. Brief: wiki.toml copies the brief to {target}; `just-wiki brief` writes it and `just-wiki nightly`\n"
                  "   keeps it current. Hermes loads HERMES.md only when its working directory is inside a git repo that\n"
                  "   holds it.")
            if soul.exists():
                changed = managed_block(soul, key, f"## Shared wiki\n\n{rule}")
                state = "block added" if changed else "block already up to date"
                print(f"   The rule for writing notes is in {soul} ({state}); SOUL.md loads in every\n"
                      "   session whatever the working directory.\n")
            else:
                print(f"   No {soul}: the rule for writing notes goes at the top of HERMES.md instead. (A new SOUL.md would\n"
                      "   replace Hermes' default identity, so it is not created; add one yourself and re-run this to put\n"
                      "   the rule there.)\n")
        else:
            print("2. Brief: re-run with --dir=<your Hermes folder, the one with config.yaml> to copy the brief into\n"
                  "   its HERMES.md and to put the rule for writing notes into its SOUL.md (or HERMES.md).\n")
        if "--memory" in args:
            if hdir:
                add_memory_source("hermes", "hermes", str(Path(hdir).expanduser() / "memories"))
            else:
                print("--memory needs --dir=<your Hermes folder> for Hermes' memories/ folder")
        print((f"3. System prompt line (put it in SOUL.md or the system prompt):\n\n{rule}\n\n4. " if not hdir else "3. ")
              + "Steward: the default steward command is Claude Code. Without it set mode = \"pipeline\" and a\n"
              "   [steward.llm] endpoint (any OpenAI-compatible server) in wiki.toml; see the README.")
        return 0
    print(f"MCP (streamable HTTP): {url}\nOff localhost: header Authorization: Bearer <{w.ROOT}/.mcp_token>\n\n"
          f"Prompt line for the agent:\n\n{PROMPT_LINE.format(name=name, agent='<agent-name>')}")
    return 0


def exe():
    found = shutil.which("just-wiki")
    return found or f"{sys.executable} -m just_wiki.cli"


UNITS = {
    "mcp": ("[Unit]\nDescription=just-wiki MCP server for {root}\n[Service]\nExecStart={exe} --root {root} serve\n"
            "Restart=always\nRestartSec=10\n[Install]\nWantedBy=default.target\n"),
    "web": ("[Unit]\nDescription=just-wiki web UI for {root}\n[Service]\nExecStart={exe} --root {root} web\n"
            "Restart=always\nRestartSec=10\n[Install]\nWantedBy=default.target\n"),
    "steward.service": ("[Unit]\nDescription=just-wiki nightly run (live blocks, brief, lint, steward, push) for "
                        "{root}\n[Service]\nType=oneshot\nWorkingDirectory={root}\nEnvironment=PATH={path}\n"
                        "ExecStart={exe} --root {root} nightly\n"),
    "steward.timer": ("[Unit]\nDescription=just-wiki nightly run for {root}\n[Timer]\nOnCalendar=*-*-* {at}\n"
                      "Persistent=true\n[Install]\nWantedBy=timers.target\n"),
}


def units(install=False, prefix=None, at=None):
    w.require_root()
    args = sys.argv
    prefix = prefix or next((a.split("=", 1)[1] for a in args if a.startswith("--prefix=")), "just-wiki")
    at = at or next((a.split("=", 1)[1] for a in args if a.startswith("--at=")), "06:40")
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    files = {}
    for key, text in UNITS.items():
        name = f"{prefix}-{key}" + ("" if "." in key else ".service")
        files[name] = text.format(root=w.ROOT, exe=exe(), at=at, path=path)
    mem = w.cfg()["memory"]
    if mem.get("enabled") and mem.get("sources"):        # optional: sweep agents' memories on change and every N minutes
        from .memory import sources as msrc
        folders = sorted({str(f) for s in msrc.load(mem) for f in s.folders()})
        svc = f"{prefix}-memory.service"
        files[svc] = (f"[Unit]\nDescription=just-wiki: agents' memories vs. the wiki for {w.ROOT}\n[Service]\nType=oneshot\n"
                      f"WorkingDirectory={w.ROOT}\nEnvironment=PATH={path}\nExecStartPre=/usr/bin/env sleep 3\n"
                      f"ExecStart={exe()} --root {w.ROOT} memory run\n")
        files[f"{prefix}-memory.timer"] = (f"[Unit]\nDescription=just-wiki memory sweep every {mem['interval_min']} min\n"
                                           f"[Timer]\n"
                                           f"OnCalendar=*:0/{int(mem['interval_min'])}\nPersistent=false\n[Install]\nWantedBy=timers.target\n")
        if folders:
            files[f"{prefix}-memory.path"] = ("[Unit]\nDescription=just-wiki: sweep when a memory folder changes\n[Path]\n"
                                              + "".join(f"PathChanged={f}\n" for f in folders)
                                              + f"TriggerLimitIntervalSec=60\nTriggerLimitBurst=5\nUnit={svc}\n"
                                              "[Install]\nWantedBy=paths.target\n")
    if not install:
        for name, text in files.items():
            print(f"# ~/.config/systemd/user/{name}\n{text}")
        return 0
    d = Path.home() / ".config/systemd/user"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    print("installed: " + ", ".join(files) + f"\nstart: systemctl --user enable --now {prefix}-mcp {prefix}-web "
          f"{prefix}-steward.timer"
          + (f" {prefix}-memory.timer {prefix}-memory.path" if f"{prefix}-memory.timer" in files else ""))
    print(json.dumps(sorted(files)))
    return 0
