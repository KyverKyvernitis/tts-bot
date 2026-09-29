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
from urllib.parse import parse_qs, urlsplit

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
_IMAGE_HOSTS = {"i.ytimg.com", "img.youtube.com", "i.scdn.co", "i1.sndcdn.com", "i2.sndcdn.com", "i3.sndcdn.com", "i4.sndcdn.com"}
_AUDIO_EXTS = {".ogg", ".opus", ".webm", ".m4a", ".mp3", ".aac", ".flac", ".wav", ".weba"}


class ArchiveTooLarge(ValueError):
    pass


class ArchiveDurationTooLong(ValueError):
    pass


def _archive_metadata(raw: dict, key: str, bot_id: int, ref: dict) -> dict:
    if str((raw.get("author") or {}).get("id")) != str(bot_id):
        raise DiscordAttachmentError("A mensagem do arquivo não foi enviada pelo bot.")
    if str(raw.get("guild_id") or ref["guild_id"]) != str(ref["guild_id"]):
        raise DiscordAttachmentError("O arquivo pertence a outro servidor.")
    embeds = raw.get("embeds") or []
    embed = embeds[0] if embeds and isinstance(embeds[0], dict) else {}
    if str((embed.get("footer") or {}).get("text") or "") != _MARKER + key:
        raise DiscordAttachmentError("A mensagem não pertence ao arquivo de música esperado.")
    fields = {str(field.get("name") or ""): str(field.get("value") or "") for field in embed.get("fields") or [] if isinstance(field, dict)}
    origin = fields.get("Fonte", "")
    emoji = origin.split(" ", 1)[0][:100]
    if not emoji or len(emoji) > 100:
        raise DiscordAttachmentError("A origem do áudio está ausente no arquivo.")
    try:
        index = int(fields["Índice de áudio"])
        duration = float(fields["Duração"])
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
    return {"url": valid_cdn_url(target.get("url")), "emoji": emoji, "source": origin.split(" ", 1)[-1],
            "filename": name, "audio_stream_index": index, "duration": duration}


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
            same_channel = int(ref.get("guild_id") or 0) == guild_id and int(ref.get("channel_id") or 0) == channel_id
            if previous.get("status") == "done" and same_channel:
                return {"ok": True, **previous}
            self._archive_results.pop(key, None)
        if key != self._archive_active and key not in {item.get("key") for item in list(self._archive_queue._queue)}:
            self._archive_queue.put_nowait({"key": key, "track": track, "guild_id": guild_id,
                                            "channel_id": channel_id, "emoji": str(body.get("source_emoji") or "🎵")[:100]})
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

    async def _archive_existing(self, channel, key: str) -> dict | None:
        # Cobre upload concluído imediatamente antes de um restart do agente.
        async for message in channel.history(limit=200):
            if message.author.id != self.client.user.id or not message.embeds:
                continue
            if (getattr(getattr(message.embeds[0], "footer", None), "text", "") or "") != _MARKER + key:
                continue
            ref = next((attachment for attachment in message.attachments if Path(attachment.filename).suffix.lower() in _AUDIO_EXTS), None)
            if ref:
                return {"status": "done", "reference": {"guild_id": channel.guild.id, "channel_id": channel.id,
                                                             "message_id": message.id, "attachment_id": ref.id},
                        "emoji": str(next((field.value.split(" ", 1)[0] for field in message.embeds[0].fields if field.name == "Fonte"), ""))}
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
                    kind = response.headers.get("Content-Type", "").lower()
                    extension = ".webp" if "webp" in kind else ".png" if "png" in kind else ".jpg"
                    image = await response.content.read(2 * 1024 * 1024 + 1)
                    if not image or len(image) > 2 * 1024 * 1024:
                        return None
                    path = destination / ("capa" + extension)
                    await asyncio.to_thread(path.write_bytes, image)
                    return path
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return None

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

    async def _archive_download(self, item: dict, folder: Path) -> Path | None:
        track = item["track"]
        url = str(track["webpage_url"])
        base = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-warnings",
                "--no-progress", "--no-part", "--max-filesize", "20M", "--limit-rate", "512K", "--socket-timeout", "12",
                "--format-sort", str(getattr(self, "ytdlp_sort", "abr,acodec,asr")),
                "--match-filter", "duration <= 600", "-o", str(folder / "audio.%(ext)s")]
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

    async def _archive_one(self, item: dict) -> dict:
        await self.client.wait_until_ready()
        channel = self.client.get_channel(item["channel_id"])
        if channel is None:
            channel = await self.client.fetch_channel(item["channel_id"])
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != item["guild_id"]:
            raise ValueError("canal do arquivo inválido")
        existing = await self._archive_existing(channel, item["key"])
        if existing:
            return existing
        # Sem usar o scheduler de resolução do player, ffmpeg de playback ou
        # o loop principal para tarefas de disco. A voz mantém prioridade.
        await asyncio.sleep(3)
        await self._archive_wait_stable_voice()
        with tempfile.TemporaryDirectory(prefix="music-archive-") as location:
            folder = Path(location)
            try:
                audio = await self._archive_download(item, folder)
            except ArchiveTooLarge:
                return {"status": "too_large"}
            except ArchiveDurationTooLong:
                return {"status": "ineligible"}
            if audio is None:
                return {"status": "failed"}
            proc = await asyncio.create_subprocess_exec(self.ffprobe_executable, "-v", "error", "-show_entries",
                 "format=duration:stream=index,codec_type", "-of", "json", str(audio), stdout=asyncio.subprocess.PIPE,
                 stderr=asyncio.subprocess.DEVNULL)
            try:
                data, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
            except BaseException:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                raise
            probe = json.loads(data) if proc.returncode == 0 else {}
            duration = float((probe.get("format") or {}).get("duration") or 0)
            streams = probe.get("streams") or []
            if not 0 < duration <= 600 or not any(x.get("codec_type") == "audio" for x in streams) or any(x.get("codec_type") == "video" for x in streams):
                return {"status": "ineligible"} if duration > 600 else {"status": "failed"}
            audio_index = int(next(x["index"] for x in streams if x.get("codec_type") == "audio"))
            cover = await self._archive_cover(str(item["track"].get("display_thumbnail") or item["track"].get("thumbnail") or ""), folder)
            title = str(item["track"].get("display_title") or item["track"].get("title") or "Música")[:240]
            source = str(item["track"].get("display_source") or item["track"].get("source") or "Áudio")[:80]
            origin = str(item["track"].get("original_url") or item["track"].get("webpage_url") or "")[:500]
            embed = discord.Embed(title=title, url=origin if origin.startswith("https://") else None)
            embed.add_field(name="Fonte", value=f"{item['emoji']} {source}", inline=True)
            embed.add_field(name="Duração", value=f"{duration:.2f}", inline=True)
            embed.add_field(name="Índice de áudio", value=str(audio_index), inline=True)
            embed.add_field(name="Formato", value=audio.suffix.lstrip(".").upper(), inline=True)
            embed.set_footer(text=_MARKER + item["key"])
            if cover:
                embed.set_image(url="attachment://" + cover.name)
            await self._archive_wait_stable_voice()
            files = [discord.File(audio, filename=f"musica-{item['key']}{audio.suffix}")]
            if cover:
                files.append(discord.File(cover, filename=cover.name))
            try:
                message = await channel.send(embed=embed, files=files, allowed_mentions=discord.AllowedMentions.none())
            finally:
                for file in files:
                    file.close()
            ref_attachment = next((attachment for attachment in message.attachments if attachment.filename.startswith("musica-")), None)
            if ref_attachment is None:
                raise ValueError("Discord não confirmou o áudio enviado")
            ref = {"guild_id": channel.guild.id, "channel_id": channel.id,
                   "message_id": message.id, "attachment_id": ref_attachment.id}
            raw = await self.client.http.get_message(channel.id, message.id)
            parsed = _archive_metadata(raw, item["key"], self.client.user.id, ref)
            return {"status": "done", "reference": ref, "emoji": parsed["emoji"]}
