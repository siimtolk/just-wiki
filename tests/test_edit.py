"""Editing from the web UI: pages (edit, create, delete) and the page folders in wiki.toml [pages]."""
import base64
import re
import subprocess
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from just_wiki import edit, lint, setup, web
from just_wiki import store as w

PAGE = """---
title: Printer
type: topic
status: verified
summary: The office printer.
sources: [/etc/cups/printers.conf]
updated: 2026-01-01
updated_by: claude-code
owner: facilities
---

# Printer

Lives in room 4. See [[topics/scanner]].

<!-- live:printers -->
generated
<!-- /live:printers -->
"""


def log(root, n=3):
    return subprocess.run(["git", "-C", str(root), "log", f"-{n}", "--format=%s"], capture_output=True, text=True).stdout.split("\n")


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "nogitconfig"))
    setup.init(tmp_path / "wiki")
    root = tmp_path / "wiki"
    (root / "topics").mkdir(exist_ok=True)
    (root / "topics/printer.md").write_text(PAGE)
    (root / "topics/scanner.md").write_text(PAGE.replace("Printer", "Scanner").replace("[[topics/scanner]]", "no links"))
    lint.build_index()
    subprocess.run(["git", "-C", str(root), "add", "-A"])
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "pages"])
    return root


def fields(**kw):
    f = {"title": "Printer", "summary": "The office printer.", "status": "verified", "sources": "/etc/cups/printers.conf",
         "body": "# Printer\n\nLives in room 5 now.\n"}
    return {**f, **kw}


# ---- pages

def test_save_keeps_the_frontmatter_stamps_the_edit_and_commits_one_page(wiki):
    page = edit.load("topics/printer")
    assert page["fields"]["sources"] == "/etc/cups/printers.conf" and page["type"] == "topic"
    ok, msg = edit.save("topics/printer", fields(summary="Room 5 printer."), page["base"], "siim")
    assert ok, msg
    text = (wiki / "topics/printer.md").read_text()
    meta, body = w.split(text)
    assert meta["owner"] == "facilities" and meta["type"] == "topic"                 # keys the form does not show are kept
    assert meta["updated_by"] == "siim" and meta["summary"] == "Room 5 printer." and "room 5 now" in body
    assert "Room 5 printer." in (wiki / "index.md").read_text()                      # the index follows
    assert log(wiki, 1)[0] == "web: siim edited topics/printer"
    assert "edit | siim: topics/printer" in (wiki / "log.md").read_text()
    changed = subprocess.run(["git", "-C", str(wiki), "show", "--name-only", "--format=", "HEAD"], capture_output=True, text=True)
    assert set(changed.stdout.split()) == {"topics/printer.md", "index.md"}          # only this page and the index


def test_a_change_made_meanwhile_is_not_overwritten(wiki):
    page = edit.load("topics/printer")
    (wiki / "topics/printer.md").write_text(PAGE + "\nA line the steward added.\n")
    ok, msg = edit.save("topics/printer", fields(), page["base"], "siim")
    assert not ok and "changed since you opened it" in msg
    assert "steward added" in (wiki / "topics/printer.md").read_text()


@pytest.mark.parametrize("change,expect", [({"status": "verified", "sources": ""}, "needs at least one source"),
                                           ({"title": "x"}, "title"), ({"status": "great"}, "status must be"),
                                           ({"summary": "a\nb"}, "summary"),
                                           ({"body": "password = abcdefghijklmnop1234"}, "secret")])
def test_invalid_edits_are_rejected_with_the_reason(wiki, change, expect):
    page = edit.load("topics/printer")
    ok, msg = edit.save("topics/printer", fields(**change), page["base"], "siim")
    assert not ok and expect in msg and msg.startswith("Not saved")
    assert (wiki / "topics/printer.md").read_text() == PAGE


@pytest.mark.parametrize("pid", ["../wiki.toml", "index", "inbox/claude-code/x", "topics/../../x", "topics/nope", "/etc/passwd", ""])
def test_only_curated_pages_can_be_opened_for_editing(wiki, pid):
    assert edit.load(pid) is None and edit.path_of(pid) in (None, wiki / f"{pid}.md")
    assert edit.save(pid, fields(), "x", "siim")[0] is False


