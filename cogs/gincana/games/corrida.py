import asyncio
import random
import time

import discord

from config import GUILD_IDS
from ..constants import CHIPS_DEFAULT


CORRIDA_STAKE = 10
_CORRIDA_TRACK_LENGTH = 8
_CORRIDA_UPDATES = 5
_CORRIDA_UPDATE_SECONDS = 2.0

_HORSE_START = "<:horse1:1485794648239636647>"
_HORSE_BOOST = "<:horse2:1485795177401417799>"
_HORSE_RUN = "<:horse2:1485795705745444995>"
_HORSE_TRIP = "<:horse2:1485795938990821547>"
_HORSE_FINISH = "<:Mine:1485797167494070524>"


class _RaceJoinView(discord.ui.View):
    def __init__(self, cog: "GincanaCorridaMixin", guild_id: int, *, timeout: float = 20.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.join_button = discord.ui.Button(style=discord.ButtonStyle.success, label="🐎 Entrar (0)")
        self.join_button.callback = self._join_race
        self.add_item(self.join_button)

    async def _join_race(self, interaction: discord.Interaction):
        await self.cog._handle_race_button(interaction, self)

    async def on_timeout(self):
        try:
            await self.cog._finish_race_lobby(self.guild_id, reason="timeout")
        except Exception:
            pass


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
        voice_channel = self._get_race_voice_channel(guild, session)
        if voice_channel is None:
            return []
        participants: list[discord.Member] = []
        for user_id in sorted(session.get("locked_participants", set())):
            member = guild.get_member(int(user_id))
            if member is None or getattr(member, "bot", False):
                continue
            if getattr(getattr(member, "voice", None), "channel", None) != voice_channel:
                continue
            participants.append(member)
        return participants

    def _race_placement_emoji(self, index: int) -> str:
        return {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, "🔘")

    def _render_race_track(self, pos: int, state_emoji: str) -> str:
        pos = max(0, min(_CORRIDA_TRACK_LENGTH - 1, int(pos)))
        before = "▰" * pos
        after = "▱" * max(0, _CORRIDA_TRACK_LENGTH - pos - 1)
        return f"{before}{state_emoji}{after}"

    def _build_race_lines(self, guild: discord.Guild, session: dict) -> list[str]:
        participants = self._get_race_participants(guild, session)
        if not participants:
            return ["🔘 Ninguém entrou ainda."]

        progress_map = session.get("progress", {}) or {}
        state_map = session.get("state_map", {}) or {}
        ordered = sorted(
            participants,
            key=lambda m: (-int(progress_map.get(m.id, 0)), m.display_name.casefold()),
        )
        lines: list[str] = []
        for index, member in enumerate(ordered, start=1):
            medal = self._race_placement_emoji(index)
            pos = int(progress_map.get(member.id, 0))
            state_emoji = str(state_map.get(member.id) or _HORSE_START)
            lines.append(f"{medal} {member.mention} | {self._render_race_track(pos, state_emoji)}")
        return lines

    def _make_race_embed(self, guild: discord.Guild, session: dict, *, finished: bool = False) -> discord.Embed:
        participants = self._get_race_participants(guild, session)
        pot_total = len(session.get("locked_participants", set())) * CORRIDA_STAKE
        title = "🏁 Corrida encerrada" if finished else "🐎 Corrida aberta"
        if session.get("started"):
            title = "🏁 Corrida encerrada" if finished else "🐎 Corrida em andamento"

        narration = str(session.get("narration") or "📣 A corrida vai começar.")
        lines = self._build_race_lines(guild, session)
        description = "\n".join(lines) + f"\n\n────────\n{narration}"
        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())

        if not session.get("started"):
            embed.add_field(
                name="Entrada",
                value=f"{self._chip_amount(CORRIDA_STAKE)} por jogador",
                inline=True,
            )
            embed.add_field(
                name="Pote atual",
                value=self._chip_amount(pot_total),
                inline=True,
            )
            embed.add_field(
                name="Duração",
                value="**10s** de corrida",
                inline=True,
            )
            embed.set_footer(text="Use o botão para entrar")
        return embed

    async def _handle_race_button(self, interaction: discord.Interaction, view: _RaceJoinView):
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

        voice_channel = self._get_race_voice_channel(guild, session)
        if voice_channel is None:
            try:
                await interaction.response.send_message("A corrida foi encerrada porque a call sumiu.", ephemeral=True)
            except Exception:
                pass
            return

        if getattr(user.voice, "channel", None) != voice_channel:
            try:
                await interaction.response.send_message("Você precisa estar na mesma call da corrida para entrar.", ephemeral=True)
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
        session.setdefault("progress", {})[user.id] = 0
        session.setdefault("state_map", {})[user.id] = _HORSE_START
        view.join_button.label = f"🐎 Entrar ({len(self._get_race_participants(guild, session))})"
        try:
            await interaction.response.send_message(chip_note or f"Você entrou na corrida pagando {self._chip_amount(CORRIDA_STAKE)}.", ephemeral=True)
        except Exception:
            pass
        await self._refresh_race_message(guild.id)

    async def _refresh_race_message(self, guild_id: int):
        session = self._get_race_session(guild_id)
        if session is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        message = session.get("message")
        view = session.get("view")
        if message is None:
            return
        try:
            if not session.get("started") and view is not None:
                view.join_button.label = f"🐎 Entrar ({len(self._get_race_participants(guild, session))})"
            await message.edit(embed=self._make_race_embed(guild, session, finished=bool(session.get("ended"))), view=view)
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
            second_pool = pot_total - first_pool
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

    def _pick_race_narration(self, participants: list[discord.Member], tick_events: list[tuple[str, discord.Member]]) -> str:
        for event_key, member in tick_events:
            if event_key == "boost":
                return f"⚡ {member.mention} largou melhor."
        for event_key, member in tick_events:
            if event_key == "trip":
                return f"💥 {member.mention} tropeçou."
        if participants:
            leader = participants[0]
            return f"👀 {leader.mention} aparece na frente."
        return "📣 A corrida segue aberta."

    async def _finish_race_lobby(self, guild_id: int, *, reason: str) -> bool:
        session = self._get_race_session(guild_id)
        if session is None or session.get("ended"):
            return False
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            self._race_sessions.pop(guild_id, None)
            return False

        session["started"] = True
        session["narration"] = "📣 A corrida começou."
        message = session.get("message")
        view = session.get("view")
        if isinstance(view, discord.ui.View):
            for child in view.children:
                child.disabled = True
            try:
                view.stop()
            except Exception:
                pass

        participants = self._get_race_participants(guild, session)
        locked_ids = set(session.get("locked_participants", set()))
        if len(locked_ids) == 1:
            only_id = next(iter(locked_ids))
            await self.db.add_user_chips(guild.id, only_id, CORRIDA_STAKE)
            session["ended"] = True
            session["narration"] = "Corrida cancelada. A entrada foi devolvida."
            if message is not None:
                try:
                    await message.edit(embed=self._make_embed("🐎 Corrida cancelada", "Só 1 jogador entrou. A entrada foi devolvida.", ok=False), view=view)
                except Exception:
                    pass
            self._race_sessions.pop(guild_id, None)
            return True
        if len(participants) < 2:
            for user_id in locked_ids:
                await self.db.add_user_chips(guild.id, user_id, CORRIDA_STAKE)
            session["ended"] = True
            if message is not None:
                try:
                    await message.edit(embed=self._make_embed("🐎 Corrida cancelada", "Não ficaram participantes suficientes na call. As entradas foram devolvidas.", ok=False), view=view)
                except Exception:
                    pass
            self._race_sessions.pop(guild_id, None)
            return True

        progress = session.setdefault("progress", {})
        state_map = session.setdefault("state_map", {})
        for member in participants:
            progress[member.id] = 0
            state_map[member.id] = _HORSE_START
            await self._record_game_played(guild.id, member.id, weekly_points=4)

        await self._refresh_race_message(guild.id)
        await asyncio.sleep(1.0)

        for tick in range(_CORRIDA_UPDATES):
            if session.get("ended"):
                return False
            participants = self._get_race_participants(guild, session)
            if len(participants) < 2:
                break
            tick_events: list[tuple[str, discord.Member]] = []
            ordered_before = sorted(participants, key=lambda m: (-int(progress.get(m.id, 0)), m.display_name.casefold()))
            leader_before = ordered_before[0].id if ordered_before else 0

            for member in participants:
                cur = int(progress.get(member.id, 0))
                if tick == 0 and random.random() < 0.22:
                    move = 3
                    state_map[member.id] = _HORSE_BOOST
                    tick_events.append(("boost", member))
                elif cur <= 1 and tick >= 2 and random.random() < 0.14:
                    move = 3
                    state_map[member.id] = _HORSE_BOOST
                    tick_events.append(("boost", member))
                elif random.random() < 0.16:
                    move = 0
                    state_map[member.id] = _HORSE_TRIP
                    tick_events.append(("trip", member))
                else:
                    move = random.randint(1, 2)
                    state_map[member.id] = _HORSE_RUN
                progress[member.id] = min(_CORRIDA_TRACK_LENGTH - 1, cur + move)

            ordered_after = sorted(participants, key=lambda m: (-int(progress.get(m.id, 0)), m.display_name.casefold()))
            leader_after = ordered_after[0].id if ordered_after else 0
            if tick == _CORRIDA_UPDATES - 1:
                session["narration"] = "🏁 Últimos metros."
            elif leader_after and leader_after != leader_before:
                leader = guild.get_member(leader_after)
                session["narration"] = f"↗️ {leader.mention} assumiu a ponta." if leader else "↗️ A ponta mudou de dono."
            else:
                session["narration"] = self._pick_race_narration(ordered_after, tick_events)
            await self._refresh_race_message(guild.id)
            await asyncio.sleep(_CORRIDA_UPDATE_SECONDS)

        participants = self._get_race_participants(guild, session)
        final_order = sorted(participants, key=lambda m: (-int(progress.get(m.id, 0)), m.display_name.casefold()))
        if final_order:
            top_progress = int(progress.get(final_order[0].id, 0))
            progress[final_order[0].id] = max(top_progress, _CORRIDA_TRACK_LENGTH - 1)
            state_map[final_order[0].id] = _HORSE_FINISH

        session["ended"] = True
        rewards, placements = self._allocate_race_rewards(final_order, len(locked_ids) * CORRIDA_STAKE)
        result_lines = self._build_race_lines(guild, session)
        result_lines.append("")
        if final_order:
            result_lines.append(f"🏆 {final_order[0].mention} venceu a corrida.")
        for badge, members, amount in placements:
            if members and amount > 0:
                result_lines.append(f"{badge} {members[0].mention} — {self._chip_text(amount, kind='gain')}")
        session["narration"] = "\n".join(result_lines[-(1 + len(placements)):]) if result_lines else "🏁 Corrida encerrada."

        for index, member in enumerate(final_order[:3], start=1):
            await self.db.add_user_game_stat(guild.id, member.id, "corrida_podiums", 1)
            await self._grant_weekly_points(guild.id, member.id, max(3, 5 - index))
        if final_order:
            await self.db.add_user_game_stat(guild.id, final_order[0].id, "corrida_wins", 1)
        for user_id, amount in rewards.items():
            if amount > 0:
                await self.db.add_user_chips(guild.id, user_id, amount)
                await self._grant_weekly_points(guild.id, user_id, max(4, amount // 4))

        if message is not None:
            try:
                final_embed = discord.Embed(
                    title="🏁 Corrida encerrada",
                    description="\n".join(self._build_race_lines(guild, session) + ["", *([f"🏆 {final_order[0].mention} venceu a corrida."] if final_order else []), *[f"{badge} {members[0].mention} — {self._chip_text(amount, kind='gain')}" for badge, members, amount in placements if members and amount > 0]]),
                    color=discord.Color.green(),
                )
                await message.edit(embed=final_embed, view=view)
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
        if GUILD_IDS and guild.id not in GUILD_IDS:
            return True
        if not self.db.gincana_enabled(guild.id):
            return True
        if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True
        if self._get_race_session(guild.id) is not None:
            return True

        voice_channel = getattr(getattr(message.author, "voice", None), "channel", None)
        if not isinstance(voice_channel, discord.VoiceChannel):
            return True

        paid, _balance, chip_note = await self._try_consume_chips(guild.id, message.author.id, CORRIDA_STAKE)
        if not paid:
            try:
                await message.channel.send(embed=self._make_embed("🐎 Saldo insuficiente", chip_note or "Você não tem saldo suficiente.", ok=False))
            except Exception:
                pass
            return True

        view = _RaceJoinView(self, guild.id, timeout=20.0)
        session = {
            "voice_channel_id": voice_channel.id,
            "text_channel_id": message.channel.id,
            "owner_id": message.author.id,
            "locked_participants": {message.author.id},
            "progress": {message.author.id: 0},
            "state_map": {message.author.id: _HORSE_START},
            "message": None,
            "view": view,
            "ended": False,
            "started": False,
            "narration": "📣 A corrida vai começar.",
        }
        self._race_sessions[guild.id] = session
        view.join_button.label = f"🐎 Entrar ({len(self._get_race_participants(guild, session))})"
        embed = self._make_race_embed(guild, session)
        if chip_note:
            embed.set_footer(text=f"{chip_note} Você já entrou na corrida.")
        try:
            panel_message = await message.channel.send(embed=embed, view=view)
        except Exception:
            self._race_sessions.pop(guild.id, None)
            await self.db.add_user_chips(guild.id, message.author.id, CORRIDA_STAKE)
            return True

        session["message"] = panel_message
        await self._react_with_emoji(message, "🐎", keep=True)
        return True
