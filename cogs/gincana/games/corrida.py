import asyncio
import random
import time

import discord


CORRIDA_STAKE = 10
_CORRIDA_TRACK_LENGTH = 18
_CORRIDA_UPDATES = 10
_CORRIDA_UPDATE_SECONDS = 2.0
_CORRIDA_DURATION_SECONDS = int(_CORRIDA_UPDATES * _CORRIDA_UPDATE_SECONDS)
_CORRIDA_LOBBY_SECONDS = 20.0

_HORSE_START = "<:horse1:1485794648239636647>"
_HORSE_BOOST = "<:horse2:1485795177401417799>"
_HORSE_RUN = "<:horse2:1485795705745444995>"
_HORSE_TRIP = "<:horse2:1485795938990821547>"
_HORSE_FINISH = "<:Mine:1485797167494070524>"
_HORSE_DASH = "<:aaa:1486376725838430248>"

_RACE_CONDITIONS = [
    {"name": "Pista seca", "boost": 0.0, "trip": 0.0, "speed": 0.0},
    {"name": "Pista molhada", "boost": -0.02, "trip": 0.08, "speed": -0.15},
    {"name": "Pista pesada", "boost": -0.03, "trip": 0.04, "speed": -0.25},
    {"name": "Pista rápida", "boost": 0.08, "trip": -0.02, "speed": 0.2},
]
_RACE_CONDITION_WEIGHTS = (0.34, 0.28, 0.26, 0.12)

_RACE_SPECIALS = [
    {"name": "Corrida turbo", "boost": 0.12, "trip": -0.03, "speed": 0.35, "bonus_pool": 0, "color": discord.Color.dark_magenta()},
    {"name": "Corrida pesada", "boost": -0.05, "trip": 0.1, "speed": -0.25, "bonus_pool": 0, "color": discord.Color.dark_orange()},
    {"name": "Corrida de zebra", "boost": 0.04, "trip": 0.0, "speed": 0.0, "bonus_pool": 0, "zebra": True, "color": discord.Color.purple()},
    {"name": "Grande prêmio", "boost": 0.02, "trip": 0.0, "speed": 0.15, "bonus_pool": 10, "color": discord.Color.gold()},
]

