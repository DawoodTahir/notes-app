"""
Note generation from a transcript.

- Provider-agnostic: supports Anthropic (Claude) and OpenAI, selected via
  env (`NOTE_PROVIDER`) or per-request.
- Structured output: the model is instructed to return Markdown with a
  consistent skeleton (Overview / Key Points / Detailed Notes / Takeaways),
  shaped by the user's topic context and the kind of notes they asked for.
- Long transcripts: if the transcript is very long, it is chunked and
  summarised map-reduce style so we never blow the context window.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional


# Roughly 4 chars per token. These default low because free tiers meter hard:
# Groq's free tier allows 8k tokens per minute *including* the reply, so a
# request much over ~12k chars is rejected outright. Raise them on a paid tier
# for fewer round-trips.
#   NOTE_CHAR_BUDGET  chars of transcript allowed in one request
#   NOTE_MAX_TOKENS   ceiling on the reply
def _env_int(name: str, default: int) -> int:
    """
    An integer from the environment, or the default.

    No floor is applied here. There used to be a `max(1000, …)`, which was
    presumably meant to stop a character budget being set uselessly low — but
    it applies to whatever the helper is asked for, so every count that used
    it silently became at least a thousand: "take notes on 3 videos" meant a
    thousand. Budgets clamp themselves where they are used.
    """
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


SINGLE_PASS_CHAR_LIMIT = _env_int("NOTE_CHAR_BUDGET", 12_000)
CHUNK_CHAR_SIZE = SINGLE_PASS_CHAR_LIMIT
MAX_OUTPUT_TOKENS = _env_int("NOTE_MAX_TOKENS", 2_000)

# Per-minute token budgets mean a burst of chunk calls trips the limit even
# when each one fits. Retry with a wait rather than failing the whole job.
_RATE_LIMIT_MARKERS = ("rate_limit", "429", "413", "too large", "tokens per minute", "tpm")
_RETRY_WAITS = (20, 40, 65)


class NoteError(Exception):
    pass


@dataclass
class NoteRequest:
    transcript: str
    title: str
    topic: str = ""       # legacy: explicit topic/context, usually unset now
    note_kind: str = ""   # legacy: explicit style, usually unset now
    extra: str = ""       # optional free-form steer from the user
    provider: Optional[str] = None
    model: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# Prompt building
# ─────────────────────────────────────────────────────────────

STRUCTURE_SPEC = """\
Structure the notes as GitHub-flavoured Markdown using exactly these top-level
sections, in this order (omit a section only if it genuinely has no content):

# {title}

## Overview
A 2–4 sentence framing of what the video covers, oriented to the user's topic.

## Key Points
A tight bulleted list of the most important takeaways (5–12 bullets).

## Detailed Notes
The substantive notes, organised under `###` sub-headings that reflect the
actual structure/flow of the content. Use nested bullets for detail. This is
the main body — be thorough and faithful to the transcript.

## Takeaways & Action Items
Concrete conclusions, next steps, or things worth remembering.
"""


# What the notes are, unless the user says otherwise. These used to be two
# form fields; almost every answer was some paraphrase of "good study notes",
# so they are built in and the user only writes something when they want to
# steer away from this default.
DEFAULT_BRIEF = """\
Take thorough, well-organised study notes on whatever this video is actually
about. Work out the subject from the transcript rather than assuming one.

- Lead with what a reader most needs to know.
- Keep the substance: definitions, numbers, names, examples, and the reasoning
  that connects them. Compress digressions, but never drop content that carries
  meaning.
- Preserve the speaker's actual claims and distinctions; do not flatten a
  nuanced argument into a generic summary.
