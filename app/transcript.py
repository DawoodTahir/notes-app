"""
Transcript extraction for online video — YouTube, Instagram, Facebook,
TikTok, X/Twitter, Vimeo, and anything else yt-dlp can reach (~1750 sites).

Strategy, in order:
  1. YouTube only: youtube-transcript-api (fast, no download).
  2. Any platform: yt-dlp caption tracks (manual or auto), parsed from
     json3 / vtt / srt.
  3. Any platform: speech-to-text. Most Instagram, Facebook and TikTok
     videos carry no caption track at all, so the audio is downloaded and
     transcribed by a Whisper-compatible endpoint. This also rescues
     YouTube videos whose caption API is rate-limiting us.

Returns a Transcript with the full text plus timestamped segments.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from . import resolver


class TranscriptError(Exception):
    """Raised when no transcript could be obtained for a video."""


@dataclass
class Segment:
    start: float          # seconds
    text: str


@dataclass
class Transcript:
    video_id: str
    title: str
    language: str
    segments: list[Segment] = field(default_factory=list)
    platform: str = "unknown"

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments).strip()

    @property
    def duration(self) -> float:
        return self.segments[-1].start if self.segments else 0.0


# ─────────────────────────────────────────────────────────────
# Platform detection
# ─────────────────────────────────────────────────────────────

# Host suffix -> friendly label. Anything not listed still works via
# yt-dlp; it is just labelled by its domain.
_PLATFORMS: list[tuple[tuple[str, ...], str]] = [
    (("youtube.com", "youtu.be", "youtube-nocookie.com"), "YouTube"),
    (("instagram.com", "instagr.am", "ddinstagram.com"), "Instagram"),
    (("facebook.com", "fb.watch", "fb.com", "m.facebook.com"), "Facebook"),
    (("tiktok.com", "vm.tiktok.com"), "TikTok"),
    (("twitter.com", "x.com", "t.co"), "X"),
    (("vimeo.com",), "Vimeo"),
    (("dailymotion.com", "dai.ly"), "Dailymotion"),
    (("twitch.tv",), "Twitch"),
    (("reddit.com", "redd.it"), "Reddit"),
    (("linkedin.com",), "LinkedIn"),
    (("bilibili.com",), "Bilibili"),
    (("soundcloud.com",), "SoundCloud"),
]

_YOUTUBE_ID_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")


def detect_platform(url_or_id: str) -> str:
    """Friendly platform name for a URL, or 'YouTube' for a bare video id."""
    s = (url_or_id or "").strip()
    if _YOUTUBE_ID_RE.fullmatch(s):
        return "YouTube"

    host = (urlparse(s if "://" in s else "https://" + s).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    if not host:
        return "unknown"

    for suffixes, label in _PLATFORMS:
        for suf in suffixes:
            if host == suf or host.endswith("." + suf):
                return label
    return host


def normalize_url(url_or_id: str) -> str:
    """A bare YouTube id becomes a full watch URL; everything else passes through."""
    s = (url_or_id or "").strip()
    if _YOUTUBE_ID_RE.fullmatch(s):
        return f"https://www.youtube.com/watch?v={s}"
    if s and "://" not in s:
        return "https://" + s
    return s


def parse_video_id(url_or_id: str) -> str:
    """Extract a YouTube video id. Only meaningful for YouTube URLs."""
    s = url_or_id.strip()
    if _YOUTUBE_ID_RE.fullmatch(s):
        return s
    m = re.search(r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/|/live/)([0-9A-Za-z_-]{11})", s)
    if m:
        return m.group(1)
    m = re.search(r"[0-9A-Za-z_-]{11}", s)
    if m:
        return m.group(0)
    raise TranscriptError(f"Could not extract a video id from: {url_or_id!r}")


# ─────────────────────────────────────────────────────────────
# Caption format parsers
# ─────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)          # inline karaoke/styling tags
    return text.replace("\n", " ").strip()


def _parse_json3(data: dict) -> list[Segment]:
    out: list[Segment] = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = _clean("".join(s.get("utf8", "") for s in segs))
        if text:
            out.append(Segment(start=event.get("tStartMs", 0) / 1000.0, text=text))
    return out


_TS_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})")


def _ts_to_seconds(ts: str) -> float:
    m = _TS_RE.search(ts)
    if not m:
        return 0.0
    h, mnt, s, ms = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def _parse_vtt_or_srt(raw: str) -> list[Segment]:
    """Parse WebVTT or SubRip. Both are cue blocks separated by blank lines."""
    out: list[Segment] = []
    seen: set[str] = set()

    for block in re.split(r"\n\s*\n", raw.replace("\r\n", "\n")):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        cue_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if cue_idx is None:
            continue
        start = _ts_to_seconds(lines[cue_idx].split("-->")[0])
        text = _clean(" ".join(lines[cue_idx + 1:]))
        if not text:
            continue
        # Rolling auto-captions repeat the previous line constantly; drop dupes.
        if text in seen:
            continue
        seen.add(text)
        out.append(Segment(start=start, text=text))
    return out


def _parse_subtitle_file(path: Path) -> list[Segment]:
    suffix = path.suffix.lower()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    if suffix == ".json3":
        try:
            return _parse_json3(json.loads(raw))
        except Exception:
            return []
    if suffix in (".vtt", ".srt"):
        return _parse_vtt_or_srt(raw)
    return []


# ─────────────────────────────────────────────────────────────
# Metadata via yt-dlp (works on every supported site)
# ─────────────────────────────────────────────────────────────

def _auth_opts() -> dict:
    """
    Cookie options for sites that require a login.

    Instagram, Facebook, TikTok and Vimeo increasingly refuse anonymous
    access. Set one of these in .env to use your own session:

      YTDLP_COOKIES_FROM_BROWSER=chrome     (or firefox, safari, edge, brave)
      YTDLP_COOKIES_FILE=/path/to/cookies.txt
    """
    opts: dict = {}
    browser = (os.environ.get("YTDLP_COOKIES_FROM_BROWSER") or "").strip()
    if browser:
        # yt-dlp wants a tuple: (browser, profile, keyring, container)
        opts["cookiesfrombrowser"] = (browser.lower(), None, None, None)
    cookie_file = (os.environ.get("YTDLP_COOKIES_FILE") or "").strip()
    if cookie_file and Path(cookie_file).exists():
        opts["cookiefile"] = cookie_file
    return opts


def _ytdlp_info(url: str) -> dict:
    try:
        import yt_dlp  # type: ignore
    except Exception:
        return {}
    opts = {"quiet": True, "no_warnings": True, "noprogress": True,
            "skip_download": True, "noplaylist": True, **_auth_opts()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception:
        return {}


def _fetch_youtube_title(video_id: str) -> str:
    try:
        u = (
            "https://www.youtube.com/oembed?url="
            f"https://www.youtube.com/watch?v={video_id}&format=json"
        )
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")).get("title", "") or ""
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────
# Strategy 1: youtube-transcript-api (YouTube only)
# ─────────────────────────────────────────────────────────────

def _via_transcript_api(video_id: str, languages: list[str]) -> Optional[list[Segment]]:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except Exception:
        return None

    raw = None
    try:  # new API (>= 1.0)
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=languages)
        raw = [
            {"start": getattr(sn, "start", 0.0), "text": getattr(sn, "text", "")}
            for sn in fetched
        ]
    except Exception:
        raw = None

    if raw is None:  # old API (<= 0.6.x)
        for args in ((video_id, languages), (video_id,)):
            try:
                raw = YouTubeTranscriptApi.get_transcript(*args)  # type: ignore
                break
            except Exception:
                continue

    if not raw:
        return None

    out = [
        Segment(start=float(i.get("start", 0.0)), text=_clean(i.get("text") or ""))
        for i in raw
    ]
    return [s for s in out if s.text] or None


# ─────────────────────────────────────────────────────────────
# Strategy 2: yt-dlp caption tracks (any platform)
# ─────────────────────────────────────────────────────────────

def _via_ytdlp_subs(url: str, languages: list[str]) -> Optional[list[Segment]]:
    try:
        import yt_dlp  # type: ignore
    except Exception:
        return None

    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            # json3 is YouTube-only; vtt/srt cover everyone else.
            "subtitlesformat": "json3/vtt/srt/best",
            "subtitleslangs": languages + [f"{l}.*" for l in languages] + ["en.*", "-live_chat"],
            "outtmpl": str(Path(tmp) / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            **_auth_opts(),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception:
            return None

        files = [
            f for ext in ("json3", "vtt", "srt")
            for f in sorted(Path(tmp).glob(f"*.{ext}"))
        ]
        if not files:
            return None

        # Prefer a file whose name carries a requested language code.
        chosen = files[0]
        for lang in languages:
            match = next((f for f in files if f".{lang}" in f.name), None)
            if match:
                chosen = match
                break

        return _parse_subtitle_file(chosen) or None


# ─────────────────────────────────────────────────────────────
# Strategy 3: speech-to-text (any platform, no captions needed)
# ─────────────────────────────────────────────────────────────

# Whisper endpoints cap upload size; Groq's free tier is 25 MB.
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


_MAX_AUDIO_BYTES = 24 * 1024 * 1024
# Ceiling on what we are willing to pull off the network in the first place.
# _MAX_AUDIO_BYTES is the provider's upload limit and is checked after ffmpeg
# has compressed the audio; this one protects the disk before that happens.
_MAX_SOURCE_BYTES = _env_int("MAX_SOURCE_BYTES", 150 * 1024 * 1024)
# Below this, a "transcript" is too thin to write notes from.
_THIN_TRANSCRIPT_CHARS = 200
_AUDIO_EXTS = (".m4a", ".mp3", ".webm", ".opus", ".ogg", ".wav", ".mp4", ".aac", ".flac")


def speech_enabled() -> bool:
    """Speech-to-text needs a Whisper-compatible endpoint and a key."""
    if os.environ.get("SPEECH_TO_TEXT", "").lower() in ("0", "false", "off"):
        return False
    return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL"))


def _have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg"))


def _compress_audio(src: Path) -> Optional[Path]:
    """
    Re-encode to 16 kHz mono Opus at 24 kbps.

    Whisper resamples to 16 kHz mono anyway, so this discards nothing it would
    have used, and turns an hour of speech into roughly 11 MB. Done with an
    explicit ffmpeg call because yt-dlp's extract-audio postprocessor only
    remuxes when the source is already Opus, leaving the original bitrate.
    """
    if not _have_ffmpeg():
        return None
    out = src.with_name(src.stem + ".16k.opus")
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libopus", "-b:a", "24k",
        str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=900)
    except Exception:
        return None
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        return None
    return out


def _download_audio(url: str, dest: Path) -> Optional[Path]:
    """
    Grab the audio for transcription, compressed when ffmpeg is available.

    Without ffmpeg we ask for the smallest audio-bearing file the site offers
    and hope it fits under the upload cap.
    """
    try:
        import yt_dlp  # type: ignore
    except Exception:
        return None

    have_ffmpeg = _have_ffmpeg()
    opts = {
        "format": ("bestaudio/best" if have_ffmpeg else
                   "bestaudio[filesize<24M]/bestaudio/worst[filesize<24M]/worst"),
        # "bestaudio/best" can fall through to a multi-gigabyte video file.
        # Temp files live on the container's own ephemeral layer, not the
        # mounted disk, so an outsized download kills the instance rather
        # than the database — but four workers can be downloading at once,
        # so the per-download ceiling has to leave room for all of them.
        "max_filesize": _MAX_SOURCE_BYTES,
        "outtmpl": str(dest / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        **_auth_opts(),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception:
        return None

    files = [f for f in dest.iterdir() if f.is_file() and f.suffix.lower() in _AUDIO_EXTS]
    if not files:
        files = [f for f in dest.iterdir() if f.is_file()]
    if not files:
        return None
    raw = min(files, key=lambda f: f.stat().st_size)

    if have_ffmpeg:
        small = _compress_audio(raw)
        # Only keep the re-encode if it actually saved space.
        if small and small.stat().st_size < raw.stat().st_size:
            return small
    return raw


def _download_direct(media_url: str, dest: Path) -> Optional[Path]:
    """Fetch an already-resolved media file, then compress it like any other."""
    target = dest / "media.mp4"
    req = urllib.request.Request(media_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        written = 0
        with urllib.request.urlopen(req, timeout=300) as r, target.open("wb") as fh:
            while chunk := r.read(1 << 16):
                written += len(chunk)
                if written > _MAX_SOURCE_BYTES:
                    # Abort mid-stream: Content-Length can lie, so the running
                    # total is the only honest check.
                    raise ValueError("media exceeds the size limit")
                fh.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        return None
    if not target.exists() or target.stat().st_size == 0:
        return None

    small = _compress_audio(target)
    return small if (small and small.stat().st_size < target.stat().st_size) else target


def _via_speech(url: str, media_url: Optional[str] = None) -> Optional[list[Segment]]:
    if not speech_enabled():
        return None
    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return None

    with tempfile.TemporaryDirectory() as tmp:
        audio = (_download_direct(media_url, Path(tmp)) if media_url
                 else _download_audio(url, Path(tmp)))
        if not audio:
            return None

        size = audio.stat().st_size
        if size > _MAX_AUDIO_BYTES:
            raise TranscriptError(
                f"The media is {size / 1e6:.0f} MB, above the {_MAX_AUDIO_BYTES / 1e6:.0f} MB "
                "limit for speech-to-text. Install ffmpeg so the audio can be "
                "compressed before upload, or try a shorter video."
            )

        # Speech-to-text has its own credentials, falling back to the note
        # provider's. They used to be the same setting, which quietly tied the
        # two together: pointing notes at a provider with no Whisper endpoint
        # (Gemini, for one) took audio transcription down with it — and that
        # is the only path that works for Instagram and Facebook, where there
        # are no captions to fall back on.
        base_url = (os.environ.get("SPEECH_BASE_URL")
                    or os.environ.get("OPENAI_BASE_URL") or None)
        key = (os.environ.get("SPEECH_API_KEY")
               or os.environ.get("OPENAI_API_KEY")
               or ("not-needed" if base_url else None))
        if not key:
            return None
        model = os.environ.get("SPEECH_MODEL") or "whisper-large-v3-turbo"

        client = OpenAI(api_key=key, base_url=base_url)
        try:
            with audio.open("rb") as fh:
                resp = client.audio.transcriptions.create(
                    model=model, file=fh, response_format="verbose_json",
                )
        except Exception as e:
            raise TranscriptError(f"Speech-to-text failed: {e}") from e

        segs = getattr(resp, "segments", None) or []
        out = [
            Segment(start=float(getattr(s, "start", 0.0) or 0.0),
                    text=_clean(getattr(s, "text", "") or ""))
            for s in segs
        ]
        out = [s for s in out if s.text]
        if out:
            return out

        # Some endpoints return plain text with no segment breakdown.
        whole = _clean(getattr(resp, "text", "") or "")
        return [Segment(start=0.0, text=whole)] if whole else None


# ─────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────

def _reject_unfetchable(url: str) -> None:
    """
    Refuse URLs that point back into our own infrastructure.

    Everything the UI sends is a public video link, but the endpoint accepts
    whatever it is given and hands it to yt-dlp, which will happily fetch
    http://169.254.169.254/ (the cloud metadata service), http://127.0.0.1/
    and anything on the private network. The failure is mostly blind, but a
    reachable URL that yt-dlp reads as media gets transcribed and returned in
    the job result — so it is a real read primitive, not a theoretical one.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise TranscriptError("Only http and https links are supported.")

    host = (parsed.hostname or "").strip("[]")
    if not host:
        raise TranscriptError("That link has no host.")

    # Literal addresses are checked directly. Names are left alone: resolving
    # them here would not bind the address yt-dlp later connects to anyway,
    # and the blanket block below on obvious internal names covers the cases
    # that matter for this deployment.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith((".local", ".internal")):
            raise TranscriptError("That link points to a private address.")
        return

    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        raise TranscriptError("That link points to a private address.")


