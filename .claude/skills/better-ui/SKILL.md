---
name: better-ui
description: Redesign or improve this project's web UI to a state-of-the-art standard using the Claude Design method. Use whenever the user asks for a better UI, a nicer or more modern frontend, a redesign, a restyle, a UI/UX improvement, "make it look good", "make it state of the art", or any change to how app/static/index.html looks or feels. Not for backend, transcript, or notes-engine changes.
---

# Better UI

Redesign `app/static/index.html` to a genuinely high standard — not a restyle,
a design pass with a point of view.

## 1. Load the design method first

**Always start by invoking the `artifact-design` skill.** That is the actual
Claude Design guidance — palette and neutral selection, type pairing, the
three-state theme structure, layout and spacing rules, the "avoid AI-generated
design" list, copywriting, and the design-plan-then-build process.

Do not work from memory and do not duplicate that guidance here; it is
maintained upstream and this file only adds what is specific to this project.

Also load `artifact-diagramming` if the redesign introduces a diagram, and
`dataviz` before writing any chart, meter, or stat tile.

## 2. Write a design plan before touching code

Per `artifact-design`: name 4–6 palette hex values, a display/body/utility type
pairing, and a one-or-two-sentence layout concept. Check the plan against the
generic-default list before building — if a part of it reads like the look you'd
produce for any similar page, revise it and say what you changed.

This app's subject is *YouTube transcripts turned into study notes*. Ground the
design in that world — reading, studying, source material and derived material,
the relationship between a timestamped transcript and structured notes. The
current design is a red-accented two-column dashboard; treat that as the
starting point to improve on, not a constraint to preserve.

## 3. Project constraints — these are hard

**One self-contained file.** `app/static/index.html` is served directly by
FastAPI (`StaticFiles`). There is no build step, no bundler, no framework.
Ship CSS in a `<style>` block and JS in a `<script>` block in that one file.

**CDN scripts are fine here.** This page is served locally, not as an Artifact,
so the Artifact CSP does not apply. It already loads `marked` from cdnjs. Pin
exact versions and place a `<script src>` before any inline script using its
global. Google Fonts works normally.

**Preserve the DOM contract, or update both halves together.** The inline JS
resolves elements by id via `const $ = id => document.getElementById(id)`.
These ids are load-bearing:

```
form  url  topic  kind  provider  model
btnPreview  btnNotes  btnCopy  btnDownload
status  keywarn  notes  outTitle  outMeta
txDetails  txText
```

`provider` must stay a `<select>` whose first `<option>` has `value=""` —
`checkHealth()` rewrites that option's text. If you restructure the markup,
update the script in the same pass and re-check every id above.

**Don't change the API contract.** The page calls `/api/health`,
`/api/transcript`, and `/api/notes` with the existing JSON shapes. A UI change
must not require backend edits; if you think it should, say so and ask first.

**Three theme states, not two.** The page currently defines dark only inside
`@media (prefers-color-scheme:dark)`. Follow the full pattern from
`artifact-design`: complete palette on bare `:root`, tokens redefined under
`@media (prefers-color-scheme: dark)` guarded as `:root:not([data-theme="light"])`,
and again under `:root[data-theme="dark"]`.

**Show real working state.** Per `artifact-design`, the page must not open as an
empty shell. It should look like a tool someone is mid-way through using —
plausible example values, clearly marked as examples, never passed off as the
user's own data.

## 4. Verify before reporting

Editing `app/static/index.html` fires the `PostToolUse` hook in
`.claude/settings.json`, which runs `.claude/verify.sh` — full server restart
plus health, UI, transcript, and notes checks. Network checks are throttled to
once per 10 minutes; use `FORCE=1 ./.claude/verify.sh` to force a full run.

A 200 from `GET /` only proves the file parses. Also confirm by hand:

- Every id in the contract above still resolves — a redesign that silently
  drops one leaves a dead button with no console error until it's clicked.
- Both themes render (toggle your OS appearance, or stamp
  `data-theme="dark"` on `<html>` to check the explicit path).
- The three flows still work: preview transcript, generate notes, copy/download.

Then look at the page once, per the "write, look once, publish" rule in
`artifact-design` — one screenshot, one pass of edits for what it shows. Don't
build a test loop around your own file.

## 5. Report honestly

Say what design direction you chose and why. If a constraint above forced a
compromise, name it. If you couldn't verify something, say that rather than
implying it works.