- Where the video walks through a process, keep the steps in order."""


def _system_prompt(topic: str, note_kind: str, extra: str = "") -> str:
    brief = DEFAULT_BRIEF
    if topic.strip():
        brief += f"\n\nThe reader gave this topic/context: {topic.strip()!r}"
    if note_kind.strip():
        brief += f"\n\nThe style of notes they want: {note_kind.strip()!r}"

    steer = ""
    if extra.strip():
        steer = (
            "\n\nTHE READER ADDED THESE INSTRUCTIONS. They take precedence over "
            "the defaults above wherever the two disagree:\n"
            f'"""{extra.strip()}"""\n'
        )

    return f"""\
You are an expert note-taker. You turn a raw video transcript into clean,
well-organised, accurate notes.

{brief}
{steer}
Rules:
- Base everything strictly on the transcript. Do not invent facts. If the
  transcript is unclear or auto-captioned with errors, infer sensibly and
  smooth obvious transcription artefacts, but never fabricate content.
- Prioritise material relevant to the user's topic; you may lightly condense
  tangents, but don't drop substance they'd want.
- Follow the user's requested note style within the required structure.
- Write in clear, concise prose and bullets. No filler, no "in this video".
"""


def _user_prompt(title: str, transcript: str) -> str:
    return (
        f"Video title: {title}\n\n"
        f"{STRUCTURE_SPEC.format(title=title)}\n\n"
        "Here is the transcript:\n\n"
        "-----BEGIN TRANSCRIPT-----\n"
        f"{transcript}\n"
        "-----END TRANSCRIPT-----\n"
    )


# ─────────────────────────────────────────────────────────────
# Provider adapters
# ─────────────────────────────────────────────────────────────

def _call_anthropic(system: str, user: str, model: Optional[str], max_tokens: int) -> str:
    try:
        import anthropic
    except Exception as e:  # pragma: no cover
        raise NoteError("The `anthropic` package is not installed.") from e

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise NoteError("ANTHROPIC_API_KEY is not set.")

    model = model or os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-20250514"
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "".join(parts).strip()


def _call_openai(system: str, user: str, model: Optional[str], max_tokens: int) -> str:
    try:
        from openai import OpenAI
    except Exception as e:  # pragma: no cover
        raise NoteError("The `openai` package is not installed.") from e

    # OPENAI_BASE_URL lets this path talk to any OpenAI-compatible endpoint
    # (Groq, OpenRouter, Gemini's compat layer, a local Ollama, …). Local
    # servers ignore the key, so a placeholder is enough there.
    base_url = os.environ.get("OPENAI_BASE_URL") or None

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        if not base_url:
            raise NoteError("OPENAI_API_KEY is not set.")
        key = "not-needed"

    model = model or os.environ.get("OPENAI_MODEL") or "gpt-4o"
    client = OpenAI(api_key=key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def _dispatch(provider: str, system: str, user: str, model: Optional[str], max_tokens: int) -> str:
    """Call the provider, waiting out per-minute rate limits instead of failing."""
    last: Optional[Exception] = None
    for attempt, wait in enumerate((0,) + _RETRY_WAITS):
        if wait:
            time.sleep(wait)
        try:
            return _dispatch_once(provider, system, user, model, max_tokens)
        except NoteError:
            raise
        except Exception as e:
            msg = str(e).lower()
            if not any(m in msg for m in _RATE_LIMIT_MARKERS):
                raise
            last = e
    raise NoteError(
        "The model kept refusing the request as too large or rate-limited. "
        "Lower NOTE_CHAR_BUDGET in .env, or use a provider with a higher "
        f"per-minute limit. Last error: {last}"
    )


def _dispatch_once(provider: str, system: str, user: str, model: Optional[str], max_tokens: int) -> str:
    provider = (provider or "anthropic").lower()
    if provider == "anthropic":
        return _call_anthropic(system, user, model, max_tokens)
    if provider == "openai":
        return _call_openai(system, user, model, max_tokens)
    raise NoteError(f"Unknown provider: {provider!r} (use 'anthropic' or 'openai').")


# ─────────────────────────────────────────────────────────────
# Map-reduce for long transcripts
# ─────────────────────────────────────────────────────────────

def _chunk(text: str, size: int) -> list[str]:
    words = text.split()
    chunks, cur, cur_len = [], [], 0
    for w in words:
        cur.append(w)
        cur_len += len(w) + 1
        if cur_len >= size:
            chunks.append(" ".join(cur))
            cur, cur_len = [], 0
    if cur:
        chunks.append(" ".join(cur))
    return chunks


# The prompt scaffolding (system prompt + structure spec) costs roughly this
# many characters, and counts against the same budget as the transcript.
_PROMPT_OVERHEAD_CHARS = 2_500


def _condense_chunks(text: str, req: NoteRequest, provider: str,
                     report: Optional[Callable[[int, int], None]] = None) -> str:
    chunks = _chunk(text, max(2_000, CHUNK_CHAR_SIZE - _PROMPT_OVERHEAD_CHARS))
    system = (
        "You are condensing part of a long video transcript so it can later be "
        "turned into notes. Preserve every substantive point, name, number, and "
        "example relevant to this topic: "
        f"\"{req.topic.strip() or 'general'}\". Output dense bullet points only."
    )
    partials = []
    for i, ch in enumerate(chunks, 1):
        if report:
            report(i, len(chunks))
        user = f"Transcript part {i} of {len(chunks)}:\n\n{ch}"
        partials.append(_dispatch(provider, system, user, req.model,
                                  max_tokens=MAX_OUTPUT_TOKENS))
    return "\n\n".join(partials)


# ─────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────

# Where note generation sits inside the job as a whole: the transcript is
# already fetched by the time this runs, and saving follows it.
_NOTES_FROM, _NOTES_TO = 30.0, 92.0


def generate_notes(req: NoteRequest,
                   progress: Optional[Callable[..., None]] = None) -> str:
    """
    Turn a transcript into notes.

    `progress(step, percent)` is called as the work proceeds. Long transcripts
    are condensed in chunks, and that loop is the only part whose length is
    known in advance — so it is the only part that can report a real fraction.
    A short transcript is a single model call with nothing to divide, and
    reports no percentage rather than inventing one.
    """
    provider = (req.provider or os.environ.get("NOTE_PROVIDER") or "anthropic").lower()

    def say(step: str, pct: Optional[float] = None) -> None:
        if progress:
            progress(step, pct)

    # Condense repeatedly until the transcript fits one request. A single pass
    # is not always enough: a three-hour talk condenses to something still over
    # budget, and the reduce step would then fail the same way the map did.
    transcript = req.transcript
    budget = max(2_000, SINGLE_PASS_CHAR_LIMIT - _PROMPT_OVERHEAD_CHARS)

    # Estimate the condense rounds up front so the bar is scaled to the whole
    # job instead of restarting on each round. A round shrinks the text by
    # roughly a third; four is the hard cap in the loop below.
    rounds_expected, probe = 0, len(transcript)
    while probe > budget and rounds_expected < 4:
        rounds_expected += 1
        probe //= 3

    # Reserve the last quarter of the span for the final write-up.
    condense_span = (_NOTES_TO - _NOTES_FROM) * 0.75 if rounds_expected else 0.0

    done_rounds = 0
    shown = _NOTES_FROM          # never let the bar go backwards

    for _ in range(4):
        if len(transcript) <= budget:
            break

        def report(i: int, n: int, _r=done_rounds) -> None:
            nonlocal shown
            # The round count is only an estimate, and a transcript that needs
            # four rounds when three were predicted must not pin the bar at
            # the cap and sit there for the rest of the work. An exponential
            # approach rises quickly, keeps rising when the estimate is
            # overshot, and never actually reaches the end of the phase — so
            # the bar is always moving and never claims to be finished early.
            frac = (_r + i / max(1, n)) / max(1, rounds_expected)
            eased = 1.0 - math.exp(-1.7 * frac)
            shown = max(shown, _NOTES_FROM + condense_span * eased)
            say(f"condensing part {i} of {n}", shown)

        condensed = _condense_chunks(transcript, req, provider, report)
        done_rounds += 1
        if len(condensed) >= len(transcript):
            transcript = condensed[:budget]     # not shrinking; cut losses
            break
        transcript = condensed

    say("writing notes", max(shown, _NOTES_FROM + condense_span) if condense_span else None)
    system = _system_prompt(req.topic, req.note_kind, req.extra)
    user = _user_prompt(req.title, transcript)
    notes = _dispatch(provider, system, user, req.model, max_tokens=MAX_OUTPUT_TOKENS)
    if not notes:
        raise NoteError("The model returned an empty response.")
    return notes


# ─────────────────────────────────────────────────────────────
# Follow-up chat about generated notes
# ─────────────────────────────────────────────────────────────

REVISE_SYSTEM = """\
You are revising a set of notes on a video, following an instruction from
the person who will keep them.