def test_create_a_page_in_a_folder(wiki):
    f = fields(title="Label printer", summary="The label printer.", sources="", status="draft", body="")
    ok, msg, pid = edit.create("topics", f, "label-printer", "siim")
    assert ok and pid == "topics/label-printer"
    meta, body = w.split((wiki / "topics/label-printer.md").read_text())
    assert meta["type"] == "topic" and meta["status"] == "draft" and body.startswith("# Label printer")
    assert "topics/label-printer" in (wiki / "index.md").read_text() and log(wiki, 1)[0] == "web: siim created topics/label-printer"
    assert edit.create("topics", f, "label-printer", "siim")[0] is False             # exists
    assert edit.create("nowhere", f, "x", "siim")[0] is False
    assert edit.create("topics", f, "Bad Name", "siim")[0] is False
    assert edit.create("topics", fields(status="verified", sources=""), "v", "siim")[0] is False


def test_delete_lists_links_and_can_be_reverted(wiki):
    assert edit.linked_from("topics/scanner") == ["topics/printer"]
    ok, msg = edit.delete("topics/scanner", "siim")
    assert ok and not (wiki / "topics/scanner.md").exists()
    assert "topics/scanner" not in (wiki / "index.md").read_text() and log(wiki, 1)[0] == "web: siim removed topics/scanner"
    subprocess.run(["git", "-C", str(wiki), "revert", "--no-edit", "HEAD"], capture_output=True)
    assert (wiki / "topics/scanner.md").exists()                                      # undo works
    assert edit.delete("status", "siim")[0] is False and edit.delete("topics/nope", "siim")[0] is False


# ---- the schema

def test_add_change_and_remove_a_page_folder(wiki):
    toml = wiki / "wiki.toml"
    before = toml.read_text()
    ok, msg = edit.add_folder("guides", "guide", "how to do things", "siim")
    assert ok and (wiki / "guides").is_dir() and "guides" in w.PAGE_DIRS and w.TYPES["guides"] == "guide"
    data = tomllib.loads(toml.read_text())
    assert {"folder": "guides", "type": "guide", "about": "how to do things"} in data["pages"]["types"]
    assert len(data["pages"]["types"]) == len(tomllib.loads(before)["pages"]["types"]) + 1
    assert "# just-wiki settings" in toml.read_text()                                 # the rest of the file is untouched
    assert log(wiki, 1)[0] == "web: siim added the page folder guides"
    assert edit.create("guides", fields(status="draft", sources=""), "first", "siim")[0]
    assert (wiki / "guides/first.md").exists() and w.split((wiki / "guides/first.md").read_text())[0]["type"] == "guide"
    assert edit.remove_folder("guides", "siim")[0] is False                           # it holds a page
    assert edit.set_about("guides", "guide", "short guides", "siim")[0] and w.ABOUT["guides"] == "short guides"
    edit.delete("guides/first", "siim")
    ok, msg = edit.remove_folder("guides", "siim")
    assert ok and "guides" not in w.PAGE_DIRS and not (wiki / "guides").exists()


def test_folder_names_and_types_are_validated(wiki):
    for bad in ("inbox", "raw", "status", "a b", "", "../x", "1x", "x" * 40):
        assert edit.add_folder(bad, "guide", "", "siim")[0] is False, bad
    assert edit.add_folder("guides", "Bad Type", "", "siim")[0] is False
    assert edit.add_folder("Notes", "note", "", "siim")[0] and "notes" in w.PAGE_DIRS         # a name is lowercased
    assert edit.add_folder("topics", "topic", "", "siim")[0] is False                  # exists
    assert edit.set_about("nope", "x", "y", "siim")[0] is False


