import json
import subprocess
import sys

import pytest

from just_wiki import brief, cli, lint, live, mcp_server, notes, setup, steward
from just_wiki import store as w

PAGE = """---
title: Example service
type: system
status: verified
summary: A demo page.
sources: [{src}]
updated: {today}
updated_by: test
---

# Example service

See [[status]].

<!-- brief -->
- **Example:** answers on port 1234.
<!-- /brief -->

<!-- live:up -->
<!-- /live:up -->
"""


def setup_layout(root):
    """These tests use the setup-wiki folders (systems, projects, …): the layout of a wiki.toml without [pages]."""
    toml = (root / "wiki.toml").read_text()
    (root / "wiki.toml").write_text(toml[:toml.index("\n[pages]")] + "\n")
    w.set_root(root)
    for d in w.PAGE_DIRS:
        (root / d).mkdir(exist_ok=True)


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    from datetime import date
    root = tmp_path / "wiki"
    setup.init(str(root))
    setup_layout(root)
    src = tmp_path / "source.txt"
    src.write_text("x")
    (root / "systems/example.md").write_text(PAGE.format(src=src, today=date.today()))
    with open(root / "wiki.toml", "a") as f:
        f.write(f'\n[live.blocks]\nup = ["{sys.executable}", "-c", "print(\'host is up\')"]\n')
    toml = (root / "wiki.toml").read_text().replace("[live.blocks]\n# <!-- live:uptime -->", "# <!-- live:uptime -->")
    (root / "wiki.toml").write_text(toml)
    w.set_root(root)
    monkeypatch.chdir(root)
    return root


def test_init_layout(wiki):
    for name in ("AGENTS.md", "status.md", "wiki.toml", "index.md", "log.md", "STEWARD.md"):
        assert (wiki / name).exists(), name
    assert (wiki / "CLAUDE.md").is_symlink()


def test_lint_clean_then_broken_link(wiki):
    lines = []
    assert lint.run(fix=True, out=lines.append) == 0
    p = wiki / "systems/example.md"
    p.write_text(p.read_text() + "\nSee [[systems/nope]].\n")
    lines = []
    assert lint.run(out=lines.append) == 1
    assert any("broken link [[systems/nope]]" in x for x in lines)


def test_lint_missing_source(wiki):
    p = wiki / "systems/example.md"
    p.write_text(p.read_text().replace("sources: [", "sources: [/no/such/file, "))
    errors, _ = lint.check(fix=False)
    assert any("source path no longer exists: /no/such/file" in x for x in errors)


def test_lint_extra_check(wiki):
    with open(wiki / "wiki.toml", "a") as f:
        f.write(f'\n[lint]\nchecks = [["{sys.executable}", "-c", "print(\'WARNING custom thing\')"]]\n')
    toml = (wiki / "wiki.toml").read_text()
    toml = toml.replace("[lint]\nstale_days = 60\nchecks = []", "[lintx]\nstale_days = 60")
    (wiki / "wiki.toml").write_text(toml)
    w.set_root(wiki)
    _, warnings = lint.check(fix=False)
    assert "custom thing" in warnings


def test_live_and_check(wiki):
    assert live.run(out=lambda s: None) == 0
    text = (wiki / "systems/example.md").read_text()
    assert "host is up" in text
    assert live.run(check=True, out=lambda s: None) == 0


def test_brief_targets_and_hand_edit(wiki, tmp_path):
    tgt = tmp_path / "CLAUDE.md"
    tgt.write_text("# mine\n\n<!-- brief -->\nold\n<!-- /brief -->\ntail\n")
    with open(wiki / "wiki.toml", "a") as f:
        f.write(f'\n[[brief.targets]]\npath = "{tgt}"\nmode = "block"\n')
    w.set_root(wiki)
    assert brief.run(out=lambda s: None) == 0
    t = tgt.read_text()
    assert t.startswith("# mine") and t.endswith("tail\n") and "answers on port 1234" in t and "old" not in t
    assert brief.run(check=True, out=lambda s: None) == 0
    (wiki / "brief.md").write_text("edited")
    errors, _ = lint.check(fix=False)
    assert any("edited by hand" in x for x in errors)