You are given the current notes, the transcript they were made from, and the
instruction.

Rules:

- Return the complete revised notes. Not a diff, not a summary of what you
  changed, not a commentary — the whole document, ready to replace the old
  one.
- Change only what the instruction asks for. Everything else keeps its
  wording, its order and its structure.
- When asked to add or expand something, take it from the transcript. The
  notes were condensed from it and detail was dropped on the way; that
  detail is what you are recovering.
- Never invent. If the transcript does not support what was asked for, leave
  the notes as they are and add one line at the very end:
  `> Not in the video: <what was asked for>`
- Keep the existing Markdown conventions: the same heading levels, the same
  bullet style, bold where it was used.
- Keep any `<div class="ayah">` blocks exactly as they are, including the
  Arabic inside them.

Output Markdown only."""


def revise_notes(notes: str, instruction: str, transcript: str = "",
                 title: str = "", provider: Optional[str] = None,
                 model: Optional[str] = None) -> str:
    """
    Rewrite notes to follow one instruction.

    The transcript goes along because the usual request is to put back a
    nuance the condensing dropped — "keep what he said about the exceptions"
    — and that detail only exists upstream of the notes.
    """
    instruction = (instruction or "").strip()
    if not instruction:
        raise NoteError("Say what you want changed.")
    if not (notes or "").strip():
        raise NoteError("There are no notes to revise.")

    provider = (provider or os.environ.get("NOTE_PROVIDER") or "anthropic").lower()

    budget = max(2_000, SINGLE_PASS_CHAR_LIMIT - len(notes) - _PROMPT_OVERHEAD_CHARS)
    excerpt = (transcript or "")[:budget]

    user = (f"Instruction: {instruction}\n\n"
            f"Current notes for \"{title or 'this video'}\":\n\n{notes}\n\n"
            + (f"Transcript:\n\n{excerpt}" if excerpt else
               "No transcript is available; work from the notes alone."))

    out = _dispatch(provider, REVISE_SYSTEM, user, model, MAX_OUTPUT_TOKENS)
    out = (out or "").strip()
    if not out:
        raise NoteError("The model returned nothing.")
    # A model that explains itself instead of producing the document is a
    # failure that would otherwise overwrite good notes with chatter.
    if len(out) < len(notes) * 0.4 and "Not in the video" not in out:
        raise NoteError("The revision came back too short to be the full notes. "
                        "Nothing was changed — try wording the instruction differently.")
    return out


CHAT_SYSTEM = """\
You are helping someone understand a set of notes you produced from a video.

