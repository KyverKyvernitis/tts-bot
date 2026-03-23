import asyncio

import discord
from discord.ext import commands

from config import GUILD_IDS, OFF_COLOR, ON_COLOR
from ..constants import CHIPS_DEFAULT, CHIPS_INITIAL, CHIPS_RESET_SECONDS, ROLETA_COST
from db import SettingsDB


class GincanaBase:
    _GINCANA_SUFFIXES = (" [ultra-censurado]", " [censurado]", " [antitts]")
    _CHIP_EMOJI = "<:emoji_63:1485041721573249135>"
    _CHIP_GAIN_EMOJI = "<:emoji_64:1485043651292827788>"
    _CHIP_LOSS_EMOJI = "<:emoji_65:1485043671077228786>"

    def __init__(self, bot: commands.Bot, db: SettingsDB):
        self.bot = bot
        self.db = db
        self._pica_expirations: dict[tuple[int, int], float] = {}
        self._dj_expirations: dict[tuple[int, int, int], float] = {}
        self._roleta_last_used: dict[int, float] = {}
        self._roleta_running_guilds: set[int] = set()
        self._buckshot_sessions: dict[int, dict] = {}
        self._target_sessions: dict[int, dict] = {}
        self._target_last_used: dict[int, float] = {}
        self._poker_games: dict[int, object] = {}
        self._payment_sessions: dict[tuple[int, int], dict] = {}

    def _strip_gincana_suffix(self, name: str) -> str:
        base = str(name or "").rstrip()
        lowered = base.casefold()
        for suffix in self._GINCANA_SUFFIXES:
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
        base_name = self._strip_gincana_suffix(current_name) or self._strip_gincana_suffix(current_display_name) or member.name
        suffix = self._target_suffix(member, ignored_tts_role)
        desired_full = f"{base_name}{suffix}".strip()

        current_nick_has_managed_suffix = bool(current_nick and self._strip_gincana_suffix(current_nick) != current_nick)

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
            await member.edit(nick=new_nick, reason="gincana atualizar sufixo do alvo")
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
        return discord.Embed(
            title=title,
            description=description,
            color=discord.Color(ON_COLOR) if ok else discord.Color(OFF_COLOR),
        )

    def _chip_text(self, amount: int | str, *, kind: str = "balance") -> str:
        emoji = self._CHIP_EMOJI
        if kind == "gain":
            emoji = self._CHIP_GAIN_EMOJI
        elif kind == "loss":
            emoji = self._CHIP_LOSS_EMOJI
        return f"**{amount} {emoji}**"

    def _chip_amount(self, amount: int | str) -> str:
        return f"**{amount} {self._CHIP_EMOJI}**"

    async def _force_reset_chips(self, guild_id: int, user_id: int, *, amount: int = CHIPS_DEFAULT) -> int:
        await self.db.set_user_chips(guild_id, user_id, int(amount))
        await self.db.set_user_chip_reset_at(guild_id, user_id, 0.0)
        return int(amount)

    def _achievement_catalog(self) -> list[dict]:
        return [
            {"key": "first_game", "name": "🎉 Primeiro sangue", "check": lambda chips, stats, weekly: stats.get("games_played", 0) >= 1},
            {"key": "sortudo", "name": "🎰 Sortudo", "check": lambda chips, stats, weekly: stats.get("roleta_jackpots", 0) >= 1},
            {"key": "na_mosca", "name": "🎯 Na mosca", "check": lambda chips, stats, weekly: stats.get("alvo_bullseyes", 0) >= 1},
            {"key": "sobrevivente", "name": "💥 Sobrevivente", "check": lambda chips, stats, weekly: stats.get("buckshot_survivals", 0) >= 1},
            {"key": "veterano", "name": "🧩 Veterano", "check": lambda chips, stats, weekly: stats.get("games_played", 0) >= 25},
            {"key": "rico", "name": "💰 Rico", "check": lambda chips, stats, weekly: chips >= 400},
            {"key": "rei_alvo", "name": "🏹 Rei do alvo", "check": lambda chips, stats, weekly: stats.get("alvo_wins", 0) >= 5},
            {"key": "mesa_quente", "name": "🃏 Mesa quente", "check": lambda chips, stats, weekly: stats.get("poker_wins", 0) >= 3},
            {"key": "teimoso", "name": "😤 Teimoso", "check": lambda chips, stats, weekly: (stats.get("poker_losses", 0) + stats.get("buckshot_eliminations", 0)) >= 10},
            {"key": "embalado", "name": "📈 Embalado", "check": lambda chips, stats, weekly: weekly >= 100},
        ]

    def _get_unlocked_achievements(self, guild_id: int, user_id: int) -> list[str]:
        chips = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        stats = self.db.get_user_game_stats(guild_id, user_id)
        weekly = self.db.get_user_weekly_points(guild_id, user_id)
        unlocked = []
        for item in self._achievement_catalog():
            try:
                if item["check"](chips, stats, weekly):
                    unlocked.append(str(item["name"]))
            except Exception:
                pass
        return unlocked

    async def _grant_weekly_points(self, guild_id: int, user_id: int, amount: int):
        if amount > 0:
            await self.db.add_user_weekly_points(guild_id, user_id, int(amount))

    async def _record_game_played(self, guild_id: int, user_id: int, *, weekly_points: int = 0):
        await self.db.add_user_game_stat(guild_id, user_id, "games_played", 1)
        if weekly_points > 0:
            await self._grant_weekly_points(guild_id, user_id, weekly_points)

    def _daily_bonus_text(self, guild_id: int, user_id: int) -> str:
        status = self.db.get_user_daily_status(guild_id, user_id)
        streak = int(status.get("streak", 0) or 0)
        if status.get("available"):
            return f"Disponível agora em **_daily**. Streak atual: **{streak}**."
        return f"Já resgatado hoje. Streak atual: **{streak}**."

    def _make_chip_balance_embed(self, member: discord.Member) -> discord.Embed:
        guild_id = member.guild.id
        chips = self.db.get_user_chips(guild_id, member.id, default=CHIPS_INITIAL)
        stats = self.db.get_user_game_stats(guild_id, member.id)
        weekly = self.db.get_user_weekly_points(guild_id, member.id)
        achievements = self._get_unlocked_achievements(guild_id, member.id)
        remaining = 0.0
        if chips <= 0:
            import time

            last_reset = self.db.get_user_chip_reset_at(guild_id, member.id)
            now = time.time()
            elapsed = max(0.0, now - float(last_reset or 0.0))
            remaining = max(0.0, CHIPS_RESET_SECONDS - elapsed)

        embed = discord.Embed(
            title=f"{self._CHIP_EMOJI} Perfil de fichas",
            description=(
                f"Saldo atual: {self._chip_amount(chips)}\n"
                f"Pontuação semanal: **{weekly}** 🏆\n"
                f"Conquistas desbloqueadas: **{len(achievements)}/{len(self._achievement_catalog())}**"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_author(name=str(member.display_name), icon_url=member.display_avatar.url)

        if chips > 0:
            recarga = f"Quando faltar saldo, a próxima recarga volta para {self._chip_amount(CHIPS_DEFAULT)}."
        elif remaining > 0:
            recarga = f"Disponível em **{self._format_chip_reset_remaining(remaining)}** para voltar a {self._chip_amount(CHIPS_DEFAULT)}."
        else:
            recarga = f"Na próxima tentativa sem saldo, seu saldo volta para {self._chip_amount(CHIPS_DEFAULT)}."

        partidas = int(stats.get("games_played", 0))
        total_wins = int(stats.get("poker_wins", 0)) + int(stats.get("alvo_wins", 0)) + int(stats.get("roleta_jackpots", 0))
        mira_hits = int(stats.get("alvo_hits", 0))
        mira_shots = int(stats.get("alvo_shots", 0))
        precision = f"{int((mira_hits / mira_shots) * 100)}%" if mira_shots > 0 else "0%"

        embed.add_field(name="⏳ Recarga", value=recarga, inline=False)
        embed.add_field(name="🎁 Login diário", value=self._daily_bonus_text(guild_id, member.id), inline=False)
        embed.add_field(
            name="📊 Resumo",
            value=f"Partidas: **{partidas}**\nVitórias marcantes: **{total_wins}**\nPrecisão no alvo: **{precision}**",
            inline=True,
        )
        embed.add_field(name="🃏 Poker", value=f"Vitórias: **{stats.get('poker_wins', 0)}**\nDerrotas: **{stats.get('poker_losses', 0)}**", inline=True)
        embed.add_field(
            name="<:gunforward:1484655577836683434> Buckshot",
            value=f"Sobreviveu: **{stats.get('buckshot_survivals', 0)}**\nEliminações: **{stats.get('buckshot_eliminations', 0)}**",
            inline=True,
        )
        embed.add_field(name="🎰 Roleta", value=f"Jackpots: **{stats.get('roleta_jackpots', 0)}**\nCusto por giro: {self._chip_amount(ROLETA_COST)}", inline=True)
        embed.add_field(name="🎯 Alvo", value=f"Vitórias: **{stats.get('alvo_wins', 0)}**\nBullseyes: **{stats.get('alvo_bullseyes', 0)}**", inline=True)
        embed.add_field(name="💸 Pagamentos", value=f"Enviados: **{stats.get('payments_sent', 0)}**\nRecebidos: **{stats.get('payments_received', 0)}**", inline=True)

        embed.set_footer(text="Use _rank para ver a disputa semanal e _daily para pegar seu bônus")
        return embed

    def _make_chip_leaderboard_embed(self, guild: discord.Guild, requester: discord.Member | None = None) -> discord.Embed:
        rows = self.db.get_weekly_points_leaderboard(guild.id, limit=10)
        embed = discord.Embed(
            title="🏆 Ranking semanal",
            description="Os maiores saldos desta semana.",
            color=discord.Color.gold(),
        )
        if requester is not None:
            embed.set_author(name=str(requester.display_name), icon_url=requester.display_avatar.url)

        if not rows:
            embed.add_field(name="Top 10", value="Ainda não há pontuação semanal registrada.", inline=False)
            embed.set_footer(text="Jogue roleta, buckshot, alvo ou poker para somar pontos")
            return embed

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        ranking_lines = []
        for index, row in enumerate(rows, start=1):
            member = guild.get_member(int(row["user_id"]))
            name = member.display_name if member is not None else f"Usuário {row['user_id']}"
            prefix = medals.get(index, f"`#{index}`")
            ranking_lines.append(f"{prefix} **{name}** — **{row['points']} pts**")
        embed.add_field(name="Top 10 da semana", value="\n".join(ranking_lines), inline=False)

        highlight_specs = [
            ("🃏 Rei do poker", "poker_wins"),
            ("<:gunforward:1484655577836683434> Sobrevivente", "buckshot_survivals"),
            ("🎰 Sortudo", "roleta_jackpots"),
            ("🎯 Melhor mira", "alvo_bullseyes"),
        ]
        highlight_lines = []
        for label, key in highlight_specs:
            leaders = self.db.get_game_stat_leaderboard(guild.id, key, limit=1)
            if not leaders:
                continue
            row = leaders[0]
            member = guild.get_member(int(row["user_id"]))
            name = member.display_name if member is not None else f"Usuário {row['user_id']}"
            highlight_lines.append(f"{label}: **{name}** — **{row['value']}**")
        if highlight_lines:
            embed.add_field(name="Destaques paralelos", value="\n".join(highlight_lines), inline=False)
        return embed

    def _format_chip_reset_remaining(self, remaining_seconds: float) -> str:
        remaining = max(0, int(remaining_seconds))
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        if hours > 0:
            return f"{hours}h {minutes:02d}min"
        return f"{minutes}min"

    async def _try_consume_chips(self, guild_id: int, user_id: int, amount: int) -> tuple[bool, int, str | None]:
        current = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        reset_note = None
        if current < amount:
            reset, new_balance, remaining = await self.db.maybe_reset_user_chips(
                guild_id, user_id, amount=CHIPS_DEFAULT, cooldown_seconds=CHIPS_RESET_SECONDS
            )
            if not reset:
                return False, current, f"Você não tem saldo suficiente. Sua recarga de {self._chip_text(CHIPS_DEFAULT)} volta em **{self._format_chip_reset_remaining(remaining)}**."
            current = new_balance
            reset_note = f"Seu saldo foi recarregado para {self._chip_text(CHIPS_DEFAULT)}."
        new_balance = await self.db.add_user_chips(guild_id, user_id, -int(amount))
        return True, new_balance, reset_note

    async def _ensure_action_chips(self, guild_id: int, user_id: int, amount: int) -> tuple[bool, int, str | None]:
        current = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        if current >= amount:
            return True, current, None
        reset, new_balance, remaining = await self.db.maybe_reset_user_chips(
            guild_id, user_id, amount=CHIPS_DEFAULT, cooldown_seconds=CHIPS_RESET_SECONDS
        )
        if reset:
            return True, new_balance, f"Seu saldo foi recarregado para {self._chip_text(CHIPS_DEFAULT)}."
        return False, current, f"Você não tem saldo suficiente. Sua recarga de {self._chip_text(CHIPS_DEFAULT)} volta em **{self._format_chip_reset_remaining(remaining)}**."

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

    def _gincana_only_kick_members(self, guild_id: int) -> bool:
        guild_cache = getattr(self.db, "guild_cache", {}) or {}
        guild_doc = guild_cache.get(guild_id, {}) or {}
        return bool(guild_doc.get("gincana_only_kick_members", guild_doc.get("anti_mzk_only_kick_members", False)))

    def _get_staff_role(self, guild: discord.Guild) -> discord.Role | None:
        role_id = 0
        try:
            role_id = max(0, int(self.db.get_gincana_staff_role_id(guild.id) or 0))
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
        focus_map = self.db.get_gincana_focus_map(guild.id)
        return bool(focus_map and member.id in focus_map)

    async def _set_gincana_only_kick_members(self, guild_id: int, value: bool):
        if hasattr(self.db, "_get_guild_doc") and hasattr(self.db, "_save_guild_doc"):
            doc = self.db._get_guild_doc(guild_id)
            doc["gincana_only_kick_members"] = bool(value)
            doc["anti_mzk_only_kick_members"] = bool(value)
            await self.db._save_guild_doc(guild_id, doc)
            return

        guild_cache = getattr(self.db, "guild_cache", None)
        coll = getattr(self.db, "coll", None)
        if guild_cache is not None:
            doc = guild_cache.get(guild_id, {"type": "guild", "guild_id": guild_id})
            doc["gincana_only_kick_members"] = bool(value)
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
        role_ids = set(self.db.get_gincana_role_ids(guild.id))

        if not role_ids:
            return []

        for member in voice_channel.members:
            member_role_ids = {role.id for role in getattr(member, "roles", [])}
            if member_role_ids & role_ids:
                targets[member.id] = member

        return list(targets.values())

    def _iter_focused_members(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> list[discord.Member]:
        focus_map = self.db.get_gincana_focus_map(guild.id)
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
