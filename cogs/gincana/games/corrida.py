import asyncio
import random
import time

import discord


CORRIDA_STAKE = 10
_CORRIDA_TRACK_LENGTH = 12
_CORRIDA_UPDATES = 15
_CORRIDA_UPDATE_SECONDS = 2.0
_CORRIDA_DURATION_SECONDS = int(_CORRIDA_UPDATES * _CORRIDA_UPDATE_SECONDS)
_CORRIDA_LOBBY_SECONDS = 20.0

_HORSE_START = "<:horse1:1485794648239636647>"
_HORSE_BOOST = "<:horse2:1485795177401417799>"
_HORSE_RUN = "<:horse2:1485795705745444995>"
_HORSE_TRIP = "<:horse2:1485795938990821547>"
_HORSE_FINISH = "<:Mine:1485797167494070524>"

_RACE_CONDITIONS = [
    {"name": "Pista seca", "boost": 0.0, "trip": 0.0, "speed": 0.0},
    {"name": "Pista molhada", "boost": -0.02, "trip": 0.08, "speed": -0.15},
    {"name": "Pista pesada", "boost": -0.03, "trip": 0.04, "speed": -0.25},
    {"name": "Pista rápida", "boost": 0.08, "trip": -0.02, "speed": 0.2},
]

_RACE_SPECIALS = [
    {"name": "Corrida turbo", "boost": 0.12, "trip": -0.03, "speed": 0.35, "bonus_pool": 0, "color": discord.Color.dark_magenta()},
    {"name": "Corrida pesada", "boost": -0.05, "trip": 0.1, "speed": -0.25, "bonus_pool": 0, "color": discord.Color.dark_orange()},
    {"name": "Corrida de zebra", "boost": 0.04, "trip": 0.0, "speed": 0.0, "bonus_pool": 0, "zebra": True, "color": discord.Color.purple()},
    {"name": "Grande prêmio", "boost": 0.02, "trip": 0.0, "speed": 0.15, "bonus_pool": 10, "color": discord.Color.gold()},
]

_RACE_IMPULSE_WINDOWS = (1, 7, 12)
_RACE_IMPULSE_INITIAL_DELAY = 2.0
_RACE_IMPULSE_STEP_SECONDS = 1.0
_RACE_IMPULSE_BUTTON_COUNT = 6
_RACE_IMPULSE_EMOJI = "⚡"


def _shared_rank_map(arrival_groups: list[list[int]]) -> dict[int, int]:
    rank_map: dict[int, int] = {}
    next_rank = 1
    for group in arrival_groups:
        for user_id in group:
            rank_map[int(user_id)] = next_rank
        next_rank += len(group)
    return rank_map