Answer their questions using the NOTES and the TRANSCRIPT EXCERPT below.

Rules:
- Ground every answer in that material. Quote or point to the relevant part
  when it helps.
- If the material genuinely does not cover something, say so plainly and, if
  useful, mark clearly that you are adding outside context.
- Be direct and concise. Short paragraphs or tight bullets — no preamble, no
  restating the question.
- Markdown is fine for structure, but do not wrap the whole reply in a heading.
"""


@dataclass
class ChatRequest:
    question: str
    notes: str
    transcript: str = ""
    title: str = ""
    history: Optional[list[dict]] = None      # [{"role": "user"|"assistant", "content": str}]
    provider: Optional[str] = None
    model: Optional[str] = None


def answer_question(req: ChatRequest) -> str:
    """Answer a follow-up question about already-generated notes."""
    question = (req.question or "").strip()
    if not question:
        raise NoteError("Ask a question first.")

    provider = (req.provider or os.environ.get("NOTE_PROVIDER") or "anthropic").lower()

    # The notes are the primary source and are short; the transcript is the
    # backup for detail the notes dropped. Spend whatever budget the notes and
    # conversation leave over on a transcript excerpt.
    history = list(req.history or [])[-6:]          # last 3 exchanges
    history_chars = sum(len(h.get("content", "")) for h in history)

    budget = max(2_000, SINGLE_PASS_CHAR_LIMIT - _PROMPT_OVERHEAD_CHARS)
    notes = (req.notes or "")[:budget]
    room = budget - len(notes) - history_chars - len(question)
    excerpt = (req.transcript or "")[:room] if room > 500 else ""

    context = f"NOTES\n-----\n{notes}\n"
    if excerpt:
        truncated = " (truncated)" if len(excerpt) < len(req.transcript or "") else ""
        context += f"\nTRANSCRIPT EXCERPT{truncated}\n------------------\n{excerpt}\n"

    convo = ""
    for turn in history:
        who = "User" if turn.get("role") == "user" else "You"
        convo += f"{who}: {turn.get('content', '').strip()}\n\n"

    user = (
        f"Video: {req.title or 'untitled'}\n\n"
        f"{context}\n"
        + (f"\nEARLIER IN THIS CONVERSATION\n----------------------------\n{convo}" if convo else "")
        + f"\nTheir question: {question}"
    )

    answer = _dispatch(provider, CHAT_SYSTEM, user, req.model, max_tokens=MAX_OUTPUT_TOKENS)
    if not answer:
        raise NoteError("The model returned an empty response.")
    return answer


# ─────────────────────────────────────────────────────────────
# Filing a saved note under a category
# ─────────────────────────────────────────────────────────────

def standpoint() -> str:
    """
    The position the library is kept from.

    Notes like these are not an encyclopaedia; they are preparation. What
    matters when you open a folder is not which academic field the material
    belongs to but who you would be talking to. So the owner's own position
    is the baseline and never a folder of its own — the folder is whatever
    that position is being asked to answer.
    """
    return (os.environ.get("NOTES_STANDPOINT") or "").strip()


def _classify_system(view: str) -> str:
    frame = (
        f"The library belongs to someone whose own position is: {view}.\n"
        "That position is the baseline. It is never a folder, and neither is\n"
        "any academic field it sits in. File by WHO OR WHAT THE MATERIAL\n"
        "ANSWERS — the tradition, movement, claim or school being engaged —\n"
        "so that opening a folder shows what to say to that interlocutor.\n\n"
        "  Notes arguing the Bible does not teach the Trinity   -> Christianity\n"
        "  Notes answering Ahmadi claims about prophethood      -> Ahmadiyya\n"
        "  Notes on Vedic chronology against Abrahamic claims   -> Hinduism\n"
        "  Notes answering arguments that God does not exist    -> Atheism\n"
        "  Notes on an internal question with no opponent       -> name the\n"
        "     question's own field, e.g. Qur'anic sciences\n\n"
        "Material that merely expounds the baseline position, with nothing\n"
        "being answered, goes to the field it belongs to — not to a folder\n"
        "named after the baseline itself.\n"
    ) if view else (
        "Name folders after the subject the material is ABOUT — the tradition,\n"
        "field, person or topic it concerns — not the format it came in.\n"
    )

    return f"""\
