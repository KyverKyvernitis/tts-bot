"""Observa reproduções confirmadas e entrega jobs leves ao agente no Android."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .agente_telefone.comandos import music_agent_command
from .busca import arquivo

log = logging.getLogger(__name__)


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
        self.listening: dict[int, _Listening] = {}
        self._wake = asyncio.Event()

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="music-archive-coordinator")

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

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
        if duration <= 0 or duration > 600 or position < 0:
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
                item = await asyncio.to_thread(arquivo.pending)
                if item is None:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=12)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    continue
                guild_id, channel_id = arquivo.channel()
                from .busca.arquivo import media_key
                from .busca.memoria import _track_from_payload
                metadata = item["track"]
                key = item["key"]
                track = _track_from_payload(metadata)
                if media_key(track) != key:
                    arquivo.mark_result(key, {"status": "failed"})
                    continue
                source = (track.display_source or track.source).lower()
                from . import configuracao as config
                emoji_key = next((name for name in config.MUSIC_SOURCE_EMOJIS if name in source), "")
                emoji = config.MUSIC_SOURCE_EMOJIS.get(emoji_key, config.MUSIC_SOURCE_EMOJI_FALLBACK)
                await music_agent_command(
                    "archive_enqueue", guild_id=guild_id, archive_channel_id=channel_id,
                    archive_key=key, archive_ref=item.get("reference") or {},
                    track=track, source_emoji=emoji, timeout_seconds=8.0,
                )
                # A resposta HTTP do enqueue é imediata; o download e o upload
                # continuam em segundo plano sem ocupar a ponte de comandos.
                for _ in range(240):
                    await asyncio.sleep(5)
                    answer = await music_agent_command("archive_status", guild_id=guild_id, archive_key=key,
                                                       timeout_seconds=8.0, command_id="")
                    status = str(answer.get("status") or "")
                    if status in {"done", "too_large", "ineligible", "failed", "missing"}:
                        await asyncio.to_thread(arquivo.mark_result, key, answer)
                        break
                else:
                    await asyncio.to_thread(arquivo.mark_result, key, {"status": "failed"})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("[music/archive] agente indisponível; tentativa posterior", exc_info=True)
                await asyncio.sleep(20)
