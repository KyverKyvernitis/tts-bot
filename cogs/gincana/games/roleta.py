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


class GincanaRoletaMixin:
        def _random_roleta_digit(self, exclude: set[int] | None = None) -> int:
            exclude = exclude or set()
            choices = [digit for digit in range(1, 10) if digit not in exclude]
            if not choices:
                choices = list(range(1, 10))
            return random.choice(choices)
        def _build_roleta_column(self, middle: int | None = None) -> list[int]:
            return [
                self._random_roleta_digit(),
                middle if middle is not None else self._random_roleta_digit(),
                self._random_roleta_digit(),
            ]
        def _spin_roleta_column(self, column: list[int], next_top: int | None = None):
            column.insert(0, self._random_roleta_digit() if next_top is None else next_top)
            del column[3:]
        def _make_roleta_stop_plan(self, column: list[int], target_middle: int) -> list[int]:
            first_top = self._random_roleta_digit(exclude={target_middle, column[0], column[1], column[2]})
            final_top = self._random_roleta_digit(exclude={target_middle, first_top})
            return [first_top, target_middle, final_top]
        def _format_roleta_row(self, row: list[int], *, compact: bool = False) -> str:
            if compact:
                return f" {row[0]}  {row[1]}  {row[2]} "
            return f"  {row[0]}  {row[1]}  {row[2]}  "
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
        def _make_roleta_spin_embed(self, board: str) -> discord.Embed:
            return discord.Embed(
                title="🎰 Girando...",
                description=(
                    f"Entrada: {self._chip_amount(ROLETA_COST)}\n"
                    f"Jackpot: {self._chip_amount(ROLETA_JACKPOT_CHIPS)}\n\n"
                    f"{board}"
                ),
                color=discord.Color.blurple(),
            )
        def _make_roleta_result_embed(self, title: str, summary: str, board: str, *, success: bool) -> discord.Embed:
            color = discord.Color.blurple() if success else discord.Color(OFF_COLOR)
            return discord.Embed(
                title=title,
                description=f"{summary}\n\n{board}",
                color=color,
            )
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
        async def _animate_roleta_spin(self, message: discord.Message, *, target_middle: list[int]) -> tuple[discord.Message | None, list[list[int]] | None]:
            columns = [self._build_roleta_column() for _ in range(3)]
            for idx in range(3):
                if columns[idx][1] == target_middle[idx]:
                    reroll = self._build_roleta_column()
                    while reroll[1] == target_middle[idx]:
                        reroll = self._build_roleta_column()
                    columns[idx] = reroll
            try:
                spin_message = await message.channel.send(embed=self._make_roleta_spin_embed(self._render_roleta_board(columns)))
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
                    await spin_message.edit(embed=self._make_roleta_spin_embed(board))
                except Exception:
                    pass

            return spin_message, columns
        async def _handle_roleta_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False

            content = (message.content or "")
            if not self._matches_exact_trigger(content, "roleta"):
                return False

            if GUILD_IDS and guild.id not in GUILD_IDS:
                return True

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            if guild.id in self._roleta_running_guilds:
                return True

            author_voice = getattr(message.author, "voice", None)
            voice_channel = getattr(author_voice, "channel", None)
            if not isinstance(voice_channel, discord.VoiceChannel):
                return True

            targets = self._resolve_targets(guild, voice_channel)
            if not targets:
                embed = self._make_embed(
                    "🎲 Roleta sem alvos",
                    "Não há usuários alvo da gincana nesse canal de voz para usar a trigger **roleta**.",
                    ok=False,
                )
                try:
                    await message.channel.send(embed=embed)
                except Exception:
                    pass
                return True

            paid, _balance, chip_note = await self._try_consume_chips(guild.id, message.author.id, ROLETA_COST)
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

                if success:
                    target_middle = [7, 7, 7]
                else:
                    while True:
                        target_middle = [random.randint(1, 9) for _ in range(3)]
                        if target_middle != [7, 7, 7]:
                            break

                spin_message, final_columns = await self._animate_roleta_spin(message, target_middle=target_middle)

                if final_columns is None:
                    final_columns = [
                        self._build_roleta_column(target_middle[0]),
                        self._build_roleta_column(target_middle[1]),
                        self._build_roleta_column(target_middle[2]),
                    ]

                try:
                    board = self._render_roleta_board(final_columns)

                    if success:
                        chosen_channel = voice_channel
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
                        await self.db.add_user_chips(guild.id, message.author.id, ROLETA_JACKPOT_CHIPS)
                        await self.db.add_user_game_stat(guild.id, message.author.id, "roleta_jackpots", 1)
                        await self._grant_weekly_points(guild.id, message.author.id, 20)
                        summary = f"✨ A sorte sorriu para você. Você ganhou {self._chip_amount(ROLETA_JACKPOT_CHIPS)} e os alvos foram tirados da call."
                        if chip_note:
                            summary = f"{chip_note}\n{summary}"
                        embed = self._make_roleta_result_embed(
                            "💥🎰 JACKPOT!!",
                            summary,
                            board,
                            success=True,
                        )
                    else:
                        await self._record_game_played(guild.id, message.author.id, weekly_points=2)
                        summary = f"💨 Dessa vez a casa venceu. Você perdeu {self._chip_amount(ROLETA_COST)}."
                        if chip_note:
                            summary = f"{chip_note}\n{summary}"
                        embed = self._make_roleta_result_embed(
                            "🎰 Não foi dessa vez...",
                            summary,
                            board,
                            success=False,
                        )
                except Exception:
                    if success:
                        fallback_title = "💥🎰 JACKPOT!!"
                        fallback_text = f"Você ganhou {self._chip_amount(ROLETA_JACKPOT_CHIPS)} e os alvos foram tirados da call."
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
