# Wiki schema

This is a shared wiki for people and AI agents: what every agent and person working here needs to know and share. It follows Karpathy's LLM-wiki pattern
(https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f), changed for several writers: agents propose in
the inbox, one steward merges. Run by [just-wiki](https://github.com/siimtolk/just-wiki).

## Who writes what

| Who | Reads | Writes |
|---|---|---|
| Any agent | MCP (`get_index`, `get_status`, `search`, `read_page`), or the files | only `submit_note` → `inbox/<agent>/` |
| Scripts | — | `just-wiki note …`, or a note file in `inbox/<script-name>/` with the frontmatter below |
| Steward (`just-wiki steward`) | everything | curated pages, `status.md`, moves notes to `inbox/_processed/` |
| `just-wiki lint --fix` | everything | `index.md`, `status: stale` flags, `log.md` |
| `just-wiki live` | the host, through configured commands | the generated blocks |
| People | everything | anything |

Nobody hand-edits `index.md` or `brief.md` (generated) or rewrites `log.md` (append-only, via `just-wiki log`).

## Layout

```
wiki.toml      settings: MCP, web UI, generated blocks, brief targets, steward command
AGENTS.md      this schema (CLAUDE.md links here)
STEWARD.md     this wiki's own rules for the steward (optional)
index.md       generated catalogue: every page + summary
status.md      what is going on now, open questions with owners
brief.md       generated brief that agents keep in context
log.md         append-only: `- [YYYY-MM-DD HH:MM] op | text`
raw/           immutable sources (exports, clipped articles); read, never edited
inbox/<agent>/ notes waiting for the steward; inbox/_processed/ after merge
people/        one person: who they are, background, relationships, situation
preferences/   how someone wants things: tastes, standing instructions, how to answer or help them
topics/        one subject, thing or piece of work (a plan, a product, a book series, a service): details, state
decisions/     ADR style: context, decision, who and when, alternatives rejected
howtos/        how to do a recurring task, step by step
results/       outputs worth keeping: test results, measurements, reports
logs/          usage, run state (not in git)
```

## Page format

```yaml
---
title: Example topic
type: topic             # the folder's type in wiki.toml [pages]: person | preference | topic | decision | howto | result
status: verified        # draft | verified | stale
summary: One line for the index.
sources: [/etc/example.conf, https://…]
updated: 2026-01-01
updated_by: claude-code
---
```

- **Sources are required for `verified`.** Every claim must be traceable to a file, URL, command output or note. A
  page with no sources stays `draft`. Local paths in `sources` are checked by lint.
- **Links** are `[[folder/page]]` (full id, no `.md`). Obsidian-compatible.
- **Generated blocks:** `<!-- live:<name> -->` … `<!-- /live:<name> -->` is written by `just-wiki live`. Don't edit
  inside; don't repeat its values in prose.
- **Brief snippets:** lines between `<!-- brief -->` and `<!-- /brief -->` in a page go into the brief.
- **Facts about code stay thin:** say what a thing is for and where it lives, link the file.
- **Contradictions are kept, not overwritten:** both claims with sources under `## Disagreements & open questions`.
- **Rewrite, don't append:** when merging, rewrite the section so it reads as current truth; history is in git and
  `log.md`.
- **One page per thing.** Search before creating; extend the existing page.
- **No secrets.** Refer to where a secret is stored, never its value. Lint and `submit_note` reject secret-looking
  strings.

## Note format (inbox)

`submit_note(agent, kind, title, body, sources, topic)` writes:

```yaml
---
title: Service X answers slowly after 20k context
kind: finding           # finding | result | decision | question | correction
agent: hermes
created: 2026-01-01 10:52
topic: topics/example   # or null
sources: [grafana panel 2026-01-01]
---
<body>
```

Write a note when (1) a page was wrong or missing what you needed and you found the real value, (2) someone tells you
a decision, rule or preference, (3) a fix worked for a problem that can come back, or (4) a job ends with a result
worth keeping. One fact per note, with numbers, dates, paths and real sources. None for chat or opinions.
