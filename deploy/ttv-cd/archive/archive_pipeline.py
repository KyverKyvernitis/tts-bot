"""Temporary-file preparation and resumable same-thread Discord archive uploads."""
from __future__ import annotations

import asyncio
import contextlib
import io
import math
import json
import os
import signal
import shutil
import threading
import time
from pathlib import Path

import aiohttp
import discord

from .archive_staging import ArchiveWorkspace
from .archive_manifest import (
    MANIFEST_FILENAME, MANIFEST_MARKER, SEGMENT_MARKER, add_inline_manifest, encode_manifest,
    file_sha256, flatten_segments, hydrate_archive_manifest, validate_manifest,
)


class _ArchiveThreadCancelled(RuntimeError):
    pass


class _ArchiveUploadReader(io.BufferedReader):
    """Seekable upload body with bounded, off-loop backpressure.

    aiohttp reads file payloads in its executor. Synchronous callers (including
    Discord test adapters) retain normal file semantics and never sleep on the
    event loop. Uploads remain alive during voice startup, so a lost HTTP ACK
    cannot create duplicate forum threads just to give playback priority.
    """

    def __init__(self, path: Path, priority, playback):
        super().__init__(io.FileIO(path, "rb"))
        self._priority, self._playback = priority, playback
        self._next_read = 0.0
        self._priority_pause = 0.0
        self._closed_for_archive = threading.Event()

    def read(self, size=-1):
        if size == 0:
            return b""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            off_loop = True
        else:
            off_loop = False
        if not off_loop or size is None or size < 0:
            return super().read(size)
        # Bound a single pause, and permit a small slice even if voice recovery
        # lasts much longer than usual. This also avoids starving the uploader.
        began = time.monotonic()
        until = began + min(2.0, max(0.0, 15.0 - self._priority_pause))
        while self._priority() and time.monotonic() < until:
            if self._closed_for_archive.wait(0.05):
                raise ValueError("arquivo de upload fechado")
        self._priority_pause += time.monotonic() - began
        foreground = self._priority()
        if foreground or self._playback():
            size = min(size, 16 * 1024 if foreground and self._priority_pause < 15.0 else 64 * 1024)
            if self._closed_for_archive.wait(max(0.0, self._next_read - time.monotonic())):
                raise ValueError("arquivo de upload fechado")
            self._next_read = time.monotonic() + size / (512 * 1024)
        return super().read(size)

    def close(self):
        self._closed_for_archive.set()
        super().close()


def upload_audio_budget(guild, *, cover_bytes: int = 0) -> int:
    """Use the actual server limit, retaining room for cover and multipart headers."""
    limit = int(getattr(guild, "filesize_limit", 10 * 1024 * 1024))
    margin = max(64 * 1024, math.ceil(limit * 0.02))
    budget = limit - margin - max(0, int(cover_bytes))
    if budget < 128 * 1024:
        raise ValueError("limite de upload insuficiente para áudio")
    return budget


