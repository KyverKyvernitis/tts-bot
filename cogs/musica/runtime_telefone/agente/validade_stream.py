"""Validade conservadora de URLs assinadas, independente do cache de metadata."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit


class DiscordAttachmentError(ValueError):
    """Erro apresentável sem expor URLs assinadas ou credenciais."""


_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"}
_AUDIO_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".wma", ".aiff", ".weba"}
_GENERIC_MIME = {"", "application/octet-stream", "binary/octet-stream"}
_AUDIO_FILE_MIME = {"application/ogg", "application/x-ogg", "application/flac", "application/x-flac"}
_REF_FIELDS = ("guild_id", "channel_id", "message_id", "attachment_id")


def normalize_reference(raw: Any, guild_id: int) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise DiscordAttachmentError("Referência da mídia inválida.")
    try:
        ref = {key: int(raw[key]) for key in _REF_FIELDS}
    except (KeyError, ValueError, TypeError):
        raise DiscordAttachmentError("Referência da mídia inválida.") from None
    if any(value <= 0 for value in ref.values()) or ref["guild_id"] != guild_id:
        raise DiscordAttachmentError("A mídia não pertence a este servidor.")
    return ref


def valid_cdn_url(raw: Any) -> str:
    url = str(raw or "").strip()
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"cdn.discordapp.com", "media.discordapp.net"}
            or not re.match(r"^/(?:attachments|ephemeral-attachments)/\d+/\d+/", parsed.path)
            or parsed.username or parsed.password or parsed.port
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise DiscordAttachmentError("O Discord não retornou uma URL válida para esta mídia.") from None
    return url


def _positive_duration(value: Any) -> float | None:
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and result > 0 else None


async def fetch_discord_attachment(client: Any, reference: dict[str, int]) -> dict[str, Any]:
    # REST fornece uma URL assinada nova; o arquivo é transmitido direto do
    # CDN para o reprodutor e nunca é baixado na VPS.
    try:
        raw = await client.http.get_message(reference["channel_id"], reference["message_id"])
    except Exception:
        raise DiscordAttachmentError("Não consegui acessar a mídia no Discord. Confira as permissões de leitura da mensagem.") from None
    if not isinstance(raw, dict) or str(raw.get("guild_id") or reference["guild_id"]) != str(reference["guild_id"]):
        raise DiscordAttachmentError("A mensagem da mídia não corresponde ao servidor.")
    for attachment in raw.get("attachments") or ():
        if not isinstance(attachment, dict) or str(attachment.get("id")) != str(reference["attachment_id"]):
            continue
        mime = str(attachment.get("content_type") or "").split(";", 1)[0].lower()
        ext = os.path.splitext(str(attachment.get("filename") or "").lower())[1]
        if not (mime.startswith(("video/", "audio/")) or (mime in _GENERIC_MIME and ext in (_VIDEO_EXTENSIONS | _AUDIO_EXTENSIONS))
                or (mime in _AUDIO_FILE_MIME and ext in _AUDIO_EXTENSIONS)):
            raise DiscordAttachmentError("Esse anexo não é um vídeo ou áudio reproduzível.")
        item = dict(attachment)
        item["url"] = valid_cdn_url(item.get("url"))
        return item
    raise DiscordAttachmentError("A mídia foi removida ou não está mais acessível.")


async def probe_discord_audio(url: str, *, executable: str = "ffprobe", timeout: float = 10.0) -> dict[str, Any]:
    valid_cdn_url(url)
    command = [
        executable, "-v", "error", "-rw_timeout", "6000000", "-select_streams", "a",
        "-show_entries", "stream=index,codec_type,codec_name,duration,bit_rate,sample_rate,channels:stream_disposition=default:format=duration",
        "-of", "json", url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise DiscordAttachmentError("Não consegui iniciar a verificação de áudio da mídia.") from None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=max(1.0, float(timeout)))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise DiscordAttachmentError("A verificação do áudio demorou demais. A mídia não entrou na fila.") from None
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
            await proc.communicate()
        raise
    if proc.returncode != 0 or len(stdout) > 65536:
        raise DiscordAttachmentError("Não consegui ler o áudio dessa mídia. Ela não entrou na fila.")
    try:
        payload = json.loads(stdout)
        streams = payload.get("streams") if isinstance(payload, dict) else None
        audio_streams = [item for item in streams or () if isinstance(item, dict) and item.get("codec_type") == "audio"
                         and str(item.get("codec_name") or "").lower() not in {"", "none"}]
        audio = next((item for item in audio_streams if (item.get("disposition") or {}).get("default")), None)
        if audio is None:
            audio = audio_streams[0] if audio_streams else None
    except (ValueError, TypeError, AttributeError):
        audio = None
        payload = {}
    if not isinstance(audio, dict):
        raise DiscordAttachmentError("Esta mídia não contém uma faixa de áudio reproduzível.")
    duration = _positive_duration(audio.get("duration")) or _positive_duration((payload.get("format") or {}).get("duration"))
    if duration is None:
        raise DiscordAttachmentError("Não consegui confirmar a duração do áudio. A mídia não entrou na fila.")
    try:
        stream_index = int(audio["index"])
        if stream_index < 0:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise DiscordAttachmentError("Não consegui identificar a faixa de áudio da mídia.") from None
    try:
        bitrate = max(0, int(audio.get("bit_rate") or 0) // 1000)
        sample_rate = max(0, int(audio.get("sample_rate") or 0))
        channels = max(0, int(audio.get("channels") or 0))
    except (ValueError, TypeError):
        bitrate, sample_rate, channels = 0, 0, 0
    return {
        "duration": duration,
        "audio_stream_index": stream_index,
        "audio_codec": str(audio["codec_name"])[:40],
        "audio_abr": bitrate,
        "audio_sample_rate": sample_rate,
        "audio_channels": channels,
    }


def prazo_stream(
    url: str,
    resolved_at: float,
    fallback_seconds: float,
    *,
    max_age_seconds: float = 1800.0,
    margin_seconds: float = 60.0,
) -> float:
    """Interpreta a expiração assinada de googlevideo e anexos Discord.

    O prazo assinado não garante disponibilidade: 403/queda de rede continuam
    invalidando a URL. O teto local limita a vida do cache mesmo com relógio ou
    assinatura inesperados, e nunca prolongamos uma assinatura vencida.
    """
    fallback = float(resolved_at) + max(0.0, float(fallback_seconds))
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"}:
            return fallback
        if host.endswith(".googlevideo.com"):
            expires = float(parse_qs(parsed.query).get("expire", [""])[0])
        elif host in {"cdn.discordapp.com", "media.discordapp.net"} and parsed.path.startswith(("/attachments/", "/ephemeral-attachments/")):
            expires = float(int(parse_qs(parsed.query).get("ex", [""])[0], 16))
        else:
            return fallback
        if not math.isfinite(expires) or expires <= 0:
            return fallback
        signed = time.monotonic() + expires - time.time() - max(0.0, margin_seconds)
        return min(signed, float(resolved_at) + max(1.0, max_age_seconds))
    except (TypeError, ValueError, OverflowError):
        return fallback
