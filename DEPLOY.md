# Deploying Gist

One container, one instance, one disk. The whole design assumption is ~20
people, so nothing here needs a broker, a cache or a second service.

## Why one instance

Sessions live in an in-memory dict in `app/main.py` and jobs run on a thread
pool inside the web process (`app/jobs.py`). A second replica would answer
job-status polls from a worker that never ran the job. Render enforces this for
you: **a service with a disk cannot scale past one instance.** If you ever need
to, move sessions to the database and jobs to a separate worker first.

## The database is a folder

`app/store.py` reads `DATA_DIR` and puts `notes.db` in it. `app/jobs.py` and
`app/auth.py` both import `store._connect`, so that single variable moves notes,
accounts, sessions and the job queue together. On Render, `DATA_DIR=/var/data`
is a mounted disk that survives deploys and restarts.

The container filesystem does **not** survive a deploy. Anything written
outside `/var/data` is gone the next time you push.

## Deploy

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → pick the repo. It reads `render.yaml` and
   creates the service, the disk and the non-secret environment variables.
3. Fill in the three secrets it asks for (they are `sync: false` in the
   blueprint, which is why they are prompted rather than committed):
   - `OPENAI_API_KEY` — your Groq key
   - `APIFY_TOKEN`
   - `ADMIN_EMAIL` — the address you will register with
4. First deploy takes a few minutes, mostly `apt-get install ffmpeg`.
5. Open the logs and find the bootstrap invite code printed on first run.
   Registration is invite-only, so you need it once.
6. Visit the URL, register with `ADMIN_EMAIL`, and you are in.

## Put it on the iPhone

Open the Render URL in **Safari** (not Chrome — only Safari can install).
Share → **Add to Home Screen**. You get the Gist icon, a full-screen launch
with no browser chrome, and the service worker caches the shell so it opens
without a signal.

The session cookie lasts 30 days, so you sign in about once a month. The
installed app keeps its own cookie store, separate from Safari's, so signing
in there is a one-off rather than something that follows your browser.

Offline, the app opens and your saved library is readable from cache. Making
new notes needs a connection — that work happens on the server.

## What protects you once it is public

A personal app on a public URL is a different threat model from localhost, so
four things changed:

- **Auth fails closed.** `auth_required()` is on unless `REQUIRE_AUTH` is
  explicitly `0`. It used to default to off, and with auth off every anonymous
  visitor is served an identity that is flagged admin — so one missing
  environment variable handed strangers your notes and your API credits.
- **Registration is invite-only** and the bootstrap invite is printed once, to
  the deploy logs, when the user table is empty.
- **Daily per-user caps** on the endpoints that cost money (`LIMIT_NOTES`,
  `LIMIT_RESEARCH`, `LIMIT_TRANSCRIPT`, `LIMIT_REFILE`). These exist for the
  retry loop, not the stranger — one wedged client can otherwise spend all
  night on Apify credits. Over the cap the endpoint returns 429.
- **Links are checked before they are fetched.** The URL you paste goes to
  yt-dlp, which will fetch cloud metadata endpoints and private addresses as
  happily as YouTube. Anything that is not public http/https is refused.

## Costs

| Item | Cost |
|---|---|
| Render Starter instance | $7/mo |
| 1 GB disk | ~$0.25/mo |
| Groq | free tier |
| Apify | pay as you go |

The free Render plan has **no disk** and sleeps after 15 minutes idle, so it
cannot host this.

## Keeping the data safe

A mounted disk is not a backup — it protects you from a redeploy, not from a
mistake. Render takes daily snapshots of disks on paid plans. For anything
beyond that, add Litestream replicating `/var/data/notes.db` to object storage.

## Local parity

Build and run exactly what Render runs:

```sh
docker build -t gist:local .
docker run --rm -p 8000:8000 \
  -v gist-data:/var/data \
  -e DATA_DIR=/var/data -e REQUIRE_AUTH=1 \
  --env-file .env \
  gist:local
```