_RACE_IMPULSE_WINDOWS_NORMAL = ((3, "Largada"), (7, "Sprint final"))
_RACE_IMPULSE_WINDOWS_FAST = ((2, "Largada"), (5, "Meio"), (8, "Sprint final"))
_RACE_IMPULSE_INITIAL_DELAY = 0.0
_RACE_IMPULSE_STEP_SECONDS = 1.0
_RACE_IMPULSE_BUTTON_COUNT = 3
_RACE_IMPULSE_STAGE_COUNT = 3
_RACE_IMPULSE_EMOJI = "⚡"
_RACE_IMPULSE_DELETE_DELAY_SECONDS = 1.0


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
        super().__init__(style=discord.ButtonStyle.secondary, label=str(index + 1), disabled=True, custom_id=f"race_impulse:{index}")
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
        self.last_best_user_id: int | None = None
        self.last_best_target_bonus: float = 0.0
        self.last_best_tier: str | None = None
        self.message: discord.Message | None = None
        self.finished = False
        self.step_index = -1
        self.active_index: int | None = None
        self.active_started_ns = 0
        self.order = random.sample(range(_RACE_IMPULSE_BUTTON_COUNT), _RACE_IMPULSE_STAGE_COUNT)
        self.results: dict[int, dict] = {}
        self.edit_lock = asyncio.Lock()
        self.press_lock = asyncio.Lock()
        self._last_render_signature = None
        self._results_applied = False
        self.activated_indices: set[int] = set()
        self.buttons = [_RaceImpulseButton(self, idx) for idx in range(_RACE_IMPULSE_BUTTON_COUNT)]
        self._rebuild()

    def _render_signature(self):
        return (
            bool(self.finished),
            int(self.step_index),
            self.active_index,
            tuple((button.disabled, button.label, int(button.style)) for button in self.buttons),
        )

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
                "Prepare-se. O primeiro botão pode acender a qualquer momento.",
            ]
        return [
            "# ⚡ Evento de impulso",
            f"**Fase:** {self.stage_name}",
            f"Aperte o botão cinza que acendeu. **Etapa {self.step_index + 1}/{_RACE_IMPULSE_STAGE_COUNT}**",
        ]

    def _rebuild(self):
        self.clear_items()
        row1 = discord.ui.ActionRow(self.buttons[0])
        row2 = discord.ui.ActionRow(self.buttons[1])
        row3 = discord.ui.ActionRow(self.buttons[2])
        container = discord.ui.Container(
            discord.ui.TextDisplay("\n".join(self._build_header_lines())),
            discord.ui.Separator(),
            row1,
            row2,
            row3,
            accent_color=discord.Color.dark_grey(),
        )
        self.add_item(container)

    async def refresh_message(self):
        if self.message is None:
            return
        async with self.edit_lock:
            signature = self._render_signature()
            if signature == self._last_render_signature:
                return
            self._rebuild()
            edit_state = await self.cog._safe_edit_message_view(self.message, self)
            if edit_state == "ok":
                self._last_render_signature = signature
            elif edit_state == "missing":
                self.message = None

    async def handle_press(self, interaction: discord.Interaction, button_index: int):
        user = interaction.user
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass
        async with self.press_lock:
            if self.finished or self.active_index is None or self.step_index < 0:
                return
            if interaction.guild is None or user is None:
                return
            user_id = int(getattr(user, "id", 0) or 0)
            if user_id <= 0:
                return
            if user_id not in {int(x) for x in (self.session.get("locked_participants", set()) or [])}:
                return

            entry = self.results.setdefault(user_id, {"times": [None] * _RACE_IMPULSE_STAGE_COUNT, "success": [False] * _RACE_IMPULSE_STAGE_COUNT})
            times = entry["times"]
            success = entry["success"]
            current_step = int(self.step_index)
            if current_step < 0 or current_step >= _RACE_IMPULSE_STAGE_COUNT:
                return
            if times[current_step] is not None:
                return
            if button_index != self.active_index:
                times[current_step] = _RACE_IMPULSE_STEP_SECONDS
                success[current_step] = False
                return

            reaction_time = max(0.0, min(_RACE_IMPULSE_STEP_SECONDS, (time.perf_counter_ns() - self.active_started_ns) / 1_000_000_000.0))
            times[current_step] = reaction_time
            success[current_step] = True

    def _activate_step(self, index: int):
        self.step_index = index
        self.active_index = self.order[index]
        self.active_started_ns = time.perf_counter_ns()
        self.activated_indices.add(int(self.active_index))
        for idx, button in enumerate(self.buttons):
            button.disabled = idx != self.active_index
            button.label = _RACE_IMPULSE_EMOJI if idx in self.activated_indices else str(idx + 1)
            button.style = discord.ButtonStyle.secondary

    def _close_current_step(self):
        if self.active_index is not None:
            self.activated_indices.add(int(self.active_index))
        for button in self.buttons:
            button.disabled = True
            button.label = _RACE_IMPULSE_EMOJI if button.index in self.activated_indices else str(button.index + 1)
            button.style = discord.ButtonStyle.secondary
        self.active_index = None
        self.active_started_ns = 0
        current_step = self.step_index
        if current_step < 0:
            return
        for user_id in set(self.session.get("locked_participants", set()) or []):
            entry = self.results.setdefault(int(user_id), {"times": [None] * _RACE_IMPULSE_STAGE_COUNT, "success": [False] * _RACE_IMPULSE_STAGE_COUNT})
            if entry["times"][current_step] is None:
                entry["times"][current_step] = _RACE_IMPULSE_STEP_SECONDS
                entry["success"][current_step] = False

    def _successful_steps(self, entry: dict) -> int:
        success = list(entry.get("success") or [])
        if success:
            return sum(1 for ok in success if bool(ok))
        times = list(entry.get("times") or [])
        return sum(1 for reaction_time in times if reaction_time is not None and float(reaction_time) < _RACE_IMPULSE_STEP_SECONDS)

    def _random_impulse_tier(self, hits: int) -> str | None:
        roll = random.random()
        if hits <= 0:
            return None
        if hits == 1:
            return "medio" if roll < 0.35 else "pequeno"
        if hits == 2:
            if roll < 0.35:
                return "grande"
            if roll < 0.82:
                return "medio"
            return "pequeno"
        if roll < 0.60:
            return "grande"
        if roll < 0.95:
            return "medio"
        return "pequeno"

    def _tier_bonus(self, tier: str, stage_name: str) -> float:
        stage_key = stage_name.strip().lower()
        bonus_table = {
            "largada": {"pequeno": (1.25, 1.65), "medio": (1.95, 2.55), "grande": (2.95, 3.75)},
            "meio": {"pequeno": (1.45, 1.90), "medio": (2.20, 2.95), "grande": (3.25, 4.20)},
            "sprint final": {"pequeno": (1.75, 2.25), "medio": (2.55, 3.35), "grande": (3.70, 4.80)},
        }
        ranges = bonus_table.get(stage_key, bonus_table["meio"])
        start, end = ranges.get(tier, (0.0, 0.0))
        return round(random.uniform(start, end), 4)

    def _tier_emoji(self, tier: str | None) -> str:
        return _HORSE_DASH if tier == "grande" else _HORSE_BOOST

    def _tier_label(self, tier: str | None) -> str:
        return {"pequeno": "impulso pequeno", "medio": "impulso médio", "grande": "impulso grande"}.get(str(tier or "").lower(), "impulso")

    def _apply_results(self, *, up_to_step: int | None = None):
        if self._results_applied:
            return list(self.session.get("recent_impulse_awards") or [])
        active_impulses = self.session.setdefault("active_impulses", {})
        state_map = self.session.setdefault("state_map", {})
        participants = [int(user_id) for user_id in set(self.session.get("locked_participants", set()) or [])]

        awarded: list[tuple[int, str, float, int]] = []
        self.last_best_user_id = None
        self.last_best_target_bonus = 0.0
        self.last_best_tier = None

        for user_id in participants:
            entry = self.results.setdefault(user_id, {"times": [None] * _RACE_IMPULSE_STAGE_COUNT, "success": [False] * _RACE_IMPULSE_STAGE_COUNT})
            times = list(entry.get("times") or [])
            if len(times) < _RACE_IMPULSE_STAGE_COUNT:
                times.extend([None] * (_RACE_IMPULSE_STAGE_COUNT - len(times)))
            hits = self._successful_steps(entry)
            tier = self._random_impulse_tier(hits)
            entry["hits"] = hits
            entry["tier"] = tier
            if not tier:
                continue
            bonus = self._tier_bonus(tier, self.stage_name)
            entry["bonus"] = bonus
            active_impulses[user_id] = {"kind": str(tier), "ticks_left": 2, "per_tick": float(bonus)}
            state_map[user_id] = self._tier_emoji(tier)
            awarded.append((user_id, tier, bonus, hits))

        awarded.sort(key=lambda item: ({"grande": 3, "medio": 2, "pequeno": 1}.get(item[1], 0), item[3], item[2], -item[0]), reverse=True)
        self.session["impulse_flash_users"] = {int(user_id) for user_id, _tier, _bonus, _hits in awarded}
        self.session["impulse_flash_levels"] = {int(user_id): str(tier) for user_id, tier, _bonus, _hits in awarded}
        self.session["recent_impulse_awards"] = [
            {"user_id": int(user_id), "tier": str(tier), "bonus": float(bonus), "stage": self.stage_name, "hits": int(hits)}
            for user_id, tier, bonus, hits in awarded[:3]
        ]
        if awarded:
            best_user_id, best_tier, best_bonus, _best_hits = awarded[0]
            self.last_best_user_id = int(best_user_id)
            self.last_best_target_bonus = float(best_bonus)
            self.last_best_tier = str(best_tier)
            mention_lines = []
            for user_id, tier, _bonus, _hits in awarded[:3]:
                member = self.guild.get_member(int(user_id))
                if member is not None:
                    mention_lines.append(f"{self._tier_emoji(tier)} {member.mention} recebeu {self._tier_label(tier)}.")
            if mention_lines:
                self.session["narration"] = "\n".join(mention_lines)
                self.session["narration_hold_ticks"] = 3
        else:
            self.session["recent_impulse_awards"] = []
            self.session["narration"] = ""
            self.session["narration_hold_ticks"] = 0

        self._results_applied = True
        return list(self.session.get("recent_impulse_awards") or [])



