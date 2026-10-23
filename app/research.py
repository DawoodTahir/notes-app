"""
"Find sources" — a small research agent behind any line of the notes.

Given one claim from a set of notes, it goes and looks for other videos that
speak to that claim, then reports what each one actually says, with a quote and
a timestamp so the finding can be checked rather than taken on trust.

The loop, deliberately shallow so it finishes in a reasonable time:

    claim ──► search queries (1 model call)
          ──► video search    (yt-dlp, no key, ~1s each)
          ──► transcripts     (Apify, ~5-15s each)
          ──► per-video read  (1 model call each)
          ──► verdict         (1 model call)

Every finding says how it relates to the claim — supports, disputes, or adds
context — because a source that contradicts the claim is worth more than one
that repeats it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

from .notes import _dispatch, NoteError, SINGLE_PASS_CHAR_LIMIT
from .transcript import TranscriptError, get_transcript, search_videos

# Keep the loop bounded: this runs while someone waits.
MAX_QUERIES = 3
MAX_CANDIDATES = 10
MAX_READS = 3
EXCERPT_CHARS = 6_000

RELATIONS = ("supports", "disputes", "context", "unrelated")


@dataclass
class Finding:
    title: str
    url: str
    platform: str
    relation: str              # one of RELATIONS
    quote: str                 # the speaker's own words
    timestamp: str             # "12:34", where the quote appears
    note: str                  # why it matters to the claim


@dataclass
class Research:
    claim: str
    instruction: str
    queries: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    verdict: str = ""
    searched: int = 0
    read: int = 0

    def public(self) -> dict:
        d = asdict(self)
        d["findings"] = [asdict(f) if not isinstance(f, dict) else f for f in self.findings]
        return d


def _json_from(text: str):
    """Models wrap JSON in prose or fences often enough to be worth handling."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1)
    for pattern in (r"\[.*\]", r"\{.*\}"):
        m = re.search(pattern, text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text)
    except Exception:
        return None


def _mmss(seconds: float) -> str:
    s = int(seconds or 0)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# ─────────────────────────────────────────────────────────────
# Steps
# ─────────────────────────────────────────────────────────────

def _plan_queries(claim: str, instruction: str, provider: str, model) -> list[str]:
    system = (
        "You turn a claim into video-search queries. Return ONLY a JSON array of "
        f"{MAX_QUERIES} short search strings — the kind a person types into a video "
        "site. Vary them: one to find the claim stated, one to find it challenged, "
        "one for the wider subject. No commentary."
    )
    user = f'Claim:\n"{claim}"\n'
    if instruction.strip():
        user += f'\nThe reader wants specifically:\n"{instruction.strip()}"\n'
    user += "\nJSON array of search strings:"

    data = _json_from(_dispatch(provider, system, user, model, max_tokens=300))
    queries = [str(q).strip() for q in (data or []) if str(q).strip()][:MAX_QUERIES]
    return queries or [claim[:120]]


def _gather(queries: list[str], exclude_url: str = "") -> list[dict]:
    seen, out = set(), []
    for q in queries:
        try:
            for v in search_videos(q, limit=4):
                url = v.get("url") or ""
                if not url or url in seen or url == exclude_url:
                    continue
                seen.add(url)
                out.append(v)
        except TranscriptError:
            continue
    return out[:MAX_CANDIDATES]


def _read_one(claim: str, instruction: str, video: dict,
              provider: str, model) -> Optional[Finding]:
    """Pull the transcript of one candidate and ask what it says about the claim."""
    try:
        tr = get_transcript(video["url"])
    except Exception:
        return None
    if not tr.segments:
        return None

    excerpt = tr.text[:EXCERPT_CHARS]
    system = (
        "You check whether a video's transcript speaks to a specific claim.\n"
        "Return ONLY a JSON object:\n"
        '{"relation":"supports|disputes|context|unrelated",'
        '"quote":"<= 45 words, the speaker\'s own words, verbatim>",'
        '"note":"one sentence on why it matters to the claim"}\n'
        "Use 'unrelated' freely — most videos will be. Never invent a quote: it "
        "must appear in the transcript."
    )
    user = f'Claim:\n"{claim}"\n'
    if instruction.strip():
        user += f'\nThe reader is looking for:\n"{instruction.strip()}"\n'
    user += f'\nVideo: {video.get("title","")}\n\nTranscript excerpt:\n"""{excerpt}"""\n\nJSON:'

    data = _json_from(_dispatch(provider, system, user, model, max_tokens=400))
    if not isinstance(data, dict):
        return None

    relation = str(data.get("relation", "")).strip().lower()
    if relation not in RELATIONS or relation == "unrelated":
        return None
    quote = " ".join(str(data.get("quote", "")).split())[:400]
    if not quote:
        return None

    # Locate the quote so the finding carries a real timestamp.
    stamp = ""
    probe = re.sub(r"[^a-z0-9 ]", "", quote.lower())[:40]
    if probe:
        for seg in tr.segments:
            if probe[:24] in re.sub(r"[^a-z0-9 ]", "", seg.text.lower()):
                stamp = _mmss(seg.start)
                break

    return Finding(
        title=tr.title or video.get("title", "Untitled"),
        url=video["url"],
        platform=getattr(tr, "platform", video.get("platform", "")),
        relation=relation,
        quote=quote,
        timestamp=stamp,
        note=" ".join(str(data.get("note", "")).split())[:300],
    )


def _verdict(claim: str, findings: list[Finding], provider: str, model) -> str:
    if not findings:
        return ""
    lines = "\n".join(
        f'- [{f.relation}] {f.title}: "{f.quote}"' for f in findings
    )[:SINGLE_PASS_CHAR_LIMIT]
    system = (
        "You summarise what a handful of sources say about a claim. Two or three "
        "sentences. Say plainly where they agree, where they conflict, and how "
        "much weight this actually carries. Do not overstate: a few videos are "
        "not proof. No preamble."
    )
    user = f'Claim:\n"{claim}"\n\nWhat the sources said:\n{lines}\n\nYour summary:'
    return _dispatch(provider, system, user, model, max_tokens=400).strip()


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def find_sources(claim: str, instruction: str = "", *, exclude_url: str = "",
                 provider: str = "", model=None) -> Research:
    claim = " ".join((claim or "").split())
    if len(claim) < 8:
        raise NoteError("Select a longer line to research.")

    import os
    provider = (provider or os.environ.get("NOTE_PROVIDER") or "anthropic").lower()

    out = Research(claim=claim, instruction=instruction or "")
    out.queries = _plan_queries(claim, out.instruction, provider, model)

    candidates = _gather(out.queries, exclude_url=exclude_url)
    out.searched = len(candidates)
    if not candidates:
        return out

    for video in candidates:
        if out.read >= MAX_READS:
            break
        out.read += 1
        finding = _read_one(claim, out.instruction, video, provider, model)
        if finding:
            out.findings.append(finding)

    out.verdict = _verdict(claim, out.findings, provider, model)
    return out