You file a set of notes into the right folder in someone's library, and give
them a subject line that says what the material is actually about.

{frame}
You are given the folders that already exist. Strongly prefer an existing one:
a library is only useful if related material lands together. Create a new folder
only when the notes genuinely do not belong in any of them.

"YouTube", "Debate", "Video notes", "Interesting" are never folders.

SUBJECT: the question or claim the material actually turns on — 4 to 9 words.
Video titles are written to be clicked, not to describe; ignore the title and
read the notes. Where the material is a debate or an argument, name the point
in dispute and, if one side is defending a position, whose position it is.

  "Dr Zakir Naik uses Bible to prove Trinity is a Lie"
     -> "Muslim case that the Bible omits the Trinity"
  "Hinduism came before you guys! Mansur Vs Hindu/Buddhist/Indian"
     -> "Whether Vedic tradition predates Abrahamic revelation"

Sentence case, no trailing full stop, no quotation marks.

Return ONLY a JSON object:
{{"category":"<folder name, 1-3 words, Title Case>",
 "subject":"<the subject line>",
 "reason":"<one sentence citing what in the notes puts it there>",
 "is_new": true|false}}

The reason is kept as the justification for the filing, so cite the actual
content — a name, a claim, a scripture, a concept — not a generality.
"""


def _parse_fields(raw: str, keys: tuple[str, ...]) -> dict:
    """
    Read named string fields out of a model reply.

    Strict JSON first. Failing that, pull each field with a regex: replies get
    truncated mid-object often enough that insisting on a closing brace throws
    away answers that were perfectly usable.
    """
    import json as _json
    import re as _re

    text = raw or ""
    fence = _re.search(r"```(?:json)?\s*(.+?)```", text, _re.S)
    if fence:
        text = fence.group(1)

    m = _re.search(r"\{.*\}", text, _re.S)
    if m:
        try:
            data = _json.loads(m.group(0))
            if isinstance(data, dict):
                return {k: data.get(k, "") for k in keys}
        except Exception:
            pass

    out, found = {}, False
    for k in keys:
        hit = _re.search(rf'"{k}"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if hit:
            out[k] = hit.group(1).replace('\\"', '"')
            found = True
        else:
            out[k] = ""
    return out if found else {}


def classify_note(title: str, notes: str, existing: Optional[list[str]] = None,
                  provider: Optional[str] = None, model: Optional[str] = None
                  ) -> tuple[str, str, str]:
    """Return (category, reason, subject). Falls back rather than raising."""
    provider = (provider or os.environ.get("NOTE_PROVIDER") or "anthropic").lower()
    folders = [c for c in (existing or []) if c and c != "Unfiled"]

    budget = max(1_500, SINGLE_PASS_CHAR_LIMIT - _PROMPT_OVERHEAD_CHARS)
    user = (
        ("Existing folders: " + ", ".join(folders) if folders else "No folders yet.")
        + f"\n\nTitle: {title or 'Untitled'}\n\nNotes:\n\"\"\"{(notes or '')[:budget]}\"\"\"\n\nJSON:"
    )
    try:
        raw = _dispatch(provider, _classify_system(standpoint()), user, model, max_tokens=500)
    except Exception:
        return "Unfiled", "", ""

    data = _parse_fields(raw, ("category", "subject", "reason"))
    if not data:
        return "Unfiled", "", ""

    category = " ".join(str(data.get("category", "")).split())[:48].strip() or "Unfiled"
    reason = " ".join(str(data.get("reason", "")).split())[:400]
    subject = " ".join(str(data.get("subject", "")).split()).strip(' ."\'')[:110]

    # Re-use an existing folder when the model only differs by case or spacing.
    for f in folders:
        if f.lower() == category.lower():
            category = f
            break
    return category, reason, subject
