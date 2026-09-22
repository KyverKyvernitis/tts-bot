"""Comparação barata entre a música pedida e o vídeo encontrado pelo yt-dlp."""
from __future__ import annotations

import re
import unicodedata
from typing import Any


_RUIDO = frozenset({"official", "audio", "video", "music", "hd", "hq", "vevo", "topic", "lyrics", "lyric"})
_VERSOES = ("live", "remix", "cover", "karaoke", "nightcore", "sped up", "slowed")


def _normalizar(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or "").lower())
    raw = "".join(char for char in raw if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", raw))


def avaliar_correspondencia(expected: dict[str, Any], candidate: dict[str, Any]) -> tuple[bool, float, str]:
    """Rejeita apenas discordâncias claras; metadata ausente não é prova de erro."""
    wanted = _normalizar(expected.get("display_title") or expected.get("title") or expected.get("track"))
    found = _normalizar(candidate.get("title") or candidate.get("fulltitle"))
    if not wanted or not found:
        return True, 0.0, "sem_titulo"

    wanted_tokens = set(wanted.split()) - _RUIDO
    found_tokens = set(found.split()) - _RUIDO
    if not wanted_tokens or not found_tokens:
        return True, 0.0, "titulo_incompleto"
    coverage = len(wanted_tokens & found_tokens) / len(wanted_tokens)
    if coverage < (0.5 if len(wanted_tokens) >= 2 else 1.0):
        return False, coverage, "titulo_divergente"

    for variant in _VERSOES:
        if f" {variant} " in f" {found} " and f" {variant} " not in f" {wanted} ":
            return False, coverage, f"versao_{variant.replace(' ', '_')}"

    try:
        duration = float(expected.get("duration") or 0)
        found_duration = float(candidate.get("duration") or 0)
    except (TypeError, ValueError):
        duration = found_duration = 0.0
    if duration > 0 and found_duration > 0:
        mismatch = abs(duration - found_duration)
        if mismatch > max(40.0, duration * 0.25):
            return False, coverage, "duracao_divergente"

    artist = _normalizar(expected.get("display_uploader") or expected.get("artist") or expected.get("uploader"))
    if artist in {"spotify", "deezer", "apple music", "unknown", "desconhecida"}:
        artist = ""
    resolved = f"{found} {_normalizar(candidate.get('uploader') or candidate.get('channel') or '')}"
    artist_tokens = set(artist.split()) - _RUIDO
    artist_overlap = len(artist_tokens & set(resolved.split())) / len(artist_tokens) if artist_tokens else 0.0
    # O artista pode não constar do título/canal de um upload válido. Nesse
    # caso não rejeitamos uma faixa com título e duração coerentes.
    score = coverage * 10.0 + artist_overlap * 3.0
    if duration and found_duration:
        score -= abs(duration - found_duration) / 120.0
    return True, score, "ok"


def busca_alternativa(query: str) -> str:
    text = str(query or "").strip()
    prefix, sep, rest = text.partition(":")
    if sep and prefix.lower().startswith(("ytsearch", "ytmsearch")):
        return f"{prefix.rstrip('0123456789')}3:{rest}"
    return f"ytsearch3:{text}"