def test_a_wiki_without_a_pages_section_gets_one_with_the_defaults_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "nogitconfig"))
    setup.init(tmp_path / "wiki")
    toml = tmp_path / "wiki/wiki.toml"
    toml.write_text(re.sub(r"(?ms)^\[pages\].*", "", toml.read_text()))              # no [pages]: the setup-wiki defaults apply
    w._cfg["root"] = None
    w._set_page_types()
    assert "systems" in w.PAGE_DIRS and "[pages]" not in toml.read_text()
    assert edit.add_folder("guides", "guide", "", "siim")[0]
    types = [t["folder"] for t in tomllib.loads(toml.read_text())["pages"]["types"]]
    assert types[:5] == ["systems", "projects", "decisions", "runbooks", "results"] and types[-1] == "guides"


def test_single_line_types_are_replaced_too(wiki):
    toml = wiki / "wiki.toml"
    toml.write_text(re.sub(r"(?ms)^\[pages\].*", '[pages]\ntypes = [{folder = "notes", type = "note", about = "n"}]\n\n[extra]\nk = 1\n',
                           toml.read_text()))
    w._cfg["root"] = None
    w._set_page_types()
    assert w.PAGE_DIRS == ("notes",)
    assert edit.add_folder("more", "thing", "", "siim")[0]
    data = tomllib.loads(toml.read_text())
    assert [t["folder"] for t in data["pages"]["types"]] == ["notes", "more"] and data["extra"] == {"k": 1}


def test_a_running_server_notices_a_changed_schema(wiki):
    assert "guides" not in w.PAGE_DIRS
    toml = wiki / "wiki.toml"
    other = toml.read_text().replace('{folder = "results"', '{folder = "guides", type = "guide", about = "g"},\n  {folder = "results"')
    toml.write_text(other)                                                            # another process edited it
    import os
    os.utime(toml, (toml.stat().st_atime + 5, toml.stat().st_mtime + 5))
    (wiki / "guides").mkdir()
    (wiki / "guides/a.md").write_text(PAGE)
    assert "guides/a" in [w.page_id(p) for p in w.pages()] and "guides" in w.PAGE_DIRS


# ---- the web UI: forms, escaping and a real request cycle

def test_forms_escape_what_they_show(wiki):
    page = edit.load("topics/printer")
    hostile = fields(title='"><script>alert(1)</script>', body="</textarea><script>alert(2)</script>", sources="<b>x</b>")
    html = web.edit_view("topics/printer", "tok", "Not saved: <i>x</i>", hostile, page["base"])
    assert "<script>alert" not in html and "&lt;script&gt;alert" in html and "<i>x</i>" not in html     # the payload is escaped
    assert 'name="t" value="tok"' in html and 'action="/_edit/topics/printer"' in html and "Cancel" in html
    assert "regenerated" not in html                                                      # no live block in this draft: no warning
    assert "live:name" in web.edit_view("topics/printer", "tok") and "regenerated" in web.edit_view("topics/printer", "tok")
    assert "New page" in web.new_view("tok") and "<option value='topics'" in web.new_view("tok") and "data-about=" in web.new_view("tok")
    assert web.delete_view("topics/scanner", "tok", web.load()).count("Printer") >= 1       # lists who links here
    assert web.delete_view("status", "tok", web.load()) is None and web.edit_view("index", "tok") is None
    assert "Schema" in web.schema_view("tok") and "/_schema/add" in web.schema_view("tok")
    assert ("/_schema", "Schema") in web.all_tabs()
    body = web.page_view("topics/printer", web.load())
    assert "/_edit/topics/printer" in body and "/_delete/topics/printer" in body


class Client:
    def __init__(self, root):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.srv.password = "pw"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.auth = "Basic " + base64.b64encode(f"{w.cfg()['web']['user']}:pw".encode()).decode()

    def req(self, path, data=None):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        r = urllib.request.Request(self.base + path, data=urllib.parse.urlencode(data).encode() if data else None,
                                   headers={"Authorization": self.auth})
        try:
            with urllib.request.build_opener(NoRedirect).open(r) as resp:
                return resp.status, resp.read().decode(), resp.headers
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode(), err.headers

    def close(self):
        self.srv.shutdown()


