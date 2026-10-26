#!/usr/bin/env bash
# Point note generation at Gemini while leaving speech-to-text on Groq.
#
# Gemini's OpenAI-compatible endpoint has no Whisper, and speech-to-text is
# the only path that works for Instagram and Facebook, so the two providers
# have to be configured separately.
#
# Reads GEMINI_API_KEY from .env, confirms it works, picks a real model from
# the provider's own catalogue rather than a guessed name, and rewrites the
# OPENAI_*/SPEECH_* lines. Never prints a key.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

./.venv/bin/python - <<'PY'
import os, pathlib, shutil, sys, time

env_path = pathlib.Path(".env")
lines = env_path.read_text().splitlines()

def val(name):
    for l in lines:
        l = l.strip()
        if l.startswith(name + "="):
            return l.split("=", 1)[1].strip().strip('"').strip("'")
    return ""

gem = val("GEMINI_API_KEY")

# The Groq key, wherever it currently lives. This script is re-runnable, so by
# the second run OPENAI_API_KEY is already the Gemini key — reading it blindly
# wrote the Gemini key into SPEECH_API_KEY and silently broke Whisper. Only a
# value that actually looks like a Groq key is accepted.
groq = next((v for v in (val("SPEECH_API_KEY"), val("OPENAI_API_KEY"))
             if v.startswith("gsk_")), "")

if not gem:
    print("No GEMINI_API_KEY in .env yet.\n")
    print("  1. Open https://aistudio.google.com/apikey and create a key (free).")
    print("  2. Add one line to .env:   GEMINI_API_KEY=your-key-here")
    print("  3. Run this script again.")
    sys.exit(1)

print(f"found GEMINI_API_KEY ({len(gem)} chars, starts {gem[:4]}…)")
if not groq:
    print("warning: no Groq key found. Speech-to-text has no provider, which\n"
          "         disables the only path that works for Instagram and Facebook.")

BASE = "https://generativelanguage.googleapis.com/v1beta/openai"

from openai import OpenAI
client = OpenAI(api_key=gem, base_url=BASE)

# Pick from the live catalogue. Guessing a model name is how the Kimi K2
# attempt failed: the key was valid and the model simply did not exist.
try:
    names = [m.id.replace("models/", "") for m in client.models.list()]
except Exception as e:
    print(f"\nthat key did not work against Gemini: {type(e).__name__}: {str(e)[:200]}")
    sys.exit(2)

print(f"catalogue returned {len(names)} models")

import re

def version_of(n):
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
    return float(m.group(1)) if m else 0.0

def usable(n):
    # Chat models only — the audio/image/embedding variants answer a
    # different API.
    bad = ("audio", "image", "tts", "embedding", "vision", "live", "thinking")
    return "flash" in n and not any(b in n for b in bad)

# Rank by version number rather than a hardcoded list — this script outlived
# its first hardcoded "2.5/2.0/1.5" ladder within a day.
# Lite first. The script cannot know prices — they are not derivable from a
# model name and a table here would go stale — so it orders by family and
# version and checks the model actually answers. Pin OPENAI_MODEL by hand if
# a specific one is cheaper: at the time of writing 3.1-flash-lite
# ($0.25/$1.50 per M) undercuts 3.5-flash-lite ($0.30/$2.50).
# Flash-Lite costs a fraction of full Flash and writes
# perfectly good notes — shorter, but accurate and well organised. Full
# Flash is the fallback when no Lite model answers.
candidates = sorted(
    {n for n in names if usable(n)},
    key=lambda n: ("lite" in n, version_of(n), "preview" not in n and "exp" not in n),
    reverse=True,
)
if not candidates:
    print("no suitable flash model found. Catalogue sample:", ", ".join(sorted(names)[:25]))
    sys.exit(3)

print(f"{len(candidates)} candidate models, newest first: {', '.join(candidates[:5])}")

# The catalogue lists models the account cannot actually call (gemini-2.5-flash
# is listed but 404s as "no longer available to new users"), and the newest
# model is not reliably the best one: 3.8-flash timed out at 60s while
# 3.6-flash answered the same prompt in 1.6s. So probe for real, measure, and
# pick the newest that is actually responsive.
PROBE_TIMEOUT = 25.0
GOOD_ENOUGH = 8.0

from openai import OpenAI as _OpenAI
probe = _OpenAI(api_key=gem, base_url=BASE, timeout=PROBE_TIMEOUT, max_retries=0)

results = []
for cand in candidates[:6]:
    t0 = time.time()
    try:
        r = probe.chat.completions.create(
            model=cand, max_tokens=300,
            messages=[{"role": "user", "content": "Say the single word: ready"}],
        )
        dt = time.time() - t0
        txt = (r.choices[0].message.content or "").strip()
        results.append((cand, dt, txt))
        print(f"  {cand:<28} {dt:6.1f}s  {txt[:24]!r}")
        if dt <= GOOD_ENOUGH:
            break                      # newest responsive model wins
    except Exception as e:
        print(f"  {cand:<28} {time.time()-t0:6.1f}s  {type(e).__name__}: {str(e)[:70]}")

ok = [r for r in results if r[1] <= PROBE_TIMEOUT]
if not ok:
    print("\nno candidate model answered in time with this key.")
    sys.exit(4)
ok.sort(key=lambda r: r[1])
model, best_dt, reply = ok[0]

print(f"\nusing model: {model}")

shutil.copy2(env_path, ".env.backup")
os.chmod(".env.backup", 0o600)
print("backed up .env -> .env.backup")

want = {
    "NOTE_PROVIDER": "openai",
    # The 12,000-char budget exists only because Groq's per-minute ceiling
    # forced long transcripts through a map-reduce: fifteen chunks, then
    # four, then two, each re-reading the text and each spending a full
    # output allowance. Gemini has room for a whole transcript in one call,
    # and a single pass costs roughly a tenth as much — output tokens are
    # several times the price of input, and chunking multiplies output.
    "NOTE_CHAR_BUDGET": "200000",
    "OPENAI_BASE_URL": BASE,
    "OPENAI_API_KEY": gem,
    "OPENAI_MODEL": model,
}
if groq:
    want |= {
        "SPEECH_BASE_URL": "https://api.groq.com/openai/v1",
        "SPEECH_API_KEY": groq,
        "SPEECH_MODEL": "whisper-large-v3-turbo",
        "SPEECH_TO_TEXT": "on",
    }

out, seen = [], set()
for l in lines:
    k = l.split("=", 1)[0].strip() if "=" in l and not l.strip().startswith("#") else None
    if k in want:
        out.append(f"{k}={want[k]}")
        seen.add(k)
    else:
        out.append(l)
missing = [k for k in want if k not in seen]
if missing:
    out.append("")
    out.append("# Set by .claude/use-gemini.sh")
    out += [f"{k}={want[k]}" for k in missing]

env_path.write_text("\n".join(out) + "\n")
os.chmod(env_path, 0o600)
print(f"rewrote .env ({len(seen)} updated, {len(missing)} added)")
print("\nnotes  -> Gemini  " + model)
print("speech -> " + ("Groq    whisper-large-v3-turbo" if groq
                      else "NOT CONFIGURED - no Groq key found in .env"))
PY
