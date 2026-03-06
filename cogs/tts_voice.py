import os
import asyncio
import tempfile
import discord
from discord.ext import commands
from gtts import gTTS

from config import TTS_ENABLED, BLOCK_VOICE_BOT_ID

class TtsVoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.locks = {}  # lock por guild

    def _lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self.locks:
            self.locks[guild_id] = asyncio.Lock()
        return self.locks[guild_id]

    async def speak(self, message: discord.Message, text: str):
        if not message.guild:
            return

        vs = getattr(message.author, "voice", None)
        if not vs or not vs.channel:
            await message.reply("⚠️ Você precisa estar em um canal de voz para eu falar.")
            return

        channel: discord.VoiceChannel = vs.channel

        # bloqueio se existir outro bot específico
        if BLOCK_VOICE_BOT_ID and any(m.bot and m.id == BLOCK_VOICE_BOT_ID for m in channel.members):
            await message.reply("❌ Já existe um bot de voz nesta call")
            return

        # permissões
        me = message.guild.me or message.guild.get_member(self.bot.user.id)
        perms = channel.permissions_for(me)
        if not perms.connect:
            await message.reply("❌ Eu não tenho permissão **Conectar** nesse canal de voz.")
            return
        if not perms.speak:
            await message.reply("❌ Eu não tenho permissão **Falar** nesse canal de voz.")
            return

        vc = message.guild.voice_client
        try:
            if vc is None:
                vc = await channel.connect()
            elif vc.channel and vc.channel.id != channel.id:
                await vc.move_to(channel)
        except Exception as e:
            await message.reply(f"❌ Não consegui entrar na call. Erro: `{type(e).__name__}` — `{e}`")
            return

        async with self._lock(message.guild.id):
            if vc.is_playing():
                vc.stop()

            text = (text or "").strip()
            if not text:
                await message.reply("⚠️ Escreva algo depois da vírgula. Ex: `,olá`")
                return
            if len(text) > 250:
                text = text[:250]

            tmp = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
                    tmp = fp.name

                try:
                    gTTS(text=text, lang="pt", tld="com.br").save(tmp)
                except Exception:
                    await message.reply("❌ Falhei ao gerar a voz (gTTS).")
                    return

                vc.play(discord.FFmpegPCMAudio(tmp))
                while vc.is_playing():
                    await asyncio.sleep(0.2)
            finally:
                if tmp:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not TTS_ENABLED:
            return
        if message.author.bot or not message.guild:
            return
        if not message.content.startswith(","):
            return

        await self.speak(message, message.content[1:])

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        vc = member.guild.voice_client
        if vc is None or vc.channel is None:
            return

        humans = [m for m in vc.channel.members if not m.bot]
        if len(humans) == 0:
            try:
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect()
            except Exception:
                pass

async def setup(bot: commands.Bot):
    await bot.add_cog(TtsVoiceCog(bot))
