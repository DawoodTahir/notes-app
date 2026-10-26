"""
Media resolution for platforms that refuse anonymous access.

Instagram and Facebook serve a login wall to anyone not signed in, so yt-dlp
cannot reach them from a server. This module resolves such links through a
third-party API that runs its own proxies and accounts, which keeps your own
Meta credentials out of the deployment entirely.

Providers are pluggable; Apify is the one implemented. Configure with:

    VIDEO_API_PROVIDER=apify
    APIFY_TOKEN=apify_api_...
    APIFY_INSTAGRAM_ACTOR=apify~instagram-scraper         (optional override)
    APIFY_FACEBOOK_ACTOR=apify~facebook-posts-scraper     (optional override)

Routing is per-URL and automatic:

    VIDEO_RESOLVER=auto   yt-dlp first, API only where yt-dlp is blocked (default)
    VIDEO_RESOLVER=api    force every link through the API
    VIDEO_RESOLVER=ytdlp  never use the API

Only public content is reachable this way. A private account or a friends-only
post is invisible to a third-party scraper — no provider can fetch those.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

# Platforms "auto" sends to the API. Instagram and Facebook block anonymous
# access outright; YouTube is here because it rate-limits datacenter IPs hard,
# which is exactly where a deployed copy of this app runs.
API_PLATFORMS = {"Instagram", "Facebook", "YouTube"}

# Some actors return a finished transcript rather than a media URL — far
# cheaper and faster, since nothing is downloaded or sent to Whisper.
TRANSCRIPT_PLATFORMS = {"YouTube"}

APIFY_BASE = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
# Verified to exist against the Apify store. Both accept the shotgun payload
# below: instagram-scraper reads `directUrls`, facebook-posts-scraper reads
# `startUrls`, and the simpler community downloaders read `url`.
_DEFAULT_ACTORS = {
    "Instagram": "apify~instagram-scraper",
    "Facebook": "apify~facebook-posts-scraper",
    "YouTube": "pintostudio~youtube-transcript-scraper",
}


class ResolveError(Exception):
    """Raised when a media URL could not be resolved to a downloadable file."""


@dataclass
class Resolved:
    """A direct, downloadable media URL plus whatever metadata came with it."""
    media_url: str = ""
    segments: list = field(default_factory=list)   # [{"start": float, "text": str}]
    caption: str = ""        # often the real content on a music-only reel
    title: str = ""
    uploader: str = ""
    duration: Optional[float] = None
    thumbnail: str = ""      # a poster frame, where the API supplies one
    provider: str = ""


def mode() -> str:
    return (os.environ.get("VIDEO_RESOLVER") or "auto").strip().lower()


def provider_name() -> str:
    return (os.environ.get("VIDEO_API_PROVIDER") or "apify").strip().lower()


def api_configured() -> bool:
    if provider_name() == "apify":
        return bool(os.environ.get("APIFY_TOKEN"))
    return False


def should_use_api(platform: str) -> bool:
    """Decide whether this platform goes through the paid API."""
    m = mode()
    if m == "ytdlp" or not api_configured():
        return False
    if m == "api":
        return True
    return platform in API_PLATFORMS          # "auto"


# ─────────────────────────────────────────────────────────────
# Apify
# ─────────────────────────────────────────────────────────────

def _actor_for(platform: str) -> str:
    env_key = f"APIFY_{platform.upper()}_ACTOR"
    return (os.environ.get(env_key) or _DEFAULT_ACTORS.get(platform) or "").strip()


def _post_json(url: str, payload: dict, timeout: int) -> list:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 401:
            raise ResolveError("The video API rejected the token (401). Check APIFY_TOKEN.")
        if e.code == 404:
            raise ResolveError(f"The configured actor does not exist (404): {detail}")
        if e.code == 408:
            raise ResolveError("The video API timed out resolving that link.")
        raise ResolveError(f"Video API error {e.code}: {detail}")
    except Exception as e:
        raise ResolveError(f"Could not reach the video API: {e}")

    try:
        data = json.loads(body)
    except Exception:
        raise ResolveError("The video API returned a response that was not JSON.")
    return data if isinstance(data, list) else [data]


# Field names vary between community-built actors, so probe the common ones
# rather than assuming one schema.
_URL_KEYS = ("videoUrl", "video_url", "downloadUrl", "download_url", "url",
             "mediaUrl", "media_url", "src", "hdUrl", "sdUrl")
_TITLE_KEYS = ("title", "caption", "text", "description", "name")
_UPLOADER_KEYS = ("ownerUsername", "username", "author", "uploader", "channel", "ownerFullName")


def _first(d: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _title_from_caption(caption: str, limit: int = 80) -> str:
    """
    A caption is a paragraph; a title is a phrase.

    Take the first sentence, and fall back to a word-boundary trim so the
    heading never runs to 300 characters.
    """
    text = " ".join((caption or "").split())
    if not text:
        return ""
    for stop in (". ", "! ", "? ", " — ", " · ", "\n"):
        head = text.split(stop)[0]
        if 12 <= len(head) <= limit:
            return head.rstrip(" .!?")
    if len(text) <= limit:
        return text.rstrip(" .!?")
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"


_SEG_TEXT_KEYS = ("text", "subtitle", "line", "content")


def _pick_segments(item: dict) -> list:
    """
    Pull timestamped segments out of a transcript actor's item.

    Shapes seen in the wild: {"data": [...]}, {"transcript": [...]}, or the
    list of cues at the top level of the item itself.
    """
    for key in ("data", "transcript", "segments", "captions", "subtitles"):
        seq = item.get(key)
        if isinstance(seq, list) and seq:
            out = []
            for cue in seq:
                if not isinstance(cue, dict):
                    continue
                text = _first(cue, _SEG_TEXT_KEYS)
                if not text:
                    continue
                try:
                    start = float(cue.get("start", cue.get("offset", 0)) or 0)
                except (TypeError, ValueError):
                    start = 0.0
                out.append({"start": start, "text": " ".join(text.split())})
            if out:
                return out
    return []


def _pick_media(item: dict) -> str:
    """Find a direct media URL in an actor's result item."""
    direct = _first(item, _URL_KEYS)
    if direct.startswith("http") and not _looks_like_page(direct):
        return direct

    # Some actors nest the media under a list of variants.
    for key in ("videoVersions", "videos", "media", "formats", "downloads"):
        seq = item.get(key)
        if isinstance(seq, list):
            for entry in seq:
                if isinstance(entry, dict):
                    u = _first(entry, _URL_KEYS)
                    if u.startswith("http") and not _looks_like_page(u):
                        return u
                elif isinstance(entry, str) and entry.startswith("http"):
                    return entry
    return ""


