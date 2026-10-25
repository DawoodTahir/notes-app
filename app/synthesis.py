"""
Combine several saved notes on one subject into a single organised document.

The library already files notes into folders — Christianity, Hinduism, and so
on. This is the next step: take everything in a folder and write one piece
that says what the sources collectively argue, where they agree, and where
they contradict each other.

Two things shape the design.

**Attribution is the point.** A merged document that blends claims without
saying who made them is worth less than the notes it came from. Every claim
carries a source marker, and the source list itself is assembled in Python
from the database rows — the model never writes a URL, so it cannot invent
one. That keeps the output usable as evidence rather than as a summary.

**It has to survive a small token budget.** A folder can hold more material
than any single request can carry, so this runs map-reduce like the note
writer does: each batch of notes is reduced to its claims, then the claims
are organised into the final document.
"""

from __future__ import annotations

import os
import re
from typing import Callable, Optional

from .notes import NoteError, _dispatch, _env_int

# Deliberately below the note budget: several notes per call still has to fit
# inside one request, and the per-minute ceiling is the real constraint.
BATCH_CHAR_BUDGET = _env_int("DIGEST_BATCH_CHARS", 8_000)
MAX_NOTES = _env_int("DIGEST_MAX_NOTES", 40)
MAX_OUTPUT_TOKENS = _env_int("DIGEST_MAX_TOKENS", 2_400)

Progress = Callable[[str], None]


EXTRACT_SYSTEM = """\
You are reading study notes taken from several videos on one subject.

For each source you are given, pull out the substantive claims it makes —
the positions it argues for, the evidence it offers, and the conclusions it
reaches. Ignore pleasantries, introductions and anything about the video
itself.

Write each claim as one line:

[S<n>] <the claim, stated plainly in one sentence>

Use the source marker exactly as it appears in the heading above the notes.
Keep every name, number, date and citation exactly as written. Do not merge
claims from different sources onto one line. Do not add commentary, and do
not write anything except these lines."""


ORGANISE_SYSTEM = """\
You are given claims extracted from several sources on one subject, each
tagged with a source marker like [S1].

Write a single organised document in Markdown with these sections:

## In short
Three or four sentences: what these sources, taken together, are arguing.

## The main threads
Group the claims by what they are about — not by which source they came
from. Use a `### ` heading per theme. Under each, write the substance as
prose or bullets, and put the source marker in brackets at the end of every
sentence or bullet that rests on a source, like [S2] or [S1][S3]. Every
factual statement must carry at least one marker.

## Where the sources agree
Points more than one source supports, each with all of its markers.

## Where they disagree
Direct contradictions or differences of emphasis. State both positions and
mark each. If there are none, write: The sources do not contradict each
other on anything substantial.

## Open questions
What these sources leave unresolved. Omit this section if nothing stands out.

Rules: use only the claims given. Never invent a fact, a name or a number.
Never write a URL — source markers only. Do not add a sources list; one is
appended for you. Do not write a title; one is added for you."""


def _source_label(row: dict, n: int) -> str:
    """How a source is introduced to the model, and listed to the reader."""
    return (row.get("subject") or row.get("title") or f"Source {n}").strip()


def _batch(rows: list[dict]) -> list[list[tuple[int, dict]]]:
    """Group notes so each batch fits inside one request."""
    batches: list[list[tuple[int, dict]]] = []
    cur: list[tuple[int, dict]] = []
    size = 0
    for i, row in enumerate(rows, start=1):
        cost = len(row.get("notes") or "") + 200      # heading overhead
        if cur and size + cost > BATCH_CHAR_BUDGET:
            batches.append(cur)
            cur, size = [], 0
        cur.append((i, row))
        size += cost
    if cur:
        batches.append(cur)
    return batches


def _extract(batch: list[tuple[int, dict]], provider: str, model: Optional[str]) -> str:
    parts = []
    for n, row in batch:
        parts.append(f"### [S{n}] {_source_label(row, n)}\n\n{(row.get('notes') or '').strip()}")
    return _dispatch(provider, EXTRACT_SYSTEM, "\n\n---\n\n".join(parts),
                     model, MAX_OUTPUT_TOKENS).strip()


def _sources_section(rows: list[dict]) -> str:
    """
    Built here, not by the model.

    The whole value of the document is that a claim can be traced back, so
    the links have to be real. A model asked to reproduce URLs will sooner or
    later produce a plausible one that does not exist.
    """
    lines = ["## Sources", ""]
    for n, row in enumerate(rows, start=1):
        label = _source_label(row, n)
        url = (row.get("url") or "").strip()
        platform = (row.get("platform") or "").strip()
        title = (row.get("title") or "").strip()
        bits = [f"**[S{n}]** {label}"]
        if title and title != label:
            bits.append(f" — {title}")
        if platform:
            bits.append(f" ({platform})")
        lines.append("".join(bits))
        if url:
            lines.append(f"  {url}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _used_markers(doc: str) -> set[int]:
    return {int(m) for m in re.findall(r"\[S(\d+)\]", doc)}


def synthesise(
    rows: list[dict],
    topic: str,
    provider: str = "openai",
    model: Optional[str] = None,
    progress: Optional[Progress] = None,
) -> dict:
    """
    Merge the notes in `rows` into one document about `topic`.

    Returns the document, the source list, and which sources actually earned
    a mention — a source the model never cited is worth knowing about.
    """
    say = progress or (lambda _s: None)

    rows = [r for r in rows if (r.get("notes") or "").strip()]
    if not rows:
        raise NoteError("There are no notes in that folder to combine.")
    if len(rows) == 1:
        raise NoteError("That folder has only one note — there is nothing to combine yet.")
    rows = rows[:MAX_NOTES]

    batches = _batch(rows)
    claims: list[str] = []
    for i, batch in enumerate(batches, start=1):
        say(f"reading sources {i} of {len(batches)}")
        claims.append(_extract(batch, provider, model))

    body = "\n".join(c for c in claims if c).strip()
    if not body:
        raise NoteError("Could not pull any claims out of those notes.")

    # If the claim list is itself too big, condense it before organising.
    rounds = 0
    while len(body) > BATCH_CHAR_BUDGET and rounds < 3:
        rounds += 1
        say(f"condensing (pass {rounds})")
        pieces = [body[i:i + BATCH_CHAR_BUDGET] for i in range(0, len(body), BATCH_CHAR_BUDGET)]
        body = "\n".join(
            _dispatch(provider, EXTRACT_SYSTEM,
                      "Condense these claim lines, keeping every source marker:\n\n" + p,
                      model, MAX_OUTPUT_TOKENS).strip()
            for p in pieces
        ).strip()

    say("organising")
    doc = _dispatch(
        provider, ORGANISE_SYSTEM,
        f"Subject: {topic}\n\nClaims:\n\n{body}",
        model, MAX_OUTPUT_TOKENS,
    ).strip()

    cited = _used_markers(doc)
    uncited = [n for n in range(1, len(rows) + 1) if n not in cited]

    document = f"# {topic}\n\n{doc}\n\n{_sources_section(rows)}\n"
    return {
        "topic": topic,
        "document": document,
        "sources": [
            {"marker": f"S{n}", "label": _source_label(r, n), "title": r.get("title", ""),
             "url": r.get("url", ""), "platform": r.get("platform", ""),
             "id": r.get("id", ""), "cited": n in cited}
            for n, r in enumerate(rows, start=1)
        ],
        "note_count": len(rows),
        "uncited": uncited,
    }
