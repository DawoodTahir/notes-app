# YouTube Notes

Paste a YouTube link, give it the topic and the kind of notes you want, and it
extracts the video's transcript and turns it into clean, structured notes.

- **Backend extractor** — pulls the transcript via `youtube-transcript-api`,
  falling back to `yt-dlp` captions when needed. No YouTube API key required.
- **Configurable note engine** — Anthropic (Claude) or OpenAI, chosen in the UI
  or via `.env`. Long videos are chunked and condensed automatically.
- **Web UI** — link + topic context + note-style box, live transcript preview,
  rendered notes, copy / download as Markdown.

## Quick start

```bash
cd youtube-notes
cp .env.example .env         # then add at least one API key
./run.sh                     # first run builds a venv and installs deps
```

Open <http://127.0.0.1:8000>.

> On first launch `run.sh` creates `.venv/` and installs everything from
> `requirements.txt`. Later launches skip straight to the server.

### Manual start (if you prefer)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `NOTE_PROVIDER` | Default provider: `anthropic` or `openai` |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | Claude credentials + optional model |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | OpenAI credentials + optional model |
| `HOST` / `PORT` | Where the server binds (default `127.0.0.1:8000`) |

You only need a key for the provider(s) you actually use. Transcript preview
works with no key at all.

## How it works

```
YouTube URL ──► transcript.py ──► transcript text
                  (api → yt-dlp fallback)
                                        │
   topic + note-kind ──► notes.py ──────┘──► structured Markdown notes
                          (Anthropic / OpenAI, map-reduce if long)
```

Notes always follow a consistent skeleton — **Overview · Key Points · Detailed
Notes · Takeaways & Action Items** — shaped by the topic and note style you
supply per request.

## API

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/api/health` | — | which providers have keys |
| `POST` | `/api/transcript` | `{url}` | transcript text + metadata |
| `POST` | `/api/notes` | `{url, topic, note_kind, provider?, model?}` | structured notes |

## Project layout

```
youtube-notes/
├── app/
│   ├── main.py           FastAPI routes
│   ├── transcript.py     extraction (api + yt-dlp fallback)
│   ├── notes.py          provider-agnostic note engine
│   └── static/index.html the UI
├── requirements.txt
├── .env.example
└── run.sh
```

## Notes & limits

- Works on videos that have captions (manual or auto-generated). Private,
  age-restricted, or caption-disabled videos can't be transcribed.
- Auto-captions carry transcription errors; the note engine smooths obvious
  ones but won't invent content.
- If YouTube rate-limits transcript requests from your IP, the `yt-dlp`
  fallback usually still works; heavy use may need a short wait.
