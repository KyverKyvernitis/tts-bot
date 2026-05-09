from __future__ import annotations

import contextlib
import logging
from typing import Optional

import config
import discord
from discord.ext import commands

from music_system import AudioRouter
from music_system.errors import MusicExtractionError
from music_system.ui import SearchResultView, QueueView, VoiceStatusSettingsView, build_queue_embed, build_now_playing_embeds
from music_system.musicnode_ui import MusicNodePanelView

logger = logging.getLogger(__name__)


def _get_router(bot) -> AudioRouter:
    router = getattr(bot, "audio_router", None)
    if router is None:
        router = AudioRouter(bot)
        setattr(bot, "audio_router", router)
    return router


class Music(commands.Cog):
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

    async def _reply(self, ctx: commands.Context, content: str | None = None, **kwargs):
        # Todas as mensagens novas da música são silenciosas por padrão para não notificar o servidor.
        kwargs.setdefault("silent", True)
        try:
            return await ctx.reply(content, mention_author=False, **kwargs)
        except Exception:
            return await ctx.send(content, **kwargs)

    def _music_error_message(self, exc: Exception) -> str:
        raw = str(exc)
        lower = raw.lower()
        if "sign in to confirm" in lower or "not a bot" in lower:
            return "`⚠️` O YouTube bloqueou a extração pedindo login/cookies. Confira `cookies.txt`, Deno e `yt-dlp[default]`."
        if "signature" in lower or "n challenge" in lower or "only images are available" in lower:
            return "`⚠️` O YouTube recusou o stream de áudio. Atualize `yt-dlp[default]` e confirme se o Deno está instalado."
        if "drm" in lower:
            return "`⚠️` Essa fonte usa DRM. Tente outro link ou pesquise pelo nome da música."
        return f"`⚠️` {raw}"

    async def _run_play(self, ctx: commands.Context, query: str) -> None:
        """Implementação compartilhada de `_play` e da alias roteada `_p <música>`."""
        query = (query or "").strip()
        if not query:
            await self._reply(ctx, "Use `_play <link ou pesquisa>`.")
            return

        voice_channel = await self._voice_channel_from_ctx(ctx)
        if voice_channel is None:
            await self._reply(ctx, "Entre em um canal de voz primeiro.")
            return

        # Shadow mode Lavalink: consulta o node em paralelo, mas mantém o áudio real
        # no player local atual. Se o node público falhar/atrasar, o usuário não é afetado.
        self.router.schedule_lavalink_shadow_search(
            ctx.guild.id,
            query,
            requester_id=ctx.author.id,
            requester_name=getattr(ctx.author, "display_name", str(ctx.author)),
            reason="play_command",
        )

        # Evita derrubar o comando quando o Discord aplica rate limit no endpoint de typing.
        # A extração pode demorar, mas o indicador de "digitando..." não é essencial.
        try:
            batch = await self.router.extractor.extract(
                query,
                requester_id=ctx.author.id,
                requester_name=getattr(ctx.author, "display_name", str(ctx.author)),
            )
        except MusicExtractionError as exc:
            await self._reply(ctx, self._music_error_message(exc))
            return
        except Exception as exc:
            logger.exception("[music] erro inesperado na extração")
            await self._reply(ctx, self._music_error_message(exc))
            return

        if not batch.tracks:
            await self._reply(ctx, "`📭` Não encontrei nada tocável.")
            return

        if not self.router.extractor.looks_like_url(query) and len(batch.tracks) > 1:
            embed = discord.Embed(
                title="🔎 Escolha a música",
                description="Selecione um dos resultados abaixo para adicionar ao queue.",
                color=discord.Color.blurple(),
            )
            for idx, track in enumerate(batch.tracks[:5], start=1):
                embed.add_field(
                    name=f"{idx}. {track.short_title}",
                    value=f"{track.uploader or track.source or 'resultado'} • `{track.duration_label}`",
                    inline=False,
                )
            await self._reply(
                ctx,
                embed=embed,
                view=SearchResultView(self.router, ctx.guild.id, voice_channel.id, ctx.channel.id, batch.tracks[:5], ctx.author.id),
            )
            return

        added, dropped = await self.router.enqueue(ctx.guild, voice_channel, ctx.channel, batch.tracks)
        if added <= 0:
            await self._reply(ctx, "`⚠️` Não adicionei nada: o queue está cheio ou essa música já está no queue/tocando.")
            return

        if batch.is_playlist:
            desc = f"`📑` **Playlist adicionada:** `{added}` música(s)"
            if batch.playlist_title:
                desc += f" de **{batch.playlist_title}**"
            if batch.truncated:
                desc += f"\n`⚠️` Playlist limitada aos primeiros `{getattr(config, 'MUSIC_MAX_PLAYLIST_ITEMS', 100)}` itens para não pesar o bot."
            if dropped:
                desc += f"\n`⚠️` `{dropped}` item(ns) não entraram por duplicata ou queue cheio."
            await self._reply(ctx, desc)
        else:
            track = batch.tracks[0]
            state = self.router.get_state(ctx.guild.id)
            position = state.queue_size() + (1 if state.current else 0)
            await self._reply(ctx, f"`🎶` **Adicionada ao queue:** {track.short_title} • `{track.duration_label}` • posição `{max(1, position)}`")

    @commands.command(name="play", aliases=["tocar", "music", "musica"])
    @commands.guild_only()
    @commands.cooldown(1, 3.0, commands.BucketType.user)
    async def play(self, ctx: commands.Context, *, query: str = ""):
        """Toca link ou pesquisa música por texto."""
        await self._run_play(ctx, query)

    @commands.command(name="pause", aliases=["pausar", "pa"])
    @commands.guild_only()
    async def pause(self, ctx: commands.Context):
        ok = await self.router.pause(ctx.guild.id)
        if not ok:
            await self._reply(ctx, "Não há música tocando para pausar.")

    @commands.command(name="resume", aliases=["retomar", "continuar", "r"])
    @commands.guild_only()
    async def resume(self, ctx: commands.Context):
        ok = await self.router.resume(ctx.guild.id)
        if not ok:
            await self._reply(ctx, "Não há música pausada.")

    @commands.command(name="skip", aliases=["s", "pular"])
    @commands.guild_only()
    async def skip(self, ctx: commands.Context):
        _ok, message = await self.router.request_skip(ctx.guild.id, ctx.author)
        await self._reply(ctx, message)

    @commands.command(name="back", aliases=["b", "previous", "voltar", "anterior"])
    @commands.guild_only()
    async def back(self, ctx: commands.Context):
        ok = await self.router.previous(ctx.guild.id)
        await self._reply(ctx, "`⏮️` Voltando para a música anterior." if ok else "Não há música anterior no histórico.")

    @commands.command(name="stop", aliases=["st", "pararmusica", "musicstop"])
    @commands.guild_only()
    async def stop(self, ctx: commands.Context):
        _ok, message = await self.router.request_stop(ctx.guild.id, ctx.author, disconnect=True)
        await self._reply(ctx, message)

    @commands.command(name="queue", aliases=["fila", "q"])
    @commands.guild_only()
    async def queue(self, ctx: commands.Context):
        state = self.router.get_state(ctx.guild.id)
        await self._reply(ctx, embed=build_queue_embed(state, 0), view=QueueView(self.router, ctx.guild.id, 0, owner_id=ctx.author.id))

    @commands.command(name="np", aliases=["now", "nowplaying", "tocando"])
    @commands.guild_only()
    async def now_playing(self, ctx: commands.Context):
        state = self.router.get_state(ctx.guild.id)
        if state.current is None and state.queue.empty():
            await self._reply(ctx, "Nada tocando agora.")
            return
        state.last_text_channel_id = ctx.channel.id
        await self.router.update_panel(ctx.guild.id, create=True)

    @commands.command(name="volume", aliases=["v", "vol"])
    @commands.guild_only()
    async def volume(self, ctx: commands.Context, value: Optional[int] = None):
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
        _ok, message = await self.router.request_shuffle(ctx.guild.id, ctx.author)
        await self._reply(ctx, message)

    @commands.command(name="loop", aliases=["l", "repeat", "repetir"])
    @commands.guild_only()
    async def loop(self, ctx: commands.Context):
        _ok, message = await self.router.request_loop(ctx.guild.id, ctx.author)
        await self._reply(ctx, message)

    @commands.command(name="remove", aliases=["rm", "remover"])
    @commands.guild_only()
    async def remove(self, ctx: commands.Context, position: Optional[int] = None):
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
        if from_pos is None or to_pos is None:
            await self._reply(ctx, "Use `_move <posição atual> <nova posição>`.")
            return
        ok = await self.router.move(ctx.guild.id, from_pos, to_pos)
        await self._reply(ctx, "`↪️` Posição atualizada." if ok else "Não consegui mover: confira as posições do queue.")

    @commands.command(name="skipto", aliases=["goto", "jump", "jumpto", "tocarfila"])
    @commands.guild_only()
    async def skipto(self, ctx: commands.Context, position: Optional[int] = None):
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
        await self.router.replace_queue(ctx.guild.id, [])
        await self._reply(ctx, "`🧹` Queue limpo.")



    async def _can_use_musicnode(self, ctx: commands.Context) -> bool:
        with contextlib.suppress(Exception):
            return bool(await self.bot.is_owner(ctx.author))
        return False

    def _format_backend_status(self, health, *, runtime: dict | None = None) -> str:
        icon = "🟢" if getattr(health, "available", False) else ("🟡" if getattr(health, "configured", False) else "🔴")
        enabled = "sim" if getattr(health, "enabled", False) else "não"
        configured = "sim" if getattr(health, "configured", False) else "não"
        mode = getattr(health, "mode", "off") or "off"
        lines = [
            f"{icon} **{getattr(health, 'name', 'backend')}**",
            f"• ativado: `{enabled}` • configurado: `{configured}` • modo: `{mode}`",
        ]
        version = getattr(health, "version", "") or ""
        latency = getattr(health, "latency_ms", None)
        if version:
            lines.append(f"• versão: `{discord.utils.escape_markdown(str(version))}`")
        if latency is not None:
            lines.append(f"• latência: `{latency} ms`")
        players = getattr(health, "players", None)
        playing = getattr(health, "playing_players", None)
        if players is not None:
            lines.append(f"• players: `{players}` • tocando: `{playing if playing is not None else '?'}`")
        extra = getattr(health, "extra", {}) or {}
        if extra.get("host"):
            lines.append(f"• host: `{discord.utils.escape_markdown(str(extra.get('host'))[:80])}`")
        if "wavelink_installed" in extra:
            lines.append(f"• wavelink instalado: `{'sim' if extra.get('wavelink_installed') else 'não'}`")
        message = getattr(health, "message", "") or ""
        if message:
            lines.append(f"• detalhe: {discord.utils.escape_markdown(str(message)[:220])}")
        return "\n".join(lines)

    def _format_lavalink_test(self, result) -> str:
        icon = "🟢" if getattr(result, "ok", False) else "🔴"
        lines = [
            f"{icon} **Teste Lavalink**",
            f"• query: `{discord.utils.escape_markdown(str(getattr(result, 'query', '') or '')[:160])}`",
            f"• resultado: `{'OK' if getattr(result, 'ok', False) else 'falhou'}`",
        ]
        latency = getattr(result, "latency_ms", None)
        if latency is not None:
            lines.append(f"• latência: `{latency} ms`")
        load_type = getattr(result, "load_type", "") or ""
        if load_type:
            lines.append(f"• loadType: `{discord.utils.escape_markdown(str(load_type))}`")
        lines.append(f"• tracks encontradas: `{int(getattr(result, 'tracks_found', 0) or 0)}`")
        playlist = getattr(result, "playlist_name", "") or ""
        if playlist:
            lines.append(f"• playlist: `{discord.utils.escape_markdown(str(playlist)[:120])}`")
        title = getattr(result, "first_title", "") or ""
        if title:
            author = getattr(result, "first_author", "") or ""
            source = getattr(result, "first_source", "") or ""
            suffix = []
            if author:
                suffix.append(str(author)[:80])
            if source:
                suffix.append(str(source)[:40])
            tail = f" • {' • '.join(discord.utils.escape_markdown(x) for x in suffix)}" if suffix else ""
            lines.append(f"• primeira: **{discord.utils.escape_markdown(str(title)[:120])}**{tail}")
        message = getattr(result, "message", "") or ""
        if message:
            lines.append(f"• detalhe: {discord.utils.escape_markdown(str(message)[:240])}")
        return "\n".join(lines)

    @commands.command(name="musicnode")
    @commands.guild_only()
    async def musicnode(self, ctx: commands.Context, *, _ignored: str = ""):
        """Abre a central técnica do Lavalink com painel, botões e modals."""
        if not await self._can_use_musicnode(ctx):
            await self._reply(ctx, "Esse painel técnico do Lavalink é exclusivo do dono do bot.")
            return

        view = MusicNodePanelView(self.router, self.bot, owner_id=ctx.author.id, guild_id=ctx.guild.id)
        await view.prepare()
        message = await self._reply(ctx, view=view, allowed_mentions=discord.AllowedMentions.none())
        view.message = message


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
