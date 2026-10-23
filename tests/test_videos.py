#!/usr/bin/env python3
"""
End-to-end check across a spread of real videos.

For each video: fetch a transcript, then generate notes from it, and record
which extraction strategy actually carried the video (caption API, yt-dlp
captions, or speech-to-text). Prints a table and exits non-zero if any
video fails outright.

Usage:
    ./.venv/bin/python tests/test_videos.py                # all
    ./.venv/bin/python tests/test_videos.py --limit 3      # first 3
    ./.venv/bin/python tests/test_videos.py --no-notes     # transcripts only
    ./.venv/bin/python tests/test_videos.py --json out.json

Run against a live server (exercises the HTTP layer too):
    ./.venv/bin/python tests/test_videos.py --server http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass


# A deliberate spread: music, long-form talks, lectures, a short, a
# documentary clip, and a non-English video — so the run exercises
# manual captions, auto-captions and the speech fallback.
VIDEOS: list[tuple[str, str]] = [
    ("Rick Astley — Never Gonna Give You Up", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("Me at the zoo (first YouTube video)",   "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
    ("Steve Jobs — Stanford commencement",    "https://www.youtube.com/watch?v=UF8uR6Z6KLc"),
    ("3Blue1Brown — Neural networks ch.1",    "https://www.youtube.com/watch?v=aircAruvnKk"),
    ("Andrej Karpathy — Intro to LLMs",       "https://www.youtube.com/watch?v=zjkBMFhNj_g"),
    ("Kurzgesagt — The Egg",                  "https://www.youtube.com/watch?v=h6fcK_fRYaI"),
    ("TED — Do schools kill creativity?",     "https://www.youtube.com/watch?v=iG9CE55wbtY"),
    ("Veritasium — The biggest ideas",        "https://www.youtube.com/watch?v=GEmuEWjHr5c"),
    ("MIT OCW — Linear algebra lec 1",        "https://www.youtube.com/watch?v=ZK3O402wf1c"),
    ("Computerphile — How GPT works",         "https://www.youtube.com/watch?v=rURRYI66E54"),
]


@dataclass
class Result:
    name: str
    url: str
    ok: bool
    platform: str = ""
    source: str = ""
    segments: int = 0
    chars: int = 0
    notes_chars: int = 0
    seconds: float = 0.0
    error: str = ""


# ─────────────────────────────────────────────────────────────
# Runners
# ─────────────────────────────────────────────────────────────

def run_direct(name: str, url: str, want_notes: bool) -> Result:
    """Call the library directly — no server needed."""
    from app.transcript import get_transcript, TranscriptError  # noqa
    from app.notes import generate_notes, NoteRequest

    t0 = time.time()
    try:
        tr = get_transcript(url)
    except Exception as e:
        return Result(name, url, False, seconds=round(time.time() - t0, 1),
                      error=f"transcript: {e}")

    res = Result(
        name=name, url=url, ok=True,
        platform=getattr(tr, "platform", "?"),
        source=getattr(tr, "source", "?"),
        segments=len(tr.segments), chars=len(tr.text),
        seconds=round(time.time() - t0, 1),
    )
    if not want_notes:
        return res

    try:
        notes = generate_notes(NoteRequest(
            transcript=tr.text, title=tr.title,
            topic="the main ideas", note_kind="concise study notes",
        ))
        res.notes_chars = len(notes or "")
        if res.notes_chars < 50:
            res.ok = False
            res.error = "notes: suspiciously short output"
    except Exception as e:
        res.ok = False
        res.error = f"notes: {e}"
    res.seconds = round(time.time() - t0, 1)
    return res


def run_server(name: str, url: str, want_notes: bool, base: str) -> Result:
    """Drive the running app over HTTP, exercising the API layer too."""
    import urllib.request

    def post(path: str, body: dict, timeout: int) -> dict:
        req = urllib.request.Request(
            base.rstrip("/") + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    t0 = time.time()
    endpoint = "/api/notes" if want_notes else "/api/transcript"
    payload = {"url": url}
    if want_notes:
        payload |= {"topic": "the main ideas", "note_kind": "concise study notes"}

    def get(path: str, timeout: int) -> dict:
        req = urllib.request.Request(base.rstrip("/") + path)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    try:
        d = post(endpoint, payload, timeout=900)

        # /api/notes is asynchronous: it answers 202 {"job": id} and the work
        # happens on a worker thread. Reading that reply as the result is a
        # false pass — it reports "ok" with no transcript and no notes, which
        # is exactly what this suite did until it was caught.
        if "job" in d and "notes" not in d:
            deadline = time.time() + 900
            while True:
                if time.time() > deadline:
                    return Result(name, url, False,
                                  seconds=round(time.time() - t0, 1),
                                  error="job did not finish within 900s")
                time.sleep(2)
                j = get(f"/api/jobs/{d['job']}", timeout=60)
                if j.get("status") == "done":
                    d = j.get("result") or {}
                    break
                if j.get("status") == "error":
                    return Result(name, url, False,
                                  seconds=round(time.time() - t0, 1),
                                  error=str(j.get("error", ""))[:160])
    except Exception as e:
        return Result(name, url, False, seconds=round(time.time() - t0, 1), error=str(e))

    if "error" in d:
        return Result(name, url, False, seconds=round(time.time() - t0, 1),
                      error=str(d["error"])[:160])

    # A run that produced nothing is a failure however cleanly it returned.
    chars = d.get("chars") or d.get("transcript_chars", 0)
    notes_len = len(d.get("notes") or "")
    if want_notes and notes_len == 0:
        return Result(name, url, False, seconds=round(time.time() - t0, 1),
                      error="completed with no notes")
    if not want_notes and not chars:
        return Result(name, url, False, seconds=round(time.time() - t0, 1),
                      error="completed with an empty transcript")

    return Result(
        name=name, url=url, ok=True,
        platform=d.get("platform", "?"), source=d.get("source", "?"),
        segments=d.get("segments", 0),
        chars=chars,
        notes_chars=notes_len,
        seconds=round(time.time() - t0, 1),
    )


# ─────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=len(VIDEOS))
    ap.add_argument("--no-notes", action="store_true", help="transcripts only")
    ap.add_argument("--server", default="", help="base URL of a running app")
    ap.add_argument("--json", default="", help="write results to this file")
    args = ap.parse_args()

    want_notes = not args.no_notes
    todo = VIDEOS[:args.limit]

    print(f"Running {len(todo)} videos "
          f"({'transcript + notes' if want_notes else 'transcript only'}"
          f"{', via ' + args.server if args.server else ', direct'})\n")

    results: list[Result] = []
    for i, (name, url) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {name} … ", end="", flush=True)
        try:
            r = (run_server(name, url, want_notes, args.server) if args.server
                 else run_direct(name, url, want_notes))
        except Exception as e:  # never let one video kill the run
            traceback.print_exc()
            r = Result(name, url, False, error=str(e))
        results.append(r)
        print(f"{'ok' if r.ok else 'FAIL'}  "
              f"({r.source or '—'}, {r.chars} chars, {r.seconds}s)"
              + (f"  {r.error}" if r.error else ""))

    # ── Summary ──
    passed = [r for r in results if r.ok]
    print("\n" + "=" * 92)
    print(f"{'video':<38} {'source':<20} {'segs':>5} {'chars':>7} {'notes':>7} {'secs':>6}")
    print("-" * 92)
    for r in results:
        print(f"{r.name[:37]:<38} {(r.source or r.error[:19])[:19]:<20} "
              f"{r.segments:>5} {r.chars:>7} {r.notes_chars:>7} {r.seconds:>6}")
    print("=" * 92)
    print(f"{len(passed)}/{len(results)} passed")

    by_source: dict[str, int] = {}
    for r in passed:
        by_source[r.source] = by_source.get(r.source, 0) + 1
    if by_source:
        print("strategy used: " + ", ".join(f"{k} ×{v}" for k, v in sorted(by_source.items())))
    for r in results:
        if not r.ok:
            print(f"  FAILED  {r.name}: {r.error}")

    if args.json:
        Path(args.json).write_text(json.dumps([asdict(r) for r in results], indent=2))
        print(f"\nwrote {args.json}")

    return 0 if len(passed) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
