from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from cogs.musica import configuracao as config
import discord
from discord.ext import commands

from ..agente_telefone.comandos import music_agent_command, music_agent_status
from ..agente_telefone.monitor import estado_local_music_agent, monitor_music_agent_ativo
from ..agente_telefone.resolucao import resolve_music_tracks_on_worker
from ..interface.carregamento import MusicLoadingReaction
from ..busca import registrar_lote_link_busca
from ..interface.componentes import SearchResultView
from ..metadados.modelos import PlayInputKind
from ..metadados.provedores import classify_play_input, describe_url
from ..nucleo.erros import MusicExtractionError
from ..nucleo.modelos import ExtractedBatch, MusicTrack
from ..nucleo.playlist_virtual import bounded_initial_window
from ..reproducao.playlist_virtual import (
    consulta_agent_para_faixa,
    payload_cursor_playlist,
    payload_faixa_agent,
    schedule_playlist_refill_from_result,
)

logger = logging.getLogger(__name__)


def agente_nao_pronto_transitorio(exc: Exception | str) -> bool:
    text = str(exc or "").strip().lower()
    return bool(text) and (
        "a música ainda não está pronta" in text
        or "musica ainda nao esta pronta" in text
        or "timed out" in text
        or "timeout" in text
        or "connect" in text
        or "connection" in text
        or "reconect" in text
        or "closing transport" in text
        or "refused" in text
    )


