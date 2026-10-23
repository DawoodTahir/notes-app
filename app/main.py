"""
FastAPI app: serves the UI and exposes the transcript + notes pipeline.
Works with YouTube, Instagram, Facebook, TikTok, X, Vimeo and more.

Endpoints
  GET  /              -> the web UI
  GET  /api/health    -> config sanity check (which providers have keys)
  POST /api/transcript-> extract transcript only (preview)
  POST /api/notes     -> extract transcript + generate structured notes
"""

from __future__ import annotations

import os
import uuid
from collections import OrderedDict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from .research import find_sources
from .notes import (ChatRequest, NoteError, NoteRequest, answer_question,
                    classify_note, generate_notes)
from . import auth, jobs, quota, store
from .transcript import TranscriptError, get_transcript, search_videos, speech_enabled

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Transcripts run to tens of thousands of characters, so the browser gets a
# session id instead of carrying the text back for every follow-up question.
# In-memory and capped — this is a single-user local app, not a cluster.
_SESSIONS: "OrderedDict[str, dict]" = OrderedDict()
_MAX_SESSIONS = 50


def _remember(title: str, transcript: str, notes: str, **extra) -> str:
    sid = uuid.uuid4().hex[:16]
    ctx = {"title": title, "transcript": transcript, "notes": notes, **extra}
    _SESSIONS[sid] = ctx
    while len(_SESSIONS) > _MAX_SESSIONS:
        _SESSIONS.popitem(last=False)
    store.put_context(sid, ctx)          # survives eviction and restart
    return sid


def _recall(sid: str | None) -> dict:
    """
    The context behind a session id.

    The dict in front is just a cache; the database is the truth. Reading
    only the dict meant a note saved after the fiftieth generation — or after
    any restart — was written with an empty transcript, with no error.
    """
    sid = sid or ""
    hit = _SESSIONS.get(sid)
    if hit is not None:
        return hit
    ctx = store.get_context(sid)
    if ctx:
        _SESSIONS[sid] = ctx
        while len(_SESSIONS) > _MAX_SESSIONS:
            _SESSIONS.popitem(last=False)
    return ctx

app = FastAPI(title="YouTube Notes", version="1.0.0")
store.init_db()
auth.init_auth()
jobs.init_jobs()
quota.init_quota()

if auth.auth_required():
    _boot = auth.bootstrap_invite()
    if _boot:
        print(f"\n  No accounts yet. Register with invite code:  {_boot}\n", flush=True)
else:
    auth.adopt_orphan_notes()   # one owner, so existing notes are theirs


# ─────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────

class TranscriptIn(BaseModel):
    url: str
    languages: list[str] | None = None


COOKIE = "yn_session"


class RegisterIn(BaseModel):
    email: str
    password: str
    invite: str


class LoginIn(BaseModel):
    email: str
    password: str


def current_user(request: Request) -> auth.User:
    """The signed-in user, or the single local owner when auth is off."""
    if not auth.auth_required():
        return auth.local_user()
    user = auth.user_for_token(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return user


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE, token,
        httponly=True,                                   # not readable by JS
        samesite="lax",                                  # survives normal navigation
        secure=os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
        max_age=auth.SESSION_DAYS * 86400,
        path="/",
    )


@app.post("/api/auth/register")
def auth_register(body: RegisterIn, response: Response):
    try:
        user = auth.register(body.email, body.password, body.invite)
    except auth.AuthError as e:
        return JSONResponse(status_code=422, content={"error": str(e)})
    _, token = auth.login(body.email, body.password)
    _set_cookie(response, token)
    return {"user": user.public()}


@app.post("/api/auth/login")
def auth_login(body: LoginIn, response: Response):
    try:
        user, token = auth.login(body.email, body.password)
    except auth.AuthError as e:
        return JSONResponse(status_code=401, content={"error": str(e)})
    _set_cookie(response, token)
    return {"user": user.public()}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    auth.logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request):
    if not auth.auth_required():
        return {"user": auth.local_user().public(), "auth_required": False}
    user = auth.user_for_token(request.cookies.get(COOKIE))
    return {"user": user.public() if user else None, "auth_required": True}