def test_the_whole_request_cycle_edit_create_delete_and_schema(wiki):
    c = Client(wiki)
    try:
        status, html, _ = c.req("/_edit/topics/printer")
        assert status == 200 and "Edit “Printer”" in html and "class=\"editor-grid\"" in html and "<nav>" not in html   # no page list in the way
        tok = re.search(r'name="t" value="([0-9a-f]+)"', html).group(1)
        base = re.search(r'name="base" value="([0-9a-f]{64})"', html).group(1)
        form = {**fields(body="# Printer\n\nRoom 6.\n"), "t": tok, "base": base}
        status, _, headers = c.req("/_edit/topics/printer", {**form, "t": "wrong"})
        assert status == 403 and "Room 6" not in (wiki / "topics/printer.md").read_text()      # no valid form token, no write
        status, _, headers = c.req("/_edit/topics/printer", form)
        assert status == 303 and headers["Location"].startswith("/topics/printer?m=")
        assert "Room 6." in (wiki / "topics/printer.md").read_text()
        status, html, _ = c.req(headers["Location"])
        assert status == 200 and "Saved." in html and "Room 6." in html
        status, html, _ = c.req("/_edit/topics/printer", form)                                # the same base again: now stale
        assert status == 200 and "changed since you opened it" in html and "Room 6." in html  # the user's text stays in the form
        # new page
        status, html, _ = c.req("/_new?folder=topics")
        assert status == 200 and "<select name='folder'>" in html
        status, _, headers = c.req("/_new", {"t": tok, "folder": "topics", "slug": "toner", "title": "Toner", "summary": "s",
                                             "status": "draft", "sources": "", "body": "Black toner."})
        assert status == 303 and (wiki / "topics/toner.md").exists()
        # delete asks first, then removes
        status, html, _ = c.req("/_delete/topics/toner")
        assert status == 200 and "Remove the page" in html
        status, _, headers = c.req("/_delete/topics/toner", {"t": tok})
        assert status == 303 and (wiki / "topics/toner.md").exists()                          # no confirm: nothing happens
        status, _, headers = c.req("/_delete/topics/toner", {"t": tok, "confirm": "1"})
        assert status == 303 and not (wiki / "topics/toner.md").exists()
        # schema
        status, _, headers = c.req("/_schema/add", {"t": tok, "folder": "guides", "type": "guide", "about": "how-tos"})
        assert status == 303 and (wiki / "guides").is_dir()
        status, html, _ = c.req("/_schema")
        assert status == 200 and "guides/" in html and "Remove" in html
        assert c.req("/_edit/index")[0] == 404                                              # not an editable page
        with pytest.raises(urllib.error.HTTPError) as err:                                  # no login, no editor
            urllib.request.urlopen(c.base + "/_edit/topics/printer")
        assert err.value.code == 401
    finally:
        c.close()


# ---- removing and renaming folders that hold pages

def setup_links(wiki):
    (wiki / "decisions").mkdir(exist_ok=True)
    (wiki / "decisions/d1.md").write_text(PAGE.replace("Printer", "Buy toner").replace("[[topics/scanner]]",
                                          "[[topics/printer#Setup|the printer]] and [[topics/scanner.md]]").replace("type: topic", "type: decision"))
    note = wiki / "inbox/claude-code"
    note.mkdir(parents=True, exist_ok=True)
    (note / "n1.md").write_text("---\ntitle: A note\nkind: finding\nagent: claude-code\ncreated: 2026-01-01 10:00\n"
                                "topic: topics/printer\nsources: [x]\n---\nSomething about the printer, long enough.\n")
    subprocess.run(["git", "-C", str(wiki), "add", "-A"])
    subprocess.run(["git", "-C", str(wiki), "commit", "-qm", "links"])


