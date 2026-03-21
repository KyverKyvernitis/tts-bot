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


def _compare_hands(player_cards: list[tuple[str, str]], dealer_cards: list[tuple[str, str]]) -> tuple[int, PokerHandEval, PokerHandEval]:
    player_eval = _evaluate_hand(player_cards)
    dealer_eval = _evaluate_hand(dealer_cards)
    player_key = (player_eval.category, *player_eval.tiebreak)
    dealer_key = (dealer_eval.category, *dealer_eval.tiebreak)
    if player_key > dealer_key:
        return 1, player_eval, dealer_eval
    if player_key < dealer_key:
        return -1, player_eval, dealer_eval
    return 0, player_eval, dealer_eval


class AntiMzkPokerMixin:
    def _make_poker_channel_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        return self._make_embed(title, description, ok=ok)

    def _make_poker_dm_embed(self, title: str, description: str, *, reveal: bool = False, player_cards: list[tuple[str, str]] | None = None, result_line: str | None = None) -> discord.Embed:
        lines = [description]
        if player_cards is not None:
            lines.append("")
            lines.append(f"**Suas cartas**\n{_format_cards(player_cards)}")
        if reveal and result_line:
            lines.append("")
            lines.append(result_line)
        return discord.Embed(title=title, description="\n".join(lines), color=discord.Color.blurple())

    def _make_poker_result_embed(self, player: discord.Member, player_cards: list[tuple[str, str]], dealer_cards: list[tuple[str, str]], *, outcome: int, player_eval: PokerHandEval, dealer_eval: PokerHandEval) -> discord.Embed:
        if outcome > 0:
            title = "🃏 Você venceu no poker"
            verdict = f"{player.mention} levou a rodada."
            ok = True
        elif outcome < 0:
            title = "🃏 Dealer venceu no poker"
            verdict = f"O dealer levou a rodada contra {player.mention}."
            ok = False
        else:
            title = "🃏 Empate no poker"
            verdict = f"{player.mention} empatou com o dealer."
            ok = True
        description = (
            f"{verdict}\n\n"
            f"**{player.display_name}** — {_RANK_LABELS[player_eval.category]}\n{_format_cards(player_cards)}\n\n"
            f"**Dealer** — {_RANK_LABELS[dealer_eval.category]}\n{_format_cards(dealer_cards)}"
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

        channel_embed = self._make_poker_channel_embed(
            "🃏 Poker iniciado",
            f"{message.author.mention}, abrindo sua mesa privada...",
            ok=True,
        )
        try:
            game_message = await message.channel.send(embed=channel_embed)
        except Exception:
            game_message = None

        try:
            dm_embed = self._make_poker_dm_embed(
                "🃏 Mesa privada de poker",
                f"Rodada iniciada a partir de {message.guild.name}. Embaralhando as cartas...",
            )
            dm_message = await message.author.send(embed=dm_embed)
        except Exception:
            fail_embed = self._make_embed(
                "🃏 Não consegui abrir sua DM",
                f"{message.author.mention}, habilite mensagens diretas do servidor para usar a trigger **poker**.",
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
            "channel_id": message.channel.id,
            "message_id": getattr(game_message, "id", 0),
            "dm_message_id": getattr(dm_message, "id", 0),
        }

        try:
            await self._react_with_emoji(message, "🃏", keep=True)
            deck = _build_deck()
            random.shuffle(deck)
            player_cards = deck[:5]
            dealer_cards = deck[5:10]
            outcome, player_eval, dealer_eval = _compare_hands(player_cards, dealer_cards)

            await asyncio.sleep(0.8)
            if game_message is not None:
                try:
                    await game_message.edit(embed=self._make_poker_channel_embed(
                        "🃏 Poker iniciado",
                        f"{message.author.mention}, suas cartas foram enviadas por DM. O dealer está preparando a mão...",
                        ok=True,
                    ))
                except Exception:
                    pass
            try:
                await dm_message.edit(embed=self._make_poker_dm_embed(
                    "🃏 Suas cartas chegaram",
                    "A rodada está conectada ao canal. Aguarde o dealer concluir a revelação.",
                    player_cards=player_cards,
                ))
            except Exception:
                pass

            await asyncio.sleep(1.2)
            result_embed = self._make_poker_result_embed(
                message.author,
                player_cards,
                dealer_cards,
                outcome=outcome,
                player_eval=player_eval,
                dealer_eval=dealer_eval,
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

            dm_result = self._make_poker_dm_embed(
                "🃏 Resultado da rodada",
                f"Dealer: {_RANK_LABELS[dealer_eval.category]}\n{_format_cards(dealer_cards)}",
                reveal=True,
                player_cards=player_cards,
                result_line=(
                    f"**Seu resultado**: {_RANK_LABELS[player_eval.category]}\n"
                    f"**Resultado final**: {'Vitória' if outcome > 0 else 'Derrota' if outcome < 0 else 'Empate'}"
                ),
            )
            try:
                await dm_message.edit(embed=dm_result)
            except Exception:
                pass
            return True
        finally:
            self._poker_sessions.pop(guild.id, None)