def test_notes_validation(wiki):
    ok, msg = notes.submit("x", "finding", "t", "b", [])
    assert not ok and msg.startswith("REJECTED")
    ok, msg = notes.submit("tester", "finding", "A good title", "A body long enough to pass.", ["/etc/hostname"],
                           "systems/example")
    assert ok and (wiki / "inbox/tester").is_dir()
    ok, msg = notes.submit("tester", "finding", "Leaky note", "api_key = abcdefghijklmnop1234", ["x"])
    assert not ok and "secret" in msg


def test_mcp_tools(wiki):
    lint.build_index()
    assert "systems/example" in mcp_server.get_index()
    assert "systems/example" in mcp_server.search("example service")
    assert mcp_server.read_page("systems/nope").startswith("No page")
    assert "Example" in mcp_server.read_page("systems/example", section="Example service")
    mcp = mcp_server.build()
    assert mcp is not None
    import asyncio
    out = asyncio.run(mcp.call_tool("read_page", {"page": "systems/example"}))
    assert [type(x).__name__ for x in out] == ["TextContent"]      # no structured copy: it doubled every result
    assert mcp_server.read_page(id="systems/example") == mcp_server.read_page("systems/example")   # aliases
    assert "Example" in mcp_server.search_text("example service")                              # focused default


def test_open_questions(wiki):
    s = wiki / "status.md"
    s.write_text(s.read_text() + "| Pick a name | siim |\n| Other thing | hermes |\n")
    rows = steward.open_questions()
    assert [r["owner"] for r in rows] == ["siim", "hermes"]


def test_steward_runs_configured_command(wiki):
    toml = (wiki / "wiki.toml").read_text().replace(
        "[steward]\n", '[steward]\ncommand = ["sh", "-c", "echo \'{\\"result\\": \\"done\\"}\'"]\n', 1)
    (wiki / "wiki.toml").write_text(toml)
    w.set_root(wiki)
    assert steward.steward_once(force=True, out=lambda s: None) == 0
    assert (wiki / ".claude/skills/wiki-steward/SKILL.md").exists()
    assert json.loads((wiki / "logs/steward-run.json").read_text())["rc"] == 0


def test_web_views(wiki):
    from just_wiki import web
    pages = web.load()
    assert "Example service" in web.page_view("systems/example", pages)
    assert "<h1>Changes" in web.changes_view({}, pages)
    assert "<h1>Open" in web.open_view(pages, {}, "t")
    assert "Run the steward now" in web.steward_view("pw")
    assert "<h1>Schedule" in web.schedule_view()
    assert "Home" in web.shell("/", "", "Home", "x", pages) or "<header>" in web.shell("/", "", "Home", "x", pages)


def test_cli_log_and_root(wiki, capsys):
    assert cli.main(["log", "test", "hello", "world"]) == 0
    assert "test | hello world" in (wiki / "log.md").read_text()
    assert cli.main(["root"]) == 0
    assert str(wiki) in capsys.readouterr().out


def test_readlog_hook(wiki):
    from just_wiki import readlog
    assert readlog.log_read(str(wiki / "systems/example.md"), wiki)
    assert not readlog.log_read(str(wiki / "inbox/x.md"), wiki)
    rec = json.loads((wiki / "logs/usage.jsonl").read_text().splitlines()[-1])
    assert rec["page"] == "systems/example"


def test_cli_help():
    r = subprocess.run([sys.executable, "-m", "just_wiki.cli", "help"], capture_output=True, text=True)
    assert "just-wiki init" in r.stdout


def _status_with(wiki, *rows):
    s = wiki / "status.md"
    s.write_text(s.read_text() + "".join(f"| {q} | {o} |\n" for q, o in rows))


def test_questions_ids_kinds_and_steward(wiki):
    from datetime import date, timedelta
    from just_wiki import questions
    toml = (wiki / "wiki.toml").read_text().replace('owner = ""', 'owner = "siim"')
    (wiki / "wiki.toml").write_text(toml)
    w.set_root(wiki)
    past, future = date.today() - timedelta(days=1), date.today() + timedelta(days=3)
    _status_with(wiki, ("Pick a name", "siim"), ("What listens on 27500?", "hermes"),
                 (f"**On {past}:** review the test", "claude-code / siim"), (f"**On {future}:** later", "siim"),
                 (f"Unknown thing (checked {date.today()}: no access)", "?"))
    rows = questions.rows()
    assert [q["kind"] for q in rows] == ["mine", "agent", "reminder", "reminder", "agent"]
    assert rows[0]["id"] == questions.qid("Pick a name")
    assert [questions.needs_steward(q) for q in rows] == [False, True, True, False, False]
    assert steward.waiting()["questions"] == 2


