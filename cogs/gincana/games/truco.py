import asyncio
import random
import re
from dataclasses import dataclass, field

import discord

TRUCO_ENTRY = 10
TRUCO_BONUS_REWARD = 10
_TRUCO_INVITE_TIMEOUT = 60.0
_TRUCO_ACTION_TIMEOUT = 180.0
_TRUCO_LOBBY_TIMEOUT = 90.0

_TRUCO_RANKS = ["4", "5", "6", "7", "Q", "J", "K", "A", "2", "3"]
_TRUCO_SUITS = ["♦", "♠", "♥", "♣"]
_TRUCO_SUIT_STRENGTH = {"♦": 1, "♠": 2, "♥": 3, "♣": 4}
_TRUCO_PUBLIC_SUIT = {"♦": "♦️", "♠": "♠️", "♥": "♥️", "♣": "♣️"}
_TRUCO_LEVELS = [1, 3, 6, 9, 12]
_TRUCO_TARGET_POT_1V1 = {1: 20, 3: 40, 6: 60, 9: 90, 12: 120}
_TRUCO_TARGET_CONTRIB_1V1 = {1: 10, 3: 20, 6: 30, 9: 45, 12: 60}
_TRUCO_TARGET_POT_2V2 = {1: 40, 3: 80, 6: 120, 9: 180, 12: 240}
_TRUCO_TARGET_CONTRIB_2V2 = {1: 10, 3: 20, 6: 30, 9: 45, 12: 60}
_TRUCO_RAISE_NAMES = {3: "truco", 6: "seis", 9: "nove", 12: "doze"}
_TRUCO_TRIGGER_RE = re.compile(r"^\s*truco\s+<@!?(\d+)>\s*$", re.IGNORECASE)
_TRUCO_2V2_TRIGGER_RE = re.compile(r"^\s*(truco2|truco\s+2v2)\s*$", re.IGNORECASE)


@dataclass
class TrucoLobby:
    guild_id: int
    channel_id: int
    creator_id: int
    team_a: list[int] = field(default_factory=list)
    team_b: list[int] = field(default_factory=list)
    message: discord.Message | None = None
    started: bool = False


@dataclass
class TrucoGame:
    guild_id: int
    channel_id: int
    mode: str
    players_order: list[int]
    teams: list[tuple[int, ...]]
    level: int = 1
    status: str = "invite"
    status_text: str = "Esperando a resposta do desafio."
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
    pot: int = 0
    accepted: bool = False
    pending_raise_by: int | None = None
    pending_raise_to: int | None = None
    finished: bool = False
    dm_messages: dict[int, discord.Message] = field(default_factory=dict)

    @property
    def players(self) -> tuple[int, ...]:
        return tuple(self.players_order)


class TrucoChallengeView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaTrucoMixin", game: TrucoGame):
        super().__init__(timeout=_TRUCO_INVITE_TIMEOUT)
        self.cog = cog
        self.game = game
        self.accept_button = discord.ui.Button(label="Aceitar", style=discord.ButtonStyle.success)
        self.decline_button = discord.ui.Button(label="Recusar", style=discord.ButtonStyle.danger)
        self.accept_button.callback = self._accept
        self.decline_button.callback = self._decline
        self._build_layout()

    def _build_layout(self):
        self.clear_items()
        lines = self.cog._truco_challenge_lines(self.game)
        row = discord.ui.ActionRow(self.accept_button, self.decline_button)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            row,
            accent_color=discord.Color.dark_green(),
        ))

    async def _accept(self, interaction: discord.Interaction):
        await self.cog._handle_truco_accept(interaction, self.game)

    async def _decline(self, interaction: discord.Interaction):
        await self.cog._handle_truco_decline(interaction, self.game)

    async def on_timeout(self):
        try:
            await self.cog._expire_truco_invite(self.game)
        except Exception:
            pass


class Truco2v2LobbyView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaTrucoMixin", lobby: TrucoLobby, guild: discord.Guild):
        super().__init__(timeout=_TRUCO_LOBBY_TIMEOUT)
        self.cog = cog
        self.lobby = lobby
        self.guild = guild
        self.join_a = discord.ui.Button(label="🟦 Entrar no time 1", style=discord.ButtonStyle.primary)
        self.join_b = discord.ui.Button(label="🟥 Entrar no time 2", style=discord.ButtonStyle.danger)
        self.swap = discord.ui.Button(label="🔄 Trocar de time", style=discord.ButtonStyle.secondary)
        self.leave = discord.ui.Button(label="Sair", style=discord.ButtonStyle.secondary)
        self.start = discord.ui.Button(label="🚩 Iniciar", style=discord.ButtonStyle.success)
        self.cancel = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.danger)
        self.join_a.callback = self._join_a
        self.join_b.callback = self._join_b
        self.swap.callback = self._swap
        self.leave.callback = self._leave
        self.start.callback = self._start
        self.cancel.callback = self._cancel
        self._build_layout()

    def _build_layout(self):
        self.clear_items()
        a_mentions = [self.guild.get_member(uid).mention for uid in self.lobby.team_a if self.guild.get_member(uid)]
        b_mentions = [self.guild.get_member(uid).mention for uid in self.lobby.team_b if self.guild.get_member(uid)]
        creator = self.guild.get_member(self.lobby.creator_id)
        header = [
            "# 🃏 Truco 2v2",
            f"**Entrada:** {self.cog._chip_amount(TRUCO_ENTRY)} cada",
            f"**Pote inicial:** {self.cog._chip_amount(40)}",
            "**Bônus dos vencedores:** +10 fichas bônus cada",
        ]
        if creator:
            header.append(f"**Criador:** {creator.mention}")
        team1 = [f"### 🟦 Time 1 ({len(a_mentions)}/2)"] + (a_mentions or ["• Vazio"])
        team2 = [f"### 🟥 Time 2 ({len(b_mentions)}/2)"] + (b_mentions or ["• Vazio"])
        foot = [
            "Entre em um time, troque rápido com 🔄 ou saia do lobby.",
            "O truco começa quando os dois times fecharem com 2 jogadores.",
        ]
        row1 = discord.ui.ActionRow(self.join_a, self.join_b, self.swap)
        row2 = discord.ui.ActionRow(self.leave, self.start, self.cancel)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(header)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(team1)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(team2)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(foot)),
            row1,
            row2,
            accent_color=discord.Color.dark_green(),
        ))

    async def _join_a(self, interaction: discord.Interaction):
        await self.cog._handle_truco2_lobby_join(interaction, self.lobby, 0, self)

    async def _join_b(self, interaction: discord.Interaction):
        await self.cog._handle_truco2_lobby_join(interaction, self.lobby, 1, self)

    async def _swap(self, interaction: discord.Interaction):
        await self.cog._handle_truco2_lobby_swap(interaction, self.lobby, self)

    async def _leave(self, interaction: discord.Interaction):
        await self.cog._handle_truco2_lobby_leave(interaction, self.lobby, self)

    async def _start(self, interaction: discord.Interaction):
        await self.cog._handle_truco2_lobby_start(interaction, self.lobby, self)

    async def _cancel(self, interaction: discord.Interaction):
        await self.cog._handle_truco2_lobby_cancel(interaction, self.lobby, self)

    async def on_timeout(self):
        try:
            await self.cog._handle_truco2_lobby_timeout(self.lobby)
        except Exception:
            pass


