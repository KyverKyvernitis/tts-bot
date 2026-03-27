import asyncio

import discord
from discord.ext import commands

from config import OFF_COLOR, ON_COLOR
from ..constants import CHIPS_DEFAULT, CHIPS_INITIAL, CHIPS_RECHARGE_THRESHOLD, CHIPS_RESET_HOURS, CHIPS_RESET_SECONDS, ROLETA_COST
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
        self._buckshot_last_used: dict[int, float] = {}
        self._poker_games: dict[int, object] = {}
        self._payment_sessions: dict[tuple[int, int], dict] = {}
        self._race_sessions: dict[int, dict] = {}

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

    def _chip_label(self) -> str:
        return f"{self._CHIP_EMOJI} Fichas"

    def _format_rate_decimal(self, value: float) -> str:
        return f"{round(float(value), 1):.1f}".replace('.', ',') + '%'

    def _chip_summary_stats(self, stats: dict) -> tuple[int, int, int, str]:
        wins = int(stats.get('poker_wins', 0)) + int(stats.get('alvo_wins', 0)) + int(stats.get('roleta_jackpots', 0)) + int(stats.get('corrida_wins', 0)) + int(stats.get('buckshot_survivals', 0))
        losses = int(stats.get('poker_losses', 0)) + int(stats.get('corrida_losses', 0)) + int(stats.get('buckshot_eliminations', 0))
        games = int(stats.get('games_played', 0))
        rate = self._format_rate_decimal((wins / games) * 100.0) if games > 0 else '0,0%'
        return wins, losses, games, rate

    def _has_meaningful_chip_profile(self, guild_id: int, user_id: int) -> bool:
        return bool(self.db.user_has_chip_activity(guild_id, user_id))

    async def _mark_chip_activity(self, guild_id: int, user_id: int):
        await self.db.mark_user_chip_activity(guild_id, user_id)

    async def _clear_chip_activity(self, guild_id: int, user_id: int):
        await self.db.set_user_chip_activity(guild_id, user_id, False)

    async def _set_user_chips_value(self, guild_id: int, user_id: int, chips: int, *, mark_activity: bool = True) -> int:
        await self.db.set_user_chips(guild_id, user_id, int(chips))
        if mark_activity:
            await self._mark_chip_activity(guild_id, user_id)
        return self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)

    async def _change_user_chips(self, guild_id: int, user_id: int, amount: int, *, mark_activity: bool = True) -> int:
        new_balance = await self.db.add_user_chips(guild_id, user_id, int(amount))
        if mark_activity and int(amount) != 0:
            await self._mark_chip_activity(guild_id, user_id)
        return int(new_balance)

    async def _transfer_user_chips(self, guild_id: int, payer_id: int, target_id: int, *, total: int, net_amount: int) -> tuple[int, int]:
        payer_balance = await self._change_user_chips(guild_id, payer_id, -int(total), mark_activity=True)
        target_balance = await self._change_user_chips(guild_id, target_id, int(net_amount), mark_activity=True)
        return payer_balance, target_balance

    async def _claim_daily_bonus_with_activity(self, guild_id: int, user_id: int, *, base_amount: int = 10) -> tuple[bool, int, int, int]:
        claimed, new_balance, bonus, streak = await self.db.claim_daily_bonus(guild_id, user_id, base_amount=base_amount)
        if claimed:
            await self._mark_chip_activity(guild_id, user_id)
        return claimed, new_balance, bonus, streak

    async def _force_reset_chips(self, guild_id: int, user_id: int, *, amount: int = CHIPS_DEFAULT) -> int:
        await self._set_user_chips_value(guild_id, user_id, int(amount), mark_activity=True)
        doc = self.db._get_user_doc(guild_id, user_id)
        doc["last_chip_reset_at"] = 0.0
        doc["chip_recharge_manual_initialized"] = False
        await self.db._save_user_doc(guild_id, user_id, doc)
        return int(amount)

    async def _force_full_reset_ficha_profile(self, guild_id: int, user_id: int, *, amount: int = CHIPS_DEFAULT) -> int:
        doc = self.db._get_user_doc(guild_id, user_id)
        doc["chips"] = max(0, int(amount))
        doc["last_chip_reset_at"] = 0.0
        doc["chip_recharge_manual_initialized"] = False
        doc["daily_last_claim_key"] = ""
        doc["daily_streak"] = 0
        doc["weekly_points_week"] = ""
        doc["weekly_points"] = 0
        doc["game_stats"] = {}
        doc["has_chip_activity"] = False
        await self.db._save_user_doc(guild_id, user_id, doc)
        return int(doc["chips"])

    def _iter_active_chip_user_ids(self, guild_id: int) -> list[int]:
        return list(self.db.get_chip_activity_user_ids(guild_id))

    def _achievement_catalog(self) -> list[dict]:
        return [
            {"key": "first_game", "name": "🎉 Primeiro sangue", "check": lambda chips, stats, weekly: stats.get("games_played", 0) >= 1},
            {"key": "sortudo", "name": "🎰 Sortudo", "check": lambda chips, stats, weekly: stats.get('roleta_jackpots', 0) >= 1},
            {"key": "na_mosca", "name": "🎯 Na mosca", "check": lambda chips, stats, weekly: stats.get('alvo_bullseyes', 0) >= 1},
            {"key": "sobrevivente", "name": "💥 Sobrevivente", "check": lambda chips, stats, weekly: stats.get('buckshot_survivals', 0) >= 1},
            {"key": "veterano", "name": "🧩 Veterano", "check": lambda chips, stats, weekly: stats.get("games_played", 0) >= 25},
            {"key": "rico", "name": "💰 Rico", "check": lambda chips, stats, weekly: chips >= 400},
            {"key": "rei_alvo", "name": "🏹 Rei do alvo", "check": lambda chips, stats, weekly: stats.get('alvo_wins', 0) >= 5},
            {"key": "mesa_quente", "name": "🃏 Mesa quente", "check": lambda chips, stats, weekly: stats.get('poker_wins', 0) >= 3},
            {"key": "teimoso", "name": "😤 Teimoso", "check": lambda chips, stats, weekly: (stats.get('poker_losses', 0) + stats.get('buckshot_eliminations', 0)) >= 10},
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
        await self._mark_chip_activity(guild_id, user_id)
        if weekly_points > 0:
            await self._grant_weekly_points(guild_id, user_id, weekly_points)

    def _daily_bonus_text(self, guild_id: int, user_id: int) -> str:
        status = self.db.get_user_daily_status(guild_id, user_id)
        streak = int(status.get("streak", 0) or 0)
        if status.get("available"):
            return f"Disponível agora em **_daily**. Streak atual: **{streak}**."
        return f"Já resgatado hoje. Streak atual: **{streak}**."

    def _best_game_summary(self, stats: dict) -> str:
        candidates = [
            ((int(stats.get('corrida_wins', 0)), int(stats.get('corrida_podiums', 0))), f"**Corrida** — {int(stats.get('corrida_wins', 0))} vitórias"),
            ((int(stats.get('alvo_wins', 0)), int(stats.get('alvo_bullseyes', 0))), f"**Alvo** — {int(stats.get('alvo_wins', 0))} vitórias"),
            ((int(stats.get('buckshot_survivals', 0)), -int(stats.get('buckshot_eliminations', 0))), f"**Buckshot** — {int(stats.get('buckshot_survivals', 0))} sobrevivências"),
            ((int(stats.get('poker_wins', 0)), -int(stats.get('poker_losses', 0))), f"**Poker** — {int(stats.get('poker_wins', 0))} vitórias"),
            ((int(stats.get('cartas_jackpots', 0)), 0), f"**Cartas** — {int(stats.get('cartas_jackpots', 0))} jackpots"),
            ((int(stats.get('roleta_jackpots', 0)), 0), f"**Roleta** — {int(stats.get('roleta_jackpots', 0))} jackpots"),
        ]
        best_score, best_text = max(candidates, key=lambda item: item[0])
        if best_score[0] <= 0:
            return "Ainda sem destaque"
        return best_text

    def _chip_recharge_state(self, guild_id: int, user_id: int) -> dict:
        import time

        chips = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        doc = getattr(self.db, "user_cache", {}).get((guild_id, user_id), {}) or {}
        initialized = bool(doc.get("chip_recharge_manual_initialized", False))
        last_reset = self.db.get_user_chip_reset_at(guild_id, user_id)
        now = time.time()
        if not initialized or last_reset <= 0:
            remaining = 0.0
        else:
            remaining = max(0.0, (float(last_reset) + float(CHIPS_RESET_SECONDS)) - now)
        below_threshold = chips < CHIPS_RECHARGE_THRESHOLD
        available = below_threshold and remaining <= 0.0
        return {
            "chips": int(chips),
            "remaining": float(remaining),
            "below_threshold": bool(below_threshold),
            "available": bool(available),
            "initialized": bool(initialized),
        }

    def _chip_recharge_text(self, guild_id: int, user_id: int) -> str:
        state = self._chip_recharge_state(guild_id, user_id)
        chips = int(state["chips"])
        remaining = float(state["remaining"])
        if chips >= CHIPS_RECHARGE_THRESHOLD:
            return (
                f"Use **recarga** quando seu saldo ficar abaixo de **{CHIPS_RECHARGE_THRESHOLD}** fichas. "
                f"A recarga restaura seu saldo para {self._chip_amount(CHIPS_DEFAULT)} e tem cooldown de **{CHIPS_RESET_HOURS} horas**."
            )
        if remaining > 0:
            return (
                f"Disponível em **{self._format_chip_reset_remaining(remaining)}** com o trigger **recarga**. "
                f"Seu saldo está abaixo de **{CHIPS_RECHARGE_THRESHOLD}** e a recarga volta para {self._chip_amount(CHIPS_DEFAULT)}."
            )
        return (
            f"Disponível agora em **recarga**. Seu saldo está abaixo de **{CHIPS_RECHARGE_THRESHOLD}** "
            f"e a recarga restaura para {self._chip_amount(CHIPS_DEFAULT)}."
        )

    async def _try_use_chip_recharge(self, guild_id: int, user_id: int) -> tuple[bool, int, str]:
        state = self._chip_recharge_state(guild_id, user_id)
        chips = int(state["chips"])
        remaining = float(state["remaining"])
        if chips >= CHIPS_RECHARGE_THRESHOLD:
            return False, chips, (
                f"A **recarga** só pode ser usada quando seu saldo estiver abaixo de **{CHIPS_RECHARGE_THRESHOLD}** fichas. "
                f"Saldo atual: {self._chip_amount(chips)}."
            )
        if remaining > 0:
            return False, chips, (
                f"Sua **recarga** volta em **{self._format_chip_reset_remaining(remaining)}**. "
                f"Quando liberar, ela restaura seu saldo para {self._chip_amount(CHIPS_DEFAULT)}."
            )
        await self._set_user_chips_value(guild_id, user_id, int(CHIPS_DEFAULT), mark_activity=True)
        doc = self.db._get_user_doc(guild_id, user_id)
        doc["last_chip_reset_at"] = float(__import__("time").time())
        doc["chip_recharge_manual_initialized"] = True
        await self.db._save_user_doc(guild_id, user_id, doc)
        return True, int(CHIPS_DEFAULT), (
            f"Seu saldo foi restaurado para {self._chip_amount(CHIPS_DEFAULT)} usando **recarga**."
        )


    def _make_chip_recharge_embed(self, used: bool, new_balance: int, note: str) -> discord.Embed:
        title = "🔋 Recarga concluída" if used else "🔋 Recarga indisponível"
        description = f"{note}\nSaldo atual: {self._chip_amount(new_balance)}"
        return self._make_embed(title, description, ok=used)

    def _insufficient_chips_text(self, guild_id: int, user_id: int, amount: int) -> str:
        state = self._chip_recharge_state(guild_id, user_id)
        chips = int(state["chips"])
        remaining = float(state["remaining"])
        amount = max(0, int(amount))
        if state["available"]:
            return (
                f"Você precisa de {self._chip_amount(amount)}, mas seu saldo atual é {self._chip_amount(chips)}. "
                f"Como ele está abaixo de **{CHIPS_RECHARGE_THRESHOLD}**, você já pode usar **recarga** para voltar a {self._chip_amount(CHIPS_DEFAULT)}."
            )
        if chips < CHIPS_RECHARGE_THRESHOLD:
            return (
                f"Você precisa de {self._chip_amount(amount)}, mas seu saldo atual é {self._chip_amount(chips)}. "
                f"Sua **recarga** volta em **{self._format_chip_reset_remaining(remaining)}** e restaura para {self._chip_amount(CHIPS_DEFAULT)}."
            )
        return (
            f"Você precisa de {self._chip_amount(amount)}, mas seu saldo atual é {self._chip_amount(chips)}. "
            f"A **recarga** só fica disponível quando seu saldo estiver abaixo de **{CHIPS_RECHARGE_THRESHOLD}** fichas."
        )

    def _make_chip_balance_embed(self, member: discord.Member) -> discord.Embed:
        guild_id = member.guild.id
        chips = self.db.get_user_chips(guild_id, member.id, default=CHIPS_INITIAL)
        stats = self.db.get_user_game_stats(guild_id, member.id)

        embed = discord.Embed(
            color=discord.Color.blurple(),
        )
        embed.set_author(name=str(member.display_name), icon_url=member.display_avatar.url)

        recarga = self._chip_recharge_text(guild_id, member.id)

        wins, losses, games, rate = self._chip_summary_stats(stats)
        weekly_points = self.db.get_user_weekly_points(guild_id, member.id)
        summary = (
            f"Vitórias: **{wins}**\n"
            f"Derrotas: **{losses}**\n"
            f"Jogos: **{games}**\n"
            f"Taxa de vitórias: **{rate}**"
        )

        embed.add_field(name=f"{self._CHIP_EMOJI} Fichas", value=f"**{chips}**", inline=False)
        embed.add_field(name="⏳ Recarga", value=recarga, inline=False)
        embed.add_field(name="🎁 Login diário", value=self._daily_bonus_text(guild_id, member.id), inline=False)
        embed.add_field(name="⭐ Weekly points", value=f"**{weekly_points}**", inline=False)
        embed.add_field(name="🎮 Melhor jogo", value=self._best_game_summary(stats), inline=False)
        embed.add_field(name="📊 Resumo", value=summary, inline=False)
        embed.set_footer(text="Use _rank para ver o ranking do servidor e _daily para pegar seu bônus")
        return embed

    def _make_chip_leaderboard_embed(self, guild: discord.Guild, requester: discord.Member | None = None) -> discord.Embed:
        rows = self.db.get_chip_leaderboard(guild.id, limit=10)
        embed = discord.Embed(
            title="🏆 Rank do servidor",
            description="Os maiores saldos deste servidor.",
            color=discord.Color.gold(),
        )
        if not rows:
            embed.add_field(name="Top 10", value="Ainda não há jogadores com movimentação nas fichas.", inline=False)
        else:
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            ranking_lines = []
            for index, row in enumerate(rows, start=1):
                member = guild.get_member(int(row["user_id"]))
                name = member.display_name if member is not None else f"Usuário {row['user_id']}"
                prefix = medals.get(index, f"`#{index}`")
                ranking_lines.append(f"{prefix} **{name}** — **{row.get('chips', row.get('points', 0))}** {self._CHIP_EMOJI}")
            embed.add_field(name="Top 10", value="\n".join(ranking_lines), inline=False)

        embed.set_footer(text="Use _ficha para ver seu perfil")
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
        if current < amount:
            return False, current, self._insufficient_chips_text(guild_id, user_id, amount)
        new_balance = await self._change_user_chips(guild_id, user_id, -int(amount), mark_activity=True)
        return True, new_balance, None

    async def _ensure_action_chips(self, guild_id: int, user_id: int, amount: int) -> tuple[bool, int, str | None]:
        current = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        if current >= amount:
            return True, current, None
        return False, current, self._insufficient_chips_text(guild_id, user_id, amount)

    async def _reject_if_not_allowed_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            embed = self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False)
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
