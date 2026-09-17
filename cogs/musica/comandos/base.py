from __future__ import annotations

import contextlib
import logging

import discord
from discord.ext import commands

from ..reproducao.controle_remoto import enviar_controle_remoto

logger = logging.getLogger(__name__)


class BaseComandosMusica:
    """Infraestrutura compartilhada pelos comandos do domínio de música."""

    async def _voice_channel_from_ctx(self, ctx: commands.Context) -> discord.VoiceChannel | discord.StageChannel | None:
        voice = getattr(getattr(ctx.author, "voice", None), "channel", None)
        if isinstance(voice, (discord.VoiceChannel, discord.StageChannel)):
            return voice
        return None

    async def _ensure_music_action_voice(self, ctx: commands.Context) -> bool:
        user_channel = await self._voice_channel_from_ctx(ctx)
        if user_channel is None:
            await self._reply(ctx, "Entre em um canal de voz primeiro.")
            return False
        if getattr(self.router, "music_worker_only_enabled", lambda: False)():
            selection = await self.router.ensure_music_worker_available()
            if not getattr(selection, "available", False):
                logger.info("[music/worker] ação bloqueada: %s", getattr(selection, "reason", "worker indisponível"))
                await self._reply(
                    ctx,
                    getattr(selection, "message", "")
                    or getattr(
                        self.router,
                        "music_worker_unavailable_message",
                        "Sistema de música indisponível no momento: Nenhum worker online",
                    ),
                )
                return False
        state = self.router.get_state(ctx.guild.id)
        vc = getattr(ctx.guild, "voice_client", None)
        bot_channel = getattr(vc, "channel", None) if vc is not None else None
        if bot_channel is None:
            player = getattr(state, "current_lavalink_player", None)
            bot_channel = getattr(player, "channel", None) if player is not None else None
        if bot_channel is not None and getattr(bot_channel, "id", None) != getattr(user_channel, "id", None):
            await self._reply(ctx, "Entre no mesmo canal de voz do bot para usar isso.")
            return False
        return True

    async def _send_music_agent_control(self, ctx: commands.Context, action: str, success_message: str) -> bool:
        if not self._music_agent_default_enabled():
            return False
        if action in {"stop", "skip", "shuffle"}:
            with contextlib.suppress(Exception):
                self.router.cancel_pending_music_operations(ctx.guild.id, reason=f"agent_{action}")
        try:
            voice = getattr(getattr(ctx.author, "voice", None), "channel", None)
            await enviar_controle_remoto(
                self.router,
                action,
                guild_id=ctx.guild.id,
                requester_id=ctx.author.id,
                requester_name=getattr(ctx.author, "display_name", str(ctx.author)),
                voice_channel_id=getattr(voice, "id", 0),
                text_channel_id=getattr(ctx.channel, "id", 0),
                create_panel=True,
            )
        except Exception as exc:
            await self._reply(ctx, self._music_error_message(exc))
            return True
        await self._reply(ctx, success_message)
        return True

    async def _reply(self, ctx: commands.Context, content: str | None = None, **kwargs):
        kwargs.setdefault("silent", True)
        try:
            return await ctx.reply(content, mention_author=False, **kwargs)
        except Exception:
            return await ctx.send(content, **kwargs)

    def _music_error_message(self, exc: Exception) -> str:
        raw = str(exc or "")
        clean = raw.strip()
        worker_unavailable = getattr(self.router, "music_worker_unavailable_message", "")
        worker_engine_unavailable = getattr(
            getattr(self.router, "backends", None),
            "music_worker_engine_error_message",
            lambda _guild_id=None: "",
        )(None)
        if clean and (
            clean in {worker_unavailable, worker_engine_unavailable}
            or clean.startswith("Sistema de música indisponível no momento:")
        ):
            return clean
        lower = raw.lower()
        if any(
            needle in lower
            for needle in (
                "invalid status code for soundcloud stream",
                "failed to load tracks",
                "trackexception",
                "something broke when playing the track",
                "não conseguiu tocar nenhuma fonte candidata",
            )
        ):
            return "`⚠️` Não consegui tocar essa fonte agora. Tente outro link ou outra pesquisa."
        if getattr(self.router, "music_worker_only_enabled", lambda: False)() and (
            "lavalink indisponível" in lower
            or "lavalink em cooldown" in lower
            or "cannot connect to" in lower
            or "node musical" in lower
            or ("wavelink" in lower and "trackexception" not in lower)
        ):
            return worker_engine_unavailable or "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta"
        if "sign in to confirm" in lower or "not a bot" in lower:
            return "`⚠️` O YouTube bloqueou a extração pedindo login/cookies. Confira `cookies.txt`, Deno e `yt-dlp[default]`."
        if "signature" in lower or "n challenge" in lower or "only images are available" in lower:
            return "`⚠️` O YouTube recusou o stream de áudio. Atualize `yt-dlp[default]` e confirme se o Deno está instalado."
        if "drm" in lower:
            return "`⚠️` Essa fonte usa DRM. Tente outro link ou pesquise pelo nome da música."
        if (
            "trackexception" in lower
            or "invalid status code" in lower
            or "stream: 404" in lower
            or "perdeu o track" in lower
            or "nenhuma fonte candidata" in lower
        ):
            if "soundcloud" in lower or "scsearch" in lower or "404" in lower:
                return "`⚠️` A fonte encontrada não entregou um stream tocável. Tente novamente ou use um link direto."
            return "`⚠️` O worker encontrou a música, mas a fonte de áudio falhou ao iniciar. Tente outra busca ou link."
        if "failed to load tracks" in lower or "lavalinkloadexception" in lower or "something went wrong while looking up" in lower:
            if "spotify" in lower or "spsearch" in lower or "response code from channel info is 403" in lower:
                return "`⚠️` Não consegui resolver esse link do Spotify no node. Vou evitar erro cru: confira a API do Spotify no `.env` e use modo `auto` para cair no player local quando o LavaSrc falhar."
            if "soundcloud" in lower or "scsearch" in lower or "invalid status code" in lower or "404" in lower:
                return "`⚠️` O SoundCloud respondeu metadados, mas recusou o stream no node. Tente novamente; em modo `auto`, o bot tenta uma fonte local equivalente."
            return "`⚠️` O Lavalink não encontrou uma faixa tocável para essa busca. Tente pesquisar com nome e artista."
        if "timed out" in lower or "timeout" in lower:
            return "`⚠️` A fonte demorou demais para responder. Tente novamente em alguns segundos."
        if not raw:
            return "`⚠️` Não consegui iniciar essa música."
        return f"`⚠️` {raw[:220]}"
