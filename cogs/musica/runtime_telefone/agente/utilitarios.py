"""Utilitários puros do runtime de música no telefone.

Este módulo não depende de Discord nem do processo HTTP do agente. Ele concentra
normalização de payloads e seleção do stream retornado pelo yt-dlp.
"""
from __future__ import annotations

import contextlib
import re
from typing import Any
from urllib.parse import urlsplit


# Formato padrão: prioriza áudio-only Opus já em 48 kHz para reduzir perdas e
# evitar resampling quando a fonte oferece esse caminho. Os fallbacks preservam
# compatibilidade com outros extratores/serviços sem exigir filtros no FFmpeg.
DEFAULT_YTDLP_AUDIO_FORMAT = (
    "bestaudio[acodec=opus][asr=48000]/"
    "bestaudio[acodec=opus]/"
    "bestaudio[asr=48000]/"
    "bestaudio/best"
)


def _int_metric(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except Exception:
        return 0


def audio_format_score(fmt: dict[str, Any]) -> float:
    """Pontua um formato de áudio sem executar decode/FFmpeg.

    O áudio-only domina vídeo+áudio; bitrate continua sendo o principal critério
    de qualidade entre candidatos equivalentes, com bônus pequenos para Opus,
    48 kHz e estéreo. Assim evitamos escolher Opus muito comprimido sobre uma
    alternativa claramente melhor apenas pelo nome do codec.
    """
    acodec = str(fmt.get("acodec") or fmt.get("codec") or "").strip().lower()
    if acodec in {"", "none"}:
        return float("-inf")
    vcodec = str(fmt.get("vcodec") or "").strip().lower()
    audio_only = vcodec in {"", "none"}
    try:
        abr = float(fmt.get("abr") or fmt.get("tbr") or 0.0)
    except Exception:
        abr = 0.0
    abr = max(0.0, min(512.0, abr))
    asr = _int_metric(fmt.get("asr") or fmt.get("sample_rate"))
    channels = _int_metric(fmt.get("audio_channels") or fmt.get("channels"))

    codec_bonus = 0.0
    if "opus" in acodec:
        codec_bonus = 6000.0
    elif acodec.startswith(("mp4a", "aac")) or "aac" in acodec:
        codec_bonus = 4000.0
    elif "vorbis" in acodec:
        codec_bonus = 2500.0
    elif "mp3" in acodec:
        codec_bonus = 1000.0

    sample_bonus = 3000.0 if asr == 48000 else (1200.0 if asr == 44100 else 0.0)
    channel_bonus = 1200.0 if channels == 2 else (600.0 if channels > 2 else 0.0)
    return (100000.0 if audio_only else 0.0) + (abr * 100.0) + codec_bonus + sample_bonus + channel_bonus


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
    info["audio_abr"] = _int_metric(fmt.get("abr") or fmt.get("tbr"))
    info["audio_sample_rate"] = _int_metric(fmt.get("asr") or fmt.get("sample_rate"))
    info["audio_channels"] = _int_metric(fmt.get("audio_channels") or fmt.get("channels"))
    return info


def _best_stream_from_formats(formats: Any) -> dict[str, Any]:
    best: dict[str, Any] = {}
    best_score = float("-inf")
    for fmt in formats or []:
        if not isinstance(fmt, dict):
            continue
        candidate = str(fmt.get("url") or "").strip()
        if not candidate.startswith(("http://", "https://")):
            continue
        score = audio_format_score(fmt)
        if score > best_score:
            best = format_audio_info(fmt, url=candidate)
            best_score = score
    return best


def select_stream_info(entry: dict[str, Any]) -> dict[str, Any]:
    # requested_downloads é a escolha final do yt-dlp para o seletor `-f`;
    # respeite-a primeiro e só faça scoring local quando o extrator não a der.
    selected = _best_stream_from_formats(entry.get("requested_downloads"))
    if selected:
        return selected
    url = str(entry.get("url") or "").strip()
    if url.startswith(("http://", "https://")) and "youtube.com/watch" not in url and "youtu.be/" not in url:
        return format_audio_info(entry, url=url)
    return _best_stream_from_formats(entry.get("formats"))


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
_audio_format_score = audio_format_score
