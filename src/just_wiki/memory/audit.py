"""Memory audit: does an agent's memory contradict or outlive what the wiki says? Report only.

The fixes move copied values out of memory. They cannot see an entry that says "use X" after the wiki recorded a decision
to use Y. This reads every memory document (a Hermes entry, a Claude memory file's body), finds the three wiki pages that
share most of its words, and asks the LLM for a verdict: ok | contradicts | stale. (A rule that the wiki also states is
fine: memory is meant to hold how to work, so "duplicate" is not a finding.) A document that still holds a value that left
the wiki (memory-facts.json "retired") is a stale finding at once, from a code check. Nothing is rewritten: findings go
to logs/memory-audit.json, the Memory tab and the report. Verdicts are cached by the text of the entry and its pages, so
only changed entries are asked again; at most MAX_CALLS per run. Without an LLM only the retired-value check runs.
"""
import hashlib
import json
import re

from .. import store as w
from . import fix, state
from .patterns import value_regex

MAX_CALLS = 40
STOP = {"this", "that", "with", "from", "have", "when", "will", "your", "must", "into", "they", "then", "there",
        "their", "about", "which", "should", "never", "always", "only", "also", "each", "does", "than", "them", "been"}
PROMPT = """You check an AI agent's private memory entry against pages of the shared wiki, which is the source of truth.

Memory entry:
{entry}

Wiki pages that share its words:
{pages}

Answer with one JSON object and nothing else: {{"verdict": "ok" | "contradicts" | "stale", "reason": "<one sentence>"}}
- contradicts: the entry says something the pages say is different or no longer true.
- stale: the entry is a dated status or a decision the pages have clearly replaced.
- ok: it is a preference or a way of working, or it agrees with the pages (repeating a page is fine), or the pages
  do not cover it."""


def keywords(text):
    return set(re.findall(r"[^\W\d_]{5,}", text.lower())) - STOP


def candidates(entry, pages, n=3):
    """[(score, page id, text)] of the wiki pages sharing most keywords with the entry (score >= 3)."""
    kw = keywords(entry)
    scored = [(len(kw & keywords(t)), pid, t) for pid, t in pages.items()]
    return sorted([x for x in scored if x[0] >= 3], key=lambda x: (-x[0], x[1]))[:n]


def parse(answer):
    m = re.search(r"\{.*\}", answer, re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    v = d.get("verdict")
    return (v, str(d.get("reason", ""))[:300]) if v in ("ok", "contradicts", "stale") else (None, answer[:120])


def run(out=print):
    state.reset()
    pages = {pid: t for pid, t in state.page_texts().items() if not pid.startswith("inbox")}
    cache_f = state.logs() / "memory-audit-cache.json"
    cache = json.loads(cache_f.read_text()) if cache_f.exists() else {}
    retired = state.retired_facts()
    gone = value_regex(retired)
    c = fix.llm_config()
    llm = None
    if c:
        from ..pipeline import LLM
        llm = LLM({**c, "max_tokens": 600})
    findings, calls = [], 0
    for s in fix.all_sources():
        for u in s.documents():
            hit = gone.search(u.text) if gone else None
            if hit:
                findings.append({"where": f"{u.agent} {u.label}", "verdict": "stale", "pages": [retired[hit.group(0)] or ""],
                                 "reason": f"holds {hit.group(0)!r}, which is no longer anywhere in the wiki "
                                           f"(was on {retired[hit.group(0)]})", "entry": u.text[:200]})
                continue
            cands = candidates(u.text, pages)
            if not cands or not llm:
                continue
            key = hashlib.sha256((u.text + "".join(t for _, _, t in cands)).encode()).hexdigest()
            verdict = cache.get(key)
            if verdict is None:
                if calls >= MAX_CALLS:
                    break
                calls += 1
                shown = "\n\n".join(f"[{pid}]\n{t[:1500]}" for _, pid, t in cands)
                try:
                    v, reason = parse(llm.chat("You judge memory entries against the wiki.",
                                               PROMPT.format(entry=u.text[:1500], pages=shown), kind="memory"))
                except Exception as e:  # noqa: BLE001 - the model is down: nothing to report, try tomorrow
                    out(f"memory audit: the LLM is unavailable ({type(e).__name__}); stopping")
                    llm = None
                    continue
                if v is None:
                    continue
                verdict = cache[key] = {"verdict": v, "reason": reason}
            if verdict["verdict"] in ("contradicts", "stale"):
                findings.append({"where": f"{u.agent} {u.label}", "verdict": verdict["verdict"], "reason": verdict["reason"],
                                 "pages": [p for _, p, _ in cands], "entry": u.text[:200]})
    state.audit_file().write_text(json.dumps({"at": w.now(), "asked": calls, "findings": findings}, indent=1,
                                             ensure_ascii=False), encoding="utf-8")
    cache_f.write_text(json.dumps(cache), encoding="utf-8")
    for f in findings:
        out(f"{f['verdict']}: {f['where']} — {f['reason']}")
    out(f"memory audit: {calls} asked, {len(findings)} findings")
    return findings
