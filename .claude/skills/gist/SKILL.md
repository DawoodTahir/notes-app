---
name: gist
description: Work on the Gist app — its interface, its note-taking pipeline, or its deployment. Use for any change to app/static/index.html, the FastAPI modules under app/, the classifier, the job queue, or the Render deployment. Carries the design system, the invariants that keep breaking, and the checks that catch them. Use before editing, not after something has gone wrong.
---

# Gist

A personal app that turns a video or a question into notes you can question,
curate, file and defend. One user, a handful of others, one container.

## Before you change anything

Run the checks. They exist because each one caught something real:

```sh
bash .claude/check-ui-contract.sh     # DOM contract, script parses, ordering
./.claude/verify.sh                   # restart, serve, transcript, notes
FORCE=1 ./.claude/verify.sh           # as above, skipping the throttle
```

After touching tokens or themes:

```sh
./.venv/bin/python - <<'PY'
# every custom property must resolve in light, OS-dark and manual-dark
PY
```
There are three theme states, not two: `:root`, `@media (prefers-color-scheme:dark)`,
and `:root[data-theme="dark"]`. The last is the Theme button. Tokens added to
the media block and not the attribute block leave the manual toggle rendering
light values on a dark page — this has happened.

## The failure that keeps recurring

**A stage that half-works and reports a confident, wrong conclusion.** Not
crashes — those are easy. Examples, all real:

- A test suite read `202 {"job": …}` as the finished result and reported
  "10/10 passed" with zero notes.
- `verify.sh` did the same and reported "notes OK" with nothing generated.
- The shortlist prompt was handed 60 candidates; the reply truncated before
  any decision, so the run announced "nothing suitable" having judged nothing.
- `_env_int` applied `max(1000, …)` to every value, so "read 3 videos" meant
  a thousand.

So: when something reports success, check it produced output. When a model
reply is parsed, check the parse found something, and distinguish *"the reply
was unusable"* from *"the answer was no"* — they need different fixes and
look identical from the outside.

## Interface

Three panes behind a bar: library, document, side panel (Ask / Transcript /
Sources). Everything is frosted glass over a drifting three-colour wash.

- Depth is carried by **clarity**, not darkness. The document is the most
  opaque because it is the surface meant for reading; the outer panes stay
  translucent so the wash shows through.
- Every glass surface needs its hairline top highlight (`--glass-hi`).
  Without it a translucent panel looks washed out rather than like glass.
- Grounds are **warm**. Pure white under the violet wash reads as a hole in
  the page, and long notes on it are tiring.
- Two accents, used consistently: `--brand` (violet) for primary structure,
  `--accent` (teal) for the second voice — sub-headings, code, nested bullets.
- Platform colour belongs on cards (`--pc` via `[data-p=…]`). Never define
  `--pc` on `.card` afterwards; same specificity, later in the file, and the
  spines silently go monochrome.
- `backdrop-filter` always paired with `-webkit-backdrop-filter`, always with
  an `@supports not` fallback to solid, and never three full-screen layers on
  a phone.

### Controls on a bullet

They share one `.li-tools` row. Do not position a new control absolutely at
the corner of an `li` — that is how the source button got covered by the drop
button and appeared broken for days.

## Notes

The library is organised **by who the material answers**, not by academic
subject. See `NOTES_STANDPOINT` in `.env`. The owner's own position is the
baseline and never a folder; the folder is the position being engaged, so
opening one shows what to say to that interlocutor.

Pipeline: transcript → notes → Qur'anic Arabic attached from a published
text → classified → saved. The model never writes scripture and never writes
a URL; both come from sources, because a citation that might be invented is
worth less than no citation.

Long transcripts go in one pass. `NOTE_CHAR_BUDGET` is large on purpose —
chunking costs roughly ten times more, since every chunk spends its own
output allowance and output is priced several times input.

## Deployment

Render, Docker, one instance, a disk at `/var/data`. `DATA_DIR` moves notes,
accounts, sessions, jobs and context together. `.env` is **not** deployed —
only dashboard variables are live. Pushing triggers a rebuild, during which
Render serves an HTML error page; a client calling `.json()` on it throws
`Unexpected token '<'`. That is a deploy window, not a bug.

Auth fails closed. `REQUIRE_AUTH` must be explicitly `0` to disable, because
with auth off every visitor is served an identity flagged admin.

## Verifying UI work

There is no browser here. Check what can be checked — token parity, brace
balance, that no rule clobbers another, that element ids referenced by the
script exist *and appear before it* — and then say plainly which parts were
not visually verified. Do not describe how something looks as though it has
been seen.
