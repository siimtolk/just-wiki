"""The pipeline steward, with a scripted model: routing, merging, the code checks, filing, and what stays pending."""
import json
import re
import subprocess
from datetime import date

import pytest

from just_wiki import notes, pipeline, questions, setup, steward
from just_wiki import store as w

PAGE = """---
title: Example service
type: system
status: verified
summary: A demo service.
sources: [{src}]
updated: 2026-01-01
updated_by: test
---

# Example service

Listens on port 8094, unit `example.service`, data in /srv/example/data.

<!-- live:up -->
up since 2026-01-01
<!-- /live:up -->

See [[status]].
"""


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    root = tmp_path / "wiki"
    setup.init(str(root))
    from test_core import setup_layout
    setup_layout(root)
    src = tmp_path / "source.txt"
    src.write_text("x")
    (root / "systems/example.md").write_text(PAGE.format(src=src))
    toml = (root / "wiki.toml").read_text()
    toml = toml.replace('mode = "agent"', 'mode = "pipeline"', 1).replace('rest = "agent"', 'rest = "skip"', 1)
    (root / "wiki.toml").write_text(toml + '\n[steward.llm]\nbase_url = "http://stub/v1"\nmodel = "stub"\n')
    w.set_root(root)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"],
                   check=True)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    return root


class Script:
    """Replies in order; records the prompts it got."""

    def __init__(self, monkeypatch, replies):
        self.replies, self.prompts = list(replies), []
        monkeypatch.setattr(pipeline.LLM, "chat", lambda llm, system, user, kind="edit": self.reply(llm, user))

    def reply(self, llm, user):
        self.prompts.append(user)
        llm.calls += 1
        llm.usage["input_tokens"] += len(user) // 4
        return self.replies.pop(0)


def section(old, heading="# Example service"):
    """The section text as the model sees it (generated blocks collapsed)."""
    small, _ = pipeline.collapse(w.split(old)[1])
    return dict(pipeline.sections(small))[heading]


def merged(old, change, extra=""):
    text = section(old).replace("port 8094", change)
    return f'<why>port changed</why><edit heading="# Example service">{text}</edit>{extra}'


def edited(text, heading="# Example service"):
    return f'<why>x</why><edit heading="{heading}">{text}</edit>'


def note(title, body, topic="", kind="finding"):
    ok, msg = notes.submit("hermes", kind, title, body, ["host check 2026-11-02"], topic=topic)
    assert ok, msg