def get_transcript(url_or_id: str, languages: Optional[list[str]] = None) -> Transcript:
    """
    Fetch a transcript for a video on any supported platform.

    Tries caption tracks first (fast, free), then falls back to transcribing
    the audio when the platform has no captions — which is the norm for
    Instagram, Facebook and TikTok.
    """
    languages = languages or ["en", "en-US", "en-GB"]
    if not (url_or_id or "").strip():
        raise TranscriptError("No video link supplied.")

    platform = detect_platform(url_or_id)
    url = normalize_url(url_or_id)
    _reject_unfetchable(url)
    is_youtube = platform == "YouTube"

    segments: Optional[list[Segment]] = None
    source = ""
    video_id = ""
    title = ""
    speech_error: Optional[str] = None
    resolved = None

    # Instagram and Facebook block anonymous access, so a configured video API
    # resolves them to a direct media URL that can simply be downloaded.
    if resolver.should_use_api(platform):
        try:
            resolved = resolver.resolve(url, platform)
            title = resolved.title or ""
        except resolver.ResolveError as e:
            # Fall through to yt-dlp; if that fails too, this is the real reason.
            speech_error = str(e)

    # 1. Best case: the API already returned a transcript — nothing to download.
    if resolved and resolved.segments:
        segments = [
            Segment(start=float(c.get("start", 0.0)), text=_clean(c.get("text", "")))
            for c in resolved.segments
        ]
        segments = [sg for sg in segments if sg.text]
        if segments:
            source = f"api transcript ({resolved.provider.split(':')[0]})"
            speech_error = None

    # 1b. A resolved media URL still needs transcribing.
    if resolved and resolved.media_url and not segments:
        try:
            segments = _via_speech(url, media_url=resolved.media_url)
            if segments:
                source = f"speech-to-text ({resolved.provider.split(':')[0]})"
                speech_error = None
        except TranscriptError as e:
            speech_error = str(e)

    if resolved and resolved.media_url:
        # Reels are frequently music over visuals, where Whisper hears nothing
        # useful but the post's caption carries the actual content. Fall back to
        # it, or prepend it when the speech is too thin to stand alone.
        spoken = " ".join(sg.text for sg in (segments or [])).strip()
        if resolved.caption and len(spoken) < _THIN_TRANSCRIPT_CHARS:
            cap = Segment(start=0.0, text=_clean(resolved.caption))
            if spoken:
                segments = [cap] + list(segments or [])
                source += " + caption"
            else:
                segments = [cap]
                source = f"caption ({resolved.provider.split(':')[0]})"
            speech_error = None

    # 1b. YouTube's caption API — fastest path when it works.
    if not segments and is_youtube:
        try:
            video_id = parse_video_id(url_or_id)
        except TranscriptError:
            video_id = ""
        if video_id:
            segments = _via_transcript_api(video_id, languages)
            if segments:
                source = "youtube-transcript-api"

    # 2. Caption tracks via yt-dlp — works on every supported site.
    if not segments:
        segments = _via_ytdlp_subs(url, languages)
        if segments:
            source = "yt-dlp captions"

    # 3. No captions anywhere: transcribe the audio.
    if not segments:
        try:
            segments = _via_speech(url)
            if segments:
                source = "speech-to-text"
        except TranscriptError as e:
            speech_error = str(e)

    if not segments:
        if speech_error:
            raise TranscriptError(speech_error)
        hint = (
            f"No captions found for this {platform} video, and speech-to-text is "
            "not configured. Set OPENAI_API_KEY (any Whisper-compatible endpoint) "
            "to transcribe videos that have no captions."
            if not speech_enabled() else
            f"Could not get a transcript for this {platform} video. "
            + (
                "Instagram and Facebook block anonymous downloads. Set "
                "APIFY_TOKEN in .env to resolve them through the video API, "
                "or YTDLP_COOKIES_FILE to use your own logged-in session."
                if platform in resolver.API_PLATFORMS else
                "It may be private, age-restricted, region-blocked, or "
                "require a login — YTDLP_COOKIES_FROM_BROWSER=chrome in .env "
                "uses your own session."
            )
        )
        raise TranscriptError(hint)

    # Metadata. Skip the yt-dlp round-trip when YouTube's oembed already answered.
    if is_youtube and video_id:
        title = _fetch_youtube_title(video_id)
    if not title:
        info = _ytdlp_info(url)
        title = info.get("title") or ""
        video_id = video_id or str(info.get("id") or "")
    if not title:
        title = f"{platform} video"
    if not video_id:
        video_id = url

    tr = Transcript(
        video_id=video_id,
        title=title,
        language=languages[0],
        segments=segments,
        platform=platform,
    )
    tr.source = source  # type: ignore[attr-defined]
    return tr


