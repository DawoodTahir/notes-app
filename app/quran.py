"""
Attach the Arabic of any Qur'anic verse a note cites.

The model never writes the Arabic. It is fetched from a published text and
cached, because an approximate quotation of scripture is not a small error —
a single altered letter changes the word. The same reasoning as the source
list in the digest: the part that must be exact is not left to a model.

A reference is only recognised when it is explicitly marked as Qur'anic
("Qur'an 2:255", "Surah Al-Baqarah 2:255"). A bare "2:255" is deliberately
ignored: notes are made from videos, and bare colon-separated numbers are
far more often timestamps than citations.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator, Optional

from .store import _connect

API = "https://api.alquran.cloud/v1/ayah/{key}/quran-uthmani"
TIMEOUT = 12
MAX_PER_NOTE = 25          # a note citing more than this is pathological

# Verses per surah. Kept here so an impossible reference is rejected without
# a network call — and so a mistyped one is never silently "corrected".
AYAH_COUNTS = (
    7, 286, 200, 176, 120, 165, 206, 75, 129, 109, 123, 111, 43, 52, 99, 128,
    111, 110, 98, 135, 112, 78, 118, 64, 77, 227, 93, 88, 69, 60, 34, 30, 73,
    54, 45, 83, 182, 88, 75, 85, 54, 53, 89, 59, 37, 35, 38, 29, 18, 45, 60,
    49, 62, 55, 78, 96, 29, 22, 24, 13, 14, 11, 11, 18, 12, 12, 30, 52, 52,
    44, 28, 28, 20, 56, 40, 31, 50, 40, 46, 42, 29, 19, 36, 25, 22, 17, 19,
    26, 30, 20, 15, 21, 11, 8, 8, 19, 5, 8, 8, 11, 11, 8, 3, 9, 5, 4, 7, 3,
    6, 3, 5, 4, 5, 6,
)

# "Qur'an", "Quran", "Qur’an", "Surah", "Surat", "Sura", "S." or a bare "Q",
# optionally followed by a surah name, then chapter:verse(-verse).
_REF = re.compile(
    r"""(?P<marker>
            (?:\b(?:qur[’'`´]?\s?an|quran|koran|surah|surat|sura|chapter)\b
               |\bQ(?=\s?\d))
        )
        [\s:,\-–]*
        (?P<name>[A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-]+){0,2}\s*)?
        \(?\b(?P<surah>\d{1,3})\s*[:.]\s*(?P<ayah>\d{1,3})
        (?:\s*[-–]\s*(?P<ayah_to>\d{1,3}))?\b\)?
    """,
    re.IGNORECASE | re.VERBOSE,
)


class Ref:
    __slots__ = ("surah", "ayah", "start", "end", "text")

    def __init__(self, surah: int, ayah: int, start: int, end: int, text: str):
        self.surah, self.ayah = surah, ayah
        self.start, self.end, self.text = start, end, text

    @property
    def key(self) -> str:
        return f"{self.surah}:{self.ayah}"

    def __repr__(self) -> str:
        return f"<Ref {self.key}>"


def valid(surah: int, ayah: int) -> bool:
    return 1 <= surah <= 114 and 1 <= ayah <= AYAH_COUNTS[surah - 1]


def find_references(text: str) -> list[Ref]:
    """Every distinct verse cited, in the order it first appears."""
    out: list[Ref] = []
    seen: set[str] = set()
    for m in _REF.finditer(text or ""):
        surah, ayah = int(m.group("surah")), int(m.group("ayah"))
        to = int(m.group("ayah_to") or ayah)
        for a in range(ayah, min(to, ayah + 9) + 1):
            if not valid(surah, a):
                continue
            key = f"{surah}:{a}"
            if key in seen:
                continue
            seen.add(key)
            out.append(Ref(surah, a, m.start(), m.end(), m.group(0)))
            if len(out) >= MAX_PER_NOTE:
                return out
    return out


# ─────────────────────────────────────────────────────────────
# Fetch, with a permanent local cache
# ─────────────────────────────────────────────────────────────

def init_quran() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quran (
                key        TEXT PRIMARY KEY,
                arabic     TEXT NOT NULL,
                surah_name TEXT NOT NULL DEFAULT '',
                english    TEXT NOT NULL DEFAULT '',
                fetched_at REAL NOT NULL
            )
        """)


def _cached(key: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM quran WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def _store(key: str, arabic: str, surah_name: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO quran (key, arabic, surah_name, fetched_at) "
            "VALUES (?,?,?,?)", (key, arabic, surah_name, time.time()),
        )


# Marks that decorate Arabic letters without changing them: harakat, the
# superscript alef, Qur'anic annotation signs, and the tatweel stretcher.
_MARKS = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640\uFEFF]")

# The Basmala with every mark removed, and alef-wasla folded to plain alef.
_BASMALA_BARE = "بسم الله الرحمن الرحيم"


