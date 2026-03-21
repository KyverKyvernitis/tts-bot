import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

import discord

from config import GUILD_IDS


Card = tuple[int, str, str]

_HAND_NAMES = {
    8: "Straight Flush",
    7: "Quadra",
    6: "Full House",
    5: "Flush",
    4: "Sequência",
    3: "Trinca",
    2: "Dois Pares",
    1: "Par",
    0: "Carta Alta",
}


@dataclass
class PokerGame:
    guild_id: int
    channel_id: int
    host_id: int
    opponent_id: int
    status_message: discord.Message | None = None
    hands: dict[int, list[Card]] = field(default_factory=dict)
    original_hands: dict[int, list[Card]] = field(default_factory=dict)
    deck: list[Card] = field(default_factory=list)
    selected: dict[int, set[int]] = field(default_factory=dict)
    confirmed: dict[int, bool] = field(default_factory=dict)
    dm_messages: dict[int, discord.Message] = field(default_factory=dict)
    views: dict[int, "PokerSelectionView"] = field(default_factory=dict)
    finished: bool = False

    @property
    def players(self) -> tuple[int, int]:
        return (self.host_id, self.opponent_id)


class PokerSelectionView(discord.ui.View):
    def __init__(self, cog: "AntiMzkPokerMixin", game: PokerGame, player_id: int, *, timeout: float = 120.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.game = game
        self.player_id = player_id
        self.position_buttons: list[discord.ui.Button] = []
        for idx in range(5):
            button = discord.ui.Button(label=str(idx + 1), style=discord.ButtonStyle.secondary, row=0)
            button.callback = self._make_toggle_callback(idx)
            self.position_buttons.append(button)
            self.add_item(button)

        self.clear_button = discord.ui.Button(label="Limpar", style=discord.ButtonStyle.danger, row=1)
        self.clear_button.callback = self._clear_selection
        self.add_item(self.clear_button)

        self.confirm_button = discord.ui.Button(label="Confirmar troca", style=discord.ButtonStyle.success, row=1)
        self.confirm_button.callback = self._confirm_selection
        self.add_item(self.confirm_button)
        self.refresh_buttons()

    def _make_toggle_callback(self, index: int):
        async def _callback(interaction: discord.Interaction):
            await self.cog._handle_poker_toggle(interaction, self.game, self.player_id, index)
        return _callback

    async def _clear_selection(self, interaction: discord.Interaction):
        await self.cog._handle_poker_clear(interaction, self.game, self.player_id)

    async def _confirm_selection(self, interaction: discord.Interaction):
        await self.cog._handle_poker_confirm(interaction, self.game, self.player_id)

    def refresh_buttons(self):
        selected = self.game.selected.get(self.player_id, set())
        confirmed = self.game.confirmed.get(self.player_id, False)
        for idx, button in enumerate(self.position_buttons):
            button.style = discord.ButtonStyle.primary if idx in selected else discord.ButtonStyle.secondary
            button.disabled = confirmed
        self.clear_button.disabled = confirmed or not selected
        self.confirm_button.disabled = confirmed
        self.confirm_button.label = "Troca confirmada" if confirmed else "Confirmar troca"

    async def on_timeout(self):
        try:
            await self.cog._cancel_poker_game(self.game, reason="timeout", notice="A partida de poker expirou por falta de resposta nas DMs.")
        except Exception:
            pass


class AntiMzkPokerMixin:
    def _create_poker_deck(self) -> list[Card]:
        suits = ["♠", "♥", "♦", "♣"]
        rank_labels = {11: "J", 12: "Q", 13: "K", 14: "A"}
        deck: list[Card] = []
        for suit in suits:
            for value in range(2, 15):
                deck.append((value, rank_labels.get(value, str(value)), suit))
        random.shuffle(deck)
        return deck

    def _deal_poker_hand(self, deck: list[Card], count: int = 5) -> list[Card]:
        hand: list[Card] = []
        for _ in range(count):
            hand.append(deck.pop())
        return hand

    def _format_card(self, card: Card) -> str:
        return f"{card[1]}{card[2]}"

    def _format_hand(self, hand: list[Card]) -> str:
        return "  ".join(f"{idx + 1}: {self._format_card(card)}" for idx, card in enumerate(hand))

    def _evaluate_poker_hand(self, hand: list[Card]) -> tuple[int, list[int]]:
        values = sorted((card[0] for card in hand), reverse=True)
        suits = [card[2] for card in hand]
        counts: dict[int, int] = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        ordered = sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)

        unique_values = sorted(set(values))
        straight = False
        straight_high = max(values)
        if len(unique_values) == 5:
            if max(unique_values) - min(unique_values) == 4:
                straight = True
                straight_high = max(unique_values)
            elif unique_values == [2, 3, 4, 5, 14]:
                straight = True
                straight_high = 5

        flush = len(set(suits)) == 1

        if straight and flush:
            return (8, [straight_high])
        if ordered[0][1] == 4:
            four = ordered[0][0]
            kicker = ordered[1][0]
            return (7, [four, kicker])
        if ordered[0][1] == 3 and ordered[1][1] == 2:
            return (6, [ordered[0][0], ordered[1][0]])
        if flush:
            return (5, values)
        if straight:
            return (4, [straight_high])
        if ordered[0][1] == 3:
            trips = ordered[0][0]
            kickers = sorted((value for value, count in counts.items() if count == 1), reverse=True)
            return (3, [trips, *kickers])
        if ordered[0][1] == 2 and ordered[1][1] == 2:
            pairs = sorted((value for value, count in counts.items() if count == 2), reverse=True)
            kicker = max(value for value, count in counts.items() if count == 1)
            return (2, [*pairs, kicker])
        if ordered[0][1] == 2:
            pair = ordered[0][0]
            kickers = sorted((value for value, count in counts.items() if count == 1), reverse=True)
            return (1, [pair, *kickers])
        return (0, values)

    def _poker_hand_name(self, hand: list[Card]) -> str:
        score, _ = self._evaluate_poker_hand(hand)
        return _HAND_NAMES[score]

    def _compare_poker_hands(self, hand_a: list[Card], hand_b: list[Card]) -> int:
        eval_a = self._evaluate_poker_hand(hand_a)
        eval_b = self._evaluate_poker_hand(hand_b)
        if eval_a > eval_b:
            return 1
        if eval_b > eval_a:
            return -1
        return 0

    def _make_poker_status_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        return self._make_embed(title, description, ok=ok)

    def _make_poker_dm_embed(self, member: discord.Member, hand: list[Card], selected: set[int], confirmed: bool) -> discord.Embed:
        selection_text = (
            "Nenhuma carta marcada." if not selected else "Selecionadas para troca: " + ", ".join(str(i + 1) for i in sorted(selected))
        )
        status_text = "Troca confirmada. Aguardando o outro jogador." if confirmed else "Você pode trocar até 3 cartas e então confirmar."
        embed = discord.Embed(
            title="🃏 Sua mão de poker",
            description=f"**{member.display_name}**\n{self._format_hand(hand)}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Seleção", value=selection_text, inline=False)
        embed.add_field(name="Status", value=status_text, inline=False)
        return embed

    def _make_poker_dm_final_embed(self, player: discord.Member, player_hand: list[Card], opponent: discord.Member, opponent_hand: list[Card], result_text: str) -> discord.Embed:
        embed = discord.Embed(title="🃏 Resultado da rodada", description=result_text, color=discord.Color.green())
        embed.add_field(
            name=f"{player.display_name} — {self._poker_hand_name(player_hand)}",
            value=self._format_hand(player_hand),
            inline=False,
        )
        embed.add_field(
            name=f"{opponent.display_name} — {self._poker_hand_name(opponent_hand)}",
            value=self._format_hand(opponent_hand),
            inline=False,
        )
        return embed

    def _make_poker_result_embed(self, player_a: discord.Member, hand_a: list[Card], player_b: discord.Member, hand_b: list[Card], outcome: int) -> discord.Embed:
        if outcome > 0:
            title = "🃏 Vitória no poker"
            description = f"{player_a.mention} levou a rodada contra {player_b.mention}."
            ok = True
        elif outcome < 0:
            title = "🃏 Vitória no poker"
            description = f"{player_b.mention} levou a rodada contra {player_a.mention}."
            ok = True
        else:
            title = "🃏 Empate no poker"
            description = f"{player_a.mention} e {player_b.mention} terminaram empatados."
            ok = True

        embed = self._make_embed(title, description, ok=ok)
        embed.add_field(name=f"{player_a.display_name} — {self._poker_hand_name(hand_a)}", value=self._format_hand(hand_a), inline=False)
        embed.add_field(name=f"{player_b.display_name} — {self._poker_hand_name(hand_b)}", value=self._format_hand(hand_b), inline=False)
        return embed

    async def _disable_poker_views(self, game: PokerGame):
        for view in game.views.values():
            for child in view.children:
                child.disabled = True
            try:
                if game.dm_messages.get(view.player_id) is not None:
                    await game.dm_messages[view.player_id].edit(view=view)
            except Exception:
                pass
            try:
                view.stop()
            except Exception:
                pass

    async def _cancel_poker_game(self, game: PokerGame, *, reason: str, notice: str):
        if game.finished:
            return
        game.finished = True
        self._poker_games.pop(game.guild_id, None)
        await self._disable_poker_views(game)
        if game.status_message is not None:
            try:
                await game.status_message.edit(embed=self._make_poker_status_embed("🃏 Partida cancelada", notice, ok=False), view=None)
            except Exception:
                pass
        for user_id, dm_message in list(game.dm_messages.items()):
            try:
                await dm_message.edit(embed=self._make_poker_status_embed("🃏 Partida cancelada", notice, ok=False), view=None)
            except Exception:
                pass

    async def _finish_poker_game(self, game: PokerGame):
        if game.finished:
            return
        game.finished = True
        self._poker_games.pop(game.guild_id, None)

        for player_id in game.players:
            selected_positions = sorted(game.selected.get(player_id, set()))
            hand = game.hands[player_id]
            game.original_hands[player_id] = list(hand)
            for idx in selected_positions:
                hand[idx] = game.deck.pop()

        guild = self.bot.get_guild(game.guild_id)
        if guild is None:
            return
        player_a = guild.get_member(game.host_id)
        player_b = guild.get_member(game.opponent_id)
        if player_a is None or player_b is None:
            return

        outcome = self._compare_poker_hands(game.hands[player_a.id], game.hands[player_b.id])
        await self._disable_poker_views(game)

        if game.status_message is not None:
            try:
                await game.status_message.edit(
                    embed=self._make_poker_result_embed(player_a, game.hands[player_a.id], player_b, game.hands[player_b.id], outcome),
                    view=None,
                )
            except Exception:
                pass

        if outcome > 0:
            text_a = "Você venceu a rodada."
            text_b = "Você perdeu a rodada."
        elif outcome < 0:
            text_a = "Você perdeu a rodada."
            text_b = "Você venceu a rodada."
        else:
            text_a = text_b = "A rodada terminou empatada."

        player_map = {player_a.id: player_a, player_b.id: player_b}
        result_texts = {player_a.id: text_a, player_b.id: text_b}
        for player_id, dm_message in list(game.dm_messages.items()):
            opponent_id = game.opponent_id if player_id == game.host_id else game.host_id
            try:
                await dm_message.edit(
                    embed=self._make_poker_dm_final_embed(
                        player_map[player_id],
                        game.hands[player_id],
                        player_map[opponent_id],
                        game.hands[opponent_id],
                        result_texts[player_id],
                    ),
                    view=None,
                )
            except Exception:
                pass

    async def _update_poker_dm(self, game: PokerGame, player_id: int):
        guild = self.bot.get_guild(game.guild_id)
        if guild is None:
            return
        member = guild.get_member(player_id)
        dm_message = game.dm_messages.get(player_id)
        view = game.views.get(player_id)
        if member is None or dm_message is None or view is None:
            return
        view.refresh_buttons()
        try:
            await dm_message.edit(
                embed=self._make_poker_dm_embed(member, game.hands[player_id], game.selected.get(player_id, set()), game.confirmed.get(player_id, False)),
                view=view,
            )
        except Exception:
            pass

    async def _update_poker_status(self, game: PokerGame):
        if game.status_message is None or game.finished:
            return
        guild = self.bot.get_guild(game.guild_id)
        if guild is None:
            return
        host = guild.get_member(game.host_id)
        opponent = guild.get_member(game.opponent_id)
        if host is None or opponent is None:
            return

        confirmed_host = game.confirmed.get(game.host_id, False)
        confirmed_opponent = game.confirmed.get(game.opponent_id, False)
        if confirmed_host and confirmed_opponent:
            description = f"{host.mention} e {opponent.mention} confirmaram as trocas. Revelando o resultado..."
        else:
            host_status = "✅ pronto" if confirmed_host else "⌛ escolhendo"
            opp_status = "✅ pronto" if confirmed_opponent else "⌛ escolhendo"
            description = (
                f"Duelo entre {host.mention} e {opponent.mention}.\n"
                f"{host.display_name}: {host_status}\n"
                f"{opponent.display_name}: {opp_status}\n\n"
                "As cartas foram enviadas por DM. Cada jogador pode trocar até 3 cartas."
            )
        try:
            await game.status_message.edit(embed=self._make_poker_status_embed("🃏 Poker em andamento", description, ok=True), view=None)
        except Exception:
            pass

    async def _handle_poker_toggle(self, interaction: discord.Interaction, game: PokerGame, player_id: int, index: int):
        if interaction.user.id != player_id:
            await interaction.response.send_message("Essa mão não é sua.", ephemeral=True)
            return
        if game.finished or game.confirmed.get(player_id, False):
            await interaction.response.defer()
            return
        selected = game.selected.setdefault(player_id, set())
        if index in selected:
            selected.remove(index)
        else:
            if len(selected) >= 3:
                await interaction.response.send_message("Você pode trocar no máximo 3 cartas.", ephemeral=True)
                return
            selected.add(index)
        await self._update_poker_dm(game, player_id)
        await interaction.response.defer()

    async def _handle_poker_clear(self, interaction: discord.Interaction, game: PokerGame, player_id: int):
        if interaction.user.id != player_id:
            await interaction.response.send_message("Essa mão não é sua.", ephemeral=True)
            return
        if game.finished or game.confirmed.get(player_id, False):
            await interaction.response.defer()
            return
        game.selected[player_id] = set()
        await self._update_poker_dm(game, player_id)
        await interaction.response.defer()

    async def _handle_poker_confirm(self, interaction: discord.Interaction, game: PokerGame, player_id: int):
        if interaction.user.id != player_id:
            await interaction.response.send_message("Essa mão não é sua.", ephemeral=True)
            return
        if game.finished or game.confirmed.get(player_id, False):
            await interaction.response.defer()
            return
        game.confirmed[player_id] = True
        await self._update_poker_dm(game, player_id)
        await self._update_poker_status(game)
        await interaction.response.defer()
        if all(game.confirmed.get(pid, False) for pid in game.players):
            await asyncio.sleep(1.2)
            await self._finish_poker_game(game)

    async def _handle_poker_trigger(self, message: discord.Message) -> bool:
        content = message.content or ""
        if "poker" not in content.lower():
            return False

        guild = message.guild
        if guild is None:
            return False
        if GUILD_IDS and guild.id not in GUILD_IDS:
            return True

        opponent = None
        for mentioned in message.mentions:
            if mentioned.id != message.author.id and not mentioned.bot:
                opponent = mentioned
                break

        if opponent is None:
            embed = self._make_poker_status_embed(
                "🃏 Duelo inválido",
                "Use a trigger como **poker @usuário** para iniciar uma partida interativa.",
                ok=False,
            )
            try:
                await message.channel.send(embed=embed)
            except Exception:
                pass
            return True

        if guild.id in self._poker_games:
            embed = self._make_poker_status_embed(
                "🃏 Partida em andamento",
                "Já existe uma partida de poker ativa neste servidor. Espere ela terminar para iniciar outra.",
                ok=False,
            )
            try:
                await message.channel.send(embed=embed)
            except Exception:
                pass
            return True

        if opponent.id == message.author.id:
            return True

        game = PokerGame(
            guild_id=guild.id,
            channel_id=message.channel.id,
            host_id=message.author.id,
            opponent_id=opponent.id,
        )
        game.deck = self._create_poker_deck()
        game.hands[message.author.id] = self._deal_poker_hand(game.deck)
        game.hands[opponent.id] = self._deal_poker_hand(game.deck)
        game.original_hands[message.author.id] = list(game.hands[message.author.id])
        game.original_hands[opponent.id] = list(game.hands[opponent.id])
        game.selected = {message.author.id: set(), opponent.id: set()}
        game.confirmed = {message.author.id: False, opponent.id: False}
        self._poker_games[guild.id] = game

        try:
            status_message = await message.channel.send(
                embed=self._make_poker_status_embed(
                    "🃏 Poker em andamento",
                    f"Duelo entre {message.author.mention} e {opponent.mention}. Enviando as mãos por DM...",
                    ok=True,
                )
            )
            game.status_message = status_message
        except Exception:
            self._poker_games.pop(guild.id, None)
            return True

        dm_failures: list[discord.Member] = []
        for player in (message.author, opponent):
            try:
                dm_message = await player.send("Carregando sua mão...")
                game.dm_messages[player.id] = dm_message
                view = PokerSelectionView(self, game, player.id)
                game.views[player.id] = view
                await dm_message.edit(
                    content=None,
                    embed=self._make_poker_dm_embed(player, game.hands[player.id], set(), False),
                    view=view,
                )
            except Exception:
                dm_failures.append(player)

        if dm_failures:
            failed_names = ", ".join(member.display_name for member in dm_failures)
            await self._cancel_poker_game(
                game,
                reason="dm_failed",
                notice=f"Não consegui enviar DM para: {failed_names}. Ativem as DMs do servidor e tentem de novo.",
            )
            return True

        await self._update_poker_status(game)
        return True
