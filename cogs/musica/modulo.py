from __future__ import annotations

import contextlib
import logging
from typing import Optional

import config
import discord
from discord import app_commands
from discord.ext import commands

from .legado.roteador_audio import AudioRouter
from .interface.componentes import QueueView, VoiceStatusSettingsView, build_queue_embed, build_now_playing_embeds
from .reproducao.controle_remoto import enviar_controle_remoto
from .comandos.tocar import FluxoTocar

logger = logging.getLogger(__name__)

def _get_router(bot) -> AudioRouter:
    router = getattr(bot, "audio_router", None)
    if router is None:
        router = AudioRouter(bot)
        setattr(bot, "audio_router", router)
    return router


class Music(FluxoTocar, commands.Cog):
    """Player de música modular integrado ao TTS."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.router = _get_router(bot)

    async def cog_unload(self) -> None:
        with contextlib.suppress(Exception):
            await self.router.close()

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
                await self._reply(ctx, getattr(selection, "message", "") or getattr(self.router, "music_worker_unavailable_message", "Sistema de música indisponível no momento: Nenhum worker online"))
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
        # Todas as mensagens novas da música são silenciosas por padrão para não notificar o servidor.
        kwargs.setdefault("silent", True)
        try:
            return await ctx.reply(content, mention_author=False, **kwargs)
        except Exception:
            return await ctx.send(content, **kwargs)

    def _music_error_message(self, exc: Exception) -> str:
        raw = str(exc or "")
        clean = raw.strip()
        worker_unavailable = getattr(self.router, "music_worker_unavailable_message", "")
        worker_engine_unavailable = getattr(getattr(self.router, "backends", None), "music_worker_engine_error_message", lambda _guild_id=None: "")(None)
        if clean and (clean in {worker_unavailable, worker_engine_unavailable} or clean.startswith("Sistema de música indisponível no momento:")):
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
        if ("trackexception" in lower or "invalid status code" in lower or "stream: 404" in lower or "perdeu o track" in lower or "nenhuma fonte candidata" in lower):
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



    @commands.command(name="play", aliases=["tocar", "music", "musica"])
    @commands.guild_only()
    @commands.cooldown(1, 3.0, commands.BucketType.user)
    async def play(self, ctx: commands.Context, *, query: str = ""):
        """Toca link ou pesquisa música por texto."""
        await self._run_play(ctx, query)

    @commands.command(name="pause", aliases=["pausar", "pa"])
    @commands.guild_only()
    async def pause(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        if await self._send_music_agent_control(ctx, "pause", "`⏸️` Música pausada."):
            return
        ok = await self.router.pause(ctx.guild.id)
        if not ok:
            await self._reply(ctx, "Não há música tocando para pausar.")

    @commands.command(name="resume", aliases=["retomar", "continuar", "r"])
    @commands.guild_only()
    async def resume(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        if await self._send_music_agent_control(ctx, "resume", "`▶️` Música retomada."):
            return
        ok = await self.router.resume(ctx.guild.id)
        if not ok:
            await self._reply(ctx, "Não há música pausada.")

    @commands.command(name="skip", aliases=["s", "pular"])
    @commands.guild_only()
    async def skip(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        if await self._send_music_agent_control(ctx, "skip", "`⏭️` Pulando música."):
            return
        _ok, message = await self.router.request_skip(ctx.guild.id, ctx.author)
        await self._reply(ctx, message)

    @commands.command(name="back", aliases=["b", "previous", "voltar", "anterior"])
    @commands.guild_only()
    async def back(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        ok = await self.router.previous(ctx.guild.id)
        await self._reply(ctx, "`⏮️` Voltando para a música anterior." if ok else "Não há música anterior no histórico.")

    @commands.command(name="stop", aliases=["st", "pararmusica", "musicstop"])
    @commands.guild_only()
    async def stop(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        if await self._send_music_agent_control(ctx, "stop", "`⏹️` Player encerrado e desconectado."):
            return
        _ok, message = await self.router.request_stop(ctx.guild.id, ctx.author, disconnect=True)
        await self._reply(ctx, message)

    @commands.command(name="queue", aliases=["fila", "q"])
    @commands.guild_only()
    async def queue(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        state = self.router.get_state(ctx.guild.id)
        await self._reply(ctx, embed=build_queue_embed(state, 0), view=QueueView(self.router, ctx.guild.id, 0, owner_id=ctx.author.id))

    @commands.command(name="np", aliases=["now", "nowplaying", "tocando"])
    @commands.guild_only()
    async def now_playing(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        state = self.router.get_state(ctx.guild.id)
        if state.current is None and state.queue.empty():
            await self._reply(ctx, "Nada tocando agora.")
            return
        state.last_text_channel_id = ctx.channel.id
        await self.router.update_panel(ctx.guild.id, create=True)

    @commands.command(name="volume", aliases=["v", "vol"])
    @commands.guild_only()
    async def volume(self, ctx: commands.Context, value: Optional[int] = None):
        if not await self._ensure_music_action_voice(ctx):
            return
        state = self.router.get_state(ctx.guild.id)
        if value is None:
            await self._reply(ctx, f"`🔊` Volume atual: `{int(round(state.volume * 100))}%`.")
            return
        if not self.router.is_music_staff(ctx.author):
            await self._reply(ctx, "Apenas staff pode alterar o volume do player.")
            return
        volume = await self.router.set_volume(ctx.guild.id, value)
        await self._reply(ctx, f"`🔊` Volume da música ajustado para `{int(round(volume * 100))}%`.")

    @commands.command(name="shuffle", aliases=["sh", "embaralhar"])
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        _ok, message = await self.router.request_shuffle(ctx.guild.id, ctx.author)
        await self._reply(ctx, message)

    @commands.command(name="loop", aliases=["l", "repeat", "repetir"])
    @commands.guild_only()
    async def loop(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        _ok, message = await self.router.request_loop(ctx.guild.id, ctx.author)
        await self._reply(ctx, message)

    @commands.command(name="remove", aliases=["rm", "remover"])
    @commands.guild_only()
    async def remove(self, ctx: commands.Context, position: Optional[int] = None):
        if not await self._ensure_music_action_voice(ctx):
            return
        if position is None:
            await self._reply(ctx, "Use `_remove <posição>`.")
            return
        removed = await self.router.remove_at(ctx.guild.id, position)
        if removed is None:
            await self._reply(ctx, "Essa posição não existe no queue.")
            return
        await self._reply(ctx, f"`🗑️` Removido do queue: **{removed.short_title}**.")

    @commands.command(name="move", aliases=["mv", "mover"])
    @commands.guild_only()
    async def move(self, ctx: commands.Context, from_pos: Optional[int] = None, to_pos: Optional[int] = None):
        if not await self._ensure_music_action_voice(ctx):
            return
        if from_pos is None or to_pos is None:
            await self._reply(ctx, "Use `_move <posição atual> <nova posição>`.")
            return
        ok = await self.router.move(ctx.guild.id, from_pos, to_pos)
        await self._reply(ctx, "`↪️` Posição atualizada." if ok else "Não consegui mover: confira as posições do queue.")

    @commands.command(name="skipto", aliases=["goto", "jump", "jumpto", "tocarfila"])
    @commands.guild_only()
    async def skipto(self, ctx: commands.Context, position: Optional[int] = None):
        if not await self._ensure_music_action_voice(ctx):
            return
        if position is None:
            await self._reply(ctx, "Use `_skipto <posição>`.")
            return
        ok = await self.router.skip_to(ctx.guild.id, position)
        await self._reply(ctx, "`▶️` Tocando a posição escolhida." if ok else "Não encontrei essa posição no queue.")

    @commands.command(name="readd", aliases=["ra", "readicionar", "historicofila", "historicoqueue"])
    @commands.guild_only()
    async def readd(self, ctx: commands.Context):
        added = await self.router.readd_history(ctx.guild.id)
        await self._reply(ctx, f"`🎶` Readicionei `{added}` música(s) do histórico." if added else "O histórico está vazio.")

    @commands.command(name="history", aliases=["h", "historico", "played"])
    @commands.guild_only()
    async def history(self, ctx: commands.Context):
        history = self.router.history_snapshot(ctx.guild.id)
        if not history:
            await self._reply(ctx, "Histórico vazio.")
            return
        lines = []
        for idx, track in enumerate(reversed(history[-10:]), start=1):
            lines.append(f"`{idx:02d}.` **{discord.utils.escape_markdown(track.short_title)}** • `{track.duration_label}`")
        embed = discord.Embed(title="↩️ Histórico de músicas", description="\n".join(lines), color=discord.Color.blurple())
        embed.set_footer(text="Use _readd para colocar o histórico de volta no queue.")
        await self._reply(ctx, embed=embed)

    @commands.command(name="clearqueue", aliases=["cq", "limparfila", "limparqueue", "clearq"])
    @commands.guild_only()
    async def clearqueue(self, ctx: commands.Context):
        if not await self._ensure_music_action_voice(ctx):
            return
        await self.router.replace_queue(ctx.guild.id, [])
        await self._reply(ctx, "`🧹` Queue limpo.")



    def _format_backend_status(self, health, *, runtime: dict | None = None) -> str:
        icon = "🟢" if getattr(health, "available", False) else ("🟡" if getattr(health, "configured", False) else "🔴")
        mode = getattr(health, "mode", "worker") or "worker"
        message = getattr(health, "message", "") or ""
        lines = [
            f"{icon} **{getattr(health, 'name', 'backend')}**",
            f"• modo: `{mode}`",
        ]
        if message:
            lines.append(f"• detalhe: {discord.utils.escape_markdown(str(message)[:220])}")
        return "\n".join(lines)



    @commands.command(name="voicestatus", aliases=["voice_status", "vstatus", "statusvoz", "canalstatus", "setvoicestatus"])
    @commands.guild_only()
    async def voicestatus(self, ctx: commands.Context, action: str = "", *, value: str = ""):
        """Configura o status automático do canal de voz com Components V2."""
        if not self.router.is_music_staff(ctx.author):
            await self._reply(ctx, "Apenas staff pode configurar o status do canal de voz.")
            return

        action_norm = (action or "").strip().lower()
        if action_norm in {"on", "ativar", "ligar", "enable", "enabled"}:
            await self.router.set_voice_status_enabled(ctx.guild.id, True)
        elif action_norm in {"off", "desativar", "desligar", "disable", "disabled"}:
            await self.router.set_voice_status_enabled(ctx.guild.id, False)
        elif action_norm in {"template", "modelo", "status", "tocando"}:
            if value.strip():
                await self.router.set_voice_status_template(ctx.guild.id, value)
        elif action_norm in {"idle", "parado", "vazio"}:
            idle = value.strip()
            if idle in {"-", "clear", "limpar", "reset", "vazio"}:
                idle = ""
            await self.router.set_voice_status_idle(ctx.guild.id, idle)
        elif action_norm in {"reset", "padrao", "padrão", "default"}:
            await self.router.reset_voice_status_settings(ctx.guild.id)
        elif action_norm and action_norm not in {"painel", "panel", "config", "configurar"}:
            await self._reply(ctx, "Use `_voicestatus` para abrir o painel, ou `_voicestatus template <modelo>` para alterar direto.")
            return

        await self._reply(ctx, view=VoiceStatusSettingsView(self.router, ctx.guild.id, owner_id=ctx.author.id))


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if getattr(getattr(message, "author", None), "bot", False) or message.guild is None:
            return
        antibot_guard = getattr(self.bot, "antibot_should_block_message", None)
        if callable(antibot_guard) and bool(antibot_guard(message)):
            return

        raw = str(getattr(message, "content", "") or "").strip()
        if not raw:
            return

        prefixes = []
        for value in (getattr(config, "BOT_PREFIX", "_"), getattr(config, "PREFIX", "_"), "_"):
            value = str(value or "_").strip() or "_"
            if value not in prefixes:
                prefixes.append(value)

        lowered = raw.lower()
        for prefix in prefixes:
            alias = f"{prefix}p"
            # `_p` sozinho é reservado para o painel do TTS. Música só assume `_p <busca/link>`.
            if lowered.startswith(alias.lower() + " "):
                query = raw[len(alias):].strip()
                if not query:
                    return
                ctx = await self.bot.get_context(message)
                await self._run_play(ctx, query)
                return


    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = getattr(member, "guild", None)
        bot_user = getattr(self.bot, "user", None)
        if guild is None or bot_user is None or int(getattr(member, "id", 0) or 0) != int(getattr(bot_user, "id", 0) or 0):
            return
        before_channel = getattr(before, "channel", None)
        after_channel = getattr(after, "channel", None)
        if before_channel is not None and after_channel is None:
            await self.router.handle_bot_voice_disconnect(guild, before_channel, after_channel)
            return
        if before_channel is not None and after_channel is not None and getattr(before_channel, "id", None) != getattr(after_channel, "id", None):
            await self.router.handle_bot_voice_move(guild, before_channel, after_channel)

    @play.error
    async def play_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandOnCooldown):
            await self._reply(ctx, f"Espere `{error.retry_after:.1f}s` antes de usar `_play` de novo.")
            return
        raise error


async def setup(bot: commands.Bot):
    _get_router(bot)
    await bot.add_cog(Music(bot))
