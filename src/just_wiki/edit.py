"""People editing the wiki from the web UI: pages (edit, create, delete) and the page folders (the schema in wiki.toml [pages]).

Agents write notes and the steward writes pages; people may change anything (AGENTS.md). A web edit is a direct write plus one
git commit of just that page (and index.md), made under the same lock as the steward's, so it never mixes with other work in
progress. A hash of the page as it was opened guards against overwriting a change made meanwhile (the steward's nightly merge,
another person). Every change is one commit, so the Changes tab can show it and git can revert it.

  load(pid) / save(pid, fields, base, user)       edit a page
  create(folder, fields, user)                    a new page in a folder
  delete(pid, user)                               remove a page (the commit can be reverted)
  folders() / add_folder() / set_about() / remove_folder()    the page folders and types in wiki.toml [pages]
"""
import hashlib
import json
import re
from datetime import date

from . import lint, pipeline, steward
from . import store as w

STATUSES = ("draft", "verified", "stale")
PID = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")
FOLDER = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
RESERVED = {"raw", "inbox", "logs", "index", "status", "log", "brief", "agents", "steward", "git", "claude"}
MAX_BODY = 200_000


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean(text):
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def path_of(pid):
    """The file of an editable page, or None: a curated page (a page folder or status), never a path outside the wiki."""
    if not PID.match(pid or "") or ".." in pid:
        return None
    if pid not in w.TOP_PAGES and pid.split("/")[0] not in w.PAGE_DIRS:
        return None
    return w.ROOT / f"{pid}.md"


def fields_of(meta, body):
    return {"title": str(meta.get("title") or ""), "summary": str(meta.get("summary") or ""),
            "status": str(meta.get("status") or "draft"),
            "sources": "\n".join(str(s) for s in (meta.get("sources") or [])), "body": body}


def load(pid):
    """{fields, base, type} of a page for the edit form, or None."""
    p = path_of(pid)
    if not p or not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    meta, body = w.split(text)
    return {"fields": fields_of(meta, body), "base": sha(text), "type": str(meta.get("type") or ""), "error": meta.get("_error")}


def source_list(text):
    return [s.strip() for s in clean(text).split("\n") if s.strip()]


def problems(f):
    """Why these fields can't be saved, as a list of sentences."""
    out = []
    title, summary, body = f["title"].strip(), f["summary"].strip(), clean(f["body"])
    if not 3 <= len(title) <= 150 or "\n" in title:
        out.append("the title must be 3–150 characters on one line")
    if len(summary) > 300 or "\n" in summary:
        out.append("the summary must be one line of at most 300 characters")
    if f["status"] not in STATUSES:
        out.append(f"the status must be one of {', '.join(STATUSES)}")
    sources = source_list(f["sources"])
    if f["status"] == "verified" and not sources:
        out.append("a verified page needs at least one source (a file, URL, command or note it comes from)")
    if any(len(s) > 300 for s in sources):
        out.append("each source must be under 300 characters")
    if len(body) > MAX_BODY:
        out.append(f"the text is over {MAX_BODY} characters")
    if w.SECRET.search(" ".join([title, summary, body, *sources])):
        out.append("it looks like it holds a secret (token, key, password): remove it and say where it is stored instead")
    return out


def commit(message, paths, user, op, pid):
    pipeline.commit(message, [str(p) for p in paths])
    w.append_log(op, f"{user}: {pid}")


def save(pid, f, base, user):
    """Write an edited page. Returns (ok, message)."""
    p = path_of(pid)
    if not p or not p.exists():
        return False, "that page does not exist"
    probs = problems(f)
    if probs:
        return False, "Not saved: " + "; ".join(probs) + "."
    with steward.git_lock():
        text = p.read_text(encoding="utf-8")
        if sha(text) != base:
            return False, ("Not saved: the page changed since you opened it (the steward or someone else edited it). Copy your "
                           "text, reload the page and apply your change again.")
        meta, _ = w.split(text)
        meta = {k: v for k, v in meta.items() if k != "_error"}
        meta.update(title=f["title"].strip(), summary=f["summary"].strip(), status=f["status"],
                    sources=source_list(f["sources"]), updated=date.today().isoformat(), updated_by=user)
        p.write_text(w.dump(meta, clean(f["body"]).strip("\n") + "\n"), encoding="utf-8")
        lint.build_index()
        commit(f"web: {user} edited {pid}", [p, w.ROOT / "index.md"], user, "edit", pid)
    return True, "Saved."


