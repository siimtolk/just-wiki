"""connect claude-code: the MCP server, the brief import, and the permission that lets Claude Code write notes."""
import json
import os
import stat

import pytest

from just_wiki import setup


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty home, a wiki, and a fake `claude` that accepts `mcp get` (not there yet) and `mcp add`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text('#!/bin/sh\nif [ "$2" = "get" ]; then exit 1; fi\necho "$@" >> "$HOME/claude-calls.txt"\nexit 0\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    setup.init(tmp_path / "wiki")
    return tmp_path


def settings(home):
    return json.loads((home / ".claude/settings.json").read_text())


def test_connect_allows_the_wiki_tools_so_notes_can_be_written(home, capsys):
    setup.connect(["claude-code"])
    out = capsys.readouterr().out
    assert settings(home) == {"permissions": {"allow": ["mcp__wiki"]}}
    assert "added 'mcp__wiki' to permissions.allow" in out and "--no-allow" in out
    assert "mcp add --scope user --transport http wiki" in (home / "claude-calls.txt").read_text()
    assert "submit_note" in (home / ".claude/CLAUDE.md").read_text()                       # the rule is installed too


def test_other_settings_are_kept_and_a_rerun_changes_nothing(home, capsys):
    (home / ".claude").mkdir()
    (home / ".claude/settings.json").write_text(json.dumps(
        {"model": "opus", "permissions": {"allow": ["Bash(ls:*)"], "deny": ["Read(.env)"]}, "hooks": {"Stop": []}}))
    setup.connect(["claude-code"])
    first = (home / ".claude/settings.json").read_text()
    data = json.loads(first)
    assert data["model"] == "opus" and data["hooks"] == {"Stop": []} and data["permissions"]["deny"] == ["Read(.env)"]
    assert data["permissions"]["allow"] == ["Bash(ls:*)", "mcp__wiki"]
    capsys.readouterr()
    setup.connect(["claude-code"])
    assert (home / ".claude/settings.json").read_text() == first and "already allowed" in capsys.readouterr().out


def test_no_allow_leaves_the_settings_alone_and_says_what_to_do(home, capsys):
    setup.connect(["claude-code", "--no-allow"])
    out = capsys.readouterr().out
    assert not (home / ".claude/settings.json").exists()
    assert 'allow "mcp__wiki" in permissions.allow' in out


def test_a_custom_server_name_is_what_gets_allowed(home):
    setup.connect(["claude-code", "--name=notes"])
    assert settings(home)["permissions"]["allow"] == ["mcp__notes"]


@pytest.mark.parametrize("content", ["{ not json", "[1, 2]", '{"permissions": "yes"}', '{"permissions": {"allow": "mcp__x"}}'])
def test_a_settings_file_it_cannot_read_safely_is_never_changed(home, capsys, content):
    (home / ".claude").mkdir()
    (home / ".claude/settings.json").write_text(content)
    setup.connect(["claude-code"])
    assert (home / ".claude/settings.json").read_text() == content
    out = capsys.readouterr().out
    assert "was not changed" in out and 'add "mcp__wiki"' in out
    assert (home / ".claude/CLAUDE.md").exists()                                           # the rest of connect still ran


def test_allow_wiki_tools_reports_what_it_did(home):
    first = setup.allow_wiki_tools("wiki")
    assert first.startswith("added 'mcp__wiki' to permissions.allow in ") and first.endswith("settings.json")
    assert setup.allow_wiki_tools("wiki") == f"mcp__wiki is already allowed in {home / '.claude/settings.json'}"
    assert not (home / ".claude/settings.json.tmp").exists()                                # the write is atomic: no temp file left


# ---- one name: just-wiki

def test_the_only_command_is_just_wiki():
    import tomllib
    from pathlib import Path
    scripts = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts == {"just-wiki": "just_wiki.cli:main"}


def test_the_old_environment_variables_are_not_read(tmp_path, monkeypatch):
    from just_wiki import config, lint
    (tmp_path / "wiki.toml").write_text("")
    for old in ("STEWARD_WIKI_ROOT", "GROUNDWIKI_ROOT", "WIKI_ROOT"):
        monkeypatch.setenv(old, str(tmp_path))
    monkeypatch.delenv("JUST_WIKI_ROOT", raising=False)
    monkeypatch.chdir("/")
    assert config.find_root() is None                                   # only JUST_WIKI_ROOT or a wiki.toml above counts
    monkeypatch.setenv("JUST_WIKI_ROOT", str(tmp_path))
    assert config.find_root() == tmp_path
    for old in ("WIKI_SANDBOX", "STEWARD_WIKI_SANDBOX", "GROUNDWIKI_SANDBOX"):
        monkeypatch.setenv(old, "1")
        monkeypatch.setenv(old + "_HIDDEN", "/srv")
    assert lint.hidden_in_sandbox("/srv/x") is False
    monkeypatch.setenv("JUST_WIKI_SANDBOX", "1")
    monkeypatch.setenv("JUST_WIKI_SANDBOX_HIDDEN", "/srv")
    assert lint.hidden_in_sandbox("/srv/x") is True


def test_a_block_with_an_old_marker_is_not_taken_over(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("<!-- groundwiki:k -->\nold text\n<!-- /groundwiki:k -->\n")
    assert setup.managed_block(f, "k", "new text") is True
    text = f.read_text()
    assert "old text" in text and text.count("<!-- just-wiki:k -->") == 1          # the old block is left alone, a new one added
    assert setup.managed_block(f, "k", "new text") is False                         # and the new one is idempotent
