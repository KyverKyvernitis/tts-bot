"""Fila, transições e reprodução direta do Music Agent."""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import replace
from typing import Any

import discord

from .ciclo_vida import remove_owned_task, stop_player_instance
from .configuracao import env_float, env_int
from .estado import AgentTrack, GuildMusicState
from .efeitos import filtros
from .mixer_pcm import AgentMixedAudioSource, AgentTelemetryAudioSource, PCM_FRAME_BYTES
from .buffer_pcm import BufferedPCMSource
from .preparacao_audio import PreparacaoAudioMixin
from .utilitarios import safe_id, short_text


class VoiceSessionError(RuntimeError):
    """Falha de transporte/sessão Discord Voice, não falha da mídia."""


class ReproducaoMixin(PreparacaoAudioMixin):
    def _voice_operation_timeout_seconds(self) -> float:
        default = min(8.0, max(3.0, float(getattr(self, "prepare_timeout", 8.0) or 8.0)))
        return max(1.5, min(20.0, env_float("MUSIC_AGENT_VOICE_OPERATION_TIMEOUT_SECONDS", default)))

    async def _disconnect_voice_client_bounded(
        self,
        voice_client: Any,
        *,
        guild_id: int = 0,
        reason: str = "cleanup",
    ) -> bool:
        """Desconecta sem permitir que um VoiceClient stale prenda o agente."""
        if voice_client is None:
            return True
        timeout = self._voice_operation_timeout_seconds()
        try:
            await asyncio.wait_for(voice_client.disconnect(force=True), timeout=timeout)
            return True
        except (asyncio.TimeoutError, TimeoutError) as exc:
            self.log(
                "voice_disconnect_timeout",
                guild_id=int(guild_id or 0),
                reason=reason,
                timeout_seconds=round(timeout, 2),
                channel=getattr(getattr(voice_client, "channel", None), "id", None),
            )
            # VoiceProtocol.cleanup() remove o registro interno do discord.py.
            # É síncrono e serve como último recurso quando disconnect() ficou
            # preso após troca/perda de rede.
            cleanup = getattr(voice_client, "cleanup", None)
            if callable(cleanup):
                with contextlib.suppress(Exception):
                    cleanup()
            return False
        except Exception as exc:
            self.log(
                "voice_disconnect_failed",
                guild_id=int(guild_id or 0),
                reason=reason,
                channel=getattr(getattr(voice_client, "channel", None), "id", None),
                error=f"{type(exc).__name__}: {short_text(exc, 180)}",
            )
            cleanup = getattr(voice_client, "cleanup", None)
            if callable(cleanup):
                with contextlib.suppress(Exception):
                    cleanup()
            return False

    @staticmethod
    def _voice_client_is_connected(voice_client: Any) -> bool:
        if voice_client is None:
            return False
        with contextlib.suppress(Exception):
            return bool(getattr(voice_client, "is_connected", lambda: False)())
        return False

    def _registered_voice_client_for_guild(self, guild_id: int, guild: Any | None = None) -> Any | None:
        """Retorna o VoiceClient registrado mesmo durante estados parciais do cache.

        ``guild.voice_client`` normalmente consulta o mesmo registry do discord.py,
        mas durante cleanup/reconnect rápidos já vimos janelas onde a referência da
        guild e ``client.voice_clients`` ficam temporariamente divergentes. O fallback
        evita abrir uma segunda sessão ou perder a chance de limpar um cliente stale.
        """
        candidate = getattr(guild, "voice_client", None) if guild is not None else None
        if candidate is not None:
            return candidate
        for voice_client in list(getattr(self.client, "voice_clients", []) or []):
            owner = getattr(voice_client, "guild", None)
            owner_id = safe_id(getattr(owner, "id", 0))
            if owner_id == int(guild_id or 0):
                return voice_client
        return None

    def _resume_offset_for_track(self, track: AgentTrack, *, played_for: float, speed: float = 1.0) -> float:
        base_offset = max(0.0, float(getattr(track, "start_offset_seconds", 0.0) or 0.0))
        resume_offset = max(
            0.0,
            base_offset + max(0.0, float(played_for or 0.0)) * speed - self.stream_recovery_backtrack_seconds,
        )
        if track.duration is not None:
            with contextlib.suppress(Exception):
                resume_offset = min(resume_offset, max(0.0, float(track.duration) - 0.05))
        return resume_offset

    @staticmethod
    def _is_already_connected_voice_error(exc: BaseException) -> bool:
        text = str(exc or "").strip().lower()
        return "already connected to a voice channel" in text

    @staticmethod
    def _is_voice_transport_error(exc: BaseException, *, phase: str = "") -> bool:
        if isinstance(exc, VoiceSessionError):
            return True
        text = f"{type(exc).__name__}: {exc}".lower()
        if "already connected to a voice channel" in text:
            return True
        if phase in {"voice_preconnect", "voice_connect", "voice_move"}:
            return True
        voice_markers = (
            "voice websocket",
            "voice connection",
            "not connected to voice",
            "disconnected from voice",
            "voz caiu",
            "conexão de voz",
            "sessão de voz",
        )
        return any(marker in text for marker in voice_markers)

    def _classify_play_failure(self, exc: BaseException, *, phase: str) -> tuple[str, bool]:
        if self._is_voice_transport_error(exc, phase=phase):
            return "voice_transport", True
        if phase == "resolve":
            return "resolution", False
        text = f"{type(exc).__name__}: {exc}".lower()
        if "ffmpeg" in text or "stream" in text or "source" in text or "áudio" in text:
            return "media_start", False
        return "playback_start", False

    def _cancel_prefetch_tasks(
        self,
        guild_id: int | None = None,
        *,
        keep_task_keys: set[str] | None = None,
    ) -> int:
        cancelled = 0
        prefix = f"{int(guild_id)}:" if guild_id else ""
        keep = set(keep_task_keys or ())
        for key, task in list(self._prefetch_tasks.items()):
            if prefix and not str(key).startswith(prefix):
                continue
            if key in keep:
                continue
            if task is not None and not task.done():
                task.cancel()
                cancelled += 1
            self._prefetch_tasks.pop(key, None)
        if cancelled:
            self.log("prefetch_cancelled", guild_id=int(guild_id or 0), count=cancelled)
        return cancelled

    def _cancel_active_resolve(self, guild_id: int) -> bool:
        guild_id = int(guild_id or 0)
        task = self._active_resolve_tasks.pop(guild_id, None)
        if task is None or task.done():
            return False
        task.cancel()
        self.log("active_resolve_cancelled", guild_id=guild_id)
        return True

    def _bump_playback_generation(
        self,
        st: GuildMusicState,
        *,
        reason: str = "change",
        keep_prefetch_task_keys: set[str] | None = None,
    ) -> int:
        self._cancel_voice_runtime_recovery(st.guild_id)
        pending_pcm = self._starting_pcm.pop(st.guild_id, None)
        if pending_pcm is not None:
            pending_pcm.cleanup()
        st.playback_token += 1
        keep_audio = st.queue[0].queue_item_id if st.queue and reason in {"skip", "direct_after", "queue_play_now"} else ""
        self._cancel_audio_preparation(st.guild_id, keep_item_id=keep_audio)
        st.updated_at = time.time()
        if keep_prefetch_task_keys:
            self._cancel_prefetch_tasks(st.guild_id, keep_task_keys=keep_prefetch_task_keys)
        else:
            self._cancel_prefetch_tasks(st.guild_id)
        self._cancel_active_resolve(st.guild_id)
        self.log("playback_generation_bumped", guild_id=st.guild_id, reason=reason, token=st.playback_token)
        return st.playback_token

    def _guild_prefetch_key(self, guild_id: int, cache_key: str) -> str:
        return f"{int(guild_id or 0)}:{cache_key}"

    def _next_resolving_prefetch_keys(self, st: GuildMusicState) -> set[str]:
        """Preserva apenas a resolução já iniciada da próxima faixa da fila."""
        if not st.queue or st.queue[0].is_virtual_playlist_marker:
            return set()
        meta = st.queue[0].public()
        query = self._query_from_track_meta(
            meta,
            fallback_query=st.queue[0].query or st.queue[0].webpage_url or st.queue[0].title,
        )
        if not query:
            return set()
        key = self._guild_prefetch_key(st.guild_id, self._resolve_cache_key(query, meta))
        task = self._prefetch_tasks.get(key)
        if task is not None and not task.done() and key in self._prefetch_resolving:
            return {key}
        return set()

    def _schedule_next_queue_prefetch(self, guild_id: int, *, reason: str = "playing") -> None:
        self._schedule_audio_prepare(guild_id)
        if not self.prefetch_enabled:
            return
        st = self.states.setdefault(int(guild_id or 0), GuildMusicState(guild_id=int(guild_id or 0)))
        if not st.queue:
            return
        if st.queue[0].is_virtual_playlist_marker:
            # O cursor virtual não é uma faixa reproduzível. O monitor da VPS
            # materializa a próxima janela quando o marker se aproxima da frente.
            return
        meta = st.queue[0].public()
        query = self._query_from_track_meta(meta, fallback_query=st.queue[0].query or st.queue[0].webpage_url or st.queue[0].title)
        if not query:
            return
        cache_key = self._resolve_cache_key(query, meta)
        task_key = self._guild_prefetch_key(guild_id, cache_key)
        if self._cached_resolved_get(cache_key):
            return
        current_task = self._prefetch_tasks.get(task_key)
        if current_task is not None and not current_task.done():
            return
        token = int(getattr(st, "playback_token", 0) or 0)
        delay = 3.0
        current = st.current
        # Em coleções metadata-only mantenha exatamente uma faixa à frente
        # pronta. Wave 13 preserva o cache global, mas a próxima faixa continua
        # sendo aquecida imediatamente para skip sem espera.
        metadata_playlist_next = self._is_metadata_collection_item(meta)
        try:
            if metadata_playlist_next:
                delay = 0.0
            elif current is not None and current.duration and st.started_monotonic:
                remaining = max(0.0, float(current.duration) - st.source_position_seconds()) / st.playback_speed
                # Se a faixa já está dentro da janela de prefetch, resolva agora.
                # O mínimo antigo de 2s atrasava desnecessariamente faixas curtas.
                delay = max(0.0, remaining - env_float("MUSIC_AGENT_PREFETCH_BEFORE_END_SECONDS", 45.0))
        except Exception:
            delay = 3.0

        async def _runner() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                latest = self.states.setdefault(int(guild_id or 0), GuildMusicState(guild_id=int(guild_id or 0)))
                if int(getattr(latest, "playback_token", 0) or 0) != token or not latest.queue:
                    return
                current_first = latest.queue[0]
                current_key = self._resolve_cache_key(
                    self._query_from_track_meta(current_first.public(), fallback_query=current_first.query or current_first.webpage_url or current_first.title),
                    current_first.public(),
                )
                if current_key != cache_key:
                    return
                started = time.time()
                body = {
                    "guild_id": guild_id,
                    "voice_channel_id": latest.voice_channel_id,
                    "text_channel_id": latest.text_channel_id,
                    "requester_id": current_first.requester_id,
                    "requester_name": current_first.requester_name,
                    "query": query,
                    "track": current_first.public(),
                }
                self._prefetch_resolving.add(task_key)
                resolved = await asyncio.wait_for(self.resolve_track(query, track_meta=current_first.public(), body=body, priority=20), timeout=self.prefetch_timeout)
                latest2 = self.states.setdefault(int(guild_id or 0), GuildMusicState(guild_id=int(guild_id or 0)))
                if int(getattr(latest2, "playback_token", 0) or 0) == token and latest2.queue:
                    check_key = self._resolve_cache_key(
                        self._query_from_track_meta(latest2.queue[0].public(), fallback_query=latest2.queue[0].query or latest2.queue[0].webpage_url or latest2.queue[0].title),
                        latest2.queue[0].public(),
                    )
                    if check_key == cache_key:
                        current = latest2.current
                        current_logical = ""
                        if current is not None:
                            current_meta = current.public()
                            current_logical = self._resolve_cache_key(
                                self._query_from_track_meta(current_meta, fallback_query=current.query or current.webpage_url or current.title),
                                current_meta,
                            )
                        next_logical = cache_key
                        current_media = self._media_cache_key(current) if current is not None else ""
                        next_media = self._media_cache_key(resolved)
                        wrong_media_reuse = bool(
                            current_logical
                            and next_logical
                            and current_logical != next_logical
                            and current_media
                            and current_media == next_media
                        )
                        if wrong_media_reuse:
                            # Distinct logical songs resolving to the same stable
                            # media identity indicates a poisoned logical mapping.
                            # Keep the shared media cache intact for legitimate
                            # users, but discard this song -> media association so
                            # the next JIT resolve performs one fresh text search.
                            self._invalidate_logical_resolution(next_logical)
                            self.log(
                                "next_prefetch_media_reuse_rejected",
                                guild_id=guild_id,
                                current=getattr(current, "title", ""),
                                next=getattr(current_first, "title", ""),
                                media=next_media[:140],
                            )
                        else:
                            latest2.queue[0] = resolved
                self.log("next_prefetch_ready", guild_id=guild_id, reason=reason, elapsed_ms=round((time.time() - started) * 1000.0, 1), title=getattr(resolved, "title", ""))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.log("next_prefetch_failed", guild_id=guild_id, reason=reason, error=short_text(exc, 180))
            finally:
                self._prefetch_resolving.discard(task_key)
                remove_owned_task(self._prefetch_tasks, task_key, asyncio.current_task())

        self._prefetch_tasks[task_key] = asyncio.create_task(_runner())
        self.log("next_prefetch_scheduled", guild_id=guild_id, delay=round(delay, 2), title=st.queue[0].title, reason=reason)

    async def cmd_prefetch(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.prefetch_enabled:
            return {"ok": True, "accepted": 0, "disabled": True}
        tracks = body.get("tracks") if isinstance(body.get("tracks"), list) else []
        if not tracks:
            single = body.get("track") if isinstance(body.get("track"), dict) else {}
            if single:
                tracks = [single]
        prefetch_kind = str(body.get("prefetch_kind") or "background").strip().lower()
        try:
            limit = max(0, min(3, int(float(body.get("limit") or len(tracks) or 0))))
        except Exception:
            limit = min(2, len(tracks))
        # Seleção de busca só especula o top-1. Resolver também os resultados
        # 2/3 aumenta CPU/rede e pode atrasar justamente a faixa escolhida.
        if prefetch_kind == "selection":
            limit = min(limit, 1)
        guild_id = safe_id(body.get("guild_id"))
        state = self.states.get(guild_id)
        active = bool(
            state
            and state.current is not None
            and str(getattr(state, "status", "") or "").lower()
            in {"playing", "starting", "preparing", "paused"}
        )
        if prefetch_kind == "selection":
            priority = (
                int(self.selection_prefetch_active_priority)
                if active
                else int(self.selection_prefetch_idle_priority)
            )
        else:
            priority = 20
        accepted = 0
        for meta in tracks[:limit]:
            if not isinstance(meta, dict):
                continue
            query = self._query_from_track_meta(meta, fallback_query=body.get("query") or "")
            if not query:
                continue
            cache_key = self._resolve_cache_key(query, meta)
            task_key = self._guild_prefetch_key(safe_id(body.get("guild_id")), cache_key)
            if self._cached_resolved_get(cache_key):
                continue
            if task_key in self._prefetch_tasks and not self._prefetch_tasks[task_key].done():
                continue
            child = dict(body)
            child["track"] = dict(meta)
            child["_prefetch_priority"] = priority
            task = asyncio.create_task(self._prefetch_track(child, dict(meta), query, cache_key))
            self._prefetch_tasks[task_key] = task
            accepted += 1
        return {"ok": True, "accepted": accepted, "cache_size": len(self._resolve_cache), "metadata_cache_size": len(self._metadata_cache)}

    async def cmd_play(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        voice_channel_id = safe_id(body.get("voice_channel_id"))
        text_channel_id = safe_id(body.get("text_channel_id"))
        if not guild_id or not voice_channel_id:
            raise ValueError("guild_id e voice_channel_id são obrigatórios")
        action = str(body.get("_agent_action") or body.get("action") or body.get("command") or "play").strip().lower().replace("-", "_")
        query = str(body.get("query") or body.get("url") or body.get("webpage_url") or body.get("original_url") or "").strip()
        track_meta = body.get("track") if isinstance(body.get("track"), dict) else {}
        tracks_payload = body.get("tracks") if isinstance(body.get("tracks"), list) else []
        if not query and tracks_payload and isinstance(tracks_payload[0], dict):
            query = self._query_from_track_meta(tracks_payload[0], fallback_query="")
        if not query:
            query = self._query_from_track_meta(track_meta, fallback_query="")
        if not query and not tracks_payload:
            raise ValueError("query/url vazia")
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        self._update_auto_leave_from_body(st, body)
        st.voice_channel_id = voice_channel_id
        st.text_channel_id = text_channel_id
        if not st.normal_volume_percent:
            st.normal_volume_percent = self.default_volume_percent
        if not st.volume_percent:
            st.volume_percent = st.normal_volume_percent
        st.last_action = "play"
        self._cancel_idle_disconnect(guild_id)
        self._cancel_voice_presence_disconnect(guild_id)
        self._set_voice_session_mode(st, "music_active", reason="play_received")
        command_generation = int(getattr(st, "playback_token", 0) or 0)
        self.log("play_received", guild_id=guild_id, voice=voice_channel_id, query=query, action=action, tracks=len(tracks_payload), generation=command_generation)

        if tracks_payload:
            tracks: list[AgentTrack] = []
            for item in tracks_payload:
                if not isinstance(item, dict):
                    continue
                track = self._agent_track_from_metadata(item, body=body, fallback_query=query)
                if track.query or track.stream_url or track.webpage_url:
                    tracks.append(track)
            if not tracks:
                raise ValueError("playlist/fila sem faixas válidas")
            playable_added = sum(1 for item in tracks if not item.is_virtual_playlist_marker)
            active = bool(st.current and st.status in {"playing", "starting", "preparing", "paused"})
            if not active:
                # Start-first: uma playlist iniciada com o player ocioso é uma
                # interação ativa. Cancele yt-dlp especulativo antigo desta guild
                # antes de resolver a primeira faixa, para ela não ficar atrás de
                # prefetches de uma seleção/sessão anterior.
                self._cancel_prefetch_tasks(guild_id)
            st.queue.extend(tracks)
            st.updated_at = time.time()
            self.log(
                "queued_many",
                guild_id=guild_id,
                added=playable_added,
                virtual_markers=len(tracks) - playable_added,
                queue_size=len(st.queue),
                active=active,
            )
            if not active:
                await self._play_next(guild_id)
                if st.status in {"failed", "error"}:
                    return {"ok": False, "queued": False, "added": playable_added, "error": st.last_error or "falha ao iniciar playback", "state": st.public()}
                return {"ok": True, "queued": False, "added": playable_added, "state": st.public()}
            self._schedule_next_queue_prefetch(guild_id, reason="enqueue_many")
            return {"ok": True, "queued": True, "added": playable_added, "state": st.public()}

        query = self._query_from_track_meta(track_meta, fallback_query=query) or query
        # Ao escolher um resultado, mantenha apenas o prefetch da faixa escolhida.
        # Prefetches de candidatos alternativos não devem continuar disputando
        # rede/CPU com o playback que o usuário acabou de selecionar.
        selected_cache_key = self._resolve_cache_key(query, track_meta)
        selected_task_key = self._guild_prefetch_key(guild_id, selected_cache_key)
        pruned = self._cancel_prefetch_tasks(guild_id, keep_task_keys={selected_task_key})
        if pruned:
            self.log("prefetch_pruned_for_play", guild_id=guild_id, count=pruned, query=query[:90])
        # Não bloqueie o comando aguardando yt-dlp quando a faixa só precisa
        # entrar na fila. Guarde os metadados agora e resolva o stream apenas
        # quando ela realmente chegar ao início da fila. Isso também permite
        # que a primeira resolução seja sobreposta à conexão de voz.
        track = self._agent_track_from_metadata(track_meta, body=body, fallback_query=query)
        if not (track.query or track.stream_url or track.webpage_url):
            raise ValueError("faixa sem query/url reproduzível")
        if int(getattr(st, "playback_token", 0) or 0) != command_generation or str(getattr(st, "last_action", "") or "").lower() == "stop":
            self.log("play_enqueue_ignored", guild_id=guild_id, reason="stale_or_stopped", generation=command_generation, current_generation=getattr(st, "playback_token", 0), title=getattr(track, "title", ""))
            return {"ok": False, "cancelled": True, "queued": False, "error": "operação cancelada", "state": st.public()}
        if st.current and st.status in {"playing", "starting", "preparing", "paused"}:
            st.queue.append(track)
            st.updated_at = time.time()
            self._schedule_next_queue_prefetch(guild_id, reason="enqueue")
            self.log("queued_lazy", guild_id=guild_id, title=track.title, queue_size=len(st.queue))
            return {"ok": True, "queued": True, "track": track.public(), "state": st.public()}
        st.queue.append(track)
        await self._play_next(guild_id)
        if st.status in {"failed", "error"}:
            return {"ok": False, "queued": False, "error": st.last_error or "falha ao iniciar playback", "state": st.public()}
        return {"ok": True, "queued": False, "state": st.public()}

    async def cmd_playlist_refill(self, body: dict[str, Any]) -> dict[str, Any]:
        """Substitui atomicamente um marker virtual pela próxima janela.

        O ``expected_cursor`` funciona como compare-and-swap: refills atrasados
        ou duplicados são ignorados, evitando inserir a mesma página duas vezes.
        """

        guild_id = safe_id(body.get("guild_id"))
        if not guild_id:
            raise ValueError("guild_id é obrigatório")
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        expected = body.get("expected_cursor") if isinstance(body.get("expected_cursor"), dict) else {}
        next_cursor = body.get("next_cursor") if isinstance(body.get("next_cursor"), dict) else {}

        def _cursor_key(value: dict[str, Any]) -> tuple[str, str, str, int, int, int]:
            try:
                offset = max(0, int(value.get("next_offset") or 0))
            except Exception:
                offset = 0
            try:
                block_end = -1 if value.get("block_end_offset") in (None, "") else max(0, int(value.get("block_end_offset")))
            except Exception:
                block_end = -1
            try:
                shuffle_seed = max(0, int(value.get("shuffle_seed") or 0))
            except Exception:
                shuffle_seed = 0
            return (
                str(value.get("instance_id") or "").strip(),
                str(value.get("provider") or "").strip(),
                str(value.get("source_url") or "").strip(),
                offset,
                block_end,
                shuffle_seed,
            )

        expected_key = _cursor_key(expected)
        marker_index = -1
        marker: AgentTrack | None = None
        for index, item in enumerate(st.queue):
            if item.is_virtual_playlist_marker and _cursor_key(item.virtual_playlist_cursor) == expected_key:
                marker_index = index
                marker = item
                break
        if marker is None:
            self.log("playlist_refill_ignored", guild_id=guild_id, reason="stale_cursor", expected_offset=expected_key[3])
            return {"ok": True, "ignored": True, "added": 0, "state": st.public()}

        incoming: list[AgentTrack] = []
        max_refill_items = max(1, min(100, env_int("MUSIC_AGENT_PLAYLIST_REFILL_MAX_ITEMS", 50)))
        raw_tracks = body.get("tracks") if isinstance(body.get("tracks"), list) else []
        for item in raw_tracks[:max_refill_items]:
            if not isinstance(item, dict):
                continue
            track = self._agent_track_from_metadata(item, body=body, fallback_query="")
            if track.is_virtual_playlist_marker:
                continue
            if track.query or track.stream_url or track.webpage_url:
                incoming.append(track)
        if len(raw_tracks) > max_refill_items:
            self.log(
                "playlist_refill_capped",
                guild_id=guild_id,
                received=len(raw_tracks),
                accepted=max_refill_items,
            )

        exhausted = bool(next_cursor.get("exhausted")) if next_cursor else not incoming
        marker_shuffle_seed = max(0, int(marker.virtual_playlist_cursor.get("shuffle_seed") or 0))
        if len(incoming) > 1 and (marker_shuffle_seed or st.virtual_shuffle_active):
            import random as _random
            import zlib as _zlib
            if marker_shuffle_seed:
                # Bloco de shuffle global conhecido: a UI usa exatamente esta
                # mesma seed, então ordem exibida e ordem reproduzida coincidem.
                refill_seed = marker_shuffle_seed
            else:
                # Compatibilidade para providers que ainda não publicam total.
                # Cada janela futura é embaralhada determinísticamente.
                identity = "|".join((expected_key[0], expected_key[1], expected_key[2])).encode("utf-8", "ignore")
                refill_seed = int(st.virtual_shuffle_seed or 0) ^ int(expected_key[3] or 0) ^ _zlib.crc32(identity)
            _random.Random(refill_seed).shuffle(incoming)
        replacement: list[AgentTrack] = list(incoming)
        if next_cursor and not exhausted:
            marker_payload = {
                "title": str(next_cursor.get("title") or marker.title or "Playlist"),
                "webpage_url": str(next_cursor.get("source_url") or marker.webpage_url or ""),
                "source": "playlist-virtual",
                "virtual_playlist_cursor": dict(next_cursor),
                "requester_id": marker.requester_id,
                "requester_name": marker.requester_name,
            }
            replacement.append(self._agent_track_from_metadata(marker_payload, body=body, fallback_query=""))

        # Preserva tudo que foi enfileirado manualmente depois do marker.
        st.queue[marker_index:marker_index + 1] = replacement
        if st.virtual_shuffle_active and exhausted and not any(item.is_virtual_playlist_marker for item in st.queue):
            st.virtual_shuffle_active = False
            st.virtual_shuffle_seed = 0
        st.updated_at = time.time()
        self.log(
            "playlist_refilled",
            guild_id=guild_id,
            added=len(incoming),
            exhausted=exhausted,
            expected_offset=expected_key[3],
            next_offset=_cursor_key(next_cursor)[3] if next_cursor else expected_key[3],
            queue_size=len(st.queue),
        )

        # Se o worker estava parado exatamente no cursor, continue assim que a
        # janela chegar. Caso contrário, apenas prepare a próxima faixa normal.
        if marker_index == 0 and st.current is None:
            await self._play_next(guild_id, preserve_current_to_history=False)
        else:
            self._schedule_next_queue_prefetch(guild_id, reason="playlist_refill")
        return {"ok": True, "ignored": False, "added": len(incoming), "state": st.public()}

    async def cmd_pause(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        player = st.player
        if player and hasattr(player, "pause"):
            player.pause()
            if not st.paused_monotonic and st.started_monotonic:
                st.paused_monotonic = time.monotonic()
            st.paused = True
            # Um prefetch programado com base no tempo restante perde validade
            # enquanto a música está pausada. Recalcule apenas no resume.
            self._cancel_prefetch_tasks(guild_id)
            self._cancel_audio_preparation(guild_id)
            self._set_status(st, "paused", event="pause")
        return {"ok": True, "state": st.public()}

    async def cmd_resume(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        player = st.player
        if player and hasattr(player, "resume"):
            now = time.monotonic()
            if st.started_monotonic and st.paused_monotonic:
                # Desloque o relógio de início pelo tempo em pausa para a posição
                # e o prefetch não avançarem enquanto o VoiceClient está parado.
                st.started_monotonic += max(0.0, now - float(st.paused_monotonic))
            st.paused_monotonic = 0.0
            player.resume()
            st.paused = False
            self._set_status(st, "playing", event="resume")
            self._schedule_next_queue_prefetch(guild_id, reason="resume")
        return {"ok": True, "state": st.public()}

    def _track_key(self, track: AgentTrack | None) -> str:
        if track is None:
            return ""
        # ``query`` identifica a faixa lógica. Em playlists Spotify várias
        # entradas compartilham a mesma ``original_url`` da coleção; usar a
        # URL antes da query fazia músicas diferentes parecerem idênticas.
        for value in (track.query, track.webpage_url, track.original_url, track.stream_url, track.title):
            raw = str(value or "").strip().lower()
            if raw:
                return raw[:300]
        return ""

    def _guard_distinct_next_stream(self, st: GuildMusicState, previous: AgentTrack | None) -> bool:
        """Reject a poisoned logical->media mapping before a skip starts.

        Signed googlevideo URLs are not stable identities. Wave 13 compares the
        resolved public media fingerprint (e.g. YouTube video id + format) and
        only rejects reuse when two *different logical songs* map to that same
        media. Consecutive legitimate duplicates keep sharing cache.
        """
        if previous is None or not st.queue or st.queue[0].is_virtual_playlist_marker:
            return False
        next_track = st.queue[0]
        previous_meta = previous.public()
        next_meta = next_track.public()
        previous_query = self._query_from_track_meta(
            previous_meta,
            fallback_query=previous.query or previous.webpage_url or previous.original_url or previous.title,
        )
        next_query = self._query_from_track_meta(
            next_meta,
            fallback_query=next_track.query or next_track.webpage_url or next_track.original_url or next_track.title,
        )
        previous_logical = self._resolve_cache_key(previous_query, previous_meta) if previous_query else ""
        next_logical = self._resolve_cache_key(next_query, next_meta) if next_query else ""
        if not previous_logical or not next_logical or previous_logical == next_logical:
            return False

        previous_media = self._media_cache_key(previous)
        queued_media = self._media_cache_key(next_track)
        stable = self._metadata_cache_get(next_logical)
        cached_media = self._media_cache_key(stable)
        previous_stream = str(previous.stream_url or "").strip()
        queued_stream = str(next_track.stream_url or "").strip()
        same_stable_media = bool(previous_media and previous_media in {queued_media, cached_media})
        same_literal_stream = bool(previous_stream and queued_stream and previous_stream == queued_stream)
        contaminated = same_stable_media or same_literal_stream
        if not contaminated:
            return False

        # Invalidate only the wrong song->media mapping. The global stream cache
        # remains available to the actual media owner and other legitimate hits.
        self._invalidate_logical_resolution(next_logical)
        if queued_media == previous_media or same_literal_stream:
            next_track.stream_url = ""
            next_track.webpage_url = "" if self._is_metadata_collection_item(next_meta) else next_track.webpage_url
            next_track.transport_hint = "metadata-lazy"
            next_track.stream_resolved_monotonic = 0.0
        self.log(
            "playlist_media_reuse_guard",
            guild_id=st.guild_id,
            previous=getattr(previous, "title", ""),
            next=getattr(next_track, "title", ""),
            media=previous_media[:140],
            queued=bool(queued_media == previous_media),
            cached=bool(cached_media == previous_media),
            same_stream=same_literal_stream,
        )
        return True

    def _clone_track(self, track: AgentTrack | None) -> AgentTrack | None:
        if track is None:
            return None
        with contextlib.suppress(Exception):
            clone = replace(track)
            clone.start_offset_seconds = 0.0
            return clone
        return None

    def _push_history(self, st: GuildMusicState, track: AgentTrack | None) -> None:
        clone = self._clone_track(track)
        if clone is None:
            return
        key = self._track_key(clone)
        if key and st.history and self._track_key(st.history[-1]) == key:
            return
        st.history.append(clone)
        max_history = max(1, env_int("MUSIC_AGENT_HISTORY_MAXSIZE", 25))
        if len(st.history) > max_history:
            del st.history[:-max_history]

    def _track_from_command_payload(self, body: dict[str, Any], *, fallback_query: str = "") -> AgentTrack | None:
        meta = body.get("track") if isinstance(body.get("track"), dict) else {}
        if not meta:
            return None
        track = self._agent_track_from_metadata(meta, body=body, fallback_query=fallback_query or self._query_from_track_meta(meta, fallback_query=""))
        track.start_offset_seconds = 0.0
        return track

    async def _stop_player_instance(self, player: Any, *, disconnect: bool = False) -> None:
        source = getattr(player, "source", None)
        if isinstance(source, AgentMixedAudioSource) and source.persistent:
            source.stop_music()
        if not disconnect:
            await stop_player_instance(player, disconnect=False)
            return
        if player is None:
            return
        with contextlib.suppress(Exception):
            if getattr(player, "is_playing", lambda: False)() or getattr(player, "is_paused", lambda: False)():
                player.stop()
        await self._disconnect_voice_client_bounded(
            player,
            guild_id=int(getattr(getattr(player, "guild", None), "id", 0) or 0),
            reason="stop_player",
        )

    async def _stop_current_player_for_transition(self, st: GuildMusicState, *, disconnect: bool = False) -> None:
        player = st.player
        if disconnect:
            st.player = None
        await self._stop_player_instance(player, disconnect=disconnect)

    async def cmd_stop(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.last_action = "stop"
        self._cancel_prefetch_tasks(guild_id)
        self._cancel_idle_disconnect(guild_id)
        self._cancel_voice_presence_disconnect(guild_id)
        st.queue.clear()
        st.history.clear()
        st.virtual_shuffle_active = False
        st.virtual_shuffle_seed = 0
        st.bassboost = False
        st.nightcore = False
        st.effects_revision += 1
        player = st.player
        st.player = None
        st.current = None
        self._set_status(st, "idle", event="stop")
        recorder = getattr(self, "_record_voice_disconnect", None)
        if callable(recorder):
            recorder(st, reason="manual_stop", event="stop", humans=self._voice_human_count(st))
        self._set_voice_session_mode(st, "disconnected", reason="manual_stop")
        st.paused = False
        self._bump_playback_generation(st, reason="stop")
        await self._stop_player_instance(player, disconnect=True)
        return {"ok": True, "state": st.public()}

    async def cmd_skip(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.last_action = "skip"
        player = st.player
        previous = st.current
        self._push_history(st, previous)
        # Antes de trocar a geração, remova somente um stream/cache comprovadamente
        # contaminado pelo áudio atual. Isso preserva o prefetch correto e evita
        # re-resolver sem necessidade.
        contaminated = self._guard_distinct_next_stream(st, previous)
        keep = set() if contaminated else self._next_resolving_prefetch_keys(st)
        self._bump_playback_generation(st, reason="skip", keep_prefetch_task_keys=keep)
        mixed = getattr(player, "source", None)
        if isinstance(mixed, AgentMixedAudioSource) and mixed.persistent and getattr(player, "is_playing", lambda: False)():
            mixed.stop_music()
        else:
            await self._stop_player_instance(player, disconnect=False)
        st.current = None
        await self._play_next(guild_id)
        return {"ok": True, "state": st.public()}

    async def cmd_previous(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.last_action = "previous"
        explicit = self._track_from_command_payload(body)
        previous = explicit
        if previous is None and st.history:
            previous = st.history.pop()
        elif explicit is not None:
            explicit_key = self._track_key(explicit)
            if explicit_key:
                # Remova a mesma faixa do topo do histórico remoto se ela estiver lá.
                for idx in range(len(st.history) - 1, -1, -1):
                    if self._track_key(st.history[idx]) == explicit_key:
                        del st.history[idx]
                        break
        if previous is None:
            return {"ok": False, "error": "sem música anterior no histórico", "state": st.public()}

        current = self._clone_track(st.current)
        if current is not None:
            st.queue.insert(0, current)
        previous.start_offset_seconds = 0.0
        st.queue.insert(0, previous)
        self._cancel_prefetch_tasks(guild_id)
        self._cancel_idle_disconnect(guild_id)
        player = st.player
        self._bump_playback_generation(st, reason="previous")
        await self._stop_player_instance(player, disconnect=False)
        st.current = None
        st.paused = False
        await self._play_next(guild_id, preserve_current_to_history=False)
        self.log("previous_started", guild_id=guild_id, title=getattr(previous, "title", ""), queue_size=len(st.queue), history_size=len(st.history))
        return {"ok": True, "previous": previous.public(), "state": st.public()}

    @staticmethod
    def _virtual_cursor_bounds(item: AgentTrack) -> tuple[int, int | None]:
        cursor = item.virtual_playlist_cursor if isinstance(item.virtual_playlist_cursor, dict) else {}
        try:
            start = max(0, int(cursor.get("next_offset") or 0))
        except Exception:
            start = 0
        raw_end = cursor.get("block_end_offset")
        if raw_end in (None, ""):
            raw_end = cursor.get("total_tracks")
        if raw_end in (None, ""):
            return start, None
        try:
            return start, max(start, int(raw_end))
        except Exception:
            return start, None

    @staticmethod
    def _virtual_marker_matches(item: AgentTrack, *, instance_id: str, provider: str, source_url: str) -> bool:
        if not item.is_virtual_playlist_marker:
            return False
        cursor = item.virtual_playlist_cursor if isinstance(item.virtual_playlist_cursor, dict) else {}
        marker_instance = str(cursor.get("instance_id") or "").strip()
        if instance_id and marker_instance:
            return marker_instance == instance_id
        return (
            str(cursor.get("provider") or "").strip() == provider
            and str(cursor.get("source_url") or "").strip() == source_url
        )

    @staticmethod
    def _marker_with_range(marker: AgentTrack, start: int, end: int | None, *, preserve_unbounded_end: bool = False) -> AgentTrack | None:
        start = max(0, int(start))
        if end is not None and int(end) <= start:
            return None
        cursor = dict(marker.virtual_playlist_cursor)
        cursor["next_offset"] = start
        cursor["exhausted"] = False
        if end is None or preserve_unbounded_end:
            cursor.pop("block_end_offset", None)
        else:
            cursor["block_end_offset"] = max(start, int(end))
        return replace(marker, virtual_playlist_cursor=cursor, queue_item_id="")

    def _remove_virtual_source_entry(
        self,
        st: GuildMusicState,
        *,
        instance_id: str,
        provider: str,
        source_url: str,
        source_index: int,
    ) -> tuple[bool, int]:
        source_index = max(0, int(source_index))
        for physical_index, marker in enumerate(st.queue):
            if not self._virtual_marker_matches(
                marker, instance_id=instance_id, provider=provider, source_url=source_url
            ):
                continue
            start, end = self._virtual_cursor_bounds(marker)
            if end is None or not (start <= source_index < end):
                continue
            had_bounded_end = marker.virtual_playlist_cursor.get("block_end_offset") not in (None, "")
            replacement: list[AgentTrack] = []
            before = self._marker_with_range(marker, start, source_index)
            if before is not None:
                replacement.append(before)
            after = self._marker_with_range(
                marker,
                source_index + 1,
                end,
                preserve_unbounded_end=not had_bounded_end,
            )
            if after is not None:
                replacement.append(after)
            st.queue[physical_index:physical_index + 1] = replacement
            return True, physical_index
        return False, -1

    def _logical_queue_size(self, st: GuildMusicState) -> int:
        total = 0
        for item in st.queue:
            if item.is_virtual_playlist_marker:
                start, end = self._virtual_cursor_bounds(item)
                if end is not None:
                    total += max(0, end - start)
            else:
                total += 1
        return total

    def _insert_track_at_logical_position(self, st: GuildMusicState, track: AgentTrack, position: int) -> bool:
        total = self._logical_queue_size(st)
        position = int(position)
        if position < 1 or position > total + 1:
            return False
        logical = 1
        for physical_index, item in enumerate(list(st.queue)):
            if not item.is_virtual_playlist_marker:
                if logical == position:
                    st.queue.insert(physical_index, track)
                    return True
                logical += 1
                continue
            start, end = self._virtual_cursor_bounds(item)
            if end is None:
                if logical == position:
                    st.queue.insert(physical_index, track)
                    return True
                continue
            length = max(0, end - start)
            if position <= logical + length - 1:
                offset = max(0, position - logical)
                if offset == 0:
                    st.queue.insert(physical_index, track)
                    return True
                split_source = start + offset
                had_bounded_end = item.virtual_playlist_cursor.get("block_end_offset") not in (None, "")
                left = self._marker_with_range(item, start, split_source)
                right = self._marker_with_range(
                    item, split_source, end, preserve_unbounded_end=not had_bounded_end
                )
                replacement = [part for part in (left, track, right) if part is not None]
                st.queue[physical_index:physical_index + 1] = replacement
                return True
            logical += length
        if position == total + 1:
            st.queue.append(track)
            return True
        return False

    async def _play_selected_queue_track(self, guild_id: int, st: GuildMusicState, selected: AgentTrack) -> None:
        selected.start_offset_seconds = 0.0
        st.queue.insert(0, selected)
        previous = st.current
        self._push_history(st, previous)
        contaminated = self._guard_distinct_next_stream(st, previous)
        keep = set() if contaminated else self._next_resolving_prefetch_keys(st)
        self._bump_playback_generation(st, reason="queue_play_now", keep_prefetch_task_keys=keep)
        player = st.player
        mixed = getattr(player, "source", None)
        if isinstance(mixed, AgentMixedAudioSource) and mixed.persistent and getattr(player, "is_playing", lambda: False)():
            mixed.stop_music()
        else:
            await self._stop_player_instance(player, disconnect=False)
        st.current = None
        st.paused = False
        st.last_action = "queue_play_now"
        await self._play_next(guild_id, preserve_current_to_history=False)

    async def cmd_queue_virtual_action(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        action = str(body.get("virtual_action") or body.get("operation") or "").strip().lower()
        instance_id = str(body.get("instance_id") or "").strip()
        provider = str(body.get("provider") or "").strip()
        source_url = str(body.get("source_url") or "").strip()
        try:
            source_index = max(0, int(body.get("source_index")))
        except Exception:
            return {"ok": False, "error": "índice virtual inválido", "state": st.public()}
        if action not in {"remove", "play_now", "move"}:
            return {"ok": False, "error": "ação virtual desconhecida", "state": st.public()}

        to_position: int | None = None
        if action == "move":
            try:
                to_position = int(body.get("to_position") or body.get("to_pos") or 0)
            except Exception:
                to_position = 0
            total_before = self._logical_queue_size(st)
            if to_position < 1 or to_position > total_before:
                return {"ok": False, "error": "posição de destino inválida", "state": st.public()}

        track_meta = body.get("track") if isinstance(body.get("track"), dict) else {}
        selected = self._agent_track_from_metadata(track_meta, body=body, fallback_query="") if track_meta else None
        if action in {"play_now", "move"} and (
            selected is None or not (selected.query or selected.webpage_url or selected.stream_url)
        ):
            return {"ok": False, "error": "metadata da entrada virtual ausente", "state": st.public()}

        removed, _physical_index = self._remove_virtual_source_entry(
            st,
            instance_id=instance_id,
            provider=provider,
            source_url=source_url,
            source_index=source_index,
        )
        if not removed:
            return {"ok": False, "error": "entrada virtual não está mais nessa posição", "stale": True, "state": st.public()}

        if action == "remove":
            st.last_action = "queue_remove_virtual"
            st.updated_at = time.time()
            self._cancel_prefetch_tasks(guild_id)
            self._schedule_next_queue_prefetch(guild_id, reason="queue_remove_virtual")
            return {"ok": True, "removed": track_meta, "state": st.public()}

        if action == "play_now":
            self._cancel_prefetch_tasks(guild_id)
            await self._play_selected_queue_track(guild_id, st, selected)
            return {"ok": True, "selected": selected.public(), "state": st.public()}

        if action == "move":
            assert to_position is not None
            if not self._insert_track_at_logical_position(st, selected, to_position):
                # A posição já foi validada antes da remoção. Se o layout mudar
                # dentro desta operação, preserve a faixa em vez de perdê-la.
                st.queue.append(selected)
                st.updated_at = time.time()
                return {"ok": False, "error": "fila mudou durante a operação; tente novamente", "stale": True, "state": st.public()}
            self._cancel_prefetch_tasks(guild_id)
            st.last_action = "queue_move_virtual"
            st.updated_at = time.time()
            self._schedule_next_queue_prefetch(guild_id, reason="queue_move_virtual")
            return {"ok": True, "moved": True, "track": selected.public(), "state": st.public()}


    def _physical_track_index_at_logical_position(self, st: GuildMusicState, position: int) -> int | None:
        position = int(position)
        if position < 1:
            return None
        logical = 1
        for physical_index, item in enumerate(st.queue):
            if not item.is_virtual_playlist_marker:
                if logical == position:
                    return physical_index
                logical += 1
                continue
            start, end = self._virtual_cursor_bounds(item)
            if end is None:
                return None
            length = max(0, end - start)
            if logical <= position < logical + length:
                return None
            logical += length
        return None

    def _queue_materialized_indices(self, st: GuildMusicState) -> list[int]:
        """Índices editáveis antes do cursor virtual.

        Itens depois do marker pertencem logicamente ao fim da playlist
        virtual (por exemplo músicas adicionadas manualmente). O controlador
        não deve atravessar o cursor e corromper essa ordem.
        """
        indices: list[int] = []
        for index, item in enumerate(st.queue):
            if item.is_virtual_playlist_marker:
                break
            indices.append(index)
        return indices

    def _queue_materialized_public(self, st: GuildMusicState) -> list[dict[str, Any]]:
        return [st.queue[index].public() for index in self._queue_materialized_indices(st)]

    async def cmd_queue_remove(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        try:
            position = int(body.get("position") or body.get("index") or 0)
        except Exception:
            position = 0
        physical_index = self._physical_track_index_at_logical_position(st, position)
        if physical_index is None:
            return {"ok": False, "error": "posição virtual ou inexistente", "state": st.public()}
        removed = st.queue.pop(physical_index)
        self._cancel_prefetch_tasks(guild_id)
        st.last_action = "queue_remove"
        st.updated_at = time.time()
        self._schedule_next_queue_prefetch(guild_id, reason="queue_remove")
        return {"ok": True, "removed": removed.public(), "state": st.public()}

    async def cmd_queue_move(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        try:
            from_pos = int(body.get("from_position") or body.get("from_pos") or 0)
            to_pos = int(body.get("to_position") or body.get("to_pos") or 0)
        except Exception:
            from_pos = to_pos = 0
        total_before = self._logical_queue_size(st)
        if from_pos < 1 or from_pos > total_before or to_pos < 1 or to_pos > total_before:
            return {"ok": False, "error": "posição fora da fila", "state": st.public()}
        if from_pos == to_pos:
            return {"ok": True, "moved": False, "state": st.public()}
        physical_index = self._physical_track_index_at_logical_position(st, from_pos)
        if physical_index is None:
            return {"ok": False, "error": "origem virtual; use ação virtual", "state": st.public()}
        track = st.queue.pop(physical_index)
        if not self._insert_track_at_logical_position(st, track, to_pos):
            st.queue.insert(min(physical_index, len(st.queue)), track)
            return {"ok": False, "error": "posição de destino inválida", "state": st.public()}
        self._cancel_prefetch_tasks(guild_id)
        st.last_action = "queue_move"
        st.updated_at = time.time()
        self._schedule_next_queue_prefetch(guild_id, reason="queue_move")
        return {"ok": True, "moved": True, "track": track.public(), "state": st.public()}

    async def cmd_queue_clear(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        removed = sum(1 for item in st.queue if not item.is_virtual_playlist_marker)
        st.queue.clear()
        st.virtual_shuffle_active = False
        st.virtual_shuffle_seed = 0
        self._cancel_prefetch_tasks(guild_id)
        self._cancel_audio_preparation(guild_id)
        st.last_action = "queue_clear"
        st.updated_at = time.time()
        return {"ok": True, "removed_count": removed, "state": st.public()}

    async def cmd_queue_play_now(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        try:
            position = int(body.get("position") or body.get("index") or 0)
        except Exception:
            position = 0
        physical_index = self._physical_track_index_at_logical_position(st, position)
        if physical_index is None:
            return {"ok": False, "error": "posição virtual ou inexistente", "state": st.public()}
        selected = st.queue.pop(physical_index)
        await self._play_selected_queue_track(guild_id, st, selected)
        return {"ok": True, "selected": selected.public(), "state": st.public()}

    async def cmd_shuffle(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.last_action = "shuffle"
        self._cancel_prefetch_tasks(guild_id)

        import math as _math
        import random as _random

        markers = [(index, item, *self._virtual_cursor_bounds(item)) for index, item in enumerate(st.queue) if item.is_virtual_playlist_marker]
        unknown_markers = [(index, item) for index, item, _begin, finish in markers if finish is None]
        if unknown_markers:
            # Sem total não existe como permutar globalmente referências que
            # ainda não têm domínio conhecido. Preserve a semântica lazy antiga:
            # embaralhe a janela pronta antes do primeiro marker e marque os
            # refills futuros para shuffle determinístico. Assim o botão segue
            # útil sem inventar posições ou materializar a coleção inteira.
            first_marker_index = unknown_markers[0][0]
            ready = list(st.queue[:first_marker_index])
            seed = int(time.time_ns() & 0x7FFFFFFF)
            if len(ready) > 1:
                _random.Random(seed).shuffle(ready)
                st.queue[:first_marker_index] = ready
            st.virtual_shuffle_active = True
            st.virtual_shuffle_seed = seed
            st.shuffle = False
            st.updated_at = time.time()
            self._schedule_next_queue_prefetch(guild_id, reason="shuffle_virtual_unknown_total")
            self.log(
                "queue_virtual_shuffle_fallback",
                guild_id=guild_id,
                queue_size=len(ready),
                virtual_markers=len(markers),
                reason="unknown_total",
                seed=seed,
            )
            return {
                "ok": True,
                "shuffled": True,
                "enabled": False,
                "virtual": True,
                "queue_size": self._logical_queue_size(st),
                "shuffle_mode": "windowed_unknown_total",
                "state": st.public(),
            }

        virtual_total = 0
        for item in st.queue:
            if item.is_virtual_playlist_marker:
                begin, finish = self._virtual_cursor_bounds(item)
                if finish is not None:
                    virtual_total += max(0, finish - begin)
        # Mantenha no máximo ~128 blocos virtuais mesmo com fila de 10 mil,
        # mas use a janela normal (25) em playlists menores. Cada bloco continua
        # metadata-only e é materializado JIT quando chega perto da reprodução.
        block_size = max(25, int(_math.ceil(virtual_total / 128.0))) if virtual_total else 25
        seed = int(time.time_ns() & 0x7FFFFFFF) or 1
        units: list[AgentTrack] = []
        for item in st.queue:
            if not item.is_virtual_playlist_marker:
                units.append(item)
                continue
            begin, finish = self._virtual_cursor_bounds(item)
            if finish is None or finish <= begin:
                units.append(item)
                continue
            cursor_was_bounded = item.virtual_playlist_cursor.get("block_end_offset") not in (None, "")
            pos = begin
            while pos < finish:
                block_end = min(finish, pos + block_size)
                block = self._marker_with_range(item, pos, block_end)
                if block is not None:
                    import zlib as _zlib
                    identity = "|".join((
                        str(block.virtual_playlist_cursor.get("instance_id") or ""),
                        str(block.virtual_playlist_cursor.get("provider") or ""),
                        str(block.virtual_playlist_cursor.get("source_url") or ""),
                        str(pos),
                        str(block_end),
                    )).encode("utf-8", "ignore")
                    block_seed = (seed ^ _zlib.crc32(identity) ^ pos ^ block_end) & 0x7FFFFFFF
                    block.virtual_playlist_cursor["shuffle_seed"] = block_seed or 1
                    units.append(block)
                pos = block_end
            # ``cursor_was_bounded`` só documenta que um shuffle anterior já
            # havia segmentado a coleção; os novos blocos substituem esse plano.
            _ = cursor_was_bounded

        logical_count = 0
        for item in units:
            if not item.is_virtual_playlist_marker:
                logical_count += 1
                continue
            begin, finish = self._virtual_cursor_bounds(item)
            logical_count += max(1, finish - begin) if finish is not None else 1
        if logical_count <= 1:
            st.shuffle = False
            st.virtual_shuffle_active = False
            st.virtual_shuffle_seed = 0
            st.updated_at = time.time()
            return {"ok": True, "shuffled": False, "enabled": False, "queue_size": logical_count, "state": st.public()}

        _random.Random(seed).shuffle(units)
        st.queue[:] = units
        # Shuffle continua sendo ação única. A ordem aleatória agora está
        # materializada como faixas prontas + blocos virtuais reordenados; não
        # existe modo persistente que possa desligar no fim da primeira playlist.
        st.shuffle = False
        st.virtual_shuffle_active = False
        st.virtual_shuffle_seed = 0
        st.updated_at = time.time()
        self._schedule_next_queue_prefetch(guild_id, reason="shuffle_global_virtual")
        self.log(
            "queue_logical_shuffled",
            guild_id=guild_id,
            logical_queue_size=logical_count,
            physical_units=len(units),
            virtual_items=virtual_total,
            block_size=block_size,
            seed=seed,
        )
        return {
            "ok": True,
            "shuffled": True,
            "enabled": False,
            "virtual": bool(virtual_total),
            "queue_size": logical_count,
            "shuffle_block_size": block_size,
            "state": st.public(),
        }

    async def cmd_loop(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        requested = str(body.get("mode") or body.get("loop_mode") or "").strip().lower()
        modes = ("off", "one", "all")
        if requested in modes:
            st.loop_mode = requested
        else:
            current = str(getattr(st, "loop_mode", "off") or "off").strip().lower()
            if current == "off":
                st.loop_mode = "one"
            elif current == "one":
                st.loop_mode = "all"
            else:
                st.loop_mode = "off"
        st.last_action = "loop"
        st.updated_at = time.time()
        self.log("loop_mode_changed", guild_id=guild_id, mode=st.loop_mode)
        return {"ok": True, "mode": st.loop_mode, "loop_mode": st.loop_mode, "state": st.public()}

    async def cmd_seek(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        raw = body.get("position_seconds")
        if raw is None:
            raw = body.get("seconds")
        if raw is None and body.get("position_ms") is not None:
            raw = float(body.get("position_ms") or 0) / 1000.0
        try:
            target = max(0.0, float(raw or 0.0))
        except Exception:
            return {"ok": False, "error": "tempo inválido", "state": self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id)).public()}
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        track = st.current
        if track is None:
            return {"ok": False, "error": "não há música tocando agora", "state": st.public()}
        if track.duration is not None:
            try:
                duration = float(track.duration)
                if duration > 0 and target > duration:
                    return {"ok": False, "error": f"momento passa da duração da música ({int(duration)}s)", "state": st.public()}
            except Exception:
                pass
        st.last_action = "seek"
        player = st.player
        if not track.stream_url:
            try:
                track = await self.resolve_track(track.query or track.webpage_url or track.title, track_meta=track.public(), body=body)
                st.current = track
            except Exception as exc:
                return {"ok": False, "error": f"não consegui preparar seek: {short_text(exc, 180)}", "state": st.public()}
        track.start_offset_seconds = target
        st.current = track
        st.paused = False
        st.paused_monotonic = 0.0
        # O seek invalida callbacks e prefetches calculados para a posição antiga.
        self._bump_playback_generation(st, reason="seek")
        if player is not None:
            with contextlib.suppress(Exception):
                if getattr(player, "is_playing", lambda: False)() or getattr(player, "is_paused", lambda: False)():
                    player.stop()
        try:
            await self._play_direct_voice(guild_id, track)
        except Exception as exc:
            self._set_status(st, "failed", event="seek_failed", error=short_text(exc, 260))
            return {"ok": False, "error": st.last_error or "seek falhou", "state": st.public()}
        self._set_status(st, "playing", event="seek")
        return {"ok": True, "position_seconds": target, "state": st.public()}

    async def _apply_player_volume(self, st: GuildMusicState, volume: int) -> bool:
        volume = max(0, min(150, int(volume)))
        player = st.player
        if player is None:
            st.volume_percent = volume
            return False
        applied = False
        setter = getattr(player, "set_volume", None)
        if callable(setter):
            maybe = setter(volume)
            if asyncio.iscoroutine(maybe):
                await maybe
            applied = True
        # discord.VoiceClient exposes the active source; when it is a
        # PCMVolumeTransformer, changing source.volume is immediate.
        source = getattr(player, "source", None)
        if source is not None and hasattr(source, "set_music_volume"):
            with contextlib.suppress(Exception):
                source.set_music_volume(max(0.0, min(1.5, volume / 100.0)))
                applied = True
        elif source is not None and hasattr(source, "volume"):
            with contextlib.suppress(Exception):
                source.volume = max(0.0, min(1.5, volume / 100.0))
                applied = True
        st.volume_percent = volume
        st.updated_at = time.time()
        return applied

    async def cmd_volume(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        volume = max(0, min(150, int(float(body.get("volume") or body.get("volume_percent") or self.default_volume_percent))))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.normal_volume_percent = volume
        if not st.ducked:
            await self._apply_player_volume(st, volume)
        return {"ok": True, "volume": st.volume_percent, "normal_volume": st.normal_volume_percent, "ducked": st.ducked, "state": st.public()}

    async def cmd_audio_effect(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        effect = str(body.get("effect") or "").strip().lower()
        enabled = body.get("enabled")
        if effect not in {"bassboost", "nightcore"} or not isinstance(enabled, bool):
            return {"ok": False, "error": "efeito ou estado inválido", "state": st.public()}

        async with st.effects_lock:
            expected = body.get("expected_revision")
            if expected is not None and str(expected) != str(st.effects_revision):
                return {"ok": False, "error": "os efeitos mudaram; tente novamente", "state": st.public()}
            previous = (st.bassboost, st.nightcore)
            desired = (enabled if effect == "bassboost" else st.bassboost,
                       enabled if effect == "nightcore" else st.nightcore)
            if desired == previous:
                return {"ok": True, "state": st.public()}
            if desired[1] and self._ffmpeg_has_custom_audio_filter(self.ffmpeg_options):
                return {"ok": False, "error": "filtro FFmpeg personalizado incompatível com os efeitos", "state": st.public()}
            if effect == "bassboost" and not self.direct_pcm_volume_enabled:
                return {"ok": False, "error": "Bassboost requer o mixer PCM do player", "state": st.public()}

            track = st.current
            if track is not None and st.status in {"starting", "resolving", "preparing"}:
                return {"ok": False, "error": "aguarde a música começar antes de alterar os efeitos", "state": st.public()}
            player = st.player
            mixer = getattr(player, "source", None)
            playing = bool(player and (getattr(player, "is_playing", lambda: False)() or
                                       getattr(player, "is_paused", lambda: False)()))
            if track is not None and playing:
                if track.is_live and effect == "nightcore":
                    return {"ok": False, "error": "não é possível trocar efeitos durante uma transmissão ao vivo", "state": st.public()}
                if not isinstance(mixer, AgentMixedAudioSource) or not mixer.persistent:
                    return {"ok": False, "error": "o player atual não permite trocar efeitos durante a música", "state": st.public()}
                if effect == "bassboost":
                    # Ajuste local ao mixer: não reinicia FFmpeg, TTS ou relógio.
                    mixer.set_bassboost(enabled)
                    st.bassboost = enabled
                    st.effects_revision += 1
                    st.last_action = "audio_effect"
                    self._set_status(st, "paused" if st.paused else "playing", event="audio_effect")
                    return {"ok": True, "state": st.public()}
                if not track.stream_url:
                    return {"ok": False, "error": "não há stream para retomar a música", "state": st.public()}
                token = st.playback_token
                self._cancel_audio_preparation(guild_id)
                offset = st.source_position_seconds()
                selected = replace(track, start_offset_seconds=offset)
                candidate = None
                swapped = False
                try:
                    candidate = self._create_pcm_source(selected, effects=desired)
                    if not isinstance(candidate, BufferedPCMSource):
                        candidate = BufferedPCMSource(candidate, max_frames=10,
                                                      stall_seconds=self.pcm_buffer_stall_seconds)
                    await candidate.wait_ready(timeout=min(4.0, self.prepare_timeout), min_frames=1)
                    if (st.current is not track or st.playback_token != token or st.player is not player or
                            getattr(player, "source", None) is not mixer or mixer.music_ended):
                        return {"ok": False, "error": "a música mudou durante a troca do efeito", "state": st.public()}

                    loop = self._loop or asyncio.get_running_loop()
                    next_token = token + 1

                    def on_music_end(error: Exception | None, metrics: dict[str, Any]) -> None:
                        if not loop.is_closed():
                            asyncio.run_coroutine_threadsafe(
                                self._direct_after(guild_id, error, next_token, audio_metrics=metrics), loop,
                            )

                    # O mixer mantém voz/TTS. A fonte anterior continua tocando
                    # até a nova produzir PCM, e só então é substituída.
                    mixer.replace_music_source(candidate, volume=st.volume_percent / 100.0,
                                               on_music_end=on_music_end, bassboost=desired[0])
                    candidate = None  # propriedade transferida ao mixer
                    swapped = True
                except Exception as exc:
                    self.log("audio_effect_failed", guild_id=guild_id, effect=effect, error=short_text(exc, 180))
                    return {"ok": False, "error": f"não consegui preparar o efeito: {short_text(exc, 150)}", "state": st.public()}
                finally:
                    if candidate is not None:
                        with contextlib.suppress(Exception):
                            candidate.cleanup()
                    if not swapped and st.playback_token == token and st.current is track and not st.paused:
                        self._schedule_audio_prepare(guild_id)

                # A partir daqui a nova fonte já foi entregue ao mixer; erros
                # de telemetria/prefetch não podem reportar uma troca concluída
                # como falha nem tentar restaurar uma fonte já encerrada.
                self._bump_playback_generation(st, reason="audio_effect")
                st.bassboost, st.nightcore = desired
                st.effects_revision += 1
                track.start_offset_seconds = offset
                now = time.monotonic()
                st.started_monotonic = now
                st.paused_monotonic = now if st.paused else 0.0
                st.last_action = "audio_effect"
                self._set_status(st, "paused" if st.paused else "playing", event="audio_effect")
                if not st.paused:
                    with contextlib.suppress(Exception):
                        self._schedule_next_queue_prefetch(guild_id, reason="audio_effect")
                with contextlib.suppress(Exception):
                    self.log("audio_effect_changed", guild_id=guild_id, effect=effect,
                             enabled=enabled, bassboost=st.bassboost, nightcore=st.nightcore)
                return {"ok": True, "state": st.public()}

            self._cancel_audio_preparation(guild_id)
            st.bassboost, st.nightcore = desired
            st.effects_revision += 1
            st.last_action = "audio_effect"
            st.updated_at = time.time()
            return {"ok": True, "state": st.public()}

    async def cmd_duck(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        requested = body.get("volume") if body.get("volume") is not None else body.get("volume_percent")
        if requested is None:
            requested = self.duck_volume_percent
        duck_volume = max(0, min(100, int(float(requested))))
        if not st.normal_volume_percent:
            st.normal_volume_percent = max(0, min(150, int(st.volume_percent or self.default_volume_percent)))
        st.ducked = True
        await self._apply_player_volume(st, duck_volume)
        self.log("tts_duck", guild_id=guild_id, volume=duck_volume, normal=st.normal_volume_percent)
        return {"ok": True, "ducked": True, "volume": st.volume_percent, "normal_volume": st.normal_volume_percent, "state": st.public()}

    async def cmd_unduck(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        restore = max(0, min(150, int(float(body.get("volume") or body.get("volume_percent") or st.normal_volume_percent or self.default_volume_percent))))
        st.ducked = False
        st.normal_volume_percent = restore
        await self._apply_player_volume(st, restore)
        self.log("tts_restore", guild_id=guild_id, volume=restore)
        return {"ok": True, "ducked": False, "volume": st.volume_percent, "normal_volume": st.normal_volume_percent, "state": st.public()}

    async def _play_next(self, guild_id: int, *, preserve_current_to_history: bool = True) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        self._cancel_idle_disconnect(guild_id)
        if not st.queue:
            if st.current is not None:
                self._push_history(st, st.current)
            st.current = None
            st.bassboost = False
            st.nightcore = False
            st.effects_revision += 1
            st.paused = False
            self._set_status(st, "idle", event="queue_empty")
            self._finish_mixer_when_idle(st)
            self._schedule_idle_disconnect(guild_id)
            await self._refresh_voice_presence_policy(guild_id, source="queue_empty")
            return
        if st.queue[0].is_virtual_playlist_marker:
            # Não remova o marker: ele é o ponto exato onde a próxima janela
            # deve entrar. O monitor já existente da VPS verá ``waiting`` e
            # solicitará o refill sem introduzir um polling adicional.
            if preserve_current_to_history and st.current is not None:
                self._push_history(st, st.current)
            st.current = None
            st.paused = False
            self._set_status(st, "queued", event="playlist_refill_needed")
            self._finish_mixer_when_idle(st)
            self.log(
                "playlist_refill_needed",
                guild_id=guild_id,
                offset=st.queue[0].virtual_playlist_cursor.get("next_offset", 0),
            )
            return
        next_track = st.queue.pop(0)
        if preserve_current_to_history and st.current is not None:
            # A faixa atual precisa virar histórico sempre que deixa de ser a
            # faixa ativa por troca direta, fallback, playlist ou transição
            # remota. O comando previous trata esse caso separadamente porque
            # recoloca a faixa atual na frente da fila.
            if self._track_key(st.current) != self._track_key(next_track):
                self._push_history(st, st.current)
        st.current = next_track
        repaired = st._repair_current_queue_alias()
        if repaired:
            self.log(
                "queue_current_duplicate_repaired",
                guild_id=guild_id,
                queue_item_id=str(getattr(next_track, "queue_item_id", "") or "")[:24],
                removed=repaired,
                title=getattr(next_track, "title", ""),
                queue_size=len(st.queue),
            )
        request_token = int(getattr(st, "playback_token", 0) or 0)
        current_ref = st.current
        st.paused = False
        st.paused_monotonic = 0.0
        st.transport = ""
        st.ducked = False
        st.normal_volume_percent = max(0, min(150, int(st.normal_volume_percent or self.default_volume_percent)))
        st.volume_percent = st.normal_volume_percent
        st.play_attempt_sequence += 1
        play_attempt = int(st.play_attempt_sequence)
        self._set_status(st, "preparing", event="play_preparing")
        self.log(
            "track_loading",
            guild_id=guild_id,
            attempt=play_attempt,
            title=getattr(st.current, "title", ""),
            source=getattr(st.current, "source", ""),
            lazy=not bool(getattr(st.current, "stream_url", "")),
            queue_size=len(st.queue),
            playback_token=int(st.playback_token),
        )
        voice_prepare_task: asyncio.Task | None = None
        prepared_voice: tuple[Any, bool] | None = None
        failure_phase = "prepare"
        try:
            if st.current and self._track_stream_needs_refresh(st.current):
                self.log(
                    "stale_prefetch_refresh",
                    guild_id=guild_id,
                    title=st.current.title,
                    age_seconds=round(time.monotonic() - float(st.current.stream_resolved_monotonic or 0.0), 1),
                )
                self._invalidate_track_stream_cache(st.current)
                st.current.stream_url = ""
                st.current.transport_hint = "metadata-lazy"
            if st.current and not st.current.stream_url:
                # A conexão do Discord e o yt-dlp são independentes. Faça os
                # dois em paralelo para que o tempo de handshake de voz não
                # seja somado ao tempo de resolução da faixa.
                if self.direct_audio_enabled and st.voice_channel_id:
                    failure_phase = "voice_preconnect"
                    voice_prepare_task = asyncio.create_task(
                        asyncio.wait_for(
                            self._ensure_direct_voice_client(guild_id),
                            timeout=max(5.0, self.prepare_timeout),
                        )
                    )
                    self.log("voice_preconnect_started", guild_id=guild_id, channel=st.voice_channel_id, transport="direct")
                failure_phase = "resolve"
                started = time.time()
                meta = st.current.public()
                query = self._query_from_track_meta(meta, fallback_query=st.current.query or st.current.title)
                body = {
                    "guild_id": guild_id,
                    "voice_channel_id": st.voice_channel_id,
                    "text_channel_id": st.text_channel_id,
                    "requester_id": st.current.requester_id,
                    "requester_name": st.current.requester_name,
                    "query": query,
                    "track": meta,
                }
                resolve_task = asyncio.create_task(self.resolve_track(query, track_meta=meta, body=body))
                self._active_resolve_tasks[guild_id] = resolve_task
                try:
                    resolved_current = await resolve_task
                except asyncio.CancelledError:
                    stale = (
                        int(getattr(st, "playback_token", 0) or 0) != request_token
                        or st.current is not current_ref
                        or str(getattr(st, "last_action", "") or "").lower() in {"stop", "skip", "previous"}
                    )
                    await self._discard_prepared_voice_task(guild_id, voice_prepare_task)
                    if stale:
                        self.log("lazy_resolve_cancelled", guild_id=guild_id, title=getattr(current_ref, "title", ""), reason="playback_changed")
                        return
                    raise
                finally:
                    remove_owned_task(self._active_resolve_tasks, guild_id, resolve_task)
                if int(getattr(st, "playback_token", 0) or 0) != request_token or st.current is not current_ref:
                    self.log("lazy_resolve_ignored", guild_id=guild_id, title=getattr(resolved_current, "title", ""), reason="stale_generation")
                    await self._discard_prepared_voice_task(guild_id, voice_prepare_task)
                    return
                st.current = resolved_current
                current_ref = st.current
                self.log("lazy_resolve_done", guild_id=guild_id, elapsed_ms=round((time.time() - started) * 1000.0, 1), title=getattr(st.current, "title", ""))
            if int(getattr(st, "playback_token", 0) or 0) != request_token or st.current is not current_ref:
                self.log("play_start_ignored", guild_id=guild_id, reason="stale_generation")
                await self._discard_prepared_voice_task(guild_id, voice_prepare_task)
                return
            if not self._should_use_direct_voice(st.current):
                raise RuntimeError("playback direto do Music Agent indisponível para a faixa resolvida")
            if voice_prepare_task is not None:
                failure_phase = "voice_preconnect"
                prepared_voice = await voice_prepare_task
                if st.playback_token != request_token or st.current is not current_ref:
                    await self._discard_prepared_voice_task(guild_id, voice_prepare_task)
                    return
                voice_prepare_task = None
            failure_phase = "playback_start"
            play_coro = (
                self._play_direct_voice(guild_id, st.current, prepared_voice=prepared_voice)
                if prepared_voice is not None
                else self._play_direct_voice(guild_id, st.current)
            )
            await asyncio.wait_for(play_coro, timeout=max(5.0, self.prepare_timeout))
        except Exception as exc:
            await self._discard_prepared_voice_task(guild_id, voice_prepare_task)
            if st.current is not current_ref:
                self.log("play_failure_superseded", guild_id=guild_id, phase=failure_phase)
                return
            if prepared_voice is not None and st.player is not prepared_voice[0]:
                # Se o preconnect criou uma sessão que nem chegou a ser entregue
                # ao player, não deixe uma conexão órfã após falha de resolução.
                voice_client, created = prepared_voice
                if created and self._voice_client_is_connected(voice_client):
                    await self._disconnect_voice_client_bounded(
                        voice_client,
                        guild_id=guild_id,
                        reason="failed_preconnect_cleanup",
                    )
            failed_track = st.current
            category, recoverable = self._classify_play_failure(exc, phase=failure_phase)
            if category != "voice_transport":
                self._invalidate_track_stream_cache(failed_track)
            self._set_status(st, "failed", event="play_failed", error=f"{type(exc).__name__}: {short_text(exc, 260)}")
            st.last_error_category = category
            st.last_error_phase = failure_phase
            st.consecutive_start_failures += 1
            self.log(
                "play_failed",
                guild_id=guild_id,
                attempt=play_attempt,
                transport=st.transport or "unknown",
                category=category,
                phase=failure_phase,
                recoverable=recoverable,
                failure_streak=st.consecutive_start_failures,
                queue_size=len(st.queue),
                error=st.last_error,
                title=getattr(failed_track, "title", ""),
            )
            if category == "voice_transport" and failed_track is not None:
                max_voice_retries = max(0, min(3, env_int("MUSIC_AGENT_VOICE_START_RETRIES", 1)))
                attempts = int(getattr(failed_track, "voice_recovery_attempts", 0) or 0)
                if attempts < max_voice_retries:
                    failed_track.voice_recovery_attempts = attempts + 1
                    st.current = None
                    st.paused = False
                    st.queue.insert(0, failed_track)
                    self._set_status(st, "queued", event="voice_transport_retry")
                    self.log(
                        "voice_transport_retry",
                        guild_id=guild_id,
                        attempt=play_attempt,
                        retry=failed_track.voice_recovery_attempts,
                        max_retries=max_voice_retries,
                        title=getattr(failed_track, "title", ""),
                        queue_size=len(st.queue),
                    )
                    await asyncio.sleep(0)
                    await self._play_next(guild_id, preserve_current_to_history=False)
                    return
                # Falha de infraestrutura não pode consumir a música nem iniciar
                # uma cascata pela playlist. Mantenha current + fila para retry.
                self.log(
                    "voice_transport_queue_preserved",
                    guild_id=guild_id,
                    attempt=play_attempt,
                    title=getattr(failed_track, "title", ""),
                    queue_size=len(st.queue),
                    retries=attempts,
                )
                return

            max_consecutive = max(1, min(25, env_int("MUSIC_AGENT_MAX_CONSECUTIVE_START_FAILURES", 5)))
            if st.consecutive_start_failures >= max_consecutive:
                # Evita consumir milhares de itens/recursão profunda quando uma
                # dependência externa quebra de forma sistêmica.
                self.log(
                    "track_failure_circuit_open",
                    guild_id=guild_id,
                    category=category,
                    failure_streak=st.consecutive_start_failures,
                    max_failures=max_consecutive,
                    title=getattr(failed_track, "title", ""),
                    queue_size=len(st.queue),
                )
                return
            if st.queue:
                # Uma faixa quebrada não deve derrubar uma playlist/fila inteira.
                # Ela não entra no histórico porque nunca chegou a tocar; avance
                # para a próxima faixa (ou para o cursor virtual) imediatamente.
                st.current = None
                st.paused = False
                self.log(
                    "track_failed_skipped",
                    guild_id=guild_id,
                    category=category,
                    phase=failure_phase,
                    failure_streak=st.consecutive_start_failures,
                    title=getattr(failed_track, "title", ""),
                    queue_size=len(st.queue),
                )
                await asyncio.sleep(0)
                await self._play_next(guild_id, preserve_current_to_history=False)
            else:
                self._finish_mixer_when_idle(st)

    @staticmethod
    def _finish_mixer_when_idle(st: GuildMusicState) -> None:
        source = getattr(st.player, "source", None)
        if isinstance(source, AgentMixedAudioSource) and source.persistent:
            source.finish_when_idle()

    def _should_use_direct_voice(self, track: AgentTrack) -> bool:
        return bool(
            self.direct_audio_enabled
            and str(track.stream_url or "").startswith(("http://", "https://"))
        )

    async def _resolve_guild_and_channel(self, guild_id: int, voice_channel_id: int) -> tuple[Any, Any]:
        # READY pode chegar antes dos GUILD_CREATE que preenchem o cache. Na primeira
        # reprodução após start/reconnect isso criava uma corrida: o agente já estava
        # saudável, mas get_guild() ainda retornava None e o usuário precisava repetir
        # o comando. Aguarde brevemente o alvo aparecer no cache em vez de falhar cedo.
        wait_seconds = max(0.0, env_float("MUSIC_AGENT_GUILD_CACHE_WAIT_SECONDS", 3.0))
        poll_seconds = max(0.02, min(0.25, env_float("MUSIC_AGENT_GUILD_CACHE_POLL_SECONDS", 0.08)))
        started = time.monotonic()
        deadline = started + wait_seconds
        wait_until_ready = getattr(self.client, "wait_until_ready", None)
        if callable(wait_until_ready) and not bool(getattr(self.client, "is_ready", lambda: True)()):
            try:
                await asyncio.wait_for(wait_until_ready(), timeout=max(0.1, min(wait_seconds or 0.1, 2.0)))
            except (asyncio.TimeoutError, TimeoutError):
                pass

        logged_wait = False
        guild = None
        channel = None
        while True:
            guild = self.client.get_guild(guild_id)
            if guild is not None:
                channel = guild.get_channel(voice_channel_id) or self.client.get_channel(voice_channel_id)
                if channel is not None:
                    if logged_wait:
                        self.log(
                            "voice_target_cache_recovered",
                            guild_id=guild_id,
                            channel=voice_channel_id,
                            elapsed_ms=round((time.monotonic() - started) * 1000.0, 1),
                        )
                    return guild, channel
            now = time.monotonic()
            if now >= deadline:
                break
            if not logged_wait:
                logged_wait = True
                self.log(
                    "voice_target_cache_wait",
                    guild_id=guild_id,
                    channel=voice_channel_id,
                    guild_cached=bool(guild is not None),
                )
            await asyncio.sleep(min(poll_seconds, max(0.0, deadline - now)))

        if guild is None:
            raise RuntimeError(f"guild {guild_id} não encontrada no player remoto após {wait_seconds:.1f}s")
        raise RuntimeError(f"canal de voz {voice_channel_id} não encontrado após {wait_seconds:.1f}s")

    async def _ensure_direct_voice_client(self, guild_id: int) -> tuple[Any, bool]:
        """Prepare a sessão de voz e diga se esta chamada criou a conexão."""
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        requested_channel_id = int(st.voice_channel_id or 0)
        lock_started = time.monotonic()
        async with self._registry_lock(self._voice_connect_locks, self._voice_connect_lock_users, guild_id):
            waited_ms = max(0.0, (time.monotonic() - lock_started) * 1000.0)
            if waited_ms >= 5.0:
                self.log(
                    "voice_connect_serialized",
                    guild_id=guild_id,
                    channel=requested_channel_id,
                    waited_ms=round(waited_ms, 1),
                )
            # O canal pode ter mudado enquanto aguardávamos o lock.
            requested_channel_id = int(st.voice_channel_id or requested_channel_id)
            guild, channel = await self._resolve_guild_and_channel(guild_id, requested_channel_id)
            existing = self._registered_voice_client_for_guild(guild_id, guild)

            if existing is not None and not getattr(existing, "is_connected", lambda: False)():
                grace = max(0.0, min(2.0, env_float("MUSIC_AGENT_VOICE_RECONNECT_GRACE_SECONDS", 0.6)))
                if grace > 0:
                    reconnect_started = time.monotonic()
                    deadline = time.monotonic() + grace
                    while time.monotonic() < deadline:
                        await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                        current = self._registered_voice_client_for_guild(guild_id, guild)
                        if current is not None and getattr(current, "is_connected", lambda: False)():
                            existing = current
                            self.log(
                                "voice_existing_recovered",
                                guild_id=guild_id,
                                channel=getattr(getattr(existing, "channel", None), "id", None),
                                waited_ms=round((time.monotonic() - reconnect_started) * 1000.0, 1),
                            )
                            break
                if not getattr(existing, "is_connected", lambda: False)():
                    self.log(
                        "voice_stale_client_cleanup",
                        guild_id=guild_id,
                        channel=getattr(getattr(existing, "channel", None), "id", None),
                    )
                    await self._disconnect_voice_client_bounded(
                        existing,
                        guild_id=guild_id,
                        reason="stale_client",
                    )
                    existing = self._registered_voice_client_for_guild(guild_id, guild)

            if existing is None or not getattr(existing, "is_connected", lambda: False)():
                self.log("voice_connecting", guild_id=guild_id, channel=requested_channel_id, transport="direct")
                connect_timeout = self._voice_operation_timeout_seconds()
                try:
                    voice_client = await asyncio.wait_for(
                        channel.connect(self_deaf=True),
                        timeout=connect_timeout,
                    )
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    recovered = self._registered_voice_client_for_guild(guild_id, guild)
                    if recovered is not None and self._voice_client_is_connected(recovered):
                        self.log(
                            "voice_connect_timeout_recovered",
                            guild_id=guild_id,
                            channel=getattr(getattr(recovered, "channel", None), "id", None),
                            timeout_seconds=round(connect_timeout, 2),
                        )
                        voice_client = recovered
                    else:
                        if recovered is not None:
                            await self._disconnect_voice_client_bounded(
                                recovered,
                                guild_id=guild_id,
                                reason="connect_timeout_cleanup",
                            )
                        raise VoiceSessionError(
                            f"timeout ao conectar voz após {connect_timeout:.1f}s"
                        ) from exc
                except Exception as exc:
                    if not self._is_already_connected_voice_error(exc):
                        raise VoiceSessionError(f"falha ao conectar voz: {type(exc).__name__}: {short_text(exc, 220)}") from exc
                    # discord.py pode registrar o VoiceClient um instante antes
                    # de a coroutine concorrente devolver. Releia o registro em
                    # vez de tratar isso como falha da música.
                    recovered = self._registered_voice_client_for_guild(guild_id, guild)
                    if recovered is not None:
                        deadline = time.monotonic() + max(0.1, min(1.5, env_float("MUSIC_AGENT_VOICE_RACE_RECOVERY_SECONDS", 0.8)))
                        while time.monotonic() < deadline and not getattr(recovered, "is_connected", lambda: False)():
                            await asyncio.sleep(0.04)
                            recovered = self._registered_voice_client_for_guild(guild_id, guild) or recovered
                    if recovered is not None and getattr(recovered, "is_connected", lambda: False)():
                        self.log(
                            "voice_connect_race_recovered",
                            guild_id=guild_id,
                            channel=getattr(getattr(recovered, "channel", None), "id", None),
                            error=short_text(exc, 160),
                        )
                        voice_client = recovered
                        current_channel_id = getattr(getattr(voice_client, "channel", None), "id", None)
                        if current_channel_id != requested_channel_id:
                            try:
                                await asyncio.wait_for(
                                    voice_client.move_to(channel),
                                    timeout=self._voice_operation_timeout_seconds(),
                                )
                            except Exception as move_exc:
                                self.log(
                                    "voice_move_failed_cleanup",
                                    guild_id=guild_id,
                                    channel=requested_channel_id,
                                    from_channel=current_channel_id,
                                    phase="race_recovery",
                                    error=f"{type(move_exc).__name__}: {short_text(move_exc, 180)}",
                                )
                                await self._disconnect_voice_client_bounded(
                                    voice_client, guild_id=guild_id, reason="move_failed_after_race"
                                )
                                raise VoiceSessionError(f"falha ao mover voz após corrida: {type(move_exc).__name__}: {short_text(move_exc, 220)}") from move_exc
                        return voice_client, False
                    raise VoiceSessionError(f"corrida de conexão de voz não recuperada: {type(exc).__name__}: {short_text(exc, 220)}") from exc
                self.log("voice_connected", guild_id=guild_id, channel=requested_channel_id, transport="direct", reused=False)
                return voice_client, True

            voice_client = existing
            current_channel_id = getattr(getattr(voice_client, "channel", None), "id", None)
            if current_channel_id != requested_channel_id:
                self.log("voice_moving", guild_id=guild_id, channel=requested_channel_id, from_channel=current_channel_id, transport="direct")
                try:
                    await asyncio.wait_for(
                        voice_client.move_to(channel),
                        timeout=self._voice_operation_timeout_seconds(),
                    )
                except Exception as exc:
                    self.log(
                        "voice_move_failed_cleanup",
                        guild_id=guild_id,
                        channel=requested_channel_id,
                        from_channel=current_channel_id,
                        phase="move",
                        error=f"{type(exc).__name__}: {short_text(exc, 180)}",
                    )
                    # Não reutilize indefinidamente uma sessão que falhou ao
                    # mover. Limpe o registro e deixe o retry seguinte conectar
                    # do zero ao canal solicitado.
                    await self._disconnect_voice_client_bounded(
                        voice_client, guild_id=guild_id, reason="move_failed"
                    )
                    raise VoiceSessionError(f"falha ao mover sessão de voz: {type(exc).__name__}: {short_text(exc, 220)}") from exc
            else:
                self.log("voice_reused", guild_id=guild_id, channel=requested_channel_id, transport="direct")
            return voice_client, False

    async def _discard_prepared_voice_task(self, guild_id: int, task: asyncio.Task | None) -> None:
        """Cancele um preconnect obsoleto e desfaça apenas conexões criadas por ele."""
        if task is None:
            return
        if not task.done():
            task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        prepared = result[0] if result else None
        if isinstance(prepared, tuple) and len(prepared) == 2:
            voice_client, created = prepared
            st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
            if created and st.player is not voice_client:
                if self._voice_client_is_connected(voice_client):
                    await self._disconnect_voice_client_bounded(
                        voice_client,
                        guild_id=guild_id,
                        reason="discarded_preconnect",
                    )
                self.log("voice_preconnect_discarded", guild_id=guild_id, transport="direct")

    async def _confirm_direct_playback(self, voice_client: Any, source: Any, *, max_delay: float) -> float:
        """Confirme playback assim que o primeiro frame sair, sem atraso fixo."""
        max_delay = max(0.05, float(max_delay or 0.35))
        poll = max(0.01, min(0.10, env_float("MUSIC_AGENT_DIRECT_CONFIRM_POLL_SECONDS", 0.04)))
        started = time.monotonic()
        deadline = started + max_delay
        tracks_first_frame = hasattr(source, "first_frame_ms")
        while True:
            if not getattr(voice_client, "is_connected", lambda: False)():
                raise VoiceSessionError("conectei no canal, mas a voz caiu antes do áudio")
            playing = bool(
                getattr(voice_client, "is_playing", lambda: False)()
                or getattr(voice_client, "is_paused", lambda: False)()
            )
            if playing and (not tracks_first_frame or getattr(source, "first_frame_ms", None) is not None):
                return max(0.0, time.monotonic() - started)
            now = time.monotonic()
            if now >= deadline:
                break
            await asyncio.sleep(min(poll, max(0.0, deadline - now)))
        if not getattr(voice_client, "is_connected", lambda: False)():
            raise VoiceSessionError("conectei no canal, mas a voz caiu antes do áudio")
        if not getattr(voice_client, "is_playing", lambda: False)() and not getattr(voice_client, "is_paused", lambda: False)():
            raise RuntimeError("ffmpeg iniciou, mas o áudio não ficou tocando")
        if tracks_first_frame and getattr(source, "first_frame_ms", None) is None:
            raise TimeoutError("FFmpeg não entregou o primeiro frame de áudio")
        return max(0.0, time.monotonic() - started)

    def _discord_opus_bitrate_kbps(self, voice_client: Any, track: AgentTrack | None = None) -> tuple[int, int]:
        """Escolhe bitrate Opus sem exceder a capacidade do canal.

        O source PCM é reencodado pelo discord.py. Dar um pouco de headroom em
        relação ao abr da fonte reduz perda geracional, mas manter um teto
        evita gastar banda/CPU sem ganho perceptível em fontes comprimidas.
        """
        channel = getattr(voice_client, "channel", None)
        channel_bps = max(0, int(getattr(channel, "bitrate", 0) or 0))
        channel_kbps = max(0, channel_bps // 1000)
        source_abr = max(0, int(getattr(track, "audio_abr", 0) or 0)) if track is not None else 0

        if source_abr > 0:
            desired = source_abr + int(self.discord_opus_source_headroom)
        else:
            desired = int(self.discord_opus_default_bitrate)
        desired = max(int(self.discord_opus_min_bitrate), desired)
        desired = min(int(self.discord_opus_max_bitrate), desired)
        if channel_kbps > 0:
            desired = min(desired, channel_kbps)

        # Limites aceitos pelo encoder do discord.py/Opus. O canal pode ter
        # bitrate menor que o piso configurado; nesse caso respeite o canal.
        return max(16, min(512, int(desired))), channel_kbps

    def _play_music_source(self, voice_client: Any, source: Any, *, after: Any, opus_bitrate_kbps: int) -> None:
        if getattr(source, "is_opus", lambda: False)():
            # FFmpegOpusAudio já chega codificado; kwargs do encoder seriam
            # ignorados pelo discord.py.
            voice_client.play(source, after=after)
            return
        try:
            voice_client.play(
                source,
                after=after,
                application="audio",
                bitrate=max(16, min(512, int(opus_bitrate_kbps))),
                bandwidth="full",
                signal_type="music",
            )
            voice_client._music_opus_bitrate_kbps = int(opus_bitrate_kbps)
        except TypeError as exc:
            # Compatibilidade defensiva com wrappers/test doubles/discord.py
            # antigo. Não esconda TypeError real vindo de dentro do play().
            if "unexpected keyword argument" not in str(exc):
                raise
            self.log("opus_encoder_kwargs_unsupported", error=short_text(exc, 180))
            voice_client.play(source, after=after)

    @staticmethod
    def _audio_source_telemetry(source: Any) -> dict[str, Any]:
        getter = getattr(source, "audio_telemetry", None)
        if not callable(getter):
            return {}
        try:
            payload = getter()
        except Exception:
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    async def _play_direct_voice(self, guild_id: int, track: AgentTrack, *, prepared_voice: tuple[Any, bool] | None = None) -> None:
        direct_start_monotonic = time.monotonic()
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        requested_token = st.playback_token
        if not track.stream_url:
            raise RuntimeError("track sem stream_url direto")
        voice_client = prepared_voice[0] if prepared_voice is not None else None
        if (
            voice_client is None
            or not getattr(voice_client, "is_connected", lambda: False)()
            or getattr(getattr(voice_client, "channel", None), "id", None) != st.voice_channel_id
        ):
            voice_client, _created = await self._ensure_direct_voice_client(guild_id)
            if requested_token != st.playback_token:
                if _created and st.player is not voice_client:
                    await self._disconnect_voice_client_bounded(voice_client, guild_id=guild_id, reason="superseded_start")
                return
        else:
            self.log("voice_preconnect_reused", guild_id=guild_id, channel=st.voice_channel_id, transport="direct")
        existing_source = getattr(voice_client, "source", None)
        reusable = bool(
            self.direct_pcm_volume_enabled
            and isinstance(existing_source, AgentMixedAudioSource)
            and existing_source.persistent
            and existing_source.music_ended
            and getattr(voice_client, "is_playing", lambda: False)()
        )
        if not reusable and (getattr(voice_client, "is_playing", lambda: False)() or getattr(voice_client, "is_paused", lambda: False)()):
            if isinstance(existing_source, AgentMixedAudioSource) and existing_source.persistent:
                existing_source.stop_music()
            voice_client.stop()
        opus_bitrate_kbps, channel_bitrate_kbps = self._discord_opus_bitrate_kbps(voice_client, track)
        source_rate = max(0, int(getattr(track, "audio_sample_rate", 0) or 0))
        effects = (st.bassboost, st.nightcore)
        _audio_options, resample_mode = self._ffmpeg_options_for_source(
            source_rate, effects=effects, is_live=track.is_live,
        )
        st.player = voice_client
        st.transport = "direct"
        st.playback_token += 1
        playback_token = st.playback_token
        self._set_status(st, "starting", event="direct_player_starting")
        # O EOF pode chegar antes da confirmação do primeiro frame. Não use o
        # relógio herdado da faixa anterior ao decidir se deve recuperar.
        st.started_monotonic = time.monotonic()
        source_channels = max(0, int(getattr(track, "audio_channels", 0) or 0))
        quality_context: dict[str, Any] = {
            "source_format": getattr(track, "audio_format_id", ""),
            "source_codec": getattr(track, "audio_codec", ""),
            "source_abr_kbps": max(0, int(getattr(track, "audio_abr", 0) or 0)),
            "source_sample_rate": source_rate,
            "source_channels": source_channels,
            "resample_mode": resample_mode,
            "output": "pcm_s16le_48k_stereo" if self.direct_pcm_volume_enabled else "discord-opus",
            "channel_bitrate_kbps": channel_bitrate_kbps,
            "opus_bitrate_kbps": opus_bitrate_kbps,
            "volume_percent": int(st.volume_percent),
            "volume_mode": "soft_limited_boost" if int(st.volume_percent) > 100 else "linear",
            "bassboost": st.bassboost,
            "nightcore": st.nightcore and not track.is_live,
        }
        self.log(
            "audio_source_selected",
            guild_id=guild_id,
            format=getattr(track, "audio_format_id", ""),
            codec=getattr(track, "audio_codec", ""),
            abr_kbps=max(0, int(getattr(track, "audio_abr", 0) or 0)),
            sample_rate=source_rate,
            channels=source_channels,
            output="pcm_s16le_48k_stereo" if self.direct_pcm_volume_enabled else "discord-opus",
            resample=bool(source_rate and source_rate != 48000),
            resample_mode=resample_mode,
            channel_bitrate_kbps=channel_bitrate_kbps,
            opus_bitrate_kbps=opus_bitrate_kbps,
            opus_signal="music",
            volume_percent=int(st.volume_percent),
            volume_mode="soft_limited_boost" if int(st.volume_percent) > 100 else "linear",
        )
        self.log("player_play_called", guild_id=guild_id, transport="direct", title=track.title, offset=round(float(getattr(track, "start_offset_seconds", 0.0) or 0.0), 2), mixer_reused=reusable)

        def on_music_end(error: Exception | None, metrics: dict[str, Any]) -> None:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            asyncio.run_coroutine_threadsafe(
                self._direct_after(guild_id, error, playback_token, audio_metrics=metrics, quality_context=dict(quality_context)),
                loop,
            )

        pcm_source = None
        if self.direct_pcm_volume_enabled:
            pcm_prepare_started = time.monotonic()
            try:
                pcm_source = await self._prepare_current_pcm(guild_id, track, playback_token)
            except Exception:
                if playback_token != st.playback_token:
                    self.log("play_start_superseded", guild_id=guild_id, phase="pcm_prepare")
                    return
                raise
            if playback_token != st.playback_token:
                pcm_source.cleanup()
                self.log("play_start_superseded", guild_id=guild_id, phase="pcm_prepare")
                return
            quality_context["pcm_prepare_ms"] = round((time.monotonic() - pcm_prepare_started) * 1000.0, 1)
        try:
            source = self._build_ffmpeg_source(
                track.stream_url,
                volume_percent=st.volume_percent,
                start_offset_seconds=getattr(track, "start_offset_seconds", 0.0),
                opus_bitrate_kbps=opus_bitrate_kbps,
                source_sample_rate=source_rate,
                on_music_end=on_music_end if self.direct_pcm_volume_enabled else None,
                reuse_mixer=existing_source if reusable else None,
                pcm_source=pcm_source,
                effects=effects,
                is_live=track.is_live,
            )
        except BaseException:
            if pcm_source is not None:
                pcm_source.cleanup()
            raise
        if isinstance(source, AgentMixedAudioSource) and source.persistent:
            source.quality_context = dict(quality_context)
        if playback_token != st.playback_token:
            # Uma ação mais recente assumiu a sessão enquanto FFmpeg preparava.
            if not reusable:
                source.cleanup()
            return

        def after(error: Exception | None) -> None:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            callback_token = playback_token
            callback_error = error
            callback_quality = dict(quality_context)
            if isinstance(source, AgentMixedAudioSource) and source.persistent:
                # A sessão de voz continua entre faixas; este callback pertence
                # ao transporte, não à música que a iniciou. Um EOF de faixa
                # já foi entregue por on_music_end, mas uma queda de voz durante
                # qualquer faixa precisa recuperar a música atual.
                if source.music_ended or st.current is None or st.player is not voice_client:
                    return
                if getattr(voice_client, "source", source) not in (source, None):
                    return
                callback_token = int(st.playback_token)
                callback_error = error or RuntimeError("sessão de voz encerrada durante a música")
                callback_quality = dict(getattr(source, "quality_context", quality_context))
            metrics = self._audio_source_telemetry(source)
            asyncio.run_coroutine_threadsafe(
                self._direct_after(
                    guild_id,
                    callback_error,
                    callback_token,
                    audio_metrics=metrics,
                    quality_context=callback_quality,
                ),
                loop,
            )

        play_called_monotonic = time.monotonic()
        if not reusable:
            try:
                self._play_music_source(voice_client, source, after=after, opus_bitrate_kbps=opus_bitrate_kbps)
            except BaseException:
                source.cleanup()
                raise
        else:
            def update_encoder() -> None:
                encoder = getattr(voice_client, "encoder", None)
                setter = getattr(encoder, "set_bitrate", None)
                actual = getattr(voice_client, "_music_opus_bitrate_kbps", None)
                if callable(setter):
                    try:
                        actual = setter(opus_bitrate_kbps)
                        if actual is None:
                            actual = opus_bitrate_kbps
                        for name, value in (("set_signal_type", "music"), ("set_bandwidth", "full")):
                            callback = getattr(encoder, name, None)
                            if callable(callback):
                                callback(value)
                        voice_client._music_opus_bitrate_kbps = int(actual)
                    except Exception as exc:
                        self.log("opus_update_failed", guild_id=guild_id, error=type(exc).__name__)
                quality_context["opus_bitrate_kbps"] = actual
                source.quality_context.update(opus_bitrate_kbps=actual)
                self.log("opus_encoder_updated", guild_id=guild_id, requested_kbps=opus_bitrate_kbps, applied_kbps=actual)
            source.queue_encoder_update(update_encoder)
        # Confirme assim que o primeiro frame for consumido. O timeout limita
        # falhas de início; não acrescenta uma espera fixa ao caminho normal.
        confirm_delay = max(0.05, min(15.0, env_float("MUSIC_AGENT_FIRST_FRAME_TIMEOUT_SECONDS", 8.0)))
        try:
            confirm_elapsed = await self._confirm_direct_playback(voice_client, source, max_delay=confirm_delay)
        except BaseException:
            if playback_token != int(getattr(st, "playback_token", 0) or 0):
                # O callback `after` ou uma ação do usuário já assumiu a transição.
                # Não deixe a confirmação atrasada sobrescrever recovery/skip/stop.
                self.log("play_start_superseded", guild_id=guild_id, transport="direct", title=track.title)
                return
            if getattr(voice_client, "source", None) is source:
                if isinstance(source, AgentMixedAudioSource) and source.persistent:
                    source.stop_music()
                else:
                    voice_client.stop()
                    source.cleanup()
            raise
        if playback_token != int(getattr(st, "playback_token", 0) or 0):
            self.log("play_start_superseded", guild_id=guild_id, transport="direct", title=track.title)
            return
        # O relógio monotônico começa no primeiro frame observado quando o source
        # fornece essa métrica; caso contrário use o instante de voice_client.play.
        first_frame_monotonic = getattr(source, "first_frame_monotonic", None)
        st.started_monotonic = float(first_frame_monotonic or play_called_monotonic)
        st.paused_monotonic = 0.0
        transition_gap_ms: float | None = None
        previous_end = float(getattr(st, "last_audio_end_monotonic", 0.0) or 0.0)
        if previous_end > 0.0:
            transition_gap_ms = max(0.0, (st.started_monotonic - previous_end) * 1000.0)
        quality_context["first_frame_ms"] = getattr(source, "first_frame_ms", None)
        quality_context["play_start_ms"] = round((time.monotonic() - direct_start_monotonic) * 1000.0, 1)
        quality_context["transition_gap_ms"] = round(transition_gap_ms, 1) if transition_gap_ms is not None else None
        if isinstance(source, AgentMixedAudioSource) and source.persistent:
            source.quality_context = dict(quality_context)
        st.consecutive_start_failures = 0
        st.last_error_category = ""
        st.last_error_phase = ""
        st.voice_runtime_recovery_pending = False
        st.voice_runtime_recovery_last_error = ""
        track.voice_recovery_attempts = 0
        self._set_status(st, "playing", event="direct_track_start_confirmed")
        self._set_voice_session_mode(st, "music_active", reason="play_started")
        await self._refresh_voice_presence_policy(guild_id, source="play_started")
        self.log(
            "play_started",
            guild_id=guild_id,
            transport="direct",
            title=track.title,
            confirm_delay=confirm_delay,
            confirm_elapsed_ms=round(confirm_elapsed * 1000.0, 1),
            first_frame_ms=getattr(source, "first_frame_ms", None),
        )
        self.log(
            "audio_pipeline_ready",
            guild_id=guild_id,
            title=track.title,
            **quality_context,
        )
        self._schedule_next_queue_prefetch(guild_id, reason="direct_playing")

    def _ffmpeg_before_options_for_offset(self, start_offset_seconds: float = 0.0) -> str:
        try:
            offset = max(0.0, float(start_offset_seconds or 0.0))
        except Exception:
            offset = 0.0
        if offset <= 0.05:
            return self.ffmpeg_before_options
        # -ss antes do input torna o seek rápido para URLs remotas.
        return f"-ss {offset:.3f} {self.ffmpeg_before_options}".strip()

    @staticmethod
    def _ffmpeg_has_custom_audio_filter(options: str) -> bool:
        padded = f" {str(options or '').strip().lower()} "
        return any(
            marker in padded
            for marker in (" -af ", " -af=", " -filter:a", " -filter_complex ", " -filter_complex=")
        )

    def _ffmpeg_quality_resample_filter(self, *, nightcore: bool = False) -> str:
        # FFmpeg SWR está disponível sem dependência opcional. Janela maior
        # preserva melhor o topo da banda e rejeita aliasing no Nightcore.
        setting = "nightcore_resample_filter_size" if nightcore else "resample_filter_size"
        size = max(16, min(64, int(getattr(self, setting, 64) or 64)))
        phase = max(8, min(12, int(getattr(self, "resample_phase_shift", 10) or 10)))
        return (
            "aresample=48000:resampler=swr"
            f":filter_size={size}:phase_shift={phase}"
            ":linear_interp=1:exact_rational=1:filter_type=kaiser"
        )

    def _ffmpeg_options_for_source(
        self, source_sample_rate: int = 0, *, effects: tuple[bool, bool] = (False, False),
        is_live: bool = False,
    ) -> tuple[str, str]:
        base = str(self.ffmpeg_options or "").strip()
        # O Bassboost usa a folga depois do volume no mixer PCM; não aplica
        # redução/limiter global ao decoder, mesmo que Nightcore esteja ativo.
        effects = (False, bool(effects[1]) and not is_live)
        try:
            rate = max(0, int(source_sample_rate or 0))
        except Exception:
            rate = 0
        quality_enabled = bool(getattr(self, "resample_quality_enabled", True))
        if any(effects):
            if self._ffmpeg_has_custom_audio_filter(base):
                raise ValueError("efeitos de áudio incompatíveis com o filtro FFmpeg personalizado")
            quality = self._ffmpeg_quality_resample_filter(nightcore=True) if quality_enabled else "aresample=48000"
            resample = self._ffmpeg_quality_resample_filter() if rate not in (0, 48000) and quality_enabled else ""
            chain = filtros(
                bassboost=effects[0], nightcore=effects[1], is_live=is_live,
                resample=resample, nightcore_resample=quality,
            )
            return f"{base} -af {chain}".strip(), "effects"
        if rate == 48000:
            return base, "native_48k"
        if rate <= 0:
            return base, "ffmpeg_auto_unknown"
        if not quality_enabled:
            return base, "ffmpeg_auto"
        if self._ffmpeg_has_custom_audio_filter(base):
            return base, "custom_filter"
        resample = self._ffmpeg_quality_resample_filter()
        return f"{base} -af {resample}".strip(), "swr_quality"

    def _build_ffmpeg_source(
        self,
        stream_url: str,
        *,
        volume_percent: int | None = None,
        start_offset_seconds: float = 0.0,
        opus_bitrate_kbps: int | None = None,
        source_sample_rate: int = 0,
        on_music_end: Any = None,
        reuse_mixer: AgentMixedAudioSource | None = None,
        pcm_source: Any = None,
        effects: tuple[bool, bool] = (False, False),
        is_live: bool = False,
    ) -> Any:
        volume = max(0.0, min(1.5, float(volume_percent if volume_percent is not None else self.default_volume_percent) / 100.0))
        before_options = self._ffmpeg_before_options_for_offset(start_offset_seconds)
        ffmpeg_options, _resample_mode = self._ffmpeg_options_for_source(
            source_sample_rate, effects=effects, is_live=is_live,
        )
        if self.direct_pcm_volume_enabled:
            pcm = pcm_source if pcm_source is not None else self._create_pcm_source(AgentTrack(
                stream_url=stream_url, audio_sample_rate=source_sample_rate,
                start_offset_seconds=start_offset_seconds, is_live=is_live,
            ), effects=effects)
            loop = self._loop or asyncio.get_running_loop()
            if reuse_mixer is not None:
                reuse_mixer.replace_music_source(pcm, volume=volume, on_music_end=on_music_end,
                                                 bassboost=effects[0])
                return reuse_mixer
            return AgentMixedAudioSource(
                loop=loop,
                music_source=pcm,
                music_volume=volume,
                duck_factor=max(0.0, min(1.0, self.duck_volume_percent / 100.0)),
                telemetry_enabled=bool(getattr(self, "audio_telemetry_enabled", True)),
                stall_threshold_ms=float(getattr(self, "audio_stall_threshold_ms", 80.0)),
                on_music_end=on_music_end,
                persistent=on_music_end is not None,
                bassboost=effects[0],
            )
        opus_cls = getattr(discord, "FFmpegOpusAudio", None)
        if opus_cls is not None:
            opus = opus_cls(
                stream_url,
                executable=self.ffmpeg_executable,
                before_options=before_options,
                options=ffmpeg_options,
                bitrate=max(16, min(512, int(opus_bitrate_kbps or self.ffmpeg_bitrate))),
            )
            return AgentTelemetryAudioSource(
                opus,
                telemetry_enabled=bool(getattr(self, "audio_telemetry_enabled", True)),
                stall_threshold_ms=float(getattr(self, "audio_stall_threshold_ms", 80.0)),
            )
        pcm = discord.FFmpegPCMAudio(
            stream_url,
            executable=self.ffmpeg_executable,
            before_options=before_options,
            options=ffmpeg_options,
        )
        transformed = discord.PCMVolumeTransformer(pcm, volume=volume)
        return AgentTelemetryAudioSource(
            transformed,
            telemetry_enabled=bool(getattr(self, "audio_telemetry_enabled", True)),
            stall_threshold_ms=float(getattr(self, "audio_stall_threshold_ms", 80.0)),
            expected_frame_bytes=PCM_FRAME_BYTES,
        )

    def _cancel_voice_runtime_recovery(self, guild_id: int) -> None:
        registry = getattr(self, "_voice_runtime_recovery_tasks", None)
        if not isinstance(registry, dict):
            return
        task = registry.pop(int(guild_id or 0), None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _schedule_voice_runtime_recovery(
        self,
        guild_id: int,
        *,
        played_for: float,
        reason: str,
        error: str,
    ) -> bool:
        """Recupera perda de Discord Voice sem consumir a faixa ou a fila."""
        guild_id = int(guild_id or 0)
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        track = st.current
        if track is None:
            return False
        registry = getattr(self, "_voice_runtime_recovery_tasks", None)
        if not isinstance(registry, dict):
            self._voice_runtime_recovery_tasks = registry = {}
        existing = registry.get(guild_id)
        if existing is not None and not existing.done():
            return True

        resume_offset = self._resume_offset_for_track(track, played_for=played_for, speed=st.playback_speed)
        track.start_offset_seconds = resume_offset
        st.voice_runtime_recovery_pending = True
        st.voice_runtime_recovery_attempts = 0
        st.voice_runtime_recovery_last_error = str(error or "")[:260]
        self._set_status(st, "preparing", event="voice_runtime_recovery_wait")
        self.log(
            "voice_runtime_recovery_scheduled",
            guild_id=guild_id,
            reason=reason,
            resume_offset=round(resume_offset, 2),
            title=getattr(track, "title", ""),
            queue_size=len(st.queue),
        )

        async def _runner() -> None:
            attempts = max(1, min(20, env_int("MUSIC_AGENT_VOICE_RUNTIME_RECOVERY_ATTEMPTS", 10)))
            base_delay = max(0.2, min(5.0, env_float("MUSIC_AGENT_VOICE_RUNTIME_RECOVERY_BASE_SECONDS", 0.75)))
            last_exc: BaseException | None = None
            try:
                for attempt in range(1, attempts + 1):
                    current = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
                    if current.current is not track:
                        return
                    delay = 0.0 if attempt == 1 else min(8.0, base_delay * (2 ** min(attempt - 2, 4)))
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if current.current is not track:
                        return
                    current.voice_runtime_recovery_attempts = attempt
                    current.updated_at = time.time()
                    self.log(
                        "voice_runtime_recovery_attempt",
                        guild_id=guild_id,
                        attempt=attempt,
                        max_attempts=attempts,
                        delay_seconds=round(delay, 2),
                        title=getattr(track, "title", ""),
                    )
                    try:
                        await asyncio.wait_for(
                            self._play_direct_voice(guild_id, track),
                            timeout=max(5.0, float(self.prepare_timeout)),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        last_exc = exc
                        category, recoverable = self._classify_play_failure(exc, phase="voice_connect")
                        current.voice_runtime_recovery_last_error = f"{type(exc).__name__}: {short_text(exc, 220)}"
                        current.last_error_category = category
                        current.last_error_phase = "voice_runtime_recovery"
                        self.log(
                            "voice_runtime_recovery_failed",
                            guild_id=guild_id,
                            attempt=attempt,
                            max_attempts=attempts,
                            category=category,
                            recoverable=recoverable,
                            error=current.voice_runtime_recovery_last_error,
                        )
                        if category != "voice_transport":
                            break
                        continue
                    current.voice_runtime_recovery_pending = False
                    current.voice_runtime_recovery_last_error = ""
                    current.last_error_category = ""
                    current.last_error_phase = ""
                    self.log(
                        "voice_runtime_recovered",
                        guild_id=guild_id,
                        attempt=attempt,
                        resume_offset=round(float(getattr(track, "start_offset_seconds", 0.0) or 0.0), 2),
                        title=getattr(track, "title", ""),
                    )
                    return

                current = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
                if current.current is track:
                    current.voice_runtime_recovery_pending = False
                    message = current.voice_runtime_recovery_last_error or (
                        f"{type(last_exc).__name__}: {short_text(last_exc, 220)}" if last_exc else "reconexão de voz esgotada"
                    )
                    self._set_status(current, "failed", event="voice_runtime_recovery_exhausted", error=message)
                    current.last_error_category = "voice_transport"
                    current.last_error_phase = "voice_runtime_recovery"
                    self.log(
                        "voice_runtime_recovery_exhausted",
                        guild_id=guild_id,
                        attempts=current.voice_runtime_recovery_attempts,
                        title=getattr(track, "title", ""),
                        queue_size=len(current.queue),
                        error=message,
                    )
            except asyncio.CancelledError:
                raise
            finally:
                current = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
                if current.current is not track or str(getattr(current, "status", "") or "") == "playing":
                    current.voice_runtime_recovery_pending = False
                remove_owned_task(registry, guild_id, asyncio.current_task())

        try:
            task = asyncio.create_task(_runner())
            registry[guild_id] = task
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            return True
        except RuntimeError:
            st.voice_runtime_recovery_pending = False
            return False

    async def _recover_current_stream(self, guild_id: int, *, played_for: float, reason: str) -> bool:
        """Re-resolve uma URL tocável quebrada e retoma a faixa uma única vez.

        URLs diretas (especialmente googlevideo) são efêmeras. Em vez de pular
        imediatamente a faixa quando FFmpeg encerra cedo/erra, invalide apenas
        o cache de stream, preserve metadata e peça uma URL nova ao yt-dlp.
        """
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        track = st.current
        if not self.stream_recovery_enabled or track is None:
            return False
        attempts = max(0, int(getattr(track, "stream_recovery_attempts", 0) or 0))
        if attempts >= self.stream_recovery_max_attempts:
            self.log(
                "stream_recovery_exhausted",
                guild_id=guild_id,
                reason=reason,
                attempts=attempts,
                title=getattr(track, "title", ""),
            )
            return False

        # A posição autoritativa é o offset usado para iniciar esta execução +
        # o tempo efetivamente tocado. Um pequeno backtrack reduz o risco de
        # cortar áudio no ponto de reconexão sem reiniciar a música inteira.
        base_offset = max(0.0, float(getattr(track, "start_offset_seconds", 0.0) or 0.0))
        resume_offset = max(0.0, base_offset + max(0.0, float(played_for or 0.0)) * st.playback_speed - self.stream_recovery_backtrack_seconds)
        if track.duration is not None:
            with contextlib.suppress(Exception):
                resume_offset = min(resume_offset, max(0.0, float(track.duration) - 0.05))

        meta = track.public()
        query = self._query_from_track_meta(meta, fallback_query=track.query or track.webpage_url or track.title)
        if not query:
            return False
        body = {
            "guild_id": guild_id,
            "voice_channel_id": st.voice_channel_id,
            "text_channel_id": st.text_channel_id,
            "requester_id": track.requester_id,
            "requester_name": track.requester_name,
            "query": query,
            "track": meta,
            "position_seconds": resume_offset,
        }
        self._invalidate_track_stream_cache(track)
        self._set_status(st, "preparing", event="stream_recovery")
        started = time.monotonic()
        self.log(
            "stream_recovery_started",
            guild_id=guild_id,
            reason=reason,
            attempt=attempts + 1,
            resume_offset=round(resume_offset, 2),
            title=track.title,
        )
        try:
            # Recovery é interação ativa e deve passar à frente de prefetches.
            recovered = await asyncio.wait_for(
                self.resolve_track(query, track_meta=meta, body=body, priority=-20),
                timeout=max(5.0, self.prepare_timeout),
            )
            recovered.start_offset_seconds = resume_offset
            recovered.stream_recovery_attempts = attempts + 1
            st.current = recovered
            await asyncio.wait_for(
                self._play_direct_voice(guild_id, recovered),
                timeout=max(5.0, self.prepare_timeout),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            category, _recoverable = self._classify_play_failure(exc, phase="playback_start")
            if category != "voice_transport":
                self._invalidate_track_stream_cache(st.current)
            else:
                st.last_error_category = "voice_transport"
                st.last_error_phase = "stream_recovery_voice"
            self.log(
                "stream_recovery_failed",
                guild_id=guild_id,
                reason=reason,
                attempt=attempts + 1,
                category=category,
                error=f"{type(exc).__name__}: {short_text(exc, 220)}",
            )
            return False
        self.log(
            "stream_recovery_ok",
            guild_id=guild_id,
            reason=reason,
            attempt=attempts + 1,
            resume_offset=round(resume_offset, 2),
            elapsed_ms=round((time.monotonic() - started) * 1000.0, 1),
            title=recovered.title,
        )
        return True

    async def _direct_after(
        self,
        guild_id: int,
        error: Exception | None,
        playback_token: int = 0,
        *,
        audio_metrics: dict[str, Any] | None = None,
        quality_context: dict[str, Any] | None = None,
    ) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if playback_token and playback_token != int(getattr(st, "playback_token", 0) or 0):
            return
        if st.current is None and not st.queue:
            return
        ended_monotonic = time.monotonic()
        played_for = ended_monotonic - float(st.started_monotonic or 0.0) if st.started_monotonic else 0.0
        remaining_expected: float | None = None
        if st.current is not None and st.current.duration is not None:
            with contextlib.suppress(Exception):
                remaining_expected = max(
                    0.0,
                    (float(st.current.duration)
                     - max(0.0, float(getattr(st.current, "start_offset_seconds", 0.0) or 0.0))) / st.playback_speed,
                )
        remaining_after_play = (
            max(0.0, remaining_expected - played_for)
            if remaining_expected is not None else None
        )
        min_ok = max(0.5, env_float("MUSIC_AGENT_EARLY_END_SECONDS", 2.5))
        cutoff_tolerance = max(10.0, min(30.0, (remaining_expected or 0.0) * 0.07))
        early_unexpected = bool(
            st.current is not None
            and (
                (played_for < min_ok and (remaining_expected is None or remaining_expected > min_ok))
                or (remaining_after_play is not None and remaining_after_play > cutoff_tolerance)
            )
        )
        # Invalidate this callback before any await in the transition. Discord may
        # invoke the same direct-player callback more than once while the next
        # track is still resolving/preparing.
        keep = self._next_resolving_prefetch_keys(st) if not error and not early_unexpected else set()
        self._bump_playback_generation(st, reason="direct_after", keep_prefetch_task_keys=keep)
        st.last_audio_end_monotonic = ended_monotonic
        metrics = dict(audio_metrics or {})
        quality = dict(quality_context or {})

        def log_summary(outcome: str) -> None:
            summary_fields = dict(quality)
            summary_fields.update(metrics)
            self.log(
                "audio_playback_summary",
                guild_id=guild_id,
                title=getattr(st.current, "title", ""),
                outcome=outcome,
                played_ms=round(max(0.0, played_for) * 1000.0, 1),
                error=f"{type(error).__name__}: {short_text(error, 180)}" if error else "",
                **summary_fields,
            )

        if error:
            log_summary("error")
            callback_voice_failure = self._is_voice_transport_error(error, phase="") or (
                st.player is not None and not self._voice_client_is_connected(st.player)
            )
            if st.current is not None and callback_voice_failure:
                message = f"{type(error).__name__}: {short_text(error, 260)}"
                st.last_error_category = "voice_transport"
                st.last_error_phase = "runtime_playback"
                self.log(
                    "voice_transport_playback_preserved",
                    guild_id=guild_id,
                    played_for=round(played_for, 2),
                    title=getattr(st.current, "title", ""),
                    queue_size=len(st.queue),
                    error=message,
                    stream_refresh_skipped=True,
                )
                # Queda de Discord Voice não invalida a URL do stream. Tentar
                # yt-dlp primeiro acrescentava 3-7 s justamente durante uma
                # oscilação de rede e ainda podia mascarar a causa real.
                if self._schedule_voice_runtime_recovery(
                    guild_id,
                    played_for=played_for,
                    reason="direct_after_error",
                    error=message,
                ):
                    return
                self._set_status(st, "failed", event="voice_runtime_recovery_unavailable", error=message)
                return
            if await self._recover_current_stream(guild_id, played_for=played_for, reason="direct_after_error"):
                return
            recovery_voice_failure = str(getattr(st, "last_error_category", "") or "") == "voice_transport"
            if st.current is not None and recovery_voice_failure:
                message = str(getattr(st, "last_error", "") or f"{type(error).__name__}: {short_text(error, 260)}")
                st.last_error_category = "voice_transport"
                st.last_error_phase = "runtime_playback"
                self.log(
                    "voice_transport_playback_preserved",
                    guild_id=guild_id,
                    played_for=round(played_for, 2),
                    title=getattr(st.current, "title", ""),
                    queue_size=len(st.queue),
                    error=message,
                    stream_refresh_skipped=False,
                )
                if self._schedule_voice_runtime_recovery(
                    guild_id,
                    played_for=0.0,
                    reason="direct_after_error",
                    error=message,
                ):
                    return
                self._set_status(st, "failed", event="voice_runtime_recovery_unavailable", error=message)
                return
            self._invalidate_track_stream_cache(st.current)
            self._set_status(st, "failed", event="direct_after_error", error=f"{type(error).__name__}: {short_text(error, 260)}")
            self.log("play_failed", guild_id=guild_id, transport="direct", error=st.last_error)
            if st.queue:
                await self._play_next(guild_id)
            else:
                self._finish_mixer_when_idle(st)
            return
        # Um EOF sem erro depois de 2,5 s também pode ser uma conexão que caiu
        # no meio de uma música de vários minutos. Tolere diferenças pequenas
        # entre as durações fornecidas pelo site e pelo player.
        if early_unexpected:
            log_summary("early_end")
            voice_was_lost = st.player is not None and not self._voice_client_is_connected(st.player)
            if st.current is not None and voice_was_lost:
                message = (
                    f"áudio encerrou cedo após perda de voz ({played_for:.1f}s; restantes {remaining_after_play:.1f}s)"
                    if remaining_after_play is not None
                    else f"áudio encerrou cedo após perda de voz ({played_for:.1f}s)"
                )
                st.last_error_category = "voice_transport"
                st.last_error_phase = "runtime_playback"
                self.log(
                    "voice_transport_early_end_preserved",
                    guild_id=guild_id,
                    played_for=round(played_for, 2),
                    title=getattr(st.current, "title", ""),
                    queue_size=len(st.queue),
                    stream_refresh_skipped=True,
                )
                if self._schedule_voice_runtime_recovery(
                    guild_id,
                    played_for=played_for,
                    reason="direct_after_early_end",
                    error=message,
                ):
                    return
                self._set_status(st, "failed", event="voice_runtime_recovery_unavailable", error=message)
                return
            if await self._recover_current_stream(guild_id, played_for=played_for, reason="direct_after_early_end"):
                return
            recovery_voice_failure = str(getattr(st, "last_error_category", "") or "") == "voice_transport"
            if st.current is not None and recovery_voice_failure:
                message = (
                    f"áudio encerrou cedo e a retomada encontrou perda de voz ({played_for:.1f}s; restantes {remaining_after_play:.1f}s)"
                    if remaining_after_play is not None
                    else f"áudio encerrou cedo e a retomada encontrou perda de voz ({played_for:.1f}s)"
                )
                st.last_error_category = "voice_transport"
                st.last_error_phase = "runtime_playback"
                self.log(
                    "voice_transport_early_end_preserved",
                    guild_id=guild_id,
                    played_for=round(played_for, 2),
                    title=getattr(st.current, "title", ""),
                    queue_size=len(st.queue),
                    stream_refresh_skipped=False,
                )
                if self._schedule_voice_runtime_recovery(
                    guild_id,
                    played_for=0.0,
                    reason="direct_after_early_end",
                    error=message,
                ):
                    return
                self._set_status(st, "failed", event="voice_runtime_recovery_unavailable", error=message)
                return
            self._invalidate_track_stream_cache(st.current)
            self._set_status(st, "failed", event="direct_after_early_end", error=f"áudio encerrou cedo demais ({played_for:.1f}s; restantes {remaining_after_play:.1f}s)" if remaining_after_play is not None else f"áudio encerrou cedo demais ({played_for:.1f}s)")
            self.log("play_failed", guild_id=guild_id, transport="direct", error=st.last_error, title=getattr(st.current, "title", ""))
            if st.queue:
                await self._play_next(guild_id)
            else:
                self._finish_mixer_when_idle(st)
            return
        log_summary("ended")
        self.log("play_ended", guild_id=guild_id, transport="direct", title=getattr(st.current, "title", ""))
        await self._finish_current(guild_id, error=None, event="direct_track_end")

    async def _finish_current(self, guild_id: int, *, error: str | None, event: str) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if error:
            self._set_status(st, "failed", event=event, error=error)
            self._finish_mixer_when_idle(st)
            return
        finished = st.current
        st.current = None
        st.paused = False
        loop_mode = str(getattr(st, "loop_mode", "off") or "off").strip().lower()
        if finished is not None and loop_mode != "one":
            self._push_history(st, finished)

        if finished is not None and loop_mode == "one":
            finished.start_offset_seconds = 0.0
            finished.stream_recovery_attempts = 0
            st.queue.insert(0, finished)
            self.log("loop_one_requeue", guild_id=guild_id, title=getattr(finished, "title", ""))
            await self._play_next(guild_id)
            return

        if finished is not None and loop_mode == "all":
            finished.start_offset_seconds = 0.0
            finished.stream_recovery_attempts = 0
            st.queue.append(finished)
            self.log("loop_all_requeue", guild_id=guild_id, title=getattr(finished, "title", ""), queue_size=len(st.queue))

        if st.queue:
            await self._play_next(guild_id)
            return
        self._set_status(st, "idle", event=event)
        self._finish_mixer_when_idle(st)
        # Fim normal de fila não é desconexão externa: mantenha a sessão de voz
        # viva e deixe o mesmo timeout AFK/idle decidir quando sair da call.
        self._schedule_idle_disconnect(guild_id)
        await self._refresh_voice_presence_policy(guild_id, source="queue_finished")
