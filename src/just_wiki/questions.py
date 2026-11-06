"""Open questions: the rows of the "## Open questions" table in status.md, and the answers waiting for the steward.

A row is `| question | owner |`. Its id is the first 6 hex digits of sha1(question text without the steward's
`(checked …)` marks), so every tool that reads the table (the web UI, the steward, a morning brief) names a question
the same way, and the id survives the steward noting that it tried. A row whose text starts with
`**On YYYY-MM-DD:**` is a dated reminder. An answer is an inbox note with `answers: <id>` in its frontmatter
(`action`: decide | investigate | owner | postpone | drop); the steward carries it out and edits the row.
The steward may append `(checked YYYY-MM-DD: why)` to a row it could not settle; such a row waits a week.
"""
import hashlib
import re
from datetime import date, timedelta

from . import store as w

REMINDER = re.compile(r"^\*\*On (\d{4}-\d{2}-\d{2}):?\*\*:?")
CHECKED = re.compile(r"\(checked (\d{4}-\d{2}-\d{2})")
CHECK_MARK = re.compile(r"\s*\(checked \d{4}-\d{2}-\d{2}(?:[^()]|\([^()]*\))*\)")
ACTIONS = ("decide", "investigate", "owner", "postpone", "drop")
RECHECK_DAYS = 7


def base_text(text):
    """The question without the steward's (checked YYYY-MM-DD: …) marks."""
    return CHECK_MARK.sub("", text).strip()


def qid(text):
    return hashlib.sha1(base_text(text).encode()).hexdigest()[:6]


def owner_names(owner):
    return {x.strip().lower() for x in re.split(r"[/,&]| and ", owner) if x.strip()}


def rows():
    """[{id, text, owner, due, kind, mine, checked}] in table order. kind: reminder | mine | agent."""
    f = w.ROOT / "status.md"
    if not f.exists():
        return []
    me = (w.cfg()["wiki"]["owner"] or "").lower()
    out, inside = [], False
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            inside = line.strip() == "## Open questions"
            continue
        if not inside or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("Question", "") or set(cells[0]) <= set("-: "):
            continue
        text, owner = " | ".join(cells[:-1]), cells[-1]
        m = REMINDER.match(text)
        due = date.fromisoformat(m.group(1)) if m else None
        c = CHECKED.findall(text)
        mine = bool(me) and me in owner_names(owner)
        out.append({"id": qid(text), "text": text, "owner": owner, "due": due, "mine": mine,
                    "kind": "reminder" if due else "mine" if mine else "agent",
                    "checked": date.fromisoformat(c[-1]) if c else None})
    return out


def answers():
    """{question id: [note meta…]} for answers still in the inbox (not yet carried out by the steward)."""
    out = {}
    for n in w.inbox_notes():
        meta, body = w.split(n.read_text(encoding="utf-8"))
        qa = meta.get("answers")
        if not qa:   # older decision notes name the question only in their file name
            m = re.search(r"-decision-([0-9a-f]{6})$", n.stem)
            qa = m.group(1) if m else None
        if qa:
            said = re.search(r"^\*(?:Decision|New owner|Postpone until|Hint):\* (.+)$", body, re.M)
            if not said and meta.get("action") == "drop":
                said = re.search(r"\*Reason:\* (.+)$", body, re.M)
            out.setdefault(str(qa), []).append({**meta, "file": w.page_id(n), "path": str(n),
                                                "said": said.group(1).strip() if said else ""})
    return out


SETTLED = re.compile(r"^- \[(\d{4}-\d{2}-\d{2}) [\d:]+\] steward \| (\w+) q-([0-9a-f]{6})\b:?\s*(?:→\s*)?(.*)$")


def settled(days=14):
    """[{date, action, id, text}] of question rows the steward acted on in the last `days` days, newest first."""
    if not w.LOG.exists():
        return []
    since = (date.today() - timedelta(days=days)).isoformat()
    out = []
    for line in w.LOG.read_text(encoding="utf-8").splitlines():
        m = SETTLED.match(line)
        if m and m.group(1) >= since:
            out.append({"date": m.group(1), "action": m.group(2), "id": m.group(3), "text": m.group(4)})
    return out[::-1]


def withdraw(i, agent):
    """Delete the not-yet-handled answer notes `agent` wrote for question i. Returns how many."""
    from pathlib import Path
    n = 0
    for a in answers().get(i, []):
        if str(a.get("agent", "")).lower() == agent and Path(a["path"]).is_file():
            Path(a["path"]).unlink()
            n += 1
    if n:
        w.append_log("note", f"{agent} withdrew {n} answer(s) to q-{i}")
    return n


