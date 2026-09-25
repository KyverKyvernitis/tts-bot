from __future__ import annotations

import contextlib
from typing import Optional

from cogs.musica import configuracao as config
import discord
from discord.ext import commands

from .legado.roteador_audio import AudioRouter
from .comandos import BaseComandosMusica, FluxoConfiguracoes, FluxoControle, FluxoFila, FluxoTocar


def _get_router(bot) -> AudioRouter:
    router = getattr(bot, "audio_router", None)
    if router is None:
        router = AudioRouter(bot)
        setattr(bot, "audio_router", router)
    return router


class Music(FluxoTocar, FluxoControle, FluxoFila, FluxoConfiguracoes, BaseComandosMusica, commands.Cog):
    """Player de música modular integrado ao TTS."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.router = _get_router(bot)

    async def cog_unload(self) -> None:
        with contextlib.suppress(Exception):
            await self.router.close()









    @commands.command(name="play", aliases=["tocar", "music", "musica"])
    @commands.guild_only()
    @commands.cooldown(1, 3.0, commands.BucketType.user)
    async def play(self, ctx: commands.Context, *, query: str = ""):
        """Toca link ou pesquisa música por texto."""
        await self._run_play(ctx, query)

    @commands.command(name="pause", aliases=["pausar", "pa"])
    @commands.guild_only()
    async def pause(self, ctx: commands.Context):
        await self._run_pause(ctx)

    @commands.command(name="resume", aliases=["retomar", "continuar", "r"])
    @commands.guild_only()
    async def resume(self, ctx: commands.Context):
        await self._run_resume(ctx)

    @commands.command(name="skip", aliases=["s", "pular"])
    @commands.guild_only()
    async def skip(self, ctx: commands.Context):
        await self._run_skip(ctx)

    @commands.command(name="back", aliases=["b", "previous", "voltar", "anterior"])
    @commands.guild_only()
    async def back(self, ctx: commands.Context):
        await self._run_back(ctx)

    @commands.command(name="stop", aliases=["st", "pararmusica", "musicstop"])
    @commands.guild_only()
    async def stop(self, ctx: commands.Context):
        await self._run_stop(ctx)

    @commands.command(name="queue", aliases=["fila", "q"])
    @commands.guild_only()
    async def queue(self, ctx: commands.Context):
        await self._run_queue(ctx)

    @commands.command(name="np", aliases=["now", "nowplaying", "tocando"])
    @commands.guild_only()
    async def now_playing(self, ctx: commands.Context):
        await self._run_now_playing(ctx)

    @commands.command(name="volume", aliases=["v", "vol"])
    @commands.guild_only()
    async def volume(self, ctx: commands.Context, value: Optional[int] = None):
        await self._run_volume(ctx, value)


    @commands.command(name="nightcore")
    @commands.guild_only()
    async def nightcore(self, ctx: commands.Context, level: str = ""):
        await self._run_audio_effect(ctx, "nightcore", level)

    @commands.command(name="bassboost")
    @commands.guild_only()
    async def bassboost(self, ctx: commands.Context, level: str = ""):
        await self._run_audio_effect(ctx, "bassboost", level)

    @commands.command(name="reverb")
    @commands.guild_only()
    async def reverb(self, ctx: commands.Context, level: str = ""):
        await self._run_audio_effect(ctx, "slowed_reverb", level)

    @commands.command(name="shuffle", aliases=["sh", "embaralhar"])
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context):
        await self._run_shuffle(ctx)

    @commands.command(name="loop", aliases=["l", "repeat", "repetir"])
    @commands.guild_only()
    async def loop(self, ctx: commands.Context):
        await self._run_loop(ctx)

    @commands.command(name="remove", aliases=["rm", "remover"])
    @commands.guild_only()
    async def remove(self, ctx: commands.Context, position: Optional[int] = None):
        await self._run_remove(ctx, position)

    @commands.command(name="move", aliases=["mv", "mover"])
    @commands.guild_only()
    async def move(self, ctx: commands.Context, from_pos: Optional[int] = None, to_pos: Optional[int] = None):
        await self._run_move(ctx, from_pos, to_pos)

    @commands.command(name="skipto", aliases=["goto", "jump", "jumpto", "tocarfila"])
    @commands.guild_only()
    async def skipto(self, ctx: commands.Context, position: Optional[int] = None):
        await self._run_skipto(ctx, position)

    @commands.command(name="readd", aliases=["ra", "readicionar", "historicofila", "historicoqueue"])
    @commands.guild_only()
    async def readd(self, ctx: commands.Context):
        await self._run_readd(ctx)

    @commands.command(name="history", aliases=["h", "historico", "played"])
    @commands.guild_only()
    async def history(self, ctx: commands.Context):
        await self._run_history(ctx)

    @commands.command(name="clearqueue", aliases=["cq", "limparfila", "limparqueue", "clearq"])
    @commands.guild_only()
    async def clearqueue(self, ctx: commands.Context):
        await self._run_clearqueue(ctx)






    @commands.command(name="voicestatus", aliases=["voice_status", "vstatus", "statusvoz", "canalstatus", "setvoicestatus"])
    @commands.guild_only()
    async def voicestatus(self, ctx: commands.Context, action: str = "", *, value: str = ""):
        await self._run_voicestatus(ctx, action, value=value)


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
    async def on_music_voice_channel_status_update_raw(self, payload: dict):
        try:
            guild_id = int(payload.get("guild_id") or 0)
            channel_id = int(payload.get("id") or payload.get("channel_id") or 0)
        except Exception:
            return
        if guild_id <= 0 or channel_id <= 0:
            return
        await self.router.handle_voice_channel_status_gateway_update(
            guild_id,
            channel_id,
            payload.get("status"),
        )

    @commands.Cog.listener()
    async def on_voice_channel_status_update(self, channel, before, after):
        """Compatibilidade futura caso discord.py passe a expor o evento."""
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        await self.router.handle_voice_channel_status_gateway_update(
            int(guild.id),
            int(getattr(channel, "id", 0) or 0),
            after,
        )

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