if __name__ == "__main__":
    import sys
    u = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    t = get_transcript(u)
    print(f"Platform: {t.platform}")
    print(f"Title:    {t.title}")
    print(f"Source:   {getattr(t, 'source', '?')} | {len(t.segments)} segments | {len(t.text)} chars")
    print(t.text[:400], "…")


# ─────────────────────────────────────────────────────────────
# Topic search
# ─────────────────────────────────────────────────────────────

# yt-dlp ships search extractors for only a few sites. YouTube and Dailymotion
# are the useful ones here; Facebook and Instagram expose no public search, so
# links from those still have to be pasted in directly.
_SEARCH_PREFIX = {
    "youtube": "ytsearch{n}:{q}",
    "dailymotion": "dmsearch{n}:{q}",
}


def search_videos(query: str, limit: int = 8, platform: str = "youtube") -> list[dict]:
    """Find videos matching a topic. No API key — yt-dlp drives the site search."""
    query = (query or "").strip()
    if not query:
        raise TranscriptError("Type something to search for.")

    template = _SEARCH_PREFIX.get((platform or "youtube").lower())
    if not template:
        raise TranscriptError(
            f"Search is not available for {platform}. Searchable: "
            + ", ".join(sorted(_SEARCH_PREFIX)) + ". Other sites still work "
            "if you paste a link directly."
        )

    try:
        import yt_dlp  # type: ignore
    except Exception:
        raise TranscriptError("yt-dlp is not installed.")

    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "skip_download": True,
        "extract_flat": True,      # metadata only — no per-video round-trips
        **_auth_opts(),
    }
    target = template.format(n=max(1, min(int(limit or 8), 25)), q=query)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False) or {}
    except Exception as e:
        raise TranscriptError(f"Search failed: {e}") from e

    out: list[dict] = []
    for e in info.get("entries") or []:
        if not e:
            continue
        url = e.get("url") or e.get("webpage_url") or ""
        if not url:
            continue
        out.append({
            "title": e.get("title") or "Untitled",
            "url": url,
            "uploader": e.get("uploader") or e.get("channel") or "",
            "duration": e.get("duration"),
            "views": e.get("view_count"),
            "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url") if e.get("thumbnails") else None,
            "platform": detect_platform(url),
        })
    return out