def create(folder, f, slug, user):
    """Create a page in a folder. Returns (ok, message, page id)."""
    if folder not in w.PAGE_DIRS:
        return False, f"{folder!r} is not a page folder (see the Schema tab)", ""
    slug = (slug or "").strip().lower()
    if not SLUG.match(slug):
        return False, "the file name must be lowercase letters, digits and hyphens (for example my-new-page)", ""
    pid = f"{folder}/{slug}"
    p = w.ROOT / f"{pid}.md"
    probs = problems(f)
    if probs:
        return False, "Not created: " + "; ".join(probs) + ".", pid
    with steward.git_lock():
        if p.exists():
            return False, f"{pid} already exists; open it and edit it instead", pid
        p.parent.mkdir(parents=True, exist_ok=True)
        meta = {"title": f["title"].strip(), "type": w.TYPES[folder], "status": f["status"], "summary": f["summary"].strip(),
                "sources": source_list(f["sources"]), "updated": date.today().isoformat(), "updated_by": user}
        body = clean(f["body"]).strip("\n") or f"# {f['title'].strip()}"
        p.write_text(w.dump(meta, body + "\n"), encoding="utf-8")
        lint.build_index()
        commit(f"web: {user} created {pid}", [p, w.ROOT / "index.md"], user, "create", pid)
    return True, "Created.", pid


def linked_from(pid):
    """Page ids whose text links to pid with [[pid]]."""
    out = []
    for q in w.pages():
        qid = w.page_id(q)
        if qid != pid and pid in w.links(q.read_text(encoding="utf-8")):
            out.append(qid)
    return out


def delete(pid, user):
    """Remove a page. Returns (ok, message). The commit can be reverted; links to it become broken."""
    p = path_of(pid)
    if not p or not p.exists():
        return False, "that page does not exist"
    if pid in w.TOP_PAGES:
        return False, f"{pid} is a standing page of the wiki and cannot be removed"
    with steward.git_lock():
        p.unlink()
        folder = p.parent
        if folder != w.ROOT / pid.split("/")[0] and not any(folder.iterdir()):
            folder.rmdir()
        lint.build_index()
        commit(f"web: {user} removed {pid}", [p, w.ROOT / "index.md"], user, "delete", pid)
    return True, f"Removed {pid}. Undo it from the Changes tab (git revert)."


# ---------------------------------------------------------------- the schema: page folders in wiki.toml [pages]

def folders():
    """[{folder, type, about, pages}] in order."""
    counts = {d: len(list((w.ROOT / d).rglob("*.md"))) if (w.ROOT / d).is_dir() else 0 for d in w.PAGE_DIRS}
    return [{"folder": d, "type": w.TYPES[d], "about": w.ABOUT.get(d, ""), "pages": counts[d]} for d in w.PAGE_DIRS]


def q(text):
    return json.dumps(str(text), ensure_ascii=False)


def types_toml(rows):
    return "types = [\n" + "".join(f"  {{folder = {q(r['folder'])}, type = {q(r['type'])}, about = {q(r.get('about', ''))}}},\n"
                                   for r in rows) + "]\n"


def write_types(rows):
    """Put rows into wiki.toml [pages] types (the rest of the file, comments included, stays as it is).

    Callers hold the git lock."""
    toml = w.ROOT / "wiki.toml"
    text = toml.read_text(encoding="utf-8") if toml.exists() else ""
    block = types_toml(rows)
    head = re.search(r"(?m)^\[pages\][ \t]*(?:#.*)?$", text)
    if not head:
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + "[pages]\n" + block
    else:
        end = re.search(r"(?m)^\[", text[head.end():])
        stop = head.end() + end.start() if end else len(text)
        section = text[head.end():stop]
        opened = re.search(r"(?m)^types[ \t]*=[ \t]*\[[ \t]*(?:#.*)?$", section)          # types = [ and the items on later lines
        if opened:
            closed = re.search(r"(?m)^\][ \t]*\n?", section[opened.end():])
            arr = (opened.start(), opened.end() + (closed.end() if closed else len(section) - opened.end()))
        else:
            one = re.search(r"(?m)^types[ \t]*=[ \t]*\[.*\][ \t]*\n?", section)               # types = [...] on one line
            arr = (one.start(), one.end()) if one else None
        section = (section[:arr[0]] + block + section[arr[1]:]) if arr else section.rstrip("\n") + "\n" + block + "\n"
        text = text[:head.end()] + section + text[stop:]
    toml.write_text(text, encoding="utf-8")
    w._cfg.update(root=None, data=None)
    w._set_page_types()


