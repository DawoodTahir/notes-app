"""
Describe what you need; this goes and builds it.

A brief in plain words — "the main arguments for and against the historicity
of the Qur'an's preservation" — becomes searches, a shortlist, notes on each
chosen video, and a set of saved notes to review. The user picks nothing
along the way.

The design problem is that every stage can be wrong in a way the next stage
cannot detect. A bad query returns plausible videos about the wrong thing; a
bad pick reads well and answers nothing. So the run keeps its reasoning:
what it searched, what it chose, what it rejected and why. A result you
cannot audit is worse than a shorter one you can.

Deliberately small by default. Each video is a transcript fetch plus several
model calls, and the per-minute token budget is the binding constraint — so
three good videos beat ten the provider will refuse halfway through.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from .notes import NoteError, NoteRequest, _dispatch, _env_int, generate_notes
from .transcript import TranscriptError, get_transcript, search_videos

MAX_VIDEOS = _env_int("ORCH_MAX_VIDEOS", 3)
MAX_QUERIES = _env_int("ORCH_MAX_QUERIES", 3)
CANDIDATES_PER_QUERY = _env_int("ORCH_CANDIDATES", 6)
# Most the selector can weigh in a single reply.
SHORTLIST = _env_int("ORCH_SHORTLIST", 12)
PLAN_TOKENS = _env_int("ORCH_PLAN_TOKENS", 700)
# The shortlist reply needs room for a line per candidate. At 700 it was cut
# off part-way through, which does not fail loudly — the run simply found no
# decisions to read and reported that nothing was suitable.
SELECT_TOKENS = _env_int("ORCH_SELECT_TOKENS", 1_800)

Progress = Callable[..., None]


PLAN_SYSTEM = """\
You are planning a research run. The user has described what they want notes
about. Turn it into a search plan.

Reply with exactly these fields, each on its own line:

QUERIES: two or three search queries, separated by ` | `. Write them the way
someone would actually search a video site — plain words, no quotes, no
boolean operators. Make them cover different angles of the request rather
than rephrasing one angle three times.
LOOKING_FOR: one sentence describing what makes a video a good match here.
AVOID: one sentence describing what would look relevant but is not.
STYLE: one short instruction for how the notes should be written, drawn from
what the user asked for. If they implied a debate or disagreement, say to lay
out each claim, its reasoning and any counterpoint.

Write nothing else."""


SELECT_SYSTEM = """\
You are choosing which videos to take notes on.

You are given a request, what to look for, what to avoid, and a numbered list
of candidates with their titles, channels and durations. Choose the ones
worth reading in full.

Reply with one line per candidate you are choosing:

PICK <number>: <one clause on what this one contributes that the others do not>

Then, for anything notable you are leaving out:

SKIP <number>: <one clause on why>

