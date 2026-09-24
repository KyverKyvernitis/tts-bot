from __future__ import annotations

import asyncio
import contextlib
import math
import logging
import time
from typing import Optional

import discord

from cogs.musica import configuracao as config

from ..nucleo.erros import MusicExtractionError
from ..busca import registrar_lote_link_busca, registrar_selecao_busca
from ..nucleo.modelos import ExtractedBatch, MusicTrack
from ..nucleo.playlist_virtual import bounded_initial_window, logical_virtual_queue_count
from ..metadados.provedores import describe_url
from ..agente_telefone.comandos import estado_comando_diferido, music_agent_command, music_agent_status
from ..agente_telefone.monitor import estado_local_music_agent, monitor_music_agent_ativo
from ..reproducao.controle_remoto import enviar_controle_remoto
from ..reproducao.playlist_virtual import (
    carregar_pagina_fila_virtual,
    payload_cursor_playlist,
    schedule_playlist_refill_from_result,
)
from ..agente_telefone.resolucao import resolve_music_tracks_on_worker
from .carregamento import MusicLoadingReaction
from .tarefas import agendar_tarefa_unica

PLAYER_BAR_URL = "https://cdn.discordapp.com/attachments/554468640942981147/1127294696025227367/rainbow_bar3.gif"
PLAYER_STATUS_ANIMATED_URL = "https://i.ibb.co/QXtk5VB/neon-circle.gif"
# Components V2 não aceita uma URL de imagem inline dentro de TextDisplay.
# O estado fica ao lado do título usando apenas emojis já resolvidos localmente;
# o spinner animado é reservado para estados de carregamento, não para playback.
PLAYER_STATUS_ANIMATED_EMOJI = "<a:loading:1510065277868445796>"
PLAYER_STATUS_PLAYING_EMOJI = "<a:circulando:1551635281738858670>"
PLAYER_QUEUE_FINISHED_EMOJI = "<:Barra:1548838704850800712>"
PLAYER_PAUSED_ICON_URL = "https://cdn.discordapp.com/attachments/480195401543188483/896013933197013002/pause.png"
PLAYER_ERROR_ICON_URL = "https://cdn.discordapp.com/emojis/1215703754471268414.png"
QUEUE_PAGE_SIZE = 8
# Já resolvido em memória no import: nenhuma chamada de API/fetch de emoji é feita
# quando o menu de resultados é montado.
YOUTUBE_SEARCH_OPTION_EMOJI = config.MUSIC_SOURCE_EMOJIS.get("youtube") or config.MUSIC_SOURCE_EMOJI_FALLBACK
logger = logging.getLogger(__name__)


def _agent_guild_state(payload: dict, guild_id: int) -> dict:
    guilds = payload.get("guilds") if isinstance(payload, dict) else {}
    if not isinstance(guilds, dict):
        return {}
    state = guilds.get(str(guild_id)) or guilds.get(guild_id)
    return state if isinstance(state, dict) else {}


def _agent_confirmed_playing(state: dict) -> bool:
    if str(state.get("status") or "").lower() != "playing":
        return False
    if "confirmed_playing" in state:
        return bool(state.get("confirmed_playing"))
    if "voice_connected" in state or "player_present" in state:
        return bool(state.get("voice_connected")) and bool(state.get("player_present"))
    return False


def _agent_not_ready_transient(exc: Exception | str) -> bool:
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


async def _safe_interaction_followup(interaction: discord.Interaction, content: str, *, ephemeral: bool = True, **kwargs):
    try:
        return await interaction.followup.send(content, ephemeral=ephemeral, **kwargs)
    except discord.NotFound:
        return None
    except Exception:
        logger.debug("[music/ui] falha ao enviar followup", exc_info=True)
        return None


def _agent_play_message(track: MusicTrack, result: dict | None = None) -> str:
    result = result or {}
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

    queued = bool(result.get("queued"))
    # Em queued=True o estado remoto ainda descreve a música atual. A confirmação
    # precisa renderizar o candidato que acabou de entrar na fila, não now-playing.
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
    if _agent_confirmed_playing(state):
        return f"`🎧` **Tocando:** {title} • `{duration_label}`"
    if status in {"failed", "error"}:
        error = str(state.get("last_error") or "fonte de áudio falhou").strip()[:180]
        return f"`⚠️` Não consegui iniciar **{title}**: `{error}`"
    return f"`🎧` **Preparando para tocar:** {title} • `{duration_label}`"


async def _sync_agent_panel(router, guild_id: int, voice_channel_id: int, text_channel_id: int, track: MusicTrack, result: dict | None, *, queued: bool = False) -> None:
    state = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
    syncer = getattr(router, "sync_music_agent_state", None)
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


def _schedule_agent_prefetch(
    router,
    guild_id: int,
    tracks: list[MusicTrack],
    *,
    voice_channel_id: int = 0,
    text_channel_id: int = 0,
    requester_id: int = 0,
    requester_name: str = "",
) -> None:
    if not bool(getattr(config, "MUSIC_AGENT_ENABLED", True)) or not getattr(router, "music_worker_only_enabled", lambda: False)():
        return
    if not bool(getattr(config, "MUSIC_AGENT_PREFETCH_ENABLED", True)):
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
                "[music/timing] prefetch solicitado por UI | guild=%s tracks=%s accepted=%s elapsed_ms=%.1f",
                guild_id,
                len(payload_tracks),
                result.get("accepted") if isinstance(result, dict) else "?",
                (time.monotonic() - started) * 1000.0,
            )
        except Exception as exc:
            logger.debug("[music/timing] prefetch UI ignorado | guild=%s erro=%s", guild_id, exc)

    agendar_tarefa_unica(("prefetch", int(guild_id)), runner())


async def _watch_agent_message(message, guild_id: int, track: MusicTrack, *, router=None, voice_channel_id: int = 0, text_channel_id: int = 0, seconds: float | None = None, loading_reaction: MusicLoadingReaction | None = None) -> None:
    limit = float(seconds or getattr(config, "MUSIC_AGENT_PLAY_STATUS_WATCH_SECONDS", 30.0) or 30.0)
    deadline = asyncio.get_running_loop().time() + max(5.0, limit)
    last_status = ""
    remote_poll = max(0.4, min(1.5, float(getattr(config, "MUSIC_AGENT_STATUS_POLL_SECONDS", 0.75) or 0.75)))
    local_poll = max(0.08, min(0.25, remote_poll / 4.0))
    try:
        while asyncio.get_running_loop().time() < deadline:
            shared_monitor = bool(router is not None and monitor_music_agent_ativo(router, guild_id))
            await asyncio.sleep(local_poll if shared_monitor else remote_poll)
            try:
                if shared_monitor:
                    state = estado_local_music_agent(router, guild_id)
                else:
                    payload = await music_agent_status(
                        timeout_seconds=getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 3.5),
                        guild_id=guild_id,
                    )
                    state = _agent_guild_state(payload, guild_id)
                    if router is not None:
                        await _sync_agent_panel(router, guild_id, voice_channel_id, text_channel_id, track, {"state": state})
                status = str(state.get("status") or "").lower()
                if not status or status == last_status:
                    continue
                last_status = status
                if _agent_confirmed_playing(state):
                    await message.edit(content=_music_agent_play_message(track, {"state": state}), embed=None, view=None)
                    return
                if status in {"failed", "error"}:
                    error = str(state.get("last_error") or "fonte de áudio falhou").strip()[:220]
                    await message.edit(content=f"`⚠️` Não consegui iniciar **{track.short_title}**: `{error}`", embed=None, view=None)
                    return
                if status in {"idle", "stopped"} and not state.get("current"):
                    # Idle pode chegar entre accepted/queued/playing quando o worker ainda
                    # está acordando ou quando a faixa acabou antes do painel atualizar.
                    # Não envie erro público antes do timeout final.
                    continue
            except discord.NotFound:
                return
            except Exception:
                # Não quebra o fluxo do usuário se o acompanhamento não conseguir consultar o worker.
                continue
        with contextlib.suppress(Exception):
            local = estado_local_music_agent(router, guild_id) if router is not None else {}
            deferred = estado_comando_diferido(guild_id)
            if str(local.get("status") or "").lower() == "reconnecting" and str(deferred.get("status") or "") == "pending":
                attempts = max(0, int(deferred.get("attempts") or 0))
                suffix = f" (tentativa {attempts})" if attempts else ""
                await message.edit(content=f"`🔄` **{track.short_title}** ainda não começou porque a conexão do player caiu{suffix}. O comando será reenviado automaticamente assim que a conexão voltar.", embed=None, view=None)
            else:
                await message.edit(content=f"`⚠️` Demorei para confirmar o início de **{track.short_title}**. Tente novamente se não tocar.", embed=None, view=None)
    finally:
        if loading_reaction is not None:
            with contextlib.suppress(Exception):
                await loading_reaction.finish()


_LAVALINK_SEARCH_PREFIXES = ("ytsearch:", "ytmsearch:", "scsearch:", "amsearch:", "dzsearch:", "spsearch:")


