import asyncio
from pathlib import Path

import discord
from discord.ext import commands


class TempSfxLoop(commands.Cog):
    """Comando temporário para tocar um SFX em loop na call atual do bot."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._loop_tasks: dict[int, asyncio.Task] = {}
        self._stop_flags: dict[int, asyncio.Event] = {}

    def cog_unload(self):
        for event in self._stop_flags.values():
            event.set()
        for task in self._loop_tasks.values():
            task.cancel()
        self._loop_tasks.clear()
        self._stop_flags.clear()

    def _is_allowed(self, member: discord.Member) -> bool:
        perms = getattr(member, "guild_permissions", None)
        if perms is None:
            return False
        return bool(getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False) or getattr(perms, "manage_channels", False))

    def _sfx_path(self) -> Path:
        return Path(__file__).resolve().parents[1] / "assets" / "sfx" / "gemi-2-remix.mp3"

    def _get_voice_client(self, guild: discord.Guild | None) -> discord.VoiceClient | None:
        if guild is None:
            return None
        for vc in getattr(self.bot, "voice_clients", []) or []:
            try:
                if getattr(getattr(vc, "guild", None), "id", None) == guild.id:
                    return vc
            except Exception:
                continue
        return getattr(guild, "voice_client", None)

    async def _play_once_and_wait(self, vc: discord.VoiceClient, sfx_path: Path, stop_event: asyncio.Event) -> bool:
        if vc is None or not vc.is_connected() or stop_event.is_set():
            return False

        done = asyncio.Event()

        def _after(_: Exception | None):
            try:
                self.bot.loop.call_soon_threadsafe(done.set)
            except Exception:
                pass

        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
        except Exception:
            pass

        try:
            source = discord.FFmpegPCMAudio(str(sfx_path))
            vc.play(source, after=_after)
        except Exception:
            return False

        while not stop_event.is_set():
            if done.is_set():
                break
            try:
                if not vc.is_connected():
                    return False
            except Exception:
                return False
            await asyncio.sleep(0.2)

        if stop_event.is_set():
            try:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
            except Exception:
                pass
        return True

    async def _loop_runner(self, guild: discord.Guild, text_channel: discord.abc.Messageable, sfx_path: Path, stop_event: asyncio.Event):
        guild_id = guild.id
        try:
            while not stop_event.is_set():
                vc = self._get_voice_client(guild)
                if vc is None or not vc.is_connected() or getattr(vc, "channel", None) is None:
                    try:
                        await text_channel.send("⚠️ Parei o loop porque o bot não está mais conectado em nenhuma call.")
                    except Exception:
                        pass
                    break

                played = await self._play_once_and_wait(vc, sfx_path, stop_event)
                if not played or stop_event.is_set():
                    break

                for _ in range(10):
                    if stop_event.is_set():
                        break
                    vc = self._get_voice_client(guild)
                    if vc is None or not vc.is_connected():
                        stop_event.set()
                        break
                    await asyncio.sleep(0.1)
        finally:
            self._loop_tasks.pop(guild_id, None)
            self._stop_flags.pop(guild_id, None)

    @commands.command(name="gemi", aliases=["gemiloop", "loopsfx"])
    @commands.guild_only()
    async def gemi(self, ctx: commands.Context):
        if not isinstance(ctx.author, discord.Member) or not self._is_allowed(ctx.author):
            await ctx.reply("Você precisa ter permissão de staff para usar esse comando temporário.", mention_author=False)
            return

        guild = ctx.guild
        if guild is None:
            await ctx.reply("Esse comando só funciona em servidor.", mention_author=False)
            return

        sfx_path = self._sfx_path()
        if not sfx_path.exists():
            await ctx.reply("Não encontrei o áudio temporário configurado para esse comando.", mention_author=False)
            return

        vc = self._get_voice_client(guild)
        if vc is None or not vc.is_connected() or getattr(vc, "channel", None) is None:
            await ctx.reply("O bot não está em nenhuma call neste servidor no momento.", mention_author=False)
            return

        existing_task = self._loop_tasks.get(guild.id)
        if existing_task is not None and not existing_task.done():
            stop_event = self._stop_flags.get(guild.id)
            if stop_event is not None:
                stop_event.set()
            try:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
            except Exception:
                pass
            await ctx.reply(
                f"⏹️ Loop temporário parado em {getattr(vc.channel, 'mention', 'sua call atual')}.",
                mention_author=False,
            )
            return

        stop_event = asyncio.Event()
        task = asyncio.create_task(self._loop_runner(guild, ctx.channel, sfx_path, stop_event))
        self._stop_flags[guild.id] = stop_event
        self._loop_tasks[guild.id] = task

        await ctx.reply(
            f"🔁 Loop temporário iniciado em {getattr(vc.channel, 'mention', 'a call atual do bot')}.",
            mention_author=False,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(TempSfxLoop(bot))
