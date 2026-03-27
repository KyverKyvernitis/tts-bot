
import asyncio
import random
import re
from dataclasses import dataclass, field

import discord

from config import GUILD_IDS

TRUCO_ENTRY = 10
TRUCO_BONUS_REWARD = 10
_TRUCO_INVITE_TIMEOUT = 60.0
_TRUCO_ACTION_TIMEOUT = 180.0

_TRUCO_RANKS = ["4", "5", "6", "7", "Q", "J", "K", "A", "2", "3"]
_TRUCO_SUITS = ["♦", "♠", "♥", "♣"]
_TRUCO_SUIT_STRENGTH = {"♦": 1, "♠": 2, "♥": 3, "♣": 4}
_TRUCO_PUBLIC_SUIT = {"♦": "♦️", "♠": "♠️", "♥": "♥️", "♣": "♣️"}
_TRUCO_LEVELS = [1, 3, 6, 9, 12]
_TRUCO_TARGET_POT = {1: 20, 3: 40, 6: 60, 9: 90, 12: 120}
_TRUCO_TARGET_CONTRIB = {1: 10, 3: 20, 6: 30, 9: 45, 12: 60}
_TRUCO_RAISE_NAMES = {3: "truco", 6: "seis", 9: "nove", 12: "doze"}
_TRUCO_TRIGGER_RE = re.compile(r"^\s*truco\s+<@!?(\d+)>\s*$", re.IGNORECASE)


@dataclass
class TrucoGame:
    guild_id: int
    channel_id: int
    challenger_id: int
    opponent_id: int
    level: int = 1
    status: str = "invite"
    status_text: str = "Aguardando resposta do desafio."
    turn_id: int | None = None
    hand_starter_id: int | None = None
    round_index: int = 0
    cards_on_table: dict[int, tuple[str, str]] = field(default_factory=dict)
    round_results: list[int | None] = field(default_factory=list)
    hands: dict[int, list[tuple[str, str]]] = field(default_factory=dict)
    table_history: list[str] = field(default_factory=list)
    status_message: discord.Message | None = None
    challenge_message: discord.Message | None = None
    vira: tuple[str, str] | None = None
    manilha_rank: str | None = None
    contribution: dict[int, int] = field(default_factory=dict)
    pot: int = _TRUCO_TARGET_POT[1]
    accepted: bool = False
    pending_raise_by: int | None = None
    pending_raise_to: int | None = None
    finished: bool = False

    @property
    def players(self) -> tuple[int, int]:
        return (self.challenger_id, self.opponent_id)

    def other_player(self, player_id: int) -> int:
        return self.opponent_id if int(player_id) == self.challenger_id else self.challenger_id


class TrucoChallengeView(discord.ui.View):
    def __init__(self, cog: "GincanaTrucoMixin", game: TrucoGame):
        super().__init__(timeout=_TRUCO_INVITE_TIMEOUT)
        self.cog = cog
        self.game = game

    @discord.ui.button(label="Aceitar", style=discord.ButtonStyle.success)
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_truco_accept(interaction, self.game)

    @discord.ui.button(label="Recusar", style=discord.ButtonStyle.danger)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_truco_decline(interaction, self.game)

    async def on_timeout(self):
        try:
            await self.cog._expire_truco_invite(self.game)
        except Exception:
            pass