def needs_steward(q, today=None):
    """True when the steward should act on this row by itself (no answer note needed): a reminder that is due, or a
    question owned by an agent / nobody that it has not tried in the last RECHECK_DAYS days."""
    today = today or date.today()
    if q["checked"] and today - q["checked"] < timedelta(days=RECHECK_DAYS):
        return False
    if q["kind"] == "reminder":
        return q["due"] <= today
    return q["kind"] == "agent"


_asked = {}


def asked(q):
    """The date the row first appeared in status.md (git history), or None. Cached per question id."""
    if q["id"] in _asked:
        return _asked[q["id"]]
    import subprocess
    probe = re.sub(r"\s+", " ", base_text(q["text"]))[:60]
    d = None
    if (w.ROOT / ".git").exists() and probe:
        r = subprocess.run(["git", "-C", str(w.ROOT), "log", "--reverse", "--format=%as", "-S", probe, "--",
                            "status.md"], capture_output=True, text=True, timeout=20)
        first = r.stdout.split()
        if first:
            try:
                d = date.fromisoformat(first[0])
            except ValueError:
                d = None
    _asked[q["id"]] = d
    return d


def find(i):
    return next((q for q in rows() if q["id"] == i), None)


def answer_note(q, action, text, agent):
    """Write the answer for question q as an inbox note. Returns (ok, message)."""
    from . import notes
    if action not in ACTIONS:
        return False, f"unknown action {action!r}"
    text = (text or "").strip()
    short = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r"\1", REMINDER.sub("", q["text"]))
    short = re.sub(r"[*`_]", "", short).strip()
    short = short[:70].rsplit(" ", 1)[0] + "…" if len(short) > 72 else short
    what = {"decide": ("decision", f"Decided: {short}", f"*Decision:* {text}"),
            "investigate": ("question", f"Steward, find out: {short}",
                            "Find this out yourself with read-only checks, write the answer into the right page and "
                            "remove the row." + (f"\n\n*Hint:* {text}" if text else "")),
            "owner": ("decision", f"New owner for: {short}", f"*New owner:* {text}"),
            "postpone": ("decision", f"Postponed: {short}", f"*Postpone until:* {text}. Turn the row into a reminder "
                         f"`**On {text}:** …`."),
            "drop": ("decision", f"Dropped: {short}", "Remove the row" + (f". *Reason:* {text}" if text else "."))}
    if action in ("decide", "owner") and not text:
        return False, "write the decision first" if action == "decide" else "name the new owner"
    if action == "postpone":
        try:
            date.fromisoformat(text)
        except ValueError:
            return False, "postpone needs a date YYYY-MM-DD"
    kind, title, line = what[action]
    body = f"Answer to the open question `{q['id']}` in status.md.\n\n*Question:* {q['text']}\n\n{line}\n"
    ok, msg = notes.submit(agent, kind, title, body, ["status.md open questions", f"web UI ({agent})"], "status",
                           extra={"answers": q["id"], "action": action})
    return ok, msg


MECHANICAL = ("owner", "postpone", "drop")      # answers code carries out; decide and investigate need the steward


def edit_row(i, new_text=None, new_owner=None, remove=False):
    """Rewrite or remove the row with id i in status.md's Open questions. True when the row was found."""
    f = w.ROOT / "status.md"
    lines, inside, found = f.read_text(encoding="utf-8").splitlines(keepends=True), False, False
    for n, line in enumerate(lines):
        if line.startswith("## "):
            inside = line.strip() == "## Open questions"
            continue
        if not inside or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in ("Question", "") or set(cells[0]) <= set("-: "):
            continue
        text, owner = " | ".join(cells[:-1]), cells[-1]
        if qid(text) != i:
            continue
        found = True
        lines[n] = "" if remove else f"| {new_text or text} | {new_owner or owner} |\n"
        break
    if found:
        f.write_text("".join(lines), encoding="utf-8")
    return found


def carry_out(a):
    """Carry out one mechanical answer (owner, postpone, drop) on its row. Returns (done, log line) or (False, why)."""
    i, action, said = str(a.get("answers")), a.get("action"), (a.get("said") or "").strip()
    q = find(i)
    if not q:
        return True, f"{action} q-{i}: the row was already gone"
    if action == "owner":
        if not said:
            return False, "no new owner given"
        edit_row(i, new_owner=said)
        return True, f"owner q-{i} → status.md: owner is now {said}"
    if action == "postpone":
        try:
            when = date.fromisoformat(said[:10])
        except ValueError:
            return False, f"postpone date {said!r} is not YYYY-MM-DD"
        text = f"**On {when.isoformat()}:** " + REMINDER.sub("", base_text(q["text"])).strip()
        edit_row(i, new_text=text)
        return True, f"postpone q-{i} → status.md: reminder on {when.isoformat()}"
    if action == "drop":
        edit_row(i, remove=True)
        return True, f"drop q-{i} → status.md: removed" + (f" ({said[:120]})" if said else "")
    return False, f"{action} is not mechanical"