def test_topic_note_is_merged_without_routing(wiki, monkeypatch):
    note("Example now on port 8095", "The example service moved from 8094 to port 8095 on 2026-11-02.", "systems/example")
    old = (wiki / "systems/example.md").read_text()
    s = Script(monkeypatch, [merged(old, "port 8095 (was 8094 until 2026-11-02)")])
    assert pipeline.run(out=lambda *_: None) == 0
    assert len(s.prompts) == 1 and "PAGE systems/example" in s.prompts[0]
    page = (wiki / "systems/example.md").read_text()
    meta, body = w.split(page)
    assert "port 8095" in body and meta["updated"] == date.today().isoformat()
    assert "host check 2026-11-02" in meta["sources"] and len(meta["sources"]) == 2      # old ∪ the note's
    assert meta["status"] == "draft"                       # A: nothing in the note code could check → not verified
    assert not w.inbox_notes() and list((wiki / "inbox/_processed/hermes").glob("*.md"))
    assert "merge " in (wiki / "log.md").read_text()
    log = subprocess.run(["git", "-C", str(wiki), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert "steward: merge 1 note(s) into systems/example" in log
    out = json.loads(next((wiki / "logs").glob(f"steward-{date.today()}-*.json")).read_text())
    assert out["num_turns"] == 1 and out["summary"]["merged"] == 1


def test_lost_facts_get_one_retry_then_stay_pending(wiki, monkeypatch):
    note("Example port", "The example service listens on port 8095 now.", "systems/example")
    old = (wiki / "systems/example.md").read_text()
    bad = edited("Port 8095.")
    s = Script(monkeypatch, [bad, merged(old, "port 8095")])
    assert pipeline.run(out=lambda *_: None) == 0
    assert "these facts from the page are gone" in s.prompts[1] and "live:up" in s.prompts[1]
    assert "up since 2026-01-01" not in s.prompts[0]                  # generated blocks are collapsed in prompts
    assert "/srv/example/data" in (wiki / "systems/example.md").read_text()

    note("Example port again", "The example service is on port 8096 now.", "systems/example")
    Script(monkeypatch, [bad, bad, bad])
    assert pipeline.run(out=lambda *_: None) == 1                     # three bad versions: the note waits
    assert list((wiki / "logs/pipeline-failures").glob("*-systems__example.txt"))   # the last reply is kept
    assert len(w.inbox_notes()) == 1 and "pending " in (wiki / "log.md").read_text()
    assert "port 8095" in (wiki / "systems/example.md").read_text()  # the page is untouched


def test_generated_block_must_stay(wiki, monkeypatch):
    note("Example up", "The example service was restarted on 2026-11-02.", "systems/example")
    old = (wiki / "systems/example.md").read_text()
    dropped = edited(section(old).split("<!-- live:")[0] + "Restarted on 2026-11-02.\n\nSee [[status]].")
    s = Script(monkeypatch, [dropped, dropped, dropped])
    assert pipeline.run(out=lambda *_: None) == 1
    assert "<!-- live:up (generated…) --> was changed or removed" in s.prompts[1]


def test_route_new_page_question_and_discard(wiki, monkeypatch):
    note("Backup runs nightly", "restic backs up /srv to the second disk at 03:00 every night.")
    note("Should backups go off-site?", "Nobody decided whether the backup should also go off-site.", kind="question")
    note("Example is fine", "The example service is still on 8094, nothing new.")
    new_page = ("<why>new</why><page>---\ntitle: Backups\ntype: runbook\nsummary: Nightly restic backup.\n"
                "sources: []\n---\n\n# Backups\n\nrestic backs up /srv to the second disk at 03:00. "
                "See [[systems/example]].\n</page>")
    s = Script(monkeypatch, [
        json.dumps({"action": "new", "folder": "runbooks", "slug": "backups", "title": "Backups",
                    "related": "systems/example", "why": "no backup page"}),
        json.dumps({"action": "question", "question": "Off-site backup?", "context": "raised 2026-11-02", "owner": "siim"}),
        json.dumps({"action": "discard", "why": "the page already says 8094"}),
        new_page])
    assert pipeline.run(out=lambda *_: None) == 0
    assert "PAGES (id — title — summary)" in s.prompts[0]
    meta, body = w.split((wiki / "runbooks/backups.md").read_text())
    assert meta["type"] == "runbook" and "03:00" in body and "host check 2026-11-02" in meta["sources"]
    assert "[[runbooks/backups]]" in (wiki / "systems/example.md").read_text()              # linked from related
    assert "**Off-site backup?**" in (wiki / "status.md").read_text()
    log = (wiki / "log.md").read_text()
    assert "discard " in log and "question " in log and "new " in log
    assert not w.inbox_notes()


def test_answers_are_left_to_the_agent(wiki, monkeypatch):
    ok, _ = notes.submit("siim", "decision", "Decided: keep 8094", "*Decision:* keep 8094 for now.",
                         ["web UI"], extra={"answers": "abc123", "action": "decide"})
    assert ok
    s = Script(monkeypatch, [])
    assert pipeline.run(out=lambda *_: None) == 0
    assert s.prompts == [] and len(w.inbox_notes()) == 1


def test_steward_once_runs_the_pipeline(wiki, monkeypatch):
    note("Example now on port 8095", "The example service moved to port 8095.", "systems/example")
    old = (wiki / "systems/example.md").read_text()
    Script(monkeypatch, [merged(old, "port 8095")])
    called = []
    monkeypatch.setattr(steward, "run_agent", lambda *a, **k: called.append(a) or 0)
    assert steward.steward_once(out=lambda *_: None) == 0
    assert called == [] and not w.inbox_notes()


def test_verified_page_stays_verified_only_when_code_checked_every_note(wiki, monkeypatch, tmp_path):
    from just_wiki import verify
    real = tmp_path / "srv" / "config.toml"
    real.parent.mkdir()
    real.write_text("x")
    note("Example config path", f"The example service reads its settings from {real} since 2026-11-02.",
         "systems/example")
    old = (wiki / "systems/example.md").read_text()
    Script(monkeypatch, [merged(old, "port 8094 (settings in " + str(real) + ")")])
    assert pipeline.run(out=lambda *_: None) == 0
    assert w.split((wiki / "systems/example.md").read_text())[0]["status"] == "verified"     # B passed → A keeps it
    assert verify.load() == {} and "[checked: " in (wiki / "log.md").read_text()

    note("Example cache", "The example service caches in /nonexistent/example/cache now.", "systems/example")
    old = (wiki / "systems/example.md").read_text()
    Script(monkeypatch, [merged(old, "port 8094, cache in /nonexistent/example/cache")])
    assert pipeline.run(out=lambda *_: None) == 0
    meta = w.split((wiki / "systems/example.md").read_text())[0]
    assert meta["status"] == "draft"                                                       # B failed → A: draft
    entries = verify.load()
    assert "systems/example" in entries and "does not exist" in entries["systems/example"]["why"][0]
    assert "[unchecked: " in (wiki / "log.md").read_text()


def test_unchecked_list_and_due(wiki, monkeypatch):
    from just_wiki import verify
    path = wiki / "systems/example.md"                                # as the pipeline leaves it: draft, listed
    meta, body = w.split(path.read_text())
    path.write_text(w.dump({**meta, "status": "draft"}, body))
    verify.add("systems/example", ["n1"], "nothing checkable")
    assert list(verify.reconcile()) == ["systems/example"]
    assert steward.waiting()["unchecked"] == 1
    assert verify.due() == {}                                        # just added: not due before max_wait_min
    entries = verify.load()
    entries["systems/example"]["since"] = "2026-01-01 00:00"
    verify.save(entries)
    assert list(verify.due()) == ["systems/example"]
    runs = []
    monkeypatch.setattr(steward, "run_agent", lambda tag, only=None: runs.append(only) or 0)
    assert steward.due(out=lambda *_: None) == 0 and runs == [["unchecked"]]
    meta, body = w.split(path.read_text())                           # the agent verified it: off the list
    meta.update(status="verified", updated=date.today().isoformat())
    path.write_text(w.dump(meta, body))
    assert verify.reconcile() == {}
    assert "only: unchecked" in steward.scope_arg(["unchecked"]) and "at most 10" in steward.prompt()


def test_verify_disabled_keeps_status_and_no_list(wiki, monkeypatch):
    from just_wiki import verify
    toml = (wiki / "wiki.toml").read_text() + "\n[steward.verify]\nenabled = false\n"
    (wiki / "wiki.toml").write_text(toml)
    w.set_root(wiki)
    note("Example cache", "The example service caches in /nonexistent/example/cache now.", "systems/example")
    old = (wiki / "systems/example.md").read_text()
    Script(monkeypatch, [merged(old, "port 8094, cache in /nonexistent/example/cache")])
    assert pipeline.run(out=lambda *_: None) == 0
    assert w.split((wiki / "systems/example.md").read_text())[0]["status"] == "verified"
    assert verify.load() == {}


def test_disagreement_and_knock_on_pages_go_to_the_agent_check(wiki, monkeypatch, tmp_path):
    from just_wiki import verify
    other = wiki / "runbooks/example-restart.md"
    other.write_text("---\ntitle: Restart example\ntype: runbook\nstatus: verified\nsummary: How to restart.\n"
                     f"sources: [{tmp_path / 'source.txt'}]\nupdated: 2026-01-01\nupdated_by: t\n---\n\n"
                     "# Restart example\n\nCheck it answers on port 8094 afterwards. See [[systems/example]].\n")
    note("Example moved to 8095", "The example service moved from port 8094 to 8095 on 2026-11-02.", "systems/example")
    old = (wiki / "systems/example.md").read_text()
    add = '<add after="# Example service" heading="## Disagreements & open questions">- The note says 8095.</add>'
    Script(monkeypatch, [merged(old, "port 8095", add)])
    assert pipeline.run(out=lambda *_: None) == 0
    entries = verify.load()
    assert "disagreement" in entries["systems/example"]["why"][-1]
    assert "may still say 8094" in entries["runbooks/example-restart"]["why"][0]
    assert w.split((wiki / "systems/example.md").read_text())[0]["status"] == "draft"


def test_scoped_prompt_has_only_the_steps_it_needs(wiki):
    full, q = steward.prompt(), steward.prompt(["questions"])
    assert "5. **Open questions**" in q and "4. **Stale pages.**" not in q and "3. For each note:" not in q
    assert "1. `just-wiki lint`" in q and "## Limits" in q and len(q) < 0.7 * len(full)


def test_mechanical_answers_are_table_edits(wiki, monkeypatch):
    from just_wiki import questions
    status = wiki / "status.md"
    status.write_text(status.read_text() + "| **Move the backups?** — raised 2026-11-01 | ? |\n"
                      "| **Rename the unit?** — raised 2026-11-01 | ? |\n| **Old question** — 2026-10-01 | ? |\n")
    rows = {q["text"].split("**")[1]: q for q in questions.rows()}
    for key, action, text in (("Move the backups?", "owner", "siim"), ("Rename the unit?", "postpone", "2026-12-01"),
                              ("Old question", "drop", "no longer relevant")):
        ok, msg = questions.answer_note(rows[key], action, text, "siim")
        assert ok, msg
    runs = []
    monkeypatch.setattr(steward, "run_agent", lambda tag, only=None: runs.append(only) or 0)
    assert steward.steward_once(out=lambda *_: None) == 0
    after = {q["text"]: q for q in questions.rows()}
    assert any(t.startswith("**Move the backups?**") and q["owner"] == "siim" for t, q in after.items())
    assert any(t.startswith("**On 2026-12-01:** **Rename the unit?**") for t in after)
    assert not any("Old question" in t for t in after)
    assert questions.answers() == {} and runs == []                    # no agent needed for these three
    acts = {d["action"] for d in questions.settled(30)}
    assert {"owner", "postpone", "drop"} <= acts                      # logged as the web UI reads them


def test_page_edited_during_the_merge_is_merged_again(wiki, monkeypatch):
    note("Example now on port 8095", "The example service moved to port 8095.", "systems/example")
    page = wiki / "systems/example.md"
    old = page.read_text()
    first = merged(old, "port 8095")
    human = old.replace("Listens on", "Since 2026-11-02 it listens on")

    class Racy(Script):
        def reply(self, llm, user):
            if not self.prompts:                                     # someone edits the page meanwhile
                page.write_text(human)
            return super().reply(llm, user)
    s = Racy(monkeypatch, [first, merged(human, "port 8095")])
    assert pipeline.run(out=lambda *_: None) == 0
    text = page.read_text()
    assert "Since 2026-11-02 it listens on port 8095" in text         # both the human edit and the note survive
    assert len(s.prompts) == 2 and "Since 2026-11-02" in s.prompts[1]


def test_commits_only_its_own_files(wiki, monkeypatch):
    (wiki / "runbooks").mkdir(exist_ok=True)
    draft = wiki / "runbooks/half-written.md"
    draft.write_text("someone's work in progress\n")
    note("Example now on port 8095", "The example service moved to port 8095.", "systems/example")
    Script(monkeypatch, [merged((wiki / "systems/example.md").read_text(), "port 8095")])
    assert pipeline.run(out=lambda *_: None) == 0
    status = subprocess.run(["git", "-C", str(wiki), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, text=True).stdout
    assert "?? runbooks/half-written.md" in status and status.count("\n") == 1   # only it: everything else committed


def test_note_for_two_pages_is_not_merged_twice(wiki, monkeypatch):
    other = wiki / "runbooks/example-restart.md"
    other.write_text("---\ntitle: Restart example\ntype: runbook\nstatus: draft\nsummary: How to restart.\nsources: [x]\n"
                     "updated: 2026-01-01\nupdated_by: t\n---\n\n# Restart example\n\nCheck port 8094. "
                     "See [[systems/example]].\n")
    note("Example moved to 8095", "The example service and its restart check moved to port 8095.")
    old = (wiki / "systems/example.md").read_text()
    route = json.dumps({"action": "merge", "pages": ["systems/example", "runbooks/example-restart"], "why": "both"})
    Script(monkeypatch, [route, merged(old, "port 8095"), "no edit", "still no edit", "no edit again"])
    assert pipeline.run(out=lambda *_: None) == 1                     # the second page failed: the note waits
    assert len(w.inbox_notes()) == 1
    nid = w.inbox_notes()[0].stem
    assert pipeline.Progress().done(nid) == {"systems/example"}
    restart = edited(section(other.read_text(), "# Restart example").replace("8094", "8095"), "# Restart example")
    s = Script(monkeypatch, [route, restart])
    assert pipeline.run(out=lambda *_: None) == 0
    assert len(s.prompts) == 2 and "PAGE runbooks/example-restart" in s.prompts[1]   # systems/example not again
    assert not w.inbox_notes() and pipeline.Progress().done(nid) == set()


def test_second_note_joins_a_planned_page_by_its_slug_and_duplicates_are_dropped(wiki, monkeypatch):
    note("Backup runs nightly", "restic backs up /srv to the second disk at 03:00 every night.")
    note("Backup retention", "The restic backup keeps 14 daily snapshots on the second disk.")
    page = ("<why>new</why><page>---\ntitle: Backups\ntype: runbook\nsummary: Nightly restic backup.\n---\n\n"
            "# Backups\n\nrestic backs up /srv at 03:00 and keeps 14 daily snapshots. See [[systems/example]].\n</page>")
    s = Script(monkeypatch, [
        json.dumps({"action": "new", "folder": "runbooks", "slug": "backups", "title": "Backups",
                    "related": "systems/example"}),
        json.dumps({"action": "merge", "pages": ["runbooks/backups"], "why": "same subject"}), page])
    assert pipeline.run(out=lambda *_: None) == 0
    assert "PLANNED (new pages this run, not written yet): runbooks/backups" in s.prompts[1]
    assert "runbooks/backups —" not in s.prompts[1]          # named by id only, not offered as a page to join
    assert len([p for p in (wiki / "runbooks").glob("*.md")]) == 1 and not w.inbox_notes()
    note("Backup runs nightly again", "restic backs up /srv to the second disk at 03:00 every night.")
    s = Script(monkeypatch, [])
    assert pipeline.run(out=lambda *_: None) == 0 and s.prompts == []   # same text as an earlier note: no model call
    assert "same text as an earlier note" in (wiki / "log.md").read_text()


def test_only_one_steward_run_at_a_time(wiki, monkeypatch):
    import fcntl
    (wiki / "logs").mkdir(exist_ok=True)
    with open(wiki / "logs/steward.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with steward.run_lock(wait_s=0) as mine:
            assert mine is False


def test_unchecked_entries_clear_only_after_a_real_check_and_back_off(wiki, monkeypatch):
    from just_wiki import verify
    page = wiki / "systems/example.md"
    verify.add("systems/example", ["n1"], "may still say 8094")       # a knock-on page that is still 'verified'
    assert list(verify.reconcile()) == ["systems/example"]            # verified but unchanged since listed: stays
    entries = verify.load()
    entries["systems/example"]["since"] = "2026-01-01 00:00"
    verify.save(entries)
    verify.mark_tried(["systems/example"])
    assert verify.due() == {}                                          # tried just now: not due for RETRY_HOURS
    page.write_text(page.read_text().replace("Listens on", "Still listens on"))   # the agent fixed and verified it
    assert verify.reconcile() == {}


def test_full_page_overflows_into_a_linked_continuation(wiki, monkeypatch):
    toml = (wiki / "wiki.toml").read_text()
    (wiki / "wiki.toml").write_text(toml.replace('model = "stub"\n', 'model = "stub"\nmax_page_chars = 300\n', 1))
    note("Example port", "The example service listens on port 8095 now.", "systems/example")
    cont = ("<why>new</why><page>---\ntitle: Example service (continued)\ntype: system\nsummary: More.\nsources: []\n"
            "---\n\n# Example service (continued)\n\nThe example service listens on port 8095 now. "
            "See [[systems/example]].\n</page>")
    s = Script(monkeypatch, [cont])
    assert pipeline.run(out=lambda *_: None) == 0
    assert len(s.prompts) == 1 and "systems/example-2" in s.prompts[0]
    assert "port 8095" in (wiki / "systems/example-2.md").read_text()
    assert "[[systems/example-2]]" in (wiki / "systems/example.md").read_text()
    assert not w.inbox_notes()

    note("Example restarted", "The example service was restarted on 2026-11-03.", "systems/example")
    old = (wiki / "systems/example-2.md").read_text()
    text = section(old, "# Example service (continued)") + "\nRestarted on 2026-11-03.\n"
    s = Script(monkeypatch, [edited(text, "# Example service (continued)")])
    assert pipeline.run(out=lambda *_: None) == 0                     # the continuation still has room: merged there
    assert "PAGE systems/example-2" in s.prompts[0]
    assert "2026-11-03" in (wiki / "systems/example-2.md").read_text()


def test_full_page_waits_for_the_agent_when_rest_is_agent(wiki, monkeypatch):
    toml = (wiki / "wiki.toml").read_text().replace('rest = "skip"', 'rest = "agent"', 1)
    (wiki / "wiki.toml").write_text(toml.replace('model = "stub"\n', 'model = "stub"\nmax_page_chars = 300\n', 1))
    note("Example port", "The example service listens on port 8095 now.", "systems/example")
    s = Script(monkeypatch, [])
    assert pipeline.run(out=lambda *_: None) == 1
    assert not s.prompts and len(w.inbox_notes()) == 1


def test_status_and_index_are_never_merge_targets(wiki, monkeypatch):
    note("Craig's budget tracker", "Craig is building a budget tracker in Flask, MVP due 2024-04-15; open questions remain.")
    s = Script(monkeypatch, [json.dumps({"action": "merge", "pages": ["status"], "why": "open questions"}),
                             json.dumps({"action": "merge", "pages": ["index"], "why": "overview"})])
    assert pipeline.run(out=lambda *_: None) == 1                     # no valid page: the note waits
    assert "- status —" not in s.prompts[0] and "- index —" not in s.prompts[0]
    assert len(w.inbox_notes()) == 1 and "Craig" not in (wiki / "status.md").read_text()
    assert pipeline.route.__module__ and not pipeline.content_page(wiki / "status.md")
    assert pipeline.content_page(wiki / "systems/example.md")


def test_reply_format_is_forgiven_and_retried(wiki, monkeypatch):
    """Single quotes and code fences parse; a reply with no blocks gets the format back with the problems."""
    note("Example port", "The example service listens on port 8095 now.", "systems/example")
    old = (wiki / "systems/example.md").read_text()
    good = merged(old, "port 8095").replace('heading="# Example service"', "heading='# Example service'")
    s = Script(monkeypatch, ["Sure! Here is the update.", "```xml\n" + good + "\n```"])
    assert pipeline.run(out=lambda *_: None) == 0
    assert "never the whole page" in s.prompts[1]
    assert "port 8095" in (wiki / "systems/example.md").read_text()


def test_whole_page_reply_is_taken_as_the_edit(wiki, monkeypatch):
    note("Example port", "The example service listens on port 8095 now.", "systems/example")
    old = (wiki / "systems/example.md").read_text()
    small, _ = pipeline.collapse(w.split(old)[1])
    s = Script(monkeypatch, ["<why>port</why>\n" + small.replace("port 8094", "port 8095")])
    assert pipeline.run(out=lambda *_: None) == 0 and len(s.prompts) == 1
    text = (wiki / "systems/example.md").read_text()
    assert "port 8095" in text and "up since 2026-01-01" in text      # the generated block is restored


def test_group_that_fails_is_merged_one_note_at_a_time(wiki, monkeypatch):
    note("Example port", "The example service listens on port 8095 now.", "systems/example")
    note("Example data", "The example service keeps its data in /srv/example/data, checked 2026-11-02.",
         "systems/example")
    old = (wiki / "systems/example.md").read_text()
    data = edited(section(old) + "\nData checked 2026-11-02.")                      # notes in id order: data first
    port = edited(section(old).replace("port 8094", "port 8095") + "\nData checked 2026-11-02.")
    s = Script(monkeypatch, ["no", "no", "no", data, port])
    assert pipeline.run(out=lambda *_: None) == 0
    assert len(s.prompts) == 5 and s.prompts[3].count("--- note ") == 1
    assert not w.inbox_notes()


def test_note_facts_must_land_on_a_page(wiki, monkeypatch):
    """Coverage: a version and a time the merge dropped are asked for once; a page that still misses them is kept,
    and the gaps are logged."""
    note("Example updated", "Example 2.4.1 was installed on 2026-11-02 at 19:35; it listens on port 8094.",
         "systems/example")
    old = (wiki / "systems/example.md").read_text()
    lossy = edited(section(old) + "\nUpdated on 2026-11-02.")
    full = edited(section(old) + "\nVersion 2.4.1 since 2026-11-02 19:35.")
    s = Script(monkeypatch, [lossy, full])
    assert pipeline.run(out=lambda *_: None) == 0
    assert "on no page after this merge: 19:35, 2.4.1" in s.prompts[1]
    assert "2.4.1 since 2026-11-02 19:35" in (wiki / "systems/example.md").read_text()

    note("Example updated again", "Example 2.5.0 was installed on 2026-11-03 at 08:10.", "systems/example")
    new = (wiki / "systems/example.md").read_text()
    lossy = edited(section(new) + "\nUpdated again on 2026-11-03.")
    s = Script(monkeypatch, [lossy, lossy])
    assert pipeline.run(out=lambda *_: None) == 0                      # kept: the gap is logged, not a failure
    assert len(s.prompts) == 2 and not w.inbox_notes()
    assert "[on no page: 08:10, 2.5.0]" in (wiki / "log.md").read_text()


def test_multi_topic_note_says_where_else_it_goes(wiki, monkeypatch):
    other = wiki / "systems/other.md"
    other.write_text("---\ntitle: Other\ntype: system\nstatus: draft\nsummary: x.\nsources: [x]\n"
                     "updated: 2026-01-01\nupdated_by: t\n---\n\n# Other\n\nRuns 1.0.0.\n")
    third = wiki / "systems/third.md"
    third.write_text(other.read_text().replace("Other", "Third"))
    note("Service updates", "Example is now 2.0.0, Other is now 1.1.0 and Third is now 3.0.1.")
    route = json.dumps({"action": "merge", "pages": ["systems/example", "systems/other", "systems/third"],
                        "why": "three services"})
    ex = (wiki / "systems/example.md").read_text()
    s = Script(monkeypatch, [route, edited(section(ex) + "\nVersion 2.0.0."),
                             edited("Runs 1.1.0.", "# Other"), edited("Runs 3.0.1.", "# Third")])
    assert pipeline.run(out=lambda *_: None) == 0
    assert "it also goes to systems/other, systems/third" in s.prompts[1]
    assert "1.1.0" in other.read_text() and "3.0.1" in third.read_text() and len(s.prompts) == 4


def test_decide_and_drop_answers_become_notes_on_a_page(wiki, monkeypatch):
    from just_wiki import questions
    status = wiki / "status.md"
    status.write_text(status.read_text() + "| **Keep [[systems/example]] on 8094?** — raised 2026-11-01 | siim |\n"
                      "| **Review the example on 2026-11-07?** — raised 2026-11-01 | siim |\n")
    rows = {q["text"].split("**")[1]: q for q in questions.rows()}
    ok, _ = questions.answer_note(rows["Keep [[systems/example]] on 8094?"], "decide", "yes, keep 8094", "siim")
    assert ok
    ok, _ = questions.answer_note(rows["Review the example on 2026-11-07?"], "drop", "not useful any more", "siim")
    assert ok
    ex = (wiki / "systems/example.md").read_text()
    runs = []
    monkeypatch.setattr(steward, "run_agent", lambda tag, only=None: runs.append(only) or 0)
    route = json.dumps({"action": "merge", "pages": ["systems/example"], "why": "the review"})
    Script(monkeypatch, [route, edited(section(ex) + "\nDecided: stays on 8094 (siim, q-x). The 2026-11-07 review "
                                                     "was dropped: not useful any more.")])
    assert steward.steward_once(out=lambda *_: None) == 0
    assert runs == [] and questions.answers() == {} and not w.inbox_notes()   # no agent, nothing left
    assert not [q for q in questions.rows() if "example" in q["text"].lower()]
    log = (wiki / "log.md").read_text()
    assert "decide q-" in log and "drop q-" in log
    assert "not useful any more" in (wiki / "systems/example.md").read_text()


def test_oversized_page_is_split_by_section(wiki, monkeypatch):
    toml = (wiki / "wiki.toml").read_text()
    (wiki / "wiki.toml").write_text(toml.replace('model = "stub"\n', 'model = "stub"\nsplit_page_chars = 600\n', 1))
    long_a = "Port 8095 serves the example service. " * 12
    long_b = "Restart it with systemctl restart example on 2026-11-03. " * 8
    note("Example details", f"{long_a}\n\n{long_b}", "systems/example")
    old = (wiki / "systems/example.md").read_text()
    head = re.search(r"(?m)^# .*$", old).group(0)
    new_text = section(old, head) + "\n" + long_a + "\n"
    reply = (f'<edit heading="{head}">{new_text}</edit>'
             f'<add after="{head}" heading="## Restarting">{long_b}</add><why>details</why>')
    split = json.dumps({"moves": [{"heading": "## Restarting", "folder": "runbooks", "slug": "restart-example",
                                   "title": "Restart the example service"}], "why": "a how-to"})
    nothing = json.dumps({"moves": [], "why": "fine"})        # the page and its new target, checked again
    s = Script(monkeypatch, [reply, split, nothing, nothing])
    assert pipeline.run(out=lambda *_: None) == 0
    assert "over the limit of 600" in s.prompts[1] and "## Restarting" in s.prompts[1]
    moved = (wiki / "runbooks/restart-example.md").read_text()
    assert "type: runbook" in moved and long_b.strip() in moved and "[[systems/example]]" in moved
    page = (wiki / "systems/example.md").read_text()
    assert "## Restarting" not in page and "[[runbooks/restart-example]]: Restarting" in page
    assert "split systems/example" in (wiki / "log.md").read_text()


def test_router_sees_people_and_preference_pages_and_the_rules(tmp_path, monkeypatch):
    root = tmp_path / "chat"
    setup.init(str(root))
    toml = (root / "wiki.toml").read_text().replace('mode = "agent"', 'mode = "pipeline"', 1)
    (root / "wiki.toml").write_text(toml.replace('rest = "agent"', 'rest = "skip"', 1)
                                    + '\n[steward.llm]\nbase_url = "http://stub/v1"\nmodel = "stub"\n')
    (root / "people/sam.md").write_text("---\ntitle: Sam\ntype: person\nstatus: draft\nsummary: The user.\n"
                                        "sources: []\n---\n\n# Sam\n\nSam is a producer.\n")
    w.set_root(root)
    monkeypatch.chdir(root)
    note("Wants short answers", "Sam asks for answers in three bullets at most.")
    s = Script(monkeypatch, [json.dumps({"action": "discard", "why": "test"})])
    assert pipeline.run(out=lambda *_: None) == 0
    p = s.prompts[0]
    assert "people/:\n- people/sam — Sam — The user." in p          # listed although the search may not find it
    assert "How someone wants things" in p and "goes to preferences/" in p and "holds one decision" in p


def test_split_markers_do_not_pile_up(wiki):
    """A section moved twice keeps one 'Moved here from' marker per source page, not the old ones as well."""
    src = ("---\ntitle: Source\ntype: topic\nstatus: draft\nsummary: s\nsources: []\n---\n\n# Source\n\nIntro.\n\n"
           "## Alpha\n\nAlpha text.\n\n(Moved here from [[systems/older]].)\n\n"
           "## Beta\n\nBeta text.\n\n(Moved here from [[topics/oldest]].)\n\n## Gamma\n\nGamma text.\n")
    (wiki / "topics").mkdir(exist_ok=True)
    (wiki / "topics/source.md").write_text(src)
    out = pipeline.apply_split("topics/source", src, [("## Alpha", "systems/example", None)], "t")
    dest = out[wiki / "systems/example.md"]
    assert "Alpha text." in dest and "[[systems/older]]" not in dest           # the old marker did not travel
    assert dest.count("(Moved here from [[topics/source]].)") == 1
    (wiki / "systems/example.md").write_text(dest)
    out2 = pipeline.apply_split("topics/source", src, [("## Beta", "systems/example", None)], "t")
    dest2 = out2[wiki / "systems/example.md"]
    assert "Beta text." in dest2 and "[[topics/oldest]]" not in dest2
    assert dest2.count("(Moved here from [[topics/source]].)") == 1            # still once for the same source


def test_answer_to_a_question_that_is_gone_is_merged_like_a_note(wiki, monkeypatch):
    """A decision note named for question row q-abcdef, when no such row exists (a replayed wiki, a dropped row), must
    not wait in the inbox forever: it is an ordinary decision note."""
    note("Siim decided: keep port 8094", "Siim's decision on an open question, 2026-11-02.\n\n*Decision:* Keep 8094.",
         "systems/example", kind="decision")
    inbox = [p for p in w.inbox_notes()][0]
    inbox.rename(inbox.with_name("2026-11-02-decision-abcdef.md"))
    assert "abcdef" in questions.answers() and questions.find("abcdef") is None
    old = (wiki / "systems/example.md").read_text()
    Script(monkeypatch, [merged(old, "port 8094 (kept, Siim 2026-11-02)")])
    assert pipeline.run(out=lambda *_: None) == 0
    assert not w.inbox_notes()
    assert "kept, Siim 2026-11-02" in (wiki / "systems/example.md").read_text()