class TrucoTableView(discord.ui.View):
    def __init__(self, cog: "GincanaTrucoMixin", game: TrucoGame):
        super().__init__(timeout=_TRUCO_ACTION_TIMEOUT)
        self.cog = cog
        self.game = game
        self.refresh_buttons()

    def refresh_buttons(self):
        self.clear_items()
        btn = discord.ui.Button(label="Minha mão", style=discord.ButtonStyle.primary)
        btn.callback = self._my_hand
        self.add_item(btn)
        if self.game.finished:
            return
        if self.game.status == "awaiting_raise_response":
            a = discord.ui.Button(label="Aceitar", style=discord.ButtonStyle.success)
            a.callback = self._accept_raise
            self.add_item(a)
            nxt = self.cog._truco_next_raise_label(self.game)
            if nxt:
                b = discord.ui.Button(label=f"Pedir {nxt}", style=discord.ButtonStyle.secondary)
                b.callback = self._raise_action
                self.add_item(b)
            c = discord.ui.Button(label="Correr", style=discord.ButtonStyle.danger)
            c.callback = self._run_action
            self.add_item(c)
            return
        nxt = self.cog._truco_next_raise_label(self.game)
        if nxt:
            b = discord.ui.Button(label=f"Pedir {nxt}", style=discord.ButtonStyle.secondary)
            b.callback = self._raise_action
            self.add_item(b)
        c = discord.ui.Button(label="Correr", style=discord.ButtonStyle.danger)
        c.callback = self._run_action
        self.add_item(c)

    async def _my_hand(self, interaction: discord.Interaction):
        await self.cog._handle_truco_show_hand(interaction, self.game)

    async def _raise_action(self, interaction: discord.Interaction):
        await self.cog._handle_truco_raise(interaction, self.game)

    async def _run_action(self, interaction: discord.Interaction):
        await self.cog._handle_truco_run(interaction, self.game)

    async def _accept_raise(self, interaction: discord.Interaction):
        await self.cog._handle_truco_accept_raise(interaction, self.game)

    async def on_timeout(self):
        try:
            if self.game.finished:
                return
            loser = self.game.turn_id or self.game.opponent_id
            await self.cog._finish_truco_game(self.game, winner_id=self.game.other_player(loser), loser_id=loser, reason="tempo esgotado")
        except Exception:
            pass