def _bare(ch: str) -> str:
    """One character reduced to its skeleton, or '' if it is only a mark."""
    if _MARKS.match(ch):
        return ""
    if ch in "\u0671\u0622\u0623\u0625":      # alef wasla / madda / hamza forms
        return "\u0627"
    return ch


def _strip_basmala(text: str, ayah: int, surah: int) -> str:
    """
    Remove the Basmala the source prepends to the opening verse.

    The published text carries it at the head of the first ayah of every
    surah, but it is only itself a numbered verse in Al-Fatiha. Leaving it in
    would print words that are not part of the verse being cited, and for
    scripture that is not a cosmetic difference. At-Tawbah (9) has no Basmala
    at all, so nothing is stripped there either.

    Matching ignores diacritics rather than spelling them out. The order of
    combining marks is not fixed — this text writes shadda before fatha —
    so a literal pattern silently fails to match while looking correct.
    """
    if ayah != 1 or surah in (1, 9):
        return text

    bare, index = [], []
    for i, ch in enumerate(text):
        b = _bare(ch)
        if b:
            bare.append(b)
            index.append(i)
    flat = "".join(bare)
    target = _BASMALA_BARE.replace(" ", "")
    squashed, squashed_index = [], []
    for j, ch in enumerate(flat):
        if not ch.isspace():
            squashed.append(ch)
            squashed_index.append(index[j])
    if "".join(squashed).startswith(target):
        # The index points at the last base letter; its own diacritics follow
        # it, so step past them or the next verse begins with an orphaned
        # kasra that belongs to the word just removed.
        cut = squashed_index[len(target) - 1] + 1
        while cut < len(text) and _MARKS.match(text[cut]):
            cut += 1
        return text[cut:].strip() or text
    return text


def arabic_for(key: str) -> Optional[dict]:
    """
    The Arabic of one verse, from cache or the published text.

    The text never changes, so a cache hit is permanent — after the first
    lookup this costs nothing and works without a network.
    """
    hit = _cached(key)
    if hit and hit.get("arabic"):
        return hit

    req = urllib.request.Request(
        API.format(key=urllib.parse.quote(key)),
        headers={"User-Agent": "gist/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None            # the note is still worth having without it

    data = (payload or {}).get("data") or {}
    # The source prefixes some verses with a byte-order mark; invisible, but
    # it defeats any prefix comparison and leaks into the saved note.
    raw = (data.get("text") or "").replace("\ufeff", "").replace("\u200f", "").strip()
    arabic = _strip_basmala(raw,
                            int(data.get("numberInSurah") or 0),
                            int(((data.get("surah") or {}).get("number")) or 0))
    if not arabic:
        return None
    name = ((data.get("surah") or {}).get("englishName") or "").strip()
    _store(key, arabic, name)
    return {"key": key, "arabic": arabic, "surah_name": name}


# ─────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────

def _block(ref: Ref, verse: dict) -> str:
    """
    One verse, Arabic first.

    Arabic is a right-to-left script and has to be marked as such, or the
    browser reorders the punctuation and the line reads wrongly. `lang="ar"`
    is what makes a system pick a naskh face rather than rendering it in a
    Latin font's fallback glyphs.
    """
    name = verse.get("surah_name") or ""
    label = f"Qur'an {ref.key}" + (f" · {name}" if name else "")
    return (
        '\n\n<div class="ayah">\n'
        f'  <div class="ayah-ref">{label}</div>\n'
        f'  <p class="ayah-ar" dir="rtl" lang="ar">{verse["arabic"]}</p>\n'
        "</div>\n"
    )


def _line_spans(text: str) -> Iterator[tuple[int, int]]:
    start = 0
    for line in text.split("\n"):
        yield start, start + len(line)
        start += len(line) + 1


def annotate(markdown: str) -> tuple[str, int]:
    """
    Insert the Arabic of every cited verse, after the line that cites it.

    Placing it after the whole line rather than at the match keeps the
    sentence — and whatever translation it already carries — intact, so the
    Arabic sits beside the translation instead of interrupting it.
    """
    refs = find_references(markdown)
    if not refs:
        return markdown, 0

    verses: dict[str, dict] = {}
    for ref in refs:
        if ref.key not in verses:
            got = arabic_for(ref.key)
            if got:
                verses[ref.key] = got
    if not verses:
        return markdown, 0

    # Group by the line each reference falls on, so a line citing three
    # verses gets all three appended once, in order.
    by_line: dict[int, list[Ref]] = {}
    for ref in refs:
        if ref.key not in verses:
            continue
        for end, (ls, le) in enumerate(_line_spans(markdown)):
            if ls <= ref.start <= le:
                by_line.setdefault(le, []).append(ref)
                break

    out, cursor, added = [], 0, 0
    for line_end in sorted(by_line):
        out.append(markdown[cursor:line_end])
        for ref in by_line[line_end]:
            out.append(_block(ref, verses[ref.key]))
            added += 1
        cursor = line_end
    out.append(markdown[cursor:])
    return "".join(out), added
