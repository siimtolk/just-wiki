# JustWiki

A shared wiki for AI agents and the people they work with, kept true by a steward. (The command, package and
repository are called `just-wiki`.)

Agents forget, contradict each other and keep private copies of facts that drift. JustWiki gives them one place to
read from and one safe way to write to it:

- **Agents read** over MCP (`get_status`, `search`, `read_page`, `get_index`, …) or the plain Markdown files.
- **Agents write only notes** (`submit_note` → `inbox/<agent>/`). A note needs a title, a body and real sources;
  anything that looks like a secret is rejected.
- **One steward** merges notes into pages, re-checks stale pages and keeps a log. Many agents can write at once without
  conflicts, and every page change goes through the same reviewed step. The steward is Claude Code by default, or any
  OpenAI-compatible model (local or hosted) in pipeline mode.
- **Facts stay grounded.** Every verified page names its sources and lint checks them. Values that change with the
  machine (timers, containers, ports) sit in generated blocks that a command you configure fills, so they can't
  go stale ([how](#generated-blocks-facts-that-cant-drift)).
- **A brief** of the most-asked facts is generated and copied into every agent's context (`CLAUDE.md`, `HERMES.md`, …).
- **A web UI** to browse, edit and create pages, see what changed and how the wiki is used, answer open questions and run the
  steward, plus tabs of your own.
- **Optionally, agents' private memories are kept pointing at the wiki** instead of holding copies of its facts.

```
 agents ──MCP/files──▶  wiki (Markdown pages, brief)  ◀── people (web UI, Obsidian, editor)
   │                        ▲
   └─ submit_note ─▶ inbox ─┴── steward merges (Claude Code, or any OpenAI-compatible LLM)
```

It follows [Karpathy's LLM-wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f), changed
for several writers. The engine is a Python package; your content lives in its own folder and git repository.

**How it compares to Hindsight.** With the same local Qwen3.8-27B and a Hermes agent doing the asking, JustWiki matches
Hindsight's accuracy (65.7% against 60.5% on BEAM, 90.6% against 92.5% on my own chat logs; both gaps are inside the ±9-point
noise) while using 36–44% fewer tokens and under half the time per question. Asked in one step, without an agent, Hindsight is a
few points ahead (67.5% against 63.0%). Method, caveats and the reasons are in
[the article](https://siimtolk.github.io/articles/wiki-vs-hindsight/).

## Quick start

Needs Python 3.11+, git and [uv](https://docs.astral.sh/uv/) (or pipx). Developed on Linux; macOS is untested and Windows is not
supported (file locks use `fcntl`). systemd is optional.

```bash
uv tool install git+https://github.com/siimtolk/just-wiki     # or: pipx install git+https://github.com/siimtolk/just-wiki
just-wiki init ~/wiki && cd ~/wiki
just-wiki serve &                 # MCP server on http://127.0.0.1:8093/mcp
just-wiki web &                   # web UI on http://127.0.0.1:8094 (user wiki, password in .web_password)
just-wiki connect claude-code     # MCP for every Claude Code session + the brief in ~/.claude/CLAUDE.md
```

`init` makes the wiki its own git repository (separate from just-wiki) with a first commit, because the steward commits every
change; it creates nothing on GitHub. To back it up or share it, add a remote (a private one: it holds your setup facts):
`git remote add origin <url> && git push -u origin main`. The nightly run then pushes there. Ports and the web login are in
`wiki.toml` (`[mcp]`, `[web]`); change the ports if something else uses them. The web UI is plain HTTP with basic auth and anyone
who can log in can edit pages: keep it on a trusted network or behind a proxy.

Then let an agent use it: ask it to `search` for something, and to `submit_note` a fact with a source. Run
`just-wiki steward` to merge the note, or `just-wiki units --install` for systemd user units (MCP, web, a nightly run).

## Connect an agent

| Agent | How |
|---|---|
| Claude Code | `just-wiki connect claude-code`: adds the MCP server at user scope and a brief import to `~/.claude/CLAUDE.md` |
| Hermes | `just-wiki connect hermes --dir=~/.hermes` prints the `mcp_servers` entry and networking notes, sets up the brief for `<dir>/HERMES.md` (written by `just-wiki brief`) and puts the rule for writing notes into `<dir>/SOUL.md` (a managed block; an existing persona is kept) or, if there is no SOUL.md, on top of `HERMES.md` |
| Codex | `just-wiki connect codex` prints the config for `~/.codex/config.toml` and `AGENTS.md` |
| Anything else | `just-wiki connect generic` prints the MCP URL (streamable HTTP, path `/mcp`) and the prompt line |

`connect claude-code` also adds `mcp__wiki` to `permissions.allow` in `~/.claude/settings.json` (skip it with `--no-allow`).
Without it Claude Code asks before every MCP call, and its auto mode refuses the note-writing one ("External System
Writes"), so notes silently do not get written.

On the host, or in Docker with host networking, the MCP URL works as printed and needs no token. From a container on a
bridge network, set `[mcp] host = "0.0.0.0"` and send the bearer token from `.mcp_token`; never paste it into a note.
It is developed and tested with Hermes, Claude Code and Open WebUI; the Codex output is generated but less exercised.

Agents write notes only when their instructions say so. The MCP server sends its own instructions on connect, and
`connect` also installs the rule where the agent always reads it (`CLAUDE.md` for Claude Code, `SOUL.md` or `HERMES.md`
for Hermes, printed for the others): write a note with `submit_note` in the same turn, before replying, when a page was
wrong or missing something, someone states a decision, a fix worked, or a job ends with a result; none for chat or
opinions, never a secret.

Agents follow the rule most of the time, not always: in our runs a Hermes agent on a local Qwen wrote a note in every case that
called for one, and Claude Code in about two thirds of the cases where a decision was only mentioned in passing. The steward and
your own review are the safety net; `just-wiki gaps` and the Stats tab show what agents look for and what the wiki lacks.

An agent can be tested with three questions: does `search` return pages, is a `submit_note` without sources rejected, and
does it answer a setup question from the brief without a tool call.

## The steward

The steward merges the inbox into pages, answers open questions in `status.md`, re-checks stale pages and writes the log.

- **Agent mode (default):** runs `claude -p /wiki-steward`. The command is `[steward] command` in `wiki.toml`, so any agent
  that can follow `STEWARD.md` works.
- **Pipeline mode, no Claude needed:** small LLM calls merge notes. Any OpenAI-compatible endpoint works (llama.cpp, vLLM,
  Ollama, a hosted API):

  ```toml
  [steward]
  mode = "pipeline"       # notes are merged with small LLM calls
  rest = "skip"           # questions and stale pages need an agent; "skip" leaves them alone

  [steward.llm]
  base_url = "http://127.0.0.1:8080/v1"
  model = "my-model"
  api_key_file = "~/.config/my-llm/key.txt"   # a file holding the key, never the key itself
  reasoning_effort = "low"
  ```

The steward also reviews **search gaps**: queries agents typed that found nothing (`just-wiki gaps`). For each it searches
again with other words and then adds the term to a page that already answers it, writes the answer from a real source, raises
a question for a person, or ignores noise; it records the outcome (`just-wiki gaps resolve`) so nothing is asked twice, and a
gap that keeps coming back opens again. It never invents an answer: no source means a question, not a page. The Stats tab
shows each gap with its status. Scope it with `just-wiki steward --only=gaps`.

Without a steward command `just-wiki steward` says so and notes wait in `inbox/`; nothing is lost.
`just-wiki nightly` is the whole night: refresh generated blocks, brief, lint, the memory step (if on), the steward when
something waits, then push and a report.

## Generated blocks: facts that can't drift

Some facts change and the machine already knows them: which timers exist, which containers run, which ports listen.
Typed into a page they are right on the day and wrong a month later. So the page holds a marker and a command fills it:

```markdown
<!-- live:containers -->
(replaced on every run)
<!-- /live:containers -->
```

```toml
[live.blocks]
containers = ["docker", "ps", "--format", "- {{.Names}}"]   # any command that prints Markdown, without times
```

`just-wiki live` (part of `just-wiki nightly`) runs each command on the machine and writes its output between the
markers, under a "generated, do not edit" header. Nothing typed inside survives the next run, and a block only changes when
the output does (so commands print no timestamps).

- **Failures show.** A missing command, a non-zero exit or a timeout (`[live] timeout`, 120 s) turns the block into
  "Not generated: …" instead of keeping old data. Output that looks like a secret is replaced by a note.
- **`just-wiki live --check`** exits 1 if a block is out of date and writes nothing, for CI or a lint step.
- **Read-only.** The commands only print; just-wiki changes nothing on the host. What is not in a block (a sentence saying
  "the web UI is on port 8094") is prose and can still drift: put such values in a block and point to it.

## The web UI

`just-wiki web` has these tabs:

- **Pages:** browse and **edit**. Every page has Edit and Remove; "＋ New page" creates one in a folder. A web edit is a
  direct write plus one git commit of just that page and `index.md`, so it shows on the Changes tab and can be reverted. A
  hash of the page as you opened it stops you from overwriting a change made meanwhile (the steward's nightly merge, another
  person); invalid edits (a verified page without sources, anything that looks like a secret) are refused with the reason.
- **Changes:** the git history with diffs. **Schedule:** timers and their last result. **Open:** questions for you, answered in
  the browser. **Steward:** run it and see progress.
- **Stats:** who contributes how much and to which pages, how much each agent uses the wiki, what is searched for (and what
  is not found, with what the steward did about it), the most read pages and sections, and what is worth a look (gaps, heavily read drafts). Choose 7 days, 30
  days, 90 days or all time. Contributions come from the note files, usage from the MCP log; name clients in `[usage] names`
  by user agent (`python-httpx2 = "hermes"`) because local clients share one address. `just-wiki stats` prints the same.
- **Schema:** the page folders and their types from `wiki.toml` `[pages]`: add a folder, change what belongs in it, remove an
  empty one. Every change is a commit; running servers pick it up without a restart.
- **Memory** when that feature is on, and your own tabs:

```toml
[[web.panels]]
name = "Reports"
command = ["python3", "{root}/tools/my_report.py", "--json"]
```

The command prints JSON that the UI draws as stat tiles, bars, tables and cards (format at the top of
`src/just_wiki/web/__init__.py`). Everything is escaped; links are limited to site paths and http(s).

## Keep agents' memory pointing at the wiki (optional)

Agents keep private memory (Claude Code's auto-memory, Hermes' `MEMORY.md`, notes in a folder). A setup fact copied there
(an address, a path, a unit name) drifts away from the wiki and then beats it, because memory is loaded before the wiki is
read. `just-wiki memory` finds such copies and, in `fix` mode, replaces each value with `(see wiki <page>)`. It is off
unless `[memory]` is in `wiki.toml`:

```bash
just-wiki connect claude-code --memory             # adds a report-only [memory] with a claude-code source
just-wiki connect hermes --dir=~/.hermes --memory  # and a hermes source (the memories/ folder)
just-wiki memory scan                              # what holds a setup fact now (read-only)
just-wiki memory fix                               # what a fix would change; --apply does it (backups first)
just-wiki memory restore --yes                     # undo: the day's first copy of each file comes back
```

```toml
[memory]
enabled = true
mode = "report"                 # "report" warns (lint, the Memory tab); "fix" rewrites, with backups
presets = ["network", "paths"]  # network (addresses, ports), paths (path_prefixes), chat-ids, models (model_names)
path_prefixes = ["/srv/"]
[[memory.sources]]
agent = "hermes"
kind = "hermes"                 # claude-code | hermes | markdown-dir | file
path = "~/.hermes/memories"
[[memory.patterns]]             # your own
name = "ticket host"
regex = 'tickets\.example\.org/\S*'
```

- **Safe by default.** `report` never writes. `fix` rewrites by rule (a value becomes a pointer; sentence punctuation
  stays), backs up the day's first copy of each file, takes a lock, stops after `max_fixes` per run and checks every result
  (no fact left, a pointer to a page that exists, 80% of the other words kept). A model rewrites only when the rule can't
  and the line holds a preset or own pattern: `[memory.llm]` (any OpenAI-compatible endpoint; default `[steward.llm]`),
  then `fallback_command` (`{prompt}` in its arguments, stdout is the answer). Memory text leaves the machine only if you
  point the LLM there.
- **The wiki lists values too.** `just-wiki memory facts` (nightly) asks the LLM for unit, container, job and path names on
  the verified pages; code filters them (`memory-deny.txt` keeps tools and project names out), and a new value waits one
  night before it can rewrite anything. A value that leaves the wiki marks memories that still hold it as stale.
- **Audit.** `just-wiki memory audit` compares each memory with the closest pages and reports contradictions and stale
  entries. It never rewrites.
- **Visible.** The Memory tab shows the patterns, the fact-list run, corrections per day, top replaced values, candidates
  for standing rules (a line in Hermes' `SOUL.md` or a `CLAUDE.md` that stops a value at the source), the audit, each
  memory with its wiki links and recent events. `just-wiki memory report | candidates | line` print the same as text.
- **Scheduled.** The nightly run includes it when `[memory] nightly` is true (the default).
  `just-wiki units --install` adds a path unit that sweeps when a memory folder changes and a 15-minute timer.

## Commands

| Command | What it does |
|---|---|
| `init <dir>` | a new wiki from the templates |
| `serve` / `web` | the MCP server / the web UI |
| `connect <agent> [--memory]` | connect `claude-code`, `codex`, `hermes` or `generic` |
| `note <agent> <kind> <title> --source S…` | write a note from a script (body on stdin) |
| `steward [--force] [--only=notes,questions,stale,unchecked]` | merge notes and check pages now |
| `nightly` | live blocks, brief, lint, memory step, steward when needed, push, report |
| `lint [--fix \| --stale]` | health check; `--fix` rebuilds `index.md` and flags stale pages |
| `live [--check \| --show X]` | refresh generated blocks |
| `brief [--show \| --check]` | write the brief to its targets |
| `memory <cmd>` | the optional memory feature (`just-wiki memory help`) |
| `stats [days] [--json]` | who contributes what and where, how the wiki is used (the Stats tab) |
| `questions`, `unchecked`, `usage`, `log` | open questions, unchecked pages, who used the wiki, append to the log |
| `units [--install]` | systemd user units for MCP, web, the nightly run and (if on) the memory sweep |

The wiki is found by `--root DIR`, `$JUST_WIKI_ROOT`, or the nearest `wiki.toml` above the current folder.

## Configuration

Everything per wiki is in `wiki.toml` (the commented template is what `init` writes):

| Section | Sets |
|---|---|
| `[wiki]`, `[pages]` | name, owner; the page folders, their types and what belongs in each |
| `[mcp]`, `[web]` | host, ports, login, instructions agents get; panels and web hooks |
| `[brief]`, `[live.blocks]` | what goes in the brief and where it is copied; commands that fill generated blocks |
| `[steward]`, `[nightly]` | how the steward is launched or which LLM it uses; push, remote, report command |
| `[lint]`, `[schedule]`, `[tickets]` | extra checks; the schedule shown in the UI; a GitHub repo whose issues are tickets |
| `[memory]` | the optional memory feature above |

Things that belong to your machine (what fills a generated block, extra lint checks, how the steward is launched, where the
nightly report goes) are commands you list there, so the engine itself stays generic.

## Layout of a wiki

```
wiki.toml  AGENTS.md  STEWARD.md  index.md  status.md  brief.md  log.md
raw/              immutable sources
inbox/<agent>/    notes waiting for the steward
<page folders>/   people, preferences, topics, decisions, howtos, results (set in [pages])
logs/             usage and run state, including logs/memory* (not in git)
memory-deny.txt   values the memory fact list never takes (optional)
```

`AGENTS.md` in a new wiki is the full schema: who writes what, the page and note formats, links and generated blocks.
Pages need `sources` to be `verified`; lint checks local paths in them.

## Development

```bash
uv run --with pytest --with-editable . pytest -q     # tests run on throwaway wikis
uv run --with ruff ruff check src tests
scripts/privacy-scan.sh                              # no private addresses, home paths, chat ids or tokens in the repo
```

CI runs the same three.

## Articles

- [Less memory, more wiki](https://siimtolk.github.io/articles/model-wiki/): how the wiki works and why agents got up to 3× faster
  on 4× fewer tokens.
- [JustWiki matches Hindsight on Hermes. Twice the speed, 40% fewer tokens](https://siimtolk.github.io/articles/wiki-vs-hindsight/):
  the benchmark against Hindsight on BEAM and on real chat logs, what each is good at, and why agents cost so much.
- [Automated nightly LLM testing](https://siimtolk.github.io/articles/llm-testing-overnight/): the home setup that runs a benchmark
  agent while I sleep, one of the agents this wiki serves.

More at [siimtolk.github.io](https://siimtolk.github.io/).

## License

MIT