def test_removing_a_folder_can_move_its_pages_and_rewrites_every_link(wiki):
    setup_links(wiki)
    plan = edit.removal_plan("topics")
    assert plan["pages"] == ["topics/printer", "topics/scanner"] and plan["linked_from"] == ["decisions/d1"]
    assert edit.remove_folder("topics", "siim")[0] is False                              # not silently
    assert edit.remove_folder("topics", "siim", "move", "topics")[0] is False             # the same folder
    assert edit.remove_folder("topics", "siim", "move", "nowhere")[0] is False
    ok, msg = edit.remove_folder("topics", "siim", "move", "howtos")
    assert ok, msg
    assert not (wiki / "topics").exists() and "topics" not in w.PAGE_DIRS
    meta, body = w.split((wiki / "howtos/printer.md").read_text())
    assert meta["type"] == "howto" and meta["owner"] == "facilities"      # type follows the folder, the rest stays
    assert "[[howtos/scanner]]" in body                                                    # a link inside a moved page
    d1 = (wiki / "decisions/d1.md").read_text()
    assert "[[howtos/printer#Setup|the printer]]" in d1 and "[[howtos/scanner.md]]" in d1  # anchor, alias and suffix kept
    assert "topic: howtos/printer" in (wiki / "inbox/claude-code/n1.md").read_text()      # a waiting note's topic too
    index = (wiki / "index.md").read_text()
    assert "howtos/printer" in index and "topics/printer" not in index
    assert tomllib.loads((wiki / "wiki.toml").read_text())["pages"]["types"] and "topics" not in (wiki / "wiki.toml").read_text().split("[pages]")[1]
    assert log(wiki, 1)[0] == "web: siim removed (its pages moved to howtos) the page folder topics"
    subprocess.run(["git", "-C", str(wiki), "revert", "--no-edit", "HEAD"], capture_output=True)
    assert (wiki / "topics/printer.md").exists() and "[[topics/scanner]]" in (wiki / "topics/printer.md").read_text()   # undo works


def test_a_move_that_would_overwrite_a_page_changes_nothing(wiki):
    (wiki / "howtos").mkdir(exist_ok=True)
    (wiki / "howtos/printer.md").write_text(PAGE.replace("Printer", "Other"))
    before = {p: p.read_text() for p in wiki.rglob("*.md")}
    ok, msg = edit.remove_folder("topics", "siim", "move", "howtos")
    assert not ok and "howtos/printer already exists" in msg
    assert {p: p.read_text() for p in wiki.rglob("*.md")} == before and "topics" in w.PAGE_DIRS


def test_removing_a_folder_can_delete_its_pages_and_the_last_folder_stays(wiki):
    ok, msg = edit.remove_folder("topics", "siim", "delete")
    assert ok and not (wiki / "topics").exists() and "2 page(s) removed" in msg
    assert "topics/printer" not in (wiki / "index.md").read_text()
    for d in list(w.PAGE_DIRS)[:-1]:
        assert edit.remove_folder(d, "siim", "move", w.PAGE_DIRS[-1] if w.PAGE_DIRS[-1] != d else w.PAGE_DIRS[0])[0]
    assert len(w.PAGE_DIRS) == 1
    ok, msg = edit.remove_folder(w.PAGE_DIRS[0], "siim")
    assert not ok and "only page folder" in msg
    assert edit.remove_folder("results", "siim", "frobnicate")[0] is False


def test_renaming_a_folder_moves_its_pages_and_updates_links(wiki):
    setup_links(wiki)
    ok, msg = edit.rename_folder("topics", "things", "siim")
    assert ok, msg
    assert (wiki / "things/printer.md").exists() and not (wiki / "topics").exists()
    assert w.PAGE_DIRS.index("things") == 2 and w.TYPES["things"] == "topic"              # same place in the list, same type
    assert "[[things/printer#Setup|the printer]]" in (wiki / "decisions/d1.md").read_text()
    assert "topic: things/printer" in (wiki / "inbox/claude-code/n1.md").read_text()
    assert w.split((wiki / "things/printer.md").read_text())[0]["type"] == "topic"
    assert log(wiki, 1)[0] == "web: siim renamed the page folder topics to things"
    changed = subprocess.run(["git", "-C", str(wiki), "show", "--name-status", "--format=", "HEAD"], capture_output=True, text=True).stdout
    assert "R" in changed.split()[0] or "things/printer.md" in changed                      # git sees a move, not a rewrite
    for bad, expect in (("Things", "already its name"), ("raw", "reserved"), ("a b", "lowercase"), ("decisions", "already exists"),
                        ("", "lowercase"), ("1x", "lowercase")):
        ok, msg = edit.rename_folder("things", bad, "siim")
        assert not ok and expect in msg, (bad, msg)
    assert edit.rename_folder("nope", "x", "siim")[0] is False
    (wiki / "stray").mkdir()
    assert "already exists" in edit.rename_folder("things", "stray", "siim")[1]               # a folder on disk that is not in the schema


