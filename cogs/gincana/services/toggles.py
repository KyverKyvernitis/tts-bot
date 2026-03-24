import asyncio
import random
import time
from pathlib import Path

import discord

from config import MUTE_TOGGLE_WORD, OFF_COLOR, TRIGGER_WORD

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


class GincanaToggleMixin:
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
                    await member.remove_roles(role, reason="gincana pica expirado")
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
                    await channel.set_permissions(member, overwrite=None, reason="gincana dj expirado")
                else:
                    await channel.set_permissions(member, overwrite=overwrite, reason="gincana dj expirado")
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
                "Por **`2 horas`** o cargo de ignorar TTS foi aplicado aos alvos atuais da gincana."
                if activated
                else "O cargo de ignorar TTS foi removido dos alvos atuais da gincana."
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
            if not self._matches_exact_trigger(content, "pica"):
                return False

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
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
                            await target.add_roles(ignored_tts_role, reason="gincana role toggle")
                        self._pica_expirations[key] = now + _PICA_DURATION_SECONDS
                        self.bot.loop.create_task(self._expire_pica_role_later(guild.id, target.id, role_id, _PICA_DURATION_SECONDS))
                        changed = True
                    else:
                        self._pica_expirations.pop(key, None)
                        if ignored_tts_role in getattr(target, "roles", []):
                            await target.remove_roles(ignored_tts_role, reason="gincana role toggle")
                        changed = True
                except Exception:
                    pass

            if changed:
                await self._refresh_targets_suffix_nicknames(guild, targets)
                await self._send_role_toggle_feedback(message, should_activate)
                await self._react_with_emoji(message, "✅", keep=False)
            return True
        async def _send_dj_toggle_feedback(self, message: discord.Message, activated: bool, affected_count: int, voice_channel: discord.VoiceChannel):
            if activated:
                title = "🎛️ Efeitos sonoros bloqueados"
                description = (
                    f"Os membros focados da gincana ficaram **sem poder usar efeitos sonoros por `6 horas`** em {voice_channel.mention}.\n\n"
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
            if not self._matches_exact_trigger(content, "dj"):
                return False

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
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
                        await voice_channel.set_permissions(target, overwrite=overwrite, reason="gincana dj trigger")
                        self._dj_expirations[key] = now + _DJ_DURATION_SECONDS
                        self.bot.loop.create_task(self._expire_dj_block_later(guild.id, voice_channel.id, target.id, _DJ_DURATION_SECONDS))
                    else:
                        self._dj_expirations.pop(key, None)
                        overwrite.use_soundboard = None
                        if overwrite.is_empty():
                            await voice_channel.set_permissions(target, overwrite=None, reason="gincana dj trigger")
                        else:
                            await voice_channel.set_permissions(target, overwrite=overwrite, reason="gincana dj trigger")
                    changed += 1
                except Exception:
                    pass

            if changed:
                await self._send_dj_toggle_feedback(message, should_activate, changed, voice_channel)
                await self._react_success_temporarily(message)
            return True
