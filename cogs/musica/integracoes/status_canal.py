from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)


def instalar_ponte_gateway_status_canal(bot: Any) -> bool:
    """Registra somente o parser de VOICE_CHANNEL_STATUS_UPDATE.

    Evita ``enable_debug_events=True`` e, portanto, evita redispatch/parse de
    todo pacote recebido do Gateway. Retorna ``True`` apenas quando instalou a
    ponte; parser nativo de versões futuras é sempre preservado.
    """
    connection = getattr(bot, "_connection", None)
    parsers = getattr(connection, "parsers", None)
    if not isinstance(parsers, dict):
        return False
    if "VOICE_CHANNEL_STATUS_UPDATE" in parsers:
        return False

    def _parse_voice_channel_status_update(data: dict[str, Any]) -> None:
        try:
            bot.dispatch("music_voice_channel_status_update_raw", dict(data or {}))
        except Exception:
            logger.debug("[music/voice-status] falha ao despachar evento raw", exc_info=True)

    parsers["VOICE_CHANNEL_STATUS_UPDATE"] = _parse_voice_channel_status_update
    return True


class VoiceStatusController:
    """Controla o ciclo de vida do status temporário do canal de voz.

    O player só informa eventos (faixa iniciou, mudou, acabou). Este controlador
    serializa os writes, invalida tarefas antigas por geração, observa os eventos
    reais do Gateway e restaura o status original/idle quando a sessão termina.
    """

    def __init__(self, router: Any) -> None:
        self.router = router
        # Cache best-effort do último status realmente visto no Gateway. Ele
        # também é útil antes de a música assumir ownership do canal.
        self._observed: dict[tuple[int, int], tuple[str, float]] = {}
        # Métricas leves, fora do hot path de áudio. Servem para diagnosticar
        # status preso/rate-limit sem aumentar o nível de log global.
        self._metrics: dict[int, Counter[str]] = {}

    def _metric(self, guild_id: int | None, key: str, amount: int = 1) -> None:
        try:
            gid = int(guild_id or 0)
        except Exception:
            gid = 0
        if gid <= 0:
            return
        self._metrics.setdefault(gid, Counter())[str(key)] += int(amount)

    def metrics_snapshot(self, guild_id: int) -> dict[str, int]:
        try:
            gid = int(guild_id or 0)
        except Exception:
            gid = 0
        return dict(self._metrics.get(gid, Counter()))

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
        state.voice_status_external_override = False
        state.voice_status_external_status = ""
        state.voice_status_gateway_status_known = False
        state.voice_status_gateway_status = ""
        state.voice_status_gateway_event_at = 0.0
        state.voice_status_expected_status = ""
        state.voice_status_expected_until = 0.0
        state.voice_status_last_write_at = 0.0
        state.voice_status_retry_count = 0

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

    def _remember_expected_gateway(self, state: Any, status: str) -> None:
        router = self.router
        state.voice_status_expected_status = router._trim_voice_status(status)
        state.voice_status_expected_until = time.monotonic() + max(
            1.0,
            float(getattr(router, "_voice_status_gateway_ack_seconds", 8.0) or 8.0),
        )

    async def _write_with_retry(
        self,
        channel: Any,
        status: str,
        state: Any,
        *,
        reason: str,
        generation: int | None,
        guild_id: int | None = None,
    ) -> bool:
        router = self.router
        attempts = max(1, min(5, int(getattr(router, "_voice_status_write_retries", 3) or 3)))
        base_delay = max(0.1, float(getattr(router, "_voice_status_retry_base_seconds", 0.75) or 0.75))
        state.voice_status_retry_count = 0

        for attempt in range(attempts):
            if generation is not None and not self._is_current_generation(state, generation):
                self._metric(guild_id, "stale_generation_dropped")
                return False
            self._remember_expected_gateway(state, status)
            self._metric(guild_id, "write_attempts")
            ok = await router._set_voice_channel_status(channel, status, reason=reason)
            if ok:
                state.voice_status_last_write_at = time.monotonic()
                state.voice_status_retry_count = attempt
                self._metric(guild_id, "write_success")
                return True
            self._metric(guild_id, "write_failures")
            state.voice_status_expected_status = ""
            state.voice_status_expected_until = 0.0
            state.voice_status_retry_count = attempt + 1
            http_status = int(getattr(router, "_voice_status_last_http_status", 0) or 0)
            retry_after = max(0.0, float(getattr(router, "_voice_status_last_retry_after_seconds", 0.0) or 0.0))
            if http_status == 429:
                self._metric(guild_id, "rate_limited")
            # 401/403/404 não melhoram com retry. Evita PUTs inúteis quando
            # permissão/endpoint não está disponível.
            if http_status in {401, 403, 404}:
                self._metric(guild_id, "permanent_http_failures")
                break
            if attempt + 1 >= attempts:
                break
            self._metric(guild_id, "retries")
            delay = min(15.0, max(base_delay * (2**attempt), retry_after if http_status == 429 else 0.0))
            logger.info(
                "[music/voice-status] retry agendado | channel=%s attempt=%s/%s http=%s delay=%.2fs",
                getattr(channel, "id", None),
                attempt + 2,
                attempts,
                http_status or "-",
                delay,
            )
            await asyncio.sleep(delay)

        return False

    async def handle_gateway_update(self, guild_id: int, channel_id: int, status: str | None) -> None:
        """Consome VOICE_CHANNEL_STATUS_UPDATE do Gateway.

        O evento é a fonte de verdade para ownership. Mudança que corresponde a
        um PUT pendente do bot é tratada como ACK; qualquer texto diferente em
        um canal que o bot possuía vira override externo e é respeitado até o
        fim da sessão musical.
        """

        router = self.router
        guild_id = int(guild_id or 0)
        channel_id = int(channel_id or 0)
        if guild_id <= 0 or channel_id <= 0:
            return
        normalized = router._trim_voice_status(status or "")
        now = time.monotonic()
        self._observed[(guild_id, channel_id)] = (normalized, now)

        # Não crie MusicGuildState para status de canal sem sessão de música.
        state = getattr(router, "_states", {}).get(guild_id)
        if state is None:
            return
        if int(getattr(state, "voice_status_channel_id", 0) or 0) != channel_id:
            return

        state.voice_status_gateway_status_known = True
        state.voice_status_gateway_status = normalized
        state.voice_status_gateway_event_at = now

        expected = router._trim_voice_status(getattr(state, "voice_status_expected_status", "") or "")
        expected_until = float(getattr(state, "voice_status_expected_until", 0.0) or 0.0)
        if now <= expected_until and normalized == expected:
            self._metric(guild_id, "gateway_acks")
            state.voice_status_expected_status = ""
            state.voice_status_expected_until = 0.0
            logger.debug(
                "[music/voice-status] gateway ack | guild=%s channel=%s status=%r",
                guild_id,
                channel_id,
                normalized,
            )
            return

        last_bot = router._trim_voice_status(getattr(state, "voice_status_last_bot", "") or "")
        if bool(getattr(state, "voice_status_owned", False)) and normalized == last_bot:
            return

        if not bool(getattr(state, "voice_status_owned", False)):
            if bool(getattr(state, "voice_status_external_override", False)):
                state.voice_status_external_status = normalized
            return

        # Mudança externa: invalida qualquer task velha e solta ownership sem
        # tentar restaurar no fim, pois o status agora pertence ao staff/usuário.
        self._metric(guild_id, "external_overrides")
        self._bump_generation(state, reason="external_override")
        self.cancel_tasks(state)
        state.voice_status_owned = False
        state.voice_status_external_override = True
        state.voice_status_external_status = normalized
        state.voice_status_expected_status = ""
        state.voice_status_expected_until = 0.0
        await router._clear_voice_status_record(guild_id)
        logger.info(
            "[music/voice-status] external_override | guild=%s channel=%s bot=%r external=%r",
            guild_id,
            channel_id,
            last_bot,
            normalized,
        )

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
        if getattr(state, "voice_status_channel_id", None) and not bool(getattr(state, "voice_status_external_override", False)):
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
            # Um override externo remove o registro persistido de propósito. No
            # fim da sessão, apenas liberamos o estado local; nunca sobrescreva o
            # texto que o staff colocou.
            if bool(getattr(state, "voice_status_external_override", False)):
                logger.info(
                    "[music/voice-status] restore pulado; ownership externo | guild=%s channel=%s reason=%s",
                    guild.id,
                    getattr(state, "voice_status_channel_id", None),
                    reason,
                )
                self._reset_runtime_state(state)
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

        known = bool(getattr(state, "voice_status_gateway_status_known", False))
        current_status = str(getattr(state, "voice_status_gateway_status", "") or "") if known else ""
        if not known:
            known, current_status = await router._fetch_voice_channel_status(channel)

        idle = str(router._voice_status_settings_from_doc(guild.id).get("idle") or "")
        target_status = original_status if original_known else idle
        target_status = router._trim_voice_status(target_status)
        current_status = router._trim_voice_status(current_status)
        last_bot_status = router._trim_voice_status(last_bot_status)

        if known and last_bot_status and current_status != last_bot_status:
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
            ok = await self._write_with_retry(
                channel,
                target_status,
                state,
                reason=f"Restaurar status do canal após música ({reason})",
                generation=self._generation(state),
                guild_id=int(guild.id),
            )
            if not ok:
                logger.warning(
                    "[music/voice-status] restore falhou; mantendo registro | guild=%s channel=%s reason=%s",
                    guild.id,
                    channel_id,
                    reason,
                )
                return
            self._metric(guild.id, "restores")
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
        reassert: bool = False,
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

            current_channel_id = int(getattr(state, "voice_status_channel_id", 0) or 0)
            if bool(getattr(state, "voice_status_external_override", False)):
                if current_channel_id == channel_id:
                    # Política padrão: uma alteração manual vence até a sessão
                    # musical acabar. Troca de faixa não deve lutar com staff.
                    return
                self._reset_runtime_state(state)
                current_channel_id = 0

            if current_channel_id and current_channel_id != channel_id:
                await self._restore_locked(guild, state, reason="channel_change")
                if not self._is_current_generation(state, generation):
                    return

            same_channel = int(getattr(state, "voice_status_channel_id", 0) or 0) == channel_id
            if not same_channel:
                observed = self._observed.get((int(guild.id), channel_id))
                if observed is not None:
                    known, current_status = True, observed[0]
                else:
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
                state.voice_status_gateway_status_known = bool(known)
                state.voice_status_gateway_status = current_status
                state.voice_status_gateway_event_at = time.monotonic() if known else 0.0
            elif getattr(state, "voice_status_last_bot", "") and not force:
                # O Gateway é prioritário. GET fica apenas como fallback para
                # versões/ambientes onde a ponte do evento não está disponível.
                known = bool(getattr(state, "voice_status_gateway_status_known", False))
                current_status = str(getattr(state, "voice_status_gateway_status", "") or "") if known else ""
                if not known:
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
                    state.voice_status_owned = False
                    state.voice_status_external_override = True
                    state.voice_status_external_status = current_status
                    await router._clear_voice_status_record(guild.id)
                    return

            if not reassert:
                if (
                    int(getattr(state, "voice_status_channel_id", 0) or 0) == channel_id
                    and str(getattr(state, "voice_status_last_bot", "") or "") == desired
                    and str(getattr(state, "voice_status_last_track_key", "") or "") == track_key
                ):
                    self._metric(guild.id, "deduplicated")
                    return
                if not force and desired_key == str(getattr(state, "voice_status_last_applied_key", "") or ""):
                    self._metric(guild.id, "deduplicated")
                    return
            if not self._is_current_generation(state, generation):
                return
            if force and not router._voice_status_track_is_current(state, track, track_key):
                return

            logger.info(
                "[music/voice-status] desired | guild=%s channel=%s generation=%s reason=%s track=%r reassert=%s",
                guild.id,
                channel_id,
                generation,
                reason,
                getattr(track, "title", ""),
                reassert,
            )
            if not await self._write_with_retry(
                channel,
                desired,
                state,
                reason="Atualizar status do canal enquanto a música toca",
                generation=generation,
                guild_id=int(guild.id),
            ):
                return
            if not self._is_current_generation(state, generation):
                self._metric(guild.id, "stale_generation_dropped")
                logger.info(
                    "[music/voice-status] write antigo concluído após nova geração | guild=%s channel=%s generation=%s current=%s",
                    guild.id,
                    channel_id,
                    generation,
                    self._generation(state),
                )
                return

            state.voice_status_owned = True
            state.voice_status_external_override = False
            state.voice_status_external_status = ""
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
            self._metric(guild.id, "applied")
            if reason in {"pause", "agent_pause"}:
                self._metric(guild.id, "pause_updates")
            elif reason in {"resume", "agent_resume"}:
                self._metric(guild.id, "resume_updates")
            elif "move" in str(reason):
                self._metric(guild.id, "channel_move_updates")
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
                if self._is_current_generation(state, generation):
                    self.schedule_refresh(guild_id, state)
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
        """Mantém refresh dinâmico e watchdog também para template estático."""

        router = self.router
        if bool(getattr(state, "voice_status_external_override", False)):
            return
        settings = router._voice_status_settings_from_doc(guild_id)
        template = str(settings.get("template") or "")
        dynamic = any(token in template for token in ("{elapsed}", "{remaining}", "{position}"))
        task = getattr(state, "voice_status_update_task", None)
        if task is not None and not task.done():
            return
        generation = self._generation(state)
        interval = (
            max(1.0, float(getattr(router, "_voice_status_update_interval_seconds", 60.0) or 60.0))
            if dynamic
            else max(1.0, float(getattr(router, "_voice_status_watchdog_interval_seconds", 45.0) or 45.0))
        )
        reassert_seconds = max(30.0, float(getattr(router, "_voice_status_reassert_seconds", 240.0) or 240.0))

        async def _runner() -> None:
            try:
                while (
                    self._is_current_generation(state, generation)
                    and state.current is not None
                    and state.current_status in {"playing", "paused"}
                    and not bool(getattr(state, "voice_status_external_override", False))
                ):
                    await asyncio.sleep(interval)
                    if not self._is_current_generation(state, generation):
                        return
                    guild = router.bot.get_guild(int(guild_id))
                    if guild is None or state.current is None or not state.last_voice_channel_id:
                        return
                    channel = guild.get_channel(int(state.last_voice_channel_id)) or router.bot.get_channel(int(state.last_voice_channel_id))
                    if channel is None:
                        return

                    reassert = False
                    if not dynamic:
                        # Com ACK recente do Gateway não existe motivo para PUT
                        # periódico. Sem observação confiável, um reassert raro
                        # recupera status perdido sem virar polling agressivo.
                        gateway_known = bool(getattr(state, "voice_status_gateway_status_known", False))
                        gateway_status = router._trim_voice_status(getattr(state, "voice_status_gateway_status", "") or "")
                        last_bot = router._trim_voice_status(getattr(state, "voice_status_last_bot", "") or "")
                        if gateway_known and last_bot and gateway_status != last_bot:
                            await self.handle_gateway_update(guild.id, channel.id, gateway_status)
                            return
                        age = time.monotonic() - float(getattr(state, "voice_status_last_write_at", 0.0) or 0.0)
                        reassert = not gateway_known and age >= reassert_seconds
                        if not reassert:
                            continue
                        self._metric(guild_id, "watchdog_reasserts")

                    await self.apply(
                        guild,
                        channel,
                        state,
                        state.current,
                        force=False,
                        generation=generation,
                        reason="elapsed_refresh" if dynamic else "watchdog_reassert",
                        reassert=reassert,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[music/voice-status] refresh/watchdog falhou", exc_info=True)
            finally:
                if getattr(state, "voice_status_update_task", None) is asyncio.current_task():
                    state.voice_status_update_task = None

        try:
            task = asyncio.create_task(_runner())
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            state.voice_status_update_task = task
        except RuntimeError:
            state.voice_status_update_task = None