class _RaceStateView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaCorridaMixin", guild: discord.Guild, session: dict, *, finished: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild = guild
        self.session = session
        self.finished = finished

        condition_name = str((session.get("condition") or {}).get("name") or "Pista seca")
        special_name = str((session.get("special") or {}).get("name") or "")
        narration = str(session.get("narration") or ("🏁 Todos cruzaram a linha." if finished else ""))
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
    async def _safe_edit_message_view(self, message: discord.Message | None, view: discord.ui.View | discord.ui.LayoutView) -> str:
        if message is None:
            return "missing"
        try:
            await message.edit(view=view)
            return "ok"
        except discord.NotFound:
            return "missing"
        except discord.HTTPException:
            return "error"

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

    def _get_race_impulse_schedule(self, session: dict) -> tuple[tuple[int, str], ...]:
        condition_name = str((session.get("condition") or {}).get("name") or "").strip().lower()
        if condition_name == "pista rápida":
            return _RACE_IMPULSE_WINDOWS_FAST
        return _RACE_IMPULSE_WINDOWS_NORMAL


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
        active_impulses = {int(user_id): data for user_id, data in (session.get("active_impulses") or {}).items()}
        flash_users = {int(user_id) for user_id in (session.get("impulse_flash_users") or set())}
        flash_levels = {int(user_id): str(level) for user_id, level in (session.get("impulse_flash_levels") or {}).items()}
        rank_map = {member.id: rank for rank, member in ordered_with_ranks}
        lines: list[str] = []
        for _index, (rank, member) in enumerate(ordered_with_ranks, start=1):
            medal = self._race_placement_emoji(rank)
            pos = float(progress_map.get(member.id, 0.0))
            state_emoji = str(state_map.get(member.id) or _HORSE_START)
            impulse_state = active_impulses.get(member.id) or {}
            impulse_kind = str((impulse_state or {}).get("kind") or "").lower()
            if int((impulse_state or {}).get("ticks_left") or 0) > 0 and state_emoji not in {_HORSE_FINISH, _HORSE_TRIP}:
                state_emoji = _HORSE_DASH if impulse_kind == "grande" else _HORSE_BOOST
            elif member.id in flash_users and state_emoji not in {_HORSE_FINISH, _HORSE_TRIP}:
                flash_level = str(flash_levels.get(member.id) or "").lower()
                if flash_level == "grande":
                    state_emoji = _HORSE_DASH
                elif flash_level in {"medio", "médio", "pequeno"}:
                    state_emoji = _HORSE_BOOST
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
        narration = str(session.get("narration") or ("" if session.get("started") else "📣 A corrida vai começar."))
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
        closed_view = _RaceLobbyClosedView(session, guild, title, detail)
        edit_state = await self._safe_edit_message_view(lobby_message, closed_view)
        if edit_state == "missing":
            session["message"] = None

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
        if session is None or session.get("ended") or session.get("started") or session.get("starting"):
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

        session["starting"] = True
        try:
            await interaction.response.defer()
        except Exception:
            session["starting"] = False
            return

        try:
            started_ok = await self._finish_race_lobby(guild.id, reason="manual_start", source_view=view, allow_when_starting=True)
            if not started_ok:
                fresh_session = self._race_sessions.get(guild.id)
                if fresh_session is not None and not fresh_session.get("ended"):
                    fresh_session["starting"] = False
                try:
                    await interaction.followup.send("Não foi possível iniciar a corrida agora.", ephemeral=True)
                except Exception:
                    pass
                return
        except Exception:
            fresh_session = self._race_sessions.get(guild.id)
            if fresh_session is not None and not fresh_session.get("ended"):
                fresh_session["starting"] = False
                fresh_session["started"] = False
                fresh_session["impulse_status"] = ""
                fresh_session["active_impulse_task"] = None
                fresh_session["active_impulse_message"] = None
                fresh_session["_last_render_key"] = None
                try:
                    await self._refresh_race_message(guild.id)
                except Exception:
                    pass
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
                render_key = (
                    bool(session.get("started")),
                    bool(session.get("ended")),
                    bool(session.get("final_stretch")),
                    str(session.get("narration") or ""),
                    str(session.get("impulse_status") or ""),
                    tuple(sorted((int(k), round(float(v), 4)) for k, v in (session.get("progress") or {}).items())),
                    tuple(sorted((int(k), str(v)) for k, v in (session.get("state_map") or {}).items())),
                    tuple(sorted((int(k), str(v.get("kind") or ""), int(v.get("ticks_left") or 0), round(float(v.get("per_tick") or 0.0), 4)) for k, v in (session.get("active_impulses") or {}).items())),
                    tuple(sorted((int(k), str(v)) for k, v in (session.get("impulse_flash_levels") or {}).items())),
                    tuple(tuple(int(user_id) for user_id in group) for group in (session.get("arrival_groups") or [])),
                    tuple(str(line) for line in (session.get("result_lines") or [])),
                    tuple(sorted(int(x) for x in (session.get("locked_participants") or set()))),
                )
                if render_key == session.get("_last_render_key"):
                    return
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
                edit_state = await self._safe_edit_message_view(message, view)
                if edit_state == "ok":
                    session["_last_render_key"] = render_key
                elif edit_state == "missing":
                    session["message"] = None
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

    def _pick_race_narration(self, guild: discord.Guild, session: dict, participants: list[discord.Member], tick_events: list[tuple[str, discord.Member]], *, tick: int, final_tick: bool = False) -> str:
        if final_tick:
            return "🏁 Todos cruzaram a linha."
        event_lines: list[str] = []
        for event_key, member in tick_events:
            if event_key == "boost_pequeno":
                event_lines.append(f"{_HORSE_BOOST} {member.mention} recebeu impulso pequeno.")
            elif event_key == "boost_medio":
                event_lines.append(f"{_HORSE_BOOST} {member.mention} recebeu impulso médio.")
            elif event_key == "boost_grande":
                event_lines.append(f"{_HORSE_DASH} {member.mention} disparou com impulso grande.")
            elif event_key == "trip":
                event_lines.append(f"💥 {member.mention} tropeçou.")
            elif event_key == "lead":
                event_lines.append(f"👑 {member.mention} assumiu a liderança.")
            if len(event_lines) >= 3:
                break
        return "\n".join(event_lines[:3])

    def _has_impulse_event(self, tick_events: list[tuple[str, discord.Member]]) -> bool:
        return any(str(event_key).startswith("boost_") for event_key, _member in tick_events)


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

    async def _delete_impulse_message(self, message: discord.Message | None, *, immediate: bool = False):
        if message is None:
            return
        try:
            if not immediate:
                await asyncio.sleep(_RACE_IMPULSE_DELETE_DELAY_SECONDS)
            await message.delete()
        except discord.NotFound:
            pass
        except Exception:
            pass

    async def _stop_active_impulse_event(self, session: dict, *, keep_status: bool = False):
        active_task = session.get("active_impulse_task")
        if active_task is not None:
            session["active_impulse_task"] = None
            if not active_task.done():
                active_task.cancel()
                try:
                    await active_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        active_message = session.get("active_impulse_message")
        if active_message is not None:
            session["active_impulse_message"] = None
            await self._delete_impulse_message(active_message, immediate=True)
        if not keep_status:
            session["impulse_status"] = ""

    async def _run_race_impulse_event(self, guild: discord.Guild, session: dict, stage_name: str):
        if session.get("ended"):
            return []
        channel = guild.get_channel(int(session.get("text_channel_id") or 0))
        if channel is None or not hasattr(channel, "send"):
            return []

        event_view = _RaceImpulseEventView(self, guild, session, stage_name)
        session["impulse_status"] = f"⏸ Evento de impulso ({stage_name.lower()}) em andamento."
        await self._refresh_race_message(guild.id)
        event_message = None
        try:
            event_message = await channel.send(view=event_view)
            session["active_impulse_message"] = event_message
            event_view.message = event_message
            await event_view.refresh_message()
            if event_view.message is None:
                raise discord.NotFound(response=None, message="Impulse event message disappeared")
            if _RACE_IMPULSE_INITIAL_DELAY > 0:
                await asyncio.sleep(_RACE_IMPULSE_INITIAL_DELAY)
            for step_index in range(_RACE_IMPULSE_STAGE_COUNT):
                if session.get("ended") or event_view.message is None:
                    break
                event_view._activate_step(step_index)
                await event_view.refresh_message()
                await asyncio.sleep(_RACE_IMPULSE_STEP_SECONDS)
                event_view._close_current_step()
                if step_index + 1 >= _RACE_IMPULSE_STAGE_COUNT:
                    event_view.finished = True
                await event_view.refresh_message()

            event_view.finished = True
            event_view._close_current_step()
            awards = list(event_view._apply_results() or [])
            await self._refresh_race_message(guild.id)
            await event_view.refresh_message()
            if event_view.last_best_user_id is not None and event_view.last_best_target_bonus > float((session.get("best_impulse") or {}).get("bonus", 0.0) or 0.0):
                session["best_impulse"] = {
                    "user_id": int(event_view.last_best_user_id),
                    "stage": stage_name,
                    "bonus": float(event_view.last_best_target_bonus),
                    "tier": str(event_view.last_best_tier or ""),
                }
            if session.get("active_impulse_message") is event_message:
                session["impulse_status"] = ""
                if not awards and not str(session.get("narration") or "").strip():
                    session["narration"] = ""
                await self._refresh_race_message(guild.id)
            return awards
        except asyncio.CancelledError:
            event_view.finished = True
            if event_view.active_index is not None:
                event_view._close_current_step()
            await event_view.refresh_message()
            raise
        except Exception:
            session["impulse_status"] = ""
            await self._refresh_race_message(guild.id)
        finally:
            if session.get("active_impulse_message") is event_message:
                session["active_impulse_message"] = None
            if session.get("active_impulse_task") is asyncio.current_task():
                session["active_impulse_task"] = None
            if event_message is not None:
                await self._delete_impulse_message(event_message)

    async def _finish_race_lobby(self, guild_id: int, *, reason: str, source_view: discord.ui.LayoutView | None = None, allow_when_starting: bool = False) -> bool:
        session = self._get_race_session(guild_id)
        if session is None or session.get("ended") or session.get("started"):
            return False
        if session.get("starting") and not allow_when_starting:
            return False
        if source_view is not None and session.get("view") is not source_view:
            return False
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            self._race_sessions.pop(guild_id, None)
            return False

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

        session["starting"] = True
        session["started"] = True
        session["narration"] = ""
        session["arrival_groups"] = []
        session["active_impulses"] = {}
        session["impulse_flash_users"] = set()
        session["impulse_flash_levels"] = {}
        session["narration_hold_ticks"] = 0
        session["finish_meta"] = {}
        session["early_rank_snapshot"] = {}
        session["best_impulse"] = None
        session["stale_ticks"] = 0
        session["impulse_status"] = ""
        session["impulse_tasks"] = []
        session["impulse_ticks_fired"] = set()
        session["impulse_schedule"] = self._get_race_impulse_schedule(session)
        session["active_impulse_message"] = None
        session["active_impulse_task"] = None
        session["_visible_before_progress"] = {member.id: float(progress.get(member.id, 0.0)) for member in participants}
        session["_last_render_key"] = None
        view = session.get("view")
        if isinstance(view, (discord.ui.View, discord.ui.LayoutView)):
            try:
                view.stop()
            except Exception:
                pass
        session["view"] = None

        await self._refresh_race_message(guild.id)
        session["starting"] = False
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

            schedule = tuple(session.get("impulse_schedule") or ())
            impulse_awards: list[dict] = []
            if schedule:
                next_event = next(((event_tick, event_stage) for event_tick, event_stage in schedule if event_tick == tick and event_tick not in session.setdefault("impulse_ticks_fired", set())), None)
                if next_event is not None:
                    event_tick, stage_name = next_event
                    await self._stop_active_impulse_event(session, keep_status=True)
                    session.setdefault("impulse_ticks_fired", set()).add(event_tick)
                    impulse_awards = list(await self._run_race_impulse_event(guild, session, stage_name) or [])

            tick_events: list[tuple[str, discord.Member]] = []
            recent_awards = impulse_awards or list(session.get("recent_impulse_awards", []) or [])
            session["recent_impulse_awards"] = []
            for award in recent_awards:
                member = guild.get_member(int(award.get("user_id") or 0))
                tier = str(award.get("tier") or "").lower()
                if member is not None and tier:
                    tick_events.append((f"boost_{tier}", member))
            if not tick_events:
                for user_id, impulse_state in dict(session.get("active_impulses") or {}).items():
                    if int((impulse_state or {}).get("ticks_left") or 0) >= 2:
                        member = guild.get_member(int(user_id))
                        tier = str((impulse_state or {}).get("kind") or "").lower()
                        if member is not None and tier:
                            tick_events.append((f"boost_{tier}", member))
            ordered_before = self._build_finalized_order(guild, session)
            leader_before = ordered_before[0].id if ordered_before else 0
            finishers_this_tick: list[tuple[int, float]] = []

            for member in participants:
                if member.id in arrived_ids:
                    progress[member.id] = track_end
                    state_map[member.id] = _HORSE_FINISH
                    continue

                cur = float(progress.get(member.id, 0.0))
                trip_chance = 0.12 + float(condition.get("trip", 0.0)) + float(special.get("trip", 0.0))
                speed_bonus = float(condition.get("speed", 0.0)) + float(special.get("speed", 0.0))

                if session.get("final_stretch"):
                    trip_chance = max(0.02, trip_chance - 0.03)
                if tick >= _CORRIDA_UPDATES:
                    trip_chance = max(0.01, trip_chance - 0.05)

                active_impulses = session.setdefault("active_impulses", {})
                impulse_state = dict(active_impulses.get(member.id) or {})
                impulse_ticks_left = int(impulse_state.get("ticks_left") or 0)
                impulse_kind = str(impulse_state.get("kind") or "").lower()
                impulse_per_tick = float(impulse_state.get("per_tick") or 0.0) if impulse_ticks_left > 0 else 0.0
                if impulse_ticks_left > 0:
                    trip_chance = max(0.01, trip_chance - 0.06)

                if random.random() < trip_chance and cur < track_end - 0.5:
                    move = max(0.18, impulse_per_tick * 0.8)
                    state_map[member.id] = _HORSE_TRIP
                    tick_events.append(("trip", member))
                else:
                    base_move = random.uniform(1.02, 1.58)
                    if speed_bonus > 0:
                        base_move += min(0.26, speed_bonus * 0.44)
                    elif speed_bonus < 0:
                        base_move += max(-0.26, speed_bonus * 0.38)
                    if session.get("final_stretch"):
                        base_move += 0.22
                    if tick >= _CORRIDA_UPDATES:
                        base_move += 0.30
                    move = max(0.0, min(4.9, base_move + impulse_per_tick))
                    if impulse_ticks_left > 0:
                        state_map[member.id] = _HORSE_DASH if impulse_kind == "grande" else _HORSE_BOOST
                    else:
                        state_map[member.id] = _HORSE_RUN

                raw_finish_score = cur + move
                new_pos = min(track_end, raw_finish_score)
                progress[member.id] = new_pos
                if impulse_ticks_left > 0:
                    remaining_ticks = impulse_ticks_left - 1
                    if remaining_ticks > 0:
                        active_impulses[member.id] = {"kind": impulse_kind, "ticks_left": remaining_ticks, "per_tick": impulse_per_tick}
                    else:
                        active_impulses.pop(member.id, None)
                if new_pos >= track_end - 1e-9:
                    finish_score = raw_finish_score + random.random() * 1e-6
                    finishers_this_tick.append((member.id, finish_score))

            visible_before = session.get("_visible_before_progress") or {}
            visible_before = {member.id: int(float(visible_before.get(member.id, 0.0))) for member in participants}
            visible_after = {member.id: int(float(progress.get(member.id, 0.0))) for member in participants}
            stale_active_ids = [member.id for member in participants if member.id not in arrived_ids]
            if stale_active_ids and not finishers_this_tick and all(visible_before.get(user_id, -1) == visible_after.get(user_id, -2) for user_id in stale_active_ids):
                session["stale_ticks"] = int(session.get("stale_ticks", 0) or 0) + 1
                for member in participants:
                    if member.id in arrived_ids:
                        continue
                    nudged = min(track_end, float(progress.get(member.id, 0.0)) + 0.35)
                    progress[member.id] = nudged
                    if state_map.get(member.id) == _HORSE_START:
                        state_map[member.id] = _HORSE_RUN
                    if nudged >= track_end - 1e-9:
                        finish_score = nudged + random.random() * 1e-6
                        finishers_this_tick.append((member.id, finish_score))
                        state_map[member.id] = _HORSE_FINISH
            else:
                session["stale_ticks"] = 0

            if finishers_this_tick:
                finish_meta = session.setdefault("finish_meta", {})
                already_arrived = {int(user_id) for group in arrival_groups for user_id in group}
                ordered_finishers = [(user_id, score) for user_id, score in sorted(finishers_this_tick, key=lambda item: (-item[1], item[0])) if int(user_id) not in already_arrived]
                primary_finisher = ordered_finishers[:1]
                delayed_finishers = ordered_finishers[1:]
                for user_id, score in primary_finisher:
                    arrival_groups.append([int(user_id)])
                    finish_meta[int(user_id)] = {"tick": tick, "score": float(score)}
                    progress[int(user_id)] = track_end
                    state_map[int(user_id)] = _HORSE_FINISH
                for user_id, _score in delayed_finishers:
                    fallback_gap = random.uniform(0.35, 1.10)
                    progress[int(user_id)] = max(track_end - fallback_gap, track_end - 1.20)
                    impulse_state = dict((session.get("active_impulses") or {}).get(int(user_id)) or {})
                    delayed_kind = str(impulse_state.get("kind") or "").lower()
                    if int(impulse_state.get("ticks_left") or 0) > 0:
                        state_map[int(user_id)] = _HORSE_DASH if delayed_kind == "grande" else _HORSE_BOOST
                    elif state_map.get(int(user_id)) == _HORSE_TRIP:
                        pass
                    else:
                        state_map[int(user_id)] = _HORSE_RUN
                arrived_ids = {int(user_id) for group in arrival_groups for user_id in group}

            leader_progress = max((float(progress.get(member.id, 0.0)) for member in participants), default=0.0)
            session["final_stretch"] = leader_progress >= track_end * 0.72 or tick >= _CORRIDA_UPDATES - 3
            ordered_after = self._build_finalized_order(guild, session)
            leader_after = ordered_after[0].id if ordered_after else 0
            session["_visible_before_progress"] = {member.id: float(progress.get(member.id, 0.0)) for member in participants}
            if tick == 2 and not session.get("early_rank_snapshot"):
                session["early_rank_snapshot"] = {member.id: rank for rank, member in self._ordered_race_members(guild, session)}
            hold_ticks = int(session.get("narration_hold_ticks", 0) or 0)
            impulse_event_this_tick = self._has_impulse_event(tick_events)
            if len(arrived_ids) >= len(participants):
                session["narration"] = self._pick_race_narration(guild, session, ordered_after, tick_events, tick=tick, final_tick=True)
                session["narration_hold_ticks"] = 0
            elif impulse_event_this_tick:
                impulse_text = self._pick_race_narration(guild, session, ordered_after, tick_events, tick=tick)
                if impulse_text.strip():
                    session["narration"] = impulse_text
                    session["narration_hold_ticks"] = 3
                else:
                    session["narration_hold_ticks"] = 0
            elif finishers_this_tick:
                ordered_finishers = [user_id for user_id, _score in sorted(finishers_this_tick, key=lambda item: (-item[1], item[0]))][:1]
                finisher = guild.get_member(int(ordered_finishers[0])) if ordered_finishers else None
                session["narration"] = f"🏁 {finisher.mention} cruzou a linha." if finisher else "🏁 Um corredor cruzou a linha."
                session["narration_hold_ticks"] = 0
            elif hold_ticks > 0 and str(session.get("narration") or "").strip():
                session["narration_hold_ticks"] = hold_ticks - 1
            else:
                if leader_after and leader_after != leader_before:
                    leader = guild.get_member(leader_after)
                    if leader is not None:
                        tick_events.append(("lead", leader))
                session["narration"] = self._pick_race_narration(guild, session, ordered_after, tick_events, tick=tick)
                session["narration_hold_ticks"] = 0
            await self._refresh_race_message(guild.id)
            if int(session.get("narration_hold_ticks", 0) or 0) <= 0 and not session.get("active_impulses"):
                session["impulse_flash_users"] = set()
                session["impulse_flash_levels"] = {}
            tick += 1
            await asyncio.sleep(_CORRIDA_UPDATE_SECONDS)

        await self._stop_active_impulse_event(session)

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
            winner = first_group[0]
            winner_amount = int(rewards.get(winner.id, 0) or 0)
            result_lines.append(f"🏆 {winner.mention} venceu a corrida — {self._chip_text(winner_amount, kind='gain')}")
        finish_meta = session.get("finish_meta") or {}
        if len(final_order) >= 2:
            leader = final_order[0]
            runner_up = final_order[1]
            leader_meta = finish_meta.get(leader.id) or {}
            runner_meta = finish_meta.get(runner_up.id) or {}
            if leader_meta and runner_meta and int(leader_meta.get("tick", -99)) == int(runner_meta.get("tick", -98)):
                diff = abs(float(leader_meta.get("score", 0.0)) - float(runner_meta.get("score", 0.0)))
                if diff <= 0.18:
                    result_lines.append(f"📸 Chegada apertadíssima entre {leader.mention} e {runner_up.mention}!")

        best_impulse = session.get("best_impulse") or {}
        best_impulse_user = guild.get_member(int(best_impulse.get("user_id") or 0)) if best_impulse else None
        if best_impulse_user is not None:
            stage = str(best_impulse.get("stage") or "impulso").lower()
            tier = str(best_impulse.get("tier") or "").lower()
            tier_text = {"pequeno": "impulso pequeno", "medio": "impulso médio", "grande": "impulso grande"}.get(tier, "impulso")
            result_lines.append(f"⚡ Melhor impulso: {best_impulse_user.mention} ({tier_text}, {stage}).")

        early_rank_snapshot = session.get("early_rank_snapshot") or {}
        final_rank_map = _shared_rank_map([[member.id for member in group] for group in final_groups])
        recovery_candidates: list[tuple[int, discord.Member]] = []
        for member in final_order:
            start_rank = int(early_rank_snapshot.get(member.id, 999))
            end_rank = int(final_rank_map.get(member.id, 999))
            gain = start_rank - end_rank
            if gain >= 2:
                recovery_candidates.append((gain, member))
        if recovery_candidates:
            recovery_candidates.sort(key=lambda item: (-item[0], item[1].display_name.casefold()))
            recovery_member = recovery_candidates[0][1]
            result_lines.append(f"🚀 Recuperação da corrida: {recovery_member.mention}.")
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
        session["result_lines"] = result_lines[:3]
        message = session.get("message")
        if message is not None:
            try:
                final_view = _RaceStateView(self, guild, session, finished=True)
                session["view"] = final_view
                edit_state = await self._safe_edit_message_view(message, final_view)
                if edit_state == "missing":
                    session["message"] = None
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

        condition = random.choices(_RACE_CONDITIONS, weights=_RACE_CONDITION_WEIGHTS, k=1)[0]
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
            "active_impulses": {},
            "impulse_flash_users": set(),
            "impulse_flash_levels": {},
            "narration_hold_ticks": 0,
            "finish_meta": {},
            "early_rank_snapshot": {},
            "best_impulse": None,
            "stale_ticks": 0,
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
            "active_impulse_message": None,
            "active_impulse_task": None,
            "_visible_before_progress": {message.author.id: 0.0},
            "_edit_lock": asyncio.Lock(),
            "_last_render_key": None,
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
