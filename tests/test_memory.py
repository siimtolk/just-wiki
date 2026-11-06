"""The optional memory feature: sources, patterns, rule rewrite, fix, backups, restore, facts, audit, report."""
import json
import subprocess
import sys

import pytest

from just_wiki import lint, memory, setup, web
from just_wiki import store as w
from just_wiki.memory import audit, facts, fix, report, sources, state
from just_wiki.memory.patterns import Patterns, has_pointer

PAGE = """---
title: Example service
type: topic
status: verified
summary: A demo page.
sources: [x]
updated: 2026-01-01
updated_by: test
---

# Example service

The web view listens on 192.0.2.5:8094 and the unit `demo-web.timer` starts it; files live in /srv/demo/data.
"""


@pytest.fixture
def mem(tmp_path, monkeypatch):
    """A wiki with [memory] on and three kinds of memory: Claude Code files, Hermes entries, a markdown folder."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "nogitconfig"))
    setup.init(tmp_path / "wiki")
    (tmp_path / "wiki/topics").mkdir(exist_ok=True)
    (tmp_path / "wiki/topics/demo.md").write_text(PAGE)
    claude = tmp_path / ".claude/projects/-srv-app/memory"
    claude.mkdir(parents=True)
    (claude / "MEMORY.md").write_text("- [index](a.md)\n")
    (claude / "a.md").write_text("---\nname: a\n---\nUse the dashboard at 192.0.2.5:8094 for status.\nAsk before restarting.\n")
    (claude / "b.md").write_text("Prefer short answers.\n")
    hermes = tmp_path / "hermes/memories"
    hermes.mkdir(parents=True)
    (hermes / "MEMORY.md").write_text("Keep replies short.\n§\nThe web view is on 192.0.2.5:8094 (restart demo-web.timer).\n§\nNo emojis.")
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "n.md").write_text("Data is in /srv/demo/data.\n")
    toml = tmp_path / "wiki/wiki.toml"
    toml.write_text(toml.read_text() + f'''
[memory]
enabled = true
mode = "fix"
presets = ["network", "paths"]
path_prefixes = ["/srv/"]
backup_dir = "{tmp_path / 'backups'}"
[[memory.sources]]
agent = "claude"
kind = "claude-code"
path = "{tmp_path}/.claude/projects/*/memory"
[[memory.sources]]
agent = "hermes"
kind = "hermes"
path = "{hermes}"
[[memory.sources]]
agent = "other"
kind = "markdown-dir"
path = "{notes}"
''')
    w._cfg["root"] = None
    state.reset()
    return tmp_path


# ---- patterns

def test_patterns_find_whole_values_and_keep_punctuation(mem):
    p = state.patterns()
    toks = p.tokens("See http://192.0.2.5:8094/x/y. Then /srv/demo/data, and port 8093!")
    assert [t.text for t in toks] == ["http://192.0.2.5:8094/x/y", "/srv/demo/data", "port 8093"]
    assert p.sub("data in /srv/demo/data.", lambda t: "(see wiki)") == "data in (see wiki)."
    assert p.fact("open 192.0.2.5 for the wiki")                    # the bare word wiki is no pointer
    assert p.fact("open (see wiki topics/demo) at 192.0.2.5") is None
    assert p.fact(f"the wiki is in {w.ROOT}/systems") is None      # the wiki's own folder is no fact
    assert has_pointer("[[topics/demo]]") and not has_pointer("the wiki")


def test_user_patterns_and_models_preset(mem):
    toml = mem / "wiki/wiki.toml"
    toml.write_text(toml.read_text() + '\n[[memory.patterns]]\nname = "ticket host"\nregex = "tickets\\\\.example\\\\.org/\\\\S*"\nkind = "url"\n')
    toml.write_text(toml.read_text().replace('presets = ["network", "paths"]', 'presets = ["network", "paths", "models"]\nmodel_names = ["qwen"]'))
    w._cfg["root"] = None
    p = Patterns(state.cfg())
    assert [t.kind for t in p.tokens("Qwen3.8-27B at tickets.example.org/x.")] == ["model", "url"]
    bad = mem / "wiki/wiki.toml"
    bad.write_text(bad.read_text() + '\n[[memory.patterns]]\nname = "broken"\nregex = "("\n')
    w._cfg["root"] = None
    with pytest.raises(SystemExit):
        Patterns(state.cfg())


# ---- sources

def test_sources_units_documents_and_atomic_replace(mem):
    srcs = {s.agent: s for s in fix.all_sources()}
    claude = srcs["claude"].units()
    assert {u.text for u in claude} >= {"Use the dashboard at 192.0.2.5:8094 for status.", "Prefer short answers."}
    assert all(u.path.name != "MEMORY.md" for u in claude)             # the index is skipped
    assert "name: a" not in "".join(u.text for u in claude)             # frontmatter too
    hermes = srcs["hermes"].units()
    assert len(hermes) == 3 and hermes[1].where == "hermes MEMORY.md"
    assert len(srcs["hermes"].documents()) == 3 and srcs["claude"].documents()[0].text.startswith("Use the dashboard")
    u = hermes[1]
    assert srcs["hermes"].replace(u, "changed") is True
    assert srcs["hermes"].replace(u, "again") is False                  # the entry is gone: nothing to replace
    assert "changed" in (mem / "hermes/memories/MEMORY.md").read_text()
    assert not list((mem / "hermes/memories").glob(".just-wiki_*"))     # no temp file left behind


# ---- rule rewrite and checks (the cases that went wrong once)

def test_rule_rewrite_values_become_pointers(mem):
    p = state.patterns()
    new = fix.rule_rewrite("Use http://192.0.2.5:8094/x or 192.0.2.5:8094 for the web view.", p, lookup=lambda v: "topics/demo")
    assert new == "Use (see wiki topics/demo) for the web view."      # neighbours merge into one pointer
    assert fix.rule_rewrite("Pushed to /srv/demo/data.", p, lookup=lambda v: "topics/demo") == "Pushed to (see wiki topics/demo)."
    assert fix.rule_rewrite("Nothing here", p) is None
    assert fix.rule_rewrite("Check /srv/a and /srv/b first", p, lookup=lambda v: "topics/demo") == "Check (see wiki topics/demo) first"
    assert fix.rule_rewrite("Run `192.0.2.5` now", p, lookup=lambda v: "topics/demo") == "Run (see wiki topics/demo) now"   # no code quotes
    assert fix.rule_rewrite("see 192.0.2.5", p, lookup=lambda v: "topics/nope") is None   # the pointer's page must exist


def test_valid_rejects_lost_instructions_and_extras(mem):
    p = state.patterns()
    old = "Never restart the service on 192.0.2.5 without asking first"
    assert fix.valid(old, "(see wiki topics/demo)", p).startswith("dropped")
    assert fix.valid(old, old, p).startswith("fact left")
    assert fix.valid(old, "Never restart the service on (see wiki topics/demo) without asking first", p) == ""
    assert fix.valid(old, "Never restart the service without asking first", p) == "no wiki pointer"
    assert fix.valid("a 192.0.2.5", "a (see wiki topics/demo)\nextra line", p).startswith("more lines")


def test_a_model_only_helps_with_preset_values_not_listed_ones(mem, monkeypatch):
    p = state.patterns()
    monkeypatch.setattr(fix, "ask_model", lambda prompt, max_tokens=800: "Total nonsense without a pointer")
    new, why = fix.rewrite("Odd 192.0.2.5 thing", p, "topics/demo")
    assert new is not None and why == "rule"                            # the rule handles it, no model
    monkeypatch.setattr(fix, "rule_rewrite", lambda *a, **k: None)
    new, why = fix.rewrite("Use 192.0.2.5 now", p, "topics/demo")
    assert new is None and why.startswith("model:")                    # the model's answer fails the checks
    monkeypatch.setattr(fix, "ask_model", lambda prompt, max_tokens=800: "Use (see wiki topics/demo) now")
    assert fix.rewrite("Use 192.0.2.5 now", p, "topics/demo") == ("Use (see wiki topics/demo) now", "model")


# ---- scan, fix, restore

def test_scan_fix_log_backup_and_restore(mem):
    hits = fix.scan()
    assert len(hits) == 3 and any(h["where"] == "hermes MEMORY.md" for h in hits)
    lines = fix.lint_lines()
    assert any("memory drift: hermes MEMORY.md" in x for x in lines)
    out = []
    assert fix.run(apply=True, out=out.append) == 0
    assert "fixed 3" in out[-1].replace(",", "") or "fixed 3" in out[-1]
    assert "192.0.2.5" not in (mem / "hermes/memories/MEMORY.md").read_text()
    assert "(see wiki topics/demo)" in (mem / "hermes/memories/MEMORY.md").read_text()
    assert "Ask before restarting." in (mem / ".claude/projects/-srv-app/memory/a.md").read_text()   # other lines untouched
    rows = state.rows()
    assert len(rows) == 3 and rows[0]["by"] == "rule" and rows[0]["replaced"] and rows[0]["before"]
    assert fix.scan() == [] and fix.lint_lines() == []
    log = (mem / "wiki/log.md").read_text()
    assert "memory | fixed hermes MEMORY.md" in log
    # the same value back again counts as a repeat
    (mem / "notes/n.md").write_text("Data is in /srv/demo/data.\n")
    fix.run(apply=True, out=lambda x: None)
    assert state.rows()[-1]["repeat"] == 1
    # undo: the day's first copy comes back, the current one is kept beside it
    assert fix.restore(out=lambda x: None) == 0
    assert "192.0.2.5" not in (mem / "hermes/memories/MEMORY.md").read_text()                       # dry: nothing yet
    assert fix.restore(yes=True, out=lambda x: None) == 0
    assert "192.0.2.5:8094" in (mem / "hermes/memories/MEMORY.md").read_text()
    assert list((mem / "backups").glob("*/*.before-restore"))


def test_report_mode_never_writes_and_the_cap_stops_a_runaway(mem):
    toml = mem / "wiki/wiki.toml"
    toml.write_text(toml.read_text().replace('mode = "fix"', 'mode = "report"'))
    w._cfg["root"] = None
    before = (mem / "hermes/memories/MEMORY.md").read_text()
    assert memory.main(["run"]) == 0
    assert (mem / "hermes/memories/MEMORY.md").read_text() == before and state.rows() == []
    toml.write_text(toml.read_text().replace('mode = "report"', 'mode = "fix"\nmax_fixes = 1'))
    w._cfg["root"] = None
    out = []
    fix.run(True, out.append)
    assert any("stopping after 1 fixes" in x for x in out) and len(state.rows()) == 1


def test_failed_rewrite_is_logged_once_a_day_and_clears_on_a_later_fix(mem, monkeypatch):
    monkeypatch.setattr(fix, "rewrite", lambda e, p, pg: (None, "model: fact left"))
    fix.run(True, out=lambda x: None)
    n = len(state.rows())
    fix.run(True, out=lambda x: None)
    assert len(state.rows()) == n and all(r.get("failed") for r in state.rows())      # logged once, not every sweep
    monkeypatch.undo()
    fix.run(True, out=lambda x: None)
    assert "memory corrections: 3" in report.line()
    assert "stuck" not in report.line()                                               # the fixes cleared the failures


def test_stale_status_lines_are_reported_not_rewritten(mem):
    (mem / "notes/n.md").write_text("Listonic is CONNECTED (2026-09-01) and fine.\n")
    assert any("dated status" in x for x in fix.lint_lines())
    fix.run(True, out=lambda x: None)
    assert "CONNECTED (2026-09-01)" in (mem / "notes/n.md").read_text()


# ---- facts and audit

class FakeLLM:
    def __init__(self, c):
        pass

    def chat(self, system, user, kind="edit"):
        if "memory entry" in user.lower():
            return '{"verdict": "contradicts", "reason": "the page says otherwise"}'
        return json.dumps([{"value": "demo-web.timer", "kind": "unit"}, {"value": "/todo", "kind": "unit"},
                           {"value": "submit_note", "kind": "unit"}, {"value": "not in the page", "kind": "job"},
                           {"value": "demo-job", "kind": "script"}, {"value": "/srv/demo/data", "kind": "host_path"}])


def llm_on(mem, monkeypatch):
    toml = mem / "wiki/wiki.toml"
    toml.write_text(toml.read_text() + '\n[memory.llm]\nbase_url = "http://127.0.0.1:1/v1"\nmodel = "m"\n')
    w._cfg["root"] = None
    import just_wiki.pipeline as pl
    monkeypatch.setattr(pl, "LLM", FakeLLM)


def test_facts_filter_pending_review_and_retirement(mem, monkeypatch):
    llm_on(mem, monkeypatch)
    assert facts.deny_list() == set()
    (mem / "wiki/memory-deny.txt").write_text("# comment\n")
    out = []
    facts.build(out=out.append)
    f = state.read_facts()
    assert list(f["values"]) == ["demo-web.timer"]              # the command, tool name, absent text, script and preset path are out
    assert f["values"]["demo-web.timer"]["page"] == "topics/demo" and "1 new" in out[-1]
    assert state.active_facts() == {}                           # pending: active from tomorrow
    assert state.active_facts(include_pending=True) == {"demo-web.timer": "topics/demo"}
    f["values"]["demo-web.timer"]["first_seen"] = "2026-01-01"
    state.facts_file().write_text(json.dumps(f))
    assert state.active_facts() == {"demo-web.timer": "topics/demo"}
    p = state.patterns()
    assert [t.text for t in p.tokens("restart `demo-web.timer` and demo-web.timer.bak; /demo-web.timer")] == ["demo-web.timer"]
    (mem / "wiki/memory-deny.txt").write_text("demo-web.timer\n")        # a person says: keep this in memories
    state.reset()
    facts.build(out=lambda x: None)
    assert state.read_facts()["values"] == {}
    # a value that is no longer anywhere in the wiki is retired, and the memory that still holds it is stale
    (mem / "wiki/memory-deny.txt").write_text("")
    f["values"] = {"gone-unit.timer": {"page": "topics/demo", "kind": "unit", "first_seen": "2026-01-01"}}
    state.facts_file().write_text(json.dumps(f))
    (mem / "notes/n.md").write_text("Restart gone-unit.timer first.\n")
    facts.build(out=lambda x: None)
    assert "gone-unit.timer" in state.read_facts()["retired"]
    findings = audit.run(out=lambda x: None)
    assert any("gone-unit.timer" in x["reason"] and x["verdict"] == "stale" for x in findings)


def test_audit_asks_the_model_and_caches(mem, monkeypatch):
    llm_on(mem, monkeypatch)
    (mem / "notes/n.md").write_text("The example service web view listens somewhere and the unit starts it with files.\n")
    calls = []
    monkeypatch.setattr(FakeLLM, "chat", lambda self, s, u, kind="edit": calls.append(1) or '{"verdict": "contradicts", "reason": "r"}')
    first = audit.run(out=lambda x: None)
    n = len(calls)
    assert n >= 1 and any(f["verdict"] == "contradicts" for f in first)
    audit.run(out=lambda x: None)
    assert len(calls) == n                                       # unchanged entries come from the cache


def test_no_llm_means_facts_do_nothing_and_audit_only_checks_retired(mem):
    out = []
    assert facts.build(out=out.append) is None and "no LLM" in out[0]
    assert audit.run(out=lambda x: None) == []


# ---- surfaces: lint, report, web tab, CLI, units, connect, nightly

def test_lint_report_and_the_memory_tab(mem):
    errors, warnings = lint.check(False)
    assert any("memory drift" in x for x in warnings)
    p = report.build()
    heads = [s["heading"] for s in p["sections"]]
    assert "Overview" in heads and "Memories and their wiki links" in heads and "Candidates for standing rules" in heads
    json.dumps(p)
    assert "holds a fact now" in json.dumps(p)
    assert "memory" in "".join(t for _, t in web.all_tabs()).lower()
    html = web.memory_view()                                   # what the /_memory route serves
    assert "<h1>Agent memory</h1>" in html and "holds a fact now" in html


def test_candidates_need_repeats(mem):
    row = {"at": "2026-11-05 10:00", "where": "hermes MEMORY.md", "values": ["192.0.2.5"], "by": "rule", "repeat": 0,
           "replaced": [{"value": "192.0.2.5:8094", "kind": "address", "page": "topics/demo"}]}
    assert report.candidates([row], "") == []
    c = report.candidates([row, row], "")
    assert c and "192.0.2.5:8094" in c[0]["title"] and c[0]["tag"] == "new"
    assert report.candidates([row, row], "never store 192.0.2.5:8094")[0]["tag"] == "already in the rules"
    c3 = report.candidates([row] * 3, "")
    assert any("keeps saving" in x["title"] for x in c3)


def test_cli_commands_and_off_by_default(mem, capsys):
    assert memory.main(["status"]) == 0 and "mode fix" in capsys.readouterr().out
    assert memory.main(["scan"]) == 1 and "3 unit(s)" in capsys.readouterr().out
    assert memory.main(["report"]) == 0 and "Agent memory" in capsys.readouterr().out
    assert memory.main(["candidates"]) == 0 and "none yet" in capsys.readouterr().out
    assert memory.main(["nightly"]) == 0 and state.rows()                 # nightly ran the sweep
    assert memory.main(["bogus"]) == 2
    toml = mem / "wiki/wiki.toml"
    toml.write_text(toml.read_text().replace("enabled = true\nmode", "enabled = false\nmode"))
    w._cfg["root"] = None
    assert memory.main(["run"]) == 0 and memory.main(["nightly"]) == 0     # off: timers and the nightly run do nothing
    assert not lint.check(False)[1] or all("memory" not in x for x in lint.check(False)[1])
    assert not any(t[0] == "/_memory" for t in web.all_tabs())


def test_units_and_connect_add_the_memory_pieces(mem, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["just-wiki"])
    setup.units()
    out = capsys.readouterr().out
    assert "just-wiki-memory.timer" in out and "OnCalendar=*:0/15" in out and "memory run" in out
    assert f"PathChanged={mem}/hermes/memories" in out and f"PathChanged={mem}/.claude/projects/-srv-app/memory" in out
    toml = mem / "wiki/wiki.toml"
    toml.write_text(toml.read_text().split("[memory]")[0])
    w._cfg["root"] = None
    setup.add_memory_source("hermes", "hermes", "/x/memories")
    setup.add_memory_source("hermes", "hermes", "/x/memories")           # twice: no duplicate
    text = toml.read_text()
    assert text.count("[memory]") == 1 and text.count("[[memory.sources]]") == 1 and 'mode = "report"' in text
    w._cfg["root"] = None
    assert state.enabled() and state.cfg()["mode"] == "report" and sources.load(state.cfg())[0].kind == "hermes"


def test_the_whole_thing_runs_from_the_installed_command(mem):
    r = subprocess.run([sys.executable, "-m", "just_wiki.cli", "--root", str(mem / "wiki"), "memory", "scan"],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "unit(s) hold a setup fact" in r.stdout


def test_the_nightly_run_includes_memory_unless_switched_off(mem, monkeypatch):
    from just_wiki import steward
    calls = []
    monkeypatch.setattr(memory, "nightly", lambda out=print: calls.append(1) or 0)
    monkeypatch.setattr(steward, "waiting", lambda: {})
    monkeypatch.setattr(steward, "run_agent", lambda *a, **k: 0)
    steward.nightly(out=lambda x: None)
    assert calls == [1]
    toml = mem / "wiki/wiki.toml"
    toml.write_text(toml.read_text().replace("enabled = true\nmode", "enabled = true\nnightly = false\nmode"))
    w._cfg["root"] = None
    steward.nightly(out=lambda x: None)
    assert calls == [1]                                      # nightly = false: the host schedules `memory nightly` itself


def test_fallback_command_gets_the_prompt_and_its_stdout_is_the_answer(mem):
    toml = mem / "wiki/wiki.toml"
    toml.write_text(toml.read_text().replace('mode = "fix"', f'mode = "fix"\nfallback_command = ["{sys.executable}", "-c", "import sys; print(sys.argv[1][:12].upper())", "{{prompt}}"]'))
    w._cfg["root"] = None
    assert fix.ask_model("hello fallback world").strip() == "HELLO FALLBA"
    toml.write_text(toml.read_text().replace('"-c", "import sys', '"-c", "import sys; sys.exit(3); #'))
    w._cfg["root"] = None
    assert fix.ask_model("x") is None                       # a failing command is no answer


def test_connect_memory_adds_the_source_even_though_the_template_mentions_the_path(tmp_path, monkeypatch, capsys):
    """The wiki.toml template has the Claude Code path in a commented example: that is not a configured source."""
    monkeypatch.setenv("HOME", str(tmp_path))
    setup.init(tmp_path / "wiki")
    assert "# path = \"~/.claude/projects/*/memory\"" in (tmp_path / "wiki/wiki.toml").read_text()
    setup.add_memory_source("claude", "claude-code", "~/.claude/projects/*/memory")
    setup.add_memory_source("claude", "claude-code", "~/.claude/projects/*/memory")       # twice: still one
    import tomllib
    cfg = tomllib.loads((tmp_path / "wiki/wiki.toml").read_text())
    assert [s["path"] for s in cfg["memory"]["sources"]] == ["~/.claude/projects/*/memory"] and cfg["memory"]["enabled"] is True