def add_folder(folder, type_, about, user):
    folder, type_ = (folder or "").strip().lower(), (type_ or "").strip().lower()
    if not FOLDER.match(folder) or folder in RESERVED:
        return False, "a folder name is lowercase letters, digits and hyphens, starts with a letter and is not a reserved name"
    if not FOLDER.match(type_):
        return False, "a type is a lowercase word (for example topic or person)"
    if folder in w.PAGE_DIRS:
        return False, f"{folder} already exists"
    with steward.git_lock():
        rows = [{k: r[k] for k in ("folder", "type", "about")} for r in folders()]
        rows.append({"folder": folder, "type": type_, "about": " ".join((about or "").split())})
        write_types(rows)
        (w.ROOT / folder).mkdir(exist_ok=True)
        commit(f"web: {user} added the page folder {folder}", [w.ROOT / "wiki.toml"], user, "schema", f"added {folder}")
    return True, f"Added {folder}/. Agents see it from their next call."


def set_about(folder, type_, about, user):
    if folder not in w.PAGE_DIRS:
        return False, f"{folder} is not a page folder"
    rows = [{"folder": r["folder"], "type": (type_ or r["type"]).strip().lower() if r["folder"] == folder else r["type"],
             "about": " ".join((about or "").split()) if r["folder"] == folder else r["about"]} for r in folders()]
    if not FOLDER.match(rows[w.PAGE_DIRS.index(folder)]["type"]):
        return False, "a type is a lowercase word (for example topic or person)"
    with steward.git_lock():
        write_types(rows)
        commit(f"web: {user} changed the page folder {folder}", [w.ROOT / "wiki.toml"], user, "schema", f"changed {folder}")
    return True, f"Saved {folder}. Existing pages keep the type in their frontmatter; new pages get the new one."


LINK = re.compile(r"(\[\[)([^\]|#\n]+)([^\]\n]*\]\])")
TOPIC = re.compile(r"(?m)^(topic:[ \t]*)(\S+)[ \t]*$")
TYPE = re.compile(r"(?m)^(type:[ \t]*)\S.*$")


def rewrite_links(text, mapping):
    """[[old/page]] → [[new/page]] for every moved page (anchors, aliases and a .md suffix are kept)."""
    def sub(m):
        ref = m.group(2).strip()
        bare = ref.removesuffix(".md")
        if bare not in mapping:
            return m.group(0)
        return m.group(1) + mapping[bare] + (".md" if ref.endswith(".md") else "") + m.group(3)
    return LINK.sub(sub, text)


def rewrite_everywhere(mapping):
    """Rewrite [[links]] in every curated page and the topic of notes still waiting. Returns the files changed."""
    changed = []
    for q in w.pages():
        text = q.read_text(encoding="utf-8")
        new = rewrite_links(text, mapping)
        if new != text:
            q.write_text(new, encoding="utf-8")
            changed.append(q)
    for n in w.inbox_notes():
        text = n.read_text(encoding="utf-8")
        new = TOPIC.sub(lambda m: m.group(1) + mapping.get(m.group(2), m.group(2)), text, count=1)
        if new != text:
            n.write_text(new, encoding="utf-8")
            changed.append(n)
    return changed


def folder_pages(folder):
    d = w.ROOT / folder
    return sorted(d.rglob("*.md")) if d.is_dir() else []


def removal_plan(folder):
    """What removing a folder involves: its pages and the pages in other folders that link into it."""
    pages = [w.page_id(p) for p in folder_pages(folder)]
    inside = set(pages)
    refs = sorted({w.page_id(q) for q in w.pages() if w.page_id(q) not in inside
                   and inside & set(w.links(q.read_text(encoding="utf-8")))})
    return {"folder": folder, "pages": pages, "linked_from": refs}