def test_the_remove_page_and_rename_over_http(wiki):
    setup_links(wiki)
    assert "Move them to" in web.remove_folder_view("topics", "tok") and "2</b> page(s)" in web.remove_folder_view("topics", "tok")
    assert "decisions/d1" in web.remove_folder_view("topics", "tok")                         # who links in
    assert web.remove_folder_view("nope", "tok") is None
    (wiki / "results").mkdir(exist_ok=True)
    assert "holds no pages" in web.remove_folder_view("results", "tok")
    assert "Rename" in web.schema_view("tok") and "/_schema/remove/topics" in web.schema_view("tok")
    c = Client(wiki)
    try:
        tok = re.search(r'name="t" value="([0-9a-f]+)"', c.req("/_new")[1]).group(1)
        assert c.req("/_schema/remove/topics")[0] == 200 and c.req("/_schema/remove/nope")[0] == 404
        status, _, h = c.req("/_schema/rename", {"t": tok, "folder": "topics", "new": "things"})
        assert status == 303 and "Renamed" in urllib.parse.unquote(h["Location"]) and (wiki / "things/printer.md").exists()
        status, _, h = c.req("/_schema/remove", {"t": tok, "folder": "things", "action": "delete", "confirm_name": "wrong"})
        assert "type the folder" in urllib.parse.unquote(h["Location"]) and (wiki / "things").exists()      # needs the typed name
        status, _, h = c.req("/_schema/remove", {"t": tok, "folder": "things", "action": "move", "target": "howtos"})
        assert status == 303 and (wiki / "howtos/printer.md").exists() and "things" not in w.PAGE_DIRS
        status, _, h = c.req("/_schema/remove", {"t": "bad", "folder": "howtos", "action": "delete", "confirm_name": "howtos"})
        assert status == 403 and (wiki / "howtos").exists()                                  # no form token, no change
    finally:
        c.close()


# ---- search gaps on the Open tab

def test_gaps_on_the_open_tab_can_be_answered_or_dismissed(wiki):
    from datetime import datetime

    from just_wiki import stats
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    (wiki / "logs").mkdir(exist_ok=True)
    (wiki / "logs/usage.jsonl").write_text("".join(
        f'{{"ts": "{now}", "ip": "127.0.0.1", "ua": "x/1", "tool": "search", "outcome": "miss", "query": "{q}"}}\n'
        for q in ("toner order", "toner order", "label maker", "zqxprobe")))
    html = web.open_view(web.load(), {}, "tok")
    assert "Search gaps <span class='count'>3" in html and "toner order" in html and "searched 2x" in html
    assert "action='/_gap/answer'" in html and "Not relevant: ignore" in html
    assert "name='only' value='gaps'" in html and "Let the steward review these now" in html
    c = Client(wiki)
    try:
        tok = re.search(r'name="t" value="([0-9a-f]+)"', c.req("/_new")[1]).group(1)
        too_short = c.req("/_gap/answer", {"t": tok, "query": "toner order", "text": "black"})
        assert too_short[0] == 303 and "Not saved" in urllib.parse.unquote(too_short[2]["Location"])
        assert [g["query"] for g in stats.open_gaps()].count("toner order") == 1                         # still open
        ok = c.req("/_gap/answer", {"t": tok, "query": "toner order", "text": "Toner is ordered by facilities from the supplier."})
        assert ok[0] == 303 and "Sent to the steward as a note" in urllib.parse.unquote(ok[2]["Location"]) and ok[2]["Location"].endswith("#gaps")
        note = next((wiki / "inbox").rglob("*what-the-wiki-should-say-about-toner-order*.md"))
        meta, body = w.split(note.read_text())
        assert meta["kind"] == "finding" and meta["agent"] == web.web_user() and "facilities" in body
        assert "answer given in the web UI" in meta["sources"][0]
        assert next(g for g in stats.gaps(30) if g["query"] == "toner order")["review"]["outcome"] == "answered"
        assert "toner order" not in [g["query"] for g in stats.open_gaps()]
        assert c.req("/_gap/noise", {"t": tok, "query": "zqxprobe"})[0] == 303
        assert {g["query"] for g in stats.open_gaps()} == {"label maker"}
        assert c.req("/_gap/noise", {"t": "bad", "query": "label maker"})[0] == 403                     # no token, no change
        assert "label maker" in [g["query"] for g in stats.open_gaps()]
        assert "Search gaps <span class='count'>1" in c.req("/_open")[1]
    finally:
        c.close()


