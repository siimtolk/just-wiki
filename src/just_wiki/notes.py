"""Writing a note into the inbox: the one way agents and scripts change the wiki. Validated: sources required,
secrets rejected. Used by the MCP tool submit_note and by `just-wiki note`."""
import re
from datetime import datetime

from . import store as w

# When an agent should write a note: said the same way in the MCP instructions, the prompt line and AGENTS.md.
WRITE_WHEN = ("(1) a page was wrong or missing something you needed and you found the real value, (2) someone states a "
              "decision, rule or preference about the setup, (3) a fix worked for a problem that can come back, (4) a job "
              "ends with a result worth keeping (a measurement, a verdict, what was installed)")


def submit(agent, kind, title, body, sources, topic="", extra=None):
    """Write one note. Returns (ok, message): the page id it was saved as, or why it was rejected.
    extra: more frontmatter keys (e.g. answers/action for an answer to an open question)."""
    agent, kind, title, body = agent.strip().lower(), kind.strip().lower(), " ".join(title.split()), body.strip()
    sources = [s.strip() for s in (sources or []) if s and s.strip()]
    problems = []
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,31}", agent):
        problems.append("agent must be a short lowercase name like 'hermes'")
    if kind not in w.NOTE_KINDS:
        problems.append(f"kind must be one of {', '.join(w.NOTE_KINDS)}")
    if not 5 <= len(title) <= 120:
        problems.append("title must be 5–120 characters")
    if not 20 <= len(body) <= 8000:
        problems.append("body must be 20–8000 characters")
    if not sources or any(len(s) > 300 for s in sources):
        problems.append("give at least one source (path, URL, command, log), each under 300 characters")
    if topic and not w.resolve(topic):
        problems.append(f"topic {topic!r} is not an existing page; leave it empty if unsure")
    if w.SECRET.search(" ".join([title, body, *sources])):
        problems.append("it looks like it holds a secret (token, key, password); remove it and refer to where it is "
                        "stored instead")
    if problems:
        return False, "REJECTED, nothing saved: " + "; ".join(problems) + ". Fix and call submit_note again."

    stamp = datetime.now().astimezone()
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "note"
    folder = w.INBOX / agent
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp:%Y-%m-%d-%H%M}-{slug}.md"
    n = 2
    while path.exists():
        path = folder / f"{stamp:%Y-%m-%d-%H%M}-{slug}-{n}.md"
        n += 1
    meta = {"title": title, "kind": kind, "agent": agent, "created": stamp.strftime("%Y-%m-%d %H:%M"),
            "topic": topic or None, "sources": sources, **(extra or {})}
    path.write_text(w.dump(meta, body), encoding="utf-8")
    w.append_log("note", f"{agent} {kind}: {title} → {w.page_id(path)}")
    return True, f"Saved as {w.page_id(path)}. It is searchable now and the steward will merge it into the wiki."
