"""What counts as a setup fact in an agent's memory, and what a pointer to the wiki looks like.

Three layers, all matched as whole values (so a replacement swaps "http://192.0.2.5:8094/x" as one piece):

  presets      conservative built-ins, switched on by name in [memory] presets:
                 network   IPv4 addresses (with :port and /path), "port 8093", "localhost:8080"
                 paths     host paths under the folders in [memory] path_prefixes (e.g. ["/srv/", "/Data/"])
                 chat-ids  messenger group ids (a long digit string with the WhatsApp-style group suffix)
                 models    the names in [memory] model_names, with a version tail (qwen3.8-27b)
  patterns     your own: [[memory.patterns]] name, regex, kind (default "other")
  listed       values the wiki itself lists (facts.py: unit, container, job and path names from verified pages)

A line that holds a pointer ("(see wiki <page>)", "(see wiki)", "[[page]]", "wiki <folder>/<page>") passes: the bare word
"wiki" does not. The wiki's own folder is where agents are told to look, so paths under it are no facts.
"""
import re
from dataclasses import dataclass

from .. import store as w

TAIL = r"[^\s,;)'\"`]*"
POINTER = re.compile(r"\(see wiki[^)]*\)|\[\[[^\]]+\]\]|\bwiki [a-z]+/[a-z0-9][a-z0-9-]*", re.IGNORECASE)
STALE = re.compile(r"\b(connected|working|broken|down|not working|currently|as of|status)\b[^.\n]{0,80}?\b20\d\d-\d\d-\d\d",
                   re.IGNORECASE)
PRESETS = ("network", "paths", "chat-ids", "models")
LISTED = "listed from the wiki"
TRAIL = ".:,;!?"          # punctuation after a value belongs to the sentence, not to the value


@dataclass
class Token:
    start: int
    end: int
    text: str
    name: str
    kind: str


def has_pointer(line):
    return bool(POINTER.search(line))


def preset_patterns(cfg):
    """[(name, kind, compiled)] of the switched-on presets."""
    out = []
    presets = set(cfg.get("presets") or [])
    if "network" in presets:
        ipv4 = r"(?:https?://)?\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{2,5})?(?:/" + TAIL + ")?"
        out += [("IPv4 address", "address", re.compile(ipv4)),
                ("port number", "port", re.compile(r"\bport \d{2,5}\b", re.IGNORECASE)),
                ("localhost port", "address", re.compile(r"localhost:\d{2,5}", re.IGNORECASE))]
    if "paths" in presets:
        for p in cfg.get("path_prefixes") or []:
            out.append((f"host path {p}", "host_path", re.compile(r"(?<![\w.~/-])" + re.escape(p) + r"(?=[\w.~-])" + TAIL)))
    if "chat-ids" in presets:
        out.append(("chat id", "chat_id", re.compile(r"\b\d{15,}@g\.us")))
    if "models" in presets and cfg.get("model_names"):
        alt = "|".join(re.escape(n) for n in sorted(cfg["model_names"], key=len, reverse=True))
        out.append(("model name", "model", re.compile(r"\b(?:" + alt + r")[\w.\-]*", re.IGNORECASE)))
    return out


def user_patterns(cfg):
    out = []
    for i, p in enumerate(cfg.get("patterns") or []):
        try:
            out.append((str(p.get("name") or f"pattern {i + 1}"), str(p.get("kind") or "other"), re.compile(p["regex"])))
        except (KeyError, re.error, TypeError) as e:
            raise SystemExit(f"just-wiki: [[memory.patterns]] #{i + 1} is not usable: {e!r}") from None
    return out


def value_regex(values):
    """One regex for listed values, longest first; not inside a longer word, path or file name, not after a slash."""
    alt = "|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))
    return re.compile(r"(?<![\w./-])(?:" + alt + r")(?![\w-]|\.\w)") if alt else None


class Patterns:
    """The patterns of the wiki's [memory] section plus the active listed values."""

    def __init__(self, cfg, listed=None, kinds=None):
        self.cfg = cfg
        self.listed = dict(listed or {})
        self.kinds = dict(kinds or {})
        self.items = preset_patterns(cfg) + user_patterns(cfg)
        rx = value_regex(self.listed)
        if rx:
            self.items.append((LISTED, "listed", rx))
        self.root = str(w.ROOT)

    def tokens(self, line):
        """Non-overlapping Tokens in a line (earliest first, the longest wins), the wiki's own folder left out."""
        found = []
        for name, kind, rx in self.items:
            for m in rx.finditer(line):
                text = m.group(0)
                if text.startswith(self.root) or not text.strip(TRAIL):
                    continue
                trimmed = text.rstrip(TRAIL) if kind != "listed" else text
                found.append(Token(m.start(), m.start() + len(trimmed), trimmed, name,
                                   self.kinds.get(trimmed) or kind))
        found.sort(key=lambda t: (t.start, -(t.end - t.start)))
        out, last = [], -1
        for t in found:
            if t.start >= last:
                out.append(t)
                last = t.end
        return out

    def fact(self, line):
        """The first Token if the line holds a setup fact and no pointer, else None."""
        if has_pointer(line):
            return None
        toks = self.tokens(line)
        return toks[0] if toks else None

    def facts_in(self, text):
        return [(line, t) for line in text.splitlines() if (t := self.fact(line))]

    def sub(self, line, replacer):
        """The line with every Token replaced by replacer(Token); punctuation after a value stays."""
        out, pos = [], 0
        for t in self.tokens(line):
            out.append(line[pos:t.start])
            out.append(replacer(t))
            pos = t.end
        out.append(line[pos:])
        return "".join(out)

    def names(self):
        return [(n, k) for n, k, _ in self.items]

    def kind_of(self, value):
        for t in self.tokens(value):
            if t.text == value:
                return t.kind
        return self.kinds.get(value, "other")
