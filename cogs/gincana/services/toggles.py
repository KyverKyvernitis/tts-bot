import asyncio
import time
from typing import Any

import discord

from ..constants import (
    _DJ_DURATION_SECONDS,
    _PICA_DURATION_SECONDS,
)


class GincanaToggleMixin:
    _DJ_PERMISSION_FLAGS = ("use_soundboard", "use_external_sounds")

    def _pica_effect_key(self, user_id: int) -> str:
        return str(int(user_id))

    def _dj_effect_key(self, channel_id: int, user_id: int) -> str:
        return f"{int(channel_id)}:{int(user_id)}"

    def _get_gincana_timed_effects(self, guild_id: int) -> dict[str, dict[str, dict[str, Any]]]:
        getter = getattr(self.db, "get_gincana_timed_effects", None)
        if callable(getter):
            return getter(int(guild_id))

        guild_cache = getattr(self.db, "guild_cache", {}) or {}
        raw = (guild_cache.get(int(guild_id), {}) or {}).get("gincana_timed_effects", {}) or {}
        result: dict[str, dict[str, dict[str, Any]]] = {"pica": {}, "dj": {}}
        for effect_name in ("pica", "dj"):
            effect_map = raw.get(effect_name, {}) if isinstance(raw, dict) else {}
            if not isinstance(effect_map, dict):
                continue
            for key, record in effect_map.items():
                if isinstance(record, dict):
                    result[effect_name][str(key)] = dict(record)
        return result

    async def _save_gincana_timed_effect(self, guild_id: int, effect_name: str, key: str, record: dict[str, Any]):
        setter = getattr(self.db, "set_gincana_timed_effect", None)
        if callable(setter):
            await setter(int(guild_id), str(effect_name), str(key), dict(record))
            return

        doc = self.db._get_guild_doc(int(guild_id))
        effects = doc.get("gincana_timed_effects", {}) or {}
        if not isinstance(effects, dict):
            effects = {}
        bucket = effects.get(str(effect_name), {}) or {}
        if not isinstance(bucket, dict):
            bucket = {}
        bucket[str(key)] = dict(record)
        effects[str(effect_name)] = bucket
        doc["gincana_timed_effects"] = effects
        await self.db._save_guild_doc(int(guild_id), doc)

    async def _remove_gincana_timed_effect(self, guild_id: int, effect_name: str, key: str):
        remover = getattr(self.db, "remove_gincana_timed_effect", None)
        if callable(remover):
            await remover(int(guild_id), str(effect_name), str(key))
            return

        doc = self.db._get_guild_doc(int(guild_id))
        effects = doc.get("gincana_timed_effects", {}) or {}
        if not isinstance(effects, dict):
            effects = {}
        bucket = effects.get(str(effect_name), {}) or {}
        if isinstance(bucket, dict):
            bucket.pop(str(key), None)
            effects[str(effect_name)] = bucket
        doc["gincana_timed_effects"] = effects
        await self.db._save_guild_doc(int(guild_id), doc)

    def _get_overwrite_flag(self, overwrite: discord.PermissionOverwrite, flag: str):
        try:
            return getattr(overwrite, flag)
        except Exception:
            return None

    def _set_overwrite_flag(self, overwrite: discord.PermissionOverwrite, flag: str, value) -> bool:
        try:
            setattr(overwrite, flag, value)
            return True
        except Exception:
            return False

    def _sound_overwrite_snapshot(self, overwrite: discord.PermissionOverwrite) -> dict[str, bool | None]:
        return {flag: self._get_overwrite_flag(overwrite, flag) for flag in self._DJ_PERMISSION_FLAGS}

    async def _get_effect_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(int(user_id))
        if member is not None:
            return member
        try:
            fetched = await guild.fetch_member(int(user_id))
            if isinstance(fetched, discord.Member):
                return fetched
        except Exception:
            return None
        return None

    def _target_voice_channel(self, member: discord.Member) -> discord.VoiceChannel | discord.StageChannel | None:
        voice_state = getattr(member, "voice", None)
        channel = getattr(voice_state, "channel", None)
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return channel
        return None

    def _voice_state_is_server_muted(self, member: discord.Member) -> bool:
        voice_state = getattr(member, "voice", None)
        try:
            return bool(getattr(voice_state, "mute", False))
        except Exception:
            return False

    async def _restore_pica_nickname(self, member: discord.Member, record: dict[str, Any]) -> None:
        previous_nick = record.get("previous_nick", None)
        current_nick = getattr(member, "nick", None)
        if current_nick is None:
            return
        try:
            has_managed_suffix = self._strip_gincana_suffix(current_nick) != current_nick
        except Exception:
            has_managed_suffix = False
        if not has_managed_suffix:
            return
        try:
            await member.edit(nick=previous_nick, reason="economia pica expirado: restaurar apelido")
        except Exception:
            pass

    async def _apply_pica_effect(self, guild: discord.Guild, member: discord.Member, record: dict[str, Any]) -> bool:
        changed = False
        try:
            if self._target_voice_channel(member) is not None and not self._voice_state_is_server_muted(member):
                await member.edit(mute=True, reason="economia pica trigger")
                changed = True
        except Exception:
            pass

        try:
            await self._refresh_targets_suffix_nicknames(guild, [member])
        except Exception:
            pass
        return changed

    async def _cleanup_pica_effect(self, guild: discord.Guild, user_id: int, record: dict[str, Any], *, force_remove_record: bool = False) -> bool:
        key = self._pica_effect_key(user_id)
        self._pica_expirations.pop((int(guild.id), int(user_id)), None)

        member = await self._get_effect_member(guild, int(user_id))
        if member is None:
            if force_remove_record:
                await self._remove_gincana_timed_effect(guild.id, "pica", key)
                return True
            return False

        previous_mute = bool(record.get("previous_mute", False))
        voice_channel = self._target_voice_channel(member)
        can_finish = True

        if not previous_mute:
            if voice_channel is None:
                # O Discord só permite alterar mute de voz quando o membro tem voice state.
                # Mantém o registro expirado para corrigir assim que ele entrar em call.
                can_finish = False
            else:
                try:
                    if self._voice_state_is_server_muted(member):
                        await member.edit(mute=False, reason="economia pica expirado")
                except Exception:
                    can_finish = False

        try:
            await self._restore_pica_nickname(member, record)
        except Exception:
            pass

        if can_finish or force_remove_record:
            await self._remove_gincana_timed_effect(guild.id, "pica", key)
            return True
        return False

    async def _expire_pica_mute_later(self, guild_id: int, user_id: int, delay: float):
        try:
            await asyncio.sleep(max(0.0, float(delay)))
        except asyncio.CancelledError:
            return
        except Exception:
            return

        key = (int(guild_id), int(user_id))
        expires_at = float(self._pica_expirations.get(key, 0.0) or 0.0)
        now = time.time()
        if expires_at <= 0 or expires_at > now + 1.0:
            return

        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            self._pica_expirations.pop(key, None)
            return

        record = self._get_gincana_timed_effects(guild.id).get("pica", {}).get(self._pica_effect_key(user_id), {})
        if not record:
            self._pica_expirations.pop(key, None)
            return
        await self._cleanup_pica_effect(guild, int(user_id), record)

    async def _apply_dj_effect(self, guild: discord.Guild, channel: discord.VoiceChannel, member: discord.Member, record: dict[str, Any]) -> bool:
        try:
            overwrite = channel.overwrites_for(member)
            changed = False
            for flag in self._DJ_PERMISSION_FLAGS:
                if self._set_overwrite_flag(overwrite, flag, False):
                    changed = True
            if not changed:
                return False
            await channel.set_permissions(member, overwrite=overwrite, reason="economia dj trigger")
            return True
        except Exception:
            return False

    async def _cleanup_dj_effect(self, guild: discord.Guild, channel_id: int, user_id: int, record: dict[str, Any], *, force_remove_record: bool = False) -> bool:
        key = self._dj_effect_key(channel_id, user_id)
        self._dj_expirations.pop((int(guild.id), int(channel_id), int(user_id)), None)

        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.VoiceChannel):
            await self._remove_gincana_timed_effect(guild.id, "dj", key)
            return True

        member = await self._get_effect_member(guild, int(user_id))
        if member is None:
            if force_remove_record:
                await self._remove_gincana_timed_effect(guild.id, "dj", key)
                return True
            return False

        previous = record.get("previous_overwrite", {}) or {}
        if not isinstance(previous, dict):
            previous = {}

        try:
            overwrite = channel.overwrites_for(member)
            for flag in self._DJ_PERMISSION_FLAGS:
                if flag in previous:
                    self._set_overwrite_flag(overwrite, flag, previous.get(flag))
            if overwrite.is_empty():
                await channel.set_permissions(member, overwrite=None, reason="economia dj expirado")
            else:
                await channel.set_permissions(member, overwrite=overwrite, reason="economia dj expirado")
        except Exception:
            if not force_remove_record:
                return False

        await self._remove_gincana_timed_effect(guild.id, "dj", key)
        return True

    async def _expire_dj_block_later(self, guild_id: int, channel_id: int, user_id: int, delay: float):
        try:
            await asyncio.sleep(max(0.0, float(delay)))
        except asyncio.CancelledError:
            return
        except Exception:
            return

        key = (int(guild_id), int(channel_id), int(user_id))
        expires_at = float(self._dj_expirations.get(key, 0.0) or 0.0)
        now = time.time()
        if expires_at <= 0 or expires_at > now + 1.0:
            return

        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            self._dj_expirations.pop(key, None)
            return

        record = self._get_gincana_timed_effects(guild.id).get("dj", {}).get(self._dj_effect_key(channel_id, user_id), {})
        if not record:
            self._dj_expirations.pop(key, None)
            return
        await self._cleanup_dj_effect(guild, int(channel_id), int(user_id), record)

    async def _send_role_toggle_feedback(self, message: discord.Message, affected_count: int, voice_channel: discord.VoiceChannel):
        title = "🔇 Mute aplicado"
        description = (
            f"Mutei **{affected_count}** alvo(s) em {voice_channel.mention} por **6 horas**.\n"
            "Se o bot reiniciar ou o timer falhar, o estado é corrigido quando o membro entrar em call."
        )
        embed = self._make_embed(title, description, ok=True)
        try:
            await message.channel.send(embed=embed)
        except Exception:
            pass

    async def _handle_role_toggle_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = message.content or ""
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

        targets = [target for target in self._resolve_targets(guild, voice_channel) if not getattr(target, "bot", False)]
        if not targets:
            return True

        now = time.time()
        expires_at = now + float(_PICA_DURATION_SECONDS)
        affected = 0
        current_records = self._get_gincana_timed_effects(guild.id).get("pica", {})

        for target in targets:
            try:
                key = self._pica_effect_key(target.id)
                existing = current_records.get(key, {}) or {}
                previous_nick = existing.get("previous_nick", getattr(target, "nick", None)) if existing else getattr(target, "nick", None)
                previous_mute = bool(existing.get("previous_mute", self._voice_state_is_server_muted(target))) if existing else self._voice_state_is_server_muted(target)
                record = {
                    "user_id": int(target.id),
                    "channel_id": int(voice_channel.id),
                    "expires_at": float(expires_at),
                    "previous_mute": bool(previous_mute),
                    "previous_nick": previous_nick,
                    "created_at": float(existing.get("created_at", now) or now) if isinstance(existing, dict) else float(now),
                    "updated_at": float(now),
                }
                await self._apply_pica_effect(guild, target, record)
                await self._save_gincana_timed_effect(guild.id, "pica", key, record)
                self._pica_expirations[(guild.id, target.id)] = expires_at
                self.bot.loop.create_task(self._expire_pica_mute_later(guild.id, target.id, _PICA_DURATION_SECONDS))
                affected += 1
            except Exception:
                pass

        if affected:
            await self._send_role_toggle_feedback(message, affected, voice_channel)
            await self._react_with_emoji(message, "✅", keep=False)
        return True

    async def _send_dj_toggle_feedback(self, message: discord.Message, affected_count: int, voice_channel: discord.VoiceChannel):
        title = "🎛️ Efeitos sonoros bloqueados"
        description = (
            f"Bloqueei os efeitos sonoros de **{affected_count}** alvo(s) em {voice_channel.mention} por **6 horas**.\n"
            "O bloqueio também volta depois de restart enquanto ainda estiver no prazo."
        )
        embed = self._make_embed(title, description, ok=True)
        try:
            await message.channel.send(embed=embed)
        except Exception:
            pass

    async def _handle_dj_toggle_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = message.content or ""
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

        targets = [
            member
            for member in self._resolve_targets(guild, voice_channel)
            if not getattr(member, "bot", False) and not self._is_staff_member(member)
        ]

        if not targets:
            embed = self._make_embed(
                "Nenhum alvo para a trigger dj",
                "Não encontrei alvos elegíveis nesse canal de voz. Staffs são ignorados por essa trigger.",
                ok=False,
            )
            try:
                await message.channel.send(embed=embed)
            except Exception:
                pass
            return True

        now = time.time()
        expires_at = now + float(_DJ_DURATION_SECONDS)
        affected = 0
        current_records = self._get_gincana_timed_effects(guild.id).get("dj", {})

        for target in targets:
            try:
                key = self._dj_effect_key(voice_channel.id, target.id)
                existing = current_records.get(key, {}) or {}
                overwrite = voice_channel.overwrites_for(target)
                previous_overwrite = existing.get("previous_overwrite") if isinstance(existing, dict) else None
                if not isinstance(previous_overwrite, dict):
                    previous_overwrite = self._sound_overwrite_snapshot(overwrite)
                record = {
                    "channel_id": int(voice_channel.id),
                    "user_id": int(target.id),
                    "expires_at": float(expires_at),
                    "previous_overwrite": previous_overwrite,
                    "created_at": float(existing.get("created_at", now) or now) if isinstance(existing, dict) else float(now),
                    "updated_at": float(now),
                }
                applied = await self._apply_dj_effect(guild, voice_channel, target, record)
                if not applied:
                    continue
                await self._save_gincana_timed_effect(guild.id, "dj", key, record)
                self._dj_expirations[(guild.id, voice_channel.id, target.id)] = expires_at
                self.bot.loop.create_task(self._expire_dj_block_later(guild.id, voice_channel.id, target.id, _DJ_DURATION_SECONDS))
                affected += 1
            except Exception:
                pass

        if affected:
            await self._send_dj_toggle_feedback(message, affected, voice_channel)
            await self._react_success_temporarily(message)
        return True

    async def _rehydrate_gincana_timed_effects(self):
        now = time.time()
        for guild in list(getattr(self.bot, "guilds", []) or []):
            effects = self._get_gincana_timed_effects(guild.id)

            for key, record in list((effects.get("pica") or {}).items()):
                try:
                    user_id = int(record.get("user_id") or key)
                    expires_at = float(record.get("expires_at", 0.0) or 0.0)
                    if expires_at <= now:
                        member = await self._get_effect_member(guild, user_id)
                        if member is not None and self._target_voice_channel(member) is not None:
                            await self._cleanup_pica_effect(guild, user_id, record)
                        # Se não está em call, deixa o registro expirado para autocorrigir na próxima entrada.
                        continue

                    member = await self._get_effect_member(guild, user_id)
                    if member is not None:
                        await self._apply_pica_effect(guild, member, record)
                    self._pica_expirations[(guild.id, user_id)] = expires_at
                    self.bot.loop.create_task(self._expire_pica_mute_later(guild.id, user_id, expires_at - now))
                except Exception:
                    pass

            for key, record in list((effects.get("dj") or {}).items()):
                try:
                    user_id = int(record.get("user_id") or 0)
                    channel_id = int(record.get("channel_id") or 0)
                    expires_at = float(record.get("expires_at", 0.0) or 0.0)
                    if not user_id or not channel_id:
                        await self._remove_gincana_timed_effect(guild.id, "dj", str(key))
                        continue
                    if expires_at <= now:
                        await self._cleanup_dj_effect(guild, channel_id, user_id, record)
                        continue

                    channel = guild.get_channel(channel_id)
                    member = await self._get_effect_member(guild, user_id)
                    if isinstance(channel, discord.VoiceChannel) and member is not None:
                        await self._apply_dj_effect(guild, channel, member, record)
                    self._dj_expirations[(guild.id, channel_id, user_id)] = expires_at
                    self.bot.loop.create_task(self._expire_dj_block_later(guild.id, channel_id, user_id, expires_at - now))
                except Exception:
                    pass

    async def _handle_gincana_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = getattr(member, "guild", None)
        if guild is None or getattr(member, "bot", False):
            return

        after_channel = getattr(after, "channel", None)
        if after_channel is None:
            return

        now = time.time()
        effects = self._get_gincana_timed_effects(guild.id)

        pica_record = (effects.get("pica") or {}).get(self._pica_effect_key(member.id))
        if isinstance(pica_record, dict):
            expires_at = float(pica_record.get("expires_at", 0.0) or 0.0)
            if expires_at <= now:
                await self._cleanup_pica_effect(guild, member.id, pica_record)
            else:
                await self._apply_pica_effect(guild, member, pica_record)
                self._pica_expirations[(guild.id, member.id)] = expires_at

        for key, record in list((effects.get("dj") or {}).items()):
            try:
                if int(record.get("user_id") or 0) != int(member.id):
                    continue
                channel_id = int(record.get("channel_id") or 0)
                expires_at = float(record.get("expires_at", 0.0) or 0.0)
                if expires_at <= now:
                    await self._cleanup_dj_effect(guild, channel_id, member.id, record)
                    continue
                if int(getattr(after_channel, "id", 0) or 0) == channel_id and isinstance(after_channel, discord.VoiceChannel):
                    await self._apply_dj_effect(guild, after_channel, member, record)
                    self._dj_expirations[(guild.id, channel_id, member.id)] = expires_at
            except Exception:
                pass
