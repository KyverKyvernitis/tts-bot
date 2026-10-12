"""Observa reproduções confirmadas e entrega jobs leves ao agente no Android."""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .agente_telefone.comandos import music_agent_command, music_agent_status
from .busca import arquivo

log = logging.getLogger(__name__)
_MIN_ARCHIVE_AGENT_VERSION = (0, 3, 82)
_MIN_SOURCE_AGENT_VERSION = (0, 3, 82)


def _archive_agent_ready(payload: dict) -> bool:
    if not payload.get("available"):
        return False
    version = str(payload.get("version") or "")
    if not re.fullmatch(r"\d+(?:\.\d+){2,3}", version):
        return False
    return tuple(int(part) for part in version.split(".")[:3]) >= _MIN_ARCHIVE_AGENT_VERSION


def _source_agent_ready(payload: dict) -> bool:
    return _archive_agent_ready(payload) and tuple(int(part) for part in payload["version"].split(".")[:3]) >= _MIN_SOURCE_AGENT_VERSION


@dataclass
class _Listening:
    queue_id: str
    token: int
    last_position: float
    counted: bool = False


class ArchiveCoordinator:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.task: asyncio.Task | None = None
        self.seed_task: asyncio.Task | None = None
        self.listening: dict[int, _Listening] = {}
        self._wake = asyncio.Event()
        self._new_archive_streak = 0
        self._cleanup_streak = 0
        self._revision = ""
        self._revision_checked_at = 0.0

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="music-archive-coordinator")
        if self.seed_task is None or self.seed_task.done():
            self.seed_task = asyncio.create_task(self._seed_learned(), name="music-archive-learned-seed")

    async def close(self) -> None:
        for task in (self.task, self.seed_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.task = self.seed_task = None

    async def _seed_learned(self) -> None:
        await self.bot.wait_until_ready()
        # Migração e cache de schema fora do primeiro comando de reprodução.
        from .busca.memoria import _ensure_loaded
        await asyncio.to_thread(_ensure_loaded)
        while True:
            try:
                # O outbox persiste escolhas até a confirmação do catálogo.
                # Drenar páginas limita CPU/RAM sem revarrer aliases a cada boot.
                added = await asyncio.to_thread(arquivo.flush_learned)
                if added:
                    self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("[music/archive] importação da memória adiada", exc_info=True)
                added = 0
            await asyncio.sleep(0.05 if added else 12)

    def observe(self, guild_id: int, track, remote: dict, *, confirmed: bool) -> None:
        """Conta o início confirmado uma vez por item real da fila."""
        if not confirmed or track is None or not arquivo.media_key(track):
            return
        try:
            duration = float(track.duration)
            position = float(remote["position_ms"]) / 1000
            token = int(remote["playback_token"])
        except (KeyError, TypeError, ValueError):
            return
        if not math.isfinite(duration) or not math.isfinite(position) or duration <= 0 or position < 0:
            return
        queue_id = str(getattr(track, "queue_item_id", "") or "")
        if not queue_id:
            return
        current = self.listening.get(guild_id)
        new_start = bool(current is None or current.queue_id != queue_id)
        if current and current.queue_id == queue_id and token != current.token:
            event = str(remote.get("last_event") or "").lower()
            if position < 5 and current.last_position > 5 and event == "direct_track_start_confirmed":
                new_start = True  # outra volta real do loop da mesma faixa
        if new_start:
            current = _Listening(queue_id, token, position)
            self.listening[guild_id] = current
            if len(self.listening) > 1000:
                self.listening.clear()
        assert current is not None
        current.last_position = position
        if current.counted:
            return
        from .busca.memoria import faixa_aprendida
        if not faixa_aprendida(track):
            current.counted = True
            return
        marker = f"{guild_id}:{queue_id}:{current.token}"
        try:
            if arquivo.record_play(track, marker):
                log.info("[music/archive] reprodução válida | guild=%s media=%s", guild_id, arquivo.media_key(track))
                self._wake.set()
            current.counted = True
        except Exception:
            log.exception("[music/archive] falha ao contar reprodução")

    async def _run(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                if await asyncio.to_thread(arquivo.channel_type) != "forum" or arquivo.channel() == (0, 0):
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=12)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    continue
                await asyncio.to_thread(arquivo.flush_learned)
                item = await asyncio.to_thread(arquivo.pending, prefer_new=self._new_archive_streak < 3)
                cleanup = await asyncio.to_thread(arquivo.cleanup_pending)
                if cleanup is not None and (item is None or self._cleanup_streak == 0):
                    try:
                        answer = await music_agent_command(
                            "archive_cleanup", guild_id=cleanup["reference"]["guild_id"],
                            archive_key=cleanup["key"], archive_ref=cleanup["reference"],
                            previous_ref=cleanup["previous"], track=cleanup["track"], timeout_seconds=8.0,
                        )
                        await asyncio.to_thread(arquivo.mark_cleanup, cleanup["key"], cleanup["previous"],
                                                done=bool(answer.get("removed")))
                    except Exception:
                        log.warning("[music/archive] limpeza adiada", exc_info=True)
                        await asyncio.to_thread(arquivo.mark_cleanup, cleanup["key"], cleanup["previous"], done=False)
                    self._cleanup_streak = 1
                    continue
                self._cleanup_streak = 0
                if item is None:
                    if time.monotonic() - self._revision_checked_at >= 300:
                        self._revision_checked_at = time.monotonic()
                        guild_id, _channel_id = arquivo.channel()
                        agent = await music_agent_status(guild_id=guild_id, timeout_seconds=3.0)
                        if _archive_agent_ready(agent):
                            revision = str(agent.get("archive_resolver_revision") or agent.get("version") or "")
                            if revision != self._revision:
                                self._revision = revision
                                if await asyncio.to_thread(arquivo.reopen_on_revision, revision):
                                    continue
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=12)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    continue
                guild_id, channel_id = arquivo.channel()
                # A release do bot pode entrar em produção antes do Android.
                # Evite publicar v5 durante a troca e adiar a correção por 1 h.
                agent = await music_agent_status(guild_id=guild_id, timeout_seconds=3.0)
                if not _archive_agent_ready(agent):
                    log.info("[music/archive] aguardando agente com auditoria v8 | versão=%s", agent.get("version"))
                    await asyncio.sleep(12)
                    continue
                revision = str(agent.get("archive_resolver_revision") or agent.get("version") or "")
                if revision != self._revision:
                    self._revision = revision
                    await asyncio.to_thread(arquivo.reopen_on_revision, revision)
                known_url = str(item.get("source_known") or "")
                bandcamp_url = str(item.get("source_override") or "")
                if not bandcamp_url and (urlsplit(known_url).hostname or "").lower().endswith(".bandcamp.com"):
                    bandcamp_url = known_url
                if bandcamp_url and not _source_agent_ready(agent):
                    log.info("[music/archive] aguardando agente com fonte externa | versão=%s", agent.get("version"))
                    await asyncio.sleep(12)
                    continue
                from .busca.arquivo import media_key
                from .busca.memoria import _track_from_payload
                metadata = item["track"]
                key = item["key"]
                track = _track_from_payload(metadata)
                if media_key(track) != key:
                    await asyncio.to_thread(arquivo.mark_result, key, {"status": "failed", "reason": "invalid_key",
                                                                         "agent_revision": revision})
                    continue
                if known_url and not bandcamp_url:
                    track.webpage_url = known_url
                    host = (urlsplit(known_url).hostname or "").lower()
                    if "youtube.com" in host:
                        track.source = track.display_source = "YouTube"
                    elif host == "soundcloud.com":
                        track.source = track.display_source = "SoundCloud"
                source = (track.display_source or track.source).lower()
                from . import configuracao as config
                emoji_key = next((name for name in config.MUSIC_SOURCE_EMOJIS if name in source), "")
                emoji = config.MUSIC_SOURCE_EMOJIS.get(emoji_key, config.MUSIC_SOURCE_EMOJI_FALLBACK)
                await asyncio.to_thread(arquivo.mark_attempt, key, source=bandcamp_url or known_url or track.webpage_url,
                                        revision=revision)
                archive_source = None
                if bandcamp_url:
                    from .arquivo_fonte import SourceMismatchError, resolve_bandcamp_source
                    try:
                        archive_source = await asyncio.wait_for(
                            asyncio.to_thread(resolve_bandcamp_source, bandcamp_url, metadata), timeout=45,
                        )
                    except SourceMismatchError:
                        log.warning("[music/archive] fonte oficial divergente | chave=%s", key)
                        await asyncio.to_thread(arquivo.mark_result, key, {"status": "unavailable",
                                                                           "reason": "source_mismatch", "agent_revision": revision})
                        continue
                    except Exception as exc:
                        log.warning("[music/archive] fonte oficial indisponível | chave=%s tipo=%s", key, type(exc).__name__)
                        await asyncio.to_thread(arquivo.mark_result, key, {"status": "failed",
                                                                           "reason": "source_resolve_error", "agent_revision": revision})
                        continue
                    emoji = config.MUSIC_SOURCE_EMOJIS.get("bandcamp", config.MUSIC_SOURCE_EMOJI_FALLBACK)
                enqueue = await music_agent_command(
                    "archive_enqueue", guild_id=guild_id, archive_channel_id=channel_id,
                    archive_key=key, archive_ref=item.get("reference") or {}, archive_retry=item.get("retry", False),
                    track=track, source_emoji=emoji, source_emojis=config.MUSIC_SOURCE_EMOJIS,
                    archive_source=archive_source,
                    timeout_seconds=8.0,
                )
                if not enqueue.get("ok", True):
                    await asyncio.to_thread(arquivo.mark_result, key, {"status": "failed",
                                                                       "reason": "agent_rejected", "agent_revision": revision})
                    continue
                # A resposta HTTP do enqueue é imediata; o download e o upload
                # continuam em segundo plano sem ocupar a ponte de comandos.
                # O agente aplica timeouts por operação e duração. Enquanto
                # responde working/queued, uma faixa longa não expira por um
                # limite fixo do coordenador. A ponte continua com timeout HTTP.
                while True:
                    await asyncio.sleep(5)
                    answer = await music_agent_command("archive_status", guild_id=guild_id, archive_key=key,
                                                       timeout_seconds=8.0, command_id="")
                    status = str(answer.get("status") or "")
                    if not answer.get("ok", True) or status not in {"queued", "working", "done", "too_large", "ineligible", "unavailable", "failed", "missing"}:
                        await asyncio.to_thread(arquivo.mark_result, key, {"status": "failed", "reason": "agent_rejected", "agent_revision": revision})
                        break
                    if status in {"done", "too_large", "ineligible", "unavailable", "failed", "missing"}:
                        discovery_rejected = False
                        if status == "unavailable" and answer.get("reason") == "no_match" and not bandcamp_url:
                            from .arquivo_fonte import SourceMismatchError, resolve_bandcamp_source
                            candidate = await asyncio.to_thread(arquivo.bandcamp_candidate, metadata)
                            if candidate:
                                try:
                                    await asyncio.wait_for(
                                        asyncio.to_thread(resolve_bandcamp_source, candidate, metadata), timeout=45,
                                    )
                                except SourceMismatchError:
                                    discovery_rejected = True
                                except Exception as exc:
                                    log.warning("[music/archive] descoberta de fonte adiada | chave=%s tipo=%s",
                                                key, type(exc).__name__)
                                    answer = {"status": "failed", "reason": "source_resolve_error"}
                                else:
                                    await asyncio.to_thread(arquivo.mark_result, key, {**answer, "agent_revision": revision})
                                    if await asyncio.to_thread(arquivo.queue_discovered_source, key, candidate):
                                        self._new_archive_streak += 1
                                    break
                        await asyncio.to_thread(arquivo.mark_result, key, {**answer, "agent_revision": revision})
                        if discovery_rejected:
                            await asyncio.to_thread(arquivo.note_discovery_failure, key, "source_mismatch")
                        self._new_archive_streak = 0 if item["reference"] else self._new_archive_streak + 1
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("[music/archive] agente indisponível; tentativa posterior", exc_info=True)
                await asyncio.sleep(20)