class ArchivePipelineMixin:
    def _archive_foreground_busy(self) -> bool:
        now = time.monotonic()
        reservations = getattr(self, "_archive_foreground_until", {})
        if isinstance(reservations, dict) and any(float(until) > now for until in list(reservations.values())):
            return True
        if any(getattr(st, "status", "") in {"starting", "preparing", "reconnecting", "tts_direct"}
               or bool(getattr(st, "voice_runtime_recovery_pending", False))
               for st in list(getattr(self, "states", {}).values())):
            return True
        return any(task and not task.done() for task in list(getattr(self, "_active_resolve_tasks", {}).values()))

    def _archive_playback_active(self) -> bool:
        return any(getattr(st, "status", "") in {"playing", "paused", "tts_direct"}
                   for st in list(getattr(self, "states", {}).values()))

    async def _archive_spawn(self, *arguments, **kwargs):
        await self._archive_wait_stable_voice()
        grouped = os.name == "posix"
        if grouped:
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(*arguments, **kwargs)
        proc._archive_process_group = grouped
        if grouped and getattr(proc, "pid", None):
            # Lower only archive children; the agent, decoder and voice sender
            # retain their original priority. Non-root Linux/Termux permits it.
            with contextlib.suppress(OSError, AttributeError):
                priority = os.getpriority(os.PRIO_PROCESS, proc.pid)
                os.setpriority(os.PRIO_PROCESS, proc.pid, min(19, priority + 10))
        return proc

    @staticmethod
    def _archive_signal_process(proc, sig: int) -> bool:
        if proc.returncode is not None:
            return False
        pid = getattr(proc, "pid", None)
        if getattr(proc, "_archive_process_group", False) and pid:
            with contextlib.suppress(ProcessLookupError):
                if os.getpgid(pid) == pid:
                    os.killpg(pid, sig)
                    return True
        return False

    async def _archive_background_call(self, function, *args, **kwargs):
        """Pause expensive Python file work at explicit, cancellable checkpoints."""
        await self._archive_wait_stable_voice()
        cancelled = threading.Event()
        next_check = 0.0
        def checkpoint():
            nonlocal next_check
            now = time.monotonic()
            if cancelled.is_set():
                raise _ArchiveThreadCancelled()
            if now < next_check:
                return
            until = now + 2.0
            while self._archive_foreground_busy() and time.monotonic() < until:
                if cancelled.wait(0.05):
                    raise _ArchiveThreadCancelled()
            # A brief work slice prevents starvation during repeated TTS or
            # slow recovery, while limiting competing reads to a small burst.
            next_check = time.monotonic() + 0.02
        task = asyncio.create_task(asyncio.to_thread(function, *args, checkpoint=checkpoint, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            raise

    def _archive_upload_file(self, path: Path, *, filename: str):
        class ArchiveUploadFile(discord.File):
            def close(self):
                # Discord owns path-opened files but not supplied streams. This
                # stream exists only for this request and always needs closing.
                super().close()
                self.fp.close()
        stream = _ArchiveUploadReader(path, self._archive_foreground_busy, self._archive_playback_active)
        try:
            return ArchiveUploadFile(stream, filename=filename)
        except BaseException:
            stream.close()
            raise

    async def _archive_transfer_checkpoint(self, transfer: dict, *, chunk_bytes: int):
        """Apply bounded network backpressure between archive HTTP chunks."""
        began = time.monotonic()
        until = began + min(2.0, max(0.0, 15.0 - transfer.get("paused", 0.0)))
        while self._archive_foreground_busy() and time.monotonic() < until:
            await asyncio.sleep(0.05)
        transfer["paused"] = transfer.get("paused", 0.0) + time.monotonic() - began
        if self._archive_foreground_busy() or self._archive_playback_active():
            await asyncio.sleep(max(0.0, transfer.get("next_chunk", 0.0) - time.monotonic()))
            transfer["next_chunk"] = time.monotonic() + chunk_bytes / (512 * 1024)

    async def _archive_download_attachment(self, attachment, destination: Path):
        """Migrate forum audio without buffering the whole attachment in RAM."""
        await self._archive_wait_stable_voice()
        if not isinstance(attachment, getattr(discord, "Attachment", ())):
            # Compatible message adapters can provide their own attachment
            # implementation (including in-memory test transports).
            await attachment.save(str(destination))
            return
        from .validade_stream import valid_cdn_url
        url = valid_cdn_url(attachment.url)
        expected = int(attachment.size)
        partial = destination.with_name(destination.name + ".part")
        transfer, written = {}, 0
        timeout = aiohttp.ClientTimeout(total=max(120.0, expected / (64 * 1024) + 30.0), sock_connect=12, sock_read=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise OSError("não consegui ler o anexo arquivado")
                with partial.open("wb") as output:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        await self._archive_transfer_checkpoint(transfer, chunk_bytes=len(chunk))
                        written += len(chunk)
                        if written > expected:
                            raise ValueError("o anexo arquivado mudou de tamanho")
                        await asyncio.to_thread(output.write, chunk)
                if written != expected or not written:
                    raise ValueError("download incompleto do anexo arquivado")
        partial.replace(destination)

    def _archive_workspace_identity(self, item: dict) -> dict:
        return {"bot_id": self.client.user.id, "guild_id": item["guild_id"], "forum_id": item["channel_id"],
                "origin": str(item["track"].get("original_url") or item["track"].get("webpage_url") or ""),
                "source": str((item.get("archive_source") or {}).get("webpage_url") or "")}

    async def _archive_run_ffmpeg(self, arguments: list[str], *, duration: float) -> bool:
        proc = await self._archive_spawn(
            self.ffmpeg_executable, "-nostdin", "-v", "error", "-threads", "1", "-y", *arguments,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            source_index = arguments.index("-i") + 1
            folder = Path(arguments[source_index]).parent
            initial_bytes = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file() and not path.is_symlink())
            free = shutil.disk_usage(folder).free
            budget = initial_bytes + max(0, free - max(64 * 1024 * 1024, int(free * 0.05)))
            await self._archive_download_communicate(proc, folder, budget=budget, timeout=max(120.0, duration * 2.0))
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            raise
        return proc.returncode == 0

    @staticmethod
    def _archive_load_prepared(folder: Path, fingerprint: dict, *, checkpoint=None) -> list[dict] | None:
        """Recover the exact prepared bytes, including random container serials."""
        marker = folder / ".prepared-segments.json"
        try:
            if marker.stat().st_size > 8 * 1024 * 1024:
                return None
            saved = json.loads(marker.read_text())
            if saved.get("fingerprint") != fingerprint:
                return None
            entries = saved.get("segments")
            if not isinstance(entries, list) or not entries:
                return None
            root, result, offset, seen = folder.resolve(), [], 0.0, set()
            for order, entry in enumerate(entries):
                if checkpoint:
                    checkpoint()
                path = (folder / str(entry["path"])).resolve()
                if not path.is_relative_to(root) or path in seen or not path.is_file():
                    return None
                seen.add(path)
                if (entry["order"] != order or int(entry["size_bytes"]) != path.stat().st_size
                        or not 0 < path.stat().st_size <= fingerprint["budget"]
                        or file_sha256(path, checkpoint=checkpoint) != entry["sha256"]):
                    return None
                seconds, start = float(entry["duration"]), float(entry["offset_seconds"])
                if not math.isfinite(seconds) or seconds <= 0 or not math.isfinite(start) or abs(start - offset) > 0.05:
                    return None
                if (not entry.get("audio_codec") or int(entry["audio_stream_index"]) < 0
                        or int(entry["audio_sample_rate"]) <= 0 or int(entry["audio_channels"]) <= 0
                        or int(entry["audio_abr"]) < 0):
                    return None
                result.append({**entry, "path": path})
                offset += seconds
            if abs(offset - fingerprint["duration"]) > max(0.25, fingerprint["duration"] * 0.002):
                return None
            return result
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    @staticmethod
    def _archive_save_prepared(folder: Path, fingerprint: dict, segments: list[dict]) -> None:
        temporary = folder / ".prepared-segments.tmp"
        entries = [{**entry, "path": str(entry["path"].relative_to(folder))} for entry in segments]
        temporary.write_text(json.dumps({"fingerprint": fingerprint, "segments": entries},
                                        ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        temporary.replace(folder / ".prepared-segments.json")

    async def _archive_prepare_segments(self, audio: Path, folder: Path, *, duration: float,
                                        audio_index: int, codec: str, budget: int) -> list[dict]:
        """Keep compressed packets where possible, encode once from the original otherwise.

        A smaller split is retried using stream copy when variable bitrate exceeds
        the upload budget. No lossy output ever becomes an encoding input.
        """
        fingerprint = {"version": 1, "source_sha256": await self._archive_background_call(file_sha256, audio),
                       "budget": int(budget), "duration": float(duration), "audio_stream_index": int(audio_index),
                       "audio_codec": str(codec), "preparation_revision": "opus-preroll-80ms-v1"}
        prepared = await self._archive_background_call(self._archive_load_prepared, folder, fingerprint)
        if prepared is not None:
            return prepared
        (folder / ".prepared-segments.json").unlink(missing_ok=True)
        containers = {"opus": (".ogg", "ogg"), "vorbis": (".ogg", "ogg"),
                      "mp3": (".mp3", "mp3"), "aac": (".m4a", "mp4"),
                      "flac": (".flac", "flac"), "pcm_s16le": (".wav", "wav")}
        if audio.stat().st_size <= budget:
            paths = [audio]
        else:
            output = folder / "segments"
            output.mkdir(exist_ok=True)
            ext, container = containers.get(codec, (".ogg", "ogg"))
            copy = codec in containers
            seconds = max(0.25, duration * budget / audio.stat().st_size * 0.80)
            paths = []
            encoded_once = False
            for attempt in range(14):
                for stale in output.iterdir():
                    stale.unlink(missing_ok=True)
                if not copy:
                    # One encoding pass, always from the original. Subsequent
                    # size adjustments only copy these compressed packets.
                    if encoded_once:
                        raise ValueError("falha ao segmentar áudio já preparado")
                    encoded = folder / "encoded-original.ogg"
                    ok = await self._archive_run_ffmpeg(
                        ["-i", str(audio), "-map", f"0:{audio_index}", "-vn", "-c:a", "libopus",
                         "-b:a", "192k", "-vbr", "on", "-ar", "48000", str(encoded)], duration=duration)
                    if not ok:
                        raise ValueError("não consegui preparar o áudio para segmentação")
                    audio, audio_index, codec = encoded, 0, "opus"
                    encoded_once, copy, ext, container = True, True, ".ogg", "ogg"
                    seconds = max(0.25, duration * budget / audio.stat().st_size * 0.75)
                args = ["-i", str(audio), "-map", f"0:{audio_index}", "-vn", "-c:a", "copy",
                        "-f", "segment", "-segment_format", container, "-segment_time", str(seconds),
                        "-reset_timestamps", "1", str(output / ("part-%06d" + ext))]
                if not await self._archive_run_ffmpeg(args, duration=duration):
                    if not encoded_once:
                        copy = False
                        continue
                    raise ValueError("não consegui preparar os segmentos de áudio")
                paths = sorted(output.glob("part-*"))
                if not paths or any(path.stat().st_size <= 0 for path in paths):
                    raise ValueError("segmentos de áudio vazios")
                largest = max(path.stat().st_size for path in paths)
                if largest <= budget:
                    break
                next_seconds = seconds * budget / largest * 0.75
                if next_seconds < 0.02:
                    raise ValueError("um pacote de áudio excede o limite de upload")
                seconds = next_seconds
            else:
                raise ValueError("não consegui adequar os segmentos ao upload do servidor")
        decoded_durations = None
        if len(paths) > 1 and codec == "opus":
            from .archive_ogg import add_opus_preroll
            decoded_durations = await self._archive_background_call(add_opus_preroll, audio, paths)
        segments, offset = [], 0.0
        for order, path in enumerate(paths):
            await self._archive_wait_stable_voice()
            probe = await self._archive_probe(path)
            primary = next((item for item in probe.get("streams") or [] if item.get("codec_type") == "audio"), None)
            try:
                seconds = decoded_durations[order] if decoded_durations is not None else float((probe.get("format") or {}).get("duration") or 0)
                if primary is None or not math.isfinite(seconds) or seconds <= 0 or path.stat().st_size > budget:
                    raise ValueError
                entry = {"order": order, "offset_seconds": offset, "duration": seconds,
                         "size_bytes": path.stat().st_size, "sha256": await self._archive_background_call(file_sha256, path),
                         "audio_codec": str(primary["codec_name"]).lower(), "audio_stream_index": int(primary["index"]),
                         "audio_sample_rate": int(primary.get("sample_rate") or 0),
                         "audio_channels": int(primary.get("channels") or 0),
                         "audio_abr": int(primary.get("bit_rate") or 0) // 1000, "path": path}
                if entry["audio_sample_rate"] <= 0 or entry["audio_channels"] <= 0:
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise ValueError("segmento de áudio inválido") from None
            if entry["audio_abr"] <= 0:
                entry["audio_abr"] = max(1, round(entry["size_bytes"] * 8 / seconds / 1000))
            segments.append(entry)
            offset += seconds
        if abs(offset - duration) > max(0.25, duration * 0.002):
            raise ValueError("a segmentação alterou a duração do áudio")
        await asyncio.to_thread(self._archive_save_prepared, folder, fingerprint, segments)
        return segments

    async def _archive_confirm_attachment(self, channel_id: int, message_id: int, *, filename: str,
                                           size_bytes: int, bot_id: int) -> dict:
        from .validade_stream import valid_cdn_url
        for attempt in range(3):
            raw = await self.client.http.get_message(channel_id, message_id)
            if str((raw.get("author") or {}).get("id")) != str(bot_id):
                raise ValueError("anexo publicado por outro autor")
            attachment = next((entry for entry in raw.get("attachments") or []
                               if entry.get("filename") == filename and int(entry.get("size") or 0) == size_bytes), None)
            if attachment is not None:
                valid_cdn_url(attachment.get("url"))
                return attachment
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
        raise ValueError("Discord não confirmou o anexo enviado")

    async def _archive_upload_v8(self, forum, item: dict, *, existing=None, previous=None) -> dict:
        from .validade_stream import (
            _AUDIO_EXTS, _PUBLIC_FOOTER, _archive_audio_filename, _archive_duration_text,
            _archive_metadata, _archive_public_origin, _archive_url, _positive_duration,
            ArchiveMetadataMismatch,
        )
        await self._archive_wait_stable_voice()
        # Original, optional remux, segments and manifest exist only here.
        with ArchiveWorkspace(item["key"], self._archive_workspace_identity(item)) as staging:
            folder = staging.path
            saved = staging.load_reference()
            if existing is None and int(saved.get("forum_id") or 0) == forum.id:
                try:
                    candidate_thread = self.client.get_channel(int(saved["channel_id"])) or await self.client.fetch_channel(int(saved["channel_id"]))
                    candidate = await candidate_thread.fetch_message(int(saved["message_id"]))
                    if self._archive_message_version(candidate, {**item, "forum": True}) >= 3:
                        existing = candidate
                except (discord.HTTPException, KeyError, ValueError, TypeError):
                    pass
            old_audio = next((ref for ref in previous.attachments if Path(ref.filename).suffix.lower() in _AUDIO_EXTS), None) if previous is not None else None
            if old_audio is not None:
                audio = folder / ("anterior" + Path(old_audio.filename).suffix.lower())
                free = shutil.disk_usage(folder).free
                if int(old_audio.size or 0) >= free * 0.45:
                    raise OSError("espaço temporário insuficiente")
                if not audio.is_file() or audio.stat().st_size != int(old_audio.size or 0):
                    await self._archive_download_attachment(old_audio, audio)
            else:
                audio = await self._archive_download(item, folder)
            if audio is None:
                return {"status": "failed", "reason": "download_failed"}
            ready = await self._archive_audio_ready(audio, folder)
            if ready is None:
                (folder / ".download-name").unlink(missing_ok=True)
                audio.unlink(missing_ok=True)
                return {"status": "failed", "reason": "media_invalid"}
            audio, duration, index, codec = ready
            expected = _positive_duration(item["track"].get("duration"))
            tolerance = 0.03 if item.get("archive_source") else 0.10
            if expected and abs(duration - expected) > max(10, expected * tolerance):
                raise ArchiveMetadataMismatch("duração do áudio não corresponde à faixa aprendida")
            cover = await self._archive_cover_for_track(item["track"], folder)
            if cover is None:
                cover = await self._archive_existing_cover(previous, folder)
            cover_bytes = cover.stat().st_size if cover else 0
            # Covers are optional; never make an otherwise valid audio impossible.
            try:
                budget = upload_audio_budget(forum.guild, cover_bytes=cover_bytes)
            except ValueError:
                cover, budget = None, upload_audio_budget(forum.guild)
            segments = await self._archive_prepare_segments(audio, folder, duration=duration, audio_index=index, codec=codec, budget=budget)
            safe_title = _archive_audio_filename(str(item["track"].get("display_title") or item["track"].get("title") or "Música"), "")[:60]
            for part in segments:
                await self._archive_wait_stable_voice()
                part["filename"] = _archive_audio_filename(
                    safe_title
                    + (f" · {part['order'] + 1:06d}" if len(segments) > 1 else "")
                    + "-" + part["sha256"][:12], part["path"].suffix)
            emoji, source = item["emoji"], str(item["track"].get("display_source") or item["track"].get("source") or "Áudio")[:80]
            total = sum(part["duration"] for part in segments)
            first = segments[0]
            title = str(item["track"].get("display_title") or item["track"].get("title") or "Música")[:240]
            embed = discord.Embed(title=title, url=_archive_url(_archive_public_origin(item["track"])[:500], item["key"], version=8))
            embed.add_field(name="Fonte", value=f"{emoji} {source}", inline=True)
            embed.add_field(name="Duração", value=_archive_duration_text(total), inline=True)
            embed.add_field(name="Formato", value=f"{first['audio_codec'].upper()} · {first['audio_sample_rate'] / 1000:g} kHz · {first['audio_channels']} canais", inline=True)
            embed.add_field(name="Qualidade", value=f"≈{first['audio_abr']} kbps", inline=True)
            embed.set_footer(text=_PUBLIC_FOOTER)
            if existing is None:
                await self._archive_wait_stable_voice()
                files = ([self._archive_upload_file(cover, filename=cover.name)] if cover else []) + [self._archive_upload_file(first["path"], filename=first["filename"])]
                try:
                    tags = [forum.available_tags[0]] if forum.flags.require_tag else []
                    post = await forum.create_thread(name=self._archive_post_name(item["track"]), content=None, embed=embed,
                                                     files=files, applied_tags=tags, allowed_mentions=discord.AllowedMentions.none())
                    message, thread = post.message, post.thread
                finally:
                    for file in files:
                        file.close()
                # Keep a recoverable ref immediately, even if later confirmation fails.
                item["pending_ref"] = {"guild_id": forum.guild.id, "forum_id": forum.id, "channel_id": thread.id,
                                       "message_id": message.id, "attachment_id": 0}
                staging.save_reference(item["pending_ref"])
            else:
                message, thread = existing, existing.channel
                item["pending_ref"] = {"guild_id": forum.guild.id, "forum_id": forum.id, "channel_id": thread.id,
                                       "message_id": message.id, "attachment_id": 0}
                staging.save_reference(item["pending_ref"])
                if getattr(thread, "archived", False):
                    await thread.edit(archived=False)
                root_raw = await self.client.http.get_message(thread.id, message.id)
                current = next((a for a in root_raw.get("attachments") or [] if a.get("filename") == first["filename"] and int(a.get("size") or 0) == first["size_bytes"]), None)
                if current is None:
                    await self._archive_wait_stable_voice()
                    files = ([self._archive_upload_file(cover, filename=cover.name)] if cover else []) + [self._archive_upload_file(first["path"], filename=first["filename"])]
                    try:
                        message = await message.edit(embed=embed, attachments=files, allowed_mentions=discord.AllowedMentions.none())
                    finally:
                        for file in files:
                            file.close()
            confirmed = await self._archive_confirm_attachment(thread.id, message.id, filename=first["filename"], size_bytes=first["size_bytes"], bot_id=self.client.user.id)
            root_ref = {**item["pending_ref"], "attachment_id": int(confirmed["id"])}
            item["pending_ref"] = root_ref
            staging.save_reference(root_ref)
            first["reference"] = dict(root_ref)
            # Child filenames incorporate content hashes. A lost upload ACK can
            # therefore be recovered without sending the same segment again.
            reusable = {}
            if existing is not None:
                async for candidate in thread.history(limit=None, oldest_first=True):
                    if candidate.id == message.id or candidate.author.id != self.client.user.id:
                        continue
                    for attachment in candidate.attachments:
                        reusable[(attachment.filename, int(attachment.size or 0))] = candidate
            for part in segments[1:]:
                await self._archive_wait_stable_voice()
                child = reusable.get((part["filename"], part["size_bytes"]))
                if child is None:
                    file = self._archive_upload_file(part["path"], filename=part["filename"])
                    try:
                        child_embed = discord.Embed(title=f"Parte {part['order'] + 1}/{len(segments)}",
                            url=_archive_url(_archive_public_origin(item["track"]), item["key"], version=8) + "&" + SEGMENT_MARKER + item["key"] + ":" + str(part["order"]))
                        child_embed.set_footer(text=_PUBLIC_FOOTER)
                        child = await thread.send(content=f"Parte {part['order'] + 1}/{len(segments)} · {_archive_duration_text(part['duration'])}",
                                                  embed=child_embed, file=file, allowed_mentions=discord.AllowedMentions.none())
                    finally:
                        file.close()
                attachment = await self._archive_confirm_attachment(thread.id, child.id, filename=part["filename"], size_bytes=part["size_bytes"], bot_id=self.client.user.id)
                part["reference"] = {**root_ref, "message_id": child.id, "attachment_id": int(attachment["id"])}
            manifest = {"version": 1, "archive_key": item["key"], "duration": total,
                        "sha256": await self._archive_background_call(file_sha256, audio),
                        **{key: first[key] for key in ("audio_codec", "audio_sample_rate", "audio_channels", "audio_stream_index", "audio_abr")},
                        "segments": [{key: value for key, value in part.items() if key != "path"} for part in segments]}
            manifest = validate_manifest(manifest, archive_key=item["key"], reference=root_ref)
            data = encode_manifest(manifest)
            manifest_path = folder / MANIFEST_FILENAME
            await asyncio.to_thread(manifest_path.write_bytes, data)
            add_inline_manifest(embed, manifest)
            raw = await self.client.http.get_message(thread.id, message.id)
            # Refresh the message's attachment objects before retaining them.
            message = await thread.fetch_message(message.id)
            keep = [a for a in message.attachments if a.filename != MANIFEST_FILENAME]
            await self._archive_wait_stable_voice()
            file = self._archive_upload_file(manifest_path, filename=MANIFEST_FILENAME)
            try:
                message = await message.edit(embed=embed, attachments=[*keep, file], allowed_mentions=discord.AllowedMentions.none())
            finally:
                file.close()
            manifest_attachment = await self._archive_confirm_attachment(thread.id, message.id, filename=MANIFEST_FILENAME, size_bytes=len(data), bot_id=self.client.user.id)
            raw = await self.client.http.get_message(thread.id, message.id)
            await hydrate_archive_manifest(raw)
            parsed = _archive_metadata(raw, item["key"], self.client.user.id, root_ref)
            root_ref.update(manifest_attachment_id=int(manifest_attachment["id"]), segments=flatten_segments(manifest))
            staging.complete()
            return {"status": "done", "reference": root_ref, "emoji": parsed["emoji"], "presentation": 8,
                    "source_url": str(item["track"].get("webpage_url") or "")[:500]}