def test_the_steward_tab_can_run_one_scope(wiki):
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    (wiki / "logs").mkdir(exist_ok=True)
    (wiki / "logs/usage.jsonl").write_text(
        f'{{"ts": "{now}", "ip": "127.0.0.1", "ua": "x/1", "tool": "search", "outcome": "miss", "query": "toner order"}}\n')
    html = web.steward_view("pw")
    assert "Search gaps <span class='count'>1</span>" in html and "value='gaps'" in html and "Run only this" in html


def test_preview_shows_the_text_without_saving_and_runs_nothing(wiki):
    c = Client(wiki)
    try:
        status, html, _ = c.req("/_edit/topics/printer")
        tok = re.search(r'name="t" value="([0-9a-f]+)"', html).group(1)
        base = re.search(r'name="base" value="([0-9a-f]{64})"', html).group(1)
        body = "# Printer\n\nSee **room 6** and [[topics/scanner]].\n\n<!-- hidden -->\n<script>alert(9)</script>\n"
        status, html, _ = c.req("/_edit/topics/printer", {**fields(body=body), "t": tok, "base": base, "do": "preview"})
        assert status == 200 and "Preview: not saved yet" in html and "<strong>room 6</strong>" in html
        assert 'href="/topics/scanner"' in html                                          # links work in the preview
        assert "<script>alert(9)" not in html and "&lt;script&gt;alert(9)" in html        # raw HTML is shown as text
        assert "hidden" not in html.split("class='preview'")[1].split("</form>")[0].split("<form")[0]   # comments are dropped
        assert f'name="base" value="{base}"' in html and "<textarea" in html and "alert(9)" in html      # the form keeps the draft
        assert (wiki / "topics/printer.md").read_text() == PAGE                           # nothing was written
        status, html, _ = c.req("/_new", {"t": tok, "folder": "topics", "slug": "x", "title": "Toner", "summary": "", "status": "draft",
                                          "sources": "", "body": "**bold**", "do": "preview"})
        assert status == 200 and "<strong>bold</strong>" in html and not (wiki / "topics/x.md").exists()
    finally:
        c.close()


def test_the_page_view_has_edit_and_remove_buttons_in_the_header(wiki):
    body = web.page_view("topics/printer", web.load())
    assert "<div class='pagehead'><h1" in body and "class='btn alt sm' href='/_edit/topics/printer'" in body
    status_page = web.page_view("status", web.load())
    assert "/_edit/status" in status_page and "/_delete/status" not in status_page         # status can be edited, not removed


def test_editors_have_no_page_list_but_the_page_view_has(wiki):
    from just_wiki import web as webmod
    pages = webmod.load()
    editor = webmod.shell("/", "topics/printer", "Edit", "x", pages, side=False)
    assert "<nav>" not in editor and "wrap wide" in editor
    assert "<nav>" in webmod.shell("/", "topics/printer", "Page", "x", pages)               # the default for Pages keeps it
    assert 'class="here">Pages' in editor or "class=here" in editor                        # the Pages tab stays highlighted
    assert webmod.notice("Saved.").count("notice ok") == 1 and "notice bad" in webmod.notice("Not saved: x")
