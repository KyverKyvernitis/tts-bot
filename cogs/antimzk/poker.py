import asyncio
import random
from collections import Counter
from dataclasses import dataclass

import discord

from config import GUILD_IDS

from .constants import _POKER_WORD_RE


_RANK_ORDER = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "10": 10, "J": 11, "Q": 12, "K": 13, "A": 14}
_RANK_LABELS = {8: "Straight Flush", 7: "Quadra", 6: "Full House", 5: "Flush", 4: "Sequência", 3: "Trinca", 2: "Dois Pares", 1: "Par", 0: "Carta Alta"}
_SUITS = ["♠", "♥", "♦", "♣"]
_RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]


@dataclass(slots=True)
class PokerHandEval:
    category: int
    tiebreak: tuple[int, ...]


def _build_deck() -> list[tuple[str, str]]:
    return [(rank, suit) for suit in _SUITS for rank in _RANKS]


def _format_cards(cards: list[tuple[str, str]]) -> str:
    return "  ".join(f"`{rank}{suit}`" for rank, suit in cards)


def _rank_value(card: tuple[str, str]) -> int:
    return _RANK_ORDER[card[0]]


def _evaluate_hand(cards: list[tuple[str, str]]) -> PokerHandEval:
    values = sorted((_rank_value(card) for card in cards), reverse=True)
    suits = [suit for _, suit in cards]
    counts = Counter(values)
    ordered_counts = sorted(counts.items(), key=lambda item: (-item[1], -item[0]))

    unique_values = sorted(set(values), reverse=True)
    straight_high = 0
    if len(unique_values) == 5:
        if unique_values[0] - unique_values[-1] == 4:
            straight_high = unique_values[0]
        elif unique_values == [14, 5, 4, 3, 2]:
            straight_high = 5
    flush = len(set(suits)) == 1

    if flush and straight_high:
        return PokerHandEval(8, (straight_high,))
    if ordered_counts[0][1] == 4:
        four = ordered_counts[0][0]
        kicker = max(v for v in values if v != four)
        return PokerHandEval(7, (four, kicker))
    if ordered_counts[0][1] == 3 and ordered_counts[1][1] == 2:
        return PokerHandEval(6, (ordered_counts[0][0], ordered_counts[1][0]))
    if flush:
        return PokerHandEval(5, tuple(values))
    if straight_high:
        return PokerHandEval(4, (straight_high,))
    if ordered_counts[0][1] == 3:
        trips = ordered_counts[0][0]
        kickers = sorted((v for v in values if v != trips), reverse=True)
        return PokerHandEval(3, (trips, *kickers))
    if ordered_counts[0][1] == 2 and ordered_counts[1][1] == 2:
        pairs = sorted((ordered_counts[0][0], ordered_counts[1][0]), reverse=True)
        kicker = max(v for v in values if v not in pairs)
        return PokerHandEval(2, (*pairs, kicker))
    if ordered_counts[0][1] == 2:
        pair = ordered_counts[0][0]
        kickers = sorted((v for v in values if v != pair), reverse=True)
        return PokerHandEval(1, (pair, *kickers))
    return PokerHandEval(0, tuple(values))


def _compare_hands(first_cards: list[tuple[str, str]], second_cards: list[tuple[str, str]]) -> tuple[int, PokerHandEval, PokerHandEval]:
    first_eval = _evaluate_hand(first_cards)
    second_eval = _evaluate_hand(second_cards)
    first_key = (first_eval.category, *first_eval.tiebreak)
    second_key = (second_eval.category, *second_eval.tiebreak)
    if first_key > second_key:
        return 1, first_eval, second_eval
    if first_key < second_key:
        return -1, first_eval, second_eval
    return 0, first_eval, second_eval


