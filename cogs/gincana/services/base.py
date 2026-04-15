import asyncio
import random
import time

import discord
from discord.ext import commands

from config import OFF_COLOR, ON_COLOR
from ..constants import (
    CHIPS_DEFAULT,
    CHIPS_INITIAL,
    CHIPS_RECHARGE_THRESHOLD,
    CHIPS_RESET_HOURS,
    CHIPS_RESET_SECONDS,
    CHIPS_SEASON_RESET_HOURS,
    CHIPS_SEASON_RESET_SECONDS,
    CHIPS_SEASON_RESET_THRESHOLD,
    CHIPS_MENDIGAR_COOLDOWN_SECONDS,
    RACE_REROLL_COST,
    RACE_SPECIAL_DEFAULT_CHANCE,
    RACE_SPECIAL_SORTUDO_CHANCE,
    ROLETA_APOSTADOR_COST,
    ROLETA_APOSTADOR_STANDARD_JACKPOT_CHIPS,
    ROLETA_APOSTADOR_MEGA_JACKPOT_CHIPS,
    ROLETA_COST,
    TRUCO_GOLDEN_BONUS_EXTRA,
)
from db import SettingsDB


class _NegativeDebtConfirmView(discord.ui.View):
    def __init__(self, *, owner_id: int, timeout: float = 20.0):
        super().__init__(timeout=timeout)
        self.owner_id = int(owner_id)
        self.confirmed = False
        self.message = None

    @discord.ui.button(label="Continuar", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if int(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("Essa confirmação não é para você.", ephemeral=True)
            return
        self.confirmed = True
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.defer()
        except Exception:
            pass
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except Exception:
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                pass
        self.stop()

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if int(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("Essa confirmação não é para você.", ephemeral=True)
            return
        self.confirmed = False
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.defer()
        except Exception:
            pass
        try:
            if self.message is not None:
                await self.message.edit(content="Entrada cancelada.", view=None)
            else:
                await interaction.edit_original_response(content="Entrada cancelada.", view=None)
        except Exception:
            try:
                await interaction.edit_original_response(content="Entrada cancelada.", view=None)
            except Exception:
                pass
        self.stop()

    async def on_timeout(self):
        try:
            for child in self.children:
                child.disabled = True
            if self.message is not None:
                await self.message.edit(view=None)
        except Exception:
            pass


class GincanaBase:
    _GINCANA_SUFFIXES = (" [ultra-censurado]", " [censurado]", " [antitts]")
    _CHIP_EMOJI = "<:emoji_63:1485041721573249135>"
    _CHIP_GAIN_EMOJI = "<:emoji_64:1485043651292827788>"
    _CHIP_LOSS_EMOJI = "<:emoji_65:1485043671077228786>"
    _CHIP_BONUS_EMOJI = "<:laranja:1487076933819830443>"
    _MAX_CHIP_DEBT = 100

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
        self._race_panel_messages: dict[tuple[int, int], tuple[int, int]] = {}
        self._truco_games: dict[int, object] = {}

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


    def _make_v2_notice(self, title: str, lines: list[str], *, ok: bool = True, accent_color: discord.Color | None = None) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        color = accent_color or (discord.Color(ON_COLOR) if ok else discord.Color(OFF_COLOR))
        body = [f"# {title}"]
        body.extend([str(x) for x in lines if str(x).strip()])
        view.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(body)), accent_color=color))
        return view

    def _chip_text(self, amount: int | str, *, kind: str = "balance") -> str:
        emoji = self._CHIP_EMOJI
        if kind == "gain":
            emoji = self._CHIP_GAIN_EMOJI
        elif kind == "loss":
            emoji = self._CHIP_LOSS_EMOJI
        return f"**{amount} {emoji}**"

    def _chip_amount(self, amount: int | str) -> str:
        return f"**{amount} {self._CHIP_EMOJI}**"

    def _bonus_chip_amount(self, amount: int | str) -> str:
        return f"**{amount} {self._CHIP_BONUS_EMOJI}**"

    def _chip_label(self) -> str:
        return f"{self._CHIP_EMOJI} Fichas"

    def _format_rate_decimal(self, value: float) -> str:
        return f"{round(float(value), 1):.1f}".replace('.', ',') + '%'

    def _chip_summary_stats(self, stats: dict) -> tuple[int, int, int, str]:
        wins = (
            int(stats.get('poker_wins', 0) or 0)
            + int(stats.get('alvo_wins', 0) or 0)
            + int(stats.get('corrida_wins', 0) or 0)
            + int(stats.get('buckshot_survivals', 0) or 0)
            + int(stats.get('truco_wins', 0) or 0)
        )
        losses = (
            int(stats.get('poker_losses', 0) or 0)
            + int(stats.get('corrida_losses', 0) or 0)
            + int(stats.get('buckshot_eliminations', 0) or 0)
            + int(stats.get('truco_losses', 0) or 0)
        )
        games = wins + losses
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

    def _get_user_bonus_chips(self, guild_id: int, user_id: int) -> int:
        try:
            return max(0, int(self.db.get_user_bonus_chips(guild_id, user_id) or 0))
        except Exception:
            return 0

    async def _change_user_bonus_chips(self, guild_id: int, user_id: int, amount: int, *, mark_activity: bool = True) -> int:
        new_bonus = await self.db.add_user_bonus_chips(guild_id, user_id, int(amount))
        if mark_activity and int(amount) != 0:
            await self._mark_chip_activity(guild_id, user_id)
        return int(new_bonus)

    async def _change_user_chips(self, guild_id: int, user_id: int, amount: int, *, mark_activity: bool = True) -> int:
        new_balance = await self.db.add_user_chips(guild_id, user_id, int(amount))
        if mark_activity and int(amount) != 0:
            await self._mark_chip_activity(guild_id, user_id)
        return int(new_balance)

    async def _transfer_user_chips(self, guild_id: int, payer_id: int, target_id: int, *, total: int, net_amount: int) -> tuple[int, int]:
        payer_balance = await self._change_user_chips(guild_id, payer_id, -int(total), mark_activity=True)
        target_balance = await self._change_user_chips(guild_id, target_id, int(net_amount), mark_activity=True)
        return payer_balance, target_balance

    async def _claim_daily_bonus_with_activity(self, guild_id: int, user_id: int, *, base_amount: int = 10) -> tuple[bool, int, int, int, int]:
        claimed, new_balance, bonus, streak = await self.db.claim_daily_bonus(guild_id, user_id, base_amount=base_amount)
        bonus_chips = self._get_user_bonus_chips(guild_id, user_id)
        if claimed:
            await self._mark_chip_activity(guild_id, user_id)
        return claimed, new_balance, bonus, 10, streak

    async def _force_reset_chips(self, guild_id: int, user_id: int, *, amount: int = CHIPS_DEFAULT) -> int:
        await self._set_user_chips_value(guild_id, user_id, int(amount), mark_activity=True)
        await self.db.set_user_bonus_chips(guild_id, user_id, 0)
        doc = self.db._get_user_doc(guild_id, user_id)
        doc["last_chip_reset_at"] = 0.0
        doc["chip_recharge_manual_initialized"] = False
        doc.pop("race_key", None)
        doc.pop("race_active", None)
        for field in ("race_free_roleta_spins", "race_free_carta_spins", "race_robbery_window_started_at", "race_robbery_uses", "race_mendigar_window_started_at", "race_mendigar_uses"):
            doc.pop(field, None)
        await self.db._save_user_doc(guild_id, user_id, doc)
        return int(amount)

    async def _force_full_reset_ficha_profile(self, guild_id: int, user_id: int, *, amount: int = CHIPS_DEFAULT) -> int:
        doc = self.db._get_user_doc(guild_id, user_id)
        doc["chips"] = int(amount)
        doc["bonus_chips"] = 0
        doc["last_chip_reset_at"] = 0.0
        doc["chip_recharge_manual_initialized"] = False
        doc["daily_last_claim_key"] = ""
        doc["daily_streak"] = 0
        doc["weekly_points_week"] = ""
        doc["weekly_points"] = 0
        doc["game_stats"] = {}
        doc["has_chip_activity"] = False
        doc.pop("race_key", None)
        doc.pop("race_active", None)
        for field in ("race_free_roleta_spins", "race_free_carta_spins", "race_robbery_window_started_at", "race_robbery_uses", "race_mendigar_window_started_at", "race_mendigar_uses"):
            doc.pop(field, None)
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
        bonus = self._get_user_bonus_chips(guild_id, user_id)
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
            return f"Use **_daily** para pegar seu bônus • Streak: **{streak}**"
        return f"Já resgatado hoje • Streak: **{streak}**"

    def _best_game_summary(self, stats: dict) -> str | None:
        roleta_wins = int(stats.get('roleta_jackpots', 0) or 0) + int(stats.get('cartas_jackpots', 0) or 0)
        candidates = [
            ((int(stats.get('truco_wins', 0) or 0), -int(stats.get('truco_losses', 0) or 0)), f"**Truco** — **{int(stats.get('truco_wins', 0) or 0)}** vitórias"),
            ((int(stats.get('corrida_wins', 0) or 0), -int(stats.get('corrida_losses', 0) or 0)), f"**Corrida** — **{int(stats.get('corrida_wins', 0) or 0)}** vitórias"),
            ((int(stats.get('alvo_wins', 0) or 0), -max(0, int(stats.get('alvo_games', 0) or 0) - int(stats.get('alvo_wins', 0) or 0))), f"**Alvo** — **{int(stats.get('alvo_wins', 0) or 0)}** vitórias"),
            ((int(stats.get('poker_wins', 0) or 0), -int(stats.get('poker_losses', 0) or 0)), f"**Poker** — **{int(stats.get('poker_wins', 0) or 0)}** vitórias"),
            ((int(stats.get('buckshot_survivals', 0) or 0), -int(stats.get('buckshot_eliminations', 0) or 0)), f"**Buckshot** — **{int(stats.get('buckshot_survivals', 0) or 0)}** vitórias"),
            ((roleta_wins, 0), f"**Roleta** — **{roleta_wins}** vitórias"),
        ]
        best_score, best_text = max(candidates, key=lambda item: item[0])
        if best_score[0] <= 0:
            return None
        return best_text

    def _build_chip_game_stat_lines(self, stats: dict) -> list[str]:
        lines: list[str] = []

        buckshot_total = int(stats.get('buckshot_survivals', 0) or 0) + int(stats.get('buckshot_eliminations', 0) or 0)
        buckshot_deaths = int(stats.get('buckshot_eliminations', 0) or 0)
        if buckshot_total > 0:
            line = f"<:propergun:1485855162198396959> **Buckshots**: **{buckshot_total}**"
            if buckshot_deaths > 0:
                line += f" (Morreu: **{buckshot_deaths}×**)"
            lines.append(line)

        truco_wins = int(stats.get('truco_wins', 0) or 0)
        truco_losses = int(stats.get('truco_losses', 0) or 0)
        truco_games = int(stats.get('truco_games', 0) or 0)
        if truco_games <= 0:
            truco_games = truco_wins + truco_losses
        if truco_games > 0:
            line = f"🃏 **Jogos de truco**: **{truco_games}**"
            parts: list[str] = []
            if truco_wins > 0:
                parts.append(f"Vitórias: **{truco_wins}**")
            if truco_losses > 0:
                parts.append(f"Derrotas: **{truco_losses}**")
            if parts:
                line += f" - {' • '.join(parts)}"
            lines.append(line)

        roleta_spins = int(stats.get('roleta_spins', 0) or 0) + int(stats.get('carta_spins', 0) or 0)
        roleta_jackpots = int(stats.get('roleta_jackpots', 0) or 0) + int(stats.get('cartas_jackpots', 0) or 0)
        if roleta_spins <= 0 and roleta_jackpots > 0:
            roleta_spins = roleta_jackpots
        if roleta_spins > 0 or roleta_jackpots > 0:
            parts = []
            if roleta_spins > 0:
                parts.append(f"🎰 **Giros**: **{roleta_spins}**")
            if roleta_jackpots > 0:
                parts.append(f"Jackpots: **{roleta_jackpots}**")
            if parts:
                lines.append(" • ".join(parts))

        corrida_wins = int(stats.get('corrida_wins', 0) or 0)
        corrida_losses = int(stats.get('corrida_losses', 0) or 0)
        corrida_games = corrida_wins + corrida_losses
        corrida_podiums = int(stats.get('corrida_podiums', 0) or 0)
        if corrida_games > 0 or corrida_podiums > 0:
            line = f"🏇 **Corridas**: **{corrida_games if corrida_games > 0 else corrida_podiums}**"
            parts = []
            if corrida_wins > 0:
                parts.append(f"Vitórias: **{corrida_wins}**")
            if corrida_podiums > 0:
                parts.append(f"Pódios: **{corrida_podiums}**")
            if parts:
                line += f" - {' • '.join(parts)}"
            lines.append(line)

        alvo_games = int(stats.get('alvo_games', 0) or 0)
        alvo_wins = int(stats.get('alvo_wins', 0) or 0)
        alvo_bullseyes = int(stats.get('alvo_bullseyes', 0) or 0)
        if alvo_games > 0 or alvo_wins > 0 or alvo_bullseyes > 0:
            left_total = alvo_games if alvo_games > 0 else alvo_wins
            line = f"🎯 **Alvos**: **{left_total}**"
            parts = []
            if alvo_wins > 0:
                parts.append(f"Vitórias: **{alvo_wins}**")
            if alvo_bullseyes > 0:
                parts.append(f"Bullseyes: **{alvo_bullseyes}**")
            if parts:
                line += f" - {' • '.join(parts)}"
            lines.append(line)

        poker_games = int(stats.get('poker_rounds', 0) or 0)
        poker_wins = int(stats.get('poker_wins', 0) or 0)
        poker_losses = int(stats.get('poker_losses', 0) or 0)
        if poker_games > 0 or poker_wins > 0 or poker_losses > 0:
            left_total = poker_games if poker_games > 0 else (poker_wins + poker_losses)
            line = f"🂡 **Pokers**: **{left_total}**"
            parts = []
            if poker_wins > 0:
                parts.append(f"Vitórias: **{poker_wins}**")
            if poker_losses > 0:
                parts.append(f"Derrotas: **{poker_losses}**")
            if parts:
                line += f" - {' • '.join(parts)}"
            lines.append(line)

        return lines

    def _chip_recharge_state(self, guild_id: int, user_id: int) -> dict:
        import time

        chips = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        bonus = self._get_user_bonus_chips(guild_id, user_id)
        doc = getattr(self.db, "user_cache", {}).get((guild_id, user_id), {}) or {}
        initialized = bool(doc.get("chip_recharge_manual_initialized", False))
        last_reset = self.db.get_user_chip_reset_at(guild_id, user_id)
        now = time.time()
        if not initialized or last_reset <= 0:
            remaining = 0.0
        else:
            remaining = max(0.0, (float(last_reset) + float(CHIPS_RESET_SECONDS)) - now)
        below_threshold = (chips + bonus) < CHIPS_RECHARGE_THRESHOLD
        available = below_threshold and remaining <= 0.0
        return {
            "chips": int(chips),
            "bonus": int(bonus),
            "remaining": float(remaining),
            "below_threshold": bool(below_threshold),
            "available": bool(available),
            "initialized": bool(initialized),
        }

    def _chip_recharge_text(self, guild_id: int, user_id: int) -> str:
        state = self._chip_recharge_state(guild_id, user_id)
        chips = int(state["chips"])
        remaining = float(state["remaining"])
        total = chips + int(state.get("bonus", 0) or 0)
        if total >= CHIPS_RECHARGE_THRESHOLD:
            return (
                f"Use **recarga** quando seu saldo total ficar abaixo de **{CHIPS_RECHARGE_THRESHOLD}**. "
                f"Ela entrega {self._bonus_chip_amount(CHIPS_DEFAULT)} em fichas bônus e tem cooldown de **{CHIPS_RESET_HOURS} horas**."
            )
        if remaining > 0:
            return (
                f"Disponível em **{self._format_chip_reset_remaining(remaining)}** com o trigger **recarga**. "
                f"Seu saldo total já está abaixo de **{CHIPS_RECHARGE_THRESHOLD}** e ela vai entregar {self._bonus_chip_amount(CHIPS_DEFAULT)} em fichas bônus."
            )
        return (
            f"Disponível agora em **recarga**. Seu saldo total está abaixo de **{CHIPS_RECHARGE_THRESHOLD}** "
            f"e ela entrega {self._bonus_chip_amount(CHIPS_DEFAULT)} em fichas bônus."
        )

    def _chip_recharge_compact_text(self, guild_id: int, user_id: int) -> str:
        state = self._chip_recharge_state(guild_id, user_id)
        remaining = float(state["remaining"])
        total = int(state["chips"]) + int(state.get("bonus", 0) or 0)
        if total >= CHIPS_RECHARGE_THRESHOLD:
            return f"Use **_recarga** quando ficar abaixo de **{CHIPS_RECHARGE_THRESHOLD}** • +{CHIPS_DEFAULT} bônus"
        if remaining > 0:
            return f"Volta em **{self._format_chip_reset_remaining(remaining)}** • +{CHIPS_DEFAULT} bônus"
        return f"Use **_recarga** agora • +{CHIPS_DEFAULT} bônus"

    async def _try_use_chip_recharge(self, guild_id: int, user_id: int) -> tuple[bool, int, str]:
        state = self._chip_recharge_state(guild_id, user_id)
        chips = int(state["chips"])
        remaining = float(state["remaining"])
        total = chips + int(state.get("bonus", 0) or 0)
        if total >= CHIPS_RECHARGE_THRESHOLD:
            return False, chips, (
                f"Use **_recarga** apenas abaixo de **{CHIPS_RECHARGE_THRESHOLD}**."
            )
        if remaining > 0:
            return False, chips, (
                f"Sua recarga volta em **{self._format_chip_reset_remaining(remaining)}**."
            )
        await self._change_user_bonus_chips(guild_id, user_id, int(CHIPS_DEFAULT), mark_activity=True)
        doc = self.db._get_user_doc(guild_id, user_id)
        doc["last_chip_reset_at"] = float(__import__("time").time())
        doc["chip_recharge_manual_initialized"] = True
        await self.db._save_user_doc(guild_id, user_id, doc)
        return True, self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL), (
            f"Você recebeu **{CHIPS_DEFAULT}** {self._CHIP_BONUS_EMOJI}."
        )


    def _make_chip_recharge_view(self, guild_id: int, user_id: int, used: bool, new_balance: int, note: str) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        if used:
            lines = [
                "# 🔋 Recarga usada",
                note,
                f"Novo saldo: {self._format_primary_chip_balance(guild_id, user_id)}",
            ]
            color = discord.Color.dark_green()
        else:
            lines = ["# 🔋 Recarga indisponível", note]
            state = self._chip_recharge_state(guild_id, user_id)
            total = int(state["chips"]) + int(state.get("bonus", 0) or 0)
            if total >= CHIPS_RECHARGE_THRESHOLD:
                lines.append(f"Saldo atual: {self._format_compact_chip_balance(guild_id, user_id)}")
            else:
                remaining = float(state["remaining"])
                lines.append(f"Saldo atual: {self._format_compact_chip_balance(guild_id, user_id)}")
                if remaining > 0:
                    lines.append(f"Volta em: **{self._format_chip_reset_remaining(remaining)}**")
            color = discord.Color.red()
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            accent_color=color,
        ))
        return view

    def _negative_cost_projection(self, guild_id: int, user_id: int, amount: int) -> dict:
        chips = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        bonus = self._get_user_bonus_chips(guild_id, user_id)
        projected_chips, projected_bonus = self._project_chip_state_after_cost(guild_id, user_id, amount)
        return {
            "chips": int(chips),
            "bonus": int(bonus),
            "projected_chips": int(projected_chips),
            "projected_bonus": int(projected_bonus),
        }

    def _negative_transition_note(self, guild_id: int, user_id: int, amount: int) -> str | None:
        state = self._negative_cost_projection(guild_id, user_id, amount)
        chips = int(state["chips"])
        bonus = int(state["bonus"])
        projected_chips = int(state["projected_chips"])
        if projected_chips >= 0:
            return None
        first_negative = chips >= 0 and projected_chips < 0
        debt_increases = chips < 0 and projected_chips < chips
        if first_negative:
            return (
                f"Se continuar, você vai ser negativado. "
                f"Você vai ficar com **{projected_chips}** {self._CHIP_LOSS_EMOJI}."
            )
        if debt_increases:
            if bonus <= 0:
                return (
                    f"Você já está negativado e não tem fichas bônus. "
                    f"Se continuar, sua dívida vai para **{projected_chips}** {self._CHIP_LOSS_EMOJI}."
                )
            return (
                f"As fichas bônus não cobrem toda essa aposta. "
                f"Sua dívida vai para **{projected_chips}** {self._CHIP_LOSS_EMOJI}."
            )
        return None

    def _needs_negative_confirmation(self, guild_id: int, user_id: int, amount: int) -> bool:
        state = self._negative_cost_projection(guild_id, user_id, amount)
        chips = int(state["chips"])
        bonus = int(state["bonus"])
        projected_chips = int(state["projected_chips"])
        projected_bonus = int(state["projected_bonus"])
        if projected_bonus > 0:
            return False
        first_negative = chips >= 0 and projected_chips < 0
        debt_increases = chips < 0 and projected_chips < chips
        return bonus <= 0 and (first_negative or debt_increases)

    async def _confirm_negative_via_message(self, channel: discord.abc.Messageable, *, user_id: int, title: str, note: str) -> bool:
        view = _NegativeDebtConfirmView(owner_id=user_id)
        embed = self._make_embed(title, note, ok=False)
        sent = None
        try:
            sent = await channel.send(embed=embed, view=view)
            view.message = sent
            await view.wait()
            return bool(view.confirmed)
        finally:
            if sent is not None:
                try:
                    await sent.delete()
                except Exception:
                    pass

    async def _confirm_negative_ephemeral(self, interaction: discord.Interaction, guild_id: int, user_id: int, amount: int, *, title: str = "⚠️ Confirmar entrada") -> bool:
        note = self._negative_transition_note(guild_id, user_id, amount)
        if not note:
            return True
        view = _NegativeDebtConfirmView(owner_id=user_id)
        embed = self._make_embed(title, note, ok=False)
        sent = None
        try:
            if interaction.response.is_done():
                sent = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)
            else:
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
                try:
                    sent = await interaction.original_response()
                except Exception:
                    sent = None
            view.message = sent
            await view.wait()
            return bool(view.confirmed)
        except Exception:
            channel = getattr(interaction, "channel", None)
            if channel is None:
                return False
            return await self._confirm_negative_via_message(channel, user_id=user_id, title=title, note=note)

    async def _confirm_negative_from_message(self, message: discord.Message, guild_id: int, user_id: int, amount: int, *, title: str = "⚠️ Confirmar entrada") -> bool:
        note = self._negative_transition_note(guild_id, user_id, amount)
        if not note:
            return True
        return await self._confirm_negative_via_message(message.channel, user_id=user_id, title=title, note=note)

    def _insufficient_chips_text(self, guild_id: int, user_id: int, amount: int) -> str:
        state = self._negative_cost_projection(guild_id, user_id, amount)
        chips = int(state["chips"])
        bonus = int(state["bonus"])
        projected_chips = int(state["projected_chips"])
        note = self._negative_transition_note(guild_id, user_id, amount)
        if projected_chips >= -self._MAX_CHIP_DEBT and note:
            return note
        state = self._chip_recharge_state(guild_id, user_id)
        remaining = float(state["remaining"])
        total = chips + bonus
        if state["available"]:
            return (
                f"Você precisa de {self._chip_amount(amount)}, mas seu saldo atual é {self._format_compact_chip_balance(guild_id, user_id)}. "
                f"Como ele está abaixo de **{CHIPS_RECHARGE_THRESHOLD}**, você já pode usar **recarga** para receber {self._bonus_chip_amount(CHIPS_DEFAULT)} em fichas bônus."
            )
        if total < CHIPS_RECHARGE_THRESHOLD:
            return (
                f"Você precisa de {self._chip_amount(amount)}, mas seu saldo atual é {self._format_compact_chip_balance(guild_id, user_id)}. "
                f"Sua **recarga** volta em **{self._format_chip_reset_remaining(remaining)}** e entrega {self._bonus_chip_amount(CHIPS_DEFAULT)} em fichas bônus."
            )
        return (
            f"Você precisa de {self._chip_amount(amount)}, mas seu saldo atual é {self._format_compact_chip_balance(guild_id, user_id)}."
        )


    def _format_primary_chip_balance(self, guild_id: int, user_id: int) -> str:
        chips = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        bonus = self._get_user_bonus_chips(guild_id, user_id)
        if chips < 0:
            primary = f"**{chips}** {self._CHIP_LOSS_EMOJI}"
        else:
            primary = f"**{chips}** {self._CHIP_EMOJI}"
        if bonus > 0:
            primary += f" • **{bonus}** {self._CHIP_BONUS_EMOJI}"
        return primary

    def _format_compact_chip_balance(self, guild_id: int, user_id: int) -> str:
        return self._format_primary_chip_balance(guild_id, user_id)

    def _chip_spend_breakdown_text(self, guild_id: int, user_id: int, amount: int) -> str:
        spend = max(0, int(amount))
        bonus = self._get_user_bonus_chips(guild_id, user_id)
        use_bonus = min(bonus, spend)
        use_normal = spend - use_bonus
        if use_bonus > 0 and use_normal > 0:
            return f"Você entrou usando {self._bonus_chip_amount(use_bonus)} e {self._chip_amount(use_normal)}."
        if use_bonus > 0:
            return f"Você entrou usando {self._bonus_chip_amount(use_bonus)}."
        return f"Você entrou usando {self._chip_amount(use_normal)}."

    def _entry_consume_text(self, guild_id: int, user_id: int, amount: int) -> str:
        spend_text = self._chip_spend_breakdown_text(guild_id, user_id, amount)
        note = self._negative_transition_note(guild_id, user_id, amount)
        if note:
            return f"{spend_text}\n{note}"
        return spend_text

    def _project_chip_state_after_cost(self, guild_id: int, user_id: int, amount: int) -> tuple[int,int]:
        chips = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        bonus = self._get_user_bonus_chips(guild_id, user_id)
        spend = max(0, int(amount))
        use_bonus = min(bonus, spend)
        remaining = spend - use_bonus
        return chips - remaining, bonus - use_bonus

    def _user_has_played_any_game(self, guild_id: int, user_id: int) -> bool:
        stats = self.db.get_user_game_stats(guild_id, user_id)
        try:
            return int(stats.get("games_played", 0) or 0) > 0
        except Exception:
            return False

    def _format_percent_text(self, value: float) -> str:
        try:
            number = float(value) * 100.0 if float(value) <= 1.0 else float(value)
        except Exception:
            number = 0.0
        text = f"{number:.2f}".rstrip("0").rstrip(".")
        return text.replace(".", ",") + "%"

    def _race_catalog(self) -> dict[str, dict[str, object]]:
        return {
            "preto": {
                "name": "Preto",
                "emoji": "🥷🏿",
                "effects": [
                    {"key": "mao_negra", "title": "Mão Negra", "desc": "Você pode roubar **2** vezes a cada **4h**"},
                    {"key": "labia", "title": "Lábia", "desc": "Você pode mendigar **2** vezes a cada **3h**"},
                    {"key": "sangue_frio", "title": "Sangue Frio", "desc": f"Ao falhar em roubar: **5** {self._CHIP_LOSS_EMOJI}"},
                    {"key": "mao_grande", "title": "Mão Grande", "desc": f"Roubo máximo: **40** {self._CHIP_EMOJI}"},
                ],
            },
            "apostador": {
                "name": "Apostador",
                "emoji": "🎰",
                "effects": [
                    {"key": "jackpot", "title": "999 Jackpot", "desc": f"**{self._format_percent_text(0.15)}** • **100** {self._CHIP_GAIN_EMOJI}"},
                    {"key": "all_in", "title": "777 All-in", "desc": f"**{self._format_percent_text(0.05)}** • **200** {self._CHIP_GAIN_EMOJI}"},
                    {"key": "666", "title": "666 Besta", "desc": f"**{self._format_percent_text(0.25)}** • O custo da roleta é estornado"},
                    {"key": "mesa_alta", "title": "Mesa Alta", "desc": f"Entrada da roleta agora é de **25** {self._CHIP_LOSS_EMOJI}"},
                ],
            },
            "sortudo": {
                "name": "Sortudo",
                "emoji": "🍀",
                "effects": [
                    {"key": "midas", "title": "Midas", "desc": f"Buckshot dourado: **{self._format_percent_text(RACE_SPECIAL_SORTUDO_CHANCE)}** • Truco dourado: **{self._format_percent_text(RACE_SPECIAL_SORTUDO_CHANCE)}**"},
                    {"key": "premio_extra", "title": "Prêmio Extra", "desc": f"Truco dourado: **+20** {self._CHIP_BONUS_EMOJI}"},
                    {"key": "daily", "title": "Benção", "desc": "Todo daily libera **+1** giro grátis de roleta e **+1** de cartas"},
                ],
            },
            "coringa": {
                "name": "Coringa",
                "emoji": "🃏",
                "effects": [
                    {"key": "as", "title": "Às", "desc": f"Na roleta: **{self._format_percent_text(0.35)}** de recuperar **50%** da entrada"},
                    {"key": "trapaceiro", "title": "Trapaceiro", "desc": f"Ao falhar em roubar: **{self._format_percent_text(0.25)}** de não perder nada"},
                    {"key": "redencao", "title": "Redenção", "desc": f"Nas cartas: **{self._format_percent_text(0.35)}** de recuperar **50%** da entrada"},
                ],
            },
        }

    def _get_race_effects(self, race_key: str) -> list[dict[str, str]]:
        info = self._get_race_info_by_key(race_key) or {}
        effects = []
        for effect in list(info.get("effects") or []):
            if isinstance(effect, dict):
                key = str(effect.get("key") or "").strip().lower()
                title = str(effect.get("title") or "").strip()
                desc = str(effect.get("desc") or "").strip()
                if key and title and desc:
                    effects.append({"key": key, "title": title, "desc": desc})
        return effects

    def _get_race_effect_title(self, race_key: str, effect_key: str) -> str:
        target = str(effect_key or "").strip().lower()
        for effect in self._get_race_effects(race_key):
            if effect.get("key") == target:
                return str(effect.get("title") or "")
        return ""

    def _race_effect_message(self, guild_id: int, user_id: int, effect_key: str, detail: str | None = None) -> str:
        title = self._get_race_effect_title(self._get_user_race_key(guild_id, user_id), effect_key)
        if not title:
            return ""
        detail_map = {
            "labia": "seu uso extra de mendigar foi aplicado.",
            "daily": "os extras do daily foram liberados.",
            "mao_negra": "você usou o limite ampliado de roubo.",
            "mao_grande": "o roubo máximo foi ampliado.",
            "sangue_frio": "a perda ao falhar foi reduzida.",
            "trapaceiro": "você se safou da perda no roubo.",
            "jackpot": "o prêmio especial de **100** foi ativado.",
            "all_in": "o prêmio especial de **200** foi ativado.",
            "666": "o custo da roleta foi estornado.",
            "midas": "a versão dourada foi ativada.",
            "premio_extra": f"o prêmio bônus de **20** {self._CHIP_BONUS_EMOJI} foi aplicado.",
            "as": "você recuperou **50%** da entrada.",
            "redencao": "você recuperou **50%** da entrada.",
        }
        suffix = str(detail or detail_map.get(str(effect_key or "").strip().lower(), "")).strip()
        return f"Efeito **{title}** foi usado, {suffix}" if suffix else f"Efeito **{title}** foi usado."

    def _race_effect_marker(self, guild_id: int, user_id: int, effect_key: str) -> str:
        return self._race_effect_message(guild_id, user_id, effect_key)

    def _format_race_identity(self, guild_id: int, user_id: int) -> str:
        info = self._get_user_race_info(guild_id, user_id) or {}
        if not info:
            return ""
        emoji = str(info.get("emoji") or "").strip()
        name = str(info.get("name") or "").strip()
        active = self._is_user_race_active(guild_id, user_id)
        label = f"{emoji}{name}" if emoji else name
        if label and not active:
            label += " (desativada)"
        return label

    def _remember_race_panel_message(self, guild_id: int, user_id: int, message: discord.Message | None):
        if message is None:
            return
        self._race_panel_messages[(int(guild_id), int(user_id))] = (int(message.channel.id), int(message.id))

    def _forget_race_panel_message(self, guild_id: int, user_id: int, *, message_id: int | None = None):
        key = (int(guild_id), int(user_id))
        current = self._race_panel_messages.get(key)
        if not current:
            return
        if message_id is None or int(current[1]) == int(message_id):
            self._race_panel_messages.pop(key, None)

    async def _delete_previous_race_panel_message(self, guild_id: int, user_id: int, channel: discord.abc.Messageable | None = None):
        key = (int(guild_id), int(user_id))
        stored = self._race_panel_messages.pop(key, None)
        if not stored:
            return
        channel_id, message_id = stored
        target_message = None
        try:
            if channel is not None and int(getattr(channel, "id", 0) or 0) == int(channel_id) and hasattr(channel, "fetch_message"):
                target_message = await channel.fetch_message(int(message_id))
            else:
                fetched_channel = self.bot.get_channel(int(channel_id))
                if fetched_channel is None:
                    try:
                        fetched_channel = await self.bot.fetch_channel(int(channel_id))
                    except Exception:
                        fetched_channel = None
                if fetched_channel is not None and hasattr(fetched_channel, "fetch_message"):
                    target_message = await fetched_channel.fetch_message(int(message_id))
            if target_message is not None:
                await target_message.delete()
        except Exception:
            pass

    def _get_user_race_key(self, guild_id: int, user_id: int) -> str:
        try:
            raw = str((self.db._get_user_doc(guild_id, user_id) or {}).get("race_key", "") or "").strip().lower()
        except Exception:
            raw = ""
        return raw if raw in self._race_catalog() else ""

    def _is_user_race_active(self, guild_id: int, user_id: int) -> bool:
        race_key = self._get_user_race_key(guild_id, user_id)
        if not race_key:
            return False
        try:
            return bool((self.db._get_user_doc(guild_id, user_id) or {}).get("race_active", True))
        except Exception:
            return True

    def _get_user_race_info(self, guild_id: int, user_id: int) -> dict[str, object] | None:
        key = self._get_user_race_key(guild_id, user_id)
        return self._race_catalog().get(key) if key else None

    def _get_race_info_by_key(self, race_key: str) -> dict[str, object] | None:
        return self._race_catalog().get(str(race_key or "").strip().lower())

    def _get_race_name(self, race_key: str) -> str:
        info = self._get_race_info_by_key(race_key)
        return str(info.get("name")) if info else "Sem raça"

    def _format_user_race(self, guild_id: int, user_id: int) -> str:
        return self._get_race_name(self._get_user_race_key(guild_id, user_id))

    async def _set_user_race_key(self, guild_id: int, user_id: int, race_key: str | None, *, reset_state: bool = False):
        doc = self.db._get_user_doc(guild_id, user_id)
        key = str(race_key or "").strip().lower()
        if key and key in self._race_catalog():
            doc["race_key"] = key
            doc["race_active"] = True
        else:
            doc.pop("race_key", None)
            doc.pop("race_active", None)
        if reset_state:
            for field in (
                "race_free_roleta_spins",
                "race_free_carta_spins",
                "race_robbery_window_started_at",
                "race_robbery_uses",
                "race_mendigar_window_started_at",
                "race_mendigar_uses",
            ):
                doc.pop(field, None)
        await self.db._save_user_doc(guild_id, user_id, doc)

    async def _set_user_race_active(self, guild_id: int, user_id: int, active: bool):
        doc = self.db._get_user_doc(guild_id, user_id)
        if not self._get_user_race_key(guild_id, user_id):
            doc.pop("race_active", None)
        else:
            doc["race_active"] = bool(active)
        await self.db._save_user_doc(guild_id, user_id, doc)

    async def _clear_user_race(self, guild_id: int, user_id: int):
        await self._set_user_race_key(guild_id, user_id, None, reset_state=True)

    async def _roll_user_race(self, guild_id: int, user_id: int, *, exclude_current: bool = False) -> str:
        choices = list(self._race_catalog().keys())
        current = self._get_user_race_key(guild_id, user_id)
        if exclude_current and current in choices and len(choices) > 1:
            choices.remove(current)
        chosen = random.choice(choices)
        await self._set_user_race_key(guild_id, user_id, chosen, reset_state=True)
        return chosen

    def _race_is(self, guild_id: int, user_id: int, race_key: str) -> bool:
        return self._get_user_race_key(guild_id, user_id) == str(race_key or "").strip().lower() and self._is_user_race_active(guild_id, user_id)

    def _roleta_cost_for_user(self, guild_id: int, user_id: int) -> int:
        return ROLETA_APOSTADOR_COST if self._race_is(guild_id, user_id, "apostador") else ROLETA_COST

    def _special_variant_chance_for_user(self, guild_id: int, user_id: int) -> float:
        return RACE_SPECIAL_SORTUDO_CHANCE if self._race_is(guild_id, user_id, "sortudo") else RACE_SPECIAL_DEFAULT_CHANCE

    def _truco_bonus_reward_for_variant(self, variant: str) -> int:
        return 10 + TRUCO_GOLDEN_BONUS_EXTRA if str(variant or "normal").lower() == "golden" else 10

    def _limited_action_config(self, guild_id: int, user_id: int, *, action: str) -> tuple[int, float]:
        action = str(action or "").strip().lower()
        if action == "robbery":
            if self._race_is(guild_id, user_id, "preto"):
                return 2, float(4 * 60 * 60)
            return 1, float(6 * 60 * 60)
        if action == "mendigar":
            if self._race_is(guild_id, user_id, "preto"):
                return 2, float(CHIPS_MENDIGAR_COOLDOWN_SECONDS)
            return 1, float(CHIPS_MENDIGAR_COOLDOWN_SECONDS)
        return 1, 0.0

    def _limited_action_state(self, guild_id: int, user_id: int, *, storage_key: str, limit: int, window_seconds: float) -> dict[str, float | int]:
        now = time.time()
        doc = self.db._get_user_doc(guild_id, user_id)
        try:
            started_at = float(doc.get(f"{storage_key}_window_started_at", 0) or 0.0)
        except Exception:
            started_at = 0.0
        try:
            used = max(0, int(doc.get(f"{storage_key}_uses", 0) or 0))
        except Exception:
            used = 0
        window = max(0.0, float(window_seconds or 0.0))
        if started_at <= 0 or (window > 0 and (started_at + window) <= now):
            started_at = 0.0
            used = 0
        available = max(0, int(limit) - used)
        remaining = max(0.0, (started_at + window) - now) if available <= 0 and started_at > 0 and window > 0 else 0.0
        return {
            "started_at": float(started_at),
            "used": int(used),
            "available": int(available),
            "limit": int(limit),
            "window_seconds": float(window),
            "remaining": float(remaining),
        }

    async def _consume_limited_action(self, guild_id: int, user_id: int, *, storage_key: str, limit: int, window_seconds: float, legacy_field: str | None = None) -> tuple[bool, dict[str, float | int]]:
        state = self._limited_action_state(guild_id, user_id, storage_key=storage_key, limit=limit, window_seconds=window_seconds)
        if int(state.get("available", 0) or 0) <= 0:
            return False, state
        now = time.time()
        doc = self.db._get_user_doc(guild_id, user_id)
        started_at = float(state.get("started_at", 0.0) or 0.0)
        if started_at <= 0:
            started_at = now
        doc[f"{storage_key}_window_started_at"] = float(started_at)
        doc[f"{storage_key}_uses"] = int(state.get("used", 0) or 0) + 1
        if legacy_field:
            doc[str(legacy_field)] = float(now)
        await self.db._save_user_doc(guild_id, user_id, doc)
        return True, self._limited_action_state(guild_id, user_id, storage_key=storage_key, limit=limit, window_seconds=window_seconds)

    def _get_race_free_spin_count(self, guild_id: int, user_id: int, *, kind: str) -> int:
        field = f"race_free_{str(kind)}_spins"
        try:
            return max(0, int((self.db._get_user_doc(guild_id, user_id) or {}).get(field, 0) or 0))
        except Exception:
            return 0

    async def _grant_race_daily_free_spins(self, guild_id: int, user_id: int) -> dict[str, bool]:
        result = {"roleta": False, "carta": False}
        if not self._race_is(guild_id, user_id, "sortudo"):
            return result
        doc = self.db._get_user_doc(guild_id, user_id)
        if int(doc.get("race_free_roleta_spins", 0) or 0) < 1:
            doc["race_free_roleta_spins"] = 1
            result["roleta"] = True
        if int(doc.get("race_free_carta_spins", 0) or 0) < 1:
            doc["race_free_carta_spins"] = 1
            result["carta"] = True
        await self.db._save_user_doc(guild_id, user_id, doc)
        return result

    async def _consume_race_free_spin(self, guild_id: int, user_id: int, *, kind: str) -> bool:
        field = f"race_free_{str(kind)}_spins"
        doc = self.db._get_user_doc(guild_id, user_id)
        current = max(0, int(doc.get(field, 0) or 0))
        if current <= 0:
            return False
        doc[field] = current - 1
        await self.db._save_user_doc(guild_id, user_id, doc)
        return True

    async def _maybe_apply_coringa_cashback(self, guild_id: int, user_id: int, entry_cost: int, *, chance: float = 0.35) -> int:
        if entry_cost <= 0 or not self._race_is(guild_id, user_id, "coringa"):
            return 0
        if random.random() >= float(chance):
            return 0
        refund = max(1, int(round(int(entry_cost) * 0.5)))
        await self._change_user_chips(guild_id, user_id, refund, mark_activity=True)
        return refund

    def _coringa_avoids_robbery_penalty(self, guild_id: int, user_id: int) -> bool:
        return self._race_is(guild_id, user_id, "coringa") and random.random() < 0.25

    def _chip_rank_position_text(self, guild: discord.Guild, user_id: int) -> str | None:
        rows = self.db.get_chip_leaderboard(guild.id, limit=1000000)
        for index, row in enumerate(rows, start=1):
            try:
                if int(row.get("user_id", 0)) == int(user_id):
                    return f"🏆 Rank: **#{index}**"
            except Exception:
                continue
        return None

    def _make_chip_balance_view(self, member: discord.Member) -> discord.ui.LayoutView:
        guild_id = member.guild.id
        chips = self.db.get_user_chips(guild_id, member.id, default=CHIPS_INITIAL)
        stats = self.db.get_user_game_stats(guild_id, member.id)

        _, _, _, rate = self._chip_summary_stats(stats)
        summary_lines = self._build_chip_game_stat_lines(stats)
        summary_lines.append(f"📈 **Taxa**: **{rate}**")

        primary_balance = self._format_primary_chip_balance(guild_id, member.id)
        race_identity = self._format_race_identity(guild_id, member.id)
        if race_identity:
            primary_balance = f"{primary_balance} • **Raça:** {race_identity}"
        balance_lines = [
            f"# {self._CHIP_EMOJI} Fichas",
            primary_balance,
        ]
        rank_text = self._chip_rank_position_text(member.guild, member.id)
        if rank_text:
            balance_lines.append(rank_text)
        balance_lines.append(f"🎁 **Diário**: {self._daily_bonus_text(guild_id, member.id)}")
        balance_lines.append(f"⏳ **Recarga**: {self._chip_recharge_compact_text(guild_id, member.id)}")
        if not race_identity:
            balance_lines.append("**🧬 Raça:** Use **race** pra definir sua raça")
        if chips < 0:
            balance_lines.append("Ganhos futuros quitam a dívida primeiro.")

        detail_lines: list[str] = []
        best_game = self._best_game_summary(stats)
        if best_game:
            detail_lines.append(f"🎮 **Melhor jogo**: {best_game}")
        detail_lines.extend([
            "# 📊 Resumo",
            "\n".join(summary_lines),
            "Dica: **_rank** mostra sua posição • **_daily** pega seu bônus diário",
        ])

        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(balance_lines)),
            accent_color=discord.Color.blurple(),
        ))
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(detail_lines)),
            accent_color=discord.Color.dark_green(),
        ))
        return view
    def _make_chip_balance_embed(self, member: discord.Member) -> discord.Embed:
        guild_id = member.guild.id
        stats = self.db.get_user_game_stats(guild_id, member.id)
        _, _, _, rate = self._chip_summary_stats(stats)
        embed = discord.Embed(color=discord.Color.blurple())
        embed.set_author(name=str(member.display_name), icon_url=member.display_avatar.url)
        embed.description = f"{self._format_primary_chip_balance(guild_id, member.id)}\n📈 **Taxa de vitórias**: **{rate}**"
        return embed

    async def _maybe_execute_due_chip_season_reset(self, guild_id: int) -> dict | None:
        state = self.db.get_chip_season_state(guild_id)
        reset_at = float(state.get("reset_at", 0.0) or 0.0)
        if not bool(state.get("active", False)) or reset_at <= 0:
            return None
        now = __import__("time").time()
        if reset_at > now:
            return None
        acquired = await self.db.try_acquire_chip_season_reset_lock(guild_id, now=now)
        if not acquired:
            return None
        affected_users = await self.db.reset_guild_chip_economy(guild_id, chips=CHIPS_DEFAULT)
        season = await self.db.complete_chip_season_reset(guild_id, executed_at=now)
        return {"affected_users": int(affected_users), "season": int(season), "executed_at": float(now)}

    async def _prepare_chip_leaderboard_state(self, guild: discord.Guild, requester: discord.Member | None = None) -> dict:
        await self._maybe_execute_due_chip_season_reset(guild.id)
        rows = self.db.get_chip_leaderboard(guild.id, limit=10)
        top_chips = 0
        if rows:
            try:
                top_chips = int(rows[0].get("chips", 0) or 0)
            except Exception:
                top_chips = 0
        if top_chips > CHIPS_SEASON_RESET_THRESHOLD:
            requester_id = int(getattr(requester, "id", 0) or 0)
            await self.db.schedule_chip_season_reset(
                guild.id,
                reset_at=__import__("time").time() + float(CHIPS_SEASON_RESET_SECONDS),
                triggered_by=requester_id,
            )
        return self.db.get_chip_season_state(guild.id)

    def _build_chip_rank_footer(self, guild_id: int) -> str:
        state = self.db.get_chip_season_state(guild_id)
        season = max(1, int(state.get("season", 1) or 1))
        season_line = f"Temporada {season}"
        reset_at = float(state.get("reset_at", 0.0) or 0.0)
        if bool(state.get("active", False)) and reset_at > 0:
            remaining = max(0.0, reset_at - __import__("time").time())
            if remaining > 0:
                season_line += f" • Reset em {self._format_chip_reset_remaining(remaining)}"
            explain = (
                f"Líder acima de {CHIPS_SEASON_RESET_THRESHOLD} fichas normais inicia reset em "
                f"{CHIPS_SEASON_RESET_HOURS}h • saldo {CHIPS_DEFAULT}, bônus 0 e estatísticas zeradas."
            )
            return f"{season_line}\n{explain}"
        return season_line

    async def _make_chip_leaderboard_embed_async(self, guild: discord.Guild, requester: discord.Member | None = None) -> discord.Embed:
        await self._prepare_chip_leaderboard_state(guild, requester)
        embed = self._make_chip_leaderboard_embed(guild, requester)
        embed.set_footer(text=self._build_chip_rank_footer(guild.id))
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
                chips_val = int(row.get('chips', row.get('points', 0)) or 0)
                bonus_val = self._get_user_bonus_chips(guild.id, int(row["user_id"]))
                emoji = self._CHIP_LOSS_EMOJI if chips_val < 0 else self._CHIP_EMOJI
                balance_text = f"**{chips_val}** {emoji}"
                if bonus_val > 0:
                    balance_text += f" • **{bonus_val}** {self._CHIP_BONUS_EMOJI}"
                ranking_lines.append(f"{prefix} **{name}** — {balance_text}")
            embed.add_field(name="Top 10", value="\n".join(ranking_lines), inline=False)

        embed.set_footer(text=self._build_chip_rank_footer(guild.id))
        return embed

    def _format_chip_reset_remaining(self, remaining_seconds: float) -> str:
        remaining = max(0, int(remaining_seconds))
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        if hours > 0:
            return f"{hours}h {minutes:02d}min"
        return f"{minutes}min"

    async def _try_consume_chips(self, guild_id: int, user_id: int, amount: int) -> tuple[bool, int, str | None]:
        spend = max(0, int(amount))
        projected_chips, projected_bonus = self._project_chip_state_after_cost(guild_id, user_id, spend)
        current_before = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        note = self._negative_transition_note(guild_id, user_id, spend)
        if projected_chips < -self._MAX_CHIP_DEBT:
            return False, current_before, self._insufficient_chips_text(guild_id, user_id, spend)
        current_bonus = self._get_user_bonus_chips(guild_id, user_id)
        use_bonus = min(current_bonus, spend)
        remaining = spend - use_bonus
        if use_bonus > 0:
            await self._change_user_bonus_chips(guild_id, user_id, -use_bonus, mark_activity=True)
        if remaining > 0:
            await self._change_user_chips(guild_id, user_id, -remaining, mark_activity=True)
        return True, projected_chips, note

    async def _ensure_action_chips(self, guild_id: int, user_id: int, amount: int) -> tuple[bool, int, str | None]:
        projected_chips, _projected_bonus = self._project_chip_state_after_cost(guild_id, user_id, amount)
        current = self.db.get_user_chips(guild_id, user_id, default=CHIPS_INITIAL)
        if projected_chips < -self._MAX_CHIP_DEBT:
            return False, current, self._insufficient_chips_text(guild_id, user_id, amount)
        note = self._negative_transition_note(guild_id, user_id, amount)
        return True, current, note

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