def test_answer_note_and_web_post(wiki):
    from just_wiki import questions, web
    _status_with(wiki, ("Pick a name", "siim"))
    q = questions.rows()[0]
    ok, _ = questions.answer_note(q, "decide", "", "siim")
    assert not ok
    ok, _ = questions.answer_note(q, "postpone", "soon", "siim")
    assert not ok
    ok, msg = questions.answer_note(q, "decide", "Call it just-wiki", "siim")
    assert ok, msg
    a = questions.answers()
    assert a[q["id"]][0]["action"] == "decide"
    pages = web.load()
    html = web.open_view(pages, {}, "tok")
    assert "Answered, waiting for the steward" in html and f"q-{q['id']}" in html and "Call it just-wiki" in html
    assert "Waiting for you <span class='count'>0" in html
    assert steward.count_open_questions() == 0
    items = web.plan_items(pages)
    assert any(q["id"] in link for _, link in items["questions"])


def test_web_answer_runs_on_decision(wiki, tmp_path):
    from just_wiki import questions, web
    out = tmp_path / "hook.txt"
    toml = (wiki / "wiki.toml").read_text()
    toml = "\n".join(f'on_decision = ["sh", "-c", "echo \\"$0 $1\\" > {out}; echo handed over"]'
                     if line.startswith("on_decision") else line for line in toml.splitlines())
    (wiki / "wiki.toml").write_text(toml)
    w.set_root(wiki)
    _status_with(wiki, ("Pick a name", "siim"))
    i = questions.rows()[0]["id"]
    msg = web.answer(i, {"action": ["decide"], "option": ["just-wiki"], "text": [""]}, "siim")
    assert "handed over" in msg
    assert out.read_text().strip() == f"{i} just-wiki"
    assert "no longer" in web.answer("abcdef", {"action": ["drop"]}, "siim")


def test_steward_scope_in_command(wiki):
    cmd = 'command = ["sh", "-c", "echo \\"{\\\\\\"result\\\\\\": \\\\\\"$0\\\\\\"}\\"", "/wiki-steward{scope}"]'
    toml = (wiki / "wiki.toml").read_text().replace("[steward]\n", f"[steward]\n{cmd}\n", 1)
    (wiki / "wiki.toml").write_text(toml)
    w.set_root(wiki)
    assert steward.scope_arg(["questions"]) == " only: questions"
    assert steward.scope_arg(list(steward.SCOPES)) == ""
    assert steward.steward_once(force=True, only=["questions"], out=lambda s: None) == 0
    run = json.loads((wiki / "logs/steward-run.json").read_text())
    assert run["scope"] == "only: questions"
    from datetime import date
    assert "only: questions" in next((wiki / "logs").glob(f"steward-{date.today()}-*.json")).read_text()


def test_id_survives_check_mark():
    from just_wiki import questions
    base = "**On 2026-11-09:** delete the symlinks (see `x`)."
    assert questions.qid(base) == questions.qid(base + " (checked 2026-11-02: too early (no logs yet); recheck)")


def test_questions_scope_runs_for_an_answer(wiki):
    from just_wiki import questions
    _status_with(wiki, ("Pick a name", "siim"))
    questions.answer_note(questions.rows()[0], "decide", "Call it just-wiki", "siim")
    toml = (wiki / "wiki.toml").read_text().replace(
        "[steward]\n", '[steward]\ncommand = ["sh", "-c", "echo \'{\\"result\\": \\"ok\\"}\'"]\n', 1)
    (wiki / "wiki.toml").write_text(toml)
    w.set_root(wiki)
    wait = steward.waiting()
    assert (wait["answers"], wait["notes"]) == (1, 0)
    lines = []
    steward.steward_once(only=["questions"], out=lines.append)
    assert "running the steward" in lines[0]
    lines = []
    steward.steward_once(only=["notes"], out=lines.append)
    assert lines[0].startswith("nothing waiting")


