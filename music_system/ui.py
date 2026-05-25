from __future__ import annotations

import asyncio
import contextlib
import math
import logging
import time
from typing import Optional

import discord

import config

from .errors import MusicExtractionError
from .models import ExtractedBatch, MusicTrack
from .providers import describe_url
from .worker_node import music_agent_command, music_agent_status, resolve_music_tracks_on_worker
from .loading_reaction import MusicLoadingReaction

PLAYER_BAR_URL = "https://cdn.discordapp.com/attachments/554468640942981147/1127294696025227367/rainbow_bar3.gif"
QUEUE_PAGE_SIZE = 8
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
    # precisa renderizar o candidato que acabou de entrar na fila.
    source_payload = {} if queued else current
    title = useful(source_payload.get("display_title")) or useful(source_payload.get("title")) or useful(source_payload.get("name")) or track.short_title
    if len(title) > 90:
        title = title[:87].rstrip() + "..."
    duration_label = track.duration_label
    try:
        duration = None if queued else current.get("duration")
        if duration is not None and str(duration) != "":
            total = max(0, int(float(duration)))
            minutes, seconds = divmod(total, 60)
            hours, minutes = divmod(minutes, 60)
            duration_label = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    except Exception:
        pass
    status = str(state.get("status") or "").lower()
    if queued:
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
        limit = max(0, min(3, int(getattr(config, "MUSIC_AGENT_PREFETCH_TOP_RESULTS", 2) or 0)))
    except Exception:
        limit = 2
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

    asyncio.create_task(runner())


