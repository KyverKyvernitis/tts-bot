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


ROLETA_JOKERS = ("🃏", "⭐")
ROLETA_SPIN_LIMIT = 10
ROLETA_WINDOW_SECONDS = 6 * 60 * 60
ROLETA_DAILY_EXTRA_CAP = 1

CARTA_COST = 15
CARTA_JACKPOT_CHIPS = 100
CARTA_SYMBOLS = ("🍀", "💎", "👑", "🃏", "⭐")
CARTA_WEIGHTS = (40, 28, 18, 10, 4)
CARTA_SPIN_LIMIT = 5
CARTA_WINDOW_SECONDS = ROLETA_WINDOW_SECONDS
CARTA_DAILY_EXTRA_CAP = ROLETA_DAILY_EXTRA_CAP


class GincanaRoletaMixin:
        def _random_roleta_digit(self, exclude: set[object] | None = None) -> int:
            exclude = exclude or set()
            choices = [digit for digit in range(1, 10) if digit not in exclude]
            if not choices:
                choices = list(range(1, 10))
            return random.choice(choices)

        def _random_roleta_joker(self) -> str:
            return random.choice(ROLETA_JOKERS)
        def _build_roleta_column(self, middle: object | None = None) -> list[object]:
            return [
                self._random_roleta_digit(),
                middle if middle is not None else self._random_roleta_digit(),
                self._random_roleta_digit(),
            ]
        def _spin_roleta_column(self, column: list[object], next_top: object | None = None):
            column.insert(0, self._random_roleta_digit() if next_top is None else next_top)
            del column[3:]
        def _make_roleta_stop_plan(self, column: list[object], target_middle: object) -> list[object]:
            first_top = self._random_roleta_digit(exclude={target_middle, column[0], column[1], column[2]})
            final_top = self._random_roleta_digit(exclude={target_middle, first_top})
            return [first_top, target_middle, final_top]
        def _format_roleta_row(self, row: list[object], *, compact: bool = False) -> str:
            cells = [str(cell) for cell in row]
            if compact:
                return f" {cells[0]}  {cells[1]}  {cells[2]} "
            return f"  {cells[0]}  {cells[1]}  {cells[2]}  "
        def _render_roleta_board(self, columns: list[list[int]]) -> str:
            rows = [[columns[0][i], columns[1][i], columns[2][i]] for i in range(3)]
            top_row = self._format_roleta_row(rows[0])
            middle_row = self._format_roleta_row(rows[1], compact=True)
            bottom_row = self._format_roleta_row(rows[2])
            lines = [
                "┌───────────┐",
                f"│{top_row}│",
                "├───────────┤",
                f"»│{middle_row}│«",
                "├───────────┤",
                f"│{bottom_row}│",
                "└───────────┘",
            ]
            return "```text\n" + "\n".join(lines) + "\n```"
        def _make_roleta_spin_embed(self, board: str, *, footer_text: str | None = None) -> discord.Embed:
            embed = discord.Embed(
                title="🎰 Girando...",
                description=(
                    f"Entrada: {self._chip_amount(ROLETA_COST)}\n"
                    f"Jackpot: {self._chip_amount(ROLETA_JACKPOT_CHIPS)}\n\n"
                    f"{board}"
                ),
                color=discord.Color.blurple(),
            )
            if footer_text:
                try:
                    embed.set_footer(text=footer_text)
                except Exception:
                    pass
            return embed
        def _make_roleta_result_embed(self, title: str, summary: str, board: str, *, success: bool, near: bool = False, footer_text: str | None = None) -> discord.Embed:
            color = discord.Color.blurple() if success or near else discord.Color(OFF_COLOR)
            embed = discord.Embed(
                title=title,
                description=f"{summary}\n\n{board}",
                color=color,
            )
            if footer_text:
                try:
                    embed.set_footer(text=footer_text)
                except Exception:
                    pass
            return embed

        def _roleta_window_total(self, bonus_spins: int = 0) -> int:
            return ROLETA_SPIN_LIMIT + max(0, min(ROLETA_DAILY_EXTRA_CAP, int(bonus_spins or 0)))

        def _format_roleta_reset_time(self, remaining_seconds: float) -> str:
            try:
                total_minutes = max(1, int((float(remaining_seconds) + 59) // 60))
            except Exception:
                total_minutes = 1
            hours, minutes = divmod(total_minutes, 60)
            if hours > 0:
                return f"{hours}h {minutes}min"
            return f"{minutes}min"

        async def _sync_roleta_spin_window(self, guild_id: int, user_id: int) -> dict[str, float | int]:
            now = time.time()
            doc = self.db._get_user_doc(guild_id, user_id)
            try:
                started_at = float(doc.get("roleta_window_started_at", 0) or 0.0)
            except Exception:
                started_at = 0.0
            try:
                used = max(0, int(doc.get("roleta_spins_used", 0) or 0))
            except Exception:
                used = 0
            try:
                bonus = max(0, min(ROLETA_DAILY_EXTRA_CAP, int(doc.get("roleta_bonus_spins", 0) or 0)))
            except Exception:
                bonus = 0
            changed = False
            if started_at <= 0 or (started_at + ROLETA_WINDOW_SECONDS) <= now:
                started_at = now
                used = 0
                bonus = 0
                doc["roleta_window_started_at"] = float(started_at)
                doc["roleta_spins_used"] = 0
                doc["roleta_bonus_spins"] = 0
                changed = True
            total = self._roleta_window_total(bonus)
            available = max(0, total - used)
            reset_in = max(0.0, (started_at + ROLETA_WINDOW_SECONDS) - now)
            if changed:
                await self.db._save_user_doc(guild_id, user_id, doc)
            return {
                "started_at": float(started_at),
                "used": int(used),
                "bonus": int(bonus),
                "total": int(total),
                "available": int(available),
                "reset_in": float(reset_in),
            }

        async def _consume_roleta_spin(self, guild_id: int, user_id: int) -> dict[str, float | int]:
            state = await self._sync_roleta_spin_window(guild_id, user_id)
            if int(state["available"]) <= 0:
                return state
            doc = self.db._get_user_doc(guild_id, user_id)
            used = int(state["used"]) + 1
            doc["roleta_window_started_at"] = float(state["started_at"])
            doc["roleta_spins_used"] = used
            doc["roleta_bonus_spins"] = int(state["bonus"])
            await self.db._save_user_doc(guild_id, user_id, doc)
            total = int(state["total"])
            return {
                "started_at": float(state["started_at"]),
                "used": used,
                "bonus": int(state["bonus"]),
                "total": total,
                "available": max(0, total - used),
                "reset_in": float(max(0.0, (float(state["started_at"]) + ROLETA_WINDOW_SECONDS) - time.time())),
            }

        async def _grant_daily_roleta_spin(self, guild_id: int, user_id: int) -> tuple[bool, dict[str, float | int]]:
            state = await self._sync_roleta_spin_window(guild_id, user_id)
            current_bonus = int(state["bonus"])
            if current_bonus >= ROLETA_DAILY_EXTRA_CAP:
                return False, state
            doc = self.db._get_user_doc(guild_id, user_id)
            doc["roleta_window_started_at"] = float(state["started_at"])
            doc["roleta_spins_used"] = int(state["used"])
            doc["roleta_bonus_spins"] = min(ROLETA_DAILY_EXTRA_CAP, current_bonus + 1)
            await self.db._save_user_doc(guild_id, user_id, doc)
            new_state = await self._sync_roleta_spin_window(guild_id, user_id)
            return True, new_state

        def _roleta_footer_text(self, *, state: dict[str, float | int], is_staff: bool) -> str:
            available = int(state.get("available", 0) or 0)
            if available <= 0 and is_staff:
                return "Seus giros acabaram, mas como você é staff você ainda pode girar."
            return f"Restam {available} giros • Reset em {self._format_roleta_reset_time(float(state.get('reset_in', 0.0) or 0.0))}"

        def _roll_roleta_target_middle(self, *, success: bool) -> list[object]:
            if success:
                return [7, 7, 7]
            roll = random.random()
            if roll < 0.05:
                base = random.randint(1, 9)
                joker = self._random_roleta_joker()
                middle = [base, joker, base]
                random.shuffle(middle)
                return middle
            if roll < 0.13:
                digits = random.sample(range(1, 10), 2)
                middle = [digits[0], digits[1], self._random_roleta_joker()]
                random.shuffle(middle)
                return middle
            if roll < 0.43:
                repeated = random.randint(1, 9)
                other = self._random_roleta_digit(exclude={repeated})
                middle = [repeated, repeated, other]
                random.shuffle(middle)
                return middle
            while True:
                middle = [random.randint(1, 9) for _ in range(3)]
                if middle != [7, 7, 7] and len(set(middle)) == 3:
                    return middle

        def _evaluate_roleta_middle(self, middle_digits: list[object]) -> tuple[str, int]:
            jokers = [value for value in middle_digits if isinstance(value, str) and value in ROLETA_JOKERS]
            normals = [value for value in middle_digits if not (isinstance(value, str) and value in ROLETA_JOKERS)]
            if middle_digits == [7, 7, 7]:
                return "jackpot", ROLETA_JACKPOT_CHIPS
            if jokers:
                if len(set(normals)) == 1 and len(normals) == 2:
                    return "joker_premium", 50
                return "return", max(3, ROLETA_COST // 2)
            if max((middle_digits.count(v) for v in set(middle_digits)), default=0) >= 2:
                return "partial", max(3, ROLETA_COST // 2)
            if random.random() < 0.08:
                return "return", max(2, ROLETA_COST // 3)
            return "loss", 0
        async def _set_roleta_reaction(self, message: discord.Message, emoji: str, *, keep: bool):
            await self._react_with_emoji(message, emoji, keep=keep)
        async def _clear_roleta_reaction(self, message: discord.Message, emoji: str):
            reaction_emoji = emoji
            try:
                if isinstance(emoji, str) and emoji.startswith("<") and emoji.endswith(">"):
                    reaction_emoji = discord.PartialEmoji.from_str(emoji)
                await message.remove_reaction(reaction_emoji, self.bot.user)
            except Exception:
                pass
        async def _animate_roleta_spin(self, message: discord.Message, *, target_middle: list[int], footer_text: str | None = None) -> tuple[discord.Message | None, list[list[int]] | None]:
            columns = [self._build_roleta_column() for _ in range(3)]
            for idx in range(3):
                if columns[idx][1] == target_middle[idx]:
                    reroll = self._build_roleta_column()
                    while reroll[1] == target_middle[idx]:
                        reroll = self._build_roleta_column()
                    columns[idx] = reroll
            try:
                spin_message = await message.channel.send(embed=self._make_roleta_spin_embed(self._render_roleta_board(columns), footer_text=footer_text))
            except Exception:
                return None, None

            target_duration = 5.0
            intervals = [0.18, 0.21, 0.24, 0.28, 0.33, 0.39, 0.47, 0.58, 0.72, 0.90, 1.05]
            scale = target_duration / sum(intervals)
            intervals = [step * scale for step in intervals]
            stop_plan_starts = {
                0: max(0, len(intervals) - 5),
                1: max(0, len(intervals) - 4),
                2: max(0, len(intervals) - 3),
            }
            active_stop_plans: dict[int, list[int]] = {}
            locked_columns: set[int] = set()
            previous_board = None

            for index, delay in enumerate(intervals):
                await asyncio.sleep(delay)

                for column_index, start_index in stop_plan_starts.items():
                    if index == start_index and column_index not in active_stop_plans and column_index not in locked_columns:
                        active_stop_plans[column_index] = self._make_roleta_stop_plan(columns[column_index], target_middle[column_index])

                for column_index in range(3):
                    if column_index in locked_columns:
                        continue
                    if column_index in active_stop_plans:
                        plan = active_stop_plans[column_index]
                        self._spin_roleta_column(columns[column_index], next_top=plan.pop(0))
                        if not plan:
                            active_stop_plans.pop(column_index, None)
                            locked_columns.add(column_index)
                    else:
                        self._spin_roleta_column(columns[column_index])

                board = self._render_roleta_board(columns)
                if board == previous_board:
                    for column_index in range(3):
                        if column_index in locked_columns:
                            continue
                        if column_index in active_stop_plans:
                            plan = active_stop_plans[column_index]
                            self._spin_roleta_column(columns[column_index], next_top=plan.pop(0))
                            if not plan:
                                active_stop_plans.pop(column_index, None)
                                locked_columns.add(column_index)
                        else:
                            self._spin_roleta_column(columns[column_index])
                    board = self._render_roleta_board(columns)
                previous_board = board

                try:
                    await spin_message.edit(embed=self._make_roleta_spin_embed(board, footer_text=footer_text))
                except Exception:
                    pass

            return spin_message, columns
        def _carta_window_total(self, bonus_spins: int = 0) -> int:
            return CARTA_SPIN_LIMIT + max(0, min(CARTA_DAILY_EXTRA_CAP, int(bonus_spins or 0)))

        async def _sync_carta_spin_window(self, guild_id: int, user_id: int) -> dict[str, float | int]:
            now = time.time()
            doc = self.db._get_user_doc(guild_id, user_id)
            try:
                started_at = float(doc.get("carta_window_started_at", 0) or 0.0)
            except Exception:
                started_at = 0.0
            try:
                used = max(0, int(doc.get("carta_spins_used", 0) or 0))
            except Exception:
                used = 0
            try:
                bonus = max(0, min(CARTA_DAILY_EXTRA_CAP, int(doc.get("carta_bonus_spins", 0) or 0)))
            except Exception:
                bonus = 0
            changed = False
            if started_at <= 0 or (started_at + CARTA_WINDOW_SECONDS) <= now:
                started_at = now
                used = 0
                bonus = 0
                doc["carta_window_started_at"] = float(started_at)
                doc["carta_spins_used"] = 0
                doc["carta_bonus_spins"] = 0
                changed = True
            total = self._carta_window_total(bonus)
            available = max(0, total - used)
            reset_in = max(0.0, (started_at + CARTA_WINDOW_SECONDS) - now)
            if changed:
                await self.db._save_user_doc(guild_id, user_id, doc)
            return {
                "started_at": float(started_at),
                "used": int(used),
                "bonus": int(bonus),
                "total": int(total),
                "available": int(available),
                "reset_in": float(reset_in),
            }

        async def _consume_carta_spin(self, guild_id: int, user_id: int) -> dict[str, float | int]:
            state = await self._sync_carta_spin_window(guild_id, user_id)
            if int(state["available"]) <= 0:
                return state
            doc = self.db._get_user_doc(guild_id, user_id)
            used = int(state["used"]) + 1
            doc["carta_window_started_at"] = float(state["started_at"])
            doc["carta_spins_used"] = used
            doc["carta_bonus_spins"] = int(state["bonus"])
            await self.db._save_user_doc(guild_id, user_id, doc)
            total = int(state["total"])
            return {
                "started_at": float(state["started_at"]),
                "used": used,
                "bonus": int(state["bonus"]),
                "total": total,
                "available": max(0, total - used),
                "reset_in": float(max(0.0, (float(state["started_at"]) + CARTA_WINDOW_SECONDS) - time.time())),
            }

        async def _grant_daily_carta_spin(self, guild_id: int, user_id: int) -> tuple[bool, dict[str, float | int]]:
            state = await self._sync_carta_spin_window(guild_id, user_id)
            current_bonus = int(state["bonus"])
            if current_bonus >= CARTA_DAILY_EXTRA_CAP:
                return False, state
            doc = self.db._get_user_doc(guild_id, user_id)
            doc["carta_window_started_at"] = float(state["started_at"])
            doc["carta_spins_used"] = int(state["used"])
            doc["carta_bonus_spins"] = min(CARTA_DAILY_EXTRA_CAP, current_bonus + 1)
            await self.db._save_user_doc(guild_id, user_id, doc)
            return True, await self._sync_carta_spin_window(guild_id, user_id)

        def _carta_footer_text(self, *, state: dict[str, float | int], is_staff: bool) -> str:
            available = int(state.get("available", 0) or 0)
            return f"Restam {available} giros de cartas • Reset em {self._format_roleta_reset_time(float(state.get('reset_in', 0.0) or 0.0))}"

        def _pick_carta_result_flavor(self, result_kind: str, *, fallback: str = "") -> str:
            options = {
                "loss": [
                    "Essa mão não rendeu nada.",
                    "As cartas não encaixaram.",
                    "Dessa vez a mão passou em branco.",
                ],
                "return": [
                    "O coringa salvou parte da aposta.",
                    "O coringa evitou a perda completa.",
                    "O coringa segurou parte da rodada.",
                ],
                "partial": [
                    "Essa mão rendeu bem.",
                    "As cartas encaixaram.",
                    "Foi uma boa combinação.",
                ],
                "premium": [
                    "O coringa completou a combinação.",
                    "O coringa fechou a mão.",
                    "O coringa puxou a melhor carta da rodada.",
                ],
                "rare": [
                    "Essa mão veio forte.",
                    "As cartas bateram bonito.",
                    "Foi uma combinação rara.",
                ],
                "jackpot": [
                    "A mão bateu o prêmio máximo.",
                    "Você acertou a mão máxima.",
                    "As cartas vieram perfeitas.",
                ],
            }
            picks = options.get(result_kind)
            if picks:
                return random.choice(picks)
            return fallback or "Resultado das cartas."

        def _pick_carta_hot_streak_text(self) -> str:
            return random.choice([
                "Você entrou em boa fase.",
                "Sua mão esquentou.",
                "A sequência ficou forte.",
            ])

        async def _advance_carta_hot_streak(self, guild_id: int, user_id: int, *, result_kind: str) -> tuple[int, str | None]:
            doc = self.db._get_user_doc(guild_id, user_id)
            try:
                current = max(0, int(doc.get("carta_hot_streak", 0) or 0))
            except Exception:
                current = 0
            counts_for_streak = result_kind in {"partial", "premium", "rare", "jackpot"}
            new_value = current + 1 if counts_for_streak else 0
            doc["carta_hot_streak"] = int(new_value)
            await self.db._save_user_doc(guild_id, user_id, doc)
            if counts_for_streak and new_value >= 2:
                return new_value, self._pick_carta_hot_streak_text()
            return new_value, None

        def _format_carta_row(self, row: list[object], *, middle: bool = False) -> str:
            cells = [str(cell) for cell in row]
            row_text = f"{cells[0]}  {cells[1]}  {cells[2]}"
            if middle:
                return f" »{row_text}«"
            return f"│ {row_text}  "

        def _render_carta_board(self, columns: list[list[object]]) -> str:
            rows = [[columns[0][i], columns[1][i], columns[2][i]] for i in range(3)]
            lines = [
                "┌────────────┐",
                self._format_carta_row(rows[0]),
                "├────────────┤",
                self._format_carta_row(rows[1], middle=True),
                "├────────────┤",
                self._format_carta_row(rows[2]),
                "└────────────┘",
            ]
            return "```text\n" + "\n".join(lines) + "\n```"

        def _random_carta_symbol(self, exclude: set[object] | None = None) -> str:
            exclude = exclude or set()
            choices = [symbol for symbol in CARTA_SYMBOLS if symbol not in exclude]
            if not choices:
                choices = list(CARTA_SYMBOLS)
            weights = [CARTA_WEIGHTS[CARTA_SYMBOLS.index(symbol)] for symbol in choices]
            return random.choices(choices, weights=weights, k=1)[0]

        def _build_carta_column(self, middle: object | None = None) -> list[object]:
            return [
                self._random_carta_symbol(),
                middle if middle is not None else self._random_carta_symbol(),
                self._random_carta_symbol(),
            ]

        def _spin_carta_column(self, column: list[object], next_top: object | None = None):
            column.insert(0, self._random_carta_symbol() if next_top is None else next_top)
            del column[3:]

        def _make_carta_stop_plan(self, column: list[object], target_middle: object) -> list[object]:
            first_top = self._random_carta_symbol(exclude={target_middle, column[0], column[1], column[2]})
            final_top = self._random_carta_symbol(exclude={target_middle, first_top})
            return [first_top, target_middle, final_top]

        def _make_carta_spin_embed(self, board: str, *, footer_text: str | None = None) -> discord.Embed:
            embed = discord.Embed(
                title="🎴 Cartas embaralhando...",
                description=(
                    f"Entrada: {self._chip_amount(CARTA_COST)}\n"
                    f"Jackpot: {self._chip_amount(CARTA_JACKPOT_CHIPS)}\n\n"
                    f"{board}"
                ),
                color=discord.Color.from_rgb(111, 88, 242),
            )
            if footer_text:
                try:
                    embed.set_footer(text=footer_text)
                except Exception:
                    pass
            return embed

        def _make_carta_result_embed(self, title: str, summary: str, board: str, *, success: bool, premium: bool = False, footer_text: str | None = None) -> discord.Embed:
            color = discord.Color.from_rgb(255, 201, 74) if premium else (discord.Color.from_rgb(88, 179, 104) if success else discord.Color(OFF_COLOR))
            embed = discord.Embed(
                title=title,
                description=f"{summary}\n\n{board}",
                color=color,
            )
            if footer_text:
                try:
                    embed.set_footer(text=footer_text)
                except Exception:
                    pass
            return embed

        def _roll_carta_target_middle(self) -> list[object]:
            roll = random.random()
            if roll < 0.02:
                return ["⭐", "⭐", "⭐"]
            if roll < 0.035:
                return ["🃏", "🃏", "🃏"]
            if roll < 0.065:
                base = random.choice(["👑", "💎", "🍀"])
                return [base, base, base]
            if roll < 0.14:
                base = random.choice(["⭐", "👑", "💎", "🍀"])
                middle = [base, base, "🃏"]
                random.shuffle(middle)
                return middle
            if roll < 0.30:
                base = random.choice(["⭐", "👑", "💎", "🍀"])
                other = self._random_carta_symbol(exclude={base, "🃏"})
                middle = [base, base, other]
                random.shuffle(middle)
                return middle
            if roll < 0.40:
                others = random.sample(["⭐", "👑", "💎", "🍀"], 2)
                middle = [others[0], others[1], "🃏"]
                random.shuffle(middle)
                return middle
            while True:
                middle = [self._random_carta_symbol() for _ in range(3)]
                if len(set(middle)) == 3 and middle.count("🃏") <= 1:
                    return middle

        def _evaluate_carta_middle(self, middle_symbols: list[object]) -> tuple[str, int, str]:
            symbols = [str(v) for v in middle_symbols]
            counts = {symbol: symbols.count(symbol) for symbol in set(symbols)}
            joker_count = counts.get("🃏", 0)
            star_count = counts.get("⭐", 0)
            if symbols == ["⭐", "⭐", "⭐"]:
                return "jackpot", CARTA_JACKPOT_CHIPS, "A mão bateu o prêmio máximo."
            if counts.get("🃏", 0) == 3:
                return "rare", 80, "Trinca de coringas na linha do meio."
            if any(count == 3 for symbol, count in counts.items() if symbol != "🃏"):
                triple_symbol = next(symbol for symbol, count in counts.items() if symbol != "🃏" and count == 3)
                values = {"👑": 50, "💎": 35, "🍀": 25, "⭐": 65}
                texts = {"👑": "Trinca de coroas.", "💎": "Trinca de diamantes.", "🍀": "Trinca de trevos.", "⭐": "Trinca rara de estrelas."}
                return "rare", values.get(triple_symbol, 25), texts.get(triple_symbol, "Trinca premiada.")
            pair_symbol = next((symbol for symbol, count in counts.items() if symbol != "🃏" and count == 2), None)
            if pair_symbol and joker_count == 1:
                values = {"⭐": 70, "👑": 40, "💎": 30, "🍀": 22}
                texts = {"⭐": "O coringa completou a mão máxima.", "👑": "O coringa completou a combinação.", "💎": "O coringa fechou a combinação.", "🍀": "O coringa ajudou a fechar a mão."}
                return "premium", values.get(pair_symbol, 20), texts.get(pair_symbol, "O coringa completou a combinação.")
            if joker_count == 2 and len(counts) == 2:
                other = next(symbol for symbol in counts if symbol != "🃏")
                values = {"⭐": 55, "👑": 32, "💎": 24, "🍀": 18}
                return "premium", values.get(other, 18), "Dois coringas puxaram a combinação."
            if pair_symbol:
                values = {"⭐": 20, "👑": 15, "💎": 12, "🍀": 10}
                texts = {"⭐": "Par raro na linha do meio.", "👑": "Par de coroas.", "💎": "Par de diamantes.", "🍀": "Par de trevos."}
                return "partial", values.get(pair_symbol, 10), texts.get(pair_symbol, "Par premiado.")
            if joker_count == 1 and len(counts) == 3:
                return "return", 10, "O coringa salvou parte da aposta."
            if star_count == 2:
                return "partial", 18, "Quase bateu a mão mais rara."
            return "loss", 0, "Essa mão não rendeu nada."

        async def _animate_carta_spin(self, message: discord.Message, *, target_middle: list[object], footer_text: str | None = None) -> tuple[discord.Message | None, list[list[object]] | None]:
            columns = [self._build_carta_column() for _ in range(3)]
            for idx in range(3):
                if columns[idx][1] == target_middle[idx]:
                    reroll = self._build_carta_column()
                    while reroll[1] == target_middle[idx]:
                        reroll = self._build_carta_column()
                    columns[idx] = reroll
            try:
                spin_message = await message.channel.send(embed=self._make_carta_spin_embed(self._render_carta_board(columns), footer_text=footer_text))
            except Exception:
                return None, None
            target_duration = 4.6
            intervals = [0.18, 0.21, 0.25, 0.30, 0.36, 0.44, 0.55, 0.70, 0.90, 1.05]
            scale = target_duration / sum(intervals)
            intervals = [step * scale for step in intervals]
            stop_plan_starts = {0: max(0, len(intervals)-5), 1: max(0, len(intervals)-4), 2: max(0, len(intervals)-3)}
            active_stop_plans: dict[int, list[object]] = {}
            locked_columns: set[int] = set()
            previous_board = None
            for index, delay in enumerate(intervals):
                await asyncio.sleep(delay)
                for column_index, start_index in stop_plan_starts.items():
                    if index == start_index and column_index not in active_stop_plans and column_index not in locked_columns:
                        active_stop_plans[column_index] = self._make_carta_stop_plan(columns[column_index], target_middle[column_index])
                for column_index in range(3):
                    if column_index in locked_columns:
                        continue
                    if column_index in active_stop_plans:
                        plan = active_stop_plans[column_index]
                        self._spin_carta_column(columns[column_index], next_top=plan.pop(0))
                        if not plan:
                            active_stop_plans.pop(column_index, None)
                            locked_columns.add(column_index)
                    else:
                        self._spin_carta_column(columns[column_index])
                board = self._render_carta_board(columns)
                if board == previous_board:
                    for column_index in range(3):
                        if column_index in locked_columns:
                            continue
                        if column_index in active_stop_plans:
                            plan = active_stop_plans[column_index]
                            self._spin_carta_column(columns[column_index], next_top=plan.pop(0))
                            if not plan:
                                active_stop_plans.pop(column_index, None)
                                locked_columns.add(column_index)
                        else:
                            self._spin_carta_column(columns[column_index])
                    board = self._render_carta_board(columns)
                previous_board = board
                try:
                    await spin_message.edit(embed=self._make_carta_spin_embed(board, footer_text=footer_text))
                except Exception:
                    pass
            return spin_message, columns

        async def _handle_carta_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False
            content = (message.content or "").strip().casefold()
            if content not in {"carta", "cartas"}:
                return False
            if not self.db.gincana_enabled(guild.id):
                return True
            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True
            if guild.id in self._roleta_running_guilds:
                return True

            is_staff = isinstance(message.author, discord.Member) and self._is_staff_member(message.author)
            carta_state = await self._sync_carta_spin_window(guild.id, message.author.id)
            if int(carta_state.get("available", 0) or 0) <= 0:
                try:
                    wait_text = self._format_roleta_reset_time(float(carta_state.get("reset_in", 0.0) or 0.0))
                    embed = discord.Embed(
                        title="🎴 Sem giros por agora",
                        description=f"Seus {CARTA_SPIN_LIMIT} giros de cartas acabaram. Reset em **{wait_text}**.",
                        color=discord.Color(OFF_COLOR),
                    )
                    await message.channel.send(embed=embed)
                except Exception:
                    pass
                return True
            if int(carta_state.get("available", 0) or 0) > 0:
                carta_state = await self._consume_carta_spin(guild.id, message.author.id)
            carta_footer = self._carta_footer_text(state=carta_state, is_staff=is_staff)

            needs_negative_confirm = self._needs_negative_confirmation(guild.id, message.author.id, CARTA_COST)
            if needs_negative_confirm:
                confirmed = await self._confirm_negative_from_message(message, guild.id, message.author.id, CARTA_COST, title="🎴 Confirmar aposta")
                if not confirmed:
                    return True
            paid, _balance, chip_note = await self._try_consume_chips(guild.id, message.author.id, CARTA_COST)
            if needs_negative_confirm:
                chip_note = None
            if not paid:
                try:
                    await message.channel.send(embed=self._make_embed("🎴 Saldo insuficiente", chip_note or "Você não tem saldo suficiente.", ok=False))
                except Exception:
                    pass
                return True

            self._roleta_running_guilds.add(guild.id)
            spinning_emoji = "🎴"
            jackpot_emoji = "<:emoji_64:1485043651292827788>"
            lose_emoji = "<:emoji_65:1485043671077228786>"
            try:
                await self._set_roleta_reaction(message, spinning_emoji, keep=True)
                result_reaction = lose_emoji
                target_middle = self._roll_carta_target_middle()
                spin_message, final_columns = await self._animate_carta_spin(message, target_middle=target_middle, footer_text=carta_footer)
                if final_columns is None:
                    final_columns = [
                        self._build_carta_column(target_middle[0]),
                        self._build_carta_column(target_middle[1]),
                        self._build_carta_column(target_middle[2]),
                    ]
                board = self._render_carta_board(final_columns)
                middle = [column[1] for column in final_columns]
                result_kind, result_amount, flavor = self._evaluate_carta_middle(middle)
                await self.db.add_user_game_stat(guild.id, message.author.id, "carta_spins", 1)
                flavor = self._pick_carta_result_flavor(result_kind, fallback=flavor)
                _streak_value, streak_line = await self._advance_carta_hot_streak(guild.id, message.author.id, result_kind=result_kind)
                if result_kind == "jackpot":
                    await self._record_game_played(guild.id, message.author.id, weekly_points=12)
                    await self._change_user_chips(guild.id, message.author.id, CARTA_JACKPOT_CHIPS)
                    await self.db.add_user_game_stat(guild.id, message.author.id, "cartas_jackpots", 1)
                    await self._grant_weekly_points(guild.id, message.author.id, 18)
                    summary = f"{flavor}\nVocê ganhou {self._chip_amount(CARTA_JACKPOT_CHIPS)}."
                    if streak_line:
                        summary = f"{summary}\n*{streak_line}*"
                    if chip_note:
                        summary = f"{chip_note}\n{summary}"
                    embed = self._make_carta_result_embed("🎴 JACKPOT!!", summary, board, success=True, premium=True, footer_text=carta_footer)
                    result_reaction = jackpot_emoji
                elif result_kind in {"rare", "premium", "partial", "return"}:
                    weekly_map = {"rare": 8, "premium": 7, "partial": 4, "return": 2}
                    await self._record_game_played(guild.id, message.author.id, weekly_points=weekly_map.get(result_kind, 3))
                    await self._change_user_chips(guild.id, message.author.id, result_amount)
                    if result_kind in {"rare", "premium"}:
                        await self._grant_weekly_points(guild.id, message.author.id, 6)
                    line = f"{flavor}\nEssa mão rendeu {self._chip_text(result_amount, kind='gain')}."
                    if result_kind == "return":
                        line = f"{flavor}\nVocê recuperou {self._chip_text(result_amount, kind='gain')}."
                    elif streak_line:
                        line = f"{line}\n*{streak_line}*"
                    if chip_note:
                        line = f"{chip_note}\n{line}"
                    titles = {"rare": "🎴 Mão rara", "premium": "🎴 Coringa premiado", "partial": "🎴 Boa mão", "return": "🎴 Giro de retorno"}
                    embed = self._make_carta_result_embed(titles.get(result_kind, "🎴 Boa mão"), line, board, success=True, premium=result_kind in {"rare", "premium"}, footer_text=carta_footer)
                    if result_kind in {"return", "premium"}:
                        result_reaction = "🃏"
                    elif result_kind == "rare":
                        result_reaction = "⭐"
                    else:
                        result_reaction = "🍀"
                else:
                    await self._record_game_played(guild.id, message.author.id, weekly_points=2)
                    summary = f"{flavor}\nVocê perdeu {self._chip_text(CARTA_COST, kind='loss')}."
                    if chip_note:
                        summary = f"{chip_note}\n{summary}"
                    embed = self._make_carta_result_embed("🎴 Não foi dessa vez...", summary, board, success=False, premium=False, footer_text=carta_footer)
                    result_reaction = lose_emoji
                delivered = False
                if spin_message is not None:
                    try:
                        await spin_message.edit(embed=embed)
                        delivered = True
                    except Exception:
                        pass
                if not delivered:
                    try:
                        await message.channel.send(embed=embed)
                    except Exception:
                        pass
                await self._clear_roleta_reaction(message, spinning_emoji)
                await self._set_roleta_reaction(message, result_reaction, keep=True)
                return True
            finally:
                self._roleta_running_guilds.discard(guild.id)

        async def _handle_roleta_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False

            content = (message.content or "")
            if not self._matches_exact_trigger(content, "roleta"):
                return False

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            if guild.id in self._roleta_running_guilds:
                return True

            is_staff = isinstance(message.author, discord.Member) and self._is_staff_member(message.author)
            roleta_state = await self._sync_roleta_spin_window(guild.id, message.author.id)
            if int(roleta_state.get("available", 0) or 0) <= 0 and not is_staff:
                try:
                    wait_text = self._format_roleta_reset_time(float(roleta_state.get("reset_in", 0.0) or 0.0))
                    embed = discord.Embed(
                        title="🎰 Sem giros por agora",
                        description=f"Seus {ROLETA_SPIN_LIMIT} giros acabaram. Reset em **{wait_text}**.",
                        color=discord.Color(OFF_COLOR),
                    )
                    await message.channel.send(embed=embed)
                except Exception:
                    pass
                return True
            if int(roleta_state.get("available", 0) or 0) > 0:
                roleta_state = await self._consume_roleta_spin(guild.id, message.author.id)
            roleta_footer = self._roleta_footer_text(state=roleta_state, is_staff=is_staff)

            author_voice = getattr(message.author, "voice", None)
            voice_channel = getattr(author_voice, "channel", None)
            targets = self._resolve_targets(guild, voice_channel) if isinstance(voice_channel, discord.VoiceChannel) else []

            needs_negative_confirm = self._needs_negative_confirmation(guild.id, message.author.id, ROLETA_COST)
            if needs_negative_confirm:
                confirmed = await self._confirm_negative_from_message(message, guild.id, message.author.id, ROLETA_COST, title="🎰 Confirmar aposta")
                if not confirmed:
                    return True
            paid, _balance, chip_note = await self._try_consume_chips(guild.id, message.author.id, ROLETA_COST)
            if needs_negative_confirm:
                chip_note = None
            if not paid:
                try:
                    await message.channel.send(embed=self._make_embed("🎰 Saldo insuficiente", chip_note or "Você não tem saldo suficiente.", ok=False))
                except Exception:
                    pass
                return True

            self._roleta_running_guilds.add(guild.id)
            spinning_emoji = "<:emoji_63:1485041721573249135>"
            win_emoji = "<:emoji_64:1485043651292827788>"
            lose_emoji = "<:emoji_65:1485043671077228786>"
            try:
                await self._set_roleta_reaction(message, spinning_emoji, keep=True)
                success = random.randint(1, 10) == 1
                target_middle = self._roll_roleta_target_middle(success=success)

                spin_message, final_columns = await self._animate_roleta_spin(message, target_middle=target_middle, footer_text=roleta_footer)

                if final_columns is None:
                    final_columns = [
                        self._build_roleta_column(target_middle[0]),
                        self._build_roleta_column(target_middle[1]),
                        self._build_roleta_column(target_middle[2]),
                    ]

                try:
                    board = self._render_roleta_board(final_columns)

                    middle_digits = [column[1] for column in final_columns]
                    result_kind, result_amount = self._evaluate_roleta_middle(middle_digits)
                    near_like = result_kind in {"partial", "return", "joker_premium"}

                    if result_kind == "jackpot":
                        chosen_channel = voice_channel if targets and isinstance(voice_channel, discord.VoiceChannel) else None
                        if chosen_channel is not None:
                            try:
                                await self._play_roleta_sfx(guild, chosen_channel)
                            except Exception:
                                pass
                            await asyncio.sleep(0.20)
                        for target in targets:
                            if target.voice and target.voice.channel:
                                try:
                                    await target.move_to(None, reason="gincana roleta")
                                except Exception:
                                    pass
                        await self._record_game_played(guild.id, message.author.id, weekly_points=12)
                        await self._change_user_chips(guild.id, message.author.id, ROLETA_JACKPOT_CHIPS)
                        await self.db.add_user_game_stat(guild.id, message.author.id, "roleta_jackpots", 1)
                        await self._grant_weekly_points(guild.id, message.author.id, 20)
                        summary = f"Você ganhou {self._chip_amount(ROLETA_JACKPOT_CHIPS)}."
                        if chip_note:
                            summary = f"{chip_note}\n{summary}"
                        embed = self._make_roleta_result_embed(
                            "💥🎰 JACKPOT!!",
                            summary,
                            board,
                            success=True,
                            footer_text=roleta_footer,
                        )
                    elif result_kind == "joker_premium":
                        await self._record_game_played(guild.id, message.author.id, weekly_points=6)
                        await self._change_user_chips(guild.id, message.author.id, result_amount)
                        await self._grant_weekly_points(guild.id, message.author.id, 8)
                        summary = f"Teve símbolo coringa e rendeu {self._chip_text(result_amount, kind='gain')}."
                        if chip_note:
                            summary = f"{chip_note}\n{summary}"
                        embed = self._make_roleta_result_embed(
                            "🎰 Coringa premiado",
                            summary,
                            board,
                            success=False,
                            near=True,
                            footer_text=roleta_footer,
                        )
                    elif result_kind == "partial":
                        await self._record_game_played(guild.id, message.author.id, weekly_points=4)
                        await self._change_user_chips(guild.id, message.author.id, result_amount)
                        await self._grant_weekly_points(guild.id, message.author.id, 6)
                        summary = f"Esse giro rendeu {self._chip_text(result_amount, kind='gain')}."
                        if chip_note:
                            summary = f"{chip_note}\n{summary}"
                        embed = self._make_roleta_result_embed(
                            "🎰 Giro parcial",
                            summary,
                            board,
                            success=False,
                            near=True,
                            footer_text=roleta_footer,
                        )
                    elif result_kind == "return":
                        await self._record_game_played(guild.id, message.author.id, weekly_points=3)
                        await self._change_user_chips(guild.id, message.author.id, result_amount)
                        summary = f"Você recuperou {self._chip_text(result_amount, kind='gain')}."
                        if chip_note:
                            summary = f"{chip_note}\n{summary}"
                        embed = self._make_roleta_result_embed(
                            "🎰 Giro de retorno",
                            summary,
                            board,
                            success=False,
                            near=True,
                            footer_text=roleta_footer,
                        )
                    else:
                        await self._record_game_played(guild.id, message.author.id, weekly_points=2)
                        summary = f"Você perdeu {self._chip_amount(ROLETA_COST)}."
                        if chip_note:
                            summary = f"{chip_note}\n{summary}"
                        embed = self._make_roleta_result_embed(
                            "🎰 Não foi dessa vez...",
                            summary,
                            board,
                            success=False,
                            footer_text=roleta_footer,
                        )
                except Exception:
                    if success:
                        fallback_title = "💥🎰 JACKPOT!!"
                        fallback_text = f"Você ganhou {self._chip_amount(ROLETA_JACKPOT_CHIPS)}."
                        if chip_note:
                            fallback_text = f"{chip_note}\n{fallback_text}"
                    else:
                        fallback_title = "🎰 Não foi dessa vez..."
                        fallback_text = f"Você perdeu {self._chip_amount(ROLETA_COST)}."
                        if chip_note:
                            fallback_text = f"{chip_note}\n{fallback_text}"
                    embed = self._make_embed(
                        fallback_title,
                        fallback_text,
                        ok=success,
                    )
                    try:
                        embed.set_footer(text=roleta_footer)
                    except Exception:
                        pass

                delivered = False
                if spin_message is not None:
                    try:
                        await spin_message.edit(embed=embed)
                        delivered = True
                    except Exception:
                        pass
                if not delivered:
                    try:
                        await message.channel.send(embed=embed)
                    except Exception:
                        pass

                await self._clear_roleta_reaction(message, spinning_emoji)
                if success:
                    await self._set_roleta_reaction(message, win_emoji, keep=True)
                else:
                    await self._set_roleta_reaction(message, lose_emoji, keep=True)
                return True
            finally:
                self._roleta_running_guilds.discard(guild.id)
