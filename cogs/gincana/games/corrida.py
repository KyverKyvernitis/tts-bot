import asyncio
import random

import discord


CORRIDA_STAKE = 10
_CORRIDA_TRACK_LENGTH = 8
_CORRIDA_UPDATES = 10
_CORRIDA_UPDATE_SECONDS = 2.0
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
        header_lines.append("**Duração:** **20s**")

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




class _RaceStateView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaCorridaMixin", guild: discord.Guild, session: dict, *, finished: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild = guild
        self.session = session
        self.finished = finished

        condition_name = str((session.get("condition") or {}).get("name") or "Pista seca")
        special_name = str((session.get("special") or {}).get("name") or "")
        narration = str(session.get("narration") or ("🏁 Todo mundo cruzou a linha." if finished else "📣 A corrida começou."))
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
            track_lines += ["", "────────", narration]

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

    def _build_race_lines(self, guild: discord.Guild, session: dict) -> list[str]:
        participants = self._get_race_participants(guild, session)
        if not participants:
            return ["🔘 Ninguém entrou ainda."]

        progress_map = session.get("progress", {}) or {}
        state_map = session.get("state_map", {}) or {}
        arrival_order = list(session.get("arrival_order", []))
        arrival_rank = {uid: index for index, uid in enumerate(arrival_order, start=1)}
        ordered = sorted(
            participants,
            key=lambda m: (arrival_rank.get(m.id, 9999), -float(progress_map.get(m.id, 0.0)), m.display_name.casefold()),
        )
        lines: list[str] = []
        for index, member in enumerate(ordered, start=1):
            medal = self._race_placement_emoji(index)
            pos = float(progress_map.get(member.id, 0.0))
            state_emoji = str(state_map.get(member.id) or _HORSE_START)
            if member.id in arrival_rank:
                state_emoji = _HORSE_FINISH
                pos = _CORRIDA_TRACK_LENGTH - 1
            lines.append(f"{medal} {member.mention} | {self._render_race_track(pos, state_emoji)}")
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
            embed.add_field(name="Duração", value="**20s**", inline=True)
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

    def _allocate_race_rewards(self, participants: list[discord.Member], pot_total: int) -> tuple[dict[int, int], list[tuple[str, list[discord.Member], int]]]:
        rewards: dict[int, int] = {}
        placements: list[tuple[str, list[discord.Member], int]] = []
        if not participants or pot_total <= 0:
            return rewards, placements
        if len(participants) == 2:
            winner = participants[0]
            rewards[winner.id] = pot_total
            return rewards, [("🥇", [winner], pot_total)]
        if len(participants) <= 4:
            first_pool = int(round(pot_total * 0.75))
            second_pool = max(0, pot_total - first_pool)
            rewards[participants[0].id] = first_pool
            rewards[participants[1].id] = second_pool
            placements.extend([("🥇", [participants[0]], first_pool), ("🥈", [participants[1]], second_pool)])
            return rewards, placements
        first_pool = int(round(pot_total * 0.6))
        second_pool = int(round(pot_total * 0.25))
        third_pool = max(0, pot_total - first_pool - second_pool)
        pools = [first_pool, second_pool, third_pool]
        badges = ["🥇", "🥈", "🥉"]
        for index, total in enumerate(pools):
            if index >= len(participants) or total <= 0:
                continue
            rewards[participants[index].id] = total
            placements.append((badges[index], [participants[index]], total))
        return rewards, placements

    def _pick_race_narration(self, participants: list[discord.Member], tick_events: list[tuple[str, discord.Member]], *, tick: int, final_tick: bool = False) -> str:
        if final_tick:
            return "🏁 Todo mundo cruzou a linha."
        event_lines: list[str] = []
        for event_key, member in tick_events:
            if event_key == "boost":
                event_lines.append(f"⚡ {member.mention} largou melhor.")
            elif event_key == "trip":
                event_lines.append(f"💥 {member.mention} tropeçou.")
            if len(event_lines) >= 2:
                break
        if event_lines:
            return "\n".join(event_lines[:2])
        if tick >= _CORRIDA_UPDATES - 3:
            return "🏁 Últimos metros."
        if tick == 0:
            return "📣 A corrida começou."
        simple_lines = [
            "👀 Tudo embolado.",
            "↗️ A disputa apertou.",
            "🐎 A corrida segue aberta.",
            "💨 Ninguém abriu folga.",
            "👀 Segue parelha.",
            ""
        ]
        if participants and tick % 3 == 1:
            leader = participants[0]
            return f"👀 {leader.mention} na frente."
        return random.choice(simple_lines)

    def _build_finalized_order(self, guild: discord.Guild, session: dict) -> list[discord.Member]:
        participants = self._get_race_participants(guild, session)
        progress = session.get("progress", {}) or {}
        arrival_order = list(session.get("arrival_order", []))
        in_order: list[discord.Member] = []
        seen: set[int] = set()
        for uid in arrival_order:
            member = guild.get_member(int(uid))
            if member is None or member not in participants:
                continue
            in_order.append(member)
            seen.add(uid)
        leftovers = sorted(
            [m for m in participants if m.id not in seen],
            key=lambda m: (-float(progress.get(m.id, 0.0)), m.display_name.casefold()),
        )
        return in_order + leftovers

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
        session["arrival_order"] = []
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
        for tick in range(_CORRIDA_UPDATES):
            if session.get("ended"):
                return False
            participants = self._get_race_participants(guild, session)
            if len(participants) < 2:
                break
            tick_events: list[tuple[str, discord.Member]] = []
            ordered_before = sorted(participants, key=lambda m: (-float(progress.get(m.id, 0.0)), m.display_name.casefold()))
            leader_before = ordered_before[0].id if ordered_before else 0
            arrival_order: list[int] = session.setdefault("arrival_order", [])
            final_tick = tick == _CORRIDA_UPDATES - 1

            for member in participants:
                if member.id in arrival_order:
                    progress[member.id] = _CORRIDA_TRACK_LENGTH - 1
                    state_map[member.id] = _HORSE_FINISH
                    continue

                cur = float(progress.get(member.id, 0.0))
                boost_chance = 0.16 + float(condition.get("boost", 0.0)) + float(special.get("boost", 0.0))
                trip_chance = 0.12 + float(condition.get("trip", 0.0)) + float(special.get("trip", 0.0))
                speed_bonus = float(condition.get("speed", 0.0)) + float(special.get("speed", 0.0))

                if special.get("zebra") and cur <= 2.0:
                    boost_chance += 0.07

                if session.get("final_stretch") and not final_tick:
                    boost_chance += 0.06
                    if cur <= 4.0:
                        boost_chance += 0.04
                    trip_chance = max(0.02, trip_chance - 0.03)

                if final_tick:
                    move = max(0.0, (_CORRIDA_TRACK_LENGTH - 1) - cur)
                    state_map[member.id] = _HORSE_FINISH
                elif tick == 0 and random.random() < boost_chance + 0.06:
                    move = 1.2
                    state_map[member.id] = _HORSE_BOOST
                    tick_events.append(("boost", member))
                elif random.random() < trip_chance:
                    move = 0.0
                    state_map[member.id] = _HORSE_TRIP
                    tick_events.append(("trip", member))
                else:
                    base_move = random.uniform(0.45, 0.9)
                    if speed_bonus > 0:
                        base_move += min(0.18, speed_bonus * 0.35)
                    elif speed_bonus < 0:
                        base_move += max(-0.18, speed_bonus * 0.30)
                    if session.get("final_stretch"):
                        base_move += 0.18
                    if random.random() < boost_chance:
                        base_move += 0.42
                        state_map[member.id] = _HORSE_BOOST
                        tick_events.append(("boost", member))
                    else:
                        state_map[member.id] = _HORSE_RUN
                    move = max(0.0, min(1.45, base_move))

                track_end = float(_CORRIDA_TRACK_LENGTH - 1)
                if not final_tick:
                    progress_cap = min(track_end - 0.15, ((tick + 1) / _CORRIDA_UPDATES) * track_end + (0.55 if session.get("final_stretch") else 0.2))
                    new_pos = min(progress_cap, cur + move)
                else:
                    new_pos = min(track_end, cur + move)
                progress[member.id] = new_pos
                if final_tick and new_pos >= track_end and member.id not in arrival_order:
                    arrival_order.append(member.id)
                    state_map[member.id] = _HORSE_FINISH

            session["final_stretch"] = tick >= _CORRIDA_UPDATES - 3
            ordered_after = self._build_finalized_order(guild, session)
            leader_after = ordered_after[0].id if ordered_after else 0
            if final_tick:
                session["narration"] = self._pick_race_narration(ordered_after, tick_events, tick=tick, final_tick=True)
            elif leader_after and leader_after != leader_before:
                leader = guild.get_member(leader_after)
                session["narration"] = f"↗️ {leader.mention} assumiu a ponta." if leader else "↗️ A ponta mudou."
            else:
                session["narration"] = self._pick_race_narration(ordered_after, tick_events, tick=tick)
            await self._refresh_race_message(guild.id)
            await asyncio.sleep(_CORRIDA_UPDATE_SECONDS)

        final_order = self._build_finalized_order(guild, session)
        for member in final_order:
            progress[member.id] = _CORRIDA_TRACK_LENGTH - 1
            state_map[member.id] = _HORSE_FINISH

        session["ended"] = True
        total_pot = len(locked_ids) * CORRIDA_STAKE + int(session.get("bonus_pool", 0) or 0)
        rewards, placements = self._allocate_race_rewards(final_order, total_pot)
        result_lines = self._build_race_lines(guild, session)
        if final_order:
            result_lines.append("")
            result_lines.append(f"🏆 {final_order[0].mention} venceu a corrida.")
        for badge, members, amount in placements:
            if members and amount > 0:
                result_lines.append(f"{badge} {members[0].mention} — {self._chip_text(amount, kind='gain')}")
        session["narration"] = "🏁 Todo mundo cruzou a linha."

        for index, member in enumerate(final_order[:3], start=1):
            await self.db.add_user_game_stat(guild.id, member.id, "corrida_podiums", 1)
            await self._grant_weekly_points(guild.id, member.id, max(3, 5 - index))
        if final_order:
            await self.db.add_user_game_stat(guild.id, final_order[0].id, "corrida_wins", 1)
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
            "arrival_order": [],
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
