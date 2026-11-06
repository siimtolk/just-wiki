"""Generated page sections: facts read from the host by commands you configure, so they cannot drift.

Pages hold blocks between `<!-- live:<name> -->` and `<!-- /live:<name> -->`. For each name, [live.blocks] in wiki.toml
gives a command (a list of arguments); its stdout (Markdown) replaces the block's contents. Nobody edits inside a block:
the next run overwrites it. A block changes only when the command's output changes, so commands print no timestamps.

  just-wiki live            refresh every block in every page
  just-wiki live --check    exit 1 if a block is out of date (nothing written)
  just-wiki live --show X   print block X
"""
import json
import re
import subprocess
from datetime import datetime

from . import config
from . import store as w

BLOCK = re.compile(r"(<!-- live:([a-z0-9_-]+) -->\n)(.*?)(<!-- /live:\2 -->)", re.S)
HEADER = "*Generated from the host by `just-wiki live` (nightly); do not edit inside this block.*\n\n"


def body(name):
    """The block's Markdown from its configured command; a short explanation when it can't be made."""
    blocks = w.cfg()["live"]["blocks"]
    if name not in blocks:
        return f"Unknown block `{name}`: add it to [live.blocks] in wiki.toml ({', '.join(blocks) or 'none set'})."
    argv = [config.expand(a, w.ROOT) for a in blocks[name]]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=int(w.cfg()["live"]["timeout"]), cwd=w.ROOT)
    except (OSError, subprocess.TimeoutExpired) as err:
        return f"Not generated: `{argv[0]}` failed ({err})."
    if r.returncode != 0:
        return f"Not generated: `{argv[0]}` exited {r.returncode}."
    return r.stdout.strip()


def render(name, cache):
    if name not in cache:
        text = body(name)
        if w.SECRET.search(text):
            text = "Not generated: the output looked like it held a secret."
        cache[name] = f"{HEADER}{text}\n"
    return cache[name]


def refresh(write=True):
    cache, changed = {}, []
    for path in w.pages():
        text = path.read_text(encoding="utf-8")
        new = BLOCK.sub(lambda m: m.group(1) + render(m.group(2), cache) + m.group(4), text)
        if new != text:
            changed.append(w.page_id(path))
            if write:
                path.write_text(new, encoding="utf-8")
    if write:
        state = w.ROOT / "logs/live.json"
        state.parent.mkdir(exist_ok=True)
        state.write_text(json.dumps({"run": datetime.now().astimezone().isoformat(timespec="seconds"),
                                     "blocks": sorted(cache), "changed": changed}, indent=1) + "\n")
    return changed


def run(check=False, show=None, out=print):
    w.require_root()
    if show:
        out(render(show, {}))
        return 0
    if check:
        stale = refresh(write=False)
        out("out of date: " + ", ".join(stale) if stale else "live blocks up to date")
        return 1 if stale else 0
    changed = refresh()
    out("live: " + (", ".join(changed) if changed else "no changes"))
    return 0