def remove_folder(folder, user, action="empty", target=""):
    """Remove a page folder from the schema. action: "empty" (only if it holds no pages), "move" (its pages go to the folder
    target: their type changes and every [[link]] to them is rewritten) or "delete" (the pages are removed too). One commit."""
    if folder not in w.PAGE_DIRS:
        return False, f"{folder} is not a page folder"
    if len(w.PAGE_DIRS) == 1:
        return False, "this is the only page folder: a wiki needs at least one"
    pages = folder_pages(folder)
    if pages and action == "empty":
        return False, f"{folder}/ still holds {len(pages)} page(s): choose to move them to another folder or to delete them"
    if action == "move" and (target not in w.PAGE_DIRS or target == folder):
        return False, "choose another existing folder to move the pages to"
    if action not in ("empty", "move", "delete"):
        return False, f"unknown action {action!r}"
    moves = {}
    if action == "move":
        for p in pages:
            dest = w.ROOT / target / p.relative_to(w.ROOT / folder)
            if dest.exists():
                return False, f"{w.page_id(dest)} already exists: rename one of the two pages first"
            moves[p] = dest
    changed = []
    with steward.git_lock():
        mapping = {w.page_id(p): w.page_id(d) for p, d in moves.items()}
        if mapping:
            changed = rewrite_everywhere(mapping)
            for p, dest in moves.items():
                dest.parent.mkdir(parents=True, exist_ok=True)
                text = p.read_text(encoding="utf-8")
                head, sep, rest = text.partition("\n---")
                text = TYPE.sub(lambda m: m.group(1) + w.TYPES[target], head, count=1) + sep + rest if sep else text
                dest.write_text(text, encoding="utf-8")
                p.unlink()
        elif action == "delete":
            for p in pages:
                p.unlink()
        for d in sorted((w.ROOT / folder).rglob("*"), reverse=True):        # empty sub-folders
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        write_types([{k: r[k] for k in ("folder", "type", "about")} for r in folders() if r["folder"] != folder])
        d = w.ROOT / folder
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
        lint.build_index()
        paths = [*pages, *moves.values(), *changed, w.ROOT / "wiki.toml", w.ROOT / "index.md"]
        verb = {"empty": "removed", "move": f"removed (its pages moved to {target})", "delete": "removed with its pages"}[action]
        commit(f"web: {user} {verb} the page folder {folder}", paths, user, "schema", f"{verb} {folder}")
    note = {"empty": "", "move": f" {len(pages)} page(s) moved to {target}/, {len(changed)} other file(s) had links updated.",
            "delete": f" {len(pages)} page(s) removed."}[action]
    return True, f"Removed {folder}/ from the schema.{note} Undo it from the Changes tab (git revert)."


def rename_folder(old, new, user):
    """Rename a page folder: same type and description, the pages move with it and every [[link]] to them is rewritten."""
    new = (new or "").strip().lower()
    if old not in w.PAGE_DIRS:
        return False, f"{old} is not a page folder"
    if not FOLDER.match(new) or new in RESERVED:
        return False, "a folder name is lowercase letters, digits and hyphens, starts with a letter and is not a reserved name"
    if new == old:
        return False, "that is already its name"
    if new in w.PAGE_DIRS or (w.ROOT / new).exists():
        return False, f"{new} already exists"
    pages = folder_pages(old)
    mapping = {w.page_id(p): new + w.page_id(p)[len(old):] for p in pages}
    with steward.git_lock():
        changed = rewrite_everywhere(mapping)
        if (w.ROOT / old).is_dir():
            (w.ROOT / old).rename(w.ROOT / new)
        write_types([{"folder": new if r["folder"] == old else r["folder"], "type": r["type"], "about": r["about"]}
                     for r in folders()])
        lint.build_index()
        paths = [*pages, *(w.ROOT / f"{m}.md" for m in mapping.values()), *changed, w.ROOT / "wiki.toml", w.ROOT / "index.md"]
        commit(f"web: {user} renamed the page folder {old} to {new}", paths, user, "schema", f"renamed {old} to {new}")
    return True, (f"Renamed {old}/ to {new}/: {len(pages)} page(s) moved, {len(changed)} other file(s) had links updated. "
                  "Undo it from the Changes tab (git revert).")