Rules: choose at most {max_videos}. Prefer substance over popularity. Prefer
a spread of positions over several videos making the same point. Reject
anything under two minutes — it will not carry an argument. If nothing is
suitable, write only: NONE"""


def _parse_lines(raw: str, keys: tuple[str, ...]) -> dict:
    """
    Read `KEY: value` lines out of a reply.

    Line-oriented on purpose. A JSON object that gets truncated mid-reply —
    which happens often enough to matter — is unparseable in one piece, while
    a truncated list of lines still yields every line that did arrive.
    """
    want = {k.upper(): k for k in keys}
    out = {k: "" for k in keys}
    current = None
    for line in (raw or "").splitlines():
        head, sep, rest = line.partition(":")
        name = want.get(head.strip().upper().replace(" ", "_"))
        if sep and name:
            out[name] = rest.strip()
            current = name
        elif current and line.strip():
            out[current] = (out[current] + " " + line.strip()).strip()
    return out


def _plan(brief: str, provider: str, model: Optional[str]) -> dict:
    raw = _dispatch(provider, PLAN_SYSTEM, f"The user wants: {brief}",
                    model, PLAN_TOKENS)
    f = _parse_lines(raw, ("queries", "looking_for", "avoid", "style"))
    queries = [q.strip() for q in (f.get("queries") or "").split("|") if q.strip()]
    if not queries:
        queries = [brief.strip()]
    return {
        "queries": queries[:MAX_QUERIES],
        "looking_for": (f.get("looking_for") or "").strip(),
        "avoid": (f.get("avoid") or "").strip(),
        "style": (f.get("style") or "").strip(),
    }


def _gather(queries: list[str], seen_urls: set[str]) -> list[dict]:
    """Search each query, keeping the first sighting of each video."""
    out: list[dict] = []
    for q in queries:
        try:
            hits = search_videos(q, CANDIDATES_PER_QUERY)
        except (TranscriptError, Exception):
            continue
        for h in hits:
            url = (h.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            h["query"] = q
            out.append(h)
    return out


def _describe(c: dict, n: int) -> str:
    dur = c.get("duration")
    mins = f"{round(dur / 60)} min" if isinstance(dur, (int, float)) and dur else "unknown length"
    views = c.get("views")
    seen = f", {views:,} views" if isinstance(views, int) and views else ""
    return (f"{n}. {c.get('title', 'untitled')}\n"
            f"   {c.get('uploader', 'unknown channel')} · {mins}{seen}")


def _select(brief: str, plan: dict, candidates: list[dict],
            provider: str, model: Optional[str]) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    listing = "\n".join(_describe(c, i + 1) for i, c in enumerate(candidates))
    user = (f"Request: {brief}\n\n"
            f"Look for: {plan['looking_for']}\n"
            f"Avoid: {plan['avoid']}\n\n"
            f"Candidates:\n{listing}")
    raw = _dispatch(provider, SELECT_SYSTEM.format(max_videos=MAX_VIDEOS),
                    user, model, SELECT_TOKENS)

    picks: list[tuple[int, str]] = []
    skips: list[tuple[int, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        for tag, bucket in (("PICK", picks), ("SKIP", skips)):
            if not line.upper().startswith(tag):
                continue
            rest = line[len(tag):].strip()
            num, _, why = rest.partition(":")
            try:
                idx = int("".join(ch for ch in num if ch.isdigit())) - 1
            except ValueError:
                continue
            if 0 <= idx < len(candidates):
                bucket.append((idx, why.strip()))
            break
    return picks[:MAX_VIDEOS], skips


def run(
    brief: str,
    *,
    provider: str = "openai",
    model: Optional[str] = None,
    max_videos: int = 0,
    known_urls: Optional[set[str]] = None,
    on_note: Optional[Callable[[dict], dict]] = None,
    progress: Optional[Progress] = None,
) -> dict:
    """
    Plan, search, choose, and take notes on each chosen video.

    `on_note` is called with each finished note and returns the saved row;
    saving lives with the caller so this module stays free of the database.
    """
    say = progress or (lambda *_a, **_k: None)
    brief = (brief or "").strip()
    if not brief:
        raise NoteError("Describe what you want notes about.")

    limit = max_videos or MAX_VIDEOS

    say("planning the search", 4)
    plan = _plan(brief, provider, model)

    say(f"searching: {plan['queries'][0]}", 10)
    seen = set(known_urls or ())
    candidates = _gather(plan["queries"], set())
    fresh = [c for c in candidates if c.get("url") not in seen]
    if not fresh:
        return {"brief": brief, "plan": plan, "candidates": len(candidates),
                "chosen": [], "skipped": [], "notes": [],
                "message": "Nothing new turned up for that — everything found is already saved."}

    # The shortlist has to fit in one reply. An over-long listing does not
    # fail loudly — the reply is simply cut off before any PICK line, and the
    # run reports that nothing was suitable when in fact nothing was read.
    fresh = fresh[:SHORTLIST]
    say(f"judging {len(fresh)} candidates", 22)
    picks, skips = _select(brief, plan, fresh, provider, model)
    if not picks:
        # No picks and no skips means the reply itself was unusable, which is
        # a different thing from a weak set of candidates — and the user
        # should be able to tell which happened.
        unusable = not skips
        return {"brief": brief, "plan": plan, "candidates": len(fresh),
                "chosen": [],
                "skipped": [{"title": fresh[i]["title"], "why": w} for i, w in skips],
                "notes": [],
                "message": ("The shortlist could not be judged — try again, or "
                            "narrow the description." if unusable else
                            "Nothing among the results was a good enough match "
                            "to be worth reading.")}

    picks = picks[:limit]
    notes_out, failures = [], []
    span_from, span_to = 28.0, 94.0
    per = (span_to - span_from) / max(1, len(picks))

    for n, (idx, why) in enumerate(picks):
        c = fresh[idx]
        base = span_from + per * n
        title = (c.get("title") or "")[:60]
        say(f"reading {n + 1} of {len(picks)}: {title}", base)
        try:
            tr = get_transcript(c["url"])
            say(f"writing notes {n + 1} of {len(picks)}", base + per * 0.45)
            md = generate_notes(NoteRequest(
                transcript=tr.text, title=tr.title,
                topic=brief, note_kind="", extra=plan["style"],
                provider=provider, model=model,
            ))
            record = {
                "url": c["url"], "title": tr.title or c.get("title", ""),
                "platform": getattr(tr, "platform", ""),
                "source": getattr(tr, "source", ""),
                "notes": md, "transcript": tr.text,
                "thumbnail": getattr(tr, "thumbnail", ""),
                "why": why, "query": c.get("query", ""),
            }
            if on_note:
                record["saved"] = on_note(record)
            record.pop("transcript", None)
            notes_out.append(record)
        except (TranscriptError, NoteError) as e:
            failures.append({"title": c.get("title", ""), "url": c.get("url", ""),
                             "why": str(e)[:200]})
        except Exception as e:
            failures.append({"title": c.get("title", ""), "url": c.get("url", ""),
                             "why": f"{type(e).__name__}: {str(e)[:160]}"})

    return {
        "brief": brief,
        "plan": plan,
        "candidates": len(fresh),
        "chosen": [{"title": fresh[i].get("title", ""), "url": fresh[i].get("url", ""),
                    "why": w} for i, w in picks],
        "skipped": [{"title": fresh[i].get("title", ""), "why": w} for i, w in skips],
        "notes": notes_out,
        "failed": failures,
        "message": (f"Read {len(notes_out)} of {len(picks)} chosen videos."
                    + (f" {len(failures)} could not be read." if failures else "")),
    }