class _RaceLobbyView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaCorridaMixin", guild_id: int, session: dict, guild: discord.Guild, *, timeout: float = _CORRIDA_LOBBY_SECONDS):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.session = session
        self.guild = guild
        self.join_button = discord.ui.Button(style=discord.ButtonStyle.success, label=f"🐎 Entrar ({len(cog._get_race_participants(guild, session))})")
        self.join_button.callback = self._join_race
        self.start_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label="Iniciar", emoji="🏁")
        self.start_button.callback = self._start_race
        self._build_layout()

    def _build_layout(self):
        self.clear_items()
        condition_name = str((self.session.get("condition") or {}).get("name") or "Pista seca")
        special_name = str((self.session.get("special") or {}).get("name") or "")
        participants = self.cog._get_race_participants(self.guild, self.session)
        pot_total = len(self.session.get("locked_participants", set())) * CORRIDA_STAKE + int(self.session.get("bonus_pool", 0) or 0)

        header_lines = [
            "# 🐎 Corrida aberta",
            f"**Condição:** {condition_name}",
        ]
        if special_name:
            header_lines.append(f"**Especial:** {special_name}")
        header_lines.append(f"**Entrada:** {self.cog._chip_amount(CORRIDA_STAKE)}")
        header_lines.append(f"**Pote atual:** {self.cog._chip_amount(pot_total)}")
        header_lines.append(f"**Duração:** **{_CORRIDA_DURATION_SECONDS}s**")

        participants_lines = [f"### Participantes ({len(participants)})"]
        if participants:
            participants_lines.extend(f"• {member.mention}" for member in participants)
        else:
            participants_lines.append("• Ninguém entrou ainda.")

        info_lines = ["Confirme abaixo para entrar."]
        info_lines.append("O criador da corrida ou a staff pode iniciar com 🏁 quando houver pelo menos 2 participantes.")

        row = discord.ui.ActionRow(self.join_button, self.start_button)
        container = discord.ui.Container(
            discord.ui.TextDisplay("\n".join(header_lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(participants_lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(info_lines)),
            row,
            accent_color=discord.Color.blurple(),
        )
        self.add_item(container)

    async def _join_race(self, interaction: discord.Interaction):
        await self.cog._handle_race_button(interaction, self)

    async def _start_race(self, interaction: discord.Interaction):
        await self.cog._handle_race_start_button(interaction, self)

    async def on_timeout(self):
        try:
            await self.cog._finish_race_lobby(self.guild_id, reason="timeout", source_view=self)
        except Exception:
            pass


class _RaceLobbyClosedView(discord.ui.LayoutView):
    def __init__(self, session: dict, guild: discord.Guild, title: str, detail: str):
        super().__init__(timeout=None)
        condition_name = str((session.get("condition") or {}).get("name") or "Pista seca")
        special_name = str((session.get("special") or {}).get("name") or "")
        participants = int(len(session.get("locked_participants", set()) or []))

        lines = [f"# {title}", f"**Condição:** {condition_name}"]
        if special_name:
            lines.append(f"**Especial:** {special_name}")
        lines.append(f"**Participantes:** {participants}")
        lines.append(detail)

        container = discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            accent_color=discord.Color.blurple(),
        )
        self.add_item(container)


class _RaceImpulseButton(discord.ui.Button):
    def __init__(self, view: "_RaceImpulseEventView", index: int):
        super().__init__(style=discord.ButtonStyle.secondary, label=str(index + 1), disabled=True)
        self._view = view
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        await self._view.handle_press(interaction, self.index)


class _RaceImpulseEventView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaCorridaMixin", guild: discord.Guild, session: dict, stage_name: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild = guild
        self.session = session
        self.stage_name = stage_name
        self.message: discord.Message | None = None
        self.finished = False
        self.step_index = -1
        self.active_index: int | None = None
        self.active_started_at = 0.0
        self.order = random.sample(range(_RACE_IMPULSE_BUTTON_COUNT), _RACE_IMPULSE_BUTTON_COUNT)
        self.results: dict[int, dict] = {}
        self.edit_lock = asyncio.Lock()
        self.buttons = [_RaceImpulseButton(self, idx) for idx in range(_RACE_IMPULSE_BUTTON_COUNT)]
        self._rebuild()

    def _build_header_lines(self) -> list[str]:
        if self.finished:
            return [
                "# ⚡ Evento de impulso encerrado",
                f"**Fase:** {self.stage_name}",
                "Impulsos calculados e aplicados.",
            ]
        if self.step_index < 0:
            return [
                "# ⚡ Evento de impulso",
                f"**Fase:** {self.stage_name}",
                "Prepare-se. O primeiro botão acende em 2 segundos.",
            ]
        return [
            "# ⚡ Evento de impulso",
            f"**Fase:** {self.stage_name}",
            f"Aperte o botão cinza que acendeu. **Etapa {self.step_index + 1}/{_RACE_IMPULSE_BUTTON_COUNT}**",
        ]

    def _rebuild(self):
        self.clear_items()
        row1 = discord.ui.ActionRow(*self.buttons[:3])
        row2 = discord.ui.ActionRow(*self.buttons[3:])
        container = discord.ui.Container(
            discord.ui.TextDisplay("\n".join(self._build_header_lines())),
            discord.ui.Separator(),
            row1,
            row2,
            accent_color=discord.Color.dark_grey(),
        )
        self.add_item(container)

    async def refresh_message(self):
        if self.message is None:
            return
        async with self.edit_lock:
            try:
                self._rebuild()
                await self.message.edit(view=self)
            except Exception:
                pass

    async def handle_press(self, interaction: discord.Interaction, button_index: int):
        user = interaction.user
        try:
            await interaction.response.defer()
        except Exception:
            pass
        if self.finished or self.active_index is None or self.step_index < 0:
            return
        if interaction.guild is None or not isinstance(user, discord.Member):
            return
        if user.id not in set(self.session.get("locked_participants", set()) or []):
            return

        entry = self.results.setdefault(user.id, {"times": [None] * _RACE_IMPULSE_BUTTON_COUNT})
        times = entry["times"]
        if times[self.step_index] is not None:
            return
        if button_index != self.active_index:
            times[self.step_index] = 1.0
            return

        reaction_time = max(0.0, min(1.0, time.perf_counter() - self.active_started_at))
        times[self.step_index] = reaction_time

    def _activate_step(self, index: int):
        self.step_index = index
        self.active_index = self.order[index]
        self.active_started_at = time.perf_counter()
        for idx, button in enumerate(self.buttons):
            button.disabled = idx != self.active_index
            button.label = f"{_RACE_IMPULSE_EMOJI} {idx + 1}" if idx == self.active_index else str(idx + 1)
            button.style = discord.ButtonStyle.secondary

    def _close_current_step(self):
        for button in self.buttons:
            button.disabled = True
            button.label = str(button.index + 1)
            button.style = discord.ButtonStyle.secondary
        self.active_index = None
        self.active_started_at = 0.0
        current_step = self.step_index
        if current_step < 0:
            return
        for user_id in set(self.session.get("locked_participants", set()) or []):
            entry = self.results.setdefault(int(user_id), {"times": [None] * _RACE_IMPULSE_BUTTON_COUNT})
            if entry["times"][current_step] is None:
                entry["times"][current_step] = 1.0

    def _apply_results(self):
        pending = self.session.setdefault("pending_impulse_bonus", {})
        for user_id, entry in self.results.items():
            times = entry.get("times") or []
            total_points = 0.0
            hits = 0
            for reaction_time in times:
                if reaction_time is None or reaction_time >= 1.0:
                    continue
                hits += 1
                total_points += max(0.0, 1.0 - float(reaction_time))
            if hits <= 0:
                continue
            bonus = min(0.72, round(total_points * 0.12, 3))
            if bonus <= 0:
                continue
            pending[int(user_id)] = float(pending.get(int(user_id), 0.0)) + bonus


class _RaceStateView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaCorridaMixin", guild: discord.Guild, session: dict, *, finished: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild = guild
        self.session = session
        self.finished = finished

        condition_name = str((session.get("condition") or {}).get("name") or "Pista seca")
        special_name = str((session.get("special") or {}).get("name") or "")
        narration = str(session.get("narration") or ("🏁 Todos cruzaram a linha." if finished else "📣 A corrida começou."))
        lines = cog._build_race_lines(guild, session)

        if finished:
            title = "# 🏁 Corrida encerrada"
        else:
            title = "# 🔥 Reta final" if session.get("final_stretch") else "# 🐎 Corrida em andamento"

        header_lines = [title, f"**Condição:** {condition_name}"]
        if special_name:
            header_lines.append(f"**Especial:** {special_name}")

        track_lines = list(lines)
        if narration:
            track_lines += ["", narration]
        impulse_status = str(session.get("impulse_status") or "").strip()
        if impulse_status:
            track_lines.append(impulse_status)

        items = [
            discord.ui.TextDisplay("\n".join(header_lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(track_lines)),
        ]

        result_lines = session.get("result_lines") or []
        if finished and result_lines:
            items.extend([
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n".join(result_lines)),
            ])

        container = discord.ui.Container(*items, accent_color=cog._race_color(session, finished=finished))
        self.add_item(container)


class GincanaCorridaMixin:
    def _get_race_session(self, guild_id: int) -> dict | None:
        session = self._race_sessions.get(guild_id)
        if session and session.get("ended"):
            self._race_sessions.pop(guild_id, None)
            return None
        return session

    def _get_race_voice_channel(self, guild: discord.Guild, session: dict) -> discord.VoiceChannel | None:
        channel = guild.get_channel(int(session.get("voice_channel_id") or 0))
        return channel if isinstance(channel, discord.VoiceChannel) else None

    def _get_race_participants(self, guild: discord.Guild, session: dict) -> list[discord.Member]:
        participants: list[discord.Member] = []
        for user_id in sorted(session.get("locked_participants", set())):
            member = guild.get_member(int(user_id))
            if member is None or getattr(member, "bot", False):
                continue
            participants.append(member)
        return participants

    def _race_placement_emoji(self, index: int) -> str:
        return {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, "🔘")

    def _race_color(self, session: dict, *, finished: bool = False) -> discord.Color:
        if finished:
            return discord.Color.green()
        if not session.get("started"):
            special = session.get("special") or {}
            return special.get("color") or discord.Color.blurple()
        if session.get("final_stretch"):
            return discord.Color.red()
        return discord.Color.orange()

    def _render_race_track(self, pos: float, state_emoji: str) -> str:
        visual_pos = int(pos)
        visual_pos = max(0, min(_CORRIDA_TRACK_LENGTH - 1, visual_pos))
        before = "▰" * visual_pos
        after = "▱" * max(0, _CORRIDA_TRACK_LENGTH - visual_pos - 1)
        return f"{before}{state_emoji}{after}"

    def _ordered_race_members(self, guild: discord.Guild, session: dict) -> list[tuple[int, discord.Member]]:
        participants = self._get_race_participants(guild, session)
        progress_map = session.get("progress", {}) or {}
        arrival_groups = [list(group) for group in (session.get("arrival_groups") or []) if group]
        rank_map = _shared_rank_map(arrival_groups)
        ordered = sorted(
            participants,
            key=lambda m: (rank_map.get(m.id, 9999), -float(progress_map.get(m.id, 0.0)), m.display_name.casefold()),
        )
        return [(rank_map.get(member.id, 9999), member) for member in ordered]

    def _build_race_lines(self, guild: discord.Guild, session: dict) -> list[str]:
        ordered_with_ranks = self._ordered_race_members(guild, session)
        if not ordered_with_ranks:
            return ["🔘 Ninguém entrou ainda."]

        progress_map = session.get("progress", {}) or {}
        state_map = session.get("state_map", {}) or {}
        rank_map = {member.id: rank for rank, member in ordered_with_ranks}
        lines: list[str] = []
        for _index, (rank, member) in enumerate(ordered_with_ranks, start=1):
            medal = self._race_placement_emoji(rank)
            pos = float(progress_map.get(member.id, 0.0))
            state_emoji = str(state_map.get(member.id) or _HORSE_START)
            if rank_map.get(member.id, 9999) != 9999 and state_emoji == _HORSE_FINISH:
                pos = _CORRIDA_TRACK_LENGTH - 1
            lines.append(f"{medal} {member.mention}")
            lines.append(self._render_race_track(pos, state_emoji))
            lines.append("")
        if lines and not lines[-1]:
            lines.pop()
        return lines

    def _make_race_embed(self, guild: discord.Guild, session: dict, *, finished: bool = False) -> discord.Embed:
        pot_total = len(session.get("locked_participants", set())) * CORRIDA_STAKE + int(session.get("bonus_pool", 0) or 0)
        title = "🐎 Corrida aberta"
        if session.get("started"):
            title = "🏁 Corrida encerrada" if finished else ("🔥 Reta final" if session.get("final_stretch") else "🐎 Corrida em andamento")

        condition_name = str((session.get("condition") or {}).get("name") or "Pista seca")
        special_name = str((session.get("special") or {}).get("name") or "")
        narration = str(session.get("narration") or "📣 A corrida vai começar.")
        lines = self._build_race_lines(guild, session)
        description_parts = [f"Condição: **{condition_name}**"]
        if special_name:
            description_parts.append(f"Especial: **{special_name}**")
        description_parts.append("")
        description_parts.extend(lines)
        description_parts.append("")
        description_parts.append("────────")
        description_parts.append(narration)
        embed = discord.Embed(title=title, description="\n".join(description_parts), color=self._race_color(session, finished=finished))

        if not session.get("started"):
            embed.add_field(name="Entrada", value=self._chip_amount(CORRIDA_STAKE), inline=True)
            embed.add_field(name="Pote atual", value=self._chip_amount(pot_total), inline=True)
            embed.add_field(name="Duração", value=f"**{_CORRIDA_DURATION_SECONDS}s**", inline=True)
            embed.set_footer(text="Entre no lobby. O criador ou a staff pode iniciar com 🏁 quando houver pelo menos 2 participantes.")
        return embed

    async def _close_lobby_message(self, session: dict, guild: discord.Guild, *, title: str, detail: str):
        lobby_message = session.get("message")
        if lobby_message is None:
            return
        try:
            closed_view = _RaceLobbyClosedView(session, guild, title, detail)
            await lobby_message.edit(view=closed_view)
        except Exception:
            pass

    async def _handle_race_button(self, interaction: discord.Interaction, view: _RaceLobbyView):
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            try:
                await interaction.response.send_message("Servidor inválido.", ephemeral=True)
            except Exception:
                pass
            return

        session = self._get_race_session(guild.id)
        if session is None or session.get("ended") or session.get("started"):
            try:
                await interaction.response.send_message("Essa corrida não está mais aceitando entradas.", ephemeral=True)
            except Exception:
                pass
            return

        locked = session.setdefault("locked_participants", set())
        if user.id in locked:
            try:
                await interaction.response.send_message("Você já entrou nessa corrida.", ephemeral=True)
            except Exception:
                pass
            return

        paid, _balance, chip_note = await self._try_consume_chips(guild.id, user.id, CORRIDA_STAKE)
        if not paid:
            try:
                await interaction.response.send_message(chip_note or "Você não tem saldo suficiente para entrar nessa corrida.", ephemeral=True)
            except Exception:
                pass
            return

        locked.add(user.id)
        session.setdefault("progress", {})[user.id] = 0.0
        session.setdefault("state_map", {})[user.id] = _HORSE_START
        view.join_button.label = f"🐎 Entrar ({len(self._get_race_participants(guild, session))})"
        try:
            await interaction.response.send_message(chip_note or f"Você entrou na corrida pagando {self._chip_amount(CORRIDA_STAKE)}.", ephemeral=True)
        except Exception:
            pass
        await self._refresh_race_message(guild.id)

    async def _handle_race_start_button(self, interaction: discord.Interaction, view: _RaceLobbyView):
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            try:
                await interaction.response.send_message("Servidor inválido.", ephemeral=True)
            except Exception:
                pass
            return

        session = self._get_race_session(guild.id)
        if session is None or session.get("ended") or session.get("started"):
            try:
                await interaction.response.send_message("Essa corrida já foi iniciada.", ephemeral=True)
            except Exception:
                pass
            return

        is_owner = int(session.get("owner_id") or 0) == user.id
        if not is_owner and not self._is_staff_member(user):
            try:
                await interaction.response.send_message("Só o criador da corrida ou a staff pode iniciar.", ephemeral=True)
            except Exception:
                pass
            return

        participants = self._get_race_participants(guild, session)
        if len(participants) < 2:
            try:
                await interaction.response.send_message("A corrida precisa de pelo menos 2 participantes para começar.", ephemeral=True)
            except Exception:
                pass
            return

        try:
            await interaction.response.defer()
        except Exception:
            return

        try:
            await self._finish_race_lobby(guild.id, reason="manual_start", source_view=view)
        except Exception:
            try:
                await interaction.followup.send("Não foi possível iniciar a corrida agora.", ephemeral=True)
            except Exception:
                pass

    async def _refresh_race_message(self, guild_id: int):
        session = self._get_race_session(guild_id)
        if session is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        message = session.get("message")
        if message is None:
            return
        edit_lock = session.setdefault("_edit_lock", asyncio.Lock())
        async with edit_lock:
            try:
                old_view = session.get("view")
                if not session.get("started"):
                    view = _RaceLobbyView(self, guild_id, session, guild, timeout=_CORRIDA_LOBBY_SECONDS)
                else:
                    view = _RaceStateView(self, guild, session, finished=bool(session.get("ended")))
                session["view"] = view
                if old_view is not None and old_view is not view and isinstance(old_view, (discord.ui.View, discord.ui.LayoutView)):
                    try:
                        old_view.stop()
                    except Exception:
                        pass
                await message.edit(view=view)
            except Exception:
                pass

    def _nominal_race_pools(self, participant_count: int, pot_total: int) -> list[int]:
        if participant_count <= 0 or pot_total <= 0:
            return []
        if participant_count == 2:
            return [pot_total]
        if participant_count <= 4:
            first_pool = int(round(pot_total * 0.75))
            second_pool = max(0, pot_total - first_pool)
            return [first_pool, second_pool]
        first_pool = int(round(pot_total * 0.6))
        second_pool = int(round(pot_total * 0.25))
        third_pool = max(0, pot_total - first_pool - second_pool)
        return [first_pool, second_pool, third_pool]

    def _allocate_race_rewards(self, arrival_groups: list[list[discord.Member]], pot_total: int) -> tuple[dict[int, int], list[tuple[str, list[discord.Member], int]]]:
        rewards: dict[int, int] = {}
        placements: list[tuple[str, list[discord.Member], int]] = []
        flat_participants = [member for group in arrival_groups for member in group]
        if not flat_participants or pot_total <= 0:
            return rewards, placements

        pools = self._nominal_race_pools(len(flat_participants), pot_total)
        if not pools:
            return rewards, placements

        next_rank = 1
        for group in arrival_groups:
            if not group:
                continue
            start_index = next_rank - 1
            end_index = min(len(pools), start_index + len(group))
            occupied_pool = sum(pools[start_index:end_index])
            if occupied_pool > 0:
                base_share = occupied_pool // len(group)
                remainder = occupied_pool % len(group)
                ordered_group = sorted(group, key=lambda m: m.display_name.casefold())
                for idx, member in enumerate(ordered_group):
                    rewards[member.id] = rewards.get(member.id, 0) + base_share + (1 if idx < remainder else 0)
                placements.append((self._race_placement_emoji(next_rank), ordered_group, occupied_pool))
            next_rank += len(group)
            if next_rank > len(pools):
                break
        return rewards, placements

    def _pick_race_narration(self, participants: list[discord.Member], tick_events: list[tuple[str, discord.Member]], *, tick: int, final_tick: bool = False) -> str:
        if final_tick:
            return "🏁 Todos cruzaram a linha."
        event_lines: list[str] = []
        for event_key, member in tick_events:
            if event_key == "boost":
                event_lines.append(f"⚡ {member.mention} ganhou impulso.")
            elif event_key == "trip":
                event_lines.append(f"💥 {member.mention} tropeçou.")
            if len(event_lines) >= 2:
                break
        if event_lines:
            return "\n".join(event_lines[:2])
        if participants and tick % 3 == 1:
            leader = participants[0]
            return f"👀 {leader.mention} na frente."
        if tick >= _CORRIDA_UPDATES - 3:
            return "🏁 Últimos metros."
        return random.choice([
            "👀 Tudo embolado.",
            "↗️ A disputa apertou.",
            "🐎 A corrida segue aberta.",
            "👀 A corrida está acirrada.",
            "👀 Segue parelha.",
            "",
        ])

    def _build_finalized_order(self, guild: discord.Guild, session: dict) -> list[discord.Member]:
        return [member for _rank, member in self._ordered_race_members(guild, session)]

    def _build_arrival_member_groups(self, guild: discord.Guild, session: dict) -> list[list[discord.Member]]:
        participants = self._get_race_participants(guild, session)
        participant_ids = {member.id for member in participants}
        progress = session.get("progress", {}) or {}
        groups: list[list[discord.Member]] = []
        for raw_group in session.get("arrival_groups", []) or []:
            members = [guild.get_member(int(user_id)) for user_id in raw_group]
            valid_members = [member for member in members if member is not None and member.id in participant_ids]
            if valid_members:
                groups.append(valid_members)
        arrived_ids = {member.id for group in groups for member in group}
        leftovers = sorted(
            [member for member in participants if member.id not in arrived_ids],
            key=lambda m: (-float(progress.get(m.id, 0.0)), m.display_name.casefold()),
        )
        for member in leftovers:
            groups.append([member])
        return groups

    async def _delete_impulse_message(self, message: discord.Message | None):
        if message is None:
            return
        try:
            await asyncio.sleep(1.5)
            await message.delete()
        except Exception:
            pass

    async def _run_race_impulse_event(self, guild: discord.Guild, session: dict, stage_name: str):
        if session.get("ended"):
            return
        channel = guild.get_channel(int(session.get("text_channel_id") or 0))
        if channel is None or not hasattr(channel, "send"):
            return

        event_view = _RaceImpulseEventView(self, guild, session, stage_name)
        session["impulse_status"] = f"⚡ Evento de impulso ({stage_name.lower()}) em andamento."
        await self._refresh_race_message(guild.id)
        try:
            event_message = await channel.send(view=event_view)
        except Exception:
            session["impulse_status"] = f"⚡ Evento de impulso ({stage_name.lower()}) falhou."
            await self._refresh_race_message(guild.id)
            return

        event_view.message = event_message
        await event_view.refresh_message()
        await asyncio.sleep(_RACE_IMPULSE_INITIAL_DELAY)
        for step_index in range(_RACE_IMPULSE_BUTTON_COUNT):
            if session.get("ended"):
                break
            event_view._activate_step(step_index)
            await event_view.refresh_message()
            await asyncio.sleep(_RACE_IMPULSE_STEP_SECONDS)
            event_view._close_current_step()
            await event_view.refresh_message()

        event_view.finished = True
        event_view._close_current_step()
        event_view._apply_results()
        await event_view.refresh_message()
        participant_count = len(self._get_race_participants(guild, session))
        hit_count = sum(1 for result in event_view.results.values() if any((time_value is not None and time_value < 1.0) for time_value in result.get("times", [])))
        session["impulse_status"] = f"⚡ Impulsos de {stage_name.lower()} aplicados ({hit_count}/{participant_count})."
        await self._refresh_race_message(guild.id)
        await self._delete_impulse_message(event_message)

    async def _finish_race_lobby(self, guild_id: int, *, reason: str, source_view: discord.ui.LayoutView | None = None) -> bool:
        session = self._get_race_session(guild_id)
        if session is None or session.get("ended") or session.get("started") or session.get("starting"):
            return False
        if source_view is not None and session.get("view") is not source_view:
            return False
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            self._race_sessions.pop(guild_id, None)
            return False

        session["starting"] = True
        session["started"] = True
        session["narration"] = "📣 A corrida começou."
        session["arrival_groups"] = []
        session["pending_impulse_bonus"] = {}
        session["impulse_status"] = ""
        session["impulse_tasks"] = []
        session["impulse_ticks_fired"] = set()
        lobby_message = session.get("message")
        view = session.get("view")
        if isinstance(view, (discord.ui.View, discord.ui.LayoutView)):
            try:
                view.stop()
            except Exception:
                pass
        session["view"] = None

        participants = self._get_race_participants(guild, session)
        locked_ids = set(session.get("locked_participants", set()))
        if len(locked_ids) == 1:
            only_id = next(iter(locked_ids))
            await self.db.add_user_chips(guild.id, only_id, CORRIDA_STAKE)
            session["starting"] = False
            session["ended"] = True
            await self._close_lobby_message(session, guild, title="🐎 Corrida cancelada", detail="Só 1 jogador entrou. A entrada foi devolvida.")
            self._race_sessions.pop(guild_id, None)
            return True
        if len(participants) < 2:
            for user_id in locked_ids:
                await self.db.add_user_chips(guild.id, user_id, CORRIDA_STAKE)
            session["starting"] = False
            session["ended"] = True
            await self._close_lobby_message(session, guild, title="🐎 Corrida cancelada", detail="Não ficaram participantes suficientes. As entradas foram devolvidas.")
            self._race_sessions.pop(guild_id, None)
            return True

        progress = session.setdefault("progress", {})
        state_map = session.setdefault("state_map", {})
        for member in participants:
            progress[member.id] = 0.0
            state_map[member.id] = _HORSE_START
            await self._record_game_played(guild.id, member.id, weekly_points=4)

        await self._refresh_race_message(guild.id)
        await asyncio.sleep(1.0)

        condition = session.get("condition") or {}
        special = session.get("special") or {}
        tick = 0
        track_end = float(_CORRIDA_TRACK_LENGTH - 1)

        while True:
            if session.get("ended"):
                return False
            participants = self._get_race_participants(guild, session)
            if len(participants) < 2:
                break
            arrival_groups: list[list[int]] = session.setdefault("arrival_groups", [])
            arrived_ids = {int(user_id) for group in arrival_groups for user_id in group}
            if len(arrived_ids) >= len(participants):
                break

            if tick in _RACE_IMPULSE_WINDOWS and tick not in session.setdefault("impulse_ticks_fired", set()):
                stage_name = {1: "Começo", 7: "Meio", 12: "Final"}.get(tick, "Impulso")
                task = asyncio.create_task(self._run_race_impulse_event(guild, session, stage_name))
                session.setdefault("impulse_tasks", []).append(task)
                session.setdefault("impulse_ticks_fired", set()).add(tick)

            tick_events: list[tuple[str, discord.Member]] = []
            ordered_before = self._build_finalized_order(guild, session)
            leader_before = ordered_before[0].id if ordered_before else 0
            finishers_this_tick: list[int] = []

            for member in participants:
                if member.id in arrived_ids:
                    progress[member.id] = track_end
                    state_map[member.id] = _HORSE_FINISH
                    continue

                cur = float(progress.get(member.id, 0.0))
                boost_chance = 0.16 + float(condition.get("boost", 0.0)) + float(special.get("boost", 0.0))
                trip_chance = 0.12 + float(condition.get("trip", 0.0)) + float(special.get("trip", 0.0))
                speed_bonus = float(condition.get("speed", 0.0)) + float(special.get("speed", 0.0))

                if special.get("zebra") and cur <= 2.0:
                    boost_chance += 0.07

                if session.get("final_stretch"):
                    boost_chance += 0.06
                    if cur <= track_end * 0.55:
                        boost_chance += 0.04
                    trip_chance = max(0.02, trip_chance - 0.03)
                if tick >= _CORRIDA_UPDATES:
                    boost_chance += 0.08
                    trip_chance = max(0.01, trip_chance - 0.05)

                pending_impulse = float(session.setdefault("pending_impulse_bonus", {}).pop(member.id, 0.0) or 0.0)
                if pending_impulse > 0:
                    boost_chance += 0.05

                if tick == 0 and random.random() < boost_chance + 0.06:
                    move = 1.0 + pending_impulse
                    state_map[member.id] = _HORSE_BOOST
                    tick_events.append(("boost", member))
                elif random.random() < trip_chance and cur < track_end - 0.5:
                    move = max(0.0, pending_impulse * 0.5)
                    state_map[member.id] = _HORSE_TRIP
                    tick_events.append(("trip", member))
                else:
                    base_move = random.uniform(0.36, 0.78)
                    if speed_bonus > 0:
                        base_move += min(0.18, speed_bonus * 0.32)
                    elif speed_bonus < 0:
                        base_move += max(-0.18, speed_bonus * 0.28)
                    if session.get("final_stretch"):
                        base_move += 0.12
                    if tick >= _CORRIDA_UPDATES:
                        base_move += 0.15
                    if random.random() < boost_chance:
                        base_move += 0.30
                        state_map[member.id] = _HORSE_BOOST
                        tick_events.append(("boost", member))
                    else:
                        state_map[member.id] = _HORSE_RUN
                    move = max(0.0, min(1.3, base_move + pending_impulse))

                new_pos = min(track_end, cur + move)
                progress[member.id] = new_pos
                if new_pos >= track_end - 1e-9:
                    finishers_this_tick.append(member.id)
                    state_map[member.id] = _HORSE_FINISH

            if finishers_this_tick:
                arrival_groups.append(sorted(finishers_this_tick))
                arrived_ids = {int(user_id) for group in arrival_groups for user_id in group}

            leader_progress = max((float(progress.get(member.id, 0.0)) for member in participants), default=0.0)
            session["final_stretch"] = leader_progress >= track_end * 0.72 or tick >= _CORRIDA_UPDATES - 3
            ordered_after = self._build_finalized_order(guild, session)
            leader_after = ordered_after[0].id if ordered_after else 0
            if len(arrived_ids) >= len(participants):
                session["narration"] = self._pick_race_narration(ordered_after, tick_events, tick=tick, final_tick=True)
            elif finishers_this_tick:
                if len(finishers_this_tick) > 1:
                    finishers = [guild.get_member(int(user_id)) for user_id in finishers_this_tick]
                    finishers = [member for member in finishers if member is not None]
                    names = ", ".join(member.mention for member in finishers[:3])
                    session["narration"] = f"🏁 {names} cruzaram juntos!" if names else "🏁 Houve empate na chegada!"
                else:
                    finisher = guild.get_member(int(finishers_this_tick[0]))
                    session["narration"] = f"🏁 {finisher.mention} cruzou a linha." if finisher else "🏁 Um corredor cruzou a linha."
            elif leader_after and leader_after != leader_before:
                leader = guild.get_member(leader_after)
                session["narration"] = f"↗️ {leader.mention} assumiu a liderança." if leader else "↗️ A liderança mudou."
            else:
                session["narration"] = self._pick_race_narration(ordered_after, tick_events, tick=tick)
            await self._refresh_race_message(guild.id)
            tick += 1
            await asyncio.sleep(_CORRIDA_UPDATE_SECONDS)

        for task in list(session.get("impulse_tasks", [])):
            try:
                await task
            except Exception:
                pass

        final_groups = self._build_arrival_member_groups(guild, session)
        final_order = [member for group in final_groups for member in group]
        for member in final_order:
            progress[member.id] = _CORRIDA_TRACK_LENGTH - 1
            state_map[member.id] = _HORSE_FINISH

        session["ended"] = True
        total_pot = len(locked_ids) * CORRIDA_STAKE + int(session.get("bonus_pool", 0) or 0)
        rewards, placements = self._allocate_race_rewards(final_groups, total_pot)
        result_lines: list[str] = []
        if final_groups:
            first_group = final_groups[0]
            if len(first_group) == 1:
                winner = first_group[0]
                winner_amount = int(rewards.get(winner.id, 0) or 0)
                result_lines.append(f"🏆 {winner.mention} venceu a corrida — {self._chip_text(winner_amount, kind='gain')}")
            else:
                winner_amount = int(sum(rewards.get(member.id, 0) for member in first_group) or 0)
                names = ", ".join(member.mention for member in first_group)
                result_lines.append(f"🏆 Empate em 1º: {names} — {self._chip_text(winner_amount, kind='gain')} no topo")
        for badge, members, total in placements:
            if members:
                names = ", ".join(member.mention for member in members)
                result_lines.append(f"{badge} {names} — {self._chip_amount(total)}")
        session["narration"] = "🏁 Todos cruzaram a linha."
        session["impulse_status"] = ""

        rank_map = _shared_rank_map([[member.id for member in group] for group in final_groups])
        for member in final_order:
            rank = rank_map.get(member.id, 9999)
            if rank <= 3:
                await self.db.add_user_game_stat(guild.id, member.id, "corrida_podiums", 1)
                await self._grant_weekly_points(guild.id, member.id, max(3, 5 - rank))
        if final_groups and len(final_groups[0]) == 1:
            await self.db.add_user_game_stat(guild.id, final_groups[0][0].id, "corrida_wins", 1)
        losing_ids = set(locked_ids)
        if final_groups:
            for winner in final_groups[0]:
                losing_ids.discard(winner.id)
        for user_id in losing_ids:
            await self.db.add_user_game_stat(guild.id, int(user_id), "corrida_losses", 1)
        for user_id, amount in rewards.items():
            if amount > 0:
                await self.db.add_user_chips(guild.id, user_id, amount)
                await self._grant_weekly_points(guild.id, user_id, max(4, amount // 4))

        session["starting"] = False
        session["result_lines"] = result_lines
        message = session.get("message")
        if message is not None:
            try:
                final_view = _RaceStateView(self, guild, session, finished=True)
                session["view"] = final_view
                await message.edit(view=final_view)
            except Exception:
                pass

        self._race_sessions.pop(guild_id, None)
        return True

    async def _handle_corrida_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False
        if not self._matches_exact_trigger(message.content or "", "corrida"):
            return False
        if not self.db.gincana_enabled(guild.id):
            return True
        if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True
        if self._get_race_session(guild.id) is not None:
            return True

        voice_channel = getattr(getattr(message.author, "voice", None), "channel", None)

        paid, _balance, chip_note = await self._try_consume_chips(guild.id, message.author.id, CORRIDA_STAKE)
        if not paid:
            try:
                await message.channel.send(embed=self._make_embed("🐎 Saldo insuficiente", chip_note or "Você não tem saldo suficiente.", ok=False))
            except Exception:
                pass
            return True

        condition = random.choice(_RACE_CONDITIONS)
        special = random.choice(_RACE_SPECIALS) if random.random() < 0.18 else None
        bonus_pool = int((special or {}).get("bonus_pool", 0) or 0)

        session = {
            "voice_channel_id": getattr(voice_channel, "id", 0),
            "text_channel_id": message.channel.id,
            "owner_id": message.author.id,
            "locked_participants": {message.author.id},
            "progress": {message.author.id: 0.0},
            "state_map": {message.author.id: _HORSE_START},
            "arrival_groups": [],
            "pending_impulse_bonus": {},
            "message": None,
            "view": None,
            "ended": False,
            "started": False,
            "final_stretch": False,
            "narration": "📣 A corrida vai começar.",
            "condition": dict(condition),
            "special": dict(special) if special else None,
            "bonus_pool": bonus_pool,
            "starting": False,
            "impulse_status": "",
            "impulse_tasks": [],
            "impulse_ticks_fired": set(),
            "_edit_lock": asyncio.Lock(),
        }
        self._race_sessions[guild.id] = session
        view = _RaceLobbyView(self, guild.id, session, guild, timeout=_CORRIDA_LOBBY_SECONDS)
        session["view"] = view
        try:
            panel_message = await message.channel.send(view=view)
        except Exception:
            self._race_sessions.pop(guild.id, None)
            await self.db.add_user_chips(guild.id, message.author.id, CORRIDA_STAKE)
            return True

        session["message"] = panel_message
        await self._react_with_emoji(message, "🐎", keep=True)
        return True