class TrucoTableView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaTrucoMixin", game: TrucoGame):
        super().__init__(timeout=_TRUCO_ACTION_TIMEOUT)
        self.cog = cog
        self.game = game
        self.refresh_buttons()

    def refresh_buttons(self):
        self.clear_items()
        hand = discord.ui.Button(label="Ver mão", style=discord.ButtonStyle.primary)
        hand.callback = self._my_hand
        buttons = [hand]
        if not self.game.finished:
            if self.game.status == "awaiting_raise_response":
                accept = discord.ui.Button(label="Aceitar", style=discord.ButtonStyle.success)
                accept.callback = self._accept_raise
                buttons.append(accept)
                nxt = self.cog._truco_next_raise_label(self.game)
                if nxt:
                    up = discord.ui.Button(label=f"Pedir {nxt}", style=discord.ButtonStyle.secondary)
                    up.callback = self._raise_action
                    buttons.append(up)
                leave = discord.ui.Button(label="Sair", style=discord.ButtonStyle.danger)
                leave.callback = self._run_action
                buttons.append(leave)
            else:
                nxt = self.cog._truco_next_raise_label(self.game)
                if nxt:
                    up = discord.ui.Button(label=f"Pedir {nxt}", style=discord.ButtonStyle.secondary)
                    up.callback = self._raise_action
                    buttons.append(up)
                leave = discord.ui.Button(label="Sair", style=discord.ButtonStyle.danger)
                leave.callback = self._run_action
                buttons.append(leave)
        lines = self.cog._truco_status_lines(self.game)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines["header"])),
            discord.ui.TextDisplay("\n".join(lines["meta"])),
            accent_color=discord.Color.dark_green(),
        ))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines["mesa"])),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(lines["status"])),
            discord.ui.ActionRow(*buttons),
            accent_color=discord.Color.blurple(),
        ))

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
            loser = self.game.turn_id or self.game.players_order[0]
            await self.cog._finish_truco_game(self.game, winner_team=self.cog._truco_other_team(self.game, loser), loser_id=loser, reason="tempo esgotado")
        except Exception:
            pass


class TrucoHandView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaTrucoMixin", game: TrucoGame, player_id: int):
        super().__init__(timeout=_TRUCO_ACTION_TIMEOUT)
        self.cog = cog
        self.game = game
        self.player_id = int(player_id)
        self.refresh_buttons()

    def refresh_buttons(self):
        self.clear_items()
        hand = self.game.hands.get(self.player_id, [])
        disabled_cards = self.game.finished or self.game.status != "active" or self.game.turn_id != self.player_id
        card_buttons = []
        for index, card in enumerate(hand):
            button = discord.ui.Button(label=self.cog._truco_card_display(card), style=discord.ButtonStyle.secondary, disabled=disabled_cards)
            async def _callback(interaction: discord.Interaction, idx=index):
                await self.cog._handle_truco_play_card(interaction, self.game, self.player_id, idx)
            button.callback = _callback
            card_buttons.append(button)
        action_buttons = []
        if not self.game.finished:
            if self.game.status == "awaiting_raise_response" and self.cog._truco_can_answer_raise(self.game, self.player_id):
                accept = discord.ui.Button(label="Aceitar", style=discord.ButtonStyle.success)
                accept.callback = self._accept_raise
                action_buttons.append(accept)
                nxt = self.cog._truco_next_raise_label(self.game)
                if nxt:
                    up = discord.ui.Button(label=f"Pedir {nxt}", style=discord.ButtonStyle.primary)
                    up.callback = self._raise_action
                    action_buttons.append(up)
                leave = discord.ui.Button(label="Sair", style=discord.ButtonStyle.danger)
                leave.callback = self._run_action
                action_buttons.append(leave)
            elif self.game.status == "active" and self.game.turn_id == self.player_id:
                nxt = self.cog._truco_next_raise_label(self.game)
                if nxt:
                    up = discord.ui.Button(label=f"Pedir {nxt}", style=discord.ButtonStyle.primary)
                    up.callback = self._raise_action
                    action_buttons.append(up)
                leave = discord.ui.Button(label="Sair", style=discord.ButtonStyle.danger)
                leave.callback = self._run_action
                action_buttons.append(leave)
        lines = self.cog._truco_hand_lines(self.game, self.player_id)
        items = [discord.ui.TextDisplay("\n".join(lines["header"])), discord.ui.Separator(), discord.ui.TextDisplay("\n".join(lines["meta"])), discord.ui.Separator(), discord.ui.TextDisplay("\n".join(lines["mesa"])), discord.ui.Separator(), discord.ui.TextDisplay("\n".join(lines["cards"])), discord.ui.Separator(), discord.ui.TextDisplay("\n".join(lines["status"]))]
        if card_buttons:
            items.append(discord.ui.ActionRow(*card_buttons[:5]))
        if action_buttons:
            items.append(discord.ui.ActionRow(*action_buttons[:5]))
        self.add_item(discord.ui.Container(*items, accent_color=discord.Color.blurple()))

    async def _raise_action(self, interaction: discord.Interaction):
        await self.cog._handle_truco_raise(interaction, self.game)

    async def _run_action(self, interaction: discord.Interaction):
        await self.cog._handle_truco_run(interaction, self.game)

    async def _accept_raise(self, interaction: discord.Interaction):
        await self.cog._handle_truco_accept_raise(interaction, self.game)