@app.get("/api/auth/invites")
def auth_invites(user: auth.User = Depends(current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only.")
    return {"invites": auth.list_invites()}


@app.post("/api/auth/invites")
def auth_invite_create(user: auth.User = Depends(current_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only.")
    return {"code": auth.create_invite(user.id)}


class SearchIn(BaseModel):
    query: str
    limit: int = 8
    platform: str = "youtube"


class SaveIn(BaseModel):
    session: str | None = None
    # Explicit fields win; otherwise they come from the session.
    title: str = ""
    url: str = ""
    platform: str = ""
    source: str = ""
    topic: str = ""
    note_kind: str = ""
    notes: str = ""


class ResearchIn(BaseModel):
    claim: str
    instruction: str = ""
    session: str | None = None
    provider: str | None = None
    model: str | None = None


class ChatIn(BaseModel):
    question: str
    session: str | None = None
    # Fallbacks when the session has expired (e.g. the server restarted).
    notes: str = ""
    transcript: str = ""
    title: str = ""
    history: list[dict] | None = None
    provider: str | None = None
    model: str | None = None


class NotesIn(BaseModel):
    url: str
    extra: str = ""          # optional free-form steer from the user
    topic: str = ""          # legacy, still honoured if sent
    note_kind: str = ""
    provider: str | None = None
    model: str | None = None
    languages: list[str] | None = None


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

@app.get("/api/health")
def health(request: Request):
    who = auth.user_for_token(request.cookies.get(COOKIE))
    return {
        "user": who.public() if who else None,
        "ok": True,
        "default_provider": os.environ.get("NOTE_PROVIDER", "anthropic"),
        "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai_key": bool(os.environ.get("OPENAI_API_KEY")),
        "speech_to_text": speech_enabled(),
    }


def _notes_job(payload: dict, progress) -> dict:
    """Transcript + notes. The part that can take four minutes."""
    progress("fetching the video")
    tr = get_transcript(payload["url"], payload.get("languages"))

    progress("writing notes")
    notes = generate_notes(NoteRequest(
        transcript=tr.text, title=tr.title,
        topic=payload.get("topic", ""), note_kind=payload.get("note_kind", ""),
        extra=payload.get("extra", ""),
        provider=payload.get("provider"), model=payload.get("model"),
    ))

    session = _remember(
        tr.title, tr.text, notes,
        url=payload["url"], platform=getattr(tr, "platform", ""),
        source=getattr(tr, "source", ""), topic=payload.get("topic", ""),
        note_kind=payload.get("note_kind", ""),
    )
    return {
        "video_id": tr.video_id, "title": tr.title,
        "platform": getattr(tr, "platform", "unknown"),
        "source": getattr(tr, "source", "?"),
        "transcript_chars": len(tr.text),
        "notes": notes, "session": session,
    }


def _research_job(payload: dict, progress) -> dict:
    progress("searching for sources")
    out = find_sources(
        payload["claim"], payload.get("instruction", ""),
        exclude_url=payload.get("exclude_url", ""),
        provider=payload.get("provider"), model=payload.get("model"),
    )
    return out.public()


jobs.register("notes", _notes_job)
jobs.register("research", _research_job)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, user: auth.User = Depends(current_user)):
    row = jobs.get(job_id, user.id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "No such job."})
    return jobs.public(row)


@app.get("/api/jobs")
def job_list(user: auth.User = Depends(current_user)):
    return {"jobs": jobs.recent(user.id)}


@app.post("/api/transcript")
def transcript_endpoint(body: TranscriptIn, user: auth.User = Depends(current_user)):
    try:
        quota.consume(user.id, "transcript")
    except quota.QuotaExceeded as e:
        return JSONResponse(status_code=429, content={"error": str(e)})
    try:
        tr = get_transcript(body.url, body.languages)
    except TranscriptError as e:
        return JSONResponse(status_code=422, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Unexpected error: {e}"})
    return {
        "video_id": tr.video_id,
        "title": tr.title,
        "platform": getattr(tr, "platform", "unknown"),
        "source": getattr(tr, "source", "?"),
        "segments": len(tr.segments),
        "chars": len(tr.text),
        "text": tr.text,
    }


@app.post("/api/notes")
def notes_endpoint(body: NotesIn, user: auth.User = Depends(current_user)):
    """
    Queue the work and hand back a job id.

    This used to run inline, which cannot survive a phone backgrounding the app
    or a proxy's idle timeout. Clients poll /api/jobs/{id}.
    """
    try:
        quota.consume(user.id, "notes")
    except quota.QuotaExceeded as e:
        return JSONResponse(status_code=429, content={"error": str(e)})
    job_id = jobs.submit("notes", {
        "url": body.url, "topic": body.topic, "note_kind": body.note_kind,
        "extra": body.extra, "provider": body.provider, "model": body.model,
        "languages": body.languages,
    }, user_id=user.id)
    return JSONResponse(status_code=202, content={"job": job_id})


@app.post("/api/research")
def research_endpoint(body: ResearchIn, user: auth.User = Depends(current_user)):
    """Queued: searching and reading several videos takes a minute or more."""
    try:
        quota.consume(user.id, "research")
    except quota.QuotaExceeded as e:
        return JSONResponse(status_code=429, content={"error": str(e)})
    ctx = _recall(body.session)
    job_id = jobs.submit("research", {
        "claim": body.claim, "instruction": body.instruction,
        "exclude_url": ctx.get("url", ""),
        "provider": body.provider, "model": body.model,
    }, user_id=user.id)
    return JSONResponse(status_code=202, content={"job": job_id})


@app.post("/api/chat")
def chat_endpoint(body: ChatIn, user: auth.User = Depends(current_user)):
    """Answer a follow-up question about notes we already generated."""
    ctx = _recall(body.session)
    notes = ctx.get("notes") or body.notes
    if not notes:
        return JSONResponse(
            status_code=422,
            content={"error": "That set of notes is no longer in memory. "
                              "Generate the notes again, then ask."},
        )

    try:
        answer = answer_question(ChatRequest(
            question=body.question,
            notes=notes,
            transcript=ctx.get("transcript") or body.transcript,
            title=ctx.get("title") or body.title,
            history=body.history or [],
            provider=body.provider,
            model=body.model,
        ))
    except NoteError as e:
        return JSONResponse(status_code=422, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Chat failed: {e}"})

    return {"answer": answer}


@app.post("/api/search")
def search_endpoint(body: SearchIn, user: auth.User = Depends(current_user)):
    """Find videos on a topic, so the user need not hunt for a link."""
    try:
        results = search_videos(body.query, body.limit, body.platform)
    except TranscriptError as e:
        return JSONResponse(status_code=422, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Search failed: {e}"})
    return {"query": body.query, "results": results, "count": len(results)}


# ── Saved notes ("the bucket") ───────────────────────────────

@app.get("/api/saved")
def saved_list(user: auth.User = Depends(current_user)):
    return {"notes": store.list_notes(user.id), "count": store.count_notes(user.id)}


@app.post("/api/saved")
def saved_create(body: SaveIn, user: auth.User = Depends(current_user)):
    ctx = _recall(body.session)
    notes = body.notes or ctx.get("notes") or ""
    if not notes:
        return JSONResponse(status_code=422,
                            content={"error": "Nothing to save — generate notes first."})
    # File it against the folders that already exist, so related material lands
    # together instead of every save inventing a new folder.
    category, reason, subject = classify_note(
        body.title or ctx.get("title", ""), notes,
        existing=store.list_categories(user.id),
    )

    row = store.save_note(
        user_id=user.id,
        category=category, category_reason=reason, subject=subject,
        title=body.title or ctx.get("title", ""),
        url=body.url or ctx.get("url", ""),
        platform=body.platform or ctx.get("platform", ""),
        source=body.source or ctx.get("source", ""),
        topic=body.topic or ctx.get("topic", ""),
        note_kind=body.note_kind or ctx.get("note_kind", ""),
        notes=notes,
        transcript=ctx.get("transcript", ""),
    )
    return {"saved": row.summary()}


class RefileIn(BaseModel):
    category: str
    reason: str = ""


@app.get("/api/categories")
def categories(user: auth.User = Depends(current_user)):
    return {"categories": store.list_categories(user.id)}


@app.post("/api/saved/{note_id}/category")
def saved_refile(note_id: str, body: RefileIn, user: auth.User = Depends(current_user)):
    cat = " ".join((body.category or "").split())[:48] or "Unfiled"
    if not store.recategorise(note_id, user.id, cat, body.reason):
        return JSONResponse(status_code=404, content={"error": "No saved note with that id."})
    return {"id": note_id, "category": cat}


@app.post("/api/saved/refile")
def saved_refile_all(user: auth.User = Depends(current_user)):
    """
    File everything still sitting in Unfiled.

    Notes saved before automatic filing existed all defaulted to Unfiled, and
    so does anything the classifier could not place at the time. Each is read
    against the folders that exist now, so the library converges instead of
    fragmenting.
    """
    try:
        quota.consume(user.id, "refile")
    except quota.QuotaExceeded as e:
        return JSONResponse(status_code=429, content={"error": str(e)})
    pending = store.notes_in(user.id, "Unfiled")
    # Notes filed before subject lines existed still need one.
    pending += [n for n in store.list_all(user.id)
                if n["category"] != "Unfiled" and not n.get("subject")]
    if not pending:
        return {"moved": [], "remaining": 0, "categories": store.list_categories(user.id)}

    moved, failed = [], 0
    for row in pending:
        try:
            category, reason, subject = classify_note(
                row.get("title", ""), row.get("notes", ""),
                existing=[c for c in store.list_categories(user.id) if c != "Unfiled"],
            )
        except Exception:
            failed += 1
            continue
        if not category or category == "Unfiled":
            category, reason = row["category"], row["category_reason"]
        if category == "Unfiled" and not subject:
            failed += 1
            continue
        store.recategorise(row["id"], user.id, category, reason, subject)
        moved.append({"id": row["id"], "title": row.get("title", ""),
                      "subject": subject, "category": category, "reason": reason})

    return {
        "moved": moved,
        "remaining": failed,
        "categories": store.list_categories(user.id),
    }


@app.get("/api/saved/{note_id}")
def saved_get(note_id: str, user: auth.User = Depends(current_user)):
    row = store.get_note(note_id, user.id)
    if not row:
        return JSONResponse(status_code=404, content={"error": "No saved note with that id."})
    # Rebuild a chat session so follow-up questions work on a reopened note.
    sid = _remember(row.title, row.transcript, row.notes, url=row.url,
                    platform=row.platform, source=row.source,
                    topic=row.topic, note_kind=row.note_kind)
    return {
        "id": row.id, "title": row.title, "url": row.url,
        "platform": row.platform, "source": row.source,
        "topic": row.topic, "note_kind": row.note_kind,
        "notes": row.notes, "transcript_chars": len(row.transcript),
        "category": row.category, "category_reason": row.category_reason,
        "subject": row.subject,
        "created_at": row.created_at, "session": sid,
    }


@app.delete("/api/saved/{note_id}")
def saved_delete(note_id: str, user: auth.User = Depends(current_user)):
    if not store.delete_note(note_id, user.id):
        return JSONResponse(status_code=404, content={"error": "No saved note with that id."})
    return {"deleted": note_id, "count": store.count_notes(user.id)}


# UI (mounted last so /api/* wins)
@app.get("/")
def index():
    # No-store: without it browsers (phones especially) heuristically cache the
    # page and keep showing a stale UI after the app has been updated.
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
    )


# The two files that make the app installable. Both need explicit routes:
# mimetypes does not know .webmanifest, and the worker must be served fresh or
# a stale copy keeps serving a stale shell long after a deploy.
@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/sw.js")
def service_worker():
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
