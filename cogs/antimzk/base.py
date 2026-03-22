import asyncio

import discord
from discord.ext import commands

from config import GUILD_IDS, OFF_COLOR, ON_COLOR
from .constants import CHIPS_DEFAULT, CHIPS_RESET_SECONDS
from db import SettingsDB


class AntiMzkBase:
    _ANTI_MZK_SUFFIXES = (" [ultra-censurado]", " [censurado]", " [antitts]")

    def __init__(self, bot: commands.Bot, db: SettingsDB):
        self.bot = bot
        self.db = db
        self._pica_expirations: dict[tuple[int, int], float] = {}
        self._dj_expirations: dict[tuple[int, int, int], float] = {}
        self._roleta_last_used: dict[int, float] = {}
        self._roleta_running_guilds: set[int] = set()
        self._buckshot_sessions: dict[int, dict] = {}
        self._poker_games: dict[int, object] = {}

    def _strip_antimzk_suffix(self, name: str) -> str:
        base = str(name or "").rstrip()
        lowered = base.casefold()
        for suffix in self._ANTI_MZK_SUFFIXES:
            if lowered.endswith(suffix.casefold()):
                return base[: -len(suffix)].rstrip()
        return base

    def _target_suffix(self, member: discord.Member, ignored_tts_role: discord.Role | None) -> str:
        is_muted = False
        voice_state = getattr(member, "voice", None)
        if voice_state is not None:
            try:
                is_muted = bool(getattr(voice_state, "mute", False))
            except Exception:
                is_muted = False

        ignores_tts = ignored_tts_role is not None and ignored_tts_role in getattr(member, "roles", [])
        if is_muted and ignores_tts:
            return " [ultra-censurado]"
        if is_muted:
            return " [censurado]"
        if ignores_tts:
            return " [antitts]"
        return ""

    async def _refresh_target_suffix_nickname(self, member: discord.Member, ignored_tts_role: discord.Role | None):
        me = member.guild.me
        if me is None:
            return

        perms = getattr(me.guild_permissions, "manage_nicknames", False)
        if not perms:
            return

        try:
            if member == member.guild.owner:
                return
            if getattr(me, "top_role", None) is not None and getattr(member, "top_role", None) is not None:
                if me.top_role <= member.top_role:
                    return
        except Exception:
            pass

        current_nick = member.nick
        current_display_name = str(getattr(member, "display_name", "") or "").strip()
        current_name = current_nick if current_nick is not None else current_display_name or member.name
        base_name = self._strip_antimzk_suffix(current_name) or self._strip_antimzk_suffix(current_display_name) or member.name
        suffix = self._target_suffix(member, ignored_tts_role)
        desired_full = f"{base_name}{suffix}".strip()

        current_nick_has_managed_suffix = bool(current_nick and self._strip_antimzk_suffix(current_nick) != current_nick)

        if current_nick is None:
            if not suffix:
                return
            if desired_full == current_display_name:
                return
            new_nick = desired_full
        else:
            if not suffix:
                if current_nick_has_managed_suffix:
                    new_nick = None
                elif base_name == member.name:
                    new_nick = None
                else:
                    return
            else:
                new_nick = desired_full

        if isinstance(new_nick, str) and len(new_nick) > 32:
            allowed = max(0, 32 - len(suffix))
            trimmed = base_name[:allowed].rstrip()
            new_nick = f"{trimmed}{suffix}".strip() if suffix else (trimmed or None)
            if current_nick is None and new_nick == member.name:
                return

        if new_nick == current_nick:
            return

        try:
            await member.edit(nick=new_nick, reason="modo censura atualizar sufixo do alvo")
        except Exception:
            pass

    async def _refresh_targets_suffix_nicknames(self, guild: discord.Guild, targets: list[discord.Member]):
        ignored_tts_role = None
        ignored_tts_role_id = 0
        try:
            ignored_tts_role_id = max(0, int(self.db.get_ignored_tts_role_id(guild.id) or 0))
        except Exception:
            ignored_tts_role_id = 0
        if ignored_tts_role_id:
            ignored_tts_role = guild.get_role(ignored_tts_role_id)

        for target in targets:
            await self._refresh_target_suffix_nickname(target, ignored_tts_role)

    def _make_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color(ON_COLOR) if ok else discord.Color(OFF_COLOR),
        )
        return embed




    def _make_chip_balance_embed(self, member: discord.Member) -> discord.Embed:
        guild_id = member.guild.id
        chips = self.db.get_user_chips(guild_id, member.id, default=CHIPS_DEFAULT)
        remaining = 0.0
        if chips <= 0:
            last_reset = self.db.get_user_chip_reset_at(guild_id, member.id)
            import time
            now = time.time()
            elapsed = max(0.0, now - float(last_reset or 0.0))
            remaining = max(0.0, CHIPS_RESET_SECONDS - elapsed)

        embed = discord.Embed(
            title="🎟️ Suas fichas",
            description=f"Saldo atual: **{chips} fichas**",
            color=discord.Color(ON_COLOR),
        )
        embed.set_author(name=str(member.display_name), icon_url=member.display_avatar.url)
        if chips > 0:
            embed.add_field(name="Recarga", value=f"Automática para **{CHIPS_DEFAULT}** quando faltar saldo.", inline=False)
        else:
            if remaining > 0:
                embed.add_field(
                    name="Recarga",
                    value=f"Disponível em **{self._format_chip_reset_remaining(remaining)}** para voltar a **{CHIPS_DEFAULT} fichas**.",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Recarga",
                    value=f"Na próxima tentativa sem saldo, suas fichas voltam para **{CHIPS_DEFAULT}**.",
                    inline=False,
                )
        embed.set_footer(text="Roleta, buckshot e poker usam esse saldo neste servidor")
        return embed

    def _format_chip_reset_remaining(self, remaining_seconds: float) -> str:
        remaining = max(0, int(remaining_seconds))
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        if hours > 0:
            return f"{hours}h {minutes:02d}min"
        return f"{minutes}min"

    async def _try_consume_chips(self, guild_id: int, user_id: int, amount: int) -> tuple[bool, int, str | None]:
        current = self.db.get_user_chips(guild_id, user_id, default=CHIPS_DEFAULT)
        reset_note = None
        if current < amount:
            reset, new_balance, remaining = await self.db.maybe_reset_user_chips(
                guild_id, user_id, amount=CHIPS_DEFAULT, cooldown_seconds=CHIPS_RESET_SECONDS
            )
            if not reset:
                return False, current, f"Você não tem fichas suficientes. Sua recarga de **{CHIPS_DEFAULT} fichas** volta em **{self._format_chip_reset_remaining(remaining)}**."
            current = new_balance
            reset_note = f"Suas fichas foram recarregadas para **{CHIPS_DEFAULT}**."
        new_balance = await self.db.add_user_chips(guild_id, user_id, -int(amount))
        return True, new_balance, reset_note

    async def _ensure_action_chips(self, guild_id: int, user_id: int, amount: int) -> tuple[bool, int, str | None]:
        current = self.db.get_user_chips(guild_id, user_id, default=CHIPS_DEFAULT)
        if current >= amount:
            return True, current, None
        reset, new_balance, remaining = await self.db.maybe_reset_user_chips(
            guild_id, user_id, amount=CHIPS_DEFAULT, cooldown_seconds=CHIPS_RESET_SECONDS
        )
        if reset:
            return True, new_balance, f"Suas fichas foram recarregadas para **{CHIPS_DEFAULT}**."
        return False, current, f"Você não tem fichas suficientes. Sua recarga de **{CHIPS_DEFAULT} fichas** volta em **{self._format_chip_reset_remaining(remaining)}**."

    async def _reject_if_not_allowed_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            embed = self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False)
        elif GUILD_IDS and interaction.guild.id not in GUILD_IDS:
            embed = self._make_embed("Indisponível aqui", "Esse comando não está habilitado neste servidor", ok=False)
        else:
            return False

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)
        return True

    def _anti_mzk_only_kick_members(self, guild_id: int) -> bool:
        guild_cache = getattr(self.db, "guild_cache", {}) or {}
        guild_doc = guild_cache.get(guild_id, {}) or {}
        return bool(guild_doc.get("anti_mzk_only_kick_members", False))

    def _get_staff_role(self, guild: discord.Guild) -> discord.Role | None:
        role_id = 0
        try:
            role_id = max(0, int(self.db.get_anti_mzk_staff_role_id(guild.id) or 0))
        except Exception:
            role_id = 0
        return guild.get_role(role_id) if role_id else None

    def _is_staff_member(self, member: discord.Member) -> bool:
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and perms.kick_members:
            return True

        guild = member.guild
        staff_role = self._get_staff_role(guild)
        return staff_role is not None and staff_role in getattr(member, "roles", [])

    def _is_focused_non_staff_member(self, member: discord.Member) -> bool:
        guild = getattr(member, "guild", None)
        if guild is None or self._is_staff_member(member):
            return False
        focus_map = self.db.get_modo_censura_focus_map(guild.id)
        return bool(focus_map and member.id in focus_map)

    async def _set_anti_mzk_only_kick_members(self, guild_id: int, value: bool):
        if hasattr(self.db, "_get_guild_doc") and hasattr(self.db, "_save_guild_doc"):
            doc = self.db._get_guild_doc(guild_id)
            doc["anti_mzk_only_kick_members"] = bool(value)
            await self.db._save_guild_doc(guild_id, doc)
            return

        guild_cache = getattr(self.db, "guild_cache", None)
        coll = getattr(self.db, "coll", None)
        if guild_cache is not None:
            doc = guild_cache.get(guild_id, {"type": "guild", "guild_id": guild_id})
            doc["anti_mzk_only_kick_members"] = bool(value)
            guild_cache[guild_id] = doc
            if coll is not None:
                await coll.update_one(
                    {"type": "guild", "guild_id": guild_id},
                    {"$set": doc},
                    upsert=True,
                )

    def _iter_target_members(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> list[discord.Member]:
        targets: dict[int, discord.Member] = {}
        role_ids = set(self.db.get_anti_mzk_role_ids(guild.id))

        if not role_ids:
            return []

        for member in voice_channel.members:
            member_role_ids = {role.id for role in getattr(member, "roles", [])}
            if member_role_ids & role_ids:
                targets[member.id] = member

        return list(targets.values())

    def _iter_focused_members(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> list[discord.Member]:
        focus_map = self.db.get_modo_censura_focus_map(guild.id)
        if not focus_map:
            return []

        targets: dict[int, discord.Member] = {}
        for member in voice_channel.members:
            if member.id in focus_map:
                targets[member.id] = member
        return list(targets.values())

    def _resolve_targets(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> list[discord.Member]:
        focused = self._iter_focused_members(guild, voice_channel)
        if focused:
            return focused
        return self._iter_target_members(guild, voice_channel)

    async def _react_success_temporarily(self, message: discord.Message):
        try:
            await message.add_reaction("✅")
        except Exception:
            return

        async def _cleanup():
            await asyncio.sleep(3)
            try:
                await message.remove_reaction("✅", self.bot.user)
            except Exception:
                pass

        asyncio.create_task(_cleanup())