def test_question_card_from_explain_command(wiki, tmp_path):
    import time as _t
    from just_wiki import questions, web
    script = tmp_path / "explain.sh"
    script.write_text('#!/bin/sh\nif [ "$1" = "--cached" ]; then\n  [ -f "$0.done" ] && echo \'{"title": "Name it", '
                      '"what": "Pick a name.", "why": "It is shared.", "options": [{"answer": "just-wiki", '
                      '"effect": "short"}, "other"]}\' || echo "{}"\nelse\n  touch "$0.done"\nfi\n')
    script.chmod(0o755)
    toml = "\n".join(f'explain_command = ["{script}"]' if line.startswith("explain_command") else line
                     for line in (wiki / "wiki.toml").read_text().splitlines())
    (wiki / "wiki.toml").write_text(toml)
    w.set_root(wiki)
    _status_with(wiki, ("Pick a name", "siim"))
    q = questions.rows()[0]
    pages = web.load()
    assert "being written" in web.question_card(q, {}, pages, "t")
    for _ in range(50):
        if (tmp_path / "explain.sh.done").exists():
            break
        _t.sleep(0.05)
    html = web.question_card(q, {}, pages, "t")
    assert "Pick a name." in html and "It is shared." in html and "value='just-wiki'" in html and "value='other'" in html


def test_withdraw_and_settled(wiki):
    from just_wiki import questions, web
    _status_with(wiki, ("Pick a name", "siim"))
    i = questions.rows()[0]["id"]
    web.answer(i, {"action": ["decide"], "text": ["just-wiki"]}, "siim")
    assert i in questions.answers()
    assert "Withdrew 1" in web.answer(i, {"action": ["withdraw"]}, "siim")
    assert i not in questions.answers()
    assert "Nothing to withdraw" in web.answer(i, {"action": ["withdraw"]}, "siim")
    w.append_log("steward", f"decide q-{i} → status: removed as decided")
    done = questions.settled()
    assert done[0]["id"] == i and done[0]["action"] == "decide"
    assert "Steward actions on questions, last 14 days" in web.open_view(web.load(), {}, "t")


def test_new_wiki_has_generic_page_types(tmp_path, monkeypatch):
    from just_wiki import config, mcp_server
    root = tmp_path / "chat-wiki"
    setup.init(str(root))
    assert w.PAGE_DIRS == ("people", "preferences", "topics", "decisions", "howtos", "results")
    assert all((root / d).is_dir() for d in w.PAGE_DIRS) and not (root / "systems").exists()
    (root / "topics/reading-plan.md").write_text("---\ntitle: Reading plan\ntype: topic\nstatus: draft\nsummary: x.\n"
                                                  "sources: []\nupdated: 2026-01-01\nupdated_by: t\n---\n\n# Reading plan\n")
    (root / "people/sam.md").write_text("---\ntitle: Sam\ntype: topic\nstatus: draft\nsummary: x.\nsources: []\n"
                                        "updated: 2026-01-01\nupdated_by: t\n---\n\n# Sam\n")
    monkeypatch.chdir(root)
    text = "\n".join(lint.check(fix=False)[0])
    assert "people/sam: type 'topic' but folder people/ wants 'person'" in text
    assert "topics/reading-plan" not in text
    assert "people = one person" in w.folders_text() and "topics = one subject" in w.folders_text()
    assert "rg -il <words> people preferences topics decisions howtos results" in steward.prompt()
    assert "(people, preferences, topics" in mcp_server.DEFAULT_INSTRUCTIONS.format(folders=", ".join(w.PAGE_DIRS))
    assert config.load(root)["pages"]["types"] == config.GENERIC_PAGES


def test_wiki_without_pages_setting_keeps_setup_folders(wiki):
    assert w.PAGE_DIRS == ("systems", "projects", "decisions", "runbooks", "results")
    assert w.TYPES["runbooks"] == "runbook"