class AntiMzkPokerMixin:
    def _make_poker_channel_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        return self._make_embed(title, description, ok=ok)

    def _make_poker_dm_embed(self, title: str, description: str, *, player_cards: list[tuple[str, str]] | None = None, opponent: discord.Member | None = None, reveal_line: str | None = None) -> discord.Embed:
        lines = [description]
        if opponent is not None:
            lines.append("")
            lines.append(f"**Adversário**\n{opponent.mention}")
        if player_cards is not None:
            lines.append("")
            lines.append(f"**Suas cartas**\n{_format_cards(player_cards)}")
        if reveal_line:
            lines.append("")
            lines.append(reveal_line)
        return discord.Embed(title=title, description="\n".join(lines), color=discord.Color.blurple())

    def _make_poker_result_embed(self, first_player: discord.Member, second_player: discord.Member, first_cards: list[tuple[str, str]], second_cards: list[tuple[str, str]], *, outcome: int, first_eval: PokerHandEval, second_eval: PokerHandEval) -> discord.Embed:
        if outcome > 0:
            title = "🃏 Vitória no duelo de poker"
            verdict = f"{first_player.mention} venceu {second_player.mention}."
            ok = True
        elif outcome < 0:
            title = "🃏 Vitória no duelo de poker"
            verdict = f"{second_player.mention} venceu {first_player.mention}."
            ok = True
        else:
            title = "🃏 Empate no duelo de poker"
            verdict = f"{first_player.mention} e {second_player.mention} empataram."
            ok = True
        description = (
            f"{verdict}\n\n"
            f"**{first_player.display_name}** — {_RANK_LABELS[first_eval.category]}\n{_format_cards(first_cards)}\n\n"
            f"**{second_player.display_name}** — {_RANK_LABELS[second_eval.category]}\n{_format_cards(second_cards)}"
        )
        return self._make_embed(title, description, ok=ok)

    async def _handle_poker_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False
        content = message.content or ""
        if not _POKER_WORD_RE.search(content):
            return False
        if GUILD_IDS and guild.id not in GUILD_IDS:
            return True
        if not self.db.anti_mzk_enabled(guild.id):
            return True
        if guild.id in self._poker_sessions:
            return True

        mentions = [member for member in message.mentions if isinstance(member, discord.Member) and not member.bot and member.id != message.author.id]
        opponent = mentions[0] if mentions else None
        if opponent is None:
            fail_embed = self._make_embed(
                "🃏 Duelo de poker inválido",
                f"{message.author.mention}, use **poker @usuário** para iniciar um duelo. O modo atual funciona apenas em duelo.",
                ok=False,
            )
            try:
                await message.channel.send(embed=fail_embed)
            except Exception:
                pass
            return True

        channel_embed = self._make_poker_channel_embed(
            "🃏 Duelo de poker iniciado",
            f"{message.author.mention} desafiou {opponent.mention}. Abrindo as mesas privadas...",
            ok=True,
        )
        try:
            game_message = await message.channel.send(embed=channel_embed)
        except Exception:
            game_message = None

        author_dm_message = None
        opponent_dm_message = None
        try:
            author_dm_message = await message.author.send(embed=self._make_poker_dm_embed(
                "🃏 Seu duelo de poker começou",
                f"Rodada iniciada a partir de {message.guild.name}. Aguarde a revelação no canal.",
                opponent=opponent,
            ))
            opponent_dm_message = await opponent.send(embed=self._make_poker_dm_embed(
                "🃏 Seu duelo de poker começou",
                f"{message.author.display_name} te desafiou em {message.guild.name}. Aguarde a revelação no canal.",
                opponent=message.author,
            ))
        except Exception:
            fail_embed = self._make_embed(
                "🃏 Não consegui abrir as DMs do duelo",
                f"{message.author.mention} e {opponent.mention} precisam estar com a DM habilitada para usar a trigger **poker** em duelo.",
                ok=False,
            )
            if game_message is not None:
                try:
                    await game_message.edit(embed=fail_embed)
                except Exception:
                    pass
            else:
                try:
                    await message.channel.send(embed=fail_embed)
                except Exception:
                    pass
            return True

        self._poker_sessions[guild.id] = {
            "author_id": message.author.id,
            "opponent_id": opponent.id,
            "channel_id": message.channel.id,
            "message_id": getattr(game_message, "id", 0),
            "author_dm_message_id": getattr(author_dm_message, "id", 0),
            "opponent_dm_message_id": getattr(opponent_dm_message, "id", 0),
        }

        try:
            await self._react_with_emoji(message, "🃏", keep=True)
            deck = _build_deck()
            random.shuffle(deck)
            author_cards = deck[:5]
            opponent_cards = deck[5:10]
            outcome, author_eval, opponent_eval = _compare_hands(author_cards, opponent_cards)

            await asyncio.sleep(0.8)
            if game_message is not None:
                try:
                    await game_message.edit(embed=self._make_poker_channel_embed(
                        "🃏 Duelo de poker iniciado",
                        f"As cartas foram enviadas por DM para {message.author.mention} e {opponent.mention}. Revelando a rodada...",
                        ok=True,
                    ))
                except Exception:
                    pass
            try:
                await author_dm_message.edit(embed=self._make_poker_dm_embed(
                    "🃏 Suas cartas chegaram",
                    "Sua mão está conectada ao duelo no canal.",
                    player_cards=author_cards,
                    opponent=opponent,
                ))
            except Exception:
                pass
            try:
                await opponent_dm_message.edit(embed=self._make_poker_dm_embed(
                    "🃏 Suas cartas chegaram",
                    "Sua mão está conectada ao duelo no canal.",
                    player_cards=opponent_cards,
                    opponent=message.author,
                ))
            except Exception:
                pass

            await asyncio.sleep(1.2)
            result_embed = self._make_poker_result_embed(
                message.author,
                opponent,
                author_cards,
                opponent_cards,
                outcome=outcome,
                first_eval=author_eval,
                second_eval=opponent_eval,
            )
            if game_message is not None:
                try:
                    await game_message.edit(embed=result_embed)
                except Exception:
                    pass
            else:
                try:
                    await message.channel.send(embed=result_embed)
                except Exception:
                    pass

            author_result_line = (
                f"**Sua mão**: {_RANK_LABELS[author_eval.category]}\n"
                f"**Mão de {opponent.display_name}**: {_RANK_LABELS[opponent_eval.category]}\n"
                f"**Resultado final**: {'Vitória' if outcome > 0 else 'Derrota' if outcome < 0 else 'Empate'}"
            )
            opponent_result_line = (
                f"**Sua mão**: {_RANK_LABELS[opponent_eval.category]}\n"
                f"**Mão de {message.author.display_name}**: {_RANK_LABELS[author_eval.category]}\n"
                f"**Resultado final**: {'Vitória' if outcome < 0 else 'Derrota' if outcome > 0 else 'Empate'}"
            )
            try:
                await author_dm_message.edit(embed=self._make_poker_dm_embed(
                    "🃏 Resultado do duelo",
                    f"Cartas de {opponent.display_name}:\n{_format_cards(opponent_cards)}",
                    player_cards=author_cards,
                    opponent=opponent,
                    reveal_line=author_result_line,
                ))
            except Exception:
                pass
            try:
                await opponent_dm_message.edit(embed=self._make_poker_dm_embed(
                    "🃏 Resultado do duelo",
                    f"Cartas de {message.author.display_name}:\n{_format_cards(author_cards)}",
                    player_cards=opponent_cards,
                    opponent=message.author,
                    reveal_line=opponent_result_line,
                ))
            except Exception:
                pass
            return True
        finally:
            self._poker_sessions.pop(guild.id, None)
