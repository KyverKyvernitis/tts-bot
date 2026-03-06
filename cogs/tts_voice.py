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

    async def _reply_temp_error(self, message: discord.Message, content: str, delay: int = 7):
        """
        Responde com uma mensagem temporária e tenta apagar:
        - a mensagem do usuário que causou o erro
        - a resposta do bot
        após 'delay' segundos.
        """
        try:
            bot_msg = await message.reply(content)
        except Exception:
            return

        async def _cleanup():
            await asyncio.sleep(delay)

            # apagar resposta do bot
            try:
                await bot_msg.delete()
            except Exception:
                pass

            # apagar mensagem do usuário
            try:
                await message.delete()
            except Exception:
                pass

        asyncio.create_task(_cleanup())

    async def speak(self, message: discord.Message, text: str):
        if not message.guild:
            return

        vs = getattr(message.author, "voice", None)
        if not vs or not vs.channel:
            await self._reply_temp_error(message, "⚠️ Você precisa estar em um canal de voz para eu falar.")
            return

        channel: discord.VoiceChannel = vs.channel

        # bloqueia entrar se o ID informado estiver no canal
        if BLOCK_VOICE_BOT_ID and any(m.id == BLOCK_VOICE_BOT_ID for m in channel.members):
            await self._reply_temp_error(message, "❌ Já existe um bot de voz nesta call")
            return

        # permissões no canal de voz (connect/speak)
        me = message.guild.me or message.guild.get_member(self.bot.user.id)
        perms = channel.permissions_for(me)
        if not perms.connect:
            await self._reply_temp_error(message, "❌ Eu não tenho permissão **Conectar** nesse canal de voz.")
            return
        if not perms.speak:
            await self._reply_temp_error(message, "❌ Eu não tenho permissão **Falar** nesse canal de voz.")
            return

        # conectar/mover
        vc = message.guild.voice_client
        try:
            if vc is None:
                vc = await channel.connect()
            elif vc.channel and vc.channel.id != channel.id:
                await vc.move_to(channel)
        except Exception as e:
            await self._reply_temp_error(
                message,
                f"❌ Não consegui entrar na call. Erro: `{type(e).__name__}` — `{e}`"
            )
            return

        async with self._lock(message.guild.id):
            if vc.is_playing():
                vc.stop()

            text = (text or "").strip()
            if not text:
                await self._reply_temp_error(message, "⚠️ Escreva algo depois da vírgula. Ex: `,olá`")
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
                    await self._reply_temp_error(message, "❌ Falhei ao gerar a voz (gTTS).")
                    return

                try:
                    vc.play(discord.FFmpegPCMAudio(tmp))
                except Exception as e:
                    await self._reply_temp_error(
                        message,
                        f"❌ Não consegui tocar o áudio. Erro: `{type(e).__name__}` — `{e}`"
                    )
                    return

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
