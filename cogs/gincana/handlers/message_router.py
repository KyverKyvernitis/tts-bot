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


class GincanaMessageRouterMixin:
        def _matches_exact_trigger(self, content: str | None, trigger: str) -> bool:
            if not trigger:
                return False
            return str(content or "").strip().casefold() == str(trigger).strip().casefold()
        async def _handle_gincana_message(self, message: discord.Message):
            if message.author.bot or not message.guild:
                return

            if GUILD_IDS and message.guild.id not in GUILD_IDS:
                return

            if await self._handle_focus_trigger(message):
                return

            if await self._handle_role_toggle_trigger(message):
                return

            if await self._handle_dj_toggle_trigger(message):
                return

            if await self._handle_buckshot_trigger(message):
                return

            if await self._handle_target_trigger(message):
                return

            if await self._handle_disparar_trigger(message):
                return

            if await self._handle_atirar_trigger(message):
                return

            if await self._handle_poker_trigger(message):
                return

            if await self._handle_roleta_trigger(message):
                return

            if not self.db.gincana_enabled(message.guild.id):
                return

            if self._gincana_only_kick_members(message.guild.id) and not self._is_staff_member(message.author):
                return

            if not TRIGGER_WORD and not MUTE_TOGGLE_WORD:
                return

            author_voice = getattr(message.author, "voice", None)
            voice_channel = getattr(author_voice, "channel", None)
            if not isinstance(voice_channel, discord.VoiceChannel):
                return

            content = (message.content or "")
            normalized_content = content.strip().casefold()
            targets = self._resolve_targets(message.guild, voice_channel)

            if not targets:
                return

            target_ids = {member.id for member in targets}
            author_is_target = message.author.id in target_ids
            author_is_focused_non_staff = self._is_focused_non_staff_member(message.author)

            did_trigger_action = False

            if TRIGGER_WORD and normalized_content == TRIGGER_WORD.casefold():
                did_trigger_action = True
                trigger_voice_channel = None
                for target in targets:
                    target_channel = getattr(getattr(target, "voice", None), "channel", None)
                    if isinstance(target_channel, discord.VoiceChannel):
                        trigger_voice_channel = target_channel
                        break

                if trigger_voice_channel is not None:
                    try:
                        await self._play_pinto_sfx(message.guild, trigger_voice_channel)
                    except Exception:
                        pass
                    try:
                        await asyncio.sleep(0.20)
                    except Exception:
                        pass

                for target in targets:
                    if target.voice and target.voice.channel:
                        try:
                            await target.move_to(None, reason="gincana disconnect")
                        except Exception:
                            pass

            if MUTE_TOGGLE_WORD and normalized_content == MUTE_TOGGLE_WORD.casefold():
                if author_is_focused_non_staff:
                    return
                did_trigger_action = True
                if author_is_target:
                    return

                for target in targets:
                    if target.voice and target.voice.channel:
                        try:
                            new_muted = not bool(target.voice.mute)
                            await target.edit(mute=new_muted, reason="gincana toggle mute")
                        except Exception:
                            pass

                await self._refresh_targets_suffix_nicknames(message.guild, targets)

            if did_trigger_action:
                await self._react_success_temporarily(message)
