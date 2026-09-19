"""Utilitários puros do runtime de música no telefone.

Este módulo não depende de Discord nem do processo HTTP do agente. Ele concentra
normalização de payloads e seleção do stream retornado pelo yt-dlp.
"""
from __future__ import annotations

import contextlib
import re
from typing import Any
from urllib.parse import urlsplit


def short_text(value: object, limit: int = 180) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit].rstrip() if len(text) > limit else text


def safe_id(value: object) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return 0


def looks_like_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        return bool(parsed.scheme and parsed.netloc)
    except Exception:
        return False


def float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None and str(value) != "" else None
    except Exception:
        return None


def metadata_text(value: Any, *, limit: int = 180) -> str:
    text = short_text(value, limit)
    lower = text.lower()
    if lower in {"youtube", "link", "música", "musica", "worker-agent", "music-agent-ytdlp", "worker-ytdlp", "desconhecida", "unknown"}:
        return ""
    if "desconhecida" in lower and ("youtube" in lower or "worker" in lower):
        return ""
    return text


def duration_from_ytdlp(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text.upper() in {"NA", "N/A", "NONE", "NULL"}:
        return None
    try:
        return float(text)
    except Exception:
        pass
    parts = text.split(":")
    try:
        total = 0
        for part in parts:
            total = total * 60 + int(float(part))
        return float(total)
    except Exception:
        return None


def format_audio_info(fmt: dict[str, Any], *, url: str = "") -> dict[str, Any]:
    info = {"stream_url": url or str(fmt.get("url") or "").strip()}
    info["audio_format_id"] = short_text(fmt.get("format_id"), 40)
    info["audio_ext"] = short_text(fmt.get("ext"), 20).lower()
    info["audio_codec"] = short_text(fmt.get("acodec") or fmt.get("codec"), 40).lower()
    with contextlib.suppress(Exception):
        info["audio_abr"] = int(float(fmt.get("abr") or fmt.get("tbr") or 0))
    return info


def select_stream_info(entry: dict[str, Any]) -> dict[str, Any]:
    for item in entry.get("requested_downloads") or []:
        if isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                return format_audio_info(item, url=url)
    url = str(entry.get("url") or "").strip()
    if url.startswith(("http://", "https://")) and "youtube.com/watch" not in url and "youtu.be/" not in url:
        return format_audio_info(entry, url=url)
    best: dict[str, Any] = {}
    best_score = -1.0
    for fmt in entry.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        candidate = str(fmt.get("url") or "").strip()
        if not candidate.startswith(("http://", "https://")):
            continue
        acodec = str(fmt.get("acodec") or "").lower()
        if acodec in {"", "none"}:
            continue
        score = float(fmt.get("abr") or fmt.get("tbr") or 0)
        if str(fmt.get("vcodec") or "").lower() in {"", "none"}:
            score += 10000
        if score > best_score:
            best = format_audio_info(fmt, url=candidate)
            best_score = score
    return best


def select_stream_url(entry: dict[str, Any]) -> str:
    return str(select_stream_info(entry).get("stream_url") or "")


# Aliases privados mantêm compatibilidade com imports e testes antigos enquanto
# o entrypoint monolítico é desmontado em waves.
_looks_like_url = looks_like_url
_float_or_none = float_or_none
_metadata_text = metadata_text
_duration_from_ytdlp = duration_from_ytdlp
_format_audio_info = format_audio_info
_select_stream_info = select_stream_info
_select_stream_url = select_stream_url
