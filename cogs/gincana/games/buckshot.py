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

class _BuckshotJoinView(discord.ui.View):
    def __init__(self, cog: "GincanaTriggerMixin", guild_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.join_button = discord.ui.Button(style=discord.ButtonStyle.success, label="Entrar na rodada (0)")
        self.join_button.callback = self._toggle_join
        self.add_item(self.join_button)

    async def _toggle_join(self, interaction: discord.Interaction):
        await self.cog._handle_buckshot_button(interaction, self)

    async def on_timeout(self):
        try:
            await self.cog._finish_buckshot(self.guild_id, reason="timeout")
        except Exception:
            pass


class GincanaBuckshotMixin:
        def _get_buckshot_session(self, guild_id: int) -> dict | None:
            session = self._buckshot_sessions.get(guild_id)
            if not session or session.get("ended"):
                return None
            return session
        def _get_buckshot_voice_channel(self, guild: discord.Guild, session: dict) -> discord.VoiceChannel | None:
            channel = guild.get_channel(int(session.get("voice_channel_id", 0) or 0))
            return channel if isinstance(channel, discord.VoiceChannel) else None
        def _get_buckshot_focus_participants(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> list[discord.Member]:
            participants = []
            seen: set[int] = set()
            for member in self._iter_focused_members(guild, voice_channel):
                if member.bot or member.id in seen:
                    continue
                seen.add(member.id)
                participants.append(member)
            return participants
        def _get_buckshot_manual_participants(self, guild: discord.Guild, voice_channel: discord.VoiceChannel, session: dict) -> list[discord.Member]:
            participants = []
            seen: set[int] = set()
            stored_ids = set(session.get("manual_participants", set()) or set())
            for user_id in stored_ids:
                member = guild.get_member(int(user_id))
                if member is None or member.bot or member.id in seen:
                    continue
                seen.add(member.id)
                participants.append(member)
            return participants
        def _get_buckshot_participant_ids(self, guild: discord.Guild, session: dict) -> list[int]:
            participant_ids: set[int] = set()
            voice_channel = self._get_buckshot_voice_channel(guild, session)
            focus_ids = set(session.get("focus_participants", set()) or set())
            if voice_channel is not None:
                current_voice_ids = {member.id for member in getattr(voice_channel, "members", []) if not getattr(member, "bot", False)}
                participant_ids.update(current_voice_ids & focus_ids)
            participant_ids.update(int(user_id) for user_id in (session.get("manual_participants", set()) or set()))
            participant_ids.update(int(user_id) for user_id in (session.get("locked_participants", set()) or set()))
            return sorted(participant_ids)
        def _get_buckshot_participants(self, guild: discord.Guild, session: dict) -> list[discord.Member]:
            participants: list[discord.Member] = []
            seen: set[int] = set()
            for user_id in self._get_buckshot_participant_ids(guild, session):
                member = guild.get_member(int(user_id))
                if member is None or member.bot or member.id in seen:
                    continue
                seen.add(member.id)
                participants.append(member)
            return participants
        def _make_buckshot_embed(self, guild: discord.Guild, session: dict, *, final_text: str | None = None) -> discord.Embed:
            participants = self._get_buckshot_participants(guild, session)
            payout_total = len(participants) * BUCKSHOT_STAKE
            title = "<:gunforward:1484655577836683434> Roleta russa"
            if final_text:
                description = final_text
                color = discord.Color.red()
            else:
                description = (
                    f"Entrada: {self._chip_amount(BUCKSHOT_STAKE)} por jogador\n"
                    f"Participantes: **{len(participants)}**\n"
                    f"{self._CHIP_GAIN_EMOJI} Pote atual: {self._chip_amount(payout_total)}\n\n"
                    "Entre na rodada e veja quem sai da call quando o disparo vier."
                )
                color = discord.Color.blurple()
            embed = discord.Embed(title=title, description=description, color=color)
            return embed
        async def _refresh_buckshot_message(self, guild_id: int):
            session = self._get_buckshot_session(guild_id)
            if session is None:
                return

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            message = session.get("message")
            view = session.get("view")
            if message is None or view is None:
                return

            participants = self._get_buckshot_participants(guild, session)
            view.join_button.label = f"Entrar na rodada ({len(participants)})"
            view.join_button.style = discord.ButtonStyle.success
            try:
                await message.edit(embed=self._make_buckshot_embed(guild, session), view=view)
            except Exception:
                pass
        async def _handle_buckshot_button(self, interaction: discord.Interaction, view: _BuckshotJoinView):
            guild = interaction.guild
            if guild is None:
                try:
                    await interaction.response.send_message("Use esse botão dentro de um servidor.", ephemeral=True)
                except Exception:
                    pass
                return

            session = self._get_buckshot_session(guild.id)
            if session is None:
                try:
                    await interaction.response.send_message("Essa rodada já terminou.", ephemeral=True)
                except Exception:
                    pass
                return

            voice_channel = self._get_buckshot_voice_channel(guild, session)
            if voice_channel is None:
                try:
                    await interaction.response.send_message("A rodada perdeu o canal de voz de referência.", ephemeral=True)
                except Exception:
                    pass
                return

            member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
            if not isinstance(member, discord.Member) or member.bot:
                try:
                    await interaction.response.send_message("Bots não podem participar dessa rodada.", ephemeral=True)
                except Exception:
                    pass
                return

            if getattr(member.voice, "channel", None) != voice_channel:
                try:
                    await interaction.response.send_message("Você precisa estar na mesma call da rodada para participar.", ephemeral=True)
                except Exception:
                    pass
                return

            locked_participants = set(session.get("locked_participants", set()) or set())
            if member.id in locked_participants:
                try:
                    await interaction.response.send_message("Você já entrou nessa rodada e sua vaga está travada.", ephemeral=True)
                except Exception:
                    pass
                return

            paid, _balance, note = await self._try_consume_chips(guild.id, member.id, BUCKSHOT_STAKE)
            if not paid:
                try:
                    await interaction.response.send_message(note or "Você não tem saldo suficiente para entrar.", ephemeral=True)
                except Exception:
                    pass
                return

            manual_participants = set(session.get("manual_participants", set()) or set())
            manual_participants.add(member.id)
            session["manual_participants"] = manual_participants
            locked_participants.add(member.id)
            session["locked_participants"] = locked_participants

            await self._refresh_buckshot_message(guild.id)

            note = f"Você entrou na rodada e pagou **{BUCKSHOT_STAKE} {self._CHIP_LOSS_EMOJI}**." if not note else f"{note} Você entrou na rodada e pagou **{BUCKSHOT_STAKE} {self._CHIP_LOSS_EMOJI}**."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(note, ephemeral=True)
                else:
                    await interaction.response.send_message(note, ephemeral=True)
            except Exception:
                try:
                    await interaction.response.defer()
                except Exception:
                    pass
        async def _finish_buckshot(self, guild_id: int, *, reason: str) -> bool:
            session = self._buckshot_sessions.get(guild_id)
            if not session or session.get("ended"):
                return False

            session["ended"] = True
            timeout_task = session.get("timeout_task")
            if timeout_task is not None and timeout_task is not asyncio.current_task() and not timeout_task.done():
                timeout_task.cancel()

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                self._buckshot_sessions.pop(guild_id, None)
                return False

            message = session.get("message")
            view = session.get("view")
            if isinstance(view, discord.ui.View):
                for child in view.children:
                    child.disabled = True
                    if isinstance(child, discord.ui.Button):
                        child.style = discord.ButtonStyle.danger
                try:
                    view.stop()
                except Exception:
                    pass

            participants = self._get_buckshot_participants(guild, session)
            locked_participants = set(session.get("locked_participants", set()) or set())
            eligible = [member for member in participants if member.id in locked_participants]
            chosen = random.choice(eligible) if eligible else None
            if chosen is not None and chosen.voice and chosen.voice.channel:
                chosen_channel = chosen.voice.channel
                try:
                    await self._play_buckshot_sfx(guild, chosen_channel)
                except Exception:
                    pass
                try:
                    await asyncio.sleep(0.20)
                except Exception:
                    pass
                try:
                    await chosen.move_to(None, reason="gincana buckshot")
                except Exception:
                    pass

            winners = [member for member in eligible if chosen is not None and member.id != chosen.id]
            payout_total = max(0, BUCKSHOT_STAKE * len(eligible))
            payout_each = 0
            payout_remainder = 0
            if chosen is None:
                final_text = "O disparo aconteceu... mas ninguém com entrada paga ficou elegível na rodada."
            else:
                if winners:
                    payout_each = payout_total // len(winners)
                    payout_remainder = payout_total % len(winners)
                    for index, winner in enumerate(winners):
                        bonus = payout_each + (1 if index < payout_remainder else 0)
                        if bonus > 0:
                            await self.db.add_user_chips(guild.id, winner.id, bonus)
                            await self.db.add_user_game_stat(guild.id, winner.id, "buckshot_survivals", 1)
                            await self._record_game_played(guild.id, winner.id, weekly_points=8)
                            await self._grant_weekly_points(guild.id, winner.id, max(3, bonus // 3))
                await self.db.add_user_game_stat(guild.id, chosen.id, "buckshot_eliminations", 1)
                await self._record_game_played(guild.id, chosen.id, weekly_points=3)
                chosen_text = chosen.mention if chosen is not None else "Alguém"
                if winners:
                    final_text = (
                        f"<:gunforward:1484655577836683434>💥 O disparo aconteceu, {chosen_text} foi tirado da call.\n"
                        f"{self._CHIP_GAIN_EMOJI} O pote de **{payout_total} {self._CHIP_EMOJI}** foi dividido entre os sobreviventes."
                    )
                else:
                    final_text = (
                        f"<:gunforward:1484655577836683434>💥 O disparo aconteceu, {chosen_text} foi tirado da call.\n"
                        f"{self._CHIP_LOSS_EMOJI} O pote de **{payout_total} {self._CHIP_EMOJI}** foi perdido."
                    )

            embed = self._make_buckshot_embed(guild, session, final_text=final_text)
            delivered = False
            if message is not None:
                try:
                    await message.edit(embed=embed, view=view)
                    delivered = True
                except Exception:
                    pass
            if not delivered and message is not None:
                try:
                    await message.channel.send(embed=embed)
                except Exception:
                    pass

            self._buckshot_sessions.pop(guild_id, None)
            return True
        async def _handle_buckshot_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False

            content = (message.content or "")
            if not self._matches_exact_trigger(content, "buckshot"):
                return False

            if GUILD_IDS and guild.id not in GUILD_IDS:
                return True

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            if self._get_buckshot_session(guild.id) is not None:
                return True

            author_voice = getattr(message.author, "voice", None)
            voice_channel = getattr(author_voice, "channel", None)
            if not isinstance(voice_channel, discord.VoiceChannel):
                return True

            view = _BuckshotJoinView(self, guild.id, timeout=30.0)
            focus_participants: set[int] = set()
            locked_participants: set[int] = set()
            for member in self._iter_focused_members(guild, voice_channel):
                if getattr(member, "bot", False):
                    continue
                paid, _balance, _note = await self._try_consume_chips(guild.id, member.id, BUCKSHOT_STAKE)
                if paid:
                    focus_participants.add(member.id)
                    locked_participants.add(member.id)

            session = {
                "voice_channel_id": voice_channel.id,
                "text_channel_id": message.channel.id,
                "manual_participants": set(),
                "focus_participants": focus_participants,
                "locked_participants": locked_participants,
                "message": None,
                "view": view,
                "ended": False,
                "timeout_task": None,
            }
            self._buckshot_sessions[guild.id] = session

            view.join_button.label = f"Entrar na rodada ({len(self._get_buckshot_participants(guild, session))})"
            view.join_button.style = discord.ButtonStyle.success
            embed = self._make_buckshot_embed(guild, session)
            try:
                panel_message = await message.channel.send(embed=embed, view=view)
            except Exception:
                self._buckshot_sessions.pop(guild.id, None)
                return True

            session["message"] = panel_message
            session["timeout_task"] = self.bot.loop.create_task(view.wait())
            await self._react_with_emoji(message, "<a:r_gun01:1484661880323838002>", keep=True)
            return True
        async def _handle_atirar_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False

            content = (message.content or "")
            if not self._matches_exact_trigger(content, "atirar"):
                return False

            if GUILD_IDS and guild.id not in GUILD_IDS:
                return True

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            session = self._get_buckshot_session(guild.id)
            if session is None:
                return True

            voice_channel = self._get_buckshot_voice_channel(guild, session)
            if voice_channel is None:
                await self._finish_buckshot(guild.id, reason="manual")
                return True

            if getattr(message.author.voice, "channel", None) != voice_channel:
                return True

            await self._finish_buckshot(guild.id, reason="manual")
            await self._react_with_emoji(message, "💥", keep=True)
            return True
