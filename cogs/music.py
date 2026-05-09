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

logger = logging.getLogger(__name__)


def _get_router(bot) -> AudioRouter:
    router = getattr(bot, "audio_router", None)
    if router is None:
        router = AudioRouter(bot)
        setattr(bot, "audio_router", router)
    return router


class Music(commands.Cog):
    """Player de música modular com TTS ducking obrigatório."""

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

    @commands.command(name="duck", aliases=["dv", "ttsduck", "ducking"])
    @commands.guild_only()
    async def duck(self, ctx: commands.Context, value: str = ""):
        state = self.router.get_state(ctx.guild.id)
        raw = (value or "").strip().lower()
        if not raw:
            await self._reply(
                ctx,
                f"`🎙️` Volume da música durante TTS: `{int(round(state.duck_volume * 100))}%`.\n"
                "Use `_duck <5-100>` para ajustar esse volume.",
            )
            return
        if raw in {"on", "true", "sim", "ativar", "ativo", "off", "false", "nao", "não", "desativar", "desligar"}:
            await self._reply(ctx, "`🎙️` Esse comando só ajusta o volume da música durante TTS. Use `_duck <5-100>`.")
            return
        try:
            percent = int(raw.replace("%", ""))
        except Exception:
            await self._reply(ctx, "Use `_duck <5-100>` para ajustar o volume da música durante TTS.")
            return
        if not self.router.is_music_staff(ctx.author):
            await self._reply(ctx, "Apenas staff pode alterar o volume durante TTS.")
            return
        volume = await self.router.set_duck_volume(ctx.guild.id, percent)
        await self._reply(ctx, f"`🎙️` Volume da música durante TTS ajustado para `{int(round(volume * 100))}%`.")

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
            if await self.bot.is_owner(ctx.author):
                return True
        return self.router.is_music_staff(ctx.author)

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

    @commands.command(name="musicnode", aliases=["lavalink", "llnode", "node"])
    @commands.guild_only()
    async def musicnode(self, ctx: commands.Context, action: str = "status", *, query: str = ""):
        """Diagnóstico seguro do suporte Lavalink sem alterar o player real."""
        if not await self._can_use_musicnode(ctx):
            await self._reply(ctx, "Apenas staff pode consultar o diagnóstico do node de música.")
            return

        action_norm = (action or "status").strip().lower()
        if action_norm in {"status", "state", "info", "s"}:
            statuses = await self.router.backend_status()
            runtime = self.router.backend_runtime_summary()
            lines = [
                "`🧪` **Diagnóstico do backend de música**",
                f"Backend configurado: `{runtime.get('configured_backend', 'local')}`",
                f"Backend real deste patch: `{runtime.get('active_backend', 'local')}`",
                "",
                self._format_backend_status(statuses.get("local"), runtime=runtime),
                "",
                self._format_backend_status(statuses.get("lavalink"), runtime=runtime),
                "",
                "Obs.: neste patch o Lavalink é só diagnóstico/estrutura; o player real continua local.",
            ]
            await self._reply(ctx, "\n".join(lines))
            return

        if action_norm in {"test", "teste", "load", "buscar", "search"}:
            query = (query or "").strip()
            if not query:
                await self._reply(ctx, "Use `_musicnode test <busca ou link>`.")
                return
            result = await self.router.test_lavalink_backend(
                query,
                requester_id=ctx.author.id,
                requester_name=getattr(ctx.author, "display_name", str(ctx.author)),
            )
            await self._reply(ctx, self._format_lavalink_test(result))
            return

        await self._reply(ctx, "Use `_musicnode status` ou `_musicnode test <busca/link>`.")

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
