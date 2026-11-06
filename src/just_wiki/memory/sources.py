"""Where agents keep their memory, as sources: [[memory.sources]] in wiki.toml (agent, kind, path).

  claude-code   Claude Code's auto-memory: <path>/*.md for every folder the glob finds (default
                ~/.claude/projects/*/memory). The MEMORY.md index of a folder and the frontmatter of each file are skipped.
  hermes        Hermes' MEMORY.md and USER.md: <path> is the memories folder or one file; entries are separated by a line
                holding only "§". Written under Hermes' own <file>.lock.
  markdown-dir  any agent that keeps notes as <path>/*.md (glob allowed); lines are the units.
  file          one text or Markdown file; lines are the units.

A Source gives two views of the same text: units() is what a fix rewrites (a line, or a Hermes entry), documents() is
what the audit reads whole (a file's body, or a Hermes entry). replace() writes one unit back atomically and returns
False when the file changed meanwhile (the next sweep looks again).
"""
import fcntl
import glob
import os
from dataclasses import dataclass
from pathlib import Path

HERMES_SEP = "\n§\n"
KINDS = ("claude-code", "hermes", "markdown-dir", "file")
DEFAULT_PATHS = {"claude-code": "~/.claude/projects/*/memory"}


@dataclass
class Unit:
    agent: str
    label: str            # shown in reports: file name, or file #n for a Hermes entry
    text: str
    path: Path
    kind: str

    @property
    def where(self):
        """The unit's place in logs and reports: the file (a Hermes entry shifts when others are removed)."""
        return f"{self.agent} {self.path.name if self.kind == 'hermes' else self.label}"


def body_lines(text):
    """The lines of a Markdown file without its frontmatter."""
    lines = text.splitlines()
    if lines and lines[0] == "---" and "---" in lines[1:]:
        return lines[lines.index("---", 1) + 1:]
    return lines


class Source:
    def __init__(self, agent, kind, path, root=None):
        if kind not in KINDS:
            raise SystemExit(f"just-wiki: [[memory.sources]] kind {kind!r} is not one of {', '.join(KINDS)}")
        self.agent, self.kind = agent, kind
        self.pattern = str(Path(os.path.expanduser(str(path or DEFAULT_PATHS.get(kind, "")))))

    # ---- which files
    def files(self):
        if self.kind == "hermes":
            p = Path(self.pattern)
            return [p] if p.is_file() else [x for x in (p / "MEMORY.md", p / "USER.md") if x.exists()]
        found = [Path(x) for x in sorted(glob.glob(self.pattern))]
        out = []
        for p in found:
            if p.is_dir():
                if self.kind == "file":
                    continue
                if p.parent.name.startswith("-tmp"):        # Claude Code's scratch projects
                    continue
                out += [f for f in sorted(p.glob("*.md")) if not (self.kind == "claude-code" and f.name == "MEMORY.md")]
            elif p.is_file():
                out.append(p)
        return out

    def folders(self):
        """Folders to watch for changes (a path unit)."""
        if self.kind == "hermes":
            p = Path(self.pattern)
            return [p if p.is_dir() else p.parent]
        return sorted({f.parent for f in self.files()} | {Path(x) for x in glob.glob(self.pattern) if os.path.isdir(x)})

    # ---- what to read
    def units(self):
        out = []
        for f in self.files():
            text = f.read_text(encoding="utf-8", errors="replace")
            if self.kind == "hermes":
                for i, e in enumerate(text.split(HERMES_SEP)):
                    if e.strip():
                        out.append(Unit(self.agent, f"{f.name} #{i + 1}", e, f, self.kind))
            else:
                label = f"{f.parent.parent.name}/{f.name}" if self.kind == "claude-code" else f.name
                out += [Unit(self.agent, label, line, f, self.kind) for line in body_lines(text) if line.strip()]
        return out

    def documents(self):
        out = []
        for f in self.files():
            text = f.read_text(encoding="utf-8", errors="replace")
            if self.kind == "hermes":
                out += [Unit(self.agent, f"{f.name} #{i + 1}", e, f, self.kind)
                        for i, e in enumerate(text.split(HERMES_SEP)) if e.strip()]
            else:
                body = "\n".join(body_lines(text)).strip()
                if body:
                    label = f"{f.parent.parent.name}/{f.name}" if self.kind == "claude-code" else f.name
                    out.append(Unit(self.agent, label, body, f, self.kind))
        return out

    # ---- write one unit back
    def replace(self, unit, new):
        path = unit.path
        if self.kind == "hermes":
            with open(path.with_suffix(path.suffix + ".lock"), "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    entries = path.read_text(encoding="utf-8").split(HERMES_SEP)
                    if unit.text not in entries:
                        return False                      # the agent changed it meanwhile
                    entries[entries.index(unit.text)] = new
                    tmp = path.with_name(".just-wiki_" + path.name)
                    tmp.write_text(HERMES_SEP.join(entries), encoding="utf-8")
                    os.replace(tmp, path)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
            return True
        text = path.read_text(encoding="utf-8")
        if unit.text not in text:
            return False
        tmp = path.with_name(".just-wiki_" + path.name)
        tmp.write_text(text.replace(unit.text, new, 1), encoding="utf-8")
        if path.read_text(encoding="utf-8") != text:      # the agent wrote it while we worked
            tmp.unlink()
            return False
        os.replace(tmp, path)
        return True


def load(cfg):
    """[Source] of [memory] sources."""
    out = []
    for i, s in enumerate(cfg.get("sources") or []):
        if not isinstance(s, dict) or not s.get("kind"):
            raise SystemExit(f"just-wiki: [[memory.sources]] #{i + 1} needs agent, kind and path")
        out.append(Source(str(s.get("agent") or s["kind"]), s["kind"], s.get("path")))
    return out