class GincanaTrucoMixin:
    def _ensure_truco_state(self):
        if not hasattr(self, "_truco_games"):
            self._truco_games = {}
        if not hasattr(self, "_truco_lobbies"):
            self._truco_lobbies = {}

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
            return 1 if _TRUCO_SUIT_STRENGTH[sa] > _TRUCO_SUIT_STRENGTH[sb] else -1 if _TRUCO_SUIT_STRENGTH[sa] < _TRUCO_SUIT_STRENGTH[sb] else 0
        if am:
            return 1
        if bm:
            return -1
        ia = _TRUCO_RANKS.index(ra)
        ib = _TRUCO_RANKS.index(rb)
        return 1 if ia > ib else -1 if ia < ib else 0

    def _truco_other_team(self, game: TrucoGame, player_id: int) -> int:
        t = self._truco_team_index(game, player_id)
        return 1 - t if t in (0, 1) else 0

    def _truco_team_index(self, game: TrucoGame, player_id: int) -> int | None:
        pid = int(player_id)
        for idx, team in enumerate(game.teams):
            if pid in team:
                return idx
        return None

    def _truco_partner(self, game: TrucoGame, player_id: int) -> int | None:
        team_idx = self._truco_team_index(game, player_id)
        if team_idx is None:
            return None
        team = list(game.teams[team_idx])
        if len(team) < 2:
            return None
        for uid in team:
            if uid != int(player_id):
                return uid
        return None

    def _truco_target_contrib(self, game: TrucoGame, level: int) -> int:
        if game.mode == "2v2":
            return _TRUCO_TARGET_CONTRIB_2V2[level]
        return _TRUCO_TARGET_CONTRIB_1V1[level]

    def _truco_target_pot(self, game: TrucoGame, level: int) -> int:
        if game.mode == "2v2":
            return _TRUCO_TARGET_POT_2V2[level]
        return _TRUCO_TARGET_POT_1V1[level]

    def _truco_next_raise_level(self, current: int):
        try:
            i = _TRUCO_LEVELS.index(int(current))
        except ValueError:
            return None
        return _TRUCO_LEVELS[i + 1] if i + 1 < len(_TRUCO_LEVELS) else None

    def _truco_next_raise_label(self, game: TrucoGame):
        nxt = self._truco_next_raise_level(game.pending_raise_to or game.level)
        return _TRUCO_RAISE_NAMES.get(nxt) if nxt else None

    def _truco_round_label(self, round_index: int) -> str:
        return {0: "1ª", 1: "2ª", 2: "3ª"}.get(int(round_index), f"{int(round_index)+1}ª")

    def _truco_member_name(self, guild: discord.Guild | None, user_id: int) -> str:
        member = guild.get_member(int(user_id)) if guild else None
        return member.display_name if member else str(user_id)

    def _truco_member_mention(self, guild: discord.Guild | None, user_id: int | None) -> str:
        if not user_id:
            return "alguém"
        member = guild.get_member(int(user_id)) if guild else None
        return member.mention if member else f"<@{int(user_id)}>"

    def _truco_team_name(self, game: TrucoGame, guild: discord.Guild | None, team_idx: int) -> str:
        if team_idx not in (0, 1):
            return "Time"
        names = [self._truco_member_name(guild, uid) for uid in game.teams[team_idx]]
        return " + ".join(names)

    def _truco_team_mentions(self, game: TrucoGame, guild: discord.Guild | None, team_idx: int) -> str:
        if team_idx not in (0, 1):
            return "—"
        return " • ".join(self._truco_member_mention(guild, uid) for uid in game.teams[team_idx])

    def _truco_rotate_order(self, players_order: list[int], starter_id: int) -> list[int]:
        if starter_id not in players_order:
            return list(players_order)
        idx = players_order.index(starter_id)
        return list(players_order[idx:]) + list(players_order[:idx])

    def _truco_make_game(self, guild_id: int, channel_id: int, mode: str, players_order: list[int], teams: list[tuple[int, ...]]) -> TrucoGame:
        game = TrucoGame(
            guild_id=guild_id,
            channel_id=channel_id,
            mode=mode,
            players_order=list(players_order),
            teams=[tuple(team) for team in teams],
            level=1,
            status="invite" if mode == "1v1" else "lobby_starting",
            hand_starter_id=players_order[0],
            turn_id=players_order[0],
            contribution={uid: TRUCO_ENTRY for uid in players_order},
        )
        game.pot = self._truco_target_pot(game, 1)
        return game

    def _truco_challenge_lines(self, game: TrucoGame) -> list[str]:
        guild = self.bot.get_guild(game.guild_id)
        return [
            "# 🃏 Truco",
            f"{self._truco_member_mention(guild, game.players_order[0])} desafiou {self._truco_member_mention(guild, game.players_order[1])}.",
            f"**Entrada:** {self._chip_amount(TRUCO_ENTRY)} cada",
            f"**Pote inicial:** {self._chip_amount(game.pot)}",
            "Aceite para começar o jogo.",
        ]

    def _truco_status_lines(self, game: TrucoGame, *, title: str = "🃏 Truco") -> dict[str, list[str]]:
        guild = self.bot.get_guild(game.guild_id)
        if game.mode == "2v2":
            duel = [title, f"{self._truco_team_mentions(game, guild, 0)}", "vs", f"{self._truco_team_mentions(game, guild, 1)}"]
        else:
            duel = [title, f"{self._truco_member_mention(guild, game.players_order[0])} vs {self._truco_member_mention(guild, game.players_order[1])}"]
        meta = [
            f"**Pote:** {self._chip_amount(game.pot)}",
            f"**Vira:** {self._truco_card_public_display(game.vira)}",
            f"**Manilha:** {game.manilha_rank or '—'}",
        ]
        team0 = sum(1 for x in game.round_results if x == 0)
        team1 = sum(1 for x in game.round_results if x == 1)
        if game.mode == "2v2":
            meta.append(f"**Vazas:** Time 1 **{team0}** × **{team1}** Time 2")
        else:
            meta.append(f"**Vazas:** **{team0}** × **{team1}**")
        mesa = ["## Mesa"]
        order = self._truco_rotate_order(game.players_order, game.hand_starter_id or game.players_order[0])
        shown = False
        for uid in order:
            card = game.cards_on_table.get(uid)
            mesa.append(f"**{self._truco_member_name(guild, uid)}** • {self._truco_card_public_display(card) if card else '—'}")
            shown = shown or bool(card)
        if not shown:
            mesa.append("Ninguém jogou carta ainda.")
        if game.table_history:
            mesa.extend(["", "## Vazas", *game.table_history[-3:]])
        status = ["## Status", game.status_text, "Use **Ver mão** para acompanhar o jogo na DM."]
        return {"header": duel, "meta": meta, "mesa": mesa, "status": status}

    def _truco_hand_lines(self, game: TrucoGame, player_id: int, *, dm_ok: bool = True) -> dict[str, list[str]]:
        guild = self.bot.get_guild(game.guild_id)
        cards = game.hands.get(int(player_id), [])
        team0 = sum(1 for x in game.round_results if x == 0)
        team1 = sum(1 for x in game.round_results if x == 1)
        partner = self._truco_partner(game, player_id)
        if game.finished:
            status = game.status_text or "Jogo encerrado."
        elif game.status == "awaiting_raise_response" and self._truco_can_answer_raise(game, player_id):
            status = f"{self._truco_member_name(guild, game.pending_raise_by or 0)} pediu {_TRUCO_RAISE_NAMES.get(game.pending_raise_to, 'aumento')}."
        elif game.status == "awaiting_raise_response":
            status = "Esperando a resposta do outro lado."
        elif int(player_id) == game.turn_id:
            status = "É a sua vez."
        else:
            status = f"Vez de {self._truco_member_name(guild, game.turn_id or 0)}."
        header = ["# 🃏 Truco", "## Seu jogo"]
        meta = [
            f"**Pote:** {self._chip_amount(game.pot)}",
            f"**Vira:** {self._truco_card_public_display(game.vira)}",
            f"**Manilha:** {game.manilha_rank}",
            f"**Vazas:** {team0} × {team1}",
        ]
        if partner:
            meta.append(f"**Parceiro:** {self._truco_member_name(guild, partner)}")
        mesa = ["## Mesa"]
        order = self._truco_rotate_order(game.players_order, game.hand_starter_id or game.players_order[0])
        any_card = False
        for uid in order:
            label = "Você" if int(uid) == int(player_id) else self._truco_member_name(guild, uid)
            card = game.cards_on_table.get(uid)
            mesa.append(f"**{label}** • {self._truco_card_public_display(card) if card else '—'}")
            any_card = any_card or bool(card)
        if not any_card:
            mesa.append("Ninguém jogou carta ainda.")
        if game.table_history:
            mesa.extend(["", "## Vazas", *game.table_history[-3:]])
        cards_lines = ["## Suas cartas"] + ([f"• {self._truco_card_display(card)}" for card in cards] if cards else ["Você já jogou todas as cartas."])
        status_lines = ["## Status", status]
        if not dm_ok:
            status_lines.append("Não consegui usar sua DM, então mostrei o jogo aqui.")
        return {"header": header, "meta": meta, "mesa": mesa, "cards": cards_lines, "status": status_lines}

    def _truco_status_embed(self, game: TrucoGame, *, title: str = "🃏 Truco") -> discord.Embed:
        lines = self._truco_status_lines(game, title=title)
        embed = discord.Embed(title=title, description="\n".join(lines["header"] + [""] + lines["mesa"] + [""] + lines["status"]), color=discord.Color.dark_green())
        return embed

    def _truco_hand_embed(self, game: TrucoGame, player_id: int, *, dm_ok: bool = True) -> discord.Embed:
        lines = self._truco_hand_lines(game, player_id, dm_ok=dm_ok)
        return discord.Embed(title="🃏 Seu jogo", description="\n".join(lines["header"] + [""] + lines["mesa"] + [""] + lines["cards"] + [""] + lines["status"]), color=discord.Color.blurple())

    async def _truco_safe_edit(self, message, *, embed=None, view=None, content=None):
        if message is None:
            return False
        try:
            if view is not None and embed is None and content is None:
                await message.edit(view=view)
            else:
                await message.edit(content=content, embed=embed, view=view)
            return True
        except Exception:
            try:
                if view is not None and embed is None:
                    await message.edit(content=content, view=view)
                    return True
            except Exception:
                pass
        return False

    async def _truco_update_interaction_message(self, interaction: discord.Interaction, *, view: discord.ui.LayoutView | None = None, content: str | None = None):
        target = getattr(interaction, 'message', None)
        if interaction.response.is_done():
            return await self._truco_safe_edit(target, view=view, content=content, embed=None)
        try:
            await interaction.response.edit_message(content=content, embed=None, view=view)
            return True
        except Exception:
            return await self._truco_safe_edit(target, view=view, content=content, embed=None)

    async def _truco_refresh_private_views(self, game: TrucoGame):
        for player_id in game.players:
            await self._truco_send_hand_dm(game, player_id, quiet=True)

    async def _truco_show_turn(self, game: TrucoGame):
        if game.finished:
            return
        guild = self.bot.get_guild(game.guild_id)
        game.status_text = f"Vez de {self._truco_member_mention(guild, game.turn_id)}."
        await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
        await self._truco_refresh_private_views(game)

    def _truco_round_winner_team(self, game: TrucoGame) -> int | None:
        if not game.cards_on_table:
            return None
        best_card = None
        winning_player = None
        tie_teams: set[int] = set()
        for pid, card in game.cards_on_table.items():
            if best_card is None:
                best_card = card
                winning_player = pid
                tie_teams = {self._truco_team_index(game, pid)}
                continue
            cmp = self._truco_compare_cards(card, best_card, game.manilha_rank or "")
            if cmp > 0:
                best_card = card
                winning_player = pid
                tie_teams = {self._truco_team_index(game, pid)}
            elif cmp == 0:
                tie_teams.add(self._truco_team_index(game, pid))
        if len(tie_teams) > 1:
            return None
        return self._truco_team_index(game, winning_player)

    def _truco_hand_winner_team(self, game: TrucoGame) -> int | None:
        results = list(game.round_results)
        if not results:
            return None
        if len(results) >= 1 and results[0] is not None and results.count(results[0]) >= 2:
            return results[0]
        if len(results) == 1:
            return None
        first, second = results[0], results[1]
        if first is None and second is not None:
            return second
        if first is not None and second is None:
            return first
        if first is not None and second is not None and first == second:
            return first
        if len(results) < 3:
            return None
        third = results[2]
        if third is not None:
            return third
        if first is not None:
            return first
        starter_team = self._truco_team_index(game, game.hand_starter_id or game.players_order[0])
        return 0 if starter_team is None else starter_team

    async def _truco_resolve_round(self, game: TrucoGame):
        expected = 2 if game.mode == "1v1" else 4
        if len(game.cards_on_table) < expected:
            return
        game.status_text = "Resolvendo a vaza..."
        await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
        await asyncio.sleep(0.8)
        winner_team = self._truco_round_winner_team(game)
        label = self._truco_round_label(game.round_index)
        guild = self.bot.get_guild(game.guild_id)
        if winner_team is None:
            game.status_text = f"A {label.lower()} vaza empatou."
            game.table_history.append(game.status_text)
        else:
            name = self._truco_team_name(game, guild, winner_team) if game.mode == "2v2" else self._truco_member_name(guild, game.players_order[winner_team])
            game.status_text = f"{name} levou a {label.lower()} vaza."
            game.table_history.append(game.status_text)
        game.round_results.append(winner_team)
        await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
        await self._truco_refresh_private_views(game)
        await asyncio.sleep(0.8)
        hand_winner = self._truco_hand_winner_team(game)
        # determine round-starter for next round before clearing
        if winner_team is not None:
            winning_players = [pid for pid in game.cards_on_table.keys() if self._truco_team_index(game, pid) == winner_team]
            # player with strongest card among winners
            starter = max(winning_players, key=lambda pid: self._truco_card_strength_key(game.cards_on_table[pid], game.manilha_rank or ""))
        else:
            starter = game.hand_starter_id or game.players_order[0]
        game.cards_on_table.clear()
        game.round_index += 1
        if hand_winner is not None or len(game.round_results) >= 3:
            await self._finish_truco_game(game, winner_team=(hand_winner if hand_winner is not None else self._truco_team_index(game, starter) or 0), loser_id=starter, reason="jogo encerrado")
            return
        game.hand_starter_id = starter
        game.players_order = self._truco_rotate_order(game.players_order, starter)
        game.turn_id = starter
        await self._truco_show_turn(game)

    def _truco_card_strength_key(self, card, manilha_rank: str):
        rank, suit = card
        if rank == manilha_rank:
            return (1, _TRUCO_SUIT_STRENGTH[suit])
        return (0, _TRUCO_RANKS.index(rank))

    async def _finish_truco_game(self, game: TrucoGame, *, winner_team: int, loser_id: int, reason: str):
        if game.finished:
            return
        game.finished = True
        game.status = "finished"
        self._truco_games.pop(game.guild_id, None)
        winners = list(game.teams[winner_team])
        losers = [uid for idx, team in enumerate(game.teams) if idx != winner_team for uid in team]
        share = int(game.pot / max(1, len(winners)))
        for uid in winners:
            await self.db.add_user_game_stat(game.guild_id, uid, "truco_wins", 1)
            await self._record_game_played(game.guild_id, uid, weekly_points=6)
            await self._change_user_chips(game.guild_id, uid, share, mark_activity=True)
            await self._change_user_bonus_chips(game.guild_id, uid, TRUCO_BONUS_REWARD, mark_activity=True)
        for uid in losers:
            await self.db.add_user_game_stat(game.guild_id, uid, "truco_losses", 1)
            await self._record_game_played(game.guild_id, uid, weekly_points=2)
        guild = self.bot.get_guild(game.guild_id)
        winner_text = self._truco_team_mentions(game, guild, winner_team) if game.mode == "2v2" else self._truco_member_mention(guild, winners[0])
        loser_text = self._truco_member_mention(guild, loser_id) if guild else "Alguém"
        if reason == "correu":
            end_line = f"{loser_text} saiu do jogo."
            summary = f"{winner_text} levou a rodada."
            game.status_text = "Jogo encerrado por saída."
        elif reason == "tempo esgotado":
            end_line = f"{loser_text} não respondeu a tempo."
            summary = f"{winner_text} levou a rodada."
            game.status_text = "Jogo encerrado por abandono."
        else:
            end_line = f"{winner_text} venceu o jogo."
            summary = "O jogo foi encerrado."
            game.status_text = "Jogo encerrado com vitória normal."
        lines = self._truco_status_lines(game, title="🃏 Truco")
        reward_line = (
            f"{winner_text} levou **{game.pot}** {self._CHIP_GAIN_EMOJI} e ganhou **+{TRUCO_BONUS_REWARD}** {self._CHIP_BONUS_EMOJI}."
            if game.mode == "1v1" else
            f"{winner_text} levou **{game.pot}** {self._CHIP_GAIN_EMOJI} e cada vencedor ganhou **+{TRUCO_BONUS_REWARD}** {self._CHIP_BONUS_EMOJI}."
        )
        lines["status"] = [game.status_text, end_line, summary, reward_line]
        closed = discord.ui.LayoutView(timeout=None)
        closed.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines["header"])),
            discord.ui.TextDisplay("\n".join(lines["meta"])),
            accent_color=discord.Color.dark_green(),
        ))
        closed.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines["mesa"])),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(lines["status"])),
            accent_color=discord.Color.blurple(),
        ))
        await self._truco_safe_edit(game.status_message, embed=None, view=closed)
        await self._truco_refresh_private_views(game)

    async def _expire_truco_invite(self, game: TrucoGame):
        if game.finished or game.accepted:
            return
        self._truco_games.pop(game.guild_id, None)
        game.finished = True
        closed = discord.ui.LayoutView(timeout=None)
        closed.add_item(discord.ui.Container(discord.ui.TextDisplay("# 🃏 Truco\nO desafio expirou porque não foi aceito a tempo."), accent_color=discord.Color.red()))
        await self._truco_safe_edit(game.challenge_message, embed=None, view=closed)

    async def _handle_truco_trigger(self, message: discord.Message) -> bool:
        self._ensure_truco_state()
        guild = message.guild
        if guild is None:
            return False
        raw = str(message.content or "").strip()
        if _TRUCO_2V2_TRIGGER_RE.match(raw):
            return await self._handle_truco2_trigger(message)
        if not _TRUCO_TRIGGER_RE.match(raw):
            return False
        if guild.id in self._truco_games or guild.id in self._truco_lobbies:
            await message.channel.send(embed=self._make_embed("🃏 Truco ocupado", "Já existe um truco em andamento neste servidor.", ok=False))
            return True
        mentions = [m for m in message.mentions if not m.bot]
        if len(mentions) != 1:
            await message.channel.send(embed=self._make_embed("🃏 Truco", "Use `truco @usuário` para desafiar alguém.", ok=False))
            return True
        challenger = message.author
        opponent = mentions[0]
        if opponent.id == challenger.id:
            await message.channel.send(embed=self._make_embed("🃏 Truco", "Você precisa desafiar outra pessoa.", ok=False))
            return True
        ok, _current, note = await self._ensure_action_chips(guild.id, challenger.id, TRUCO_ENTRY)
        if not ok:
            await message.channel.send(embed=self._make_embed("🃏 Truco", note or "Você não tem saldo suficiente para entrar nesse jogo.", ok=False))
            return True
        game = self._truco_make_game(guild.id, message.channel.id, "1v1", [challenger.id, opponent.id], [(challenger.id,), (opponent.id,)])
        self._truco_games[guild.id] = game
        embed = discord.Embed(
            title="🃏 Truco",
            description=(
                f"{challenger.mention} chamou {opponent.mention} pro truco.\n"
                f"Entrada: **{TRUCO_ENTRY}** {self._CHIP_EMOJI} cada."
            ),
            color=discord.Color.dark_green(),
        )
        view = TrucoChallengeView(self, game)
        game.challenge_message = await message.channel.send(view=view)
        if note:
            try:
                await message.channel.send(note)
            except Exception:
                pass
        return True

    async def _handle_truco2_trigger(self, message: discord.Message) -> bool:
        self._ensure_truco_state()
        guild = message.guild
        if guild is None:
            return False
        if guild.id in self._truco_games or guild.id in self._truco_lobbies:
            await message.channel.send(embed=self._make_embed("🃏 Truco ocupado", "Já existe um truco em andamento neste servidor.", ok=False))
            return True
        lobby = TrucoLobby(guild_id=guild.id, channel_id=message.channel.id, creator_id=message.author.id)
        lobby.team_a = [message.author.id]
        self._truco_lobbies[guild.id] = lobby
        view = Truco2v2LobbyView(self, lobby, guild)
        lobby.message = await message.channel.send(view=view)
        return True

    async def _handle_truco2_lobby_join(self, interaction: discord.Interaction, lobby: TrucoLobby, team_idx: int, view: Truco2v2LobbyView):
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            return
        if guild.id not in self._truco_lobbies or self._truco_lobbies[guild.id] is not lobby or lobby.started:
            await interaction.response.send_message("Esse lobby já foi fechado.", ephemeral=True)
            return
        if user.id in lobby.team_a or user.id in lobby.team_b:
            await interaction.response.send_message("Você já está no lobby. Use 🔄 para trocar de time.", ephemeral=True)
            return
        team = lobby.team_a if team_idx == 0 else lobby.team_b
        if len(team) >= 2:
            await interaction.response.send_message("Esse time já está cheio.", ephemeral=True)
            return
        ok, _cur, note = await self._ensure_action_chips(guild.id, user.id, TRUCO_ENTRY)
        if not ok:
            await interaction.response.send_message(note or "Você não pode entrar nesse truco agora.", ephemeral=True)
            return
        if self._needs_negative_confirmation(guild.id, user.id, TRUCO_ENTRY):
            confirmed = await self._confirm_negative_ephemeral(interaction, guild.id, user.id, TRUCO_ENTRY, title="⚠️ Confirmar entrada")
            if not confirmed:
                return
        team.append(user.id)
        view._build_layout()
        await self._truco_update_interaction_message(interaction, view=view)

    async def _handle_truco2_lobby_swap(self, interaction: discord.Interaction, lobby: TrucoLobby, view: Truco2v2LobbyView):
        user_id = interaction.user.id
        if user_id in lobby.team_a:
            if len(lobby.team_b) >= 2:
                await interaction.response.send_message("O outro time já está cheio.", ephemeral=True)
                return
            lobby.team_a.remove(user_id)
            lobby.team_b.append(user_id)
        elif user_id in lobby.team_b:
            if len(lobby.team_a) >= 2:
                await interaction.response.send_message("O outro time já está cheio.", ephemeral=True)
                return
            lobby.team_b.remove(user_id)
            lobby.team_a.append(user_id)
        else:
            await interaction.response.send_message("Você ainda não entrou no lobby.", ephemeral=True)
            return
        view._build_layout()
        await self._truco_update_interaction_message(interaction, view=view)

    async def _handle_truco2_lobby_leave(self, interaction: discord.Interaction, lobby: TrucoLobby, view: Truco2v2LobbyView):
        uid = interaction.user.id
        changed = False
        if uid in lobby.team_a:
            lobby.team_a.remove(uid)
            changed = True
        if uid in lobby.team_b:
            lobby.team_b.remove(uid)
            changed = True
        if not changed:
            await interaction.response.send_message("Você não está no lobby.", ephemeral=True)
            return
        view._build_layout()
        await self._truco_update_interaction_message(interaction, view=view)

    async def _handle_truco2_lobby_cancel(self, interaction: discord.Interaction, lobby: TrucoLobby, view: Truco2v2LobbyView):
        guild = interaction.guild
        if guild is None:
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
        if interaction.user.id != lobby.creator_id and not (isinstance(member, discord.Member) and self._is_staff_member(member)):
            await interaction.response.send_message("Só o criador ou a staff pode cancelar.", ephemeral=True)
            return
        self._truco_lobbies.pop(guild.id, None)
        lobby.started = True
        closed = discord.ui.LayoutView(timeout=None)
        closed.add_item(discord.ui.Container(discord.ui.TextDisplay("# 🃏 Truco 2v2\nO lobby foi cancelado."), accent_color=discord.Color.red()))
        await self._truco_update_interaction_message(interaction, view=closed)

    async def _handle_truco2_lobby_timeout(self, lobby: TrucoLobby):
        self._truco_lobbies.pop(lobby.guild_id, None)
        if lobby.message:
            closed = discord.ui.LayoutView(timeout=None)
            closed.add_item(discord.ui.Container(discord.ui.TextDisplay("# 🃏 Truco 2v2\nO lobby expirou sem fechar os times."), accent_color=discord.Color.red()))
            try:
                await lobby.message.edit(view=closed)
            except Exception:
                pass

    async def _handle_truco2_lobby_start(self, interaction: discord.Interaction, lobby: TrucoLobby, view: Truco2v2LobbyView):
        guild = interaction.guild
        if guild is None:
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
        if interaction.user.id != lobby.creator_id and not (isinstance(member, discord.Member) and self._is_staff_member(member)):
            await interaction.response.send_message("Só o criador ou a staff pode iniciar.", ephemeral=True)
            return
        if len(lobby.team_a) != 2 or len(lobby.team_b) != 2:
            await interaction.response.send_message("Feche os dois times com 2 jogadores antes de começar.", ephemeral=True)
            return
        players = lobby.team_a + lobby.team_b
        for uid in players:
            ok, _cur, note = await self._ensure_action_chips(guild.id, uid, TRUCO_ENTRY)
            if not ok:
                await interaction.response.send_message(f"{self._truco_member_mention(guild, uid)} não pode entrar agora. {note or ''}".strip(), ephemeral=True)
                return
        # consume entry now
        for uid in players:
            paid, _bal, note = await self._try_consume_chips(guild.id, uid, TRUCO_ENTRY)
            if not paid:
                await interaction.response.send_message(f"Não foi possível cobrar a entrada de {self._truco_member_mention(guild, uid)}.", ephemeral=True)
                return
        order = [lobby.team_a[0], lobby.team_b[0], lobby.team_a[1], lobby.team_b[1]]
        game = self._truco_make_game(guild.id, lobby.channel_id, "2v2", order, [tuple(lobby.team_a), tuple(lobby.team_b)])
        self._truco_games[guild.id] = game
        self._truco_lobbies.pop(guild.id, None)
        lobby.started = True
        closed = discord.ui.LayoutView(timeout=None)
        closed.add_item(discord.ui.Container(discord.ui.TextDisplay("# 🃏 Truco 2v2\nOs times fecharam e o jogo vai começar."), accent_color=discord.Color.dark_green()))
        await self._truco_update_interaction_message(interaction, view=closed)
        await self._start_truco_game(game)

    async def _handle_truco_decline(self, interaction: discord.Interaction, game: TrucoGame):
        if interaction.user.id != game.players_order[1]:
            await interaction.response.send_message("Esse desafio não é seu.", ephemeral=True)
            return
        self._truco_games.pop(game.guild_id, None)
        game.finished = True
        closed = discord.ui.LayoutView(timeout=None)
        closed.add_item(discord.ui.Container(discord.ui.TextDisplay(f"# 🃏 Truco\n{interaction.user.mention} recusou o truco."), accent_color=discord.Color.red()))
        await self._truco_update_interaction_message(interaction, view=closed)

    async def _handle_truco_accept(self, interaction: discord.Interaction, game: TrucoGame):
        if interaction.user.id != game.players_order[1]:
            await interaction.response.send_message("Esse desafio não é seu.", ephemeral=True)
            return
        ok, _cur, note = await self._ensure_action_chips(game.guild_id, interaction.user.id, TRUCO_ENTRY)
        if not ok:
            await interaction.response.send_message(note or "Você não tem saldo suficiente para aceitar.", ephemeral=True)
            return
        if self._needs_negative_confirmation(game.guild_id, interaction.user.id, TRUCO_ENTRY):
            confirmed = await self._confirm_negative_ephemeral(interaction, game.guild_id, interaction.user.id, TRUCO_ENTRY, title="⚠️ Confirmar entrada")
            if not confirmed:
                return
        paid, _b, note = await self._try_consume_chips(game.guild_id, game.players_order[0], TRUCO_ENTRY)
        if not paid:
            await interaction.response.send_message("Não foi possível cobrar a entrada do desafiante agora.", ephemeral=True)
            return
        paid, _b, note = await self._try_consume_chips(game.guild_id, interaction.user.id, TRUCO_ENTRY)
        if not paid:
            await interaction.response.send_message("Não foi possível cobrar a sua entrada agora.", ephemeral=True)
            return
        started = discord.ui.LayoutView(timeout=None)
        started.add_item(discord.ui.Container(discord.ui.TextDisplay("# 🃏 Truco\nAs cartas foram distribuídas."), accent_color=discord.Color.dark_green()))
        await self._truco_update_interaction_message(interaction, view=started)
        await self._start_truco_game(game)

    async def _start_truco_game(self, game: TrucoGame):
        channel = self.bot.get_channel(game.channel_id)
        if channel is None:
            self._truco_games.pop(game.guild_id, None)
            return
        deck = self._truco_create_deck()
        game.vira = deck.pop(0)
        game.manilha_rank = self._truco_manilha_rank(game.vira[0])
        for uid in game.players:
            game.hands[uid] = [deck.pop(0) for _ in range(3)]
        game.accepted = True
        game.status = "active"
        game.status_text = "Distribuindo as cartas..."
        game.challenge_message = None
        game.status_message = await channel.send(view=TrucoTableView(self, game))
        await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
        await asyncio.sleep(0.8)
        game.status_text = "Virando a carta..."
        await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
        await asyncio.sleep(0.8)
        for uid in game.players:
            await self._truco_send_hand_dm(game, uid)
        await self._truco_show_turn(game)

    async def _truco_send_hand_dm(self, game: TrucoGame, player_id: int, quiet: bool = False) -> bool:
        try:
            user = self.bot.get_user(int(player_id)) or await self.bot.fetch_user(int(player_id))
        except Exception:
            return False
        view = TrucoHandView(self, game, int(player_id))
        old = game.dm_messages.get(int(player_id))
        if old is not None:
            ok = await self._truco_safe_edit(old, embed=None, view=view, content=None)
            if ok:
                return True
            game.dm_messages.pop(int(player_id), None)
        try:
            msg = await user.send(view=view)
            game.dm_messages[int(player_id)] = msg
            return True
        except Exception:
            if not quiet:
                game.dm_messages.pop(int(player_id), None)
            return False

    async def _handle_truco_show_hand(self, interaction: discord.Interaction, game: TrucoGame):
        if interaction.user.id not in game.players:
            await interaction.response.send_message("Esse jogo não é seu.", ephemeral=True)
            return
        sent_dm = await self._truco_send_hand_dm(game, interaction.user.id)
        if sent_dm:
            await interaction.response.send_message("Atualizei o seu jogo na DM.", ephemeral=True)
            return
        await interaction.response.send_message("Não consegui usar sua DM, então mostrei o jogo aqui.", ephemeral=True, view=TrucoHandView(self, game, interaction.user.id))

    async def _handle_truco_play_card(self, interaction: discord.Interaction, game: TrucoGame, player_id: int, card_index: int):
        if interaction.user.id != player_id:
            await interaction.response.send_message("Esse jogo não é seu.", ephemeral=True)
            return
        if game.finished or game.status != "active":
            await interaction.response.send_message("Esse jogo não está pronto para jogada agora.", ephemeral=True)
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
        game.status_text = f"{self._truco_member_mention(guild, player_id)} puxou uma carta."
        game.cards_on_table[player_id] = card
        if not interaction.response.is_done():
            await interaction.response.defer()
        try:
            if interaction.message is not None:
                await self._truco_safe_edit(interaction.message, embed=None, view=TrucoHandView(self, game, player_id), content=None)
        except Exception:
            pass
        await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
        await self._truco_refresh_private_views(game)
        await asyncio.sleep(0.8)
        expected = 2 if game.mode == "1v1" else 4
        if len(game.cards_on_table) < expected:
            # next in current rotated order not yet played
            for uid in game.players_order:
                if uid not in game.cards_on_table:
                    game.turn_id = uid
                    break
            await self._truco_show_turn(game)
            return
        await self._truco_resolve_round(game)

    def _truco_requesting_team(self, game: TrucoGame) -> int | None:
        return self._truco_team_index(game, game.pending_raise_by or 0)

    def _truco_can_answer_raise(self, game: TrucoGame, player_id: int) -> bool:
        if game.status != "awaiting_raise_response" or not game.pending_raise_by:
            return False
        req_team = self._truco_team_index(game, game.pending_raise_by)
        return self._truco_team_index(game, player_id) != req_team

    async def _handle_truco_raise(self, interaction: discord.Interaction, game: TrucoGame):
        if interaction.user.id not in game.players:
            await interaction.response.send_message("Esse jogo não é seu.", ephemeral=True)
            return
        if game.finished:
            await interaction.response.send_message("O jogo já terminou.", ephemeral=True)
            return
        if game.status == "awaiting_raise_response":
            if not self._truco_can_answer_raise(game, interaction.user.id):
                await interaction.response.send_message("Aguardando o outro time responder o aumento atual.", ephemeral=True)
                return
            base = game.pending_raise_to or game.level
        else:
            if interaction.user.id != game.turn_id:
                await interaction.response.send_message("Você só pode pedir aumento na sua vez.", ephemeral=True)
                return
            base = game.level
        nxt = self._truco_next_raise_level(base)
        if not nxt:
            await interaction.response.send_message("Esse jogo já chegou no máximo.", ephemeral=True)
            return
        requester_team = self._truco_team_index(game, interaction.user.id)
        target_contrib = self._truco_target_contrib(game, nxt)
        for pid in game.teams[requester_team]:
            delta = max(0, target_contrib - int(game.contribution.get(pid, TRUCO_ENTRY)))
            ok, _cur, note = await self._ensure_action_chips(game.guild_id, pid, delta)
            if not ok:
                await interaction.response.send_message(note or "Seu time não pode subir o jogo agora.", ephemeral=True)
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
        await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
        await self._truco_refresh_private_views(game)

    async def _handle_truco_accept_raise(self, interaction: discord.Interaction, game: TrucoGame):
        if game.status != "awaiting_raise_response" or not game.pending_raise_to or not game.pending_raise_by:
            await interaction.response.send_message("Não há aumento pendente agora.", ephemeral=True)
            return
        if not self._truco_can_answer_raise(game, interaction.user.id):
            await interaction.response.send_message("A resposta desse aumento não é sua.", ephemeral=True)
            return
        target_level = game.pending_raise_to
        target_contrib = self._truco_target_contrib(game, target_level)
        requester_team = self._truco_team_index(game, game.pending_raise_by)
        accepting_team = 1 - requester_team
        deltas = {}
        for pid in game.players:
            deltas[pid] = max(0, target_contrib - int(game.contribution.get(pid, TRUCO_ENTRY)))
        for pid in game.teams[requester_team] + game.teams[accepting_team]:
            delta = deltas[pid]
            ok, _cur, note = await self._ensure_action_chips(game.guild_id, pid, delta)
            if not ok:
                await interaction.response.send_message(note or "Nem todo mundo consegue bancar o aumento agora.", ephemeral=True)
                return
        for pid, delta in deltas.items():
            if delta > 0:
                await self._try_consume_chips(game.guild_id, pid, delta)
                game.contribution[pid] = int(game.contribution.get(pid, TRUCO_ENTRY)) + delta
        game.level = target_level
        game.pot = self._truco_target_pot(game, target_level)
        game.status = "active"
        game.pending_raise_by = None
        game.pending_raise_to = None
        guild = self.bot.get_guild(game.guild_id)
        game.status_text = f"{self._truco_member_mention(guild, interaction.user.id)} aceitou. Agora o jogo vale {target_level}."
        if interaction.response.is_done():
            await interaction.followup.send("Aumento aceito.", ephemeral=True)
        else:
            await interaction.response.send_message("Aumento aceito.", ephemeral=True)
        await self._truco_safe_edit(game.status_message, embed=None, view=TrucoTableView(self, game))
        await self._truco_refresh_private_views(game)

    async def _handle_truco_run(self, interaction: discord.Interaction, game: TrucoGame):
        if interaction.user.id not in game.players:
            await interaction.response.send_message("Esse jogo não é seu.", ephemeral=True)
            return
        loser_team = self._truco_team_index(game, interaction.user.id)
        winner_team = 1 - loser_team
        if interaction.response.is_done():
            await interaction.followup.send("Você saiu do jogo.", ephemeral=True)
        else:
            await interaction.response.send_message("Você saiu do jogo.", ephemeral=True)
        await self._finish_truco_game(game, winner_team=winner_team, loser_id=interaction.user.id, reason="correu")
