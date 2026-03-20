import asyncio
import random
import time

import discord

from config import GUILD_IDS, MUTE_TOGGLE_WORD, TRIGGER_WORD

from .constants import (
    _DJ_DURATION_SECONDS,
    _DJ_TOGGLE_WORD_RE,
    _PICA_DURATION_SECONDS,
    _ROLETA_WORD_RE,
    _ROLE_TOGGLE_WORD_RE,
)


class AntiMzkTriggerMixin:
    async def _expire_pica_role_later(self, guild_id: int, user_id: int, role_id: int, delay: float):
        try:
            await asyncio.sleep(max(0.0, delay))
        except Exception:
            return

        key = (guild_id, user_id)
        expires_at = self._pica_expirations.get(key)
        now = time.time()
        if expires_at is None or expires_at > now + 1.0:
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            self._pica_expirations.pop(key, None)
            return

        member = guild.get_member(user_id)
        role = guild.get_role(role_id) if role_id else None
        if member is None or role is None:
            self._pica_expirations.pop(key, None)
            return

        try:
            if role in getattr(member, "roles", []):
                await member.remove_roles(role, reason="modo censura pica expirado")
        except Exception:
            return
        finally:
            self._pica_expirations.pop(key, None)

        try:
            await self._refresh_target_suffix_nickname(member, role)
        except Exception:
            pass

    async def _expire_dj_block_later(self, guild_id: int, channel_id: int, user_id: int, delay: float):
        try:
            await asyncio.sleep(max(0.0, delay))
        except Exception:
            return

        key = (guild_id, channel_id, user_id)
        expires_at = self._dj_expirations.get(key)
        now = time.time()
        if expires_at is None or expires_at > now + 1.0:
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            self._dj_expirations.pop(key, None)
            return

        channel = guild.get_channel(channel_id)
        member = guild.get_member(user_id)
        if not isinstance(channel, discord.VoiceChannel) or member is None:
            self._dj_expirations.pop(key, None)
            return

        try:
            overwrite = channel.overwrites_for(member)
            overwrite.use_soundboard = None
            if overwrite.is_empty():
                await channel.set_permissions(member, overwrite=None, reason="modo censura dj expirado")
            else:
                await channel.set_permissions(member, overwrite=overwrite, reason="modo censura dj expirado")
        except Exception:
            return
        finally:
            self._dj_expirations.pop(key, None)

    def _tracked_pica_targets(self, guild: discord.Guild, current_targets: list[discord.Member]) -> list[discord.Member]:
        targets: dict[int, discord.Member] = {member.id: member for member in current_targets}
        for tracked_guild_id, tracked_user_id in list(self._pica_expirations.keys()):
            if tracked_guild_id != guild.id:
                continue
            member = guild.get_member(tracked_user_id)
            if member is not None:
                targets[member.id] = member
        return list(targets.values())

    def _tracked_dj_targets(self, guild: discord.Guild, voice_channel: discord.VoiceChannel, current_targets: list[discord.Member]) -> list[discord.Member]:
        targets: dict[int, discord.Member] = {member.id: member for member in current_targets}
        for tracked_guild_id, tracked_channel_id, tracked_user_id in list(self._dj_expirations.keys()):
            if tracked_guild_id != guild.id or tracked_channel_id != voice_channel.id:
                continue
            member = guild.get_member(tracked_user_id)
            if member is not None:
                targets[member.id] = member
        return list(targets.values())

    async def _send_role_toggle_feedback(self, message: discord.Message, activated: bool):
        title = "🔇 TTS desativado para os alvos" if activated else "🔊 TTS reativado para os alvos"
        description = (
            "Por **`2 horas`** o cargo de ignorar TTS foi aplicado aos alvos atuais do modo censura."
            if activated
            else "O cargo de ignorar TTS foi removido dos alvos atuais do modo censura."
        )
        embed = self._make_embed(title, description, ok=not activated)
        try:
            await message.channel.send(embed=embed)
        except Exception:
            pass

    async def _handle_role_toggle_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = (message.content or "")
        if not _ROLE_TOGGLE_WORD_RE.search(content):
            return False

        if GUILD_IDS and guild.id not in GUILD_IDS:
            return True

        if not self.db.anti_mzk_enabled(guild.id):
            return True

        if self._anti_mzk_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True

        if self._is_focused_non_staff_member(message.author):
            return True

        author_voice = getattr(message.author, "voice", None)
        voice_channel = getattr(author_voice, "channel", None)
        if not isinstance(voice_channel, discord.VoiceChannel):
            return True

        base_targets = self._resolve_targets(guild, voice_channel)
        targets = self._tracked_pica_targets(guild, base_targets)
        if not targets:
            return True

        ignored_tts_role = None
        ignored_tts_role_id = 0
        try:
            ignored_tts_role_id = max(0, int(self.db.get_ignored_tts_role_id(guild.id) or 0))
        except Exception:
            ignored_tts_role_id = 0
        if ignored_tts_role_id:
            ignored_tts_role = guild.get_role(ignored_tts_role_id)

        if ignored_tts_role is None:
            embed = self._make_embed(
                "Cargo ignorado não configurado",
                "Defina primeiro o cargo ignorado do TTS no painel do servidor para usar a trigger **pica**.",
                ok=False,
            )
            try:
                await message.channel.send(embed=embed)
            except Exception:
                pass
            return True

        tracked_ids = {user_id for tracked_guild_id, user_id in self._pica_expirations.keys() if tracked_guild_id == guild.id}
        should_activate = any(target.id not in tracked_ids for target in base_targets) or not tracked_ids

        changed = False
        now = time.time()
        role_id = int(getattr(ignored_tts_role, "id", 0) or 0)
        for target in targets:
            try:
                key = (guild.id, target.id)
                if should_activate:
                    if ignored_tts_role not in getattr(target, "roles", []):
                        await target.add_roles(ignored_tts_role, reason="modo censura role toggle")
                    self._pica_expirations[key] = now + _PICA_DURATION_SECONDS
                    self.bot.loop.create_task(self._expire_pica_role_later(guild.id, target.id, role_id, _PICA_DURATION_SECONDS))
                    changed = True
                else:
                    self._pica_expirations.pop(key, None)
                    if ignored_tts_role in getattr(target, "roles", []):
                        await target.remove_roles(ignored_tts_role, reason="modo censura role toggle")
                    changed = True
            except Exception:
                pass

        if changed:
            await self._refresh_targets_suffix_nicknames(guild, targets)
            await self._send_role_toggle_feedback(message, should_activate)
            await self._react_success_temporarily(message)
        return True

    async def _send_dj_toggle_feedback(self, message: discord.Message, activated: bool, affected_count: int, voice_channel: discord.VoiceChannel):
        if activated:
            title = "🎛️ Efeitos sonoros bloqueados"
            description = (
                f"Os membros focados do modo censura ficaram **sem poder usar efeitos sonoros por `6 horas`** em {voice_channel.mention}.\n\n"
                "Staffs não são afetados"
            )
        else:
            title = "🎚️ Efeitos sonoros liberados"
            description = f"Removi o bloqueio de **efeitos sonoros** dos membros focados em {voice_channel.mention}."
        embed = self._make_embed(title, description, ok=not activated)
        try:
            await message.channel.send(embed=embed)
        except Exception:
            pass

    async def _handle_dj_toggle_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = (message.content or "")
        if not _DJ_TOGGLE_WORD_RE.search(content):
            return False

        if GUILD_IDS and guild.id not in GUILD_IDS:
            return True

        if not self.db.anti_mzk_enabled(guild.id):
            return True

        if self._anti_mzk_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True

        author_voice = getattr(message.author, "voice", None)
        voice_channel = getattr(author_voice, "channel", None)
        if not isinstance(voice_channel, discord.VoiceChannel):
            return True

        focus_targets = self._iter_focused_members(guild, voice_channel)
        current_targets = [member for member in focus_targets if not self._is_staff_member(member)]
        targets = self._tracked_dj_targets(guild, voice_channel, current_targets)

        if not targets:
            embed = self._make_embed(
                "Nenhum alvo para a trigger dj",
                "Não há membros focados elegíveis nesse canal de voz. Staffs são ignorados por essa trigger.",
                ok=False,
            )
            try:
                await message.channel.send(embed=embed)
            except Exception:
                pass
            return True

        tracked_ids = {user_id for tracked_guild_id, tracked_channel_id, user_id in self._dj_expirations.keys() if tracked_guild_id == guild.id and tracked_channel_id == voice_channel.id}
        should_activate = any(target.id not in tracked_ids for target in current_targets) or not tracked_ids

        changed = 0
        now = time.time()
        for target in targets:
            try:
                overwrite = voice_channel.overwrites_for(target)
                key = (guild.id, voice_channel.id, target.id)
                if should_activate:
                    overwrite.use_soundboard = False
                    await voice_channel.set_permissions(target, overwrite=overwrite, reason="modo censura dj trigger")
                    self._dj_expirations[key] = now + _DJ_DURATION_SECONDS
                    self.bot.loop.create_task(self._expire_dj_block_later(guild.id, voice_channel.id, target.id, _DJ_DURATION_SECONDS))
                else:
                    self._dj_expirations.pop(key, None)
                    overwrite.use_soundboard = None
                    if overwrite.is_empty():
                        await voice_channel.set_permissions(target, overwrite=None, reason="modo censura dj trigger")
                    else:
                        await voice_channel.set_permissions(target, overwrite=overwrite, reason="modo censura dj trigger")
                changed += 1
            except Exception:
                pass

        if changed:
            await self._send_dj_toggle_feedback(message, should_activate, changed, voice_channel)
            await self._react_success_temporarily(message)
        return True

    async def _handle_roleta_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = (message.content or "")
        if not _ROLETA_WORD_RE.search(content):
            return False

        if GUILD_IDS and guild.id not in GUILD_IDS:
            return True

        if not self.db.anti_mzk_enabled(guild.id):
            return True

        if self._anti_mzk_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True

        author_voice = getattr(message.author, "voice", None)
        voice_channel = getattr(author_voice, "channel", None)
        if not isinstance(voice_channel, discord.VoiceChannel):
            return True

        targets = self._resolve_targets(guild, voice_channel)
        if not targets:
            embed = self._make_embed(
                "🎲 Roleta sem alvos",
                "Não há usuários alvo do modo censura nesse canal de voz para usar a trigger **roleta**.",
                ok=False,
            )
            try:
                await message.channel.send(embed=embed)
            except Exception:
                pass
            return True

        rolled = random.randint(1, 10)
        success = rolled == 1

        if success:
            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        await target.move_to(None, reason="modo censura roleta")
                    except Exception:
                        pass
            title = "💥🎰 JACKPOT!!"
            description = "Membros alvos foram tirados da call"
            ok = False
        else:
            title = "🎰 Não foi dessa vez..."
            description = "Ninguém foi expulso da call... Ainda (chance: **10%**)"
            ok = None

        embed = self._make_embed(title, description, ok=ok)
        try:
            await message.channel.send(embed=embed)
        except Exception:
            pass

        await self._react_success_temporarily(message)
        return True

    async def _handle_antimzk_message(self, message: discord.Message):
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

        if await self._handle_roleta_trigger(message):
            return

        if not self.db.anti_mzk_enabled(message.guild.id):
            return

        if self._anti_mzk_only_kick_members(message.guild.id) and not self._is_staff_member(message.author):
            return

        if not TRIGGER_WORD and not MUTE_TOGGLE_WORD:
            return

        author_voice = getattr(message.author, "voice", None)
        voice_channel = getattr(author_voice, "channel", None)
        if not isinstance(voice_channel, discord.VoiceChannel):
            return

        content = (message.content or "").lower()
        targets = self._resolve_targets(message.guild, voice_channel)

        if not targets:
            return

        target_ids = {member.id for member in targets}
        author_is_target = message.author.id in target_ids
        author_is_focused_non_staff = self._is_focused_non_staff_member(message.author)

        did_trigger_action = False

        if TRIGGER_WORD and TRIGGER_WORD in content:
            did_trigger_action = True
            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        await target.move_to(None, reason="modo censura disconnect")
                    except Exception:
                        pass

        if MUTE_TOGGLE_WORD and MUTE_TOGGLE_WORD in content:
            if author_is_focused_non_staff:
                return
            did_trigger_action = True
            if author_is_target:
                return

            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        new_muted = not bool(target.voice.mute)
                        await target.edit(mute=new_muted, reason="modo censura toggle mute")
                    except Exception:
                        pass

            await self._refresh_targets_suffix_nicknames(message.guild, targets)

        if did_trigger_action:
            await self._react_success_temporarily(message)