def test_brief_holds_the_preferences_folder(tmp_path, monkeypatch):
    from just_wiki import brief
    root = tmp_path / "chat"
    setup.init(str(root))
    monkeypatch.chdir(root)
    (root / "preferences/answers.md").write_text("---\ntitle: How Sam wants answers\ntype: preference\nstatus: draft\n"
                                                 "summary: x.\nsources: []\n---\n\n# How Sam wants answers\n\n"
                                                 "Always three bullets at most. " * 3 + "\n")
    text = brief.build()
    assert "## Preferences" in text and "### How Sam wants answers (`preferences/answers`)" in text
    assert "Always three bullets at most." in text and "# How Sam wants answers\n" not in text
    (root / "preferences/long.md").write_text("---\ntitle: Long\ntype: preference\n---\n\n" + "word " * 5000)
    assert len(brief.build()) <= 9000                         # trimmed to fit, not a failure


def test_search_finds_sections_and_search_text_returns_them_within_a_budget(wiki):
    from just_wiki import mcp_server
    (wiki / "systems/backup.md").write_text(
        "---\ntitle: Backups\ntype: system\nstatus: verified\nsummary: How the server is backed up.\nsources: []\n---\n\n"
        "# Backups\n\nNightly restic job.\n\n## Retention\n\nKeeps 14 daily snapshots.\n\n## Schedule\n\n"
        "Runs at 03:00 every night.\n\n## Restore\n\n" + "Use restic restore with the snapshot id. " * 60 + "\n")
    hits = mcp_server.search("snapshots retention")
    assert "systems/backup › Retention — Backups" in hits
    text = mcp_server.search_text("when does the backup schedule run nightly", max_tokens=500)
    assert "### Backups › Schedule (systems/backup;" in text and "Runs at 03:00" in text
    assert "Use restic restore" not in text                    # the long section doesn't fit the budget
    assert "Backups (systems/backup;" in mcp_server.search_text("nightly restic job") \
        and "How the server is backed up." in mcp_server.search_text("nightly restic job")   # page's own part + summary


def test_search_text_adds_the_disagreements_of_the_pages_it_draws_on(wiki):
    p = wiki / "systems/example.md"
    meta, body = w.split(p.read_text())
    p.write_text(w.dump(meta, "# Example service\n\n## Port\n\nThe zebra service listens on port 8094.\n\n"
                              "## Disagreements & open questions\n\nPort 8094 or 8095? The 2026-11-02 note says "
                              "8095; the 2026-10-30 note says 8094.\n"))
    out = mcp_server.search_text("zebra")
    assert "### Disagreements & open questions (systems/example)" in out and "note says 8095" in out
    assert "### Disagreements" not in mcp_server.search_text("zebra", max_tokens=2000)   # explicit: as before


def test_init_makes_a_committed_repo_with_an_identity(tmp_path, monkeypatch):
    """The steward commits: a new wiki needs a first commit and a git identity even when the user has none."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "nogitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    setup.init(tmp_path / "wiki")
    log = subprocess.run(["git", "-C", str(tmp_path / "wiki"), "log", "--format=%an %s"], capture_output=True, text=True)
    assert log.stdout.strip() == "just-wiki just-wiki init"


def test_connect_hermes_adds_the_brief_target(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    setup.init(tmp_path / "wiki")
    setup.connect(["hermes", f"--dir={tmp_path / 'hermes'}"])
    out = capsys.readouterr().out
    assert "mcp_servers" in out and "HERMES.md" in out
    assert str(tmp_path / "hermes" / "HERMES.md") in (tmp_path / "wiki" / "wiki.toml").read_text()


def test_connect_hermes_puts_the_note_rule_where_hermes_always_loads_it(tmp_path, monkeypatch, capsys):
    """With a SOUL.md the rule is a managed block (persona kept, idempotent); without one SOUL.md is not created (it would
    replace Hermes' default identity) and the rule rides on top of the brief in HERMES.md."""
    import tomllib
    monkeypatch.setenv("HOME", str(tmp_path))
    setup.init(tmp_path / "wiki")
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "SOUL.md").write_text("# Persona\nYou are Pip.\n")
    setup.connect(["hermes", f"--dir={home}"])
    setup.connect(["hermes", f"--dir={home}"])
    soul = (home / "SOUL.md").read_text()
    assert soul.startswith("# Persona\nYou are Pip.") and soul.count("submit_note(agent=\"hermes\"") == 1
    assert "in the same turn, before you reply" in soul and "none for chat, opinions or one-off errors" in soul
    cfg = tomllib.loads((tmp_path / "wiki/wiki.toml").read_text())
    targets = cfg["brief"]["targets"]
    assert [t for t in targets if t["path"].endswith("HERMES.md")] and not any("prefix" in t for t in targets)
    # no SOUL.md: the prefix target, and no SOUL.md created
    other = tmp_path / "hermes2"
    other.mkdir()
    setup.init(tmp_path / "wiki2")
    w.set_root(tmp_path / "wiki2")
    capsys.readouterr()
    setup.connect(["hermes", f"--dir={other}"])
    assert "would" in capsys.readouterr().out and not (other / "SOUL.md").exists()
    cfg = tomllib.loads((tmp_path / "wiki2/wiki.toml").read_text())
    target = next(t for t in cfg["brief"]["targets"] if t["path"].endswith("hermes2/HERMES.md"))
    assert "# Shared wiki rules" in target["prefix"] and "submit_note" in target["prefix"] and target["prefix"].endswith("\n\n")
    from just_wiki import brief
    w._cfg["root"] = None
    brief.run(out=lambda x: None)
    text = (other / "HERMES.md").read_text()
    assert text.startswith("# Shared wiki rules") and "# Brief" in text


