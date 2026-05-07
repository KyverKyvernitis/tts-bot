from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, quote, unquote, urlparse, urlunparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_SPOTIFY_RE = re.compile(r"https?://open\.spotify\.com/(track|album|playlist|episode)/", re.IGNORECASE)
_APPLE_RE = re.compile(r"https?://music\.apple\.com/", re.IGNORECASE)
_DEEZER_RE = re.compile(r"https?://(?:www\.)?deezer\.com/", re.IGNORECASE)
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

_DIRECT_AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".webm")
_METADATA_TITLE_PATTERNS = (
    re.compile(r'<meta[^>]+property=["\\\']og:title["\\\'][^>]+content=["\\\']([^"\\\']+)', re.IGNORECASE),
    re.compile(r'<meta[^>]+content=["\\\']([^"\\\']+)["\\\'][^>]+property=["\\\']og:title["\\\']', re.IGNORECASE),
    re.compile(r'<meta[^>]+name=["\\\']twitter:title["\\\'][^>]+content=["\\\']([^"\\\']+)', re.IGNORECASE),
    re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL),
)


@dataclass(frozen=True)
class UrlProfile:
    raw: str
    canonical: str
    host: str
    is_url: bool
    is_youtube: bool = False
    is_metadata_only: bool = False
    is_direct_audio: bool = False
    youtube_video_id: str = ""


def looks_like_url(value: str) -> bool:
    return bool(_URL_RE.search(value or ""))


def describe_url(value: str) -> UrlProfile:
    raw = (value or "").strip()
    if not looks_like_url(raw):
        return UrlProfile(raw=raw, canonical=raw, host="", is_url=False)

    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower().removeprefix("www.")
    path_lower = (parsed.path or "").lower()
    canonical = raw
    video_id = ""

    if host in _YOUTUBE_HOSTS or host.endswith(".youtube.com"):
        video_id = extract_youtube_video_id(raw)
        if video_id:
            canonical = f"https://www.youtube.com/watch?v={video_id}"
        return UrlProfile(
            raw=raw,
            canonical=canonical,
            host=host,
            is_url=True,
            is_youtube=True,
            is_metadata_only=False,
            is_direct_audio=False,
            youtube_video_id=video_id,
        )

    metadata_only = bool(_SPOTIFY_RE.search(raw) or _APPLE_RE.search(raw) or _DEEZER_RE.search(raw))
    direct_audio = any(path_lower.endswith(ext) for ext in _DIRECT_AUDIO_EXTENSIONS)
    return UrlProfile(
        raw=raw,
        canonical=canonical,
        host=host,
        is_url=True,
        is_youtube=False,
        is_metadata_only=metadata_only,
        is_direct_audio=direct_audio,
    )


def extract_youtube_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    query = parse_qs(parsed.query or "")

    if "youtu.be" in host:
        candidate = path.strip("/").split("/")[0]
        return _clean_youtube_id(candidate)
    if "youtube.com" in host:
        if query.get("v"):
            return _clean_youtube_id(query["v"][0])
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            return _clean_youtube_id(parts[1])
    return ""


def _clean_youtube_id(candidate: str) -> str:
    candidate = (candidate or "").strip()
    match = re.search(r"[A-Za-z0-9_-]{6,}", candidate)
    return match.group(0) if match else ""


def clean_metadata_title(text: str) -> str:
    value = html.unescape(re.sub(r"\s+", " ", text or "").strip())
    if not value:
        return ""

    # Limpezas comuns de título de página/oEmbed.
    value = re.sub(r"\s*[-|•]\s*(YouTube|Spotify|Apple Music|Deezer)\s*$", "", value, flags=re.IGNORECASE)
    value = value.replace(" - song and lyrics by ", " ")
    value = value.replace(" | Spotify", "")
    value = re.sub(r"\bListen to\b", "", value, flags=re.IGNORECASE).strip()
    return re.sub(r"\s+", " ", value).strip(" -|•\t\n\r")


def spotify_oembed_url(url: str) -> str:
    return "https://open.spotify.com/oembed?url=" + quote(url, safe="")


def fetch_metadata_title(url: str, *, timeout: float = 6.0) -> str:
    """Busca título público sem API key. Usado só como fallback."""
    candidates = []
    if _SPOTIFY_RE.search(url or ""):
        candidates.append((spotify_oembed_url(url), True))
    candidates.append((url, False))

    for candidate_url, is_json in candidates:
        try:
            request = Request(
                candidate_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; DiscordMusicBot/1.0)",
                    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                },
            )
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL do usuário, usado só para metadata pública
                raw = response.read(256_000)
                content = raw.decode("utf-8", errors="ignore")
        except Exception:
            logger.debug("metadata title fetch failed for %s", candidate_url, exc_info=True)
            continue

        if is_json:
            try:
                data = json.loads(content)
                title = clean_metadata_title(str(data.get("title") or ""))
                if title:
                    return title
            except Exception:
                logger.debug("metadata json parse failed for %s", candidate_url, exc_info=True)

        for pattern in _METADATA_TITLE_PATTERNS:
            match = pattern.search(content)
            if not match:
                continue
            title = clean_metadata_title(match.group(1))
            if title:
                return title
    return ""


def slug_search_terms(url: str) -> str:
    parsed = urlparse(url)
    parts = [unquote(p) for p in parsed.path.split("/") if p and not p.isdigit()]
    if not parts:
        return ""
    useful = []
    for part in parts[-3:]:
        part = re.sub(r"[-_]+", " ", part)
        part = re.sub(r"\.(html?|php|aspx?)$", "", part, flags=re.IGNORECASE)
        part = re.sub(r"\s+", " ", part).strip()
        if part and not re.fullmatch(r"[A-Za-z0-9_-]{15,}", part):
            useful.append(part)
    return " ".join(useful).strip()


def unique_queries(*queries: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for query in queries:
        clean = re.sub(r"\s+", " ", (query or "").strip())
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out