async def _watch_agent_message(message, guild_id: int, track: MusicTrack, *, router=None, voice_channel_id: int = 0, text_channel_id: int = 0, seconds: float | None = None, loading_reaction: MusicLoadingReaction | None = None) -> None:
    limit = float(seconds or getattr(config, "MUSIC_AGENT_PLAY_STATUS_WATCH_SECONDS", 30.0) or 30.0)
    deadline = asyncio.get_running_loop().time() + max(5.0, limit)
    last_status = ""
    poll = max(0.4, min(1.5, float(getattr(config, "MUSIC_AGENT_STATUS_POLL_SECONDS", 0.75) or 0.75)))
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(poll)
        try:
            payload = await music_agent_status(timeout_seconds=getattr(config, "MUSIC_AGENT_STATUS_TIMEOUT_SECONDS", 3.5))
            state = _agent_guild_state(payload, guild_id)
            status = str(state.get("status") or "").lower()
            if router is not None:
                await _sync_agent_panel(router, guild_id, voice_channel_id, text_channel_id, track, {"state": state})
            if not status or status == last_status:
                continue
            last_status = status
            if _agent_confirmed_playing(state):
                await message.edit(content=_music_agent_play_message(track, {"state": state}), embed=None, view=None)
                if loading_reaction is not None:
                    await loading_reaction.finish()
                return
            if status in {"failed", "error"}:
                error = str(state.get("last_error") or "fonte de áudio falhou").strip()[:220]
                await message.edit(content=f"`⚠️` Não consegui iniciar **{track.short_title}**: `{error}`", embed=None, view=None)
                if loading_reaction is not None:
                    await loading_reaction.finish()
                return
            if status in {"idle", "stopped"} and not state.get("current"):
                await message.edit(content=f"`⚠️` A música não ficou ativa para **{track.short_title}**. Tente novamente.", embed=None, view=None)
                if loading_reaction is not None:
                    await loading_reaction.finish()
                return
        except Exception:
            # Não quebra o fluxo do usuário se o acompanhamento não conseguir consultar o worker.
            continue
    with contextlib.suppress(Exception):
        await message.edit(content=f"`⚠️` Demorei para confirmar o início de **{track.short_title}**. Tente novamente se não tocar.", embed=None, view=None)
    if loading_reaction is not None:
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
    raw = (query or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    profile = describe_url(raw)
    if lower.startswith(("scsearch:", "spsearch:", "amsearch:", "dzsearch:")):
        return True
    if profile.is_youtube and profile.resource_type == "playlist":
        return True
    if profile.platform in {"spotify", "soundcloud", "apple", "deezer"}:
        return True
    return False


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
        if _worker_only_should_use_lavalink(query):
            if _is_lavalink_search_request(query):
                batch = await backends.search_lavalink_tracks(
                    query,
                    requester_id=requester_id,
                    requester_name=requester_name,
                    guild_id=guild_id,
                    limit=max(1, min(10, int(getattr(config, "MUSIC_SEARCH_RESULTS", 5) or 5))),
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
        youtube_text_search = _is_youtube_text_search(query)
        batch = await resolve_music_tracks_on_worker(
            query,
            requester_id=requester_id,
            requester_name=requester_name,
            limit=(max(1, min(10, int(getattr(config, "MUSIC_SEARCH_RESULTS", 5) or 5))) if youtube_text_search else 1),
            metadata_only=youtube_text_search,
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
                limit=max(1, min(10, int(getattr(config, "MUSIC_SEARCH_RESULTS", 5) or 5))),
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


async def _require_music_voice_interaction(interaction: discord.Interaction, router, guild_id: int, *, same_as_bot: bool = True) -> bool:
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
    if getattr(router, "music_worker_only_enabled", lambda: False)():
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


def _track_link(track: MusicTrack, *, title_limit: int = 82) -> str:
    title = _escape(track.short_title or track.title, limit=title_limit)
    if track.display_url:
        return f"[`{title}`]({track.display_url})"
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


def build_now_playing_embeds(state, track: MusicTrack) -> list[discord.Embed]:
    """Painel inspirado no MuseHeart, adaptado para discord.py/FFmpeg."""
    status = str(getattr(state, "current_status", "playing") or "playing")
    paused = bool(getattr(state, "paused", False)) or status == "paused"
    skipping = status == "skipping"
    loading = status in {"resolving", "starting", "skipping"}
    errored = status == "error"
    color = discord.Color.gold() if paused or loading else discord.Color.red() if errored else discord.Color.blurple()
    queue = _queue_items(state)
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
        if queue:
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
    elif loading:
        lines.append("> -# 🎚️ **⠂** `Qualidade: resolvendo`")

    loop_mode = getattr(state, "loop_mode", None)
    loop_label = getattr(loop_mode, "label", "desligado")
    if loop_label and loop_label != "desligado":
        loop_emoji = "🔂" if loop_label == "música atual" else "🔁"
        lines.append(f"> -# {loop_emoji} **⠂** `Repetição: {loop_label}`")

    if getattr(state, "shuffle", False):
        lines.append("> -# 🔀 **⠂** `Queue misturado`")

    if queue:
        lines.append(f"> -# 🎶 **⠂** `{len(queue)} música{'s' if len(queue) != 1 else ''} no queue`")


    for label, count, needed in list(getattr(state, "panel_vote_summary", []) or []):
        lines.append(f"> -# 🗳️ **⠂** `{label}: {count}/{needed}`")

    embed.description = "\n".join(lines)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    embed.set_image(url=PLAYER_BAR_URL)
    embed.set_footer(text="Use os botões ou o menu abaixo para controlar o player.")

    embeds: list[discord.Embed] = []
    if queue:
        mini = discord.Embed(
            title=f"Músicas no queue: {len(queue)}",
            color=discord.Color.blurple(),
        )
        mini_lines = []
        for n, item in enumerate(queue[:3], start=1):
            mini_lines.append(f"-# `{n:02}) [{item.duration_label}]` {_track_link(item, title_limit=42)}")
        if len(queue) > 3:
            mini_lines.append(f"-# `+ {len(queue) - 3} restante(s)`")
        mini_lines.append(f"-# `⌛ Duração aproximada do queue: {_queue_duration_label(queue)}`")
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
        lines = [
            f"> -# 🎶 **⠂** `{len(queue)} música{'s' if len(queue) != 1 else ''} aguardando`",
            f"> -# ⌛ **⠂** `Duração aproximada: {_queue_duration_label(queue)}`",
        ]
        for n, item in enumerate(queue[:5], start=1):
            lines.append(f"-# `{n:02}) [{item.duration_label}]` {_track_link(item, title_limit=48)}")
        if len(queue) > 5:
            lines.append(f"-# `+ {len(queue) - 5} restante(s)`")
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
            embed.set_author(name="As músicas acabaram", icon_url="https://i.ibb.co/QXtk5VB/neon-circle.gif")
            embed.description = (
                "O queue terminou e não tem mais nada para tocar.\n"
                "Use `_play <link ou pesquisa>` para adicionar outra música."
            )
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
            embed.set_author(name="Player encerrado", icon_url="https://cdn.discordapp.com/emojis/1215703754471268414.png")
            embed.description = (
                "A reprodução foi parada e o queue foi limpo.\n"
                "Use `_play <link ou pesquisa>` quando quiser tocar algo de novo."
            )
        elif reason == "external_disconnect":
            embed.set_author(name="Player interrompido", icon_url="https://cdn.discordapp.com/emojis/1215703754471268414.png")
            if actor_id:
                who = f"<@{int(actor_id)}>"
            elif actor_name:
                who = _escape(actor_name, limit=48)
            else:
                who = "alguém"
            where = f" de **{_escape(channel_name, limit=48)}**" if channel_name else ""
            embed.description = (
                f"O bot foi desconectado{where} por {who}.\n"
                "Use `_play <link ou pesquisa>` para iniciar novamente."
            )
        elif reason == "external_move":
            embed.set_author(name="Player movido", icon_url="https://i.ibb.co/QXtk5VB/neon-circle.gif")
            if actor_id:
                who = f" por <@{int(actor_id)}>"
            elif actor_name:
                who = f" por {_escape(actor_name, limit=48)}"
            else:
                who = ""
            where = f" para **{_escape(channel_name, limit=48)}**" if channel_name else ""
            embed.description = f"O bot foi movido{where}{who}."
        else:
            embed.set_author(name="Nada tocando agora", icon_url="https://i.ibb.co/QXtk5VB/neon-circle.gif")
            embed.description = "Use `_play <link ou pesquisa>` para adicionar uma música."

    embed.set_image(url=PLAYER_BAR_URL)
    return [embed]


def build_now_playing_embed(state, track: MusicTrack) -> discord.Embed:
    return build_now_playing_embeds(state, track)[-1]


def build_queue_embed(state, page: int = 0, *, selected_position: int | None = None) -> discord.Embed:
    items = _queue_items(state)
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
        lines.append(f"-# ⏳ Duração aproximada do queue: `{_queue_duration_label(items)}`")

    embed.description = "\n".join(lines)
    if selected_position and 1 <= selected_position <= len(items):
        selected = items[selected_position - 1]
        if selected.thumbnail:
            embed.set_thumbnail(url=selected.thumbnail)
        embed.set_footer(text=f"Posição {selected_position} selecionada • escolha uma ação abaixo")
    elif items:
        embed.set_footer(text=f"{len(items)} item(ns) no queue • selecione uma música para ver ações")
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
        if not await _require_music_voice_interaction(interaction, self.router, self.guild_id):
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
        await self.router.set_volume(self.guild_id, value)
        await interaction.response.send_message(f"🔊 Volume da música: `{value}%`.", ephemeral=True)


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
        if not await _require_music_voice_interaction(interaction, self.router, self.guild_id):
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
    def __init__(self, router, guild_id: int, voice_channel_id: int, text_channel_id: int, tracks: list[MusicTrack], requester_id: int | None = None) -> None:
        self.router = router
        self.guild_id = int(guild_id)
        self.voice_channel_id = int(voice_channel_id)
        self.text_channel_id = int(text_channel_id)
        self.tracks = tracks
        self.requester_id = int(requester_id or 0)
        options = []
        for idx, track in enumerate(tracks[:10]):
            options.append(
                discord.SelectOption(
                    label=track.short_title[:100],
                    description=f"{track.uploader or track.source or 'resultado'} • {track.duration_label}"[:100],
                    value=str(idx),
                    emoji="🎵",
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
            if bool(getattr(config, "MUSIC_AGENT_ENABLED", True)) and getattr(self.router, "music_worker_only_enabled", lambda: False)():
                try:
                    result = await music_agent_command(
                        "play",
                        guild_id=self.guild_id,
                        voice_channel_id=getattr(voice_channel, "id", self.voice_channel_id),
                        text_channel_id=getattr(text_channel, "id", self.text_channel_id),
                        query=track.webpage_url or track.original_url or track.title,
                        track=track,
                        requester_id=getattr(interaction.user, "id", 0),
                        requester_name=getattr(interaction.user, "display_name", str(interaction.user)),
                    )
                except Exception as exc:
                    await edit_original(f"`⚠️` Não consegui preparar essa música: `{exc}`")
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
                        asyncio.create_task(_watch_agent_message(message, self.guild_id, track, router=self.router, voice_channel_id=getattr(voice_channel, "id", self.voice_channel_id), text_channel_id=getattr(text_channel, "id", self.text_channel_id), loading_reaction=loading_reaction))
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
    def __init__(self, router, guild_id: int, voice_channel_id: int, text_channel_id: int, tracks: list[MusicTrack], requester_id: int | None = None) -> None:
        super().__init__(timeout=120)
        self.add_item(SearchSelect(router, guild_id, voice_channel_id, text_channel_id, tracks, requester_id))


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
        if not await _require_music_voice_interaction(interaction, self.router, self.guild_id):
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

        if not batch.tracks:
            await interaction.followup.send("`📭` Não encontrei nada tocável.", ephemeral=True)
            return

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
            await interaction.followup.send(
                embed=embed,
                view=SearchResultView(self.router, guild.id, getattr(voice_channel, "id", 0), getattr(text_channel, "id", 0), batch.tracks[:10], interaction.user.id),
                ephemeral=True,
            )
            _schedule_agent_prefetch(
                self.router,
                guild.id,
                batch.tracks[:10],
                voice_channel_id=getattr(voice_channel, "id", 0),
                text_channel_id=getattr(text_channel, "id", 0),
                requester_id=interaction.user.id,
                requester_name=requester_name,
            )
            return

        if bool(getattr(config, "MUSIC_AGENT_ENABLED", True)) and getattr(self.router, "music_worker_only_enabled", lambda: False)():
            track = batch.tracks[0]
            try:
                result = await music_agent_command(
                    "play",
                    guild_id=guild.id,
                    voice_channel_id=getattr(voice_channel, "id", self.voice_channel_id),
                    text_channel_id=getattr(text_channel, "id", self.text_channel_id),
                    query=track.webpage_url or track.original_url or query,
                    track=track,
                    requester_id=interaction.user.id,
                    requester_name=requester_name,
                )
            except Exception as exc:
                await interaction.followup.send(f"`⚠️` Não consegui preparar essa música: `{exc}`", ephemeral=True)
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
            msg = _agent_play_message(track, result)
            sent = await interaction.followup.send(msg, ephemeral=True, wait=True)
            state = result.get("state") if isinstance(result.get("state"), dict) else {}
            status = str(state.get("status") or "").lower()
            if sent is not None and not result.get("queued") and status not in {"playing", "failed", "error"}:
                asyncio.create_task(_watch_agent_message(sent, guild.id, track, router=self.router, voice_channel_id=getattr(voice_channel, "id", self.voice_channel_id), text_channel_id=getattr(text_channel, "id", self.text_channel_id)))
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
    def __init__(self, router, guild_id: int, page: int = 0, selected_position: int | None = None) -> None:
        self.router = router
        self.guild_id = int(guild_id)
        self.page = max(0, int(page))
        self.selected_position = selected_position
        items = router.snapshot_queue(guild_id)
        start = self.page * QUEUE_PAGE_SIZE
        options = []
        for idx, track in enumerate(items[start : start + QUEUE_PAGE_SIZE], start=start + 1):
            options.append(
                discord.SelectOption(
                    label=f"{idx}. {track.short_title}"[:100],
                    description=f"{track.duration_label} • {track.uploader or track.source or 'queue'}"[:100],
                    value=str(idx),
                    emoji="🎵",
                    default=selected_position == idx,
                )
            )
        super().__init__(placeholder="Selecione uma música do queue", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, QueueView):
            view.selected_position = int(self.values[0])
            await view._redraw(interaction)
            return
        await interaction.response.defer()


class MoveSelectedModal(discord.ui.Modal):
    def __init__(self, router, guild_id: int, from_pos: int, *, page: int = 0, owner_id: int | None = None, message=None) -> None:
        super().__init__(title="Mover música selecionada")
        self.router = router
        self.guild_id = int(guild_id)
        self.from_pos = int(from_pos)
        self.page = max(0, int(page))
        self.owner_id = int(owner_id or 0)
        self.message = message
        self.to_pos = discord.ui.TextInput(
            label="Nova posição no queue",
            placeholder="Exemplo: 1",
            min_length=1,
            max_length=4,
            required=True,
        )
        self.add_item(self.to_pos)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await _require_music_voice_interaction(interaction, self.router, self.guild_id):
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
        ok = await self.router.move(self.guild_id, self.from_pos, to_pos)
        await interaction.response.send_message("`↪️` Música movida." if ok else "Não consegui mover: confira a posição no queue.", ephemeral=True)
        if ok and self.message is not None:
            view = QueueView(self.router, self.guild_id, self.page, owner_id=self.owner_id)
            with contextlib.suppress(Exception):
                await self.message.edit(embed=build_queue_embed(self.router.get_state(self.guild_id), view.page), view=view)


class QueueConfirmView(discord.ui.View):
    def __init__(self, router, guild_id: int, *, action: str, owner_id: int | None = None, page: int = 0, position: int | None = None, message=None) -> None:
        super().__init__(timeout=45)
        self.router = router
        self.guild_id = int(guild_id)
        self.action = action
        self.owner_id = int(owner_id or 0)
        self.page = max(0, int(page))
        self.position = int(position or 0)
        self.message = message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id and interaction.user and interaction.user.id != self.owner_id:
            await interaction.response.send_message(f"Apenas <@{self.owner_id}> pode confirmar essa ação.", ephemeral=True)
            return False
        return await _require_music_voice_interaction(interaction, self.router, self.guild_id)

    async def _refresh_parent(self) -> None:
        if self.message is None:
            return
        view = QueueView(self.router, self.guild_id, self.page, owner_id=self.owner_id)
        with contextlib.suppress(Exception):
            await self.message.edit(embed=build_queue_embed(self.router.get_state(self.guild_id), view.page), view=view)

    @discord.ui.button(label="Confirmar", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.action == "clear":
            await self.router.replace_queue(self.guild_id, [])
            await interaction.response.edit_message(content="`🧹` Queue limpo.", view=None)
            await self._refresh_parent()
            self.stop()
            return

        if self.action == "remove":
            removed = await self.router.remove_at(self.guild_id, self.position)
            if removed is None:
                await interaction.response.edit_message(content="Essa posição não existe mais no queue.", view=None)
            else:
                await interaction.response.edit_message(content=f"`🗑️` Removido do queue: **{_escape(removed.short_title, limit=80)}**.", view=None)
            await self._refresh_parent()
            self.stop()
            return

        await interaction.response.edit_message(content="Ação desconhecida.", view=None)
        self.stop()

    @discord.ui.button(label="Cancelar", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Ação cancelada.", view=None)
        self.stop()


class QueueView(discord.ui.View):
    def __init__(self, router, guild_id: int, page: int = 0, *, owner_id: int | None = None, selected_position: int | None = None) -> None:
        super().__init__(timeout=300)
        self.router = router
        self.guild_id = int(guild_id)
        self.page = max(0, int(page))
        self.owner_id = int(owner_id or 0)
        self.selected_position = selected_position
        self._refresh_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id and interaction.user and interaction.user.id != self.owner_id:
            await interaction.response.send_message(f"Apenas <@{self.owner_id}> pode interagir nesse painel de queue.", ephemeral=True)
            return False
        return await _require_music_voice_interaction(interaction, self.router, self.guild_id)

    def _queue_items(self) -> list[MusicTrack]:
        return self.router.snapshot_queue(self.guild_id)

    def _max_page(self) -> int:
        items = self._queue_items()
        return max(0, (len(items) - 1) // QUEUE_PAGE_SIZE)

    def _refresh_components(self) -> None:
        self.clear_items()
        items = self._queue_items()
        max_page = self._max_page()
        self.page = max(0, min(self.page, max_page))
        if self.selected_position and not (1 <= self.selected_position <= len(items)):
            self.selected_position = None

        if items:
            self.add_item(QueueSelect(self.router, self.guild_id, self.page, self.selected_position))

        row = 1
        if self.selected_position:
            play = discord.ui.Button(label="Tocar agora", emoji="▶️", style=discord.ButtonStyle.primary, row=row)
            play.callback = self.play_selected
            self.add_item(play)
            move = discord.ui.Button(label="Mover", emoji="↪️", style=discord.ButtonStyle.secondary, row=row)
            move.callback = self.move_selected
            self.add_item(move)
            remove = discord.ui.Button(label="Remover", emoji="🗑️", style=discord.ButtonStyle.danger, row=row)
            remove.callback = self.remove_selected
            self.add_item(remove)
            row += 1

        if max_page > 0:
            previous = discord.ui.Button(emoji="⬅️", style=discord.ButtonStyle.secondary, row=row, disabled=self.page <= 0)
            previous.callback = self.previous_page
            self.add_item(previous)
            page_label = discord.ui.Button(label=f"Página {self.page + 1}/{max_page + 1}", style=discord.ButtonStyle.secondary, row=row, disabled=True)
            self.add_item(page_label)
            next_button = discord.ui.Button(emoji="➡️", style=discord.ButtonStyle.secondary, row=row, disabled=self.page >= max_page)
            next_button.callback = self.next_page
            self.add_item(next_button)
            row += 1

        if items:
            clear = discord.ui.Button(label="Limpar queue", emoji="🧹", style=discord.ButtonStyle.danger, row=row)
            clear.callback = self.clear_queue
            self.add_item(clear)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._refresh_components()
        embed = build_queue_embed(self.router.get_state(self.guild_id), self.page, selected_position=self.selected_position)
        await interaction.response.edit_message(embed=embed, view=self)

    async def previous_page(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self.selected_position = None
        await self._redraw(interaction)

    async def next_page(self, interaction: discord.Interaction):
        self.page = min(self._max_page(), self.page + 1)
        self.selected_position = None
        await self._redraw(interaction)

    async def cancel_selection(self, interaction: discord.Interaction):
        self.selected_position = None
        await self._redraw(interaction)

    async def play_selected(self, interaction: discord.Interaction):
        if not self.selected_position:
            await interaction.response.send_message("Selecione uma música primeiro.", ephemeral=True)
            return
        ok = await self.router.skip_to(self.guild_id, self.selected_position)
        await interaction.response.send_message("`▶️` Tocando a música selecionada." if ok else "Não consegui tocar essa posição no queue.", ephemeral=True)

    async def move_selected(self, interaction: discord.Interaction):
        if not self.selected_position:
            await interaction.response.send_message("Selecione uma música primeiro.", ephemeral=True)
            return
        await interaction.response.send_modal(
            MoveSelectedModal(
                self.router,
                self.guild_id,
                self.selected_position,
                page=self.page,
                owner_id=self.owner_id,
                message=getattr(interaction, "message", None),
            )
        )

    async def remove_selected(self, interaction: discord.Interaction):
        if not self.selected_position:
            await interaction.response.send_message("Selecione uma música primeiro.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Remover esta música do queue?",
            view=QueueConfirmView(
                self.router,
                self.guild_id,
                action="remove",
                owner_id=self.owner_id,
                page=self.page,
                position=self.selected_position,
                message=getattr(interaction, "message", None),
            ),
            ephemeral=True,
        )

    async def reload(self, interaction: discord.Interaction):
        await self._redraw(interaction)

    async def clear_queue(self, interaction: discord.Interaction):
        if not self._queue_items():
            await interaction.response.send_message("O queue já está vazio.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Limpar todas as músicas do queue?",
            view=QueueConfirmView(
                self.router,
                self.guild_id,
                action="clear",
                owner_id=self.owner_id,
                page=0,
                message=getattr(interaction, "message", None),
            ),
            ephemeral=True,
        )



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
            "-# Variáveis: `{source_emoji}`, `{title}`, `{author}`, `{requester}`, `{elapsed}`, `{duration}`, `{remaining}`, `{queue}`, `{quality}`, `{kbps}`.",
            "-# O bot salva o status antigo do canal e restaura depois que a música terminar, parar, mover ou após restart.",
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
            discord.SelectOption(label="Adicionar música", emoji="🎶", value="add_song", description="Adicionar uma música ou playlist no queue."),
            discord.SelectOption(label=f"Volume: {volume_percent}%", emoji="🔊", value="volume", description="Ajustar volume da música."),
            discord.SelectOption(label="Selecionar momento", emoji="💠", value="seek", description="Ir para um tempo específico da música."),
            discord.SelectOption(label="Repetição", emoji="🔁", value="loop", description="Alternar repetição da música/queue."),
            discord.SelectOption(label="Shuffle", emoji="🔀", value="shuffle", description="Misturar o queue."),
        ]
        super().__init__(placeholder="⚙️ Mais opções", min_values=1, max_values=1, options=options, row=1)
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
            _ok, message = await self.router.request_shuffle(self.guild_id, interaction.user)
            await interaction.response.send_message(message, ephemeral=True)
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
            _ok, message = await self.router.request_loop(self.guild_id, interaction.user)
            await interaction.response.send_message(message, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)


class MusicPlayerView(discord.ui.View):
    def __init__(self, router, guild_id: int) -> None:
        super().__init__(timeout=None)
        self.router = router
        self.guild_id = int(guild_id)
        self.add_item(PlayerOptionsSelect(router, guild_id))
        self._sync_components()

    def _emoji_name(self, item) -> str:
        emoji = getattr(item, "emoji", None)
        return str(getattr(emoji, "name", None) or emoji or "")

    def _sync_components(self) -> None:
        state = self.router.get_state(self.guild_id)
        status = str(getattr(state, "current_status", "") or "")
        paused = bool(getattr(state, "paused", False)) or status == "paused"
        has_current = bool(getattr(state, "current", None) or getattr(state, "current_source", None) or status in {"resolving", "starting", "skipping", "playing", "paused"})
        has_queue = bool(_queue_items(state))
        has_history = bool(list(getattr(state, "history", []) or []))
        has_session = bool(getattr(state, "music_session_active", False) or has_current or has_queue)
        controls_invalid = _panel_controls_invalid(state)

        for item in self.children:
            if isinstance(item, discord.ui.Select):
                item.disabled = controls_invalid
                continue
            if not isinstance(item, discord.ui.Button):
                continue
            item.label = None
            emoji_name = self._emoji_name(item)
            custom_id = str(getattr(item, "custom_id", "") or "")
            if controls_invalid:
                # Painel encerrado expira controles destrutivos, mas o botão de
                # voltar continua útil quando há histórico tocável após fila vazia.
                item.disabled = not (emoji_name == "⏮️" and has_history)
                continue
            if custom_id.endswith(":pause_resume") or emoji_name in {"⏸️", "▶️"}:
                item.emoji = "▶️" if paused else "⏸️"
                item.style = discord.ButtonStyle.primary if paused else discord.ButtonStyle.secondary
                item.disabled = not has_current
            elif emoji_name == "⏮️":
                item.disabled = not has_history
            elif emoji_name == "⏭️":
                item.disabled = not (has_current or has_queue)
            elif custom_id.endswith(":stop") or emoji_name == "⏹️":
                item.disabled = not has_session
            elif emoji_name == "📜":
                item.disabled = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        state = self.router.get_state(self.guild_id)
        if _panel_controls_invalid(state):
            custom_id = str((getattr(interaction, "data", {}) or {}).get("custom_id") or "")
            has_history = bool(list(getattr(state, "history", []) or []))
            if not (custom_id.endswith(":back") or custom_id == "") or not has_history:
                await _send_interaction_notice(interaction, "`⌛` Esse painel expirou. Use `_play <link ou pesquisa>` para começar de novo.")
                return False
        return await _require_music_voice_interaction(interaction, self.router, self.guild_id)

    async def _ack(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _defer_control(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0, custom_id="music:back")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok = await self.router.previous(self.guild_id)
        await self._ack(interaction, "`⏮️` Voltando para a música anterior." if ok else "Não há música anterior no histórico.")

    def _music_agent_default_enabled(self) -> bool:
        return bool(getattr(config, "MUSIC_AGENT_ENABLED", True)) and getattr(self.router, "music_worker_only_enabled", lambda: False)()

    async def _send_agent_control(self, interaction: discord.Interaction, action: str, message: str) -> bool:
        if not self._music_agent_default_enabled():
            return False
        try:
            result = await music_agent_command(action, guild_id=self.guild_id, requester_id=getattr(interaction.user, "id", 0), requester_name=getattr(interaction.user, "display_name", str(interaction.user)))
        except Exception as exc:
            await self._ack(interaction, f"`⚠️` O player não respondeu: `{str(exc)[:180]}`")
            return True
        state = result.get("state") if isinstance(result, dict) and isinstance(result.get("state"), dict) else {}
        if state:
            with contextlib.suppress(Exception):
                await self.router.sync_music_agent_state(
                    self.guild_id,
                    None,
                    state,
                    voice_channel_id=state.get("voice_channel_id"),
                    text_channel_id=state.get("text_channel_id"),
                    create_panel=True,
                )
        await self._ack(interaction, message)
        return True

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.secondary, row=0, custom_id="music:pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
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

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._send_agent_control(interaction, "skip", "`⏭️` Pulando música."):
            return
        _ok, message = await self.router.request_skip(self.guild_id, interaction.user)
        await self._ack(interaction, message)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0, custom_id="music:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._send_agent_control(interaction, "stop", "`⏹️` Player encerrado e desconectado."):
            return
        _ok, message = await self.router.request_stop(self.guild_id, interaction.user, disconnect=True)
        await self._ack(interaction, message)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, row=0)
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        state = self.router.get_state(self.guild_id)
        try:
            await interaction.followup.send(
                embed=build_queue_embed(state, 0),
                view=QueueView(self.router, self.guild_id, 0, owner_id=getattr(interaction.user, "id", None)),
                ephemeral=True,
            )
        except discord.NotFound:
            return

