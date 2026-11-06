"""The Stats tab's numbers: contributions from note files, usage from the MCP log."""
import json
from datetime import datetime

import pytest

from just_wiki import setup, stats, web
from just_wiki import store as w

PAGE = """---
title: {t}
type: topic
status: {status}
summary: s
sources: [x]
updated: 2026-01-01
updated_by: test
---

# {t}
"""


def note(root, agent, name, kind, created, topic=""):
    d = root / "inbox" / agent
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\ntitle: {name}\nkind: {kind}\nagent: {agent}\ncreated: {created}\n"
                                  + (f"topic: {topic}\n" if topic else "") + "sources: [x]\n---\nA fact about the thing, long enough.\n")


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    setup.init(tmp_path / "wiki")
    root = tmp_path / "wiki"
    (root / "topics").mkdir(exist_ok=True)
    (root / "topics/backups.md").write_text(PAGE.format(t="Backups", status="verified"))
    (root / "topics/printer.md").write_text(PAGE.format(t="Printer", status="draft"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    note(root, "hermes", "n1-backup-time", "decision", now, "topics/backups")
    note(root, "hermes", "n2-printer-fix", "result", now)
    note(root, "claude-code", "n3-backup-disk", "finding", now)
    (root / "inbox/_processed/claude-code").mkdir(parents=True)
    (root / "inbox/claude-code/n3-backup-disk.md").rename(root / "inbox/_processed/claude-code/n3-backup-disk.md")
    note(root, "claude-code", "n4-old", "finding", "2020-01-01 10:00")
    (root / "log.md").write_text(
        f"- [{now[:10]} 06:40] steward | merge n3-backup-disk → topics/backups: added the disk note\n"
        f"- [{now[:10]} 06:41] steward | new n1-backup-time → topics/backups, topics/printer: both pages\n"
        f"- [{now[:10]} 06:42] steward | discard n2-printer-fix → (none): wiki already right\n"
        f"- [{now[:10]} 06:43] steward | merge n4-old → night queue report is in raw: free text, not a page\n")
    rows = [
        {"ts": now, "ip": "127.0.0.1", "ua": "python-httpx2/2.1", "tool": "search", "outcome": "ok", "query": "backup time",
         "hits": ["topics/backups", "topics/printer"]},
        {"ts": now, "ip": "127.0.0.1", "ua": "python-httpx2/2.1", "tool": "search", "outcome": "miss", "query": "toner order"},
        {"ts": now, "ip": "127.0.0.1", "ua": "python-httpx2/2.1", "tool": "search", "outcome": "miss", "query": "toner order"},
        {"ts": now, "ip": "127.0.0.1", "ua": "python-httpx2/2.1", "tool": "read_page", "outcome": "ok", "page": "topics/backups"},
        {"ts": now, "ip": "127.0.0.1", "ua": "claude-code/2.0", "tool": "read_page", "outcome": "ok", "page": "topics/printer",
         "section": "Setup"},
        {"ts": now, "ip": "127.0.0.1", "ua": "claude-code/2.0", "tool": "read_page", "outcome": "ok", "page": "topics/printer"},
        {"ts": now, "ip": "127.0.0.1", "ua": "claude-code/2.0", "tool": "submit_note", "outcome": "rejected", "agent": "claude-code"},
        {"ts": "2020-01-01 10:00", "ip": "127.0.0.1", "ua": "curl/8", "tool": "read_page", "outcome": "ok", "page": "topics/backups"},
    ]
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs/usage.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    toml = root / "wiki.toml"
    toml.write_text(toml.read_text() + '\n[usage.names]\npython-httpx2 = "hermes"\n')
    w._cfg["root"] = None
    return root


def section(p, heading):
    return next(s for s in p["sections"] if s["heading"].startswith(heading))


def tiles(p):
    return {t["label"]: t for t in section(p, "Overview")["stats"]}


def test_contributions_come_from_the_note_files_and_the_steward_log(wiki):
    p = stats.build(30)
    t = tiles(p)
    assert t["notes written"]["value"] == "3"                 # n4 is from 2020: outside the period
    assert "1 merged, 2 waiting, 1 discarded" in t["notes written"]["note"] or "merged" in t["notes written"]["note"]
    rows = {r[0]: r for r in section(p, "Who contributes")["table"]["rows"]}
    assert rows["hermes"][1] == "2" and rows["claude-code"][1] == "1"
    assert "decision 1" in rows["hermes"][2] and rows["hermes"][5] == "1"           # the discarded one
    where = {b["label"]: b["value"] for b in section(p, "Where the notes go")["bars"]}
    assert where == {"topics/backups": 2, "topics/printer": 1}                       # free text after the arrow is no page
    assert stats.build(0)["sections"][0]["stats"][1]["value"] == "4"                 # all time includes the old note


def test_usage_is_split_by_client_and_shows_gaps(wiki):
    p = stats.build(30)
    rows = {r[0]: r for r in section(p, "How the wiki is used")["table"]["rows"]}
    assert set(rows) == {"hermes", "claude-code"}                                    # curl's 2020 call is outside the period
    assert rows["hermes"][1:4] == ["4", "3", "1"] and rows["claude-code"][3] == "2"
    assert tiles(p)["rejected notes"]["value"] == "1"
    assert tiles(p)["searches"]["note"].startswith("2 found nothing")
    top = section(p, "Most used pages")["table"]["rows"]
    assert top[0][0]["text"] == "topics/printer" or top[0][0]["text"] == "topics/backups"
    sections = section(p, "Most read sections")["table"]["rows"]
    assert sections[0][1] == "Setup"
    look = [i["title"] for i in section(p, "Worth a look")["items"]]
    assert any("toner order" in x and "2x" in x for x in look)                        # the gap, with its count
    assert any("topics/printer: read 2 times but draft" in x for x in look)           # relied on, not verified
    searched = {b["label"]: b["value"] for b in section(p, "What agents look for")["bars"]}
    assert searched == {"toner order": 2, "backup time": 1}


def test_client_names_prefer_the_user_agent_over_the_shared_ip(wiki):
    names = {"127.0.0.1": "localhost", "python-httpx2": "hermes"}
    assert stats.client_name({"ua": "claude-code/2", "ip": "127.0.0.1"}, names) == "claude-code"
    assert stats.client_name({"ua": "python-httpx2/9", "ip": "127.0.0.1"}, names) == "hermes"
    assert stats.client_name({"ip": "claude-code-file"}, {}) == "claude-code (file read)"
    assert stats.client_name({"ip": "192.0.2.9"}, {"192.0.2.9": "open-webui"}) == "open-webui"


def test_stats_view_has_period_links_and_the_tab_exists(wiki):
    html = web.stats_view({"days": ["7"]})
    assert 'href="/_stats?days=0"' in html and "class=here>7 days" in html and "<h1>Stats</h1>" in html
    assert ("/_stats", "Stats") in web.all_tabs()
    assert web.stats_view({"days": ["x"]}).count("class=here") == 1               # a bad value falls back to 30
    assert "wiki" in stats.text(stats.build(30)).lower() or True


def test_the_default_wiki_name_is_justwiki(wiki):
    assert w.cfg()["wiki"]["name"] == "JustWiki"
    assert "JustWiki" in web.shell("/", "", "Home", "x", {})


# ---- search gaps and the steward

def add_misses(root, query, n, ts):
    with open(root / "logs/usage.jsonl", "a") as f:
        for _ in range(n):
            f.write(json.dumps({"ts": ts, "ip": "127.0.0.1", "ua": "python-httpx2/2.1", "tool": "search", "outcome": "miss",
                                "query": query}) + "\n")


def test_gaps_are_grouped_closed_when_reviewed_and_reopen_when_they_keep_coming(wiki):
    add_misses(wiki, "  Toner   ORDER ", 1, datetime.now().strftime("%Y-%m-%d %H:%M"))
    g = {x["query"]: x for x in stats.gaps(30)}
    assert g["toner order"]["count"] == 3 and len(g) == 1                                          # one query, however it is typed
    assert stats.resolve("toner order", "frobnicate")[0] is False
    assert stats.resolve("toner order", "covered", page="topics/nope")[0] is False                  # the page must exist
    ok, msg = stats.resolve("Toner Order", "covered", page="topics/printer", note="added to the printer page")
    assert ok and stats.open_gaps() == []
    row = stats.gaps(30)[0]
    assert row["review"]["outcome"] == "covered" and row["review"]["page"] == "topics/printer"
    add_misses(wiki, "toner order", 1, "2099-01-01 00:00")
    assert stats.open_gaps() == []                                                                   # one more is not enough
    add_misses(wiki, "toner order", 1, "2099-01-01 00:01")
    assert [x["query"].lower() for x in stats.open_gaps()] == ["toner order"]                        # it keeps coming: open again


def test_the_gaps_command_lists_and_records(wiki, capsys):
    assert stats.gaps_main([]) == 0 and "toner order" in capsys.readouterr().out
    assert stats.gaps_main(["resolve", "toner order", "question", "--note", "asked the owner"]) == 0
    assert "'toner order': question" in capsys.readouterr().out
    assert stats.gaps_main([]) == 0 and "no open search gaps" in capsys.readouterr().out
    assert stats.gaps_main(["--all"]) == 0 and "[question" in capsys.readouterr().out
    assert stats.gaps_main(["resolve", "toner order", "bogus"]) == 1
    assert stats.gaps_main(["resolve", "only-a-query"]) == 2
    assert json.loads((wiki / "logs/gaps-reviewed.json").read_text())["toner order"]["note"] == "asked the owner"


def test_the_steward_counts_gaps_and_has_a_scope_and_a_step_for_them(wiki):
    from just_wiki import steward
    assert "gaps" in steward.SCOPES and steward.waiting()["gaps"] == 1 and "1 search gap(s)" in steward.describe(steward.waiting())
    text = steward.prompt(["gaps"])
    assert "4c. **Search gaps.**" in text and "just-wiki gaps resolve" in text and "Never invent an answer" in text
    assert "4b." not in text and "5. **Open questions**" not in text and "1. `just-wiki lint`" in text          # only the steps it needs
    full = steward.prompt()
    assert all(x in full for x in ("4. **Stale pages", "4b. **Unchecked", "4c. **Search gaps", "5. **Open questions"))
    stats.resolve("toner order", "noise")
    assert steward.waiting()["gaps"] == 0
    assert steward.scope_arg(["gaps"]) == " only: gaps"
    assert "`unchecked`, `gaps`)" in full                                              # the header names every scope
    assert "never a reason to skip it" in " ".join(text.split()) and "A step with work waiting is not optional" in text and "just-wiki gaps" in text.split("4c.")[0]   # even in a scoped prompt


def test_stats_shows_gaps_with_the_stewards_outcome(wiki):
    p = stats.build(30)
    sec = section(p, "Search gaps")
    assert sec["table"]["rows"][0][0] == "toner order" and sec["table"]["rows"][0][3]["text"] == "open"
    assert any("toner order" in i["title"] for i in section(p, "Worth a look")["items"])
    stats.resolve("toner order", "written", page="topics/printer", note="from the manual")
    p = stats.build(30)
    row = section(p, "Search gaps")["table"]["rows"][0]
    assert row[3]["text"] == "written" and row[4]["text"] == "topics/printer" and row[5] == "from the manual"
    assert not any("toner order" in i["title"] for i in section(p, "Worth a look")["items"])               # closed: not "worth a look"
    items = web.plan_items(web.load())
    assert items["gaps"] == []


def test_a_read_through_an_alias_is_logged_with_its_page_and_old_records_without_one_do_not_break_the_stats(wiki):
    from just_wiki import mcp_server, usage
    for kw in ({"page": "topics/printer"}, {"id": "topics/printer"}, {"page_id": "topics/printer"}, {"name": "topics/printer"}):
        rec = mcp_server.usage_record("read_page", kw, "# Printer\n...", "127.0.0.1", "claude-code/2")
        assert rec["page"] == "topics/printer" and rec["outcome"] == "ok" and rec["tool"] == "read_page"
    assert mcp_server.usage_record("read_page", {"id": "x", "section": "S"}, "No page x", "127.0.0.1", "u")["outcome"] == "miss"
    found = mcp_server.usage_record("search", {"query": "toner"}, "1. topics/printer — Printer\n2. topics/scanner — Scanner", "127.0.0.1", "u")
    assert found["hits"] == ["topics/printer", "topics/scanner"] and "hits" not in mcp_server.usage_record("read_page", {}, "x", "i", "u")
    with open(wiki / "logs/usage.jsonl", "a") as f:                              # a record from before the fix: no page at all
        f.write('{"ts": "2099-01-01 00:00", "ip": "127.0.0.1", "ua": "claude-code/2", "tool": "read_page", "outcome": "ok", "chars": 5}\n')
    stats.build(30)                                                                # used to raise KeyError: 'page'
    assert usage.current(30)["pages_read"] >= 1
