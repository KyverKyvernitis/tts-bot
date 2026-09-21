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
from .mixer_pcm import AgentMixedAudioSource, AgentTelemetryAudioSource, PCM_FRAME_BYTES
from .utilitarios import safe_id, short_text


class ReproducaoMixin:
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

    def _bump_playback_generation(self, st: GuildMusicState, *, reason: str = "change") -> int:
        st.playback_token += 1
        st.updated_at = time.time()
        self._cancel_prefetch_tasks(st.guild_id)
        self._cancel_active_resolve(st.guild_id)
        self.log("playback_generation_bumped", guild_id=st.guild_id, reason=reason, token=st.playback_token)
        return st.playback_token

    def _guild_prefetch_key(self, guild_id: int, cache_key: str) -> str:
        return f"{int(guild_id or 0)}:{cache_key}"

    def _schedule_next_queue_prefetch(self, guild_id: int, *, reason: str = "playing") -> None:
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
        if self._resolve_cache_get(cache_key):
            return
        current_task = self._prefetch_tasks.get(task_key)
        if current_task is not None and not current_task.done():
            return
        token = int(getattr(st, "playback_token", 0) or 0)
        delay = 3.0
        current = st.current
        try:
            if current is not None and current.duration and st.started_monotonic:
                base = max(0.0, float(getattr(current, "start_offset_seconds", 0.0) or 0.0))
                elapsed = base + max(0.0, time.monotonic() - float(st.started_monotonic))
                remaining = max(0.0, float(current.duration) - elapsed)
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
                resolved = await asyncio.wait_for(self.resolve_track(query, track_meta=current_first.public(), body=body, priority=20), timeout=self.prefetch_timeout)
                latest2 = self.states.setdefault(int(guild_id or 0), GuildMusicState(guild_id=int(guild_id or 0)))
                if int(getattr(latest2, "playback_token", 0) or 0) == token and latest2.queue:
                    check_key = self._resolve_cache_key(
                        self._query_from_track_meta(latest2.queue[0].public(), fallback_query=latest2.queue[0].query or latest2.queue[0].webpage_url or latest2.queue[0].title),
                        latest2.queue[0].public(),
                    )
                    if check_key == cache_key:
                        latest2.queue[0] = resolved
                self.log("next_prefetch_ready", guild_id=guild_id, reason=reason, elapsed_ms=round((time.time() - started) * 1000.0, 1), title=getattr(resolved, "title", ""))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.log("next_prefetch_failed", guild_id=guild_id, reason=reason, error=short_text(exc, 180))
            finally:
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
            if self._resolve_cache_get(cache_key):
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
        st.voice_channel_id = voice_channel_id
        st.text_channel_id = text_channel_id
        if not st.normal_volume_percent:
            st.normal_volume_percent = self.default_volume_percent
        if not st.volume_percent:
            st.volume_percent = st.normal_volume_percent
        st.last_action = "play"
        self._cancel_idle_disconnect(guild_id)
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

        def _cursor_key(value: dict[str, Any]) -> tuple[str, str, int]:
            try:
                offset = max(0, int(value.get("next_offset") or 0))
            except Exception:
                offset = 0
            return (
                str(value.get("provider") or "").strip(),
                str(value.get("source_url") or "").strip(),
                offset,
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
            self.log("playlist_refill_ignored", guild_id=guild_id, reason="stale_cursor", expected_offset=expected_key[2])
            return {"ok": True, "ignored": True, "added": 0, "state": st.public()}

        incoming: list[AgentTrack] = []
        for item in body.get("tracks") if isinstance(body.get("tracks"), list) else []:
            if not isinstance(item, dict):
                continue
            track = self._agent_track_from_metadata(item, body=body, fallback_query="")
            if track.is_virtual_playlist_marker:
                continue
            if track.query or track.stream_url or track.webpage_url:
                incoming.append(track)

        exhausted = bool(next_cursor.get("exhausted")) if next_cursor else not incoming
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
        st.updated_at = time.time()
        self.log(
            "playlist_refilled",
            guild_id=guild_id,
            added=len(incoming),
            exhausted=exhausted,
            expected_offset=expected_key[2],
            next_offset=_cursor_key(next_cursor)[2] if next_cursor else expected_key[2],
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
        for value in (track.webpage_url, track.query, track.stream_url, track.title):
            raw = str(value or "").strip().lower()
            if raw:
                return raw[:300]
        return ""

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
        await stop_player_instance(player, disconnect=disconnect)

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
        st.queue.clear()
        st.history.clear()
        player = st.player
        st.player = None
        st.current = None
        self._set_status(st, "idle", event="stop")
        st.paused = False
        self._bump_playback_generation(st, reason="stop")
        await self._stop_player_instance(player, disconnect=True)
        return {"ok": True, "state": st.public()}

    async def cmd_skip(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.last_action = "skip"
        self._cancel_prefetch_tasks(guild_id)
        player = st.player
        self._push_history(st, st.current)
        self._bump_playback_generation(st, reason="skip")
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

    async def cmd_shuffle(self, body: dict[str, Any]) -> dict[str, Any]:
        guild_id = safe_id(body.get("guild_id"))
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        st.last_action = "shuffle"
        # Shuffle é uma ação única para embaralhar a fila atual, não um modo
        # persistente ligado/desligado. Não altere playback_token aqui: ele é
        # usado pelo callback do áudio atual; mudar esse token faria a faixa
        # atual terminar sem avançar a queue. Cancele apenas o prefetch antigo.
        self._cancel_prefetch_tasks(guild_id)
        if any(item.is_virtual_playlist_marker for item in st.queue):
            # Embaralhar através de um cursor ainda não materializado destruiria
            # a ordem lógica e exigiria carregar a playlist inteira em memória.
            return {
                "ok": False,
                "shuffled": False,
                "enabled": False,
                "error": "A playlist ainda está sendo carregada; aguarde para embaralhar.",
                "queue_size": sum(1 for item in st.queue if not item.is_virtual_playlist_marker),
                "state": st.public(),
            }
        if len(st.queue) > 1:
            import random as _random
            _random.shuffle(st.queue)
            st.shuffle = False
            st.updated_at = time.time()
            self._schedule_next_queue_prefetch(guild_id, reason="shuffle")
            self.log("queue_shuffled", guild_id=guild_id, queue_size=len(st.queue))
            return {"ok": True, "shuffled": True, "enabled": False, "queue_size": len(st.queue), "state": st.public()}
        st.shuffle = False
        st.updated_at = time.time()
        return {"ok": True, "shuffled": False, "enabled": False, "queue_size": len(st.queue), "state": st.public()}

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
                track = await self.resolve_track(track.webpage_url or track.query or track.title, track_meta=track.public(), body=body)
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
            st.paused = False
            self._set_status(st, "idle", event="queue_empty")
            self._schedule_idle_disconnect(guild_id)
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
        request_token = int(getattr(st, "playback_token", 0) or 0)
        current_ref = st.current
        st.paused = False
        st.paused_monotonic = 0.0
        st.transport = ""
        st.ducked = False
        st.normal_volume_percent = max(0, min(150, int(st.normal_volume_percent or self.default_volume_percent)))
        st.volume_percent = st.normal_volume_percent
        self._set_status(st, "preparing", event="play_preparing")
        self.log("track_loading", guild_id=guild_id, title=getattr(st.current, "title", ""), source=getattr(st.current, "source", ""), lazy=not bool(getattr(st.current, "stream_url", "")))
        voice_prepare_task: asyncio.Task | None = None
        prepared_voice: tuple[Any, bool] | None = None
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
                    voice_prepare_task = asyncio.create_task(
                        asyncio.wait_for(
                            self._ensure_direct_voice_client(guild_id),
                            timeout=max(5.0, self.prepare_timeout),
                        )
                    )
                    self.log("voice_preconnect_started", guild_id=guild_id, channel=st.voice_channel_id, transport="direct")
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
                prepared_voice = await voice_prepare_task
                voice_prepare_task = None
            play_coro = (
                self._play_direct_voice(guild_id, st.current, prepared_voice=prepared_voice)
                if prepared_voice is not None
                else self._play_direct_voice(guild_id, st.current)
            )
            await asyncio.wait_for(play_coro, timeout=max(5.0, self.prepare_timeout))
        except Exception as exc:
            await self._discard_prepared_voice_task(guild_id, voice_prepare_task)
            if prepared_voice is not None and st.player is not prepared_voice[0]:
                # Se o preconnect criou uma sessão que nem chegou a ser entregue
                # ao player, não deixe uma conexão órfã após falha de resolução.
                voice_client, created = prepared_voice
                if created:
                    with contextlib.suppress(Exception):
                        if getattr(voice_client, "is_connected", lambda: False)():
                            await voice_client.disconnect(force=True)
            self._invalidate_track_stream_cache(st.current)
            self._set_status(st, "failed", event="play_failed", error=f"{type(exc).__name__}: {short_text(exc, 260)}")
            self.log("play_failed", guild_id=guild_id, transport=st.transport or "unknown", error=st.last_error)

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
        guild, channel = await self._resolve_guild_and_channel(guild_id, st.voice_channel_id)
        existing = guild.voice_client
        if existing is None or not getattr(existing, "is_connected", lambda: False)():
            self.log("voice_connecting", guild_id=guild_id, channel=st.voice_channel_id, transport="direct")
            voice_client = await channel.connect(self_deaf=True)
            self.log("voice_connected", guild_id=guild_id, channel=st.voice_channel_id, transport="direct", reused=False)
            return voice_client, True
        voice_client = existing
        current_channel_id = getattr(getattr(voice_client, "channel", None), "id", None)
        if current_channel_id != st.voice_channel_id:
            self.log("voice_moving", guild_id=guild_id, channel=st.voice_channel_id, from_channel=current_channel_id, transport="direct")
            await voice_client.move_to(channel)
        else:
            self.log("voice_reused", guild_id=guild_id, channel=st.voice_channel_id, transport="direct")
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
                with contextlib.suppress(Exception):
                    if getattr(voice_client, "is_connected", lambda: False)():
                        await voice_client.disconnect(force=True)
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
                raise RuntimeError("conectei no canal, mas a voz caiu antes do áudio")
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
            raise RuntimeError("conectei no canal, mas a voz caiu antes do áudio")
        if not getattr(voice_client, "is_playing", lambda: False)() and not getattr(voice_client, "is_paused", lambda: False)():
            raise RuntimeError("ffmpeg iniciou, mas o áudio não ficou tocando")
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
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if not track.stream_url:
            raise RuntimeError("track sem stream_url direto")
        voice_client = prepared_voice[0] if prepared_voice is not None else None
        if (
            voice_client is None
            or not getattr(voice_client, "is_connected", lambda: False)()
            or getattr(getattr(voice_client, "channel", None), "id", None) != st.voice_channel_id
        ):
            voice_client, _created = await self._ensure_direct_voice_client(guild_id)
        else:
            self.log("voice_preconnect_reused", guild_id=guild_id, channel=st.voice_channel_id, transport="direct")
        if getattr(voice_client, "is_playing", lambda: False)() or getattr(voice_client, "is_paused", lambda: False)():
            voice_client.stop()
        opus_bitrate_kbps, channel_bitrate_kbps = self._discord_opus_bitrate_kbps(voice_client, track)
        source_rate = max(0, int(getattr(track, "audio_sample_rate", 0) or 0))
        _audio_options, resample_mode = self._ffmpeg_options_for_source(source_rate)
        source = self._build_ffmpeg_source(
            track.stream_url,
            volume_percent=st.volume_percent,
            start_offset_seconds=getattr(track, "start_offset_seconds", 0.0),
            opus_bitrate_kbps=opus_bitrate_kbps,
            source_sample_rate=source_rate,
        )
        st.player = voice_client
        st.transport = "direct"
        st.playback_token += 1
        playback_token = st.playback_token
        self._set_status(st, "starting", event="direct_player_starting")
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
        self.log("player_play_called", guild_id=guild_id, transport="direct", title=track.title, offset=round(float(getattr(track, "start_offset_seconds", 0.0) or 0.0), 2))

        def after(error: Exception | None) -> None:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            metrics = self._audio_source_telemetry(source)
            asyncio.run_coroutine_threadsafe(
                self._direct_after(
                    guild_id,
                    error,
                    playback_token,
                    audio_metrics=metrics,
                    quality_context=dict(quality_context),
                ),
                loop,
            )

        play_called_monotonic = time.monotonic()
        self._play_music_source(voice_client, source, after=after, opus_bitrate_kbps=opus_bitrate_kbps)
        # Confirme assim que o primeiro frame PCM for consumido. O limite antigo
        # continua como fallback, mas deixa de ser uma espera fixa no hot path.
        confirm_delay = max(0.05, min(1.2, env_float("MUSIC_AGENT_DIRECT_CONFIRM_SECONDS", 0.35)))
        try:
            confirm_elapsed = await self._confirm_direct_playback(voice_client, source, max_delay=confirm_delay)
        except Exception:
            if playback_token != int(getattr(st, "playback_token", 0) or 0):
                # O callback `after` ou uma ação do usuário já assumiu a transição.
                # Não deixe a confirmação atrasada sobrescrever recovery/skip/stop.
                self.log("play_start_superseded", guild_id=guild_id, transport="direct", title=track.title)
                return
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
        quality_context["transition_gap_ms"] = round(transition_gap_ms, 1) if transition_gap_ms is not None else None
        self._set_status(st, "playing", event="direct_track_start_confirmed")
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

    def _ffmpeg_options_for_source(self, source_sample_rate: int = 0) -> tuple[str, str]:
        base = str(self.ffmpeg_options or "").strip()
        try:
            rate = max(0, int(source_sample_rate or 0))
        except Exception:
            rate = 0
        if rate == 48000:
            return base, "native_48k"
        if rate <= 0:
            return base, "ffmpeg_auto_unknown"
        if not bool(getattr(self, "resample_quality_enabled", True)):
            return base, "ffmpeg_auto"
        if self._ffmpeg_has_custom_audio_filter(base):
            return base, "custom_filter"
        filter_size = max(16, min(64, int(getattr(self, "resample_filter_size", 32) or 32)))
        phase_shift = max(8, min(12, int(getattr(self, "resample_phase_shift", 10) or 10)))
        resample = (
            "aresample=48000:resampler=swr"
            f":filter_size={filter_size}:phase_shift={phase_shift}"
            ":linear_interp=0:exact_rational=1"
        )
        return f"{base} -af {resample}".strip(), "swr_quality"

    def _build_ffmpeg_source(
        self,
        stream_url: str,
        *,
        volume_percent: int | None = None,
        start_offset_seconds: float = 0.0,
        opus_bitrate_kbps: int | None = None,
        source_sample_rate: int = 0,
    ) -> Any:
        volume = max(0.0, min(1.5, float(volume_percent if volume_percent is not None else self.default_volume_percent) / 100.0))
        before_options = self._ffmpeg_before_options_for_offset(start_offset_seconds)
        ffmpeg_options, _resample_mode = self._ffmpeg_options_for_source(source_sample_rate)
        if self.direct_pcm_volume_enabled:
            pcm = discord.FFmpegPCMAudio(
                stream_url,
                executable=self.ffmpeg_executable,
                before_options=before_options,
                options=ffmpeg_options,
            )
            loop = self._loop or asyncio.get_running_loop()
            return AgentMixedAudioSource(
                loop=loop,
                music_source=pcm,
                music_volume=volume,
                duck_factor=max(0.0, min(1.0, self.duck_volume_percent / 100.0)),
                telemetry_enabled=bool(getattr(self, "audio_telemetry_enabled", True)),
                stall_threshold_ms=float(getattr(self, "audio_stall_threshold_ms", 80.0)),
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
        resume_offset = max(0.0, base_offset + max(0.0, float(played_for or 0.0)) - self.stream_recovery_backtrack_seconds)
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
            self._invalidate_track_stream_cache(st.current)
            self.log(
                "stream_recovery_failed",
                guild_id=guild_id,
                reason=reason,
                attempt=attempts + 1,
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
        # Invalidate this callback before any await in the transition. Discord may
        # invoke the same direct-player callback more than once while the next
        # track is still resolving/preparing.
        self._bump_playback_generation(st, reason="direct_after")
        ended_monotonic = time.monotonic()
        played_for = ended_monotonic - float(st.started_monotonic or 0.0) if st.started_monotonic else 0.0
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

        min_ok = max(0.5, env_float("MUSIC_AGENT_EARLY_END_SECONDS", 2.5))
        if error:
            log_summary("error")
            if await self._recover_current_stream(guild_id, played_for=played_for, reason="direct_after_error"):
                return
            self._invalidate_track_stream_cache(st.current)
            self._set_status(st, "failed", event="direct_after_error", error=f"{type(error).__name__}: {short_text(error, 260)}")
            self.log("play_failed", guild_id=guild_id, transport="direct", error=st.last_error)
            if st.queue:
                await self._play_next(guild_id)
            return
        remaining_expected: float | None = None
        if st.current is not None and st.current.duration is not None:
            with contextlib.suppress(Exception):
                remaining_expected = max(
                    0.0,
                    float(st.current.duration)
                    - max(0.0, float(getattr(st.current, "start_offset_seconds", 0.0) or 0.0)),
                )
        early_unexpected = bool(
            played_for < min_ok
            and st.current is not None
            and (remaining_expected is None or remaining_expected > min_ok)
        )
        if early_unexpected:
            log_summary("early_end")
            if await self._recover_current_stream(guild_id, played_for=played_for, reason="direct_after_early_end"):
                return
            self._invalidate_track_stream_cache(st.current)
            self._set_status(st, "failed", event="direct_after_early_end", error=f"áudio encerrou cedo demais ({played_for:.1f}s)")
            self.log("play_failed", guild_id=guild_id, transport="direct", error=st.last_error, title=getattr(st.current, "title", ""))
            if st.queue:
                await self._play_next(guild_id)
            return
        log_summary("ended")
        self.log("play_ended", guild_id=guild_id, transport="direct", title=getattr(st.current, "title", ""))
        await self._finish_current(guild_id, error=None, event="direct_track_end")

    async def _finish_current(self, guild_id: int, *, error: str | None, event: str) -> None:
        st = self.states.setdefault(guild_id, GuildMusicState(guild_id=guild_id))
        if error:
            self._set_status(st, "failed", event=event, error=error)
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
        # Fim normal de fila não é desconexão externa: mantenha a sessão de voz
        # viva e deixe o mesmo timeout AFK/idle decidir quando sair da call.
        self._schedule_idle_disconnect(guild_id)
