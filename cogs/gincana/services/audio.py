import asyncio
import random
import time
from pathlib import Path

import discord

from config import GUILD_IDS, MUTE_TOGGLE_WORD, OFF_COLOR, TRIGGER_WORD

from ..constants import (
    _ALVO_WORD_RE,
    _ATIRAR_WORD_RE,
    _BUCKSHOT_WORD_RE,
    _DJ_DURATION_SECONDS,
    _DJ_TOGGLE_WORD_RE,
    _PICA_DURATION_SECONDS,
    _POKER_WORD_RE,
    _ROLETA_WORD_RE,
    _ROLE_TOGGLE_WORD_RE,
    ALVO_STAKE,
    BUCKSHOT_STAKE,
    ROLETA_COST,
    ROLETA_JACKPOT_CHIPS,
)


class GincanaAudioMixin:
        def _sfx_path(self, filename: str) -> Path:
            return Path(__file__).resolve().parents[2] / "assets" / "sfx" / filename
        def _buckshot_sfx_path(self) -> Path:
            return self._sfx_path("buckshot.mp3")
        def _pinto_sfx_path(self) -> Path:
            return self._sfx_path("pinto.mp3")
        def _roleta_sfx_path(self) -> Path:
            return self._sfx_path("roleta777.mp3")
        async def _play_sfx_file(self, guild: discord.Guild, voice_channel: discord.VoiceChannel, sfx_path: Path) -> bool:
            if not sfx_path.exists():
                return False

            voice_client = guild.voice_client
            connected_here = False

            try:
                if voice_client is None or not getattr(voice_client, "is_connected", lambda: False)():
                    voice_client = await voice_channel.connect(self_deaf=True)
                    connected_here = True
                elif getattr(voice_client, "channel", None) != voice_channel:
                    await voice_client.move_to(voice_channel)

                if voice_client is None:
                    return False

                try:
                    if voice_client.is_playing() or voice_client.is_paused():
                        voice_client.stop()
                except Exception:
                    pass

                source = discord.FFmpegPCMAudio(str(sfx_path))
                voice_client.play(source)
                return True
            except Exception:
                return False
            finally:
                if connected_here and voice_client is not None:
                    async def _delayed_disconnect(vc: discord.VoiceClient):
                        await asyncio.sleep(2.0)
                        try:
                            if vc.is_connected() and not vc.is_playing():
                                await vc.disconnect(force=False)
                        except Exception:
                            pass

                    asyncio.create_task(_delayed_disconnect(voice_client))
        async def _play_buckshot_sfx(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> bool:
            return await self._play_sfx_file(guild, voice_channel, self._buckshot_sfx_path())
        async def _play_pinto_sfx(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> bool:
            return await self._play_sfx_file(guild, voice_channel, self._pinto_sfx_path())
        async def _play_roleta_sfx(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> bool:
            return await self._play_sfx_file(guild, voice_channel, self._roleta_sfx_path())
        async def _react_with_emoji(self, message: discord.Message, emoji: str, *, keep: bool, delay: float = 3.0):
            reaction_emoji = emoji
            try:
                if isinstance(emoji, str) and emoji.startswith("<") and emoji.endswith(">"):
                    reaction_emoji = discord.PartialEmoji.from_str(emoji)
                await message.add_reaction(reaction_emoji)
            except Exception:
                return

            if keep:
                return

            async def _cleanup():
                await asyncio.sleep(max(0.0, delay))
                try:
                    await message.remove_reaction(reaction_emoji, self.bot.user)
                except Exception:
                    pass

            asyncio.create_task(_cleanup())