def _is_lavalink_search_request(query: str) -> bool:
    raw = (query or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    if lower.startswith(_LAVALINK_SEARCH_PREFIXES):
        return True
    return not describe_url(raw).is_url


def _lavalink_batch_for_direct_query(query: str, *, requester_id: int, requester_name: str) -> ExtractedBatch:
    raw = (query or "").strip()
    profile = describe_url(raw)
    if profile.is_url:
        source = profile.platform or "lavalink"
        title = "YouTube" if profile.is_youtube else (f"{source.title()} link" if source != "lavalink" else "Link")
        identifier = profile.canonical or raw
        webpage_url = profile.canonical or raw
    else:
        # Só é usado para fallback defensivo. Texto normal deve passar por busca
        # com seleção, não por autoplay do primeiro resultado.
        identifier = f"scsearch:{raw}"
        title = raw or identifier
        source = "scsearch"
        webpage_url = ""
    track = MusicTrack(
        title=title or identifier or "Música",
        webpage_url=webpage_url,
        original_url=identifier,
        requester_id=int(requester_id or 0),
        requester_name=requester_name or "",
        source=source or "lavalink",
        extractor="lavalink",
    )
    return ExtractedBatch(tracks=[track], query=identifier, is_playlist=False)


def _is_youtube_link(query: str) -> bool:
    return bool(describe_url((query or "").strip()).is_youtube)


def _is_youtube_text_search(query: str) -> bool:
    raw = (query or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    if lower.startswith(("ytsearch:", "ytmsearch:")):
        return True
    if lower.startswith(("scsearch:", "spsearch:", "amsearch:", "dzsearch:")):
        return False
    return not describe_url(raw).is_url


def _worker_only_should_use_lavalink(query: str) -> bool:
    # Worker-only é Music Agent/yt-dlp first. Spotify/playlists/SoundCloud não
    # devem cair em Lavalink só para resolver metadata.
    return False


def _worker_url_should_flatten_first(query: str) -> bool:
    profile = describe_url((query or "").strip())
    if not profile.is_url:
        return False
    if profile.is_youtube and profile.resource_type == "playlist":
        return True
    if profile.resource_type in {"playlist", "album"}:
        return True
    return False


def _agent_query_for_track(track: MusicTrack, fallback: str = "") -> str:
    extractor = str(getattr(track, "extractor", "") or "").lower()
    source = str(getattr(track, "source", "") or getattr(track, "display_source", "") or "").lower()
    is_metadata = extractor == "metadata" or any(token in source for token in ("spotify", "deezer", "apple", "metadata"))
    if is_metadata:
        base = str(getattr(track, "display_title", "") or getattr(track, "title", "") or fallback or "").strip()
        uploader = str(getattr(track, "display_uploader", "") or getattr(track, "uploader", "") or "").strip()
        if uploader and uploader.lower() not in base.lower():
            base = f"{uploader} {base}".strip()
        if base and "official" not in base.lower():
            base = f"{base} official audio"
        return base or fallback or getattr(track, "title", "") or ""
    return str(getattr(track, "webpage_url", "") or getattr(track, "original_url", "") or getattr(track, "stream_url", "") or getattr(track, "title", "") or fallback or "").strip()


def _agent_tracks_payload(tracks: list[MusicTrack], *, requester_id: int = 0, requester_name: str = "") -> list[dict]:
    payload: list[dict] = []
    for track in tracks:
        payload.append({
            "title": track.title,
            "webpage_url": track.webpage_url,
            "original_url": track.original_url,
            "stream_url": track.stream_url,
            "duration": track.duration,
            "uploader": track.uploader,
            "thumbnail": track.thumbnail,
            "source": track.source,
            "extractor": track.extractor,
            "display_title": getattr(track, "display_title", ""),
            "display_uploader": getattr(track, "display_uploader", ""),
            "display_source": getattr(track, "display_source", ""),
            "query": _agent_query_for_track(track),
            "requester_id": requester_id or track.requester_id,
            "requester_name": requester_name or track.requester_name,
            "queue_item_id": str(getattr(track, "queue_item_id", "") or ""),
        })
    return payload


async def _extract_batch_for_add_modal(router, guild_id: int, query: str, *, requester_id: int, requester_name: str) -> tuple[ExtractedBatch, bool]:
    """Resolve o input do modal respeitando o backend ativo.

    Em modo worker-only, todo input vai para o nó remoto do worker e não
    executa resolução/yt-dlp local na VPS. No modo legado, mantém o fluxo
    anterior de metadados e fallback local.
    """
    backends = getattr(router, "backends", None)
    if getattr(router, "music_worker_only_enabled", lambda: False)():
        selection = await router.ensure_music_worker_available()
        if not getattr(selection, "available", False):
            raise RuntimeError(getattr(selection, "message", "") or getattr(router, "music_worker_unavailable_message", "Sistema de música indisponível no momento: Nenhum worker online"))
        profile = describe_url(query)
        youtube_text_search = _is_youtube_text_search(query)
        if profile.is_metadata_only:
            batch = await router.extractor.extract(
                query,
                requester_id=requester_id,
                requester_name=requester_name,
            )
            return batch, False
        if _worker_url_should_flatten_first(query):
            batch = await resolve_music_tracks_on_worker(
                query,
                requester_id=requester_id,
                requester_name=requester_name,
                limit=max(1, int(getattr(config, "MUSIC_MAX_PLAYLIST_ITEMS", 100) or 100)),
                metadata_only=True,
                allow_playlist=True,
                guild_id=guild_id,
            )
            return batch, False
        batch = await resolve_music_tracks_on_worker(
            query,
            requester_id=requester_id,
            requester_name=requester_name,
            limit=(max(1, min(10, int(getattr(config, "MUSIC_SEARCH_RESULTS", 3) or 3))) if youtube_text_search else 1),
            metadata_only=youtube_text_search,
            guild_id=guild_id,
        )
        return batch, bool(youtube_text_search and len(batch.tracks) > 1)

    should_use_lavalink = getattr(backends, "should_use_lavalink_real", None)
    lavalink_active = bool(callable(should_use_lavalink) and should_use_lavalink(guild_id))

    if _is_youtube_text_search(query):
        # Pesquisa textual sempre mostra resultados reais do YouTube. Depois que o
        # usuário escolher, o playback tenta espelhar autor+título no LavaSrc; se
        # não houver correspondência exata, cai para o yt-dlp local.
        batch = await router.extractor.search_youtube(
            query,
            requester_id=requester_id,
            requester_name=requester_name,
        )
        return batch, True

    profile = describe_url(query)

    if lavalink_active and profile.is_metadata_only:
        batch = await router.extractor.extract(
            query,
            requester_id=requester_id,
            requester_name=requester_name,
        )
        return batch, False

    if lavalink_active and not profile.is_youtube:
        is_search = _is_lavalink_search_request(query)
        if is_search:
            batch = await backends.search_lavalink_tracks(
                query,
                requester_id=requester_id,
                requester_name=requester_name,
                guild_id=guild_id,
                limit=max(1, min(10, int(getattr(config, "MUSIC_SEARCH_RESULTS", 3) or 3))),
            )
            return batch, True
        batch = await backends.resolve_lavalink_direct_tracks(
            query,
            requester_id=requester_id,
            requester_name=requester_name,
            guild_id=guild_id,
            limit=max(1, int(getattr(config, "MUSIC_MAX_PLAYLIST_ITEMS", 25) or 25)),
        )
        return batch, False

    batch = await router.extractor.extract(
        query,
        requester_id=requester_id,
        requester_name=requester_name,
    )
    return batch, False




def _panel_controls_invalid(state) -> bool:
    try:
        invalid_at = float(getattr(state, "panel_controls_invalid_at", 0.0) or 0.0)
    except Exception:
        invalid_at = 0.0
    return bool(invalid_at > 0.0 and time.monotonic() >= invalid_at)


def _interaction_user_voice_channel(interaction: discord.Interaction):
    return getattr(getattr(getattr(interaction, "user", None), "voice", None), "channel", None)


def _interaction_bot_voice_channel(interaction: discord.Interaction, state=None):
    guild = getattr(interaction, "guild", None)
    vc = getattr(guild, "voice_client", None) if guild is not None else None
    channel = getattr(vc, "channel", None) if vc is not None else None
    if channel is not None:
        return channel
    player = getattr(state, "current_lavalink_player", None) if state is not None else None
    channel = getattr(player, "channel", None) if player is not None else None
    return channel


async def _send_interaction_notice(interaction: discord.Interaction, message: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.NotFound:
        return


async def _require_music_voice_interaction(
    interaction: discord.Interaction,
    router,
    guild_id: int,
    *,
    same_as_bot: bool = True,
    check_worker: bool = True,
) -> bool:
    state = router.get_state(guild_id)
    user_channel = _interaction_user_voice_channel(interaction)
    if user_channel is None:
        await _send_interaction_notice(interaction, "Entre em um canal de voz primeiro.")
        return False
    if same_as_bot:
        bot_channel = _interaction_bot_voice_channel(interaction, state)
        if bot_channel is not None and getattr(bot_channel, "id", None) != getattr(user_channel, "id", None):
            await _send_interaction_notice(interaction, "Entre no mesmo canal de voz do bot para usar isso.")
            return False
    if check_worker and getattr(router, "music_worker_only_enabled", lambda: False)():
        selection = await router.ensure_music_worker_available()
        if not getattr(selection, "available", False):
            message = getattr(selection, "message", "") or getattr(router, "music_worker_unavailable_message", "Sistema de música indisponível no momento: Nenhum worker online")
            await _send_interaction_notice(interaction, message)
            return False
    return True


def _current_track_requester_id(state) -> int:
    current = getattr(state, "current", None)
    try:
        return int(getattr(current, "requester_id", 0) or 0)
    except Exception:
        return 0

def _bar(percent: float, *, size: int = 12) -> str:
    percent = max(0.0, min(1.0, float(percent)))
    filled = int(round(percent * size))
    return "▰" * filled + "▱" * max(0, size - filled)


def _escape(value: str, *, limit: int | None = None) -> str:
    value = discord.utils.escape_markdown((value or "").strip()) or "sem título"
    if limit and len(value) > limit:
        return value[: max(0, limit - 3)].rstrip() + "..."
    return value


def _public_track_link_url(track: MusicTrack) -> str:
    """Retorna a URL pública da música, separada da fonte real do áudio.

    Em direct play por Spotify o áudio normalmente resolve para YouTube, mas a
    UI deve continuar levando à faixa individual do Spotify. Queries internas
    (``ytsearch1:...``) e URLs de coleção nunca viram hyperlinks.
    """
    values = (
        getattr(track, "original_url", ""),
        getattr(track, "webpage_url", ""),
        getattr(track, "display_url", ""),
    )

    # Se houver uma faixa Spotify individual, ela é a procedência pública
    # preferida mesmo quando ``webpage_url`` já virou o vídeo YouTube tocado.
    for raw in values:
        value = str(raw or "").strip()
        if not value.lower().startswith(("http://", "https://")):
            continue
        profile = describe_url(value)
        if profile.is_url and profile.platform == "spotify" and profile.resource_type == "track":
            return profile.canonical or value

    for raw in values:
        value = str(raw or "").strip()
        if not value.lower().startswith(("http://", "https://")):
            continue
        profile = describe_url(value)
        if not profile.is_url or profile.resource_type in {"playlist", "album"}:
            continue
        return profile.canonical or value
    return ""


def _track_link(track: MusicTrack, *, title_limit: int = 82) -> str:
    title = _escape(track.short_title or track.title, limit=title_limit)
    url = _public_track_link_url(track)
    if url:
        return f"[`{title}`]({url})"
    return f"`{title}`"




def _local_audio_format_label(track: MusicTrack) -> str:
    format_id = str(getattr(track, "resolved_audio_format_id", "") or "").strip()
    ext = str(getattr(track, "resolved_audio_ext", "") or "").strip().lower()
    codec = str(getattr(track, "resolved_audio_codec", "") or "").strip().lower()
    abr = 0
    with contextlib.suppress(Exception):
        abr = int(getattr(track, "resolved_audio_abr", 0) or 0)
    parts: list[str] = []
    if format_id:
        parts.append(format_id[:24])
    if ext and codec and codec not in {"none", ext}:
        short_codec = codec.split(".", 1)[0]
        parts.append(f"{ext}/{short_codec}")
    elif ext:
        parts.append(ext)
    elif codec and codec != "none":
        parts.append(codec.split(".", 1)[0])
    if abr:
        parts.append(f"{abr}kbps")
    return " · ".join(parts)

def _queue_items(state) -> list[MusicTrack]:
    items: list[MusicTrack] = []
    with contextlib.suppress(Exception):
        items.extend(list(getattr(state, "forward_queue", []) or []))
    with contextlib.suppress(Exception):
        items.extend(list(getattr(state.queue, "_queue", [])))
    return items


def _queue_total_count(state, items: list[MusicTrack]) -> int:
    total = len(items)
    with contextlib.suppress(Exception):
        total = max(total, int(state.queue_size()))
    with contextlib.suppress(Exception):
        total = max(total, int(getattr(state, "agent_remote_queue_size", 0) or 0))
    return max(0, total)


def _virtual_playlists_info(state) -> list[dict]:
    values = getattr(state, "agent_virtual_playlists", None)
    if isinstance(values, list):
        normalized = [value for value in values if isinstance(value, dict) and bool(value.get("active"))]
        if normalized:
            return normalized
    value = getattr(state, "agent_virtual_playlist", None)
    if isinstance(value, dict) and bool(value.get("active")):
        return [value]
    return []


def _virtual_playlist_info(state) -> dict:
    values = _virtual_playlists_info(state)
    return values[0] if values else {}


def _virtual_playlist_total_label(state) -> str:
    info = _virtual_playlist_info(state)
    if not info:
        return ""
    total = info.get("total_tracks")
    try:
        total_int = max(0, int(total)) if total not in (None, "") else 0
    except Exception:
        total_int = 0
    if total_int:
        return f"{total_int} música{'s' if total_int != 1 else ''}"
    return ""


def _queue_duration_label(items: list[MusicTrack]) -> str:
    total = 0
    unknown = False
    for track in items:
        if track.is_live or track.duration is None:
            unknown = True
            continue
        total += max(0, int(track.duration))
    if not total and unknown:
        return "desconhecida"
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    label = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    if unknown:
        label += "+"
    return label


def _source_key_for_track(track: MusicTrack | None) -> str:
    if track is None:
        return ""

    # A fonte explícita do stream vence a URL de origem. Isso importa para
    # direct play por metadata: uma faixa pedida via Spotify pode estar sendo
    # efetivamente reproduzida pelo YouTube/yt-dlp.
    primary = " ".join(
        str(getattr(track, attr, "") or "").strip().lower()
        for attr in ("display_source", "source", "extractor")
    )
    if "youtube" in primary or "yt-dlp" in primary or "ytdlp" in primary:
        return "youtube"
    if "soundcloud" in primary or "sound cloud" in primary:
        return "soundcloud"
    if "spotify" in primary:
        return "spotify"
    if "deezer" in primary:
        return "deezer"

    urls = " ".join(
        str(getattr(track, attr, "") or "").strip().lower()
        for attr in ("original_url", "webpage_url", "display_url", "stream_url")
    )
    if "youtube" in urls or "youtu.be" in urls or "ytmusic" in urls:
        return "youtube"
    if "soundcloud" in urls or "sound cloud" in urls:
        return "soundcloud"
    if "spotify" in urls:
        return "spotify"
    if "deezer" in urls:
        return "deezer"
    return ""


def _source_badge_for_track(track: MusicTrack | None) -> tuple[str, str]:
    key = _source_key_for_track(track)
    emoji = config.MUSIC_SOURCE_EMOJIS.get(key) or config.MUSIC_SOURCE_EMOJI_FALLBACK
    label = {
        "youtube": "YouTube",
        "spotify": "Spotify",
        "deezer": "Deezer",
        "soundcloud": "SoundCloud",
    }.get(key, "Áudio")
    return emoji, label


def _public_audio_quality_label(state, track: MusicTrack | None) -> str:
    if track is None:
        return ""
    ext = str(getattr(track, "resolved_audio_ext", "") or "").strip().lower()
    codec = str(getattr(track, "resolved_audio_codec", "") or "").strip().lower()
    abr = 0
    with contextlib.suppress(Exception):
        abr = int(float(getattr(track, "resolved_audio_abr", 0) or 0))

    codec_label = ""
    if codec and codec != "none":
        short_codec = codec.split(".", 1)[0]
        codec_label = {
            "opus": "Opus",
            "aac": "AAC",
            "mp3": "MP3",
            "vorbis": "Vorbis",
        }.get(short_codec, short_codec.upper())
    elif ext:
        codec_label = ext.upper()

    backend = str(getattr(state, "current_backend", "") or "").lower()
    if not abr and backend == "agent":
        with contextlib.suppress(Exception):
            abr = int(float(getattr(state, "current_quality_kbps", 0) or 0))

    parts: list[str] = []
    if codec_label:
        parts.append(codec_label)
    if abr:
        parts.append(f"{abr} kbps")
    if parts:
        return " · ".join(parts)

    quality = str(getattr(state, "current_quality_label", "") or "").strip() if backend == "agent" else ""
    return quality or ("Resolvendo áudio" if str(getattr(state, "current_status", "") or "") in {"resolving", "starting"} else "")


def _track_link_v2(track: MusicTrack, *, title_limit: int = 84, bold: bool = False) -> str:
    title = _escape(track.short_title or track.title, limit=title_limit)
    label = f"**{title}**" if bold else title
    url = _public_track_link_url(track)
    if url:
        return f"[{label}]({url})"
    return label


def _player_status_presentation(state) -> tuple[str, str, discord.Colour]:
    """Retorna título, emoji inline e cor do estado do player.

    O embed antigo podia usar ``set_author(..., icon_url=...)``. Components V2
    não possui author icon e TextDisplay não renderiza uma URL arbitrária como
    imagem inline; por isso o equivalente correto é um emoji Discord no próprio
    texto do cabeçalho. Todos os valores são locais, sem fetch ou request.
    """
    status = str(getattr(state, "current_status", "playing") or "playing").lower()
    paused = bool(getattr(state, "paused", False)) or status == "paused"
    if status == "error":
        return "Erro no player", "❌", discord.Color.red()
    if status == "skipping":
        return "Pulando música", "⏭️", discord.Color.gold()
    if status == "reconnecting":
        return "Restabelecendo conexão do player", "🔄", discord.Color.gold()
    if bool(getattr(state, "agent_voice_recovery_pending", False)):
        return "Reconectando ao canal de voz", "🔄", discord.Color.gold()
    if status in {"resolving", "starting"}:
        return "Preparando áudio", PLAYER_STATUS_ANIMATED_EMOJI, discord.Color.gold()
    if paused:
        return "Em pausa", "⏸️", discord.Color.gold()
    if getattr(state, "current", None) is not None:
        return "Tocando Agora", PLAYER_STATUS_PLAYING_EMOJI, discord.Color.blurple()

    queue = _queue_items(state)
    if queue:
        return "Fila pronta", "🎶", discord.Color.blurple()
    if _virtual_playlist_info(state):
        return "Carregando playlist", PLAYER_STATUS_ANIMATED_EMOJI, discord.Color.gold()
    reason = str(getattr(state, "idle_reason", "idle") or "idle")
    if reason == "manual_stop":
        return "Reprodução encerrada", "⏹️", discord.Color.dark_grey()
    if reason == "music_alone_timeout":
        return "Saí do canal por ficar sozinho", "👋", discord.Color.dark_grey()
    if reason == "music_idle_timeout":
        return "Fila encerrada", "📭", discord.Color.dark_grey()
    if reason == "voice_idle_empty":
        return "Canal vazio", "🔈", discord.Color.dark_grey()
    if reason == "voice_connection_lost":
        return "Não consegui voltar ao canal", "📡", discord.Color.red()
    if reason == "worker_unreachable":
        return "Confirmando estado do player", "🔄", discord.Color.gold()
    if reason == "unknown_disconnect":
        return "Bot saiu do canal", "⚠️", discord.Color.red()
    if reason == "external_disconnect":
        has_actor = bool(getattr(state, "idle_actor_id", None) or str(getattr(state, "idle_actor_name", "") or "").strip())
        return ("Removido do canal" if has_actor else "Bot saiu do canal"), "⚠️", discord.Color.red()
    if reason == "external_move":
        return "Movido para outro canal", "↪️", discord.Color.blurple()
    if reason == "track_failed":
        return "Não consegui iniciar", "❌", discord.Color.red()
    if reason == "queue_finished":
        return "Fila concluída", PLAYER_QUEUE_FINISHED_EMOJI, discord.Color.dark_grey()
    return "Nada tocando agora", "💤", discord.Color.dark_grey()


def _player_track_text(state, track: MusicTrack) -> str:
    # Um nível menor que o antigo H2 reduz quebras de linha no mobile sem
    # truncar agressivamente o nome da faixa. Tudo vem do estado em memória.
    title = _track_link_v2(track, title_limit=88, bold=True)
    source = _escape(track.uploader or track.source or track.extractor or "fonte desconhecida", limit=64)
    requester = _escape(track.requester_name, limit=42) if track.requester_name else f"<@{track.requester_id}>"
    source_emoji, source_label = _source_badge_for_track(track)
    quality = _public_audio_quality_label(state, track)
    duration = "Ao vivo" if track.is_live else track.duration_label

    metadata = [duration, f"{source_emoji} {source_label}"]
    if quality:
        metadata.append(quality)
    origin = str(getattr(track, "fallback_reason", "") or "").strip()
    requester_line = f"-# Pedido por {requester}" + (f" · via {_escape(origin, limit=32)}" if origin else "")
    lines = [f"### {title}", f"-# {source}", " · ".join(metadata), requester_line]
    if bool(getattr(state, "agent_voice_recovery_pending", False)):
        attempts = max(0, int(getattr(state, "agent_voice_recovery_attempts", 0) or 0))
        suffix = f" · tentativa {attempts}" if attempts else ""
        lines.append(f"-# 🔄 Reconectando ao canal de voz sem descartar a faixa ou a fila{suffix}.")
    elif str(getattr(state, "current_status", "") or "").lower() == "reconnecting":
        failures = max(0, int(getattr(state, "agent_monitor_failures", 0) or 0))
        deferred_attempts = max(0, int(getattr(state, "agent_deferred_command_attempts", 0) or 0))
        deferred_status = str(getattr(state, "agent_deferred_command_status", "") or "").lower()
        shown_attempt = deferred_attempts if deferred_status == "pending" and deferred_attempts else failures
        suffix = f" · tentativa {shown_attempt}" if shown_attempt else ""
        if deferred_status == "pending":
            lines.append(f"-# 🔄 A conexão do player caiu antes do início; vou reenviar esta faixa automaticamente assim que ela voltar{suffix}.")
        else:
            lines.append(f"-# 🔄 Perdi a comunicação com o player; esta faixa e a fila continuam preservadas enquanto reconecto{suffix}.")

    loop_mode = getattr(state, "loop_mode", None)
    loop_label = str(getattr(loop_mode, "label", "desligado") or "desligado")
    state_bits: list[str] = []
    if loop_label != "desligado":
        state_bits.append(f"🔁 {loop_label}")
    if getattr(state, "shuffle", False):
        state_bits.append("🔀 embaralhado")
    if state_bits:
        lines.append("-# " + " · ".join(state_bits))
    return "\n".join(lines)


def _queue_preview_text(state, *, limit: int = 4, selected_position: int | None = None, page: int = 0) -> str:
    items = _queue_items(state)
    total = _queue_total_count(state, items)
    virtual = _virtual_playlist_info(state)
    if not items:
        if virtual:
            return "**Fila** · carregando próximas…"
        return "**Fila** · vazia\n-# Use `_play <nome ou link>` para adicionar músicas."

    page = max(0, int(page))
    start = page * QUEUE_PAGE_SIZE if page else 0
    if page:
        preview = items[start : start + QUEUE_PAGE_SIZE]
    else:
        preview = items[: max(1, int(limit))]

    duration = _queue_duration_label(items)
    virtual_total_known = bool(virtual and virtual.get("total_tracks") not in (None, ""))
    total_text = str(total) if (not virtual or virtual_total_known) else f"{total}+"
    count_label = "música" if total == 1 else "músicas"
    header = f"**Fila** · {total_text} {count_label}"
    # Em playlist virtual a duração calculada é somente da janela em memória,
    # não da coleção inteira. Omiti-la evita apresentar ``34:20+`` como se fosse
    # uma duração total. Quando a coleção deixa de ser virtual, volta ao normal.
    if not virtual and duration and duration != "desconhecida":
        header += f" · {duration}"

    lines = [header]
    for offset, item in enumerate(preview, start=1):
        position = start + offset
        marker = "▶" if selected_position == position else f"{position}."
        duration_label = "" if item.duration is None and not item.is_live else f" · {item.duration_label}"
        lines.append(f"**{marker}** {_track_link_v2(item, title_limit=58)}{duration_label}")
    hidden = max(0, int(total) - len(preview) - start)
    if hidden:
        lines.append(f"-# + {hidden} música{'s' if hidden != 1 else ''}")
    return "\n".join(lines)


def _idle_player_text(state) -> str:
    reason = str(getattr(state, "idle_reason", "idle") or "idle")
    actor_id = getattr(state, "idle_actor_id", None)
    actor_name = str(getattr(state, "idle_actor_name", "") or "")
    channel_name = str(getattr(state, "idle_channel_name", "") or "")
    if reason == "queue_finished":
        return "A última música terminou e não há mais nada na fila."
    if reason == "track_failed":
        title = _escape(actor_name or "essa música", limit=64)
        detail = _escape(channel_name, limit=120) if channel_name else ""
        return f"Falhei antes do áudio começar em **{title}**." + (f"\n-# {detail}" if detail else "")
    if reason == "manual_stop":
        return "A reprodução foi encerrada pelo controle do player. A faixa atual e a fila foram limpas."
    if reason == "music_alone_timeout":
        return "Fiquei 2 minutos sem ninguém no canal de voz, então encerrei a reprodução e saí."
    if reason == "music_idle_timeout":
        return "A última música terminou e nenhuma outra começou nos 2 minutos seguintes, então saí do canal."
    if reason == "voice_idle_empty":
        return "O canal ficou sem usuários. Esperei 2 segundos para confirmar e saí."
    if reason == "voice_connection_lost":
        return "A conexão com o canal de voz caiu sem registro de remoção manual. Não consegui recuperar a sessão, então encerrei o player."
    if reason == "worker_unreachable":
        return "Ainda não consegui confirmar se a reprodução continua no canal. A faixa e a fila continuam preservadas enquanto faço uma nova verificação."
    if reason == "unknown_disconnect":
        where = f" **{_escape(channel_name, limit=48)}**" if channel_name else ""
        return f"O Discord confirmou que o bot saiu do canal{where}, mas não há registro de quem o removeu nem de uma saída automática."
    if reason == "external_disconnect":
        where = f" do canal **{_escape(channel_name, limit=48)}**" if channel_name else " do canal de voz"
        if actor_id:
            who = f"<@{int(actor_id)}>"
            return f"{who} desconectou o bot{where}."
        if actor_name:
            who = _escape(actor_name, limit=48)
            return f"{who} desconectou o bot{where}."
        return f"O Discord confirmou que o bot saiu{where}, mas não há registro suficiente para atribuir a saída a uma pessoa."
    if reason == "external_move":
        destination = f" **{_escape(channel_name, limit=48)}**" if channel_name else " outro canal de voz"
        if actor_id:
            return f"<@{int(actor_id)}> moveu o bot para{destination}."
        if actor_name:
            return f"{_escape(actor_name, limit=48)} moveu o bot para{destination}."
        return f"O bot foi movido para{destination}."
    return "Use `_play <link ou pesquisa>` para adicionar uma música."


def build_now_playing_embeds(state, track: MusicTrack) -> list[discord.Embed]:
    """Painel inspirado no MuseHeart, adaptado para discord.py/FFmpeg."""
    status = str(getattr(state, "current_status", "playing") or "playing")
    paused = bool(getattr(state, "paused", False)) or status == "paused"
    skipping = status == "skipping"
    loading = status in {"resolving", "starting", "skipping"}
    errored = status == "error"
    color = discord.Color.gold() if paused or loading else discord.Color.red() if errored else discord.Color.blurple()
    queue = _queue_items(state)
    queue_total = _queue_total_count(state, queue)
    embed = discord.Embed(color=color)
    if skipping:
        author_name = "Pulando música:"
        author_icon = "https://i.ibb.co/QXtk5VB/neon-circle.gif"
    elif loading:
        author_name = "Preparando áudio:"
        author_icon = "https://i.ibb.co/QXtk5VB/neon-circle.gif"
    elif paused:
        author_name = "Em Pausa:"
        author_icon = "https://cdn.discordapp.com/attachments/480195401543188483/896013933197013002/pause.png"
    elif errored:
        author_name = "Erro no player:"
        author_icon = "https://cdn.discordapp.com/emojis/1215703754471268414.png"
    else:
        author_name = "Tocando Agora:"
        author_icon = "https://i.ibb.co/QXtk5VB/neon-circle.gif"
    embed.set_author(name=author_name, icon_url=author_icon)

    duration_line = "> -# 🔴 **⠂** `Livestream`" if track.is_live else f"> -# ⏰ **⠂** `{track.duration_label}`"
    requester = track.requester_name or f"<@{track.requester_id}>"
    source = track.uploader or track.source or track.extractor or "fonte desconhecida"

    lines = [
        f"-# {_track_link(track)}",
        "",
    ]
    if skipping:
        if queue_total:
            lines.append("> -# ⏭️ **⠂** `Pulando... preparando a próxima música do queue.`")
        else:
            lines.append("> -# ⏭️ **⠂** `Pulando... encerrando a música atual.`")
    elif loading:
        lines.append("> -# 🔄 **⠂** `Resolvendo stream de áudio...`")
    lines.extend([
        duration_line,
        f"> -# 👤 **⠂** {_escape(source, limit=64)}",
        f"> -# ✋ **⠂** {requester}",
    ])
    backend = str(getattr(state, "current_backend", "local") or "local").lower()
    # Backend é detalhe interno. O painel público deve falar de player/música,
    # não de Music Agent, Lavalink, worker ou fallback.
    backend_label = "Player de música"
    lines.append(f"> -# 🎧 **⠂** `{backend_label}`")
    format_label = _local_audio_format_label(track)
    if not format_label and backend == "agent":
        kbps = 0
        with contextlib.suppress(Exception):
            kbps = int(float(getattr(state, "current_quality_kbps", 0) or 0))
        quality = str(getattr(state, "current_quality_label", "") or "").strip()
        if kbps:
            format_label = f"{quality + ' · ' if quality else ''}{kbps}kbps"
        elif quality:
            format_label = quality
    if format_label:
        lines.append(f"> -# 🎚️ **⠂** `Qualidade: {format_label}`")
    elif backend == "agent":
        # O caminho worker-owned às vezes começa a tocar antes de terminar o
        # enriquecimento completo de yt-dlp. Ainda assim, mantenha a linha de
        # qualidade visível para não regredir o painel antigo.
        lines.append("> -# 🎚️ **⠂** `Qualidade: áudio remoto`")
    elif loading:
        lines.append("> -# 🎚️ **⠂** `Qualidade: resolvendo`")

    loop_mode = getattr(state, "loop_mode", None)
    loop_label = getattr(loop_mode, "label", "desligado")
    if loop_label and loop_label != "desligado":
        loop_emoji = "🔂" if loop_label == "música atual" else "🔁"
        lines.append(f"> -# {loop_emoji} **⠂** `Repetição: {loop_label}`")

    if getattr(state, "shuffle", False):
        lines.append("> -# 🔀 **⠂** `Queue misturado`")

    if queue_total:
        lines.append(f"> -# 🎶 **⠂** `{queue_total} música{'s' if queue_total != 1 else ''} no queue`")
    else:
        lines.append("> -# 🎶 **⠂** `Queue vazio`")


    for label, count, needed in list(getattr(state, "panel_vote_summary", []) or []):
        lines.append(f"> -# 🗳️ **⠂** `{label}: {count}/{needed}`")

    embed.description = "\n".join(lines)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    embed.set_image(url=PLAYER_BAR_URL)
    embed.set_footer(text="Use os botões ou o menu abaixo para controlar o player.")

    embeds: list[discord.Embed] = []
    if queue:
        preview_limit = 3
        preview_items = queue[:preview_limit]
        remaining_count = max(0, int(queue_total or len(queue)) - len(preview_items))
        mini = discord.Embed(
            title=f"Músicas no queue: {int(queue_total or len(queue))}",
            color=discord.Color.blurple(),
        )
        mini_lines = []
        for n, item in enumerate(preview_items, start=1):
            mini_lines.append(f"-# `{n:02}) [{item.duration_label}]` {_track_link(item, title_limit=42)}")
        if remaining_count:
            mini_lines.append(f"-# `+ {remaining_count} restante(s)`")
        duration_prefix = "preview" if int(queue_total or len(queue)) > len(queue) else "queue"
        mini_lines.append(f"-# `⌛ Duração aproximada do {duration_prefix}: {_queue_duration_label(queue)}`")
        mini.description = "\n".join(mini_lines)
        mini.set_image(url=PLAYER_BAR_URL)
        embeds.append(mini)

    embeds.append(embed)
    return embeds


def build_player_embeds(state) -> list[discord.Embed]:
    """Renderização central do painel fixo do player.

    Deve ser usada sempre que queue/estado/música mudar, inclusive quando não há
    música atual. Isso evita painel congelado com snapshot antigo.
    """
    current = getattr(state, "current", None)
    if current is not None:
        return build_now_playing_embeds(state, current)

    queue = _queue_items(state)
    status = str(getattr(state, "current_status", "idle") or "idle")

    embed = discord.Embed(color=discord.Color.gold() if status == "skipping" else (discord.Color.dark_grey() if not queue else discord.Color.blurple()))
    if status == "skipping":
        embed.set_author(name="Pulando música...", icon_url="https://i.ibb.co/QXtk5VB/neon-circle.gif")
        if queue:
            first = queue[0]
            embed.description = (
                "A música atual foi pulada e a próxima já está sendo preparada.\n"
                f"Próxima: {_track_link(first, title_limit=60)}"
            )
            if first.thumbnail:
                embed.set_thumbnail(url=first.thumbnail)
        else:
            embed.description = "A música atual foi pulada. O player está finalizando a transição."
    elif queue:
        embed.set_author(name="Queue pronto:", icon_url="https://i.ibb.co/QXtk5VB/neon-circle.gif")
        queue_total = _queue_total_count(state, queue)
        preview_limit = 5
        preview_items = queue[:preview_limit]
        remaining_count = max(0, int(queue_total or len(queue)) - len(preview_items))
        duration_prefix = "preview" if int(queue_total or len(queue)) > len(queue) else "queue"
        lines = [
            f"> -# 🎶 **⠂** `{int(queue_total or len(queue))} música{'s' if int(queue_total or len(queue)) != 1 else ''} aguardando`",
            f"> -# ⌛ **⠂** `Duração aproximada do {duration_prefix}: {_queue_duration_label(queue)}`",
        ]
        for n, item in enumerate(preview_items, start=1):
            lines.append(f"-# `{n:02}) [{item.duration_label}]` {_track_link(item, title_limit=48)}")
        if remaining_count:
            lines.append(f"-# `+ {remaining_count} restante(s)`")
        embed.description = "\n".join(lines)
        first = queue[0]
        if first.thumbnail:
            embed.set_thumbnail(url=first.thumbnail)
        embed.set_footer(text="A próxima música será preparada automaticamente.")
    else:
        reason = str(getattr(state, "idle_reason", "idle") or "idle")
        actor_id = getattr(state, "idle_actor_id", None)
        actor_name = getattr(state, "idle_actor_name", "") or ""
        channel_name = getattr(state, "idle_channel_name", "") or ""
        if reason == "queue_finished":
            embed.set_author(name="Fila concluída", icon_url="https://i.ibb.co/QXtk5VB/neon-circle.gif")
            embed.description = "A última música terminou e não há mais nada na fila."
        elif reason == "track_failed":
            embed.set_author(name="Não consegui iniciar", icon_url="https://cdn.discordapp.com/emojis/1215703754471268414.png")
            failed_title = _escape(actor_name or "essa música", limit=64)
            detail = _escape(channel_name, limit=120) if channel_name else ""
            embed.description = (
                f"Falhei antes do áudio começar em **{failed_title}**.\n"
                + (f"-# `{detail}`\n" if detail else "")
                + "Use `_play <link ou pesquisa>` para tentar outra música."
            )
        elif reason == "manual_stop":
            embed.set_author(name="Reprodução encerrada", icon_url="https://cdn.discordapp.com/emojis/1215703754471268414.png")
            embed.description = "A reprodução foi encerrada pelo controle do player. A faixa atual e a fila foram limpas."
        elif reason == "music_alone_timeout":
            embed.set_author(name="Saí do canal por ficar sozinho", icon_url="https://i.ibb.co/QXtk5VB/neon-circle.gif")
            embed.description = "Fiquei 2 minutos sem ninguém no canal de voz, então encerrei a reprodução e saí."
        elif reason == "music_idle_timeout":
            embed.set_author(name="Fila encerrada", icon_url="https://i.ibb.co/QXtk5VB/neon-circle.gif")
            embed.description = "A última música terminou e nenhuma outra começou nos 2 minutos seguintes, então saí do canal."
        elif reason == "voice_idle_empty":
            embed.set_author(name="Canal vazio", icon_url="https://i.ibb.co/QXtk5VB/neon-circle.gif")
            embed.description = "O canal ficou sem usuários. Esperei 2 segundos para confirmar e saí."
        elif reason == "voice_connection_lost":
            embed.set_author(name="Não consegui voltar ao canal", icon_url="https://cdn.discordapp.com/emojis/1215703754471268414.png")
            embed.description = "A conexão com o canal de voz caiu sem registro de remoção manual. Não consegui recuperar a sessão, então encerrei o player."
        elif reason == "worker_unreachable":
            embed.set_author(name="Confirmando estado do player", icon_url="https://i.ibb.co/QXtk5VB/neon-circle.gif")
            embed.description = "Ainda não consegui confirmar se a reprodução continua no canal. A faixa e a fila continuam preservadas enquanto faço uma nova verificação."
        elif reason == "unknown_disconnect":
            embed.set_author(name="Bot saiu do canal", icon_url="https://cdn.discordapp.com/emojis/1215703754471268414.png")
            where = f" **{_escape(channel_name, limit=48)}**" if channel_name else ""
            embed.description = f"O Discord confirmou que o bot saiu do canal{where}, mas não há registro de quem o removeu nem de uma saída automática."
        elif reason == "external_disconnect":
            where = f" do canal **{_escape(channel_name, limit=48)}**" if channel_name else " do canal de voz"
            if actor_id:
                embed.set_author(name="Removido do canal", icon_url="https://cdn.discordapp.com/emojis/1215703754471268414.png")
                embed.description = f"<@{int(actor_id)}> desconectou o bot{where}."
            elif actor_name:
                embed.set_author(name="Removido do canal", icon_url="https://cdn.discordapp.com/emojis/1215703754471268414.png")
                embed.description = f"{_escape(actor_name, limit=48)} desconectou o bot{where}."
            else:
                embed.set_author(name="Bot saiu do canal", icon_url="https://cdn.discordapp.com/emojis/1215703754471268414.png")
                embed.description = f"O Discord confirmou que o bot saiu{where}, mas não há registro suficiente para atribuir a saída a uma pessoa."
        elif reason == "external_move":
            embed.set_author(name="Movido para outro canal", icon_url="https://i.ibb.co/QXtk5VB/neon-circle.gif")
            destination = f" **{_escape(channel_name, limit=48)}**" if channel_name else " outro canal de voz"
            if actor_id:
                embed.description = f"<@{int(actor_id)}> moveu o bot para{destination}."
            elif actor_name:
                embed.description = f"{_escape(actor_name, limit=48)} moveu o bot para{destination}."
            else:
                embed.description = f"O bot foi movido para{destination}."
        else:
            embed.set_author(name="Nada tocando agora", icon_url="https://i.ibb.co/QXtk5VB/neon-circle.gif")
            embed.description = "Use `_play <link ou pesquisa>` para adicionar uma música."

    embed.set_image(url=PLAYER_BAR_URL)
    return [embed]


def build_now_playing_embed(state, track: MusicTrack) -> discord.Embed:
    return build_now_playing_embeds(state, track)[-1]


def build_queue_embed(state, page: int = 0, *, selected_position: int | None = None) -> discord.Embed:
    items = _queue_items(state)
    queue_total = _queue_total_count(state, items)
    page = max(0, int(page))
    max_page = max(0, (len(items) - 1) // QUEUE_PAGE_SIZE)
    page = min(page, max_page)
    start = page * QUEUE_PAGE_SIZE
    chunk = items[start : start + QUEUE_PAGE_SIZE]
    page_label = f" — Página {page + 1}/{max_page + 1}" if max_page > 0 else ""
    embed = discord.Embed(
        title=f"Músicas no queue{page_label}",
        color=discord.Color.dark_grey() if not items else discord.Color.blurple(),
    )

    lines: list[str] = []
    if state.current:
        lines.append(f"`▶️` **Tocando agora:** {_track_link(state.current, title_limit=55)}")
        lines.append("")

    if not items:
        lines.append("`📭` **O queue está vazio.**")
        lines.append("-# Use `_play <nome ou link>` para adicionar músicas.")
    else:
        lines.append("**Queue:**")
        for offset, track in enumerate(chunk, start=1):
            index = start + offset
            requester = track.requester_name or f"<@{track.requester_id}>"
            prefix = "➤" if selected_position == index else f"{index}."
            lines.append(f"`{prefix}` {_track_link(track, title_limit=52)}")
            lines.append(f"-# `{track.duration_label}` • pedido por {requester}")

        lines.append("")
        duration_prefix = "preview" if int(queue_total or len(items)) > len(items) else "queue"
        if int(queue_total or len(items)) > len(items):
            lines.append(f"-# `+ {int(queue_total or len(items)) - len(items)} item(ns) fora deste preview`")
        lines.append(f"-# ⏳ Duração aproximada do {duration_prefix}: `{_queue_duration_label(items)}`")

    embed.description = "\n".join(lines)
    if selected_position and 1 <= selected_position <= len(items):
        selected = items[selected_position - 1]
        if selected.thumbnail:
            embed.set_thumbnail(url=selected.thumbnail)
        embed.set_footer(text=f"Posição {selected_position} selecionada • escolha uma ação abaixo")
    elif items:
        footer_total = int(queue_total or len(items))
        suffix = " • preview parcial" if footer_total > len(items) else ""
        embed.set_footer(text=f"{footer_total} item(ns) no queue{suffix} • selecione uma música para ver ações")
    else:
        embed.set_footer(text="O queue está pronto para receber músicas")
    return embed


class VolumeModal(discord.ui.Modal):
    def __init__(self, router, guild_id: int) -> None:
        super().__init__(title="Volume da música")
        self.router = router
        self.guild_id = int(guild_id)
        self.value = discord.ui.TextInput(
            label="Volume em %",
            placeholder="Exemplo: 55",
            min_length=1,
            max_length=3,
            required=True,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # O interaction ACK não pode depender de healthcheck/seleção remota.
        # O próprio comando de volume valida a disponibilidade depois do defer.
        if not await _require_music_voice_interaction(
            interaction, self.router, self.guild_id, check_worker=False
        ):
            return
        if not self.router.is_music_staff(getattr(interaction, "user", None)):
            await interaction.response.send_message("Apenas staff pode alterar volumes do player.", ephemeral=True)
            return
        try:
            raw = str(self.value.value).strip().replace("%", "")
            value = int(raw)
        except Exception:
            await interaction.response.send_message("Envie apenas um número válido.", ephemeral=True)
            return
        value = max(0, min(150, value))
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.router.set_volume(self.guild_id, value)
        await interaction.followup.send(f"🔊 Volume da música: `{value}%`.", ephemeral=True)


def _parse_seek_seconds(raw: str) -> int | None:
    value = str(raw or "").strip().replace(" ", "")
    if not value:
        return None
    if ":" in value:
        parts = value.split(":")
        if len(parts) not in {2, 3} or any(part == "" or not part.isdigit() for part in parts):
            return None
        numbers = [int(part) for part in parts]
        if numbers[-1] >= 60 or (len(numbers) == 3 and numbers[-2] >= 60):
            return None
        if len(numbers) == 2:
            minutes, seconds = numbers
            return minutes * 60 + seconds
        hours, minutes, seconds = numbers
        return hours * 3600 + minutes * 60 + seconds
    if not value.isdigit():
        return None
    if len(value) <= 2:
        return int(value)
    minutes = int(value[:-2] or "0")
    seconds = int(value[-2:])
    if seconds >= 60:
        return None
    return minutes * 60 + seconds


class SeekModal(discord.ui.Modal):
    def __init__(self, router, guild_id: int) -> None:
        super().__init__(title="Selecionar momento")
        self.router = router
        self.guild_id = int(guild_id)
        self.value = discord.ui.TextInput(
            label="Tempo da música",
            placeholder="Exemplos: 129, 45, 1:29 ou 01:29",
            min_length=1,
            max_length=12,
            required=True,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_music_voice_interaction(
            interaction, self.router, self.guild_id, check_worker=False
        ):
            return
        state = self.router.get_state(self.guild_id)
        requester_id = _current_track_requester_id(state)
        if getattr(state, "current", None) is None:
            await interaction.response.send_message("Não há música tocando agora.", ephemeral=True)
            return
        if int(getattr(interaction.user, "id", 0) or 0) != requester_id:
            await interaction.response.send_message("Apenas quem adicionou a música atual pode selecionar o momento.", ephemeral=True)
            return
        seconds = _parse_seek_seconds(str(self.value.value))
        if seconds is None:
            await interaction.response.send_message("Tempo inválido. Use algo como `129`, `45`, `1:29` ou `01:29`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, message = await self.router.seek_to(self.guild_id, seconds)
        await interaction.followup.send(message, ephemeral=True)


class SearchSelect(discord.ui.Select):
    def __init__(self, router, guild_id: int, voice_channel_id: int, text_channel_id: int, tracks: list[MusicTrack], requester_id: int | None = None, query: str = "") -> None:
        self.router = router
        self.guild_id = int(guild_id)
        self.voice_channel_id = int(voice_channel_id)
        self.text_channel_id = int(text_channel_id)
        self.tracks = tracks
        self.requester_id = int(requester_id or 0)
        self.query = str(query or "").strip()
        options = []
        for idx, track in enumerate(tracks[:10]):
            options.append(
                discord.SelectOption(
                    label=track.short_title[:100],
                    description=f"{track.uploader or track.source or 'resultado'} • {track.duration_label}"[:100],
                    value=str(idx),
                    emoji=YOUTUBE_SEARCH_OPTION_EMOJI,
                )
            )
        super().__init__(placeholder="Escolha o resultado para adicionar ao queue", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.requester_id and interaction.user and interaction.user.id != self.requester_id:
            await interaction.response.send_message("Só quem abriu essa busca pode escolher o resultado.", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild não encontrada.", ephemeral=True)
            return

        # ACK imediato: escolher um resultado pode resolver stream, consultar worker
        # e iniciar voz. Se isso acontecer antes do defer, o Discord mostra
        # "Esta interação falhou" mesmo quando o painel acaba atualizando.
        try:
            await interaction.response.defer(thinking=False)
        except discord.NotFound:
            return
        except Exception:
            # Se já foi respondida por algum caminho defensivo, continua usando edit/followup.
            pass

        loading_reaction = MusicLoadingReaction(getattr(interaction, "message", None))
        await loading_reaction.start()
        finish_loading_reaction = True
        operation_generation = self.router.current_music_operation_generation(self.guild_id)
        try:
            async def edit_original(content: str) -> None:
                try:
                    await interaction.edit_original_response(content=content, embed=None, view=None)
                except discord.NotFound:
                    return
                except Exception:
                    with contextlib.suppress(Exception):
                        await interaction.followup.send(content, ephemeral=True)

            if not await _require_music_voice_interaction(interaction, self.router, self.guild_id):
                return
            idx = int(self.values[0])
            track = self.tracks[idx]
            voice_channel = _interaction_user_voice_channel(interaction)
            text_channel = guild.get_channel(self.text_channel_id) or interaction.channel
            if voice_channel is None or text_channel is None:
                await edit_original("Canal não encontrado.")
                return
            registrar_selecao_busca(
                self.query,
                track,
                guild_id=self.guild_id,
                requester_id=getattr(interaction.user, "id", self.requester_id),
                posicao=idx + 1,
                total=len(self.tracks),
            )
            if bool(getattr(config, "MUSIC_AGENT_ENABLED", True)) and getattr(self.router, "music_worker_only_enabled", lambda: False)():
                try:
                    result = await music_agent_command(
                        "play",
                        guild_id=self.guild_id,
                        voice_channel_id=getattr(voice_channel, "id", self.voice_channel_id),
                        text_channel_id=getattr(text_channel, "id", self.text_channel_id),
                        query=_agent_query_for_track(track),
                        track=track,
                        requester_id=getattr(interaction.user, "id", 0),
                        requester_name=getattr(interaction.user, "display_name", str(interaction.user)),
                    )
                except Exception as exc:
                    if _agent_not_ready_transient(exc):
                        await edit_original(f"`🎧` **Preparando para tocar:** {track.short_title} • `{track.duration_label}`")
                        with contextlib.suppress(Exception):
                            message = await interaction.original_response()
                            finish_loading_reaction = False
                            agendar_tarefa_unica(("watch", int(self.guild_id)), _watch_agent_message(message, self.guild_id, track, router=self.router, voice_channel_id=getattr(voice_channel, "id", self.voice_channel_id), text_channel_id=getattr(text_channel, "id", self.text_channel_id), loading_reaction=loading_reaction))
                        return
                    await edit_original(f"`⚠️` Não consegui preparar essa música: `{exc}`")
                    return
                if isinstance(result, dict) and result.get("cancelled"):
                    return
                if self.router.current_music_operation_generation(self.guild_id) != operation_generation:
                    return
                await _sync_agent_panel(
                    self.router,
                    self.guild_id,
                    getattr(voice_channel, "id", self.voice_channel_id),
                    getattr(text_channel, "id", self.text_channel_id),
                    track,
                    result,
                    queued=bool(result.get("queued")),
                )
                msg = _agent_play_message(track, result)
                await edit_original(msg)
                state = result.get("state") if isinstance(result.get("state"), dict) else {}
                status = str(state.get("status") or "").lower()
                confirmed = _agent_confirmed_playing(state)
                if not result.get("queued") and not confirmed and status not in {"failed", "error"}:
                    with contextlib.suppress(Exception):
                        message = await interaction.original_response()
                        finish_loading_reaction = False
                        agendar_tarefa_unica(("watch", int(self.guild_id)), _watch_agent_message(message, self.guild_id, track, router=self.router, voice_channel_id=getattr(voice_channel, "id", self.voice_channel_id), text_channel_id=getattr(text_channel, "id", self.text_channel_id), loading_reaction=loading_reaction))
                return
            state_before = self.router.get_state(self.guild_id)
            was_session_active = bool(
                state_before.current
                or state_before.queue_size() > 0
                or getattr(state_before, "current_status", "") in {"resolving", "starting", "playing", "paused", "queued"}
            )
            added, dropped = await self.router.enqueue(guild, voice_channel, text_channel, [track])
            if added <= 0:
                await edit_original("`⚠️` Não adicionei nada: essa música já está no queue/tocando ou o queue está cheio.")
                return
            state = self.router.get_state(self.guild_id)
            position = state.queue_size() + (1 if state.current else 0)
            if was_session_active or position > 1:
                msg = f"`🎶` **Adicionada ao queue:** {track.short_title} • `{track.duration_label}` • posição `{max(1, position)}`"
            else:
                msg = f"`🎧` **Preparando para tocar:** {track.short_title} • `{track.duration_label}`"
            if dropped:
                msg += "\n`⚠️` Alguns itens extras não entraram porque já estavam no queue/tocando ou porque o queue está cheio."
            await edit_original(msg)
        finally:
            if finish_loading_reaction:
                await loading_reaction.finish()


class SearchResultView(discord.ui.View):
    def __init__(self, router, guild_id: int, voice_channel_id: int, text_channel_id: int, tracks: list[MusicTrack], requester_id: int | None = None, query: str = "") -> None:
        super().__init__(timeout=120)
        self.add_item(SearchSelect(router, guild_id, voice_channel_id, text_channel_id, tracks, requester_id, query))


class AddSongModal(discord.ui.Modal):
    def __init__(self, router, guild_id: int, *, voice_channel_id: int | None = None, text_channel_id: int | None = None) -> None:
        super().__init__(title="Adicionar música")
        self.router = router
        self.guild_id = int(guild_id)
        self.voice_channel_id = int(voice_channel_id or 0)
        self.text_channel_id = int(text_channel_id or 0)
        self.query = discord.ui.TextInput(
            label="Nome, link ou playlist",
            placeholder="Exemplo: Laufey From The Start",
            min_length=2,
            max_length=300,
            required=True,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild não encontrada.", ephemeral=True)
            return

        state = self.router.get_state(self.guild_id)
        if not await _require_music_voice_interaction(
            interaction, self.router, self.guild_id, check_worker=False
        ):
            return
        voice_channel = _interaction_user_voice_channel(interaction)
        text_channel = None
        if self.text_channel_id or state.last_text_channel_id:
            cid = self.text_channel_id or state.last_text_channel_id
            text_channel = guild.get_channel(int(cid)) or interaction.client.get_channel(int(cid))
        text_channel = text_channel or interaction.channel

        await interaction.response.defer(ephemeral=True, thinking=True)
        query = str(self.query.value).strip()
        requester_name = getattr(interaction.user, "display_name", str(interaction.user))
        operation_generation = self.router.current_music_operation_generation(self.guild_id)
        try:
            batch, force_selection = await _extract_batch_for_add_modal(
                self.router,
                guild.id,
                query,
                requester_id=interaction.user.id,
                requester_name=requester_name,
            )
        except MusicExtractionError as exc:
            await interaction.followup.send(f"`⚠️` {exc}", ephemeral=True)
            return
        except Exception as exc:
            message = str(exc or "").strip()
            if message == getattr(self.router, "music_worker_unavailable_message", "") or message.startswith("Sistema de música indisponível no momento:"):
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.followup.send(f"`⚠️` Não consegui preparar essa música: `{exc}`", ephemeral=True)
            return

        if self.router.current_music_operation_generation(self.guild_id) != operation_generation:
            return

        if not batch.tracks:
            await interaction.followup.send("`📭` Não encontrei nada tocável.", ephemeral=True)
            return

        profile = describe_url(query)
        if batch.is_playlist and profile.platform == "spotify":
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

        should_open_selection = bool(
            force_selection
            or (not self.router.extractor.looks_like_url(query) and len(batch.tracks) > 1)
        )
        if should_open_selection:
            embed = discord.Embed(
                title="🔎 Escolha a música",
                description="Selecione um dos resultados abaixo.",
                color=discord.Color.blurple(),
            )
            for idx, track in enumerate(batch.tracks[:10], start=1):
                embed.add_field(name=f"{idx}. {track.short_title}", value=f"{track.uploader or track.source or 'resultado'} • `{track.duration_label}`", inline=False)
            # Sobreponha resolução do top-1 ao envio do menu no Discord.
            _schedule_agent_prefetch(
                self.router,
                guild.id,
                batch.tracks[:10],
                voice_channel_id=getattr(voice_channel, "id", 0),
                text_channel_id=getattr(text_channel, "id", 0),
                requester_id=interaction.user.id,
                requester_name=requester_name,
            )
            await interaction.followup.send(
                embed=embed,
                view=SearchResultView(self.router, guild.id, getattr(voice_channel, "id", 0), getattr(text_channel, "id", 0), batch.tracks[:10], interaction.user.id, query),
                ephemeral=True,
            )
            return

        if bool(getattr(config, "MUSIC_AGENT_ENABLED", True)) and getattr(self.router, "music_worker_only_enabled", lambda: False)():
            track = batch.tracks[0]
            virtual_cursor = getattr(batch, "playlist_cursor", None)
            virtual_active = bool(batch.is_playlist and virtual_cursor is not None and not virtual_cursor.exhausted)
            is_multi = bool(len(batch.tracks) > 1 or virtual_active)
            try:
                if is_multi:
                    tracks_payload = _agent_tracks_payload(batch.tracks, requester_id=interaction.user.id, requester_name=requester_name)
                    if virtual_active:
                        tracks_payload.append(
                            payload_cursor_playlist(
                                virtual_cursor,
                                requester_id=interaction.user.id,
                                requester_name=requester_name,
                            )
                        )
                    result = await music_agent_command(
                        "enqueue_many",
                        guild_id=guild.id,
                        voice_channel_id=getattr(voice_channel, "id", self.voice_channel_id),
                        text_channel_id=getattr(text_channel, "id", self.text_channel_id),
                        query=query,
                        track=track,
                        tracks=tracks_payload,
                        requester_id=interaction.user.id,
                        requester_name=requester_name,
                    )
                else:
                    result = await music_agent_command(
                        "play",
                        guild_id=guild.id,
                        voice_channel_id=getattr(voice_channel, "id", self.voice_channel_id),
                        text_channel_id=getattr(text_channel, "id", self.text_channel_id),
                        query=_agent_query_for_track(track, query),
                        track=track,
                        requester_id=interaction.user.id,
                        requester_name=requester_name,
                    )
            except Exception as exc:
                if _agent_not_ready_transient(exc):
                    sent = await _safe_interaction_followup(
                        interaction,
                        f"`🎧` **Preparando para tocar:** {track.short_title} • `{track.duration_label}`",
                        ephemeral=True,
                        wait=True,
                    )
                    if sent is not None:
                        agendar_tarefa_unica(("watch", int(self.guild_id)), _watch_agent_message(sent, guild.id, track, router=self.router, voice_channel_id=getattr(voice_channel, "id", self.voice_channel_id), text_channel_id=getattr(text_channel, "id", self.text_channel_id)))
                    return
                await _safe_interaction_followup(interaction, f"`⚠️` Não consegui preparar essa música: `{exc}`", ephemeral=True)
                return
            if isinstance(result, dict) and result.get("cancelled"):
                return
            if self.router.current_music_operation_generation(self.guild_id) != operation_generation:
                return
            await _sync_agent_panel(
                self.router,
                guild.id,
                getattr(voice_channel, "id", self.voice_channel_id),
                getattr(text_channel, "id", self.text_channel_id),
                track,
                result,
                queued=bool(result.get("queued")),
            )
            if virtual_active:
                # Igual ao comando _play: a primeira faixa chega ao Worker antes
                # de qualquer refill. O restante da janela só é agendado depois
                # do ACK de play/enqueue, sem polling adicional.
                schedule_playlist_refill_from_result(self.router, guild.id, result)
            if is_multi:
                added = int(result.get("added") or len(batch.tracks))
                label = f" de **{discord.utils.escape_markdown((batch.playlist_title or '')[:80])}**" if batch.playlist_title else ""
                count_label = "música" if added == 1 else "músicas"
                state_payload = result.get("state") if isinstance(result.get("state"), dict) else {}
                try:
                    queue_total = int(state_payload.get("queue_size") or 0)
                except Exception:
                    queue_total = 0
                if virtual_active:
                    if bool(result.get("queued")):
                        sent = await interaction.followup.send(
                            f"`📑` **Playlist adicionada à fila{label}.**",
                            ephemeral=True,
                            wait=True,
                        )
                    else:
                        sent = await interaction.followup.send(
                            f"`📑` **Playlist iniciada{label}.**",
                            ephemeral=True,
                            wait=True,
                        )
                elif bool(result.get("queued")):
                    total_line = f"\n`🎶` Fila agora: `{queue_total}` música(s)." if queue_total else ""
                    sent = await interaction.followup.send(
                        f"`📑` **Playlist adicionada ao final da fila:** `{added}` {count_label}{label}.{total_line}",
                        ephemeral=True,
                        wait=True,
                    )
                else:
                    sent = await interaction.followup.send(
                        f"`📑` **Playlist adicionada à fila:** `{added}` {count_label}{label}.\n`🎧` Preparando a primeira faixa...",
                        ephemeral=True,
                        wait=True,
                    )
            else:
                msg = _agent_play_message(track, result)
                sent = await interaction.followup.send(msg, ephemeral=True, wait=True)
            state = result.get("state") if isinstance(result.get("state"), dict) else {}
            status = str(state.get("status") or "").lower()
            if sent is not None and not result.get("queued") and status not in {"playing", "failed", "error"}:
                agendar_tarefa_unica(("watch", int(self.guild_id)), _watch_agent_message(sent, guild.id, track, router=self.router, voice_channel_id=getattr(voice_channel, "id", self.voice_channel_id), text_channel_id=getattr(text_channel, "id", self.text_channel_id)))
            return

        state_before = self.router.get_state(self.guild_id)
        was_session_active = bool(
            state_before.current
            or state_before.queue_size() > 0
            or getattr(state_before, "current_status", "") in {"resolving", "starting", "playing", "paused", "queued"}
        )
        added, dropped = await self.router.enqueue(guild, voice_channel, text_channel, batch.tracks)
        if added <= 0:
            await interaction.followup.send("`⚠️` Não adicionei nada: tudo já estava no queue/tocando ou o queue está cheio.", ephemeral=True)
            return
        if batch.is_playlist:
            msg = f"`📑` **Playlist adicionada:** `{added}` música(s)"
            if batch.playlist_title:
                msg += f" de **{batch.playlist_title}**"
            if batch.truncated:
                msg += f"\n`⚠️` Playlist limitada aos primeiros `{getattr(config, 'MUSIC_MAX_PLAYLIST_ITEMS', 100)}` itens para economizar RAM."
        else:
            state = self.router.get_state(self.guild_id)
            position = state.queue_size() + (1 if state.current else 0)
            if was_session_active or position > 1:
                msg = f"`🎶` **Adicionada ao queue:** {batch.tracks[0].short_title} • `{batch.tracks[0].duration_label}` • posição `{max(1, position)}`"
            else:
                msg = f"`🎧` **Preparando para tocar:** {batch.tracks[0].short_title} • `{batch.tracks[0].duration_label}`"
        if dropped and len(batch.tracks) > added:
            msg += f"\n`⚠️` `{dropped}` item(ns) não entraram porque já estavam no queue/tocando ou porque o queue está cheio."
        await interaction.followup.send(msg, ephemeral=True)


class QueueSelect(discord.ui.Select):
    def __init__(
        self,
        router,
        guild_id: int,
        page: int = 0,
        selected_position: int | None = None,
        *,
        items: list[MusicTrack] | None = None,
        start_position: int | None = None,
    ) -> None:
        self.router = router
        self.guild_id = int(guild_id)
        self.page = max(0, int(page))
        self.selected_position = selected_position
        if items is None:
            all_items = router.snapshot_queue(guild_id)
            start = self.page * QUEUE_PAGE_SIZE
            page_items = all_items[start : start + QUEUE_PAGE_SIZE]
            first_position = start + 1
        else:
            page_items = list(items)
            first_position = max(1, int(start_position or (self.page * QUEUE_PAGE_SIZE + 1)))
        options = []
        for idx, track in enumerate(page_items, start=first_position):
            options.append(
                discord.SelectOption(
                    label=f"{idx}. {track.short_title}"[:100],
                    description=f"{track.duration_label} • {track.uploader or track.source or 'fila'}"[:100],
                    value=str(idx),
                    emoji="🎵",
                    default=selected_position == idx,
                )
            )
        super().__init__(placeholder="Selecione uma música da fila", min_values=1, max_values=1, options=options, custom_id="music:queue:select")

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, QueueView):
            await view._redraw(interaction, selected_position=int(self.values[0]))
            return
        await interaction.response.defer()


class JumpQueuePageModal(discord.ui.Modal):
    def __init__(self, view: "QueueView", *, max_page: int) -> None:
        super().__init__(title="Ir para página")
        self.queue_view = view
        self.max_page = max(0, int(max_page))
        self.page_value = discord.ui.TextInput(
            label=f"Página (1 a {self.max_page + 1})",
            placeholder=str(min(self.max_page + 1, self.queue_view.page + 1)),
            min_length=1,
            max_length=max(1, len(str(self.max_page + 1))),
            required=True,
        )
        self.add_item(self.page_value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            page = int(str(self.page_value.value).strip())
        except Exception:
            await interaction.response.send_message("Digite apenas o número da página.", ephemeral=True)
            return
        if page < 1 or page > self.max_page + 1:
            await interaction.response.send_message(
                f"Escolha uma página entre 1 e {self.max_page + 1}.",
                ephemeral=True,
            )
            return
        await self.queue_view._redraw(
            interaction,
            absolute_page=page - 1,
            selected_position=None,
        )


class MoveSelectedModal(discord.ui.Modal):
    def __init__(self, router, guild_id: int, from_pos: int, *, page: int = 0, owner_id: int | None = None, message=None, selected_track: MusicTrack | None = None) -> None:
        super().__init__(title="Mover música selecionada")
        self.router = router
        self.guild_id = int(guild_id)
        self.from_pos = int(from_pos)
        self.page = max(0, int(page))
        self.owner_id = int(owner_id or 0)
        self.message = message
        self.selected_track = selected_track
        self.to_pos = discord.ui.TextInput(
            label="Nova posição na fila",
            placeholder="Exemplo: 1",
            min_length=1,
            max_length=4,
            required=True,
        )
        self.add_item(self.to_pos)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_music_voice_interaction(
            interaction, self.router, self.guild_id, check_worker=False
        ):
            return
        try:
            to_pos = int(str(self.to_pos.value).strip())
        except Exception:
            await interaction.response.send_message("Use apenas número válido.", ephemeral=True)
            return
        if to_pos < 1:
            await interaction.response.send_message("A posição precisa ser maior que zero.", ephemeral=True)
            return
        if to_pos == self.from_pos:
            await interaction.response.send_message("Essa música já está nessa posição.", ephemeral=True)
            return
        # ACK como atualização da mensagem original. Funciona também para
        # painéis ephemeral e mantém toda a operação remota fora dos 3 s.
        await interaction.response.defer()
        virtual = bool(
            self.selected_track is not None
            and int(getattr(self.selected_track, "virtual_source_index", -1)) >= 0
        )
        if virtual:
            result = await self.router.virtual_queue_action(
                self.guild_id,
                "move",
                self.selected_track,
                to_position=to_pos,
            )
            ok = bool(result.get("ok"))
        else:
            ok = await self.router.move(self.guild_id, self.from_pos, to_pos)
        await interaction.followup.send(
            "`↪️` Música movida." if ok else "Não consegui mover: a fila mudou ou a posição não existe mais.",
            ephemeral=True,
        )
        if ok:
            refresh = getattr(self.router, "refresh_queue_controller", None)
            if callable(refresh):
                await refresh(self.guild_id)
            view = QueueView(self.router, self.guild_id, self.page, owner_id=self.owner_id)
            with contextlib.suppress(Exception):
                await interaction.edit_original_response(content=None, embeds=[], attachments=[], view=view)


class StaticMusicMessageView(discord.ui.LayoutView):
    def __init__(self, text: str, *, accent_color: discord.Colour | int | None = None) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                accent_color=accent_color if accent_color is not None else discord.Color.dark_grey(),
            )
        )


class QueueConfirmView(discord.ui.LayoutView):
    def __init__(self, router, guild_id: int, *, action: str, owner_id: int | None = None, page: int = 0, position: int | None = None, message=None, selected_track: MusicTrack | None = None) -> None:
        super().__init__(timeout=45)
        self.router = router
        self.guild_id = int(guild_id)
        self.action = action
        self.owner_id = int(owner_id or 0)
        self.page = max(0, int(page))
        self.position = int(position or 0)
        self.message = message
        self.selected_track = selected_track
        self.confirm_message = None
        prompt = "Limpar todas as músicas da fila?" if action == "clear" else "Remover esta música da fila?"
        confirm = discord.ui.Button(label="Confirmar", emoji="✅", style=discord.ButtonStyle.danger, custom_id=f"music:queue:confirm:{action}")
        confirm.callback = self.confirm
        cancel = discord.ui.Button(label="Cancelar", emoji="❌", style=discord.ButtonStyle.secondary, custom_id=f"music:queue:cancel:{action}")
        cancel.callback = self.cancel
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"### {prompt}"),
                discord.ui.ActionRow(confirm, cancel),
                accent_color=discord.Color.red(),
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id and interaction.user and interaction.user.id != self.owner_id:
            await interaction.response.send_message(f"Apenas <@{self.owner_id}> pode confirmar essa ação.", ephemeral=True)
            return False
        # Não faça healthcheck/seleção de worker no interaction_check: o Discord
        # só chama o callback depois disso, então qualquer I/O aqui pode gerar
        # "Esta interação falhou" mesmo com a ação aplicada.
        return await _require_music_voice_interaction(interaction, self.router, self.guild_id, check_worker=False)

    async def _refresh_parent(self) -> None:
        if self.message is None:
            return
        refresh = getattr(self.router, "refresh_queue_controller", None)
        if callable(refresh):
            await refresh(self.guild_id)
        view = QueueView(self.router, self.guild_id, self.page, owner_id=self.owner_id)
        with contextlib.suppress(Exception):
            await self.message.edit(content=None, embeds=[], attachments=[], view=view)

    async def confirm(self, interaction: discord.Interaction):
        # ACK imediato: replace/remove podem atravessar a rede e nunca devem
        # ficar na frente da confirmação da interação do Discord.
        if not interaction.response.is_done():
            # Em componente, defer sem ``thinking`` confirma a atualização da
            # própria mensagem (inclusive ephemeral). Depois podemos usar
            # edit_original_response sem criar um segundo card de resposta.
            await interaction.response.defer()
        if self.action == "clear":
            await self.router.replace_queue(self.guild_id, [])
            await interaction.edit_original_response(
                content=None, embeds=[], attachments=[],
                view=StaticMusicMessageView("### 🧹 Fila limpa", accent_color=discord.Color.green()),
            )
            await self._refresh_parent()
            self.stop()
            return

        if self.action == "remove":
            is_virtual = bool(
                self.selected_track is not None
                and int(getattr(self.selected_track, "virtual_source_index", -1)) >= 0
            )
            if is_virtual:
                result = await self.router.virtual_queue_action(
                    self.guild_id, "remove", self.selected_track
                )
                removed = self.selected_track if bool(result.get("ok")) else None
            else:
                removed = await self.router.remove_at(self.guild_id, self.position)
            if removed is None:
                text = "### Essa música não existe mais nessa posição da fila."
                color = discord.Color.red()
            else:
                text = f"### 🗑️ Removido da fila\n{_escape(removed.short_title, limit=80)}"
                color = discord.Color.green()
            await interaction.edit_original_response(
                content=None, embeds=[], attachments=[],
                view=StaticMusicMessageView(text, accent_color=color),
            )
            await self._refresh_parent()
            self.stop()
            return

        await interaction.edit_original_response(
            content=None, embeds=[], attachments=[],
            view=StaticMusicMessageView("### Ação desconhecida", accent_color=discord.Color.red()),
        )
        self.stop()

    async def on_timeout(self) -> None:
        # Confirmações podem expirar; nesse caso remova os botões visivelmente
        # em vez de deixar componentes que parecem clicáveis sem callback.
        message = self.confirm_message
        if message is not None:
            with contextlib.suppress(Exception):
                await message.edit(
                    content=None, embeds=[], attachments=[],
                    view=StaticMusicMessageView("### Confirmação expirada", accent_color=discord.Color.dark_grey()),
                )
        self.stop()

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        logger.error(
            "[music/queue] confirmação falhou | guild=%s action=%s",
            self.guild_id,
            self.action,
            exc_info=(type(error), error, error.__traceback__),
        )
        await _send_interaction_notice(interaction, "Não consegui concluir essa ação. Tente novamente.")

    async def cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=None, embeds=[], attachments=[],
            view=StaticMusicMessageView("### Ação cancelada", accent_color=discord.Color.dark_grey()),
        )
        self.stop()


class QueueView(discord.ui.LayoutView):
    def __init__(self, router, guild_id: int, page: int = 0, *, owner_id: int | None = None, selected_position: int | None = None) -> None:
        # O controlador de fila não pode morrer silenciosamente enquanto seus
        # botões continuam visíveis. Ele permanece ativo durante a vida do
        # processo; confirmações destrutivas continuam com timeout próprio.
        super().__init__(timeout=None)
        self.router = router
        self.guild_id = int(guild_id)
        self.page = max(0, int(page))
        self.owner_id = int(owner_id or 0)
        self.selected_position = selected_position
        # Uma mesma view pode receber cliques consecutivos antes de o redraw
        # anterior terminar. Serialize mudanças de página/seleção para não
        # aplicar respostas fora de ordem nem pular páginas em double-click.
        self._interaction_lock = asyncio.Lock()
        self._refresh_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id and interaction.user and interaction.user.id != self.owner_id:
            await interaction.response.send_message(f"Apenas <@{self.owner_id}> pode interagir nesse painel de fila.", ephemeral=True)
            return False
        # Não faça healthcheck/seleção de worker no interaction_check.
        return await _require_music_voice_interaction(interaction, self.router, self.guild_id, check_worker=False)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        logger.error(
            "[music/queue] interação falhou | guild=%s page=%s item=%s",
            self.guild_id,
            self.page,
            getattr(item, "custom_id", type(item).__name__),
            exc_info=(type(error), error, error.__traceback__),
        )
        await _send_interaction_notice(interaction, "Não consegui atualizar a fila agora. Tente novamente.")

    def _queue_items(self) -> list[MusicTrack]:
        return self.router.snapshot_queue(self.guild_id)

    def _max_page(self, items: list[MusicTrack] | None = None, state=None) -> int:
        items = self._queue_items() if items is None else items
        state = self.router.get_state(self.guild_id) if state is None else state
        total = max(len(items), _queue_total_count(state, items))
        return max(0, (total - 1) // QUEUE_PAGE_SIZE)

    def _page_items(self, state, items: list[MusicTrack]) -> tuple[list[MusicTrack], bool]:
        """Retorna itens da página e se vieram do layout lógico/cache virtual."""
        pages = getattr(state, "agent_virtual_playlist_pages", None)
        if isinstance(pages, dict):
            cached = pages.get(self.page)
            if isinstance(cached, list):
                return list(cached), True
        start = self.page * QUEUE_PAGE_SIZE
        end = start + QUEUE_PAGE_SIZE
        if not _virtual_playlists_info(state):
            return items[start:end], False
        # Antes do primeiro browse assíncrono, preserve a janela materializada
        # já espelhada. Páginas virtuais serão preenchidas por _prepare_page().
        return items[start:end], False

    async def _prepare_page(self) -> None:
        # Cada clique precisa partir do snapshot autoritativo atual. Antes, o
        # usuário podia navegar 1/3 -> 2/3 -> 3/3 enquanto a música avançava e
        # o card mantinha current/fila antigos, aparentando duplicação.
        refresh = getattr(self.router, "refresh_queue_controller", None)
        if callable(refresh):
            with contextlib.suppress(Exception):
                await refresh(self.guild_id)
        state = self.router.get_state(self.guild_id)
        items = self._queue_items()
        self.page = max(0, min(self.page, self._max_page(items, state)))
        start = self.page * QUEUE_PAGE_SIZE
        end = start + QUEUE_PAGE_SIZE
        virtuals = _virtual_playlists_info(state)
        if virtuals:
            await carregar_pagina_fila_virtual(
                self.router,
                self.guild_id,
                self.page,
                page_size=QUEUE_PAGE_SIZE,
            )

    def _queue_text(self, state, items: list[MusicTrack]) -> str:
        total = _queue_total_count(state, items)
        virtual = _virtual_playlist_info(state)
        page_items, from_virtual_cache = self._page_items(state, items)
        if not items and not page_items:
            if virtual:
                error = str(getattr(state, "agent_virtual_playlist_browse_error", "") or "").strip()
                if error:
                    return "## 📜 Fila\nNão consegui carregar esta página agora.\n-# Tente navegar novamente em alguns segundos."
                return "## 📜 Fila\nCarregando próximas músicas…"
            return "## 📜 Fila\nA fila está vazia.\n-# Use `_play <nome ou link>` para adicionar músicas."

        max_page = self._max_page(items, state)
        start = self.page * QUEUE_PAGE_SIZE
        if virtual:
            virtuals = _virtual_playlists_info(state)
            count = f"{total} música{'s' if total != 1 else ''}"
            lines = [f"## 📜 Fila · {count}"]
            lines.append(f"-# Página {self.page + 1}/{max_page + 1}")
            if len(virtuals) == 1:
                title = _escape(str(virtual.get("title") or ""), limit=80)
                if title and title.lower() != "playlist":
                    lines.append(f"-# {title}")
            elif len(virtuals) > 1:
                # Não exponha cursor/janela/materialização na UI. Só informe que
                # a fila contém mais de uma coleção quando isso for útil.
                instances = {str(info.get("instance_id") or info.get("source_url") or "") for info in virtuals}
                instances.discard("")
                if len(instances) > 1:
                    lines.append(f"-# {len(instances)} playlists na fila")
        else:
            lines = [f"## 📜 Fila · {total} música{'s' if total != 1 else ''}"]
            if max_page:
                lines.append(f"-# Página {self.page + 1}/{max_page + 1}")

        current = getattr(state, "current", None)
        if current is not None:
            lines.extend([f"-# Tocando agora: {_track_link_v2(current, title_limit=64)}", ""])

        if not page_items:
            lines.append("-# Esta página ainda não ficou disponível para visualização.")
        else:
            for offset, track in enumerate(page_items, start=1):
                index = start + offset
                marker = "▶" if self.selected_position == index else f"{index:02d}"
                lines.append(f"**{marker}**  {_track_link_v2(track, title_limit=62)}  ·  {track.duration_label}")

        if not virtual:
            lines.extend(["", f"-# Duração aproximada: {_queue_duration_label(items)}"])
        return "\n".join(lines)

    def _refresh_components(self) -> None:
        self.clear_items()
        items = self._queue_items()
        state = self.router.get_state(self.guild_id)
        max_page = self._max_page(items, state)
        self.page = max(0, min(self.page, max_page))
        page_items, from_virtual_cache = self._page_items(state, items)

        if self.selected_position and not (
            1 <= int(self.selected_position) <= max(1, _queue_total_count(state, items))
        ):
            self.selected_position = None

        start = self.page * QUEUE_PAGE_SIZE
        selected_track: MusicTrack | None = None
        if self.selected_position and start < int(self.selected_position) <= start + len(page_items):
            selected_track = page_items[int(self.selected_position) - start - 1]

        virtual = _virtual_playlist_info(state)
        container = discord.ui.Container(accent_color=discord.Color.blurple() if (items or page_items or virtual) else discord.Color.dark_grey())
        container.add_item(discord.ui.TextDisplay(self._queue_text(state, items)))

        start = self.page * QUEUE_PAGE_SIZE
        if page_items:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.ActionRow(
                    QueueSelect(
                        self.router,
                        self.guild_id,
                        self.page,
                        self.selected_position,
                        items=page_items,
                        start_position=start + 1,
                    )
                )
            )

        if selected_track is not None:
            play = discord.ui.Button(label="Tocar agora", emoji="▶️", style=discord.ButtonStyle.primary, custom_id="music:queue:play")
            play.callback = self.play_selected
            move = discord.ui.Button(label="Mover", emoji="↪️", style=discord.ButtonStyle.secondary, custom_id="music:queue:move")
            move.callback = self.move_selected
            remove = discord.ui.Button(label="Remover", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="music:queue:remove")
            remove.callback = self.remove_selected
            container.add_item(discord.ui.ActionRow(play, move, remove))

        if max_page > 0:
            previous = discord.ui.Button(emoji="⬅️", style=discord.ButtonStyle.secondary, disabled=self.page <= 0, custom_id="music:queue:previous")
            previous.callback = self.previous_page
            page_label = discord.ui.Button(label=f"Página {self.page + 1}/{max_page + 1}", style=discord.ButtonStyle.secondary, custom_id="music:queue:page")
            page_label.callback = self.jump_page
            next_button = discord.ui.Button(emoji="➡️", style=discord.ButtonStyle.secondary, disabled=self.page >= max_page, custom_id="music:queue:next")
            next_button.callback = self.next_page
            container.add_item(discord.ui.ActionRow(previous, page_label, next_button))

        if items or virtual:
            clear = discord.ui.Button(label="Limpar fila", emoji="🧹", style=discord.ButtonStyle.danger, custom_id="music:queue:clear")
            clear.callback = self.clear_queue
            container.add_item(discord.ui.ActionRow(clear))

        self.add_item(container)

    async def _redraw(
        self,
        interaction: discord.Interaction,
        *,
        page_delta: int = 0,
        absolute_page: int | None = None,
        selected_position: int | None | object = ...,
    ) -> bool:
        # ACK primeiro. Para componentes, defer() sem thinking usa
        # DEFERRED_MESSAGE_UPDATE; edit_original_response() é então a rota
        # correta (e necessária para mensagens ephemeral) para editar o próprio
        # painel que originou o clique. interaction.message.edit() pode tentar a
        # rota normal de mensagem e falhar em painéis ephemeral.
        if not interaction.response.is_done():
            await interaction.response.defer()

        async with self._interaction_lock:
            old_page = self.page
            old_selected = self.selected_position
            if absolute_page is not None:
                self.page = max(0, int(absolute_page))
            elif page_delta:
                self.page = max(0, self.page + int(page_delta))
            if selected_position is not ...:
                self.selected_position = selected_position
            try:
                await self._prepare_page()
                self._refresh_components()
                await interaction.edit_original_response(
                    content=None, embeds=[], attachments=[], view=self
                )
                return True
            except Exception:
                # Se o Discord rejeitar a edição, não deixe o objeto da view
                # avançado para uma página que nunca foi mostrada. Isso evita
                # que um segundo clique pule 1/11 -> 3/11 depois de uma falha.
                self.page = old_page
                self.selected_position = old_selected
                self._refresh_components()
                logger.exception(
                    "[music/queue] redraw falhou | guild=%s page=%s",
                    self.guild_id,
                    old_page,
                )
                await _send_interaction_notice(
                    interaction,
                    "Não consegui carregar esta página agora. Tente novamente.",
                )
                return False

    async def previous_page(self, interaction: discord.Interaction):
        await self._redraw(interaction, page_delta=-1, selected_position=None)

    async def next_page(self, interaction: discord.Interaction):
        await self._redraw(interaction, page_delta=1, selected_position=None)

    async def jump_page(self, interaction: discord.Interaction):
        items = self._queue_items()
        state = self.router.get_state(self.guild_id)
        max_page = self._max_page(items, state)
        await interaction.response.send_modal(JumpQueuePageModal(self, max_page=max_page))

    async def cancel_selection(self, interaction: discord.Interaction):
        await self._redraw(interaction, selected_position=None)

    async def play_selected(self, interaction: discord.Interaction):
        if not self.selected_position:
            await interaction.response.send_message("Selecione uma música primeiro.", ephemeral=True)
            return
        state = self.router.get_state(self.guild_id)
        items = self._queue_items()
        page_items, _from_virtual = self._page_items(state, items)
        start = self.page * QUEUE_PAGE_SIZE
        selected_track = None
        if start < int(self.selected_position) <= start + len(page_items):
            selected_track = page_items[int(self.selected_position) - start - 1]
        await interaction.response.defer()
        if selected_track is not None and int(getattr(selected_track, "virtual_source_index", -1)) >= 0:
            result = await self.router.virtual_queue_action(self.guild_id, "play_now", selected_track)
            ok = bool(result.get("ok"))
        else:
            ok = await self.router.skip_to(self.guild_id, self.selected_position)
        await interaction.followup.send(
            "`▶️` Tocando a música selecionada." if ok else "Não consegui tocar essa música; a fila pode ter mudado.",
            ephemeral=True,
        )
        if ok:
            refresh = getattr(self.router, "refresh_queue_controller", None)
            if callable(refresh):
                await refresh(self.guild_id)
            self.selected_position = None
            self.page = min(self.page, self._max_page())
            await self._prepare_page()
            self._refresh_components()
            with contextlib.suppress(Exception):
                await interaction.edit_original_response(content=None, embeds=[], attachments=[], view=self)

    async def move_selected(self, interaction: discord.Interaction):
        if not self.selected_position:
            await interaction.response.send_message("Selecione uma música primeiro.", ephemeral=True)
            return
        state = self.router.get_state(self.guild_id)
        items = self._queue_items()
        page_items, _from_virtual = self._page_items(state, items)
        start = self.page * QUEUE_PAGE_SIZE
        selected_track = None
        if start < int(self.selected_position) <= start + len(page_items):
            selected_track = page_items[int(self.selected_position) - start - 1]
        await interaction.response.send_modal(
            MoveSelectedModal(
                self.router,
                self.guild_id,
                self.selected_position,
                page=self.page,
                owner_id=self.owner_id,
                message=getattr(interaction, "message", None),
                selected_track=selected_track,
            )
        )

    async def remove_selected(self, interaction: discord.Interaction):
        if not self.selected_position:
            await interaction.response.send_message("Selecione uma música primeiro.", ephemeral=True)
            return
        state = self.router.get_state(self.guild_id)
        items = self._queue_items()
        page_items, _from_virtual = self._page_items(state, items)
        start = self.page * QUEUE_PAGE_SIZE
        selected_track = None
        if start < int(self.selected_position) <= start + len(page_items):
            selected_track = page_items[int(self.selected_position) - start - 1]
        confirm_view = QueueConfirmView(
            self.router,
            self.guild_id,
            action="remove",
            owner_id=self.owner_id,
            page=self.page,
            position=self.selected_position,
            message=getattr(interaction, "message", None),
            selected_track=selected_track,
        )
        await interaction.response.send_message(view=confirm_view, ephemeral=True)
        with contextlib.suppress(Exception):
            confirm_view.confirm_message = await interaction.original_response()

    async def reload(self, interaction: discord.Interaction):
        await self._redraw(interaction)

    async def clear_queue(self, interaction: discord.Interaction):
        state = self.router.get_state(self.guild_id)
        if not self._queue_items() and not _virtual_playlist_info(state):
            await interaction.response.send_message("A fila já está vazia.", ephemeral=True)
            return
        confirm_view = QueueConfirmView(
            self.router,
            self.guild_id,
            action="clear",
            owner_id=self.owner_id,
            page=0,
            message=getattr(interaction, "message", None),
        )
        await interaction.response.send_message(view=confirm_view, ephemeral=True)
        with contextlib.suppress(Exception):
            confirm_view.confirm_message = await interaction.original_response()


class VoiceStatusTemplateModal(discord.ui.Modal):
    def __init__(self, parent: "VoiceStatusSettingsView") -> None:
        super().__init__(title="Editar status enquanto toca")
        self.parent = parent
        settings = parent.router.get_voice_status_settings(parent.guild_id)
        self.template = discord.ui.TextInput(
            label="Modelo do status do canal",
            placeholder="{source_emoji} {title}, {author} ({requester})",
            default=str(settings.get("template") or "")[:500],
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=500,
            required=True,
        )
        self.add_item(self.template)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.parent.router.is_music_staff(getattr(interaction, "user", None)):
            await interaction.response.send_message("Apenas staff pode configurar o status do canal de voz.", ephemeral=True)
            return
        await self.parent.router.set_voice_status_template(self.parent.guild_id, str(self.template.value or ""))
        view = VoiceStatusSettingsView(self.parent.router, self.parent.guild_id, owner_id=self.parent.owner_id)
        await interaction.response.edit_message(view=view)


class VoiceStatusIdleModal(discord.ui.Modal):
    def __init__(self, parent: "VoiceStatusSettingsView") -> None:
        super().__init__(title="Editar status parado")
        self.parent = parent
        settings = parent.router.get_voice_status_settings(parent.guild_id)
        self.idle = discord.ui.TextInput(
            label="Status quando não tiver música",
            placeholder="Deixe vazio para restaurar o status anterior do canal",
            default=str(settings.get("idle") or "")[:500],
            style=discord.TextStyle.paragraph,
            min_length=0,
            max_length=500,
            required=False,
        )
        self.add_item(self.idle)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.parent.router.is_music_staff(getattr(interaction, "user", None)):
            await interaction.response.send_message("Apenas staff pode configurar o status do canal de voz.", ephemeral=True)
            return
        await self.parent.router.set_voice_status_idle(self.parent.guild_id, str(self.idle.value or ""))
        view = VoiceStatusSettingsView(self.parent.router, self.parent.guild_id, owner_id=self.parent.owner_id)
        await interaction.response.edit_message(view=view)


class VoiceStatusPreviewView(discord.ui.LayoutView):
    def __init__(self, router, guild_id: int) -> None:
        super().__init__(timeout=120)
        preview = router.preview_voice_status(guild_id)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# 👁️ Pré-visualização do status"),
                discord.ui.TextDisplay(
                    "É assim que o canal de voz vai aparecer quando uma música estiver tocando:\n\n"
                    f"> {preview or 'sem status'}"
                ),
                discord.ui.Separator(),
                discord.ui.TextDisplay("-# A prévia usa a música atual quando existe; caso contrário, usa um exemplo."),
                accent_color=discord.Color.blurple(),
            )
        )


class VoiceStatusSettingsView(discord.ui.LayoutView):
    def __init__(self, router, guild_id: int, *, owner_id: int | None = None) -> None:
        super().__init__(timeout=300)
        self.router = router
        self.guild_id = int(guild_id)
        self.owner_id = int(owner_id or 0)
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id and interaction.user and int(interaction.user.id) != self.owner_id:
            await interaction.response.send_message(f"Apenas <@{self.owner_id}> pode usar este painel de configuração.", ephemeral=True)
            return False
        if not self.router.is_music_staff(getattr(interaction, "user", None)):
            await interaction.response.send_message("Apenas staff pode configurar o status do canal de voz.", ephemeral=True)
            return False
        return True

    def _settings_lines(self) -> list[str]:
        settings = self.router.get_voice_status_settings(self.guild_id)
        enabled = bool(settings.get("enabled", True))
        template = str(settings.get("template") or "")
        idle = str(settings.get("idle") or "")
        preview = self.router.preview_voice_status(self.guild_id)
        idle_text = idle if idle else "restaurar o status anterior do canal"
        metrics = self.router.get_voice_status_metrics(self.guild_id) if hasattr(self.router, "get_voice_status_metrics") else {}
        metric_line = (
            f"-# Diagnóstico: writes={int(metrics.get('write_success', 0))} • retries={int(metrics.get('retries', 0))} • "
            f"overrides={int(metrics.get('external_overrides', 0))} • stale={int(metrics.get('stale_generation_dropped', 0))}."
        )
        return [
            "# 🎙️ Status automático do canal de voz",
            "Configure como o bot mostra a música atual diretamente no status do canal de voz.",
            "",
            f"**Status:** {'ativado' if enabled else 'desativado'}",
            f"**Modelo tocando:** `{template}`",
            f"**Quando parar:** `{idle_text}`",
            "",
            "**Prévia:**",
            f"> {preview or 'sem status'}",
            "",
            "-# Variáveis: `{source_emoji}`, `{title}`, `{author}`, `{requester}`, `{elapsed}`, `{position}`, `{duration}`, `{remaining}`, `{queue}`, `{quality}`, `{kbps}`, `{state}`, `{paused}`, `{loop}`, `{volume}`.",
            "-# O bot salva o status antigo do canal e restaura depois que a música terminar, parar, mover ou após restart.",
            metric_line,
        ]

    def _build(self) -> None:
        self.clear_items()
        settings = self.router.get_voice_status_settings(self.guild_id)
        enabled = bool(settings.get("enabled", True))
        toggle = discord.ui.Button(
            label="Desativar" if enabled else "Ativar",
            emoji="🟢" if enabled else "⚪",
            style=discord.ButtonStyle.success if not enabled else discord.ButtonStyle.secondary,
        )
        toggle.callback = self.toggle_enabled
        edit_template = discord.ui.Button(label="Editar modelo", emoji="📝", style=discord.ButtonStyle.primary)
        edit_template.callback = self.edit_template
        edit_idle = discord.ui.Button(label="Status parado", emoji="💤", style=discord.ButtonStyle.secondary)
        edit_idle.callback = self.edit_idle
        preview = discord.ui.Button(label="Pré-visualizar", emoji="👁️", style=discord.ButtonStyle.secondary)
        preview.callback = self.preview
        reset = discord.ui.Button(label="Restaurar padrão", emoji="🔄", style=discord.ButtonStyle.danger)
        reset.callback = self.reset
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(self._settings_lines())),
                discord.ui.ActionRow(toggle, edit_template, edit_idle),
                discord.ui.ActionRow(preview, reset),
                accent_color=discord.Color.green() if enabled else discord.Color.dark_grey(),
            )
        )

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(view=self)

    async def toggle_enabled(self, interaction: discord.Interaction) -> None:
        settings = self.router.get_voice_status_settings(self.guild_id)
        await self.router.set_voice_status_enabled(self.guild_id, not bool(settings.get("enabled", True)))
        await self._redraw(interaction)

    async def edit_template(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(VoiceStatusTemplateModal(self))

    async def edit_idle(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(VoiceStatusIdleModal(self))

    async def preview(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(view=VoiceStatusPreviewView(self.router, self.guild_id), ephemeral=True)

    async def reset(self, interaction: discord.Interaction) -> None:
        await self.router.reset_voice_status_settings(self.guild_id)
        await self._redraw(interaction)

class PlayerOptionsSelect(discord.ui.Select):
    def __init__(self, router, guild_id: int) -> None:
        state = router.get_state(guild_id)
        volume_percent = int(round(float(getattr(state, "volume", 0.55)) * 100))
        options = [
            discord.SelectOption(label="Adicionar música", emoji="🎶", value="add_song", description="Adicionar uma música ou playlist na fila."),
            discord.SelectOption(label=f"Volume: {volume_percent}%", emoji="🔊", value="volume", description="Ajustar volume da música."),
            discord.SelectOption(label="Selecionar momento", emoji="💠", value="seek", description="Ir para um tempo específico da música."),
            discord.SelectOption(label="Repetição", emoji="🔁", value="loop", description="Alternar repetição da música/fila."),
            discord.SelectOption(label="Shuffle", emoji="🔀", value="shuffle", description="Embaralhar a fila uma vez."),
        ]
        super().__init__(placeholder="⚙️ Mais opções", min_values=1, max_values=1, options=options, custom_id="music:options")
        self.router = router
        self.guild_id = int(guild_id)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        state = self.router.get_state(self.guild_id)
        if value == "add_song":
            await interaction.response.send_modal(
                AddSongModal(
                    self.router,
                    self.guild_id,
                    voice_channel_id=state.last_voice_channel_id,
                    text_channel_id=state.last_text_channel_id,
                )
            )
            return
        if value == "volume":
            if not self.router.is_music_staff(getattr(interaction, "user", None)):
                await interaction.response.send_message("Apenas staff pode alterar o volume do player.", ephemeral=True)
                return
            await interaction.response.send_modal(VolumeModal(self.router, self.guild_id))
            return
        if value == "shuffle":
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
            _ok, message = await self.router.request_shuffle(self.guild_id, interaction.user)
            await _safe_interaction_followup(interaction, message, ephemeral=True)
            return
        if value == "seek":
            requester_id = _current_track_requester_id(state)
            if getattr(state, "current", None) is None:
                await interaction.response.send_message("Não há música tocando agora.", ephemeral=True)
                return
            if int(getattr(interaction.user, "id", 0) or 0) != requester_id:
                await interaction.response.send_message("Apenas quem adicionou a música atual pode selecionar o momento.", ephemeral=True)
                return
            await interaction.response.send_modal(SeekModal(self.router, self.guild_id))
            return
        if value == "loop":
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
            _ok, message = await self.router.request_loop(self.guild_id, interaction.user)
            await _safe_interaction_followup(interaction, message, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)


class MusicPlayerView(discord.ui.LayoutView):
    """Painel principal 100% Discord Components V2.

    O renderer usa apenas o estado já disponível em memória. Não faz consultas de
    rede, yt-dlp, banco, Lavalink ou worker para montar a interface.
    """

    def __init__(self, router, guild_id: int) -> None:
        super().__init__(timeout=None)
        self.router = router
        self.guild_id = int(guild_id)
        self._build()

    def _control_state(self, state, queue: list[MusicTrack]) -> dict[str, bool]:
        status = str(getattr(state, "current_status", "") or "")
        paused = bool(getattr(state, "paused", False)) or status == "paused"
        virtual = _virtual_playlist_info(state)
        has_current = bool(
            getattr(state, "current", None)
            or getattr(state, "current_source", None)
            or status in {"resolving", "starting", "reconnecting", "skipping", "playing", "paused"}
        )
        has_queue = bool(queue or virtual)
        has_history = bool(list(getattr(state, "history", []) or [])) or bool(
            int(getattr(state, "agent_remote_history_size", 0) or 0) > 0
        )
        has_session = bool(getattr(state, "music_session_active", False) or has_current or has_queue)
        return {
            "paused": paused,
            "has_current": has_current,
            "has_queue": has_queue,
            "has_history": has_history,
            "has_session": has_session,
            "invalid": _panel_controls_invalid(state),
        }

    def _build(self) -> None:
        self.clear_items()
        state = self.router.get_state(self.guild_id)
        current = getattr(state, "current", None)
        queue = _queue_items(state)
        virtual = _virtual_playlist_info(state)
        status_title, status_emoji, accent_color = _player_status_presentation(state)
        controls = self._control_state(state, queue)

        container = discord.ui.Container(accent_color=accent_color)
        # No embed antigo o indicador ficava ao lado do author/title. Em V2 ele
        # volta a ocupar essa posição como emoji inline, em vez de virar thumbnail.
        container.add_item(discord.ui.TextDisplay(f"**{status_emoji} {status_title}**"))

        if current is not None:
            track_text = discord.ui.TextDisplay(_player_track_text(state, current))
            if current.thumbnail:
                container.add_item(
                    discord.ui.Section(
                        track_text,
                        accessory=discord.ui.Thumbnail(current.thumbnail, description=_escape(current.short_title, limit=120)),
                    )
                )
            else:
                container.add_item(track_text)
        elif queue:
            first = queue[0]
            next_text = discord.ui.TextDisplay(
                "### Próxima música\n"
                f"{_track_link_v2(first, title_limit=88, bold=True)}\n"
                f"-# {first.duration_label}"
            )
            if first.thumbnail:
                container.add_item(
                    discord.ui.Section(
                        next_text,
                        accessory=discord.ui.Thumbnail(first.thumbnail, description=_escape(first.short_title, limit=120)),
                    )
                )
            else:
                container.add_item(next_text)
        elif virtual:
            title = _escape(str(virtual.get("title") or "playlist"), limit=88)
            container.add_item(
                discord.ui.TextDisplay(
                    "### Carregando próximas músicas\n"
                    f"**{title}**"
                )
            )
        else:
            container.add_item(discord.ui.TextDisplay(_idle_player_text(state)))

        # A barra animada pertence visualmente à faixa/estado atual, por isso fica
        # imediatamente antes da fila. Continua sendo o mesmo GIF e não cria
        # timer, polling ou qualquer I/O adicional.
        bar = discord.ui.MediaGallery()
        bar.add_item(media=PLAYER_BAR_URL, description="Barra animada do player")
        container.add_item(bar)

        # Em estado ocioso a mensagem acima já explica por que o player parou
        # e como iniciar novamente. Não repetimos um segundo bloco "Fila · vazia".
        if current is not None or queue:
            container.add_item(discord.ui.TextDisplay(_queue_preview_text(state, limit=4)))
        elif virtual:
            container.add_item(discord.ui.TextDisplay(_queue_preview_text(state, limit=4)))

        vote_lines = [f"{label}: {count}/{needed}" for label, count, needed in list(getattr(state, "panel_vote_summary", []) or [])]
        if vote_lines:
            container.add_item(discord.ui.TextDisplay("-# 🗳️ " + " · ".join(vote_lines)))

        container.add_item(discord.ui.Separator())

        invalid = controls["invalid"]
        back = discord.ui.Button(
            emoji="⏮️",
            style=discord.ButtonStyle.secondary,
            disabled=not controls["has_history"],
            custom_id="music:back",
        )
        back.callback = self.back
        pause = discord.ui.Button(
            emoji="▶️" if controls["paused"] else "⏸️",
            style=discord.ButtonStyle.primary if controls["paused"] else discord.ButtonStyle.secondary,
            disabled=invalid or not controls["has_current"],
            custom_id="music:pause_resume",
        )
        pause.callback = self.pause_resume
        skip = discord.ui.Button(
            emoji="⏭️",
            style=discord.ButtonStyle.secondary,
            disabled=invalid or not (controls["has_current"] or controls["has_queue"]),
            custom_id="music:skip",
        )
        skip.callback = self.skip
        stop = discord.ui.Button(
            emoji="⏹️",
            style=discord.ButtonStyle.danger,
            disabled=invalid or not controls["has_session"],
            custom_id="music:stop",
        )
        stop.callback = self.stop
        queue_button = discord.ui.Button(
            emoji="📜",
            style=discord.ButtonStyle.secondary,
            disabled=False,
            custom_id="music:queue",
        )
        queue_button.callback = self.queue
        container.add_item(discord.ui.ActionRow(back, pause, skip, stop, queue_button))

        options = PlayerOptionsSelect(self.router, self.guild_id)
        options.disabled = invalid
        container.add_item(discord.ui.ActionRow(options))
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        state = self.router.get_state(self.guild_id)
        if _panel_controls_invalid(state):
            custom_id = str((getattr(interaction, "data", {}) or {}).get("custom_id") or "")
            has_history = bool(list(getattr(state, "history", []) or [])) or bool(
                int(getattr(state, "agent_remote_history_size", 0) or 0) > 0
            )
            if custom_id != "music:back" or not has_history:
                await _send_interaction_notice(
                    interaction,
                    "`⌛` Esse painel expirou. Use `_play <link ou pesquisa>` para começar de novo.",
                )
                return False
        # O callback confirma a interação antes de qualquer I/O do worker.
        return await _require_music_voice_interaction(interaction, self.router, self.guild_id, check_worker=False)

    async def _ack(self, interaction: discord.Interaction, message: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.NotFound:
            return
        except Exception:
            logger.debug("[music/ui] falha ao confirmar interação", exc_info=True)

    async def _defer_control(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.NotFound:
                return
            except Exception:
                logger.debug("[music/ui] falha ao deferir controle", exc_info=True)

    async def back(self, interaction: discord.Interaction):
        await self._defer_control(interaction)
        ok = await self.router.previous(self.guild_id)
        await self._ack(interaction, "`⏮️` Voltando para a música anterior." if ok else "Não há música anterior no histórico.")

    def _music_agent_default_enabled(self) -> bool:
        return bool(getattr(config, "MUSIC_AGENT_ENABLED", True)) and getattr(
            self.router, "music_worker_only_enabled", lambda: False
        )()

    async def _send_agent_control(self, interaction: discord.Interaction, action: str, message: str) -> bool:
        if not self._music_agent_default_enabled():
            return False
        if not interaction.response.is_done():
            with contextlib.suppress(Exception):
                await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await enviar_controle_remoto(
                self.router,
                action,
                guild_id=self.guild_id,
                requester_id=getattr(interaction.user, "id", 0),
                requester_name=getattr(interaction.user, "display_name", str(interaction.user)),
                create_panel=True,
            )
        except Exception as exc:
            await self._ack(interaction, f"`⚠️` O player não respondeu: `{str(exc)[:180]}`")
            return True
        await self._ack(interaction, message)
        return True

    async def pause_resume(self, interaction: discord.Interaction):
        await self._defer_control(interaction)
        state = self.router.get_state(self.guild_id)
        if state.paused:
            if await self._send_agent_control(interaction, "resume", "`▶️` Música retomada."):
                return
            ok = await self.router.resume(self.guild_id)
            if ok:
                await self._defer_control(interaction)
            else:
                await self._ack(interaction, "Não havia música pausada.")
        else:
            if await self._send_agent_control(interaction, "pause", "`⏸️` Música pausada."):
                return
            ok = await self.router.pause(self.guild_id)
            if ok:
                await self._defer_control(interaction)
            else:
                await self._ack(interaction, "Não havia música tocando.")

    async def skip(self, interaction: discord.Interaction):
        await self._defer_control(interaction)
        if await self._send_agent_control(interaction, "skip", "`⏭️` Pulando música."):
            return
        _ok, message = await self.router.request_skip(self.guild_id, interaction.user)
        await self._ack(interaction, message)

    async def stop(self, interaction: discord.Interaction):
        await self._defer_control(interaction)
        if await self._send_agent_control(interaction, "stop", "`⏹️` Player encerrado e desconectado."):
            return
        _ok, message = await self.router.request_stop(self.guild_id, interaction.user, disconnect=True)
        await self._ack(interaction, message)

    async def queue(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        try:
            refresh = getattr(self.router, "refresh_queue_controller", None)
            if callable(refresh):
                await refresh(self.guild_id)
            view = QueueView(
                self.router,
                self.guild_id,
                0,
                owner_id=getattr(interaction.user, "id", None),
            )
            await view._prepare_page()
            view._refresh_components()
            await interaction.followup.send(view=view, ephemeral=True)
        except discord.NotFound:
            return