def test_panel_html_escapes_and_draws():
    from just_wiki import web
    data = {"title": "T <b>", "sections": [
        {"heading": "H", "text": "x<script>", "stats": [{"label": "l", "value": "1", "note": "n", "tone": "bad"}],
         "bars": [{"label": "a", "value": 3}, {"label": "b", "value": 6}],
         "table": {"columns": ["c"], "rows": [[{"text": "t<i>", "href": "javascript:alert(1)", "tag": "ok"}],
                                              [[{"text": "p", "href": "/systems/wiki"}, "q"]]]},
         "items": [{"title": "i", "text": "<u>", "tag": "new"}]}]}
    out = web.panel_html(data)
    assert "<script>" not in out and "<b>" not in out.replace("<b>i</b>", "") and "javascript:" not in out
    assert "t&lt;i&gt;" in out and 'href="/systems/wiki"' in out and "width:50%" in out and "stat bad" in out
    assert "<section class='panel'>" in out and "class='tablewrap'" in out and "class='cols'" in out
    assert "title='a'" in out
    series = {"sections": [{"heading": "S", "bars_style": "columns", "bars": [{"label": "10-01", "value": 0}, {"label": "10-02", "value": 4}]}]}
    chart = web.panel_html(series)
    assert "class='colchart'" in chart and "peak 4" in chart and "height:100%" in chart and "<span>10-02</span>" in chart
    rows = web.table_html({"columns": ["a", "b"], "rows": [["short", "a text that is quite a bit longer than twenty-two characters"]]})
    assert "<td class=nowrap>short</td>" in rows and "<td>a text" in rows
    joined = web.panel_html({"sections": [{"heading": "One", "text": "x"}, {"heading": "", "bars": [{"label": "q", "value": 1}]}]})
    assert joined.count("<section class='panel'>") == 1
    assert web.safe_href("//evil.example") == "" and web.safe_href("https://x.example") == "https://x.example"


def test_panel_tab_runs_the_configured_command(tmp_path, monkeypatch):
    from just_wiki import web
    monkeypatch.setenv("HOME", str(tmp_path))
    setup.init(tmp_path / "wiki")
    script = tmp_path / "rep.py"
    script.write_text('import json; print(json.dumps({"title": "Mem", "sections": [{"heading": "Hi", "text": "ok"}]}))')
    toml = tmp_path / "wiki" / "wiki.toml"
    toml.write_text(toml.read_text() + f'\n[[web.panels]]\nname = "Agent Memory"\ncommand = ["{sys.executable}", "{script}"]\n')
    w._cfg["root"] = None                                  # re-read wiki.toml
    assert [p[:2] for p in web.panels()] == [("agent-memory", "Agent Memory")]
    assert ("/_p/agent-memory", "Agent Memory") in web.all_tabs()
    assert "<h2>Hi</h2>" in web.panel_view("agent-memory") and web.panel_view("nope") is None
    script.write_text("import sys; sys.exit(3)")
    web.PANEL_CACHE.clear()
    assert "failed (exit 3)" in web.panel_view("agent-memory")