class TrucoHandView(discord.ui.View):
    def __init__(self, cog: "GincanaTrucoMixin", game: TrucoGame, player_id: int):
        super().__init__(timeout=90.0)
        self.cog = cog
        self.game = game
        self.player_id = int(player_id)
        self.refresh_buttons()

    def refresh_buttons(self):
        self.clear_items()
        for index, card in enumerate(self.game.hands.get(self.player_id, [])):
            button = discord.ui.Button(label=self.cog._truco_card_display(card), style=discord.ButtonStyle.secondary, row=index // 3)
            async def _callback(interaction: discord.Interaction, idx=index):
                await self.cog._handle_truco_play_card(interaction, self.game, self.player_id, idx)
            button.callback = _callback
            self.add_item(button)


class GincanaTrucoMixin:
    def _truco_create_deck(self):
        deck = [(rank, suit) for rank in _TRUCO_RANKS for suit in _TRUCO_SUITS]
        random.shuffle(deck)
        return deck

    def _truco_card_display(self, card):
        return f"{card[0]}{card[1]}" if card else "—"

    def _truco_card_public_display(self, card):
        return f"{card[0]}{_TRUCO_PUBLIC_SUIT.get(card[1], card[1])}" if card else "—"

    def _truco_manilha_rank(self, vira_rank: str) -> str:
        return _TRUCO_RANKS[(_TRUCO_RANKS.index(vira_rank) + 1) % len(_TRUCO_RANKS)]

    def _truco_compare_cards(self, card_a, card_b, manilha_rank: str) -> int:
        ra, sa = card_a
        rb, sb = card_b
        am = ra == manilha_rank
        bm = rb == manilha_rank
        if am and bm:
            return (1 if _TRUCO_SUIT_STRENGTH[sa] > _TRUCO_SUIT_STRENGTH[sb] else -1 if _TRUCO_SUIT_STRENGTH[sa] < _TRUCO_SUIT_STRENGTH[sb] else 0)
        if am:
            return 1
        if bm:
            return -1
        ia = _TRUCO_RANKS.index(ra)
        ib = _TRUCO_RANKS.index(rb)
        return 1 if ia > ib else -1 if ia < ib else 0

    def _truco_next_raise_level(self, current: int):
        try:
            i = _TRUCO_LEVELS.index(int(current))
        except ValueError:
            return None
        return _TRUCO_LEVELS[i + 1] if i + 1 < len(_TRUCO_LEVELS) else None

    def _truco_next_raise_label(self, game: TrucoGame):
        base = game.pending_raise_to if game.status == "awaiting_raise_response" and game.pending_raise_to else game.level
        nxt = self._truco_next_raise_level(base)
        return _TRUCO_RAISE_NAMES.get(nxt) if nxt else None

    def _truco_round_label(self, idx: int) -> str:
        return ["1ª", "2ª", "3ª"][max(0, min(2, idx))]

    def _truco_target_contrib(self, level: int) -> int:
        return int(_TRUCO_TARGET_CONTRIB[int(level)])

    def _truco_target_pot(self, level: int) -> int:
        return int(_TRUCO_TARGET_POT[int(level)])

    def _truco_member_name(self, guild, uid: int) -> str:
        member = guild.get_member(uid) if guild else None
        return member.display_name if member else f"Usuário {uid}"

    def _truco_member_mention(self, guild, uid: int) -> str:
        member = guild.get_member(uid) if guild else None
        return member.mention if member else f"<@{uid}>"

    def _truco_status_embed(self, game: TrucoGame, title: str = "🃏 Truco Paulista") -> discord.Embed:
        guild = self.bot.get_guild(game.guild_id)
        a = self._truco_member_name(guild, game.challenger_id)
        b = self._truco_member_name(guild, game.opponent_id)
        embed = discord.Embed(title=title, description=f"**{a}** vs **{b}**\nPote atual: **{game.pot}** {self._CHIP_EMOJI}", color=discord.Color.dark_green())
        embed.add_field(name="Vira", value=self._truco_card_public_display(game.vira), inline=True)
        embed.add_field(name="Manilha", value=str(game.manilha_rank or "—"), inline=True)
        wa = sum(1 for x in game.round_results if x == game.challenger_id)
        wb = sum(1 for x in game.round_results if x == game.opponent_id)
        wt = sum(1 for x in game.round_results if x is None)
        placar = f"{a}: **{wa}**\n{b}: **{wb}**"
        if wt:
            placar += f"\nEmpates: **{wt}**"
        embed.add_field(name="Vazas", value=placar, inline=True)
        mesa = []
        mesa.extend(game.table_history[-3:])
        mesa.append(f"{a}: {self._truco_card_public_display(game.cards_on_table.get(game.challenger_id))}")
        mesa.append(f"{b}: {self._truco_card_public_display(game.cards_on_table.get(game.opponent_id))}")
        embed.add_field(name="Mesa", value="\n".join(mesa), inline=False)
        embed.add_field(name="Status", value=game.status_text, inline=False)
        embed.set_footer(text="Use o botão Minha mão para jogar")
        return embed

    async def _truco_safe_edit(self, message, *, embed=None, view=None):
        if message is None:
            return
        try:
            await message.edit(embed=embed, view=view)
        except Exception:
            pass

    async def _truco_show_turn(self, game: TrucoGame):
        if game.finished:
            return
        guild = self.bot.get_guild(game.guild_id)
        game.status_text = f"Vez de {self._truco_member_mention(guild, game.turn_id)}"
        await self._truco_safe_edit(game.status_message, embed=self._truco_status_embed(game), view=TrucoTableView(self, game))

    def _truco_hand_winner(self, results: list[int | None], hand_starter_id: int):
        if len(results) < 2:
            return None
        r1 = results[0]
        r2 = results[1]
        if len(results) == 2:
            if r1 is None and r2 is None:
                return hand_starter_id
            if r1 is None:
                return r2
            if r2 is None:
                return r1
            if r1 == r2:
                return r1
            return None
        r3 = results[2]
        return r3 if r3 is not None else r2 if r2 is not None else r1 if r1 is not None else hand_starter_id

    async def _truco_resolve_round(self, game: TrucoGame):
        a = game.challenger_id
        b = game.opponent_id
        ca = game.cards_on_table.get(a)
        cb = game.cards_on_table.get(b)
        if not ca or not cb:
            return
        game.status_text = "Resolvendo a vaza..."
        await self._truco_safe_edit(game.status_message, embed=self._truco_status_embed(game), view=TrucoTableView(self, game))
        await asyncio.sleep(0.8)
        cmp = self._truco_compare_cards(ca, cb, game.manilha_rank or "")
        winner = a if cmp > 0 else b if cmp < 0 else None
        label = self._truco_round_label(game.round_index)
        guild = self.bot.get_guild(game.guild_id)
        if winner is None:
            game.status_text = f"{label} vaza empatou."
            game.table_history.append(game.status_text)
        else:
            game.status_text = f"{self._truco_member_mention(guild, winner)} levou a {label.lower()} vaza."
            game.table_history.append(game.status_text)
        game.round_results.append(winner)
        await self._truco_safe_edit(game.status_message, embed=self._truco_status_embed(game), view=TrucoTableView(self, game))
        await asyncio.sleep(0.8)
        hand_winner = self._truco_hand_winner(game.round_results, game.hand_starter_id or game.challenger_id)
        game.cards_on_table.clear()
        game.round_index += 1
        if hand_winner is not None or len(game.round_results) >= 3:
            await self._finish_truco_game(game, winner_id=(hand_winner or game.hand_starter_id or game.challenger_id), loser_id=game.other_player(hand_winner or game.hand_starter_id or game.challenger_id), reason="mão encerrada")
            return
        game.turn_id = winner if winner is not None else (game.hand_starter_id or game.challenger_id)
        await self._truco_show_turn(game)

    async def _finish_truco_game(self, game: TrucoGame, *, winner_id: int, loser_id: int, reason: str):
        if game.finished:
            return
        game.finished = True
        game.status = "finished"
        self._truco_games.pop(game.guild_id, None)
        await self.db.add_user_game_stat(game.guild_id, winner_id, "truco_wins", 1)
        await self.db.add_user_game_stat(game.guild_id, loser_id, "truco_losses", 1)
        await self._record_game_played(game.guild_id, winner_id, weekly_points=6)
        await self._record_game_played(game.guild_id, loser_id, weekly_points=2)
        await self._change_user_chips(game.guild_id, winner_id, game.pot, mark_activity=True)
        await self._change_user_bonus_chips(game.guild_id, winner_id, TRUCO_BONUS_REWARD, mark_activity=True)
        guild = self.bot.get_guild(game.guild_id)
        winner = self._truco_member_mention(guild, winner_id)
        game.status_text = f"{winner} venceu a mão."
        embed = self._truco_status_embed(game, title="🃏 Truco encerrado")
        extra = f"\n{winner} levou **{game.pot}** {self._CHIP_GAIN_EMOJI} e ganhou **+{TRUCO_BONUS_REWARD}** {self._CHIP_BONUS_EMOJI}."
        if reason == "correu":
            embed.description += "\nA mão terminou porque alguém correu." + extra
        elif reason == "tempo esgotado":
            embed.description += "\nA mão terminou por tempo esgotado." + extra
        else:
            embed.description += extra
        await self._truco_safe_edit(game.status_message, embed=embed, view=None)

    async def _expire_truco_invite(self, game: TrucoGame):
        if game.finished or game.accepted:
            return
        self._truco_games.pop(game.guild_id, None)
        game.finished = True
        await self._truco_safe_edit(game.challenge_message, embed=discord.Embed(title="🃏 Truco Paulista", description="O desafio expirou porque não foi aceito a tempo.", color=discord.Color.red()), view=None)

    async def _handle_truco_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None or (GUILD_IDS and guild.id not in GUILD_IDS):
            return False
        raw = str(message.content or "").strip()
        if not _TRUCO_TRIGGER_RE.match(raw):
            return False
        if guild.id in getattr(self, "_truco_games", {}):
            await message.channel.send(embed=self._make_embed("🃏 Truco ocupado", "Já existe uma mão de truco em andamento neste servidor.", ok=False))
            return True
        mentions = [m for m in message.mentions if not m.bot]
        if len(mentions) != 1:
            await message.channel.send(embed=self._make_embed("🃏 Truco Paulista", "Use `truco @usuário` para desafiar alguém.", ok=False))
            return True
        challenger = message.author
        opponent = mentions[0]
        if opponent.id == challenger.id:
            await message.channel.send(embed=self._make_embed("🃏 Truco Paulista", "Você precisa desafiar outra pessoa.", ok=False))
            return True
        ok, _current, note = await self._ensure_action_chips(guild.id, challenger.id, TRUCO_ENTRY)
        if not ok:
            await message.channel.send(embed=self._make_embed("🃏 Truco Paulista", note or "Você não pode entrar agora.", ok=False))
            return True
        if self._needs_negative_confirmation(guild.id, challenger.id, TRUCO_ENTRY):
            confirmed = await self._confirm_negative_from_message(message, guild.id, challenger.id, TRUCO_ENTRY, title="⚠️ Confirmar truco")
            if not confirmed:
                return True
        game = TrucoGame(guild_id=guild.id, channel_id=message.channel.id, challenger_id=challenger.id, opponent_id=opponent.id, hand_starter_id=challenger.id, turn_id=challenger.id)
        game.contribution = {challenger.id: TRUCO_ENTRY, opponent.id: TRUCO_ENTRY}
        self._truco_games[guild.id] = game
        embed = discord.Embed(title="🃏 Truco Paulista", description=f"{challenger.mention} desafiou {opponent.mention} no truco.\nEntrada: **{TRUCO_ENTRY}** {self._CHIP_EMOJI} cada.", color=discord.Color.dark_green())
        embed.add_field(name="Status", value=f"Aguardando {opponent.mention} aceitar ou recusar.", inline=False)
        view = TrucoChallengeView(self, game)
        sent = await message.channel.send(embed=embed, view=view)
        game.challenge_message = sent
        return True

    async def _handle_truco_decline(self, interaction: discord.Interaction, game: TrucoGame):
        if interaction.user.id != game.opponent_id:
            await interaction.response.send_message("Esse desafio não é para você.", ephemeral=True)
            return
        self._truco_games.pop(game.guild_id, None)
        game.finished = True
        await interaction.response.edit_message(embed=discord.Embed(title="🃏 Truco Paulista", description="O desafio foi recusado.", color=discord.Color.red()), view=None)

    async def _handle_truco_accept(self, interaction: discord.Interaction, game: TrucoGame):
        if interaction.user.id != game.opponent_id:
            await interaction.response.send_message("Esse desafio não é para você.", ephemeral=True)
            return
        ok, _current, note = await self._ensure_action_chips(game.guild_id, interaction.user.id, TRUCO_ENTRY)
        if not ok:
            await interaction.response.send_message(note or "Você não pode aceitar agora.", ephemeral=True)
            return
        if self._needs_negative_confirmation(game.guild_id, interaction.user.id, TRUCO_ENTRY):
            confirmed = await self._confirm_negative_ephemeral(interaction, game.guild_id, interaction.user.id, TRUCO_ENTRY, title="⚠️ Confirmar truco")
            if not confirmed:
                return
        await self._change_user_chips(game.guild_id, game.challenger_id, -TRUCO_ENTRY, mark_activity=True)
        await self._change_user_chips(game.guild_id, game.opponent_id, -TRUCO_ENTRY, mark_activity=True)
        deck = self._truco_create_deck()
        game.hands = {pid: [deck.pop(), deck.pop(), deck.pop()] for pid in game.players}
        game.vira = deck.pop()
        game.manilha_rank = self._truco_manilha_rank(game.vira[0])
        game.accepted = True
        game.status = "active"
        game.pot = self._truco_target_pot(game.level)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Duelo aceito.", ephemeral=True)
            else:
                await interaction.response.send_message("Duelo aceito.", ephemeral=True)
        except Exception:
            pass
        await self._truco_safe_edit(game.challenge_message, embed=discord.Embed(title="🃏 Truco Paulista", description="As cartas foram distribuídas.", color=discord.Color.dark_green()), view=None)
        game.status_message = await interaction.channel.send(embed=self._truco_status_embed(game), view=TrucoTableView(self, game))
        game.status_text = "Distribuindo as cartas..."
        await self._truco_safe_edit(game.status_message, embed=self._truco_status_embed(game), view=TrucoTableView(self, game))
        await asyncio.sleep(0.8)
        game.status_text = "Virando a carta..."
        await self._truco_safe_edit(game.status_message, embed=self._truco_status_embed(game), view=TrucoTableView(self, game))
        await asyncio.sleep(0.8)
        await self._truco_show_turn(game)

    async def _handle_truco_show_hand(self, interaction: discord.Interaction, game: TrucoGame):
        if interaction.user.id not in game.players:
            await interaction.response.send_message("Essa mão não é sua.", ephemeral=True)
            return
        cards = game.hands.get(interaction.user.id, [])
        if not cards:
            await interaction.response.send_message("Você já jogou todas as cartas desta mão.", ephemeral=True)
            return
        embed = discord.Embed(title="🃏 Sua mão", description=f"Vira: **{self._truco_card_public_display(game.vira)}**\nManilha: **{game.manilha_rank}**\nPote atual: **{game.pot}** {self._CHIP_EMOJI}", color=discord.Color.blurple())
        embed.add_field(name="Cartas", value="\n".join(f"• {self._truco_card_display(card)}" for card in cards), inline=False)
        if game.status == "awaiting_raise_response":
            embed.add_field(name="Status", value="Aguardando resposta para o aumento da mão.", inline=False)
        elif interaction.user.id != game.turn_id:
            embed.add_field(name="Status", value="Aguarde sua vez para jogar.", inline=False)
        else:
            embed.add_field(name="Status", value="Escolha uma carta para jogar.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, view=TrucoHandView(self, game, interaction.user.id))

    async def _handle_truco_play_card(self, interaction: discord.Interaction, game: TrucoGame, player_id: int, card_index: int):
        if interaction.user.id != player_id:
            await interaction.response.send_message("Essa mão não é sua.", ephemeral=True)
            return
        if game.finished or game.status != "active":
            await interaction.response.send_message("Essa mão não está pronta para jogada agora.", ephemeral=True)
            return
        if player_id != game.turn_id:
            await interaction.response.send_message("Ainda não é a sua vez.", ephemeral=True)
            return
        hand = game.hands.get(player_id, [])
        if card_index < 0 or card_index >= len(hand):
            await interaction.response.send_message("Essa carta não está mais disponível.", ephemeral=True)
            return
        card = hand.pop(card_index)
        guild = self.bot.get_guild(game.guild_id)
        game.status_text = f"{self._truco_member_mention(guild, player_id)} jogou uma carta..."
        game.cards_on_table[player_id] = card
        await interaction.response.edit_message(content="Carta jogada.", embed=None, view=None)
        await self._truco_safe_edit(game.status_message, embed=self._truco_status_embed(game), view=TrucoTableView(self, game))
        await asyncio.sleep(0.8)
        if len(game.cards_on_table) < 2:
            game.turn_id = game.other_player(player_id)
            await self._truco_show_turn(game)
            return
        await self._truco_resolve_round(game)

    async def _handle_truco_raise(self, interaction: discord.Interaction, game: TrucoGame):
        if interaction.user.id not in game.players:
            await interaction.response.send_message("Essa mão não é sua.", ephemeral=True)
            return
        if game.finished:
            await interaction.response.send_message("A mão já terminou.", ephemeral=True)
            return
        if game.status == "awaiting_raise_response":
            if interaction.user.id != game.other_player(game.pending_raise_by or 0):
                await interaction.response.send_message("Aguardando o outro jogador responder o aumento atual.", ephemeral=True)
                return
            base = game.pending_raise_to or game.level
        else:
            if interaction.user.id != game.turn_id:
                await interaction.response.send_message("Você só pode pedir aumento na sua vez.", ephemeral=True)
                return
            base = game.level
        nxt = self._truco_next_raise_level(base)
        if not nxt:
            await interaction.response.send_message("Essa mão já chegou no máximo.", ephemeral=True)
            return
        target_contrib = self._truco_target_contrib(nxt)
        current_contrib = int(game.contribution.get(interaction.user.id, TRUCO_ENTRY))
        delta = max(0, target_contrib - current_contrib)
        ok, _cur, note = await self._ensure_action_chips(game.guild_id, interaction.user.id, delta)
        if not ok:
            await interaction.response.send_message(note or "Você não pode subir a mão agora.", ephemeral=True)
            return
        if delta > 0 and self._needs_negative_confirmation(game.guild_id, interaction.user.id, delta):
            confirmed = await self._confirm_negative_ephemeral(interaction, game.guild_id, interaction.user.id, delta, title="⚠️ Confirmar aumento")
            if not confirmed:
                return
        guild = self.bot.get_guild(game.guild_id)
        game.status = "awaiting_raise_response"
        game.pending_raise_by = interaction.user.id
        game.pending_raise_to = nxt
        game.status_text = f"{self._truco_member_mention(guild, interaction.user.id)} pediu {_TRUCO_RAISE_NAMES[nxt]}."
        if interaction.response.is_done():
            await interaction.followup.send("Pedido enviado.", ephemeral=True)
        else:
            await interaction.response.send_message("Pedido enviado.", ephemeral=True)
        await self._truco_safe_edit(game.status_message, embed=self._truco_status_embed(game), view=TrucoTableView(self, game))

    async def _handle_truco_accept_raise(self, interaction: discord.Interaction, game: TrucoGame):
        if game.status != "awaiting_raise_response" or not game.pending_raise_to or not game.pending_raise_by:
            await interaction.response.send_message("Não há aumento pendente agora.", ephemeral=True)
            return
        target_id = game.other_player(game.pending_raise_by)
        if interaction.user.id != target_id:
            await interaction.response.send_message("A resposta desse aumento não é sua.", ephemeral=True)
            return
        target_level = game.pending_raise_to
        target_contrib = self._truco_target_contrib(target_level)
        deltas = {}
        for pid in game.players:
            deltas[pid] = max(0, target_contrib - int(game.contribution.get(pid, TRUCO_ENTRY)))
        my_delta = deltas[interaction.user.id]
        ok, _cur, note = await self._ensure_action_chips(game.guild_id, interaction.user.id, my_delta)
        if not ok:
            await interaction.response.send_message(note or "Você não pode aceitar esse aumento agora.", ephemeral=True)
            return
        if my_delta > 0 and self._needs_negative_confirmation(game.guild_id, interaction.user.id, my_delta):
            confirmed = await self._confirm_negative_ephemeral(interaction, game.guild_id, interaction.user.id, my_delta, title="⚠️ Confirmar aumento")
            if not confirmed:
                return
        for pid, delta in deltas.items():
            if delta > 0:
                await self._change_user_chips(game.guild_id, pid, -delta, mark_activity=True)
                game.contribution[pid] = int(game.contribution.get(pid, TRUCO_ENTRY)) + delta
        game.level = target_level
        game.pot = self._truco_target_pot(target_level)
        game.status = "active"
        game.pending_raise_by = None
        game.pending_raise_to = None
        guild = self.bot.get_guild(game.guild_id)
        game.status_text = f"{self._truco_member_mention(guild, interaction.user.id)} aceitou. A mão subiu para {target_level}."
        if interaction.response.is_done():
            await interaction.followup.send("Aumento aceito.", ephemeral=True)
        else:
            await interaction.response.send_message("Aumento aceito.", ephemeral=True)
        await self._truco_safe_edit(game.status_message, embed=self._truco_status_embed(game), view=TrucoTableView(self, game))

    async def _handle_truco_run(self, interaction: discord.Interaction, game: TrucoGame):
        if interaction.user.id not in game.players:
            await interaction.response.send_message("Essa mão não é sua.", ephemeral=True)
            return
        loser = interaction.user.id
        winner = game.other_player(loser)
        if interaction.response.is_done():
            await interaction.followup.send("Você correu.", ephemeral=True)
        else:
            await interaction.response.send_message("Você correu.", ephemeral=True)
        await self._finish_truco_game(game, winner_id=winner, loser_id=loser, reason="correu")
