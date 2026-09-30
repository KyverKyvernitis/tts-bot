"""Resolução de páginas oficiais para o arquivo, sem baixar áudio na VPS."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .runtime_telefone.agente.correspondencia import avaliar_correspondencia


class SourceMismatchError(RuntimeError):
    """Página ou áudio acessível, porém incompatível com a música aprendida."""


def _bandcamp_page(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and host.endswith(".bandcamp.com")
                and parsed.path.startswith("/track/") and not parsed.username
                and not parsed.password and parsed.port is None)
    except ValueError:
        return False


def _bandcamp_audio(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and host.endswith(".bcbits.com")
                and not parsed.username and not parsed.password and parsed.port is None)
    except ValueError:
        return False


def resolve_bandcamp_source(url: str, expected: dict) -> dict:
    """Valida título/duração e retorna só a URL assinada, para o Android baixar."""
    if not _bandcamp_page(url):
        raise SourceMismatchError("página Bandcamp inválida")
    try:
        import curl_cffi  # noqa: F401 - permite ao extrator emular o navegador
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp com curl_cffi indisponível na VPS") from None

    try:
        with yt_dlp.YoutubeDL({
            "quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "socket_timeout": 12, "retries": 1,
            "format": "bestaudio/best", "format_sort": ["abr", "acodec", "asr"],
        }) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        if re.search(r"\b404\b|\bnot found\b|\btrack not available\b", str(exc), re.I):
            raise SourceMismatchError("faixa ausente neste domínio") from None
        raise RuntimeError("Bandcamp temporariamente indisponível") from None
    if not isinstance(info, dict):
        raise RuntimeError("Bandcamp não retornou uma faixa")
    title = str(info.get("title") or "").strip()
    valid, _score, reason = avaliar_correspondencia(expected, info)
    try:
        wanted = float(expected.get("duration") or 0)
        duration = float(info.get("duration") or 0)
    except (ValueError, TypeError):
        wanted = duration = 0
    if not title or not valid or not 0 < wanted <= 600 or not 0 < duration <= 600 or abs(wanted - duration) > max(5, wanted * 0.03):
        raise SourceMismatchError(f"fonte Bandcamp não corresponde à faixa ({reason})")
    stream_url = str(info.get("url") or "")
    if not _bandcamp_audio(stream_url) or str(info.get("acodec") or "none").lower() == "none":
        raise SourceMismatchError("Bandcamp não retornou áudio direto confiável")
    return {"webpage_url": url, "stream_url": stream_url, "duration": duration,
            "title": title[:160], "source": "Bandcamp"}