class FluxoTocar:
    """Fluxo do comando de reprodução.

    A VPS apenas orquestra metadata, seleção e comandos do Music Agent. A
    reprodução real continua pertencendo ao Phone Worker.
    """
    def _music_agent_default_enabled(self) -> bool:
        return bool(getattr(config, "MUSIC_AGENT_ENABLED", True)) and getattr(self.router, "music_worker_only_enabled", lambda: False)()

    def _schedule_music_agent_prefetch(
        self,
        guild_id: int,
        tracks: list[MusicTrack],
        *,
        voice_channel_id: int = 0,
        text_channel_id: int = 0,
        requester_id: int = 0,
        requester_name: str = "",
    ) -> None:
        if not self._music_agent_default_enabled() or not bool(getattr(config, "MUSIC_AGENT_PREFETCH_ENABLED", True)):
            return
        try:
            limit = max(0, min(3, int(getattr(config, "MUSIC_AGENT_PREFETCH_TOP_RESULTS", 1) or 0)))
        except Exception:
            limit = 1
        if limit <= 0 or not tracks:
            return

        def serialize(track: MusicTrack) -> dict:
            return {
                "title": track.title,
                "webpage_url": track.webpage_url,
                "original_url": track.original_url,
                "stream_url": track.stream_url,
                "duration": track.duration,
                "uploader": track.uploader,
                "thumbnail": track.thumbnail,
                "source": track.source,
                "extractor": track.extractor,
                "requester_id": requester_id or track.requester_id,
                "requester_name": requester_name or track.requester_name,
            }

        payload_tracks = [serialize(track) for track in tracks[:limit]]

        async def runner() -> None:
            started = time.monotonic()
            try:
                result = await music_agent_command(
                    "prefetch",
                    guild_id=guild_id,
                    voice_channel_id=voice_channel_id,
                    text_channel_id=text_channel_id,
                    requester_id=requester_id,
                    requester_name=requester_name,
                    tracks=payload_tracks,
                    limit=len(payload_tracks),
                    prefetch_kind="selection",
                    timeout_seconds=getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 5.0),
                )
                logger.info(
                    "[music/timing] prefetch solicitado | guild=%s tracks=%s accepted=%s elapsed_ms=%.1f",
                    guild_id,
                    len(payload_tracks),
                    result.get("accepted") if isinstance(result, dict) else "?",
                    (time.monotonic() - started) * 1000.0,
                )
            except Exception as exc:
                logger.debug("[music/timing] prefetch ignorado | guild=%s erro=%s", guild_id, exc)

        asyncio.create_task(runner())


    def _music_agent_guild_state(self, payload: dict, guild_id: int) -> dict:
        guilds = payload.get("guilds") if isinstance(payload, dict) else {}
        if not isinstance(guilds, dict):
            return {}
        state = guilds.get(str(guild_id)) or guilds.get(guild_id)
        return state if isinstance(state, dict) else {}

    @staticmethod
    def _music_agent_confirmed_playing(state: dict) -> bool:
        if str(state.get("status") or "").lower() != "playing":
            return False
        if "confirmed_playing" in state:
            return bool(state.get("confirmed_playing"))
        if "voice_connected" in state or "player_present" in state:
            return bool(state.get("voice_connected")) and bool(state.get("player_present"))
        return False

    def _music_agent_play_message(self, track: MusicTrack, result: dict | None = None) -> str:
        result = result or {}
        queued = bool(result.get("queued"))
        state = result.get("state") if isinstance(result.get("state"), dict) else {}
        current = state.get("current") if isinstance(state.get("current"), dict) else {}

        def useful(value: object) -> str:
            text = str(value or "").strip()
            lower = text.lower()
            if lower in {"youtube", "link", "música", "musica", "desconhecida", "unknown"}:
                return ""
            if "desconhecida" in lower and ("youtube" in lower or "worker" in lower):
                return ""
            if lower.startswith("youtube •") or lower.startswith("youtube -"):
                return ""
            return text

        # Quando o Music Agent responde queued=True, ``state.current`` continua
        # sendo a música que já estava tocando. A confirmação de "adicionada ao
        # queue" precisa usar a faixa selecionada/adicionada, não now-playing.
        queued_track = result.get("track") if isinstance(result.get("track"), dict) else {}
        queue_preview = state.get("queue") if isinstance(state.get("queue"), list) else []
        if queued and not queued_track and queue_preview:
            last = queue_preview[-1]
            queued_track = last if isinstance(last, dict) else {}
        source_payload = queued_track if queued else current
        title = (
            useful(source_payload.get("display_title"))
            or useful(source_payload.get("title"))
            or useful(source_payload.get("name"))
            or useful(getattr(track, "display_title", ""))
            or useful(track.short_title)
        )
        if len(title) > 90:
            title = title[:87].rstrip() + "..."
        duration_label = track.duration_label
        try:
            duration_source = source_payload if isinstance(source_payload, dict) else {}
            duration = duration_source.get("duration")
            if duration is None or str(duration).strip() == "":
                duration = getattr(track, "duration", None)
            if duration is not None and str(duration).strip() != "":
                total = max(0, int(float(duration)))
                minutes, seconds = divmod(total, 60)
                hours, minutes = divmod(minutes, 60)
                duration_label = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
        except Exception:
            pass
        status = str(state.get("status") or "").lower()
        if queued:
            if not title or title.lower() in {"youtube", "desconhecida", "música", "musica"}:
                return "`🎶` **Música adicionada ao queue.** Carregando detalhes..."
            return f"`🎶` **Adicionada ao queue:** {title} • `{duration_label}`"
        if self._music_agent_confirmed_playing(state):
            return f"`🎧` **Tocando:** {title} • `{duration_label}`"
        if status in {"failed", "error"}:
            error = str(state.get("last_error") or "fonte de áudio falhou").strip()[:180]
            lower_error = error.lower()
            if any(token in lower_error for token in ("music agent", "configure music_agent", "sem token", "pynacl", "davey", "dependency", "unauthorized")):
                error = "backend musical ainda não está pronto"
            return f"`⚠️` Não consegui iniciar **{title}**: `{error}`"
        return f"`🎧` **Preparando para tocar:** {title} • `{duration_label}`"

    async def _sync_music_agent_panel(self, guild_id: int, track: MusicTrack, result: dict | None, *, voice_channel_id: int = 0, text_channel_id: int = 0, queued: bool = False) -> None:
        state = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
        syncer = getattr(self.router, "sync_music_agent_state", None)
        if callable(syncer):
            await syncer(
                guild_id,
                track,
                state,
                voice_channel_id=voice_channel_id,
                text_channel_id=text_channel_id,
                queued=queued,
                create_panel=True,
            )

    async def _watch_music_agent_message(self, message, guild_id: int, track: MusicTrack, *, voice_channel_id: int = 0, text_channel_id: int = 0, seconds: float | None = None, loading_reaction: MusicLoadingReaction | None = None) -> None:
        limit = float(seconds or getattr(config, "MUSIC_AGENT_PLAY_STATUS_WATCH_SECONDS", 30.0) or 30.0)
        deadline = asyncio.get_running_loop().time() + max(5.0, limit)
        last_status = ""
        remote_poll = max(0.4, min(1.5, float(getattr(config, "MUSIC_AGENT_STATUS_POLL_SECONDS", 0.75) or 0.75)))
        local_poll = max(0.08, min(0.25, remote_poll / 4.0))
        while asyncio.get_running_loop().time() < deadline:
            shared_monitor = monitor_music_agent_ativo(self.router, guild_id)
            await asyncio.sleep(local_poll if shared_monitor else remote_poll)
            try:
                if shared_monitor:
                    # O monitor por guild é o único polling contínuo; este watcher
                    # só observa o espelho local em memória.
                    state = estado_local_music_agent(self.router, guild_id)
                else:
                    # Bootstrap/falha transitória: faça uma consulta de fallback.
                    # Assim que houver estado ativo, o sync inicia o monitor e as
                    # próximas iterações deixam de tocar na rede.
                    payload = await music_agent_status(
                        timeout_seconds=getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 3.5),
                        guild_id=guild_id,
                    )
                    state = self._music_agent_guild_state(payload, guild_id)
                    await self._sync_music_agent_panel(
                        guild_id,
                        track,
                        {"state": state},
                        voice_channel_id=voice_channel_id,
                        text_channel_id=text_channel_id,
                    )
                status = str(state.get("status") or "").lower()
                if not status or status == last_status:
                    continue
                last_status = status
                if self._music_agent_confirmed_playing(state):
                    await message.edit(content=self._music_agent_play_message(track, {"state": state}))
                    if loading_reaction is not None:
                        await loading_reaction.finish()
                    return
                if status in {"failed", "error"}:
                    error = str(state.get("last_error") or "fonte de áudio falhou").strip()[:220]
                    lower_error = error.lower()
                    if any(token in lower_error for token in ("music agent", "configure music_agent", "sem token", "pynacl", "davey", "dependency", "unauthorized")):
                        error = "backend musical ainda não está pronto"
                    await message.edit(content=f"`⚠️` Não consegui iniciar **{track.short_title}**: `{error}`")
                    if loading_reaction is not None:
                        await loading_reaction.finish()
                    return
                if status in {"idle", "stopped"} and not state.get("current"):
                    # Durante wake/playlist/resolve o worker pode piscar idle antes
                    # de publicar accepted/playing. Não envie erro público antes do
                    # timeout final; isso causava falso "worker não está pronto"
                    # enquanto a música começava logo depois.
                    continue
            except Exception:
                logger.debug("[music/agent] falha ao acompanhar estado remoto", exc_info=True)
        with contextlib.suppress(Exception):
            await message.edit(content=f"`⚠️` Demorei para confirmar o início de **{track.short_title}**. Tente novamente se não tocar.")
        if loading_reaction is not None:
            await loading_reaction.finish()
    def _is_lavalink_real_enabled(self, guild_id: int | None) -> bool:
        try:
            return bool(self.router.backends.should_use_lavalink_real(guild_id))
        except Exception:
            logger.debug("[music/lavalink] falha ao checar modo real", exc_info=True)
            return False

    def _query_profile(self, query: str):
        return describe_url((query or "").strip())

    def _play_input_kind(self, query: str) -> PlayInputKind:
        return classify_play_input((query or "").strip())

    def _is_youtube_link(self, query: str) -> bool:
        return bool(self._query_profile(query).is_youtube)

    def _is_youtube_text_search(self, query: str) -> bool:
        raw = (query or "").strip()
        if not raw:
            return False
        lower = raw.lower()
        if lower.startswith(("ytsearch:", "ytmsearch:")):
            return True
        if lower.startswith(("scsearch:", "spsearch:", "amsearch:", "dzsearch:")):
            return False
        profile = describe_url(raw)
        return not profile.is_url

    def _should_use_lavalink_for_input(self, query: str, guild_id: int | None) -> bool:
        if getattr(self.router, "music_worker_only_enabled", lambda: False)():
            # Worker-only é yt-dlp/Music Agent first. Spotify/álbuns/playlists são
            # convertidos para metadata + busca YouTube no worker, não para Lavalink.
            return False
        if not self._is_lavalink_real_enabled(guild_id):
            return False
        profile = self._query_profile(query)
        # YouTube direto nunca deve ir para Lavalink. O node fica reservado
        # para LavaSrc/SoundCloud; YouTube direto é player local yt-dlp.
        if profile.is_youtube:
            return False
        # Spotify/Deezer/Apple são links de metadata: primeiro o bot lê título,
        # artista e duração pela API. Só na hora do playback ele tenta espelhar
        # no LavaSrc/SoundCloud; assim não joga URL do Spotify crua no node nem
        # mostra erro inglês de `spsearch`/SpotifySourceManager no chat.
        if profile.is_metadata_only:
            return False
        # Pesquisa textual sempre mostra resultados do YouTube. A escolha do
        # usuário tenta espelho LavaSrc por autor+título exatos no playback e, se
        # não bater, cai para yt-dlp local.
        if self._is_youtube_text_search(query):
            return False
        return True

    def _lavalink_identifier_for_query(self, query: str) -> tuple[str, str, str, str]:
        """Cria uma faixa leve para o node resolver, sem chamar yt-dlp no comando.

        Em modo node de áudio, o node deve receber a URL/busca crua. Fazer
        ``_play`` passar antes pelo extractor local bloqueia o event loop
        (``voice heartbeat blocked``) e ainda pode trocar YouTube por SoundCloud.
        """
        raw = (query or "").strip()
        lower = raw.lower()
        known_prefixes = ("ytsearch:", "ytmsearch:", "scsearch:", "amsearch:", "dzsearch:", "spsearch:")
        for prefix in known_prefixes:
            if lower.startswith(prefix):
                body = raw[len(prefix):].strip()
                identifier = f"{prefix}{body}" if body else raw
                source = prefix[:-1]
                return identifier, (body or raw), source, ""

        profile = describe_url(raw)
        if profile.is_url:
            source = profile.platform or "lavalink"
            title = "YouTube" if profile.is_youtube else f"{source.title()} link" if source != "lavalink" else "Link"
            return profile.canonical or raw, title, source, profile.canonical or raw

        # Busca textual normal em node de áudio: usa SoundCloud/LavaSrc primeiro.
        # YouTube direto continua local; texto sem prefixo ganha seleção via Lavalink.
        return f"scsearch:{raw}", raw, "scsearch", ""

    def _lavalink_batch_for_query(self, query: str, *, requester_id: int, requester_name: str) -> ExtractedBatch:
        identifier, title, source, webpage_url = self._lavalink_identifier_for_query(query)
        track = MusicTrack(
            title=title or identifier or "Música",
            webpage_url=webpage_url,
            original_url=identifier,
            requester_id=requester_id,
            requester_name=requester_name,
            source=source or "lavalink",
            extractor="lavalink",
        )
        return ExtractedBatch(tracks=[track], query=identifier, is_playlist=False)

    def _worker_direct_url_batch(self, query: str, *, requester_id: int, requester_name: str) -> ExtractedBatch:
        profile = self._query_profile(query)
        title = "YouTube" if profile.is_youtube else "Link"
        track = MusicTrack(
            title=title,
            webpage_url=profile.canonical or query,
            original_url=query,
            requester_id=requester_id,
            requester_name=requester_name or "",
            source="YouTube" if profile.is_youtube else (profile.platform or "link"),
            extractor="worker-ytdlp",
        )
        return ExtractedBatch(tracks=[track], query=query, is_playlist=False)

    def _worker_url_should_flatten_first(self, query: str) -> bool:
        profile = self._query_profile(query)
        if not profile.is_url:
            return False
        if profile.is_youtube and profile.resource_type == "playlist":
            return True
        if profile.resource_type in {"playlist", "album"}:
            return True
        return False

    def _music_agent_query_for_track(self, track: MusicTrack, fallback: str = "") -> str:
        return consulta_agent_para_faixa(track, fallback)

    def _music_agent_tracks_payload(self, tracks: list[MusicTrack], *, requester_id: int = 0, requester_name: str = "") -> list[dict]:
        return [
            payload_faixa_agent(
                track,
                requester_id=requester_id,
                requester_name=requester_name,
            )
            for track in tracks
        ]

    def _is_lavalink_search_request(self, query: str) -> bool:
        raw = (query or "").strip()
        if not raw:
            return False
        lower = raw.lower()
        known_prefixes = ("ytsearch:", "ytmsearch:", "scsearch:", "amsearch:", "dzsearch:", "spsearch:")
        if lower.startswith(known_prefixes):
            return True
        return not describe_url(raw).is_url

    async def _lavalink_search_batch_for_query(
        self,
        ctx: commands.Context,
        query: str,
        *,
        requester_id: int,
        requester_name: str,
    ) -> ExtractedBatch:
        # Busca textual em node de áudio: consulta o node e preserva a lista
        # de resultados para o usuário escolher. Não usa yt-dlp nem escolhe
        # automaticamente o primeiro resultado.
        return await self.router.backends.search_lavalink_tracks(
            query,
            requester_id=requester_id,
            requester_name=requester_name,
            guild_id=getattr(ctx.guild, "id", None),
            limit=max(1, min(10, int(getattr(config, "MUSIC_SEARCH_RESULTS", 3) or 3))),
        )

    async def _run_play(self, ctx: commands.Context, query: str) -> None:
        """Implementação compartilhada de `_play` e da alias roteada `_p <música>`."""
        query = (query or "").strip()
        command_started = time.monotonic()
        if not query:
            await self._reply(ctx, "Use `_play <link ou pesquisa>`.")
            return

        voice_channel = await self._voice_channel_from_ctx(ctx)
        if voice_channel is None:
            await self._reply(ctx, "Entre em um canal de voz primeiro.")
            return

        operation_generation = self.router.current_music_operation_generation(ctx.guild.id)
        loading_reaction = MusicLoadingReaction(getattr(ctx, "message", None))
        await loading_reaction.start()
        finish_loading_reaction = True
        try:
            if getattr(self.router, "music_worker_only_enabled", lambda: False)():
                worker_check_started = time.monotonic()
                selection = await self.router.ensure_music_worker_available()
                logger.info("[music/timing] worker checado | guild=%s elapsed_ms=%.1f available=%s", ctx.guild.id, (time.monotonic() - worker_check_started) * 1000.0, getattr(selection, "available", False))
                if not getattr(selection, "available", False):
                    logger.info("[music/worker] play bloqueado: %s", getattr(selection, "reason", "worker indisponível"))
                    await self._reply(ctx, getattr(selection, "message", "") or getattr(self.router, "music_worker_unavailable_message", "Sistema de música indisponível no momento: Nenhum worker online"))
                    return

            input_profile = self._query_profile(query)
            input_kind = self._play_input_kind(query)

            # Shadow mode Lavalink: consulta o node em paralelo, mas mantém o áudio real
            # no player local atual. YouTube direto fica totalmente fora do LavaSrc/node
            # para não criar atraso nem mirror desnecessário.
            if not getattr(self.router, "music_worker_only_enabled", lambda: False)() and not input_profile.is_youtube:
                self.router.schedule_lavalink_shadow_search(
                    ctx.guild.id,
                    query,
                    requester_id=ctx.author.id,
                    requester_name=getattr(ctx.author, "display_name", str(ctx.author)),
                    reason="play_command",
                )

            requester_name = getattr(ctx.author, "display_name", str(ctx.author))

            if getattr(self.router, "music_worker_only_enabled", lambda: False)():
                try:
                    youtube_text_search = self._is_youtube_text_search(query)
                    if input_profile.is_metadata_only:
                        # Spotify/Deezer/Apple são metadata-only: lemos título/artista
                        # rapidamente e o Music Agent resolve cada faixa via yt-dlp/YouTube
                        # no worker. Nunca envie URL crua dessas plataformas ao Lavalink.
                        resolve_started = time.monotonic()
                        batch = await self.router.extractor.extract(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                        )
                        logger.info("[music/worker] metadata-only resolvido sem lavalink | guild=%s source=%s tracks=%s elapsed_ms=%.1f", ctx.guild.id, input_profile.platform, len(batch.tracks), (time.monotonic() - resolve_started) * 1000.0)
                    elif self._worker_url_should_flatten_first(query):
                        resolve_started = time.monotonic()
                        batch = await resolve_music_tracks_on_worker(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                            limit=max(1, int(getattr(config, "MUSIC_MAX_PLAYLIST_ITEMS", 100) or 100)),
                            metadata_only=True,
                            allow_playlist=True,
                            guild_id=ctx.guild.id,
                        )
                        logger.info("[music/worker] playlist/link flat resolvido no worker | guild=%s tracks=%s elapsed_ms=%.1f", ctx.guild.id, len(batch.tracks), (time.monotonic() - resolve_started) * 1000.0)
                    elif input_profile.is_url and not youtube_text_search:
                        # Link direto de faixa: o Music Agent resolve o stream no phone worker
                        # no momento do play usando o caminho rápido de URL direta.
                        batch = self._worker_direct_url_batch(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                        )
                    else:
                        resolve_started = time.monotonic()
                        batch = await resolve_music_tracks_on_worker(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                            limit=(max(1, min(10, int(getattr(config, "MUSIC_SEARCH_RESULTS", 3) or 3))) if youtube_text_search else 1),
                            metadata_only=youtube_text_search,
                            guild_id=ctx.guild.id,
                        )
                        logger.info("[music/worker] resolve etapa concluída | guild=%s metadata_only=%s elapsed_ms=%.1f", ctx.guild.id, youtube_text_search, (time.monotonic() - resolve_started) * 1000.0)
                except MusicExtractionError as exc:
                    await self._reply(ctx, self._music_error_message(exc))
                    return
                except Exception as exc:
                    logger.exception("[music/worker] erro ao resolver música no worker")
                    await self._reply(ctx, self._music_error_message(exc))
                    return

            elif self._should_use_lavalink_for_input(query, ctx.guild.id):
                # LavaSrc/Lavalink fica responsável por SoundCloud/scsearch e por
                # links que o node resolve com segurança. Spotify cru passa antes
                # pela API do bot para não cair em erro bruto do SpotifySourceManager.
                try:
                    if self._is_lavalink_search_request(query):
                        try:
                            batch = await self._lavalink_search_batch_for_query(
                                ctx,
                                query,
                                requester_id=ctx.author.id,
                                requester_name=requester_name,
                            )
                        except Exception as lavalink_exc:
                            raw_lower = query.lower().strip()
                            explicit_lavalink = raw_lower.startswith(("scsearch:", "spsearch:", "amsearch:", "dzsearch:"))
                            if getattr(self.router, "music_worker_only_enabled", lambda: False)():
                                # Worker-only é uma fronteira dura: busca textual também
                                # fica no engine musical do worker. Nada de yt-dlp local na VPS.
                                raise lavalink_exc
                            if not self._is_youtube_text_search(query) or explicit_lavalink:
                                raise
                            logger.warning(
                                "[music/lavalink] busca textual no node falhou; fallback local yt-dlp | guild=%s query=%r erro=%s",
                                ctx.guild.id,
                                query,
                                lavalink_exc,
                            )
                            batch = await self.router.extractor.search_youtube(
                                query,
                                requester_id=ctx.author.id,
                                requester_name=requester_name,
                            )
                        if not batch.tracks and self._is_youtube_text_search(query):
                            raw_lower = query.lower().strip()
                            explicit_lavalink = raw_lower.startswith(("scsearch:", "spsearch:", "amsearch:", "dzsearch:"))
                            if getattr(self.router, "music_worker_only_enabled", lambda: False)():
                                batch = ExtractedBatch(tracks=[], query=query, is_playlist=False)
                            elif not explicit_lavalink:
                                logger.info(
                                    "[music/lavalink] busca textual no node vazia; fallback local yt-dlp | guild=%s query=%r",
                                    ctx.guild.id,
                                    query,
                                )
                                batch = await self.router.extractor.search_youtube(
                                    query,
                                    requester_id=ctx.author.id,
                                    requester_name=requester_name,
                                )
                    else:
                        batch = await self.router.backends.resolve_lavalink_direct_tracks(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                            guild_id=getattr(ctx.guild, "id", None),
                            limit=max(1, int(getattr(config, "MUSIC_MAX_PLAYLIST_ITEMS", 25) or 25)),
                        )
                except MusicExtractionError as exc:
                    await self._reply(ctx, self._music_error_message(exc))
                    return
                except Exception as exc:
                    logger.exception("[music/lavalink] erro ao buscar no node")
                    await self._reply(ctx, self._music_error_message(exc))
                    return
            else:
                # YouTube direto e pesquisa textual usam yt-dlp/local para metadata.
                # Links do YouTube tocam direto pelo local; pesquisas abrem seleção
                # e, na reprodução, tentam mirror LavaSrc antes do fallback local.
                try:
                    if self._is_youtube_text_search(query):
                        batch = await self.router.extractor.search_youtube(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                        )
                    else:
                        batch = await self.router.extractor.extract(
                            query,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                        )
                except MusicExtractionError as exc:
                    await self._reply(ctx, self._music_error_message(exc))
                    return
                except Exception as exc:
                    logger.exception("[music] erro inesperado na extração")
                    await self._reply(ctx, self._music_error_message(exc))
                    return

            if self.router.current_music_operation_generation(ctx.guild.id) != operation_generation:
                logger.info("[music] play ignorado: operação antiga cancelada | guild=%s query=%r", ctx.guild.id, query)
                return

            if not batch.tracks:
                await self._reply(ctx, "`📭` Não encontrei nada tocável.")
                return

            # Playlist Spotify também alimenta a mesma memória agressiva de
            # links diretos: título, título limpo e primeira palavra útil viram
            # direct-hit de `_play`, sem API de busca na próxima chamada.
            if batch.is_playlist and input_profile.platform == "spotify":
                with contextlib.suppress(Exception):
                    registrar_lote_link_busca(batch.tracks)

            virtual_playlist_cursor = getattr(batch, "playlist_cursor", None)
            if (
                getattr(self.router, "music_worker_only_enabled", lambda: False)()
                and batch.is_playlist
                and virtual_playlist_cursor is not None
                and not virtual_playlist_cursor.exhausted
            ):
                window_tracks, virtual_playlist_cursor = bounded_initial_window(
                    batch.tracks,
                    virtual_playlist_cursor,
                )
                batch.tracks = window_tracks
                batch.playlist_cursor = virtual_playlist_cursor
                batch.truncated = True
                logger.info(
                    "[music/playlist] primeira janela materializada | guild=%s tracks=%s next_offset=%s",
                    ctx.guild.id,
                    len(batch.tracks),
                    getattr(virtual_playlist_cursor, "next_offset", 0),
                )

            # `input_profile` já classificou URL/texto antes da resolução.
            # Reusar esse resultado evita materializar o extrator local legado
            # apenas para chamar `looks_like_url()` no caminho Worker-only.
            # Somente pesquisa textual pode abrir seleção. Links são direct
            # play por contrato — inclusive Spotify track/playlist — mesmo que
            # a resolução interna gere vários candidatos/faixas.
            should_open_selection = bool(
                input_kind is PlayInputKind.SEARCH
                and len(batch.tracks) > 1
                and (
                    self._is_lavalink_search_request(query)
                    or self._is_youtube_text_search(query)
                    or not input_profile.is_url
                )
            )
            if should_open_selection:
                logger.info("[music/timing] resultados prontos | guild=%s elapsed_ms=%.1f tracks=%s", ctx.guild.id, (time.monotonic() - command_started) * 1000.0, len(batch.tracks))
                embed = discord.Embed(
                    title="🔎 Escolha a música",
                    description="Selecione um dos resultados abaixo.",
                    color=discord.Color.blurple(),
                )
                for idx, track in enumerate(batch.tracks[:10], start=1):
                    embed.add_field(
                        name=f"{idx}. {track.short_title}",
                        value=f"{track.uploader or track.source or 'resultado'} • `{track.duration_label}`",
                        inline=False,
                    )
                # Inicie o top-1 especulativo antes do POST da mensagem ao
                # Discord. Assim resolução yt-dlp e latência da UI se sobrepõem.
                self._schedule_music_agent_prefetch(
                    ctx.guild.id,
                    batch.tracks[:10],
                    voice_channel_id=voice_channel.id,
                    text_channel_id=ctx.channel.id,
                    requester_id=ctx.author.id,
                    requester_name=requester_name,
                )
                await self._reply(
                    ctx,
                    embed=embed,
                    view=SearchResultView(self.router, ctx.guild.id, voice_channel.id, ctx.channel.id, batch.tracks[:10], ctx.author.id, query),
                )
                return

            if bool(getattr(config, "MUSIC_AGENT_ENABLED", True)) and getattr(self.router, "music_worker_only_enabled", lambda: False)():
                track = batch.tracks[0]
                virtual_cursor = getattr(batch, "playlist_cursor", None)
                virtual_active = bool(batch.is_playlist and virtual_cursor is not None and not virtual_cursor.exhausted)
                is_multi = bool(len(batch.tracks) > 1 or virtual_active)
                try:
                    agent_started = time.monotonic()
                    if is_multi:
                        tracks_payload = self._music_agent_tracks_payload(
                            batch.tracks,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                        )
                        if virtual_active:
                            tracks_payload.append(
                                payload_cursor_playlist(
                                    virtual_cursor,
                                    requester_id=ctx.author.id,
                                    requester_name=requester_name,
                                )
                            )
                        result = await music_agent_command(
                            "enqueue_many",
                            guild_id=ctx.guild.id,
                            voice_channel_id=voice_channel.id,
                            text_channel_id=ctx.channel.id,
                            query=query,
                            track=track,
                            tracks=tracks_payload,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                        )
                    else:
                        result = await music_agent_command(
                            "play",
                            guild_id=ctx.guild.id,
                            voice_channel_id=voice_channel.id,
                            text_channel_id=ctx.channel.id,
                            query=self._music_agent_query_for_track(track, query),
                            track=track,
                            requester_id=ctx.author.id,
                            requester_name=requester_name,
                        )
                    logger.info("[music/agent] play etapa concluída | guild=%s elapsed_ms=%.1f queued=%s added=%s cancelled=%s", ctx.guild.id, (time.monotonic() - agent_started) * 1000.0, bool(result.get("queued")), result.get("added"), bool(result.get("cancelled")))
                    if isinstance(result, dict) and result.get("cancelled"):
                        logger.info("[music/agent] play cancelado antes de publicar resposta | guild=%s query=%r", ctx.guild.id, query)
                        return
                    if isinstance(result, dict) and result.get("ok") is False and result.get("error"):
                        await self._reply(ctx, self._music_error_message(Exception(str(result.get("error")))))
                        return
                except Exception as exc:
                    logger.warning("[music/agent] falha ao enviar play direto | guild=%s erro=%s", ctx.guild.id, exc)
                    if agente_nao_pronto_transitorio(exc):
                        msg = await self._reply(ctx, self._music_agent_play_message(track, {"state": {"status": "preparing"}}))
                        if msg is not None:
                            finish_loading_reaction = False
                            asyncio.create_task(self._watch_music_agent_message(msg, ctx.guild.id, track, voice_channel_id=voice_channel.id, text_channel_id=ctx.channel.id, loading_reaction=loading_reaction))
                        return
                    await self._reply(ctx, self._music_error_message(exc))
                    return
                if virtual_active:
                    # O comando do worker só retorna após iniciar/enfileirar a
                    # primeira faixa. A partir daqui podemos preencher o restante
                    # da janela em background sem competir com o start inicial.
                    if schedule_playlist_refill_from_result(self.router, ctx.guild.id, result):
                        logger.info("[music/playlist] refill start-first agendado | guild=%s", ctx.guild.id)
                await self._sync_music_agent_panel(
                    ctx.guild.id,
                    track,
                    result,
                    voice_channel_id=voice_channel.id,
                    text_channel_id=ctx.channel.id,
                    queued=bool(result.get("queued")),
                )
                if is_multi:
                    added = int(result.get("added") or len(batch.tracks))
                    title = (batch.playlist_title or "playlist").strip()
                    label = f" de **{discord.utils.escape_markdown(title[:80])}**" if title else ""
                    state_payload = result.get("state") if isinstance(result.get("state"), dict) else {}
                    try:
                        queue_total = int(state_payload.get("queue_size") or 0)
                    except Exception:
                        queue_total = 0
                    if virtual_active:
                        if bool(result.get("queued")):
                            msg = await self._reply(ctx, f"`📑` **Playlist adicionada à fila{label}.**")
                        else:
                            msg = await self._reply(ctx, f"`📑` **Playlist iniciada{label}.**")
                    else:
                        count_label = "música" if added == 1 else "músicas"
                        if bool(result.get("queued")):
                            total_line = f"\n`🎶` Queue agora: `{queue_total}` música(s)." if queue_total else ""
                            msg = await self._reply(
                                ctx,
                                f"`📑` **Playlist adicionada ao final do queue:** `{added}` {count_label}{label}.{total_line}",
                            )
                        else:
                            msg = await self._reply(
                                ctx,
                                f"`📑` **Playlist adicionada ao queue:** `{added}` {count_label}{label}.\n`🎧` Preparando a primeira faixa...",
                            )
                else:
                    msg = await self._reply(ctx, self._music_agent_play_message(track, result))
                state = result.get("state") if isinstance(result.get("state"), dict) else {}
                status = str(state.get("status") or "").lower()
                confirmed = self._music_agent_confirmed_playing(state)
                if msg is not None and not result.get("queued") and not confirmed and status not in {"failed", "error"}:
                    finish_loading_reaction = False
                    asyncio.create_task(self._watch_music_agent_message(msg, ctx.guild.id, track, voice_channel_id=voice_channel.id, text_channel_id=ctx.channel.id, loading_reaction=loading_reaction))
                logger.info("[music/timing] play enviado | guild=%s elapsed_ms=%.1f queued=%s confirmed=%s", ctx.guild.id, (time.monotonic() - command_started) * 1000.0, bool(result.get("queued")), confirmed)
                return

            state_before = self.router.get_state(ctx.guild.id)
            was_session_active = bool(
                state_before.current
                or state_before.queue_size() > 0
                or getattr(state_before, "current_status", "") in {"resolving", "starting", "playing", "paused", "queued"}
            )
            added, dropped = await self.router.enqueue(ctx.guild, voice_channel, ctx.channel, batch.tracks)
            if added <= 0:
                await self._reply(ctx, "`⚠️` Não adicionei nada: o queue está cheio ou essa música já está no queue/tocando.")
                return

            if batch.is_playlist:
                count_label = "música" if added == 1 else "músicas"
                playlist_title = (batch.playlist_title or "").strip()
                if playlist_title:
                    desc = f"`📑` **Playlist adicionada:** `{added}` {count_label} de **{playlist_title}**"
                else:
                    desc = f"`📑` **Adicionadas ao queue:** `{added}` {count_label}"
                if batch.truncated:
                    desc += f"\n`⚠️` Playlist limitada aos primeiros `{getattr(config, 'MUSIC_MAX_PLAYLIST_ITEMS', 100)}` itens para não pesar o bot."
                if dropped:
                    desc += f"\n`⚠️` `{dropped}` item(ns) não entraram porque já estavam no queue/tocando ou porque o queue está cheio."
                await self._reply(ctx, desc)
            else:
                track = batch.tracks[0]
                state = self.router.get_state(ctx.guild.id)
                position = state.queue_size() + (1 if state.current else 0)
                if was_session_active:
                    await self._reply(ctx, f"`🎶` **Adicionada ao queue:** {track.short_title} • `{track.duration_label}` • posição `{max(1, position)}`")
                else:
                    # O worker pode pegar a primeira música imediatamente após enqueue,
                    # fazendo ``state.current`` existir antes da resposta do comando. Isso
                    # não significa posição 2; ainda é a faixa que está iniciando agora.
                    await self._reply(ctx, f"`🎧` **Preparando para tocar:** {track.short_title} • `{track.duration_label}`")
        finally:
            if finish_loading_reaction:
                await loading_reaction.finish()
