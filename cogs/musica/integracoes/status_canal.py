from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class VoiceStatusController:
    """Controla o ciclo de vida do status temporário do canal de voz.

    O player só informa eventos (faixa iniciou, mudou, acabou). Este controlador
    serializa os writes, invalida tarefas antigas por geração e restaura o
    status original/idle quando a sessão termina.
    """

    def __init__(self, router: Any) -> None:
        self.router = router

    @staticmethod
    def _generation(state: Any) -> int:
        return int(getattr(state, "voice_status_generation", 0) or 0)

    def _bump_generation(self, state: Any, *, reason: str) -> int:
        generation = self._generation(state) + 1
        state.voice_status_generation = generation
        logger.debug(
            "[music/voice-status] generation | generation=%s reason=%s",
            generation,
            reason,
        )
        return generation

    def _is_current_generation(self, state: Any, generation: int) -> bool:
        return self._generation(state) == int(generation)

    def _reset_runtime_state(self, state: Any) -> None:
        state.voice_status_channel_id = None
        state.voice_status_had_original = False
        state.voice_status_original_known = False
        state.voice_status_original = ""
        state.voice_status_owned = False
        state.voice_status_last_bot = ""
        state.voice_status_last_track_key = ""
        state.voice_status_last_applied_key = ""
        state.voice_status_last_sync_request_key = ""
        state.voice_status_last_sync_request_at = 0.0
        state.voice_status_last_update_at = 0.0

    def cancel_tasks(self, state: Any) -> None:
        current = asyncio.current_task()
        for attr in ("voice_status_update_task", "voice_status_force_task"):
            task = getattr(state, attr, None)
            if task is not None and not task.done() and task is not current:
                task.cancel()
            if task is not current:
                setattr(state, attr, None)

    def mark_track_change(self, state: Any) -> None:
        # A geração é incrementada no schedule, quando já existe um novo evento
        # autoritativo para aplicar. Aqui só removemos a deduplicação visual.
        state.voice_status_last_track_key = ""
        state.voice_status_last_applied_key = ""
        state.voice_status_last_sync_request_key = ""
        state.voice_status_last_sync_request_at = 0.0

    async def _restore_locked(
        self,
        guild: Any,
        state: Any,
        *,
        reason: str,
        channel_hint: Any = None,
    ) -> None:
        router = self.router
        record = None
        if getattr(state, "voice_status_channel_id", None):
            record = {
                "channel_id": int(state.voice_status_channel_id),
                "had_original_status": bool(getattr(state, "voice_status_had_original", False)),
                "original_known_status": bool(getattr(state, "voice_status_original_known", False)),
                "original_status": str(getattr(state, "voice_status_original", "") or ""),
                "owned_by_bot": bool(getattr(state, "voice_status_owned", False)),
                "last_bot_status": str(getattr(state, "voice_status_last_bot", "") or ""),
            }
        else:
            record = router._load_voice_status_record_into_state(guild.id, state)
        if not record:
            return

        try:
            channel_id = int(record.get("channel_id") or 0)
            original_status = str(record.get("original_status") or "")
            had_original = bool(record.get("had_original_status"))
            # Registros antigos não distinguiam vazio conhecido de desconhecido.
            original_known = bool(record.get("original_known_status", had_original))
            last_bot_status = str(record.get("last_bot_status") or "")
            owned = bool(record.get("owned_by_bot", bool(last_bot_status)))
        except Exception:
            await router._clear_voice_status_record(guild.id)
            self._reset_runtime_state(state)
            return

        if channel_id <= 0:
            await router._clear_voice_status_record(guild.id)
            self._reset_runtime_state(state)
            return

        channel = channel_hint if channel_hint is not None and int(getattr(channel_hint, "id", 0) or 0) == channel_id else None
        channel = channel or guild.get_channel(channel_id) or router.bot.get_channel(channel_id)
        if channel is None:
            await router._clear_voice_status_record(guild.id)
            self._reset_runtime_state(state)
            return

        known, current_status = await router._fetch_voice_channel_status(channel)
        idle = str(router._voice_status_settings_from_doc(guild.id).get("idle") or "")
        target_status = original_status if original_known else idle
        target_status = router._trim_voice_status(target_status)
        current_status = router._trim_voice_status(current_status)
        last_bot_status = router._trim_voice_status(last_bot_status)

        if known and last_bot_status and current_status != last_bot_status:
            # Alteração externa/staff: não sobrescreva algo que já não é nosso.
            logger.info(
                "[music/voice-status] restore ignorado por override externo | guild=%s channel=%s reason=%s",
                guild.id,
                channel_id,
                reason,
            )
            await router._clear_voice_status_record(guild.id)
            self._reset_runtime_state(state)
            return

        if known and current_status == target_status:
            await router._clear_voice_status_record(guild.id)
            self._reset_runtime_state(state)
            return

        # O ponto crítico: se o bot aplicou o status temporário, uma leitura
        # indisponível do GET não impede a limpeza. Status vazio é enviado como
        # null pelo endpoint PUT, em vez de apenas esquecer o registro local.
        if owned:
            if not router._bot_can_set_voice_status(guild, channel):
                await router._clear_voice_status_record(guild.id)
                self._reset_runtime_state(state)
                logger.debug(
                    "[music/voice-status] restore sem permissão | guild=%s channel=%s",
                    guild.id,
                    channel_id,
                )
                return
            restore_key = f"{channel_id}:{target_status}:{reason}"
            now = time.monotonic()
            if (
                restore_key == str(getattr(state, "voice_status_last_restore_key", "") or "")
                and now - float(getattr(state, "voice_status_last_restore_at", 0.0) or 0.0) < 1.5
            ):
                return
            state.voice_status_last_restore_key = restore_key
            state.voice_status_last_restore_at = now
            ok = await router._set_voice_channel_status(
                channel,
                target_status,
                reason=f"Restaurar status do canal após música ({reason})",
            )
            if not ok:
                # Falha de rede/API não deve apagar ownership persistido; assim o
                # próximo reconcile ainda consegue restaurar.
                logger.warning(
                    "[music/voice-status] restore falhou; mantendo registro | guild=%s channel=%s reason=%s",
                    guild.id,
                    channel_id,
                    reason,
                )
                return
            logger.info(
                "[music/voice-status] restaurado | guild=%s channel=%s reason=%s target=%r",
                guild.id,
                channel_id,
                reason,
                target_status,
            )

        await router._clear_voice_status_record(guild.id)
        self._reset_runtime_state(state)

    async def restore(
        self,
        guild: Any,
        state: Any,
        *,
        reason: str = "music_finished",
        channel_hint: Any = None,
    ) -> None:
        self._bump_generation(state, reason=f"restore:{reason}")
        self.cancel_tasks(state)
        if guild is None:
            return
        async with state.voice_status_lock:
            await self._restore_locked(guild, state, reason=reason, channel_hint=channel_hint)

    async def apply(
        self,
        guild: Any,
        channel: Any,
        state: Any,
        track: Any,
        *,
        force: bool = False,
        generation: int | None = None,
        reason: str = "track_sync",
    ) -> None:
        router = self.router
        if guild is None or channel is None or track is None:
            return
        settings = router._voice_status_settings_from_doc(guild.id)
        if not bool(settings.get("enabled", True)):
            return
        if not router._bot_can_set_voice_status(guild, channel):
            logger.warning("[music/voice-status] sem permissão | guild=%s", guild.id)
            return
        channel_id = int(getattr(channel, "id", 0) or 0)
        if channel_id <= 0:
            return

        if generation is None:
            generation = self._generation(state)
        generation = int(generation)
        if not self._is_current_generation(state, generation):
            return

        track_key = router._voice_status_track_key(track)
        desired = router.render_voice_status(guild.id, track, template=settings.get("template"))
        if not desired:
            return
        desired_key = f"{channel_id}:{track_key}:{desired}"

        async with state.voice_status_lock:
            if not self._is_current_generation(state, generation):
                return
            if force and not router._voice_status_track_is_current(state, track, track_key):
                return

            if getattr(state, "voice_status_channel_id", None) and int(state.voice_status_channel_id) != channel_id:
                await self._restore_locked(guild, state, reason="channel_change")
                if not self._is_current_generation(state, generation):
                    return

            same_channel = int(getattr(state, "voice_status_channel_id", 0) or 0) == channel_id
            if not same_channel:
                known, current_status = await router._fetch_voice_channel_status(channel)
                if not self._is_current_generation(state, generation):
                    return
                current_status = router._trim_voice_status(current_status if known else "")
                record = {
                    "channel_id": channel_id,
                    "had_original_status": bool(known and current_status),
                    "original_known_status": bool(known),
                    "original_status": current_status,
                    "owned_by_bot": True,
                    "last_bot_status": "",
                    "last_track_key": "",
                    "started_at": time.time(),
                    "reason": "music_player",
                }
                if not await router._save_voice_status_record(guild.id, record):
                    return
                state.voice_status_channel_id = channel_id
                state.voice_status_had_original = bool(known and current_status)
                state.voice_status_original_known = bool(known)
                state.voice_status_original = current_status
                state.voice_status_owned = True
                state.voice_status_last_bot = ""
                state.voice_status_last_track_key = ""
            elif getattr(state, "voice_status_last_bot", "") and not force:
                known, current_status = await router._fetch_voice_channel_status(channel)
                if not self._is_current_generation(state, generation):
                    return
                current_status = router._trim_voice_status(current_status)
                if known and current_status != router._trim_voice_status(state.voice_status_last_bot):
                    logger.info(
                        "[music/voice-status] ownership liberado por override externo | guild=%s channel=%s",
                        guild.id,
                        channel_id,
                    )
                    await router._clear_voice_status_record(guild.id)
                    self._reset_runtime_state(state)
                    return

            # Dedup independe de force: se o texto visível já é exatamente o
            # desejado não há motivo para fazer outro PUT. Troca real com texto
            # diferente continua passando imediatamente.
            if (
                int(getattr(state, "voice_status_channel_id", 0) or 0) == channel_id
                and str(getattr(state, "voice_status_last_bot", "") or "") == desired
                and str(getattr(state, "voice_status_last_track_key", "") or "") == track_key
            ):
                return
            if not force and desired_key == str(getattr(state, "voice_status_last_applied_key", "") or ""):
                return
            if not self._is_current_generation(state, generation):
                return
            if force and not router._voice_status_track_is_current(state, track, track_key):
                return

            logger.info(
                "[music/voice-status] desired | guild=%s channel=%s generation=%s reason=%s track=%r",
                guild.id,
                channel_id,
                generation,
                reason,
                getattr(track, "title", ""),
            )
            if not await router._set_voice_channel_status(
                channel,
                desired,
                reason="Atualizar status do canal enquanto a música toca",
            ):
                # Não perca o registro persistido em falha transitória. Ele é a
                # garantia de restore/reconcile posterior.
                return
            if not self._is_current_generation(state, generation):
                # O write antigo pode ter terminado depois de uma transição. A
                # geração nova já está agendada e vai corrigi-lo; não marque o
                # valor antigo como autoritativo localmente.
                logger.info(
                    "[music/voice-status] write antigo concluído após nova geração | guild=%s channel=%s generation=%s current=%s",
                    guild.id,
                    channel_id,
                    generation,
                    self._generation(state),
                )
                return

            state.voice_status_owned = True
            state.voice_status_last_bot = desired
            state.voice_status_last_track_key = track_key
            state.voice_status_last_applied_key = desired_key
            state.voice_status_last_update_at = time.monotonic()
            await router._save_voice_status_record(
                guild.id,
                {
                    "channel_id": channel_id,
                    "had_original_status": bool(getattr(state, "voice_status_had_original", False)),
                    "original_known_status": bool(getattr(state, "voice_status_original_known", False)),
                    "original_status": str(getattr(state, "voice_status_original", "") or ""),
                    "owned_by_bot": True,
                    "last_bot_status": desired,
                    "last_track_key": track_key,
                    "started_at": time.time(),
                    "reason": "music_player",
                },
            )
            logger.info(
                "[music/voice-status] applied | guild=%s channel=%s generation=%s reason=%s",
                guild.id,
                channel_id,
                generation,
                reason,
            )
            self.schedule_refresh(guild.id, state)

    def schedule_track_sync(
        self,
        guild_id: int,
        *,
        repeat_after: float = 2.0,
        reason: str = "track_change",
    ) -> None:
        router = self.router
        state = router.get_state(guild_id)
        current_track = getattr(state, "current", None)
        if current_track is None:
            return
        generation = self._bump_generation(state, reason=f"track:{reason}")
        self.cancel_tasks(state)
        track_key = router._voice_status_track_key(current_track)
        state.voice_status_last_sync_request_key = f"{track_key}:{reason}:{generation}"
        state.voice_status_last_sync_request_at = time.monotonic()

        async def _runner() -> None:
            try:
                guild = router.bot.get_guild(int(guild_id))
                if guild is None or not self._is_current_generation(state, generation):
                    return
                track = state.current
                if track is None or not state.last_voice_channel_id:
                    return
                if router._voice_status_track_key(track) != track_key:
                    return
                channel = guild.get_channel(int(state.last_voice_channel_id)) or router.bot.get_channel(int(state.last_voice_channel_id))
                if channel is None:
                    return
                await self.apply(
                    guild,
                    channel,
                    state,
                    track,
                    force=True,
                    generation=generation,
                    reason=reason,
                )
                if repeat_after <= 0:
                    return
                await asyncio.sleep(max(0.0, float(repeat_after)))
                if not self._is_current_generation(state, generation):
                    return
                track = state.current
                if track is None or router._voice_status_track_key(track) != track_key:
                    return
                channel = guild.get_channel(int(state.last_voice_channel_id)) or router.bot.get_channel(int(state.last_voice_channel_id))
                if channel is None:
                    return
                # Retry/reconcile não força PUT duplicado: apply deduplica quando
                # o primeiro write já foi concluído com sucesso.
                await self.apply(
                    guild,
                    channel,
                    state,
                    track,
                    force=False,
                    generation=generation,
                    reason=f"{reason}:retry",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music/voice-status] sync falhou", exc_info=True)
            finally:
                if getattr(state, "voice_status_force_task", None) is asyncio.current_task():
                    state.voice_status_force_task = None

        try:
            task = asyncio.create_task(_runner())
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            state.voice_status_force_task = task
        except RuntimeError:
            state.voice_status_force_task = None

    def schedule_refresh(self, guild_id: int, state: Any) -> None:
        router = self.router
        settings = router._voice_status_settings_from_doc(guild_id)
        template = str(settings.get("template") or "")
        if "{elapsed}" not in template and "{remaining}" not in template:
            return
        task = getattr(state, "voice_status_update_task", None)
        if task is not None and not task.done():
            return
        generation = self._generation(state)

        async def _runner() -> None:
            try:
                while (
                    self._is_current_generation(state, generation)
                    and state.current is not None
                    and state.current_status in {"playing", "paused"}
                ):
                    await asyncio.sleep(max(1.0, float(getattr(router, "_voice_status_update_interval_seconds", 60.0) or 60.0)))
                    if not self._is_current_generation(state, generation):
                        return
                    guild = router.bot.get_guild(int(guild_id))
                    if guild is None or state.current is None or not state.last_voice_channel_id:
                        return
                    channel = guild.get_channel(int(state.last_voice_channel_id)) or router.bot.get_channel(int(state.last_voice_channel_id))
                    if channel is None:
                        return
                    await self.apply(
                        guild,
                        channel,
                        state,
                        state.current,
                        force=False,
                        generation=generation,
                        reason="elapsed_refresh",
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music/voice-status] refresh falhou", exc_info=True)
            finally:
                if getattr(state, "voice_status_update_task", None) is asyncio.current_task():
                    state.voice_status_update_task = None

        try:
            task = asyncio.create_task(_runner())
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            state.voice_status_update_task = task
        except RuntimeError:
            state.voice_status_update_task = None