_PAGE_HOSTS = ("instagram.com", "facebook.com", "fb.watch")


def _looks_like_page(u: str) -> bool:
    """
    Reject the original post URL echoed back as a 'result'.

    Match on host boundaries, not substrings: the real media lives on
    scontent-*.cdninstagram.com, which *contains* "instagram.com" but is
    not the page host.
    """
    host = (urllib.parse.urlparse(u).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _PAGE_HOSTS)


def _resolve_apify(url: str, platform: str) -> Resolved:
    token = os.environ.get("APIFY_TOKEN", "").strip()
    actor = _actor_for(platform)
    if not actor:
        raise ResolveError(f"No API actor configured for {platform}.")

    endpoint = APIFY_BASE.format(actor=urllib.parse.quote(actor, safe="~"))
    endpoint += "?" + urllib.parse.urlencode({"token": token, "timeout": 180, "clean": "true"})

    # Actors disagree on the input key; send the usual spellings together.
    payload = {"urls": [url], "url": url, "directUrls": [url],
               "startUrls": [{"url": url}],
               "videoUrl": url, "targetLanguage": "en"}
    items = _post_json(endpoint, payload, timeout=240)
    if not items:
        raise ResolveError(
            f"The video API returned nothing for that {platform} link. "
            "It may be private, deleted, or not a video post."
        )

    # A finished transcript beats a media URL: nothing to download or transcribe.
    for item in items:
        if isinstance(item, dict):
            segs = _pick_segments(item)
            if segs:
                return Resolved(
                    segments=segs,
                    title=_title_from_caption(_first(item, _TITLE_KEYS)),
                    uploader=_first(item, _UPLOADER_KEYS),
                    provider=f"apify:{actor}",
                )

    for item in items:
        if not isinstance(item, dict):
            continue
        media = _pick_media(item)
        if media:
            dur = item.get("duration") or item.get("videoDuration")
            try:
                dur = float(dur) if dur is not None else None
            except (TypeError, ValueError):
                dur = None
            caption = _first(item, _TITLE_KEYS)
            return Resolved(
                media_url=media,
                caption=caption,
                title=_title_from_caption(caption),
                uploader=_first(item, _UPLOADER_KEYS),
                duration=dur,
                provider=f"apify:{actor}",
            )

    raise ResolveError(
        f"The video API found the {platform} post but no transcript or "
        "downloadable video in it. Is the link a video (not a photo)?"
    )


def resolve(url: str, platform: str) -> Resolved:
    """Resolve a page URL to a direct media URL via the configured provider."""
    if not api_configured():
        raise ResolveError(
            f"{platform} needs a video API, which is not configured. "
            "Set APIFY_TOKEN in .env (see .env.example)."
        )
    if provider_name() == "apify":
        return _resolve_apify(url, platform)
    raise ResolveError(f"Unknown VIDEO_API_PROVIDER: {provider_name()!r}")
