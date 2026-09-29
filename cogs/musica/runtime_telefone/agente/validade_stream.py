"""Validade conservadora de URLs assinadas, independente do cache de metadata."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import aiohttp
import discord


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


def initial_discord_cdn_url(raw: Any, reference: dict[str, int], *, min_remaining_seconds: float = 20.0) -> str:
    """Use a fresh URL from the selected Discord message without another REST GET.

    The hint is transient. A queued item still keeps only ``attachment_ref`` and
    fetches a new signed URL when it reaches the front of the queue.
    """
    try:
        url = valid_cdn_url(raw)
        if len(url) > 4096:
            return ""
        parsed = urlsplit(url)
        path = parsed.path.split("/", 4)
        if (
            len(path) != 5
            or path[1] not in {"attachments", "ephemeral-attachments"}
            or path[2] != str(reference["channel_id"])
            or path[3] != str(reference["attachment_id"])
            or not path[4]
            or parsed.fragment
        ):
            return ""
        params = parse_qs(parsed.query)
        if not params.get("hm"):
            return ""
        expiry = int(params.get("ex", [""])[0], 16)
        if expiry - time.time() < min_remaining_seconds:
            return ""
        return url
    except (KeyError, ValueError, TypeError, OverflowError, DiscordAttachmentError):
        return ""


def _positive_duration(value: Any) -> float | None:
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def discord_voice_audio_hint(metadata: dict[str, Any], attachment: dict[str, Any], *, from_message: bool) -> dict[str, Any] | None:
    """Use os metadados do Discord somente para mensagens de voz Ogg identificadas."""
    if metadata.get("attachment_is_voice_message") is not True:
        return None
    duration = _positive_duration(metadata.get("attachment_duration_hint"))
    if duration is None or duration > 1200.0:
        return None
    filename = str(attachment.get("filename") or metadata.get("attachment_filename") or "").lower()
    mime = str(metadata.get("attachment_content_type") or "").lower()
    if not filename.endswith(".ogg") or mime != "audio/ogg":
        return None
    if from_message:
        if not urlsplit(str(attachment.get("url") or "")).path.lower().endswith(".ogg"):
            return None
    else:
        remote_duration = _positive_duration(attachment.get("duration_secs"))
        if (
            str(attachment.get("content_type") or "").split(";", 1)[0].lower() != "audio/ogg"
            or attachment.get("waveform") is None
            or remote_duration is None
            or abs(remote_duration - duration) > 2.0
        ):
            return None
        duration = remote_duration
    # Voice messages do Discord são Ogg/Opus; o primeiro frame PCM ainda é
    # exigido antes do ACK quando esta faixa inicia imediatamente.
    return {
        "duration": duration,
        "audio_stream_index": 0,
        "audio_codec": "opus",
        "audio_ext": "ogg",
        "audio_abr": 0,
        "audio_sample_rate": 48000,
        "audio_channels": 0,
    }


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
        first_audio_stream_index = int(audio_streams[0]["index"]) if audio_streams else None
        audio = next((item for item in audio_streams if (item.get("disposition") or {}).get("default")), None)
        if audio is None:
            audio = audio_streams[0] if audio_streams else None
    except (ValueError, TypeError, AttributeError, KeyError):
        audio = None
        first_audio_stream_index = None
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
        "first_audio_stream_index": first_audio_stream_index,
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


# Arquivo de músicas no módulo de validação de anexos já distribuído ao worker.
MAX_AUDIO = 20 * 1024 * 1024
_MARKER = "music-archive:v1:"
_MARKER_V2 = "music-archive-v2-"
_MARKER_V3 = "music-archive-v3-"
_MARKER_V4 = "music-archive-v4-"
_MARKER_V5 = "music-archive-v5-"
_PUBLIC_FOOTER = "Arquivo de músicas"
_IMAGE_HOSTS = {"i.ytimg.com", "img.youtube.com", "i.scdn.co", "e-cdns-images.dzcdn.net",
                "is1-ssl.mzstatic.com", "is2-ssl.mzstatic.com", "is3-ssl.mzstatic.com",
                "i1.sndcdn.com", "i2.sndcdn.com", "i3.sndcdn.com", "i4.sndcdn.com"}
_AUDIO_EXTS = {".ogg", ".opus", ".webm", ".m4a", ".mp3", ".aac", ".flac", ".wav", ".weba"}


class ArchiveTooLarge(ValueError):
    pass


class ArchiveDurationTooLong(ValueError):
    pass


def _archive_url(origin: str, key: str, *, version: int = 2) -> str:
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("link público da faixa ausente")
    marker = _MARKER_V5 if version >= 5 else _MARKER_V4 if version >= 4 else _MARKER_V3 if version >= 3 else _MARKER_V2
    fragment = "&".join(filter(None, (parsed.fragment, marker + key)))
    return urlunsplit(parsed._replace(fragment=fragment))


def _archive_origin(url: str) -> str:
    return urlunsplit(urlsplit(str(url or ""))._replace(fragment=""))


def _archive_duration_text(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 60}:{total % 60:02d}"


def _archive_duration_seconds(value: str) -> float:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        minutes, seconds = map(int, text.split(":"))
        if seconds >= 60:
            raise ValueError("duração inválida")
        return float(minutes * 60 + seconds)
    return float(text)  # mensagem v1: duração decimal em segundos


def _archive_audio_filename(title: str, ext: str) -> str:
    clean = re.sub(r"\s+", " ", re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", title)).strip(" .")[:90].rstrip(" .")
    return (clean or "Música") + ext.lower()


def _archive_cover_confirmed(raw: dict) -> bool:
    embed = (raw.get("embeds") or [{}])[0]
    thumbnail = (embed.get("thumbnail") or {}).get("url")
    try:
        path = urlsplit(valid_cdn_url(thumbnail)).path
        return any(Path(str(item.get("filename") or "")).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                   and urlsplit(valid_cdn_url(item.get("url"))).path == path
                   for item in raw.get("attachments") or [])
    except (DiscordAttachmentError, ValueError, TypeError, AttributeError, IndexError):
        return False


def _archive_metadata(raw: dict, key: str, bot_id: int, ref: dict) -> dict:
    if str((raw.get("author") or {}).get("id")) != str(bot_id):
        raise DiscordAttachmentError("A mensagem do arquivo não foi enviada pelo bot.")
    if str(raw.get("guild_id") or ref["guild_id"]) != str(ref["guild_id"]):
        raise DiscordAttachmentError("O arquivo pertence a outro servidor.")
    embeds = raw.get("embeds") or []
    embed = embeds[0] if embeds and isinstance(embeds[0], dict) else {}
    footer = str((embed.get("footer") or {}).get("text") or "")
    if footer not in {_MARKER + key, _PUBLIC_FOOTER}:
        raise DiscordAttachmentError("A mensagem não pertence ao arquivo de música esperado.")
    if footer == _PUBLIC_FOOTER:
        markers = [part for part in urlsplit(str(embed.get("url") or "")).fragment.split("&")
                   if part.startswith((_MARKER_V2, _MARKER_V3, _MARKER_V4, _MARKER_V5))]
        expected = ({_MARKER_V3 + key, _MARKER_V4 + key, _MARKER_V5 + key} if ref.get("forum_id")
                    else {_MARKER_V2 + key})
        if markers and (len(markers) != 1 or markers[0] not in expected):
            raise DiscordAttachmentError("Identificador do arquivo diferente da faixa esperada.")
    fields = {str(field.get("name") or ""): str(field.get("value") or "") for field in embed.get("fields") or [] if isinstance(field, dict)}
    origin = fields.get("Fonte", "")
    emoji = origin.split(" ", 1)[0][:100]
    if not emoji or len(emoji) > 100:
        raise DiscordAttachmentError("A origem do áudio está ausente no arquivo.")
    try:
        index = int(fields.get("Índice de áudio", "0"))
        duration = _archive_duration_seconds(fields["Duração"])
        if not 0 <= index <= 16 or not 0 < duration <= 600:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise DiscordAttachmentError("O arquivo não tem áudio e duração verificados.") from None
    target = next((item for item in raw.get("attachments") or [] if str(item.get("id")) == str(ref["attachment_id"])), None)
    if not isinstance(target, dict):
        raise DiscordAttachmentError("O áudio do arquivo foi removido.")
    name = str(target.get("filename") or "").lower()
    if Path(name).suffix not in _AUDIO_EXTS:
        raise DiscordAttachmentError("O anexo do arquivo não é áudio.")
    if raw.get("channel_id") and str(raw["channel_id"]) != str(ref["channel_id"]):
        raise DiscordAttachmentError("A mensagem do arquivo pertence a outro post.")
    quality = fields.get("Qualidade", "")
    abr = re.search(r"\b(\d{1,5})\s*kbps\b", quality, re.I)
    sample_rate = re.search(r"\b(\d{2,3})\s*kHz\b", fields.get("Formato", ""), re.I)
    return {"url": valid_cdn_url(target.get("url")), "emoji": emoji, "source": origin.split(" ", 1)[-1],
            "filename": name, "audio_stream_index": index, "duration": duration,
            "audio_abr": int(abr.group(1)) if abr else 0,
            "audio_sample_rate": int(sample_rate.group(1)) * 1000 if sample_rate else 0}


class ArchiveMixin:
    def _archive_init(self) -> None:
        self._archive_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=32)
        self._archive_results: dict[str, dict] = {}
        self._archive_task: asyncio.Task | None = None
        self._archive_active = ""

    async def cmd_archive_enqueue(self, body: dict) -> dict:
        key = str(body.get("archive_key") or "")
        track = body.get("track") if isinstance(body.get("track"), dict) else {}
        try:
            duration = float(track["duration"])
            guild_id = int(body["guild_id"])
            channel_id = int(body["archive_channel_id"])
        except (TypeError, ValueError, KeyError):
            raise ValueError("metadados incompletos para arquivar") from None
        url = str(track.get("webpage_url") or "").strip()
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        try:
            public_host = not ipaddress.ip_address(host).is_private
        except ValueError:
            public_host = host not in {"localhost", "localhost.localdomain"} and "." in host
        if (not re.fullmatch(r"[a-f0-9]{32}", key) or not 0 < duration <= 600
                or guild_id <= 0 or channel_id <= 0 or parsed.scheme != "https"
                or not public_host or host.endswith(("discord.com", "discordapp.com"))):
            raise ValueError("faixa inelegível para o arquivo")
        if key in self._archive_results:
            previous = self._archive_results[key]
            ref = previous.get("reference") if isinstance(previous.get("reference"), dict) else {}
            same_channel = int(ref.get("guild_id") or 0) == guild_id and int(ref.get("forum_id") or 0) == channel_id
            if previous.get("status") == "done" and same_channel and int(previous.get("presentation") or 1) >= 5:
                return {"ok": True, **previous}
            self._archive_results.pop(key, None)
        if key != self._archive_active and key not in {item.get("key") for item in list(self._archive_queue._queue)}:
            self._archive_queue.put_nowait({"key": key, "track": track, "guild_id": guild_id,
                                            "channel_id": channel_id, "emoji": str(body.get("source_emoji") or "🎵")[:100],
                                            "retry": bool(body.get("archive_retry")),
                                            "existing_ref": body.get("archive_ref") if isinstance(body.get("archive_ref"), dict) else {}})
        if self._archive_task is None or self._archive_task.done():
            self._archive_task = asyncio.create_task(self._archive_loop(), name="music-archive-upload")
        return {"ok": True, "status": "queued"}

    async def cmd_archive_status(self, body: dict) -> dict:
        key = str(body.get("archive_key") or "")
        if key in self._archive_results:
            return {"ok": True, **self._archive_results[key]}
        if key == self._archive_active or key in {item.get("key") for item in list(self._archive_queue._queue)}:
            return {"ok": True, "status": "working"}
        return {"ok": True, "status": "missing"}

    async def cmd_archive_cleanup(self, body: dict) -> dict:
        key = str(body.get("archive_key") or "")
        current = normalize_reference(body.get("archive_ref"), int(body.get("guild_id") or 0))
        previous = normalize_reference(body.get("previous_ref"), current["guild_id"])
        if (not re.fullmatch(r"[a-f0-9]{32}", key) or current == previous
                or int(body["archive_ref"].get("forum_id") or 0) <= 0):
            raise ValueError("referências de migração inválidas")
        raw = await self.client.http.get_message(current["channel_id"], current["message_id"])
        _archive_metadata(raw, key, int(self.client.user.id), {**current, "forum_id": int(body["archive_ref"]["forum_id"])})
        if self._archive_in_use(previous["message_id"]):
            return {"ok": True, "removed": False}
        try:
            old_channel = self.client.get_channel(previous["channel_id"]) or await self.client.fetch_channel(previous["channel_id"])
            message = await old_channel.fetch_message(previous["message_id"])
        except discord.NotFound:
            return {"ok": True, "removed": True}
        item = {"key": key, "track": body.get("track") if isinstance(body.get("track"), dict) else {}}
        if (message.author.id != self.client.user.id or not self._archive_message_version(message, item)
                or not any(ref.id == previous["attachment_id"] for ref in message.attachments)):
            raise ValueError("a mensagem anterior não pertence a esta música")
        if isinstance(old_channel, discord.Thread) and previous.get("forum_id"):
            await old_channel.delete(reason="Arquivo de música migrado para outro fórum")
        else:
            await message.delete()
        return {"ok": True, "removed": True}

    async def _archive_loop(self) -> None:
        while not self._archive_queue.empty():
            item = await self._archive_queue.get()
            key = item["key"]
            self._archive_active = key
            try:
                result = await self._archive_one(item)
                self._archive_results[key] = result
                self.log("archive_finished", status=result["status"], key=key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._archive_results[key] = {"status": "failed"}
                self.log("archive_failed", key=key, error=f"{type(exc).__name__}: {str(exc)[:180]}")
            finally:
                self._archive_active = ""
                self._archive_queue.task_done()
                if len(self._archive_results) > 512:
                    for old in list(self._archive_results)[:128]:
                        self._archive_results.pop(old, None)

    def _archive_message_version(self, message, item: dict) -> int:
        if message.author.id != self.client.user.id or not message.embeds:
            return 0
        embed = message.embeds[0]
        footer = str(getattr(getattr(embed, "footer", None), "text", "") or "")
        if footer == _MARKER + item["key"]:
            return 1
        if footer != _PUBLIC_FOOTER:
            return 0
        url = str(embed.url or "")
        fragments = urlsplit(url).fragment.split("&")
        if _MARKER_V5 + item["key"] in fragments:
            return 5
        if _MARKER_V4 + item["key"] in fragments:
            return 4
        if _MARKER_V3 + item["key"] in fragments:
            return 3
        if _MARKER_V2 + item["key"] in fragments:
            return 2
        if any(fragment.startswith((_MARKER_V2, _MARKER_V3, _MARKER_V4, _MARKER_V5)) for fragment in fragments):
            return 0
        # Alguns clientes descartam o fragmento do título do embed. O link
        # original, a autoria e o canal ainda identificam a mensagem.
        origin = str(item["track"].get("original_url") or item["track"].get("webpage_url") or "")
        return (3 if item.get("forum") else 2) if origin and _archive_origin(url) == _archive_origin(origin) else 0

    async def _archive_existing(self, forum, item: dict):
        # No caminho comum de criação não há varredura de posts. O índice aponta
        # diretamente para a mensagem; a enumeração só recupera um ACK perdido.
        reference = item.get("existing_ref") or {}
        try:
            if int(reference.get("forum_id") or 0) == forum.id:
                thread = self.client.get_channel(int(reference["channel_id"])) or await self.client.fetch_channel(int(reference["channel_id"]))
                message = await thread.fetch_message(int(reference["message_id"]))
                version = self._archive_message_version(message, {**item, "forum": True})
                if version >= 3:
                    return message, version
        except (discord.HTTPException, KeyError, TypeError, ValueError):
            pass
        if not item.get("retry"):
            return None
        name = self._archive_post_name(item["track"])
        async def inspect(thread):
            if thread.name != name or thread.owner_id != self.client.user.id:
                return None
            async for message in thread.history(limit=1, oldest_first=True):
                version = self._archive_message_version(message, {**item, "forum": True})
                if version >= 3 and any(Path(a.filename).suffix.lower() in _AUDIO_EXTS for a in message.attachments):
                    return message, version
            return None

        for thread in forum.threads:
            found = await inspect(thread)
            if found is not None:
                return found
        active_threads = getattr(forum.guild, "active_threads", None)
        if callable(active_threads):
            for thread in await active_threads():
                if thread.parent_id == forum.id:
                    found = await inspect(thread)
                    if found is not None:
                        return found
        async for thread in forum.archived_threads(limit=None):
            found = await inspect(thread)
            if found is not None:
                return found
        return None

    @staticmethod
    def _archive_post_name(track: dict) -> str:
        title = str(track.get("display_title") or track.get("title") or "Música")
        return re.sub(r"\s+", " ", title).strip()[:100] or "Música"

    async def _archive_legacy(self, item: dict):
        reference = item.get("existing_ref") or {}
        if not reference or int(reference.get("guild_id") or 0) != item["guild_id"]:
            return None
        try:
            channel = self.client.get_channel(int(reference["channel_id"])) or await self.client.fetch_channel(int(reference["channel_id"]))
            message = await channel.fetch_message(int(reference["message_id"]))
            if (self._archive_message_version(message, item)
                    and any(a.id == int(reference["attachment_id"]) for a in message.attachments)):
                return message
        except (discord.HTTPException, KeyError, TypeError, ValueError):
            pass
        return None

    async def _archive_cover(self, url: str, destination: Path) -> Path | None:
        try:
            parts = urlsplit(url)
            if parts.scheme != "https" or (parts.hostname or "").lower() not in _IMAGE_HOSTS:
                return None
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200 or not response.headers.get("Content-Type", "").lower().startswith("image/"):
                        return None
                    # read(n) pode retornar apenas o primeiro bloco disponível.
                    # Espere o fim da resposta antes de converter a imagem.
                    image = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        image.extend(chunk)
                        if len(image) > 2 * 1024 * 1024:
                            return None
                    if not image:
                        return None
                    # O FFmpeg aceita JPEG incompleto e preenche os pixels
                    # ausentes de verde. Exija o terminador antes de transcodar.
                    if image.startswith(b"\xff\xd8\xff") and not image.rstrip(b"\x00\r\n \t").endswith(b"\xff\xd9"):
                        return None
                    if image.startswith(b"\x89PNG\r\n\x1a\n") and not image.endswith(b"IEND\xaeB`\x82"):
                        return None
                    if image.startswith(b"RIFF") and image[8:12] == b"WEBP" and int.from_bytes(image[4:8], "little") + 8 != len(image):
                        return None
                    source = destination / "capa-entrada"
                    await asyncio.to_thread(source.write_bytes, bytes(image))
                    # Nunca publique bytes AVIF/WebP com nome .jpg. FFmpeg
                    # valida a imagem e cria um JPEG que o cliente consegue ler.
                    return await self._archive_jpeg(source, destination / "capa.jpg")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return None

    async def _archive_cover_for_track(self, track: dict, destination: Path) -> Path | None:
        candidates = [str(track.get(field) or "") for field in ("display_thumbnail", "thumbnail")]
        for field in ("webpage_url", "original_url"):
            try:
                parts = urlsplit(str(track.get(field) or ""))
            except ValueError:
                continue
            host = (parts.hostname or "").lower().removeprefix("www.")
            video_id = (parse_qs(parts.query).get("v") or [""])[0] if host in {
                "youtube.com", "m.youtube.com", "music.youtube.com"
            } and parts.path == "/watch" else parts.path.strip("/") if host == "youtu.be" else ""
            if parts.scheme == "https" and re.fullmatch(r"[A-Za-z0-9_-]{3,64}", video_id):
                candidates.append(f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")
        for url in dict.fromkeys(candidates):
            if url:
                cover = await self._archive_cover(url, destination)
                if cover is not None:
                    return cover
        return None

    async def _archive_jpeg(self, source: Path, output: Path) -> Path | None:
        proc = await asyncio.create_subprocess_exec(
            self.ffmpeg_executable, "-nostdin", "-v", "error", "-threads", "1", "-y",
            "-i", str(source), "-frames:v", "1", "-vf", "scale=768:768:force_original_aspect_ratio=decrease,format=yuvj420p",
            "-q:v", "2", str(output), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=12)
        except asyncio.CancelledError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        except (asyncio.TimeoutError, OSError):
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return None
        if proc.returncode != 0 or not output.is_file() or not 0 < output.stat().st_size <= 2 * 1024 * 1024:
            return None
        if output.read_bytes()[:3] != b"\xff\xd8\xff":
            return None
        probe = await asyncio.create_subprocess_exec(
            self.ffprobe_executable, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,pix_fmt,width,height", "-of", "json", str(output),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            data, _ = await asyncio.wait_for(probe.communicate(), timeout=5)
        except asyncio.TimeoutError:
            probe.kill()
            with contextlib.suppress(Exception):
                await probe.wait()
            return None
        try:
            info = (json.loads(data).get("streams") or [{}])[0] if probe.returncode == 0 else {}
            if (info.get("codec_name") == "mjpeg" and info.get("pix_fmt") == "yuvj420p"
                    and int(info.get("width") or 0) > 0 and int(info.get("height") or 0) > 0):
                return output
        except (ValueError, KeyError, TypeError):
            pass
        return None

    async def _archive_existing_cover(self, message, folder: Path) -> Path | None:
        if message is None:
            return None
        attachment = next((item for item in message.attachments
                           if Path(item.filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                           and int(item.size or 0) <= 2 * 1024 * 1024), None)
        if attachment is None:
            return None
        try:
            source = folder / "capa-antiga"
            await attachment.save(str(source))
            if source.stat().st_size > 2 * 1024 * 1024:
                return None
            return await self._archive_jpeg(source, folder / "capa.jpg")
        except (discord.HTTPException, OSError):
            return None

    async def _archive_probe(self, audio: Path) -> dict:
        proc = await asyncio.create_subprocess_exec(
            self.ffprobe_executable, "-v", "error", "-show_entries",
            "format=duration,bit_rate:stream=index,codec_type,codec_name,bit_rate,sample_rate,channels", "-of", "json", str(audio),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            data, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
        except BaseException:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        return json.loads(data) if proc.returncode == 0 else {}

    async def _archive_audio_ready(self, audio: Path, folder: Path) -> tuple[Path, float, int, str] | None:
        probe = await self._archive_probe(audio)
        streams = probe.get("streams") or []
        primary = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        if primary is None or any(stream.get("codec_type") == "video" for stream in streams):
            return None
        codec = str(primary.get("codec_name") or "").lower()
        if audio.suffix.lower() in {".webm", ".weba"} and codec in {"opus", "vorbis"}:
            # Só troca o contêiner. Os pacotes Opus/Vorbis permanecem intactos.
            ogg = folder / "audio.ogg"
            proc = await asyncio.create_subprocess_exec(
                self.ffmpeg_executable, "-nostdin", "-v", "error", "-y", "-i", str(audio),
                "-map", "0:a:0", "-c:a", "copy", str(ogg), stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=30)
            except BaseException:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                raise
            if proc.returncode == 0 and ogg.is_file() and 0 < ogg.stat().st_size <= MAX_AUDIO:
                audio = ogg
                probe = await self._archive_probe(audio)
                streams = probe.get("streams") or []
                primary = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
                if primary is None or any(stream.get("codec_type") == "video" for stream in streams):
                    return None
                codec = str(primary.get("codec_name") or "").lower()
        duration = float((probe.get("format") or {}).get("duration") or 0)
        if duration > 600:
            raise ArchiveDurationTooLong("áudio com mais de 10 minutos")
        if duration <= 0:
            return None
        return audio, duration, int(primary["index"]), codec

    async def _archive_wait_stable_voice(self) -> None:
        for _ in range(45):
            busy = any(
                st.status in {"starting", "preparing", "reconnecting", "tts_direct"}
                or bool(getattr(st, "voice_runtime_recovery_pending", False))
                for st in self.states.values()
            )
            if not busy and not any(task and not task.done() for task in self._active_resolve_tasks.values()):
                return
            await asyncio.sleep(2)
        raise RuntimeError("voz ocupada; arquivamento adiado")

    def _archive_in_use(self, message_id: int) -> bool:
        return any(
            any(int((track.archive_ref or {}).get("message_id") or 0) == message_id
                for track in ([state.current] if state.current is not None else []) + list(state.queue))
            for state in self.states.values()
        )

    async def _archive_download(self, item: dict, folder: Path) -> Path | None:
        track = item["track"]
        url = str(track["webpage_url"])
        base = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-warnings",
                "--no-progress", "--no-part", "--max-filesize", "20M", "--socket-timeout", "12",
                "--format-sort", str(getattr(self, "ytdlp_sort", "abr,acodec,asr")),
                "--match-filter", "duration <= 600", "-o", str(folder / "audio.%(ext)s")]
        if any(st.status in {"playing", "paused"} for st in self.states.values()):
            base.extend(("--limit-rate", "512K"))
        cookies = str(getattr(self, "cookies_file", "") or "")
        if cookies and os.path.isfile(cookies):
            base += ["--cookies", cookies]
        for runtime in str(getattr(self, "js_runtimes", "") or "").split(","):
            if runtime.strip():
                base += ["--js-runtimes", runtime.strip()]
        oversized = False
        for fmt in ("bestaudio[filesize<20M]/bestaudio[filesize_approx<20M]/bestaudio[abr<=256]/bestaudio",
                    "bestaudio[abr<=192]"):
            if self._archive_active != item["key"]:
                return None
            for stale in folder.glob("audio.*"):
                stale.unlink(missing_ok=True)
            cmd = [*base, "-f", fmt, url]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL,
                                                         stderr=asyncio.subprocess.PIPE)
            try:
                _out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
            except BaseException:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                raise
            candidates = [path for path in folder.glob("audio.*") if path.is_file()]
            if proc.returncode == 0 and len(candidates) == 1 and 0 < candidates[0].stat().st_size <= MAX_AUDIO:
                return candidates[0]
            oversized |= any(path.stat().st_size > MAX_AUDIO for path in candidates)
            stderr = (err or b"").lower()
            if b"does not pass filter duration <= 600" in stderr:
                raise ArchiveDurationTooLong("música com mais de 10 minutos")
            oversized |= b"larger than max-filesize" in stderr or b"file is larger" in stderr
            self.log("archive_format_retry", key=item["key"], format=fmt[:32], error=(err or b"")[-160:].decode("utf-8", "replace"))
        if oversized:
            raise ArchiveTooLarge("a melhor faixa compatível ultrapassa 20 MiB")
        raise RuntimeError("não consegui baixar um formato de áudio válido")

    async def _archive_finish_cover(self, message, item: dict, cover_name: str):
        cover = next((attachment for attachment in message.attachments if attachment.filename == cover_name), None)
        if cover is None:
            raise ValueError("Discord não confirmou a imagem enviada")
        cdn = valid_cdn_url(cover.url)
        # URLs de anexos vêm assinadas. O Discord renova a URL sem query nos
        # embeds, inclusive depois que a assinatura original expira.
        cdn = urlunsplit(urlsplit(cdn)._replace(query="", fragment=""))
        embed = message.embeds[0].copy()
        origin = str(item["track"].get("original_url") or item["track"].get("webpage_url") or "")[:500]
        embed.url = _archive_url(origin, item["key"], version=5)
        embed.set_thumbnail(url=cdn)
        return await message.edit(embed=embed, attachments=list(message.attachments),
                                  allowed_mentions=discord.AllowedMentions.none())

    async def _archive_one(self, item: dict) -> dict:
        await self.client.wait_until_ready()
        forum = self.client.get_channel(item["channel_id"])
        if forum is None:
            forum = await self.client.fetch_channel(item["channel_id"])
        if not isinstance(forum, discord.ForumChannel) or forum.is_media() or forum.guild.id != item["guild_id"]:
            raise ValueError("fórum do arquivo inválido")
        existing = await self._archive_existing(forum, item)
        if existing is not None:
            message, version = existing
            attachment = next((ref for ref in message.attachments if Path(ref.filename).suffix.lower() in _AUDIO_EXTS), None)
            if attachment is not None:
                ref = {"guild_id": forum.guild.id, "forum_id": forum.id,
                       "channel_id": message.channel.id, "message_id": message.id, "attachment_id": attachment.id}
                raw = await self.client.http.get_message(ref["channel_id"], message.id)
                parsed = _archive_metadata(raw, item["key"], self.client.user.id, ref)
                if version >= 4 and not _archive_cover_confirmed(raw):
                    version = 3
                if version < 5:
                    with tempfile.TemporaryDirectory(prefix="music-cover-") as location:
                        folder = Path(location)
                        cover = await self._archive_cover_for_track(item["track"], folder)
                        if cover is not None:
                            await self._archive_wait_stable_voice()
                            cover_name = "capa-v5.jpg"
                            file = discord.File(cover, filename=cover_name)
                            try:
                                staged = message.embeds[0].copy()
                                staged.set_thumbnail(url=None)
                                message = await message.edit(embed=staged, attachments=[attachment, file],
                                                             allowed_mentions=discord.AllowedMentions.none())
                            finally:
                                file.close()
                            try:
                                message = await self._archive_finish_cover(message, item, cover_name)
                                version = 5
                            except (discord.HTTPException, ValueError, IndexError) as exc:
                                self.log("archive_cover_retry", key=item["key"], error=type(exc).__name__)
                        else:
                            self.log("archive_cover_missing", key=item["key"])
                raw = await self.client.http.get_message(ref["channel_id"], message.id)
                parsed = _archive_metadata(raw, item["key"], self.client.user.id, ref)
                if version == 5 and not _archive_cover_confirmed(raw):
                    self.log("archive_cover_retry", key=item["key"], error="cover_not_confirmed")
                    version = 3
                return {"status": "done", "reference": ref, "emoji": parsed["emoji"], "presentation": version}
        previous = await self._archive_legacy(item)
        if previous is not None and self._archive_in_use(previous.id):
            raise RuntimeError("o arquivo antigo está sendo reproduzido; atualização adiada")
        # Sem usar o scheduler de resolução do player, ffmpeg de playback ou
        # o loop principal para tarefas de disco. A voz mantém prioridade.
        await asyncio.sleep(3)
        await self._archive_wait_stable_voice()
        with tempfile.TemporaryDirectory(prefix="music-archive-") as location:
            folder = Path(location)
            try:
                old_attachment = next((ref for ref in previous.attachments if Path(ref.filename).suffix.lower() in _AUDIO_EXTS), None) if previous is not None else None
                if old_attachment is not None:
                    audio = folder / ("anterior" + Path(old_attachment.filename).suffix.lower())
                    if int(old_attachment.size or 0) > MAX_AUDIO:
                        return {"status": "too_large"}
                    await old_attachment.save(str(audio))
                else:
                    audio = await self._archive_download(item, folder)
            except ArchiveTooLarge:
                return {"status": "too_large"}
            except ArchiveDurationTooLong:
                return {"status": "ineligible"}
            if audio is None:
                return {"status": "failed"}
            try:
                ready = await self._archive_audio_ready(audio, folder)
            except ArchiveDurationTooLong:
                return {"status": "ineligible"}
            if ready is None:
                return {"status": "failed"}
            audio, duration, audio_index, codec = ready
            details = await self._archive_probe(audio)
            audio_stream = next((entry for entry in details.get("streams") or []
                                 if entry.get("codec_type") == "audio" and int(entry.get("index", -1)) == audio_index), {})
            try:
                bitrate = int(audio_stream.get("bit_rate") or 0) // 1000
                sample_rate = int(audio_stream.get("sample_rate") or 0)
            except (ValueError, TypeError):
                bitrate, sample_rate = 0, 0
            if bitrate <= 0:
                bitrate = max(1, round(audio.stat().st_size * 8 / duration / 1000))
            cover = await self._archive_cover_for_track(item["track"], folder)
            if cover is None:
                cover = await self._archive_existing_cover(previous, folder)
            title = str(item["track"].get("display_title") or item["track"].get("title") or "Música")[:240]
            source = str(item["track"].get("display_source") or item["track"].get("source") or "Áudio")[:80]
            origin = str(item["track"].get("original_url") or item["track"].get("webpage_url") or "")[:500]
            embed = discord.Embed(title=title, url=_archive_url(origin, item["key"], version=3))
            prior_emoji = next((str(field.value).split(" ", 1)[0] for field in previous.embeds[0].fields
                                if field.name == "Fonte"), "") if previous is not None and previous.embeds else ""
            embed.add_field(name="Fonte", value=f"{prior_emoji or item['emoji']} {source}", inline=True)
            embed.add_field(name="Duração", value=_archive_duration_text(duration), inline=True)
            sample_text = f" · {sample_rate // 1000} kHz" if sample_rate and sample_rate % 1000 == 0 else ""
            embed.add_field(name="Formato", value=f"{codec.upper()} · {audio.suffix.lstrip('.').upper()}{sample_text}", inline=True)
            embed.add_field(name="Qualidade", value=f"≈{bitrate} kbps", inline=True)
            if audio_index != 0:
                embed.add_field(name="Índice de áudio", value=str(audio_index), inline=True)
            embed.set_footer(text=_PUBLIC_FOOTER)
            await self._archive_wait_stable_voice()
            audio_name = _archive_audio_filename(title, audio.suffix)
            files = ([discord.File(cover, filename=cover.name)] if cover else []) + [discord.File(audio, filename=audio_name)]
            try:
                tags = [forum.available_tags[0]] if forum.flags.require_tag else []
                post = await forum.create_thread(name=self._archive_post_name(item["track"]),
                                                 embed=embed, files=files, applied_tags=tags,
                                                 allowed_mentions=discord.AllowedMentions.none())
                message, thread = post.message, post.thread
            finally:
                for file in files:
                    file.close()
            ref_attachment = next((attachment for attachment in message.attachments if attachment.filename == audio_name), None)
            if ref_attachment is None:
                raise ValueError("Discord não confirmou o áudio enviado")
            ref = {"guild_id": forum.guild.id, "forum_id": forum.id, "channel_id": thread.id,
                   "message_id": message.id, "attachment_id": ref_attachment.id}
            version = 3
            if cover is not None:
                try:
                    message = await self._archive_finish_cover(message, item, cover.name)
                    version = 5
                except (discord.HTTPException, ValueError, IndexError) as exc:
                    self.log("archive_cover_retry", key=item["key"], error=type(exc).__name__)
            else:
                self.log("archive_cover_missing", key=item["key"])
            raw = await self.client.http.get_message(thread.id, message.id)
            parsed = _archive_metadata(raw, item["key"], self.client.user.id, ref)
            if version == 5 and not _archive_cover_confirmed(raw):
                self.log("archive_cover_retry", key=item["key"], error="cover_not_confirmed")
                version = 3
            return {"status": "done", "reference": ref, "emoji": parsed["emoji"], "presentation": version}
