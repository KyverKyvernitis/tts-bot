import asyncio
import random
import time
from pathlib import Path

import discord

from config import MUTE_TOGGLE_WORD, OFF_COLOR, TRIGGER_WORD

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

            needs_negative_confirm = self._needs_negative_confirmation(guild.id, member.id, BUCKSHOT_STAKE)
            if needs_negative_confirm:
                confirmed = await self._confirm_negative_ephemeral(interaction, guild.id, member.id, BUCKSHOT_STAKE, title="💥 Confirmar entrada")
                if not confirmed:
                    return

            entry_text = self._entry_consume_text(guild.id, member.id, BUCKSHOT_STAKE)
            paid, _balance, note = await self._try_consume_chips(guild.id, member.id, BUCKSHOT_STAKE, reason="Entrada no buckshot")
            if needs_negative_confirm:
                note = None
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

            reply_text = note or entry_text
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(reply_text, ephemeral=True)
                else:
                    await interaction.response.send_message(reply_text, ephemeral=True)
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
                    await chosen.move_to(None, reason="economia buckshot")
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
                            await self._change_user_bonus_chips(guild.id, winner.id, bonus, reason="Vitória no buckshot")
                            await self.db.add_user_game_stat(guild.id, winner.id, "buckshot_survivals", 1)
                            await self._record_game_played(guild.id, winner.id, weekly_points=8)
                            await self._grant_weekly_points(guild.id, winner.id, max(3, bonus // 3))
                await self.db.add_user_game_stat(guild.id, chosen.id, "buckshot_eliminations", 1)
                await self._record_game_played(guild.id, chosen.id, weekly_points=3)
                chosen_text = chosen.mention if chosen is not None else "Alguém"
                if winners:
                    final_text = (
                        f"<:gunforward:1484655577836683434>💥 O disparo aconteceu. {chosen_text} foi eliminado.\n"
                        f"Cada sobrevivente recebeu **{payout_each} {self._CHIP_BONUS_EMOJI}** de bônus."
                    )
                else:
                    final_text = (
                        f"<:gunforward:1484655577836683434>💥 O disparo aconteceu, {chosen_text} foi eliminado.\n"
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

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            if self._get_buckshot_session(guild.id) is not None:
                return True

            voice_channel = getattr(getattr(message.author, "voice", None), "channel", None)

            view = _BuckshotJoinView(self, guild.id, timeout=30.0)
            session = {
                "voice_channel_id": getattr(voice_channel, "id", 0),
                "text_channel_id": message.channel.id,
                "manual_participants": {message.author.id},
                "focus_participants": set(),
                "locked_participants": {message.author.id},
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

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            session = self._get_buckshot_session(guild.id)
            if session is None:
                return True

            await self._finish_buckshot(guild.id, reason="manual")
            await self._react_with_emoji(message, "💥", keep=True)
            return True


# --- V2 lobby overrides ---
class _BuckshotJoinView(discord.ui.LayoutView):
    def __init__(self, cog: "GincanaBuckshotMixin", guild_id: int, session: dict, guild: discord.Guild, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.session = session
        self.guild = guild
        self.join_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label='Entrar (0)', emoji=self.cog._buckshot_join_emoji(self.session))
        self.join_button.callback = self._toggle_join
        self.start_button = discord.ui.Button(style=discord.ButtonStyle.secondary, label='Atirar', emoji=self.cog._buckshot_start_emoji(self.session))
        self.start_button.callback = self._start_round
        self._build_layout()

    def _build_layout(self):
        self.clear_items()
        participants = self.cog._get_buckshot_participants(self.guild, self.session)
        stake = self.cog._buckshot_stake(self.session)
        payout_total = len(participants) * stake
        countdown = int(self.session.get('start_countdown') or 0)
        self.join_button.label = f"Entrar ({len(participants)})"
        if countdown > 0:
            self.start_button.label = f"Atirar ({countdown})"
            self.start_button.disabled = True
        else:
            self.start_button.label = 'Atirar'
            self.start_button.disabled = False
        lines1 = [f"# {self.cog._buckshot_title(self.session)}", f"**Entrada:** {self.cog._chip_amount(stake)}", f"**Pote atual:** {self.cog._chip_amount(payout_total)}", "**Lobby:** **30s**"]
        plist = [f"### Participantes ({len(participants)})"]
        if participants:
            plist.extend(f"• {m.mention}" for m in participants)
        else:
            plist.append('• Ninguém entrou ainda.')
        foot = ['Entre pelo botão abaixo.', 'O criador da rodada ou a staff pode começar quando houver pelo menos 2 participantes.']
        if countdown > 0:
            foot.append('A contagem começou e ainda dá tempo de entrar.')
        row = discord.ui.ActionRow(self.join_button, self.start_button)
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines1)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(plist)),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(foot)),
            row,
            accent_color=self.cog._buckshot_color(self.session),
        ))

    async def _toggle_join(self, interaction: discord.Interaction):
        await self.cog._handle_buckshot_button(interaction, self)

    async def _start_round(self, interaction: discord.Interaction):
        await self.cog._handle_buckshot_start_button(interaction, self)

    async def on_timeout(self):
        try:
            session = self.cog._get_buckshot_session(self.guild_id)
            if session is None or session.get('starting'):
                return
            await self.cog._finish_buckshot(self.guild_id, reason='timeout')
        except Exception:
            pass

class _BuckshotLobbyClosedView(discord.ui.LayoutView):
    def __init__(self, title: str, lines: list[str], *, color: discord.Color | None = None):
        super().__init__(timeout=None)
        body = [f"# {title}", *[line for line in lines if line]]
        self.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(body)), accent_color=color or discord.Color.blurple()))

class GincanaBuckshotMixin(GincanaBuckshotMixin):
    def _get_buckshot_session(self, guild_id: int) -> dict | None:
        session = self._buckshot_sessions.get(guild_id)
        if not session:
            return None
        if session.get('ended'):
            self._buckshot_sessions.pop(guild_id, None)
            return None
        if self._runtime_state_is_stale(session, max_idle=120.0, max_age=300.0):
            if not session.get('_stale_cleanup_started'):
                session['_stale_cleanup_started'] = True
                session['ended'] = True
                self.bot.loop.create_task(self._cleanup_stale_buckshot_session(guild_id, session))
            return None
        self._touch_runtime_state(session, kind='buckshot', guild_id=guild_id)
        return session

    async def _cleanup_stale_buckshot_session(self, guild_id: int, session: dict):
        async with self._runtime_lock(session):
            if self._buckshot_sessions.get(guild_id) is not session:
                return
            await self._safe_cancel_task(session.get('countdown_task'))
            guild = self.bot.get_guild(guild_id)
            locked_participants = {int(user_id) for user_id in (session.get('locked_participants', set()) or set())}
            if guild is not None:
                stake = self._buckshot_stake(session)
                for user_id in sorted(locked_participants):
                    try:
                        await self._buckshot_refund_entry(guild.id, session, int(user_id), stake)
                    except Exception:
                        pass
            lobby_message = session.get('lobby_message') or session.get('message')
            if lobby_message is not None:
                try:
                    await lobby_message.edit(view=_BuckshotLobbyClosedView('✨ Rodada dourada cancelada' if self._buckshot_is_golden(session) else '<a:r_gun01:1484661880323838002> Rodada cancelada', [
                        'A rodada foi encerrada automaticamente porque ficou travada por tempo demais.',
                        'As entradas foram devolvidas.',
                    ], color=self._buckshot_color(session)))
                except Exception:
                    pass
            if self._buckshot_sessions.get(guild_id) is session:
                self._buckshot_sessions.pop(guild_id, None)

    def _buckshot_render_key(self, session: dict, guild: discord.Guild) -> tuple:
        return (
            bool(session.get('ended')),
            bool(session.get('starting')),
            int(session.get('start_countdown', 0) or 0),
            tuple(sorted(int(x) for x in (session.get('locked_participants', set()) or set()))),
            tuple(int(member.id) for member in self._get_buckshot_participants(guild, session)),
            str(session.get('variant') or 'normal'),
        )

    def _buckshot_is_golden(self, session: dict | None) -> bool:
        return str((session or {}).get('variant') or 'normal').lower() == 'golden'

    def _buckshot_stake(self, session: dict | None) -> int:
        return 30 if self._buckshot_is_golden(session) else BUCKSHOT_STAKE

    def _buckshot_title(self, session: dict | None) -> str:
        return '<a:uzi:1487936659692458054> Roleta dourada' if self._buckshot_is_golden(session) else '<:gunforward:1484655577836683434> Roleta russa'

    def _buckshot_color(self, session: dict | None) -> discord.Color:
        return discord.Color.gold() if self._buckshot_is_golden(session) else discord.Color.blurple()

    def _buckshot_join_emoji(self, session: dict | None):
        return discord.PartialEmoji.from_str('<:gold:1487936417790431433>') if self._buckshot_is_golden(session) else discord.PartialEmoji.from_str('<:propergun:1485855162198396959>')

    def _buckshot_start_emoji(self, session: dict | None):
        return discord.PartialEmoji.from_str('<:star:1487936913431072780>') if self._buckshot_is_golden(session) else '💥'

    def _buckshot_reaction_emoji(self, session: dict | None) -> str:
        return '<a:uzi:1487936659692458054>' if self._buckshot_is_golden(session) else '<a:r_gun01:1484661880323838002>'

    def _buckshot_entry_spend(self, session: dict, user_id: int, stake: int) -> dict[str, int]:
        spend_map = session.get('entry_spend') or {}
        raw = spend_map.get(int(user_id)) or {}
        bonus_spent = max(0, int(raw.get('bonus', 0) or 0))
        normal_spent = max(0, int(raw.get('chips', 0) or 0))
        total = bonus_spent + normal_spent
        if total <= 0 and stake > 0:
            return {'chips': int(stake), 'bonus': 0}
        if total > stake > 0:
            overflow = total - int(stake)
            reduce_bonus = min(bonus_spent, overflow)
            bonus_spent -= reduce_bonus
            overflow -= reduce_bonus
            if overflow > 0:
                normal_spent = max(0, normal_spent - overflow)
        return {'chips': normal_spent, 'bonus': bonus_spent}

    def _buckshot_record_entry_spend(self, session: dict, user_id: int, stake: int, *, bonus_before: int):
        spend_map = dict(session.get('entry_spend') or {})
        bonus_used = min(max(0, int(bonus_before)), max(0, int(stake)))
        spend_map[int(user_id)] = {
            'bonus': bonus_used,
            'chips': max(0, int(stake) - bonus_used),
        }
        session['entry_spend'] = spend_map

    async def _buckshot_refund_entry(self, guild_id: int, session: dict, user_id: int, stake: int):
        spend = self._buckshot_entry_spend(session, user_id, stake)
        if spend.get('bonus', 0) > 0:
            await self._change_user_bonus_chips(guild_id, user_id, int(spend['bonus']), reason='Devolução do buckshot')
        if spend.get('chips', 0) > 0:
            await self._change_user_chips(guild_id, user_id, int(spend['chips']), reason='Devolução do buckshot')

    async def _refresh_buckshot_message(self, guild_id: int):
        session = self._get_buckshot_session(guild_id)
        if session is None:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        message = session.get('lobby_message') or session.get('message')
        view = session.get('view')
        if message is None or view is None:
            return
        if hasattr(view, '_build_layout'):
            view._build_layout()
        render_key = self._buckshot_render_key(session, guild)
        await self._safe_view_edit(message, view, state=session, render_key=render_key)

    async def _handle_buckshot_button(self, interaction: discord.Interaction, view: _BuckshotJoinView):
        guild = interaction.guild
        if guild is None:
            try: await interaction.response.send_message('Use esse botão dentro de um servidor.', ephemeral=True)
            except Exception: pass
            return
        session = self._get_buckshot_session(guild.id)
        if session is None or session.get('view') is not view:
            try: await interaction.response.send_message('Essa rodada já terminou.', ephemeral=True)
            except Exception: pass
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
        if not isinstance(member, discord.Member) or member.bot:
            try: await interaction.response.send_message('Bots não podem participar dessa rodada.', ephemeral=True)
            except Exception: pass
            return
        async with self._runtime_lock(session):
            self._touch_runtime_state(session, kind='buckshot', guild_id=guild.id)
            if session.get('ended') or session.get('view') is not view:
                try: await interaction.response.send_message('Essa rodada já terminou.', ephemeral=True)
                except Exception: pass
                return
            locked = set(session.get('locked_participants', set()) or set())
            if member.id in locked:
                try: await interaction.response.send_message('Você já entrou nessa rodada e sua vaga está travada.', ephemeral=True)
                except Exception: pass
                return
            stake = self._buckshot_stake(session)
            needs_negative_confirm = self._needs_negative_confirmation(guild.id, member.id, stake)
            if needs_negative_confirm:
                confirmed = await self._confirm_negative_ephemeral(interaction, guild.id, member.id, stake, title="💥 Confirmar entrada")
                if not confirmed:
                    return
            entry_text = self._entry_consume_text(guild.id, member.id, stake)
            bonus_before = self._get_user_bonus_chips(guild.id, member.id)
            paid, _balance, note = await self._try_consume_chips(guild.id, member.id, stake, reason="Entrada no buckshot")
            if needs_negative_confirm:
                note = None
            if not paid:
                try:
                    if interaction.response.is_done():
                        await interaction.followup.send(note or 'Você não tem saldo suficiente para entrar.', ephemeral=True)
                    else:
                        await interaction.response.send_message(note or 'Você não tem saldo suficiente para entrar.', ephemeral=True)
                except Exception:
                    pass
                return
            manual = set(session.get('manual_participants', set()) or set())
            manual.add(member.id)
            session['manual_participants'] = manual
            self._buckshot_record_entry_spend(session, member.id, stake, bonus_before=bonus_before)
            locked.add(member.id)
            session['locked_participants'] = locked
            self._touch_runtime_state(session, kind='buckshot', guild_id=guild.id)
        await self._refresh_buckshot_message(guild.id)
        try: await interaction.response.send_message(note or entry_text, ephemeral=True)
        except Exception: pass

    async def _handle_buckshot_start_button(self, interaction: discord.Interaction, view: _BuckshotJoinView):
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            try: await interaction.response.send_message('Servidor inválido.', ephemeral=True)
            except Exception: pass
            return
        session = self._get_buckshot_session(guild.id)
        if session is None or session.get('view') is not view or session.get('ended'):
            try: await interaction.response.send_message('Essa rodada já terminou.', ephemeral=True)
            except Exception: pass
            return
        async with self._runtime_lock(session):
            self._touch_runtime_state(session, kind='buckshot', guild_id=guild.id)
            if session.get('starting'):
                try: await interaction.response.send_message('A contagem já começou.', ephemeral=True)
                except Exception: pass
                return
            is_owner = int(session.get('owner_id') or 0) == user.id
            if not is_owner and not self._is_staff_member(user):
                try: await interaction.response.send_message('Só o criador da rodada ou a staff pode começar.', ephemeral=True)
                except Exception: pass
                return
            participants = self._get_buckshot_participants(guild, session)
            if len(participants) < 2:
                try: await interaction.response.send_message('A rodada precisa de pelo menos 2 participantes.', ephemeral=True)
                except Exception: pass
                return
            session['starting'] = True
            session['start_countdown'] = 3
            await self._safe_cancel_task(session.get('countdown_task'))
            session['countdown_task'] = self.bot.loop.create_task(self._run_buckshot_start_countdown(guild.id, view))
            self._touch_runtime_state(session, kind='buckshot', guild_id=guild.id)
        try: await interaction.response.send_message('Contagem iniciada.', ephemeral=True)
        except Exception: pass
        await self._refresh_buckshot_message(guild.id)

    async def _run_buckshot_start_countdown(self, guild_id: int, view: _BuckshotJoinView):
        for remaining in range(3,0,-1):
            session = self._get_buckshot_session(guild_id)
            if session is None or session.get('ended') or session.get('view') is not view:
                return
            session['start_countdown'] = remaining
            self._touch_runtime_state(session, kind='buckshot', guild_id=guild_id)
            await self._refresh_buckshot_message(guild_id)
            await asyncio.sleep(1)
        session = self._get_buckshot_session(guild_id)
        if session is None or session.get('ended') or session.get('view') is not view:
            return
        session['start_countdown'] = 0
        await self._finish_buckshot(guild_id, reason='manual')

    async def _finish_buckshot(self, guild_id: int, *, reason: str) -> bool:
        session = self._buckshot_sessions.get(guild_id)
        if not session or session.get('ended'):
            return False
        async with self._runtime_lock(session):
            if session.get('ended'):
                return False
            session['ended'] = True
            self._touch_runtime_state(session, kind='buckshot', guild_id=guild_id)
            self._buckshot_last_used[guild_id] = time.time()
            await self._safe_cancel_task(session.get('countdown_task'))
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                self._buckshot_sessions.pop(guild_id, None)
                return False
            lobby_message = session.get('lobby_message') or session.get('message')
            view = session.get('view')
            if isinstance(view, (discord.ui.View, discord.ui.LayoutView)):
                try: view.stop()
                except Exception: pass
            participants = self._get_buckshot_participants(guild, session)
            locked_participants = set(session.get('locked_participants', set()) or set())
            eligible = [member for member in participants if member.id in locked_participants]
            if len(eligible) < 2:
                for uid in locked_participants:
                    await self._buckshot_refund_entry(guild.id, session, int(uid), self._buckshot_stake(session))
                if lobby_message is not None:
                    try:
                        await lobby_message.edit(view=_BuckshotLobbyClosedView('✨ Rodada dourada cancelada' if self._buckshot_is_golden(session) else '<a:r_gun01:1484661880323838002> Rodada cancelada', [
                            'Não ficaram participantes suficientes.',
                            'As entradas foram devolvidas.',
                        ], color=self._buckshot_color(session)))
                    except Exception:
                        pass
                self._buckshot_sessions.pop(guild_id, None)
                return True

            chosen = random.choice(eligible) if eligible else None
            if chosen is not None and chosen.voice and chosen.voice.channel:
                try:
                    await self._play_buckshot_sfx(guild, chosen.voice.channel)
                except Exception:
                    pass
                try:
                    await asyncio.sleep(0.20)
                except Exception:
                    pass
                try:
                    await chosen.move_to(None, reason='gincana buckshot')
                except Exception:
                    pass

            winners = [member for member in eligible if chosen is not None and member.id != chosen.id]
            player_count = len(eligible)
            stake = self._buckshot_stake(session)
            if self._buckshot_is_golden(session):
                for participant in eligible:
                    await self.db.add_user_game_stat(guild.id, participant.id, 'buckshot_golden_games', 1)
            is_golden = self._buckshot_is_golden(session)
            eliminated_entry_total = stake if chosen is not None else 0
            bonus_total = 0
            if player_count >= 5:
                bonus_total = 10
            elif player_count >= 3:
                bonus_total = 5
            golden_bonus_each = 15 if is_golden else 0
            lines: list[str] = []
            if chosen is None:
                lines.append(('<a:uzi:1487936659692458054>' if is_golden else '<:gunforward:1484655577836683434>') + ' O disparo aconteceu. Ninguém foi eliminado.')
            else:
                base_each = 0
                base_remainder = 0
                bonus_each = 0
                bonus_remainder = 0
                if winners:
                    chosen_spend = self._buckshot_entry_spend(session, chosen.id, stake)
                    eliminated_normal_total = max(0, int(chosen_spend.get('chips', 0) or 0))
                    eliminated_bonus_total = max(0, int(chosen_spend.get('bonus', 0) or 0))
                    base_each = eliminated_normal_total // len(winners) if eliminated_normal_total > 0 else 0
                    base_remainder = eliminated_normal_total % len(winners) if eliminated_normal_total > 0 else 0
                    eliminated_bonus_each = eliminated_bonus_total // len(winners) if eliminated_bonus_total > 0 else 0
                    eliminated_bonus_remainder = eliminated_bonus_total % len(winners) if eliminated_bonus_total > 0 else 0
                    bonus_each = bonus_total // len(winners) if bonus_total > 0 else 0
                    bonus_remainder = bonus_total % len(winners) if bonus_total > 0 else 0
                    for index, winner in enumerate(winners):
                        own_spend = self._buckshot_entry_spend(session, winner.id, stake)
                        returned_normal = max(0, int(own_spend.get('chips', 0) or 0))
                        returned_bonus = max(0, int(own_spend.get('bonus', 0) or 0))
                        normal_gain = returned_normal + base_each + (1 if index < base_remainder else 0)
                        bonus_gain = returned_bonus + eliminated_bonus_each + (1 if index < eliminated_bonus_remainder else 0) + bonus_each + (1 if index < bonus_remainder else 0) + golden_bonus_each
                        if normal_gain > 0:
                            await self._change_user_chips(guild.id, winner.id, normal_gain, reason="Vitória no buckshot")
                        if bonus_gain > 0:
                            await self._change_user_bonus_chips(guild.id, winner.id, bonus_gain, reason='Vitória no buckshot')
                        if normal_gain > 0 or bonus_gain > 0:
                            await self.db.add_user_game_stat(guild.id, winner.id, 'buckshot_survivals', 1)
                            if is_golden:
                                await self.db.add_user_game_stat(guild.id, winner.id, 'buckshot_golden_survivals', 1)
                            await self._record_game_played(guild.id, winner.id, weekly_points=8)
                            await self._grant_weekly_points(guild.id, winner.id, max(3, (normal_gain + bonus_gain) // 3))
                await self.db.add_user_game_stat(guild.id, chosen.id, 'buckshot_eliminations', 1)
                if is_golden:
                    await self.db.add_user_game_stat(guild.id, chosen.id, 'buckshot_golden_eliminations', 1)
                await self._record_game_played(guild.id, chosen.id, weekly_points=3)
                refund = await self._maybe_apply_coringa_lobby_refund(guild.id, chosen.id, stake)
                lines.append(f"{'<a:uzi:1487936659692458054>💥' if is_golden else '<:gunforward:1484655577836683434>💥'} O disparo aconteceu. {chosen.mention} foi eliminado.")
                if refund > 0:
                    effect_note = self._race_effect_message(guild.id, chosen.id, 'as', f"{chosen.mention} recuperou {self._chip_text(refund, kind='gain')} da entrada.")
                    if effect_note:
                        lines.append(effect_note)
                if winners:
                    winner_count = len(winners)
                    returned_entry_text = self._chip_amount(stake)
                    if chosen_spend.get('chips', 0) and chosen_spend.get('bonus', 0):
                        eliminated_entry_text = f"**{chosen_spend['chips']} {self._CHIP_GAIN_EMOJI}** + **{chosen_spend['bonus']} {self._CHIP_BONUS_EMOJI}**"
                    elif chosen_spend.get('bonus', 0):
                        eliminated_entry_text = f"**{chosen_spend['bonus']} {self._CHIP_BONUS_EMOJI}**"
                    else:
                        eliminated_entry_text = f"**{chosen_spend['chips']} {self._CHIP_GAIN_EMOJI}**"

                    lines.append(f"Cada sobrevivente recebeu a própria entrada de volta: {returned_entry_text}.")

                    if eliminated_normal_total > 0 or eliminated_bonus_total > 0:
                        share_normal_max = base_each + (1 if base_remainder > 0 else 0)
                        share_normal_min = base_each
                        share_bonus_max = eliminated_bonus_each + (1 if eliminated_bonus_remainder > 0 else 0)
                        share_bonus_min = eliminated_bonus_each
                        share_parts = []
                        if eliminated_normal_total > 0:
                            if base_remainder == 0:
                                share_parts.append(f"**{base_each} {self._CHIP_GAIN_EMOJI}** para cada um")
                            else:
                                share_parts.append(f"**{share_normal_max} {self._CHIP_GAIN_EMOJI}** para alguns e **{share_normal_min} {self._CHIP_GAIN_EMOJI}** para os demais")
                        if eliminated_bonus_total > 0:
                            if eliminated_bonus_remainder == 0:
                                share_parts.append(f"**{eliminated_bonus_each} {self._CHIP_BONUS_EMOJI}** para cada um")
                            else:
                                share_parts.append(f"**{share_bonus_max} {self._CHIP_BONUS_EMOJI}** para alguns e **{share_bonus_min} {self._CHIP_BONUS_EMOJI}** para os demais")
                        lines.append(
                            f"A entrada do eliminado ({eliminated_entry_text}) foi dividida entre **{winner_count}** sobreviventes: " + " • ".join(share_parts) + "."
                        )

                    if bonus_total > 0:
                        max_bonus_gain = bonus_each + (1 if bonus_remainder > 0 else 0)
                        min_bonus_gain = bonus_each
                        if bonus_remainder == 0:
                            lines.append(
                                f"Bônus da rodada: **{bonus_each} {self._CHIP_BONUS_EMOJI}** para cada sobrevivente."
                            )
                        else:
                            lines.append(
                                f"Bônus da rodada: **{max_bonus_gain} {self._CHIP_BONUS_EMOJI}** para alguns e **{min_bonus_gain} {self._CHIP_BONUS_EMOJI}** para os demais."
                            )

                    if eliminated_normal_total <= 0 and eliminated_bonus_total <= 0 and bonus_total <= 0 and golden_bonus_each <= 0:
                        lines.append("Os sobreviventes não receberam nada.")

                    if is_golden and winners:
                        lines.append(f"Bônus dourado: cada sobrevivente recebeu **+{golden_bonus_each} {self._CHIP_BONUS_EMOJI}**.")
                else:
                    lines.append(f"Ninguém sobreviveu para receber a entrada de **{eliminated_entry_total} {self._CHIP_LOSS_EMOJI}**.")

            if lobby_message is not None:
                try:
                    await lobby_message.edit(view=_BuckshotLobbyClosedView('✨ Resultado do buckshot dourado' if is_golden else '<:gunforward:1484655577836683434> Resultado do buckshot', lines, color=self._buckshot_color(session)))
                except Exception:
                    pass
            current = self._buckshot_sessions.get(guild_id)
            if current is session:
                self._buckshot_sessions.pop(guild_id, None)
            return True

    async def _handle_buckshot_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False
        content = (message.content or '')
        if not self._matches_exact_trigger(content, 'buckshot'):
            return False
        if not self.db.gincana_enabled(guild.id):
            return True
        if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True
        last_used = float(self._buckshot_last_used.get(guild.id, 0.0))
        cooldown_remaining = max(0.0, (last_used + 6.0) - time.time())
        if cooldown_remaining > 0:
            try:
                await message.channel.send(embed=self._make_embed('💥 Aguarde um pouco', f'Espere **{int(cooldown_remaining) + 1}s** para abrir outra rodada de buckshot.', ok=False))
            except Exception:
                pass
            return True
        if self._get_buckshot_session(guild.id) is not None:
            return True
        variant = 'golden' if random.random() < self._special_variant_chance_for_user(guild.id, message.author.id) else 'normal'
        stake = 30 if variant == 'golden' else BUCKSHOT_STAKE
        bonus_before = self._get_user_bonus_chips(guild.id, message.author.id)
        needs_negative_confirm = self._needs_negative_confirmation(guild.id, message.author.id, stake)
        if needs_negative_confirm:
            confirmed = await self._confirm_negative_from_message(message, guild.id, message.author.id, stake, title="💥 Confirmar entrada")
            if not confirmed:
                return True
        paid, _balance, note = await self._try_consume_chips(guild.id, message.author.id, stake, reason="Entrada no buckshot")
        if needs_negative_confirm:
            note = None
        if not paid:
            try: await message.channel.send(embed=self._make_embed('💥 Saldo insuficiente', note or 'Você não tem saldo suficiente para entrar.', ok=False))
            except Exception: pass
            return True
        session = {
            'voice_channel_id': 0,
            'text_channel_id': message.channel.id,
            'owner_id': message.author.id,
            'manual_participants': {message.author.id},
            'focus_participants': set(),
            'locked_participants': {message.author.id},
            'entry_spend': {message.author.id: {'bonus': min(max(0, int(bonus_before)), stake), 'chips': max(0, stake - min(max(0, int(bonus_before)), stake))}},
            'lobby_message': None,
            'message': None,
            'view': None,
            'ended': False,
            'starting': False,
            'start_countdown': 0,
            'countdown_task': None,
            'variant': variant,
            '_last_render_key': None,
        }
        self._touch_runtime_state(session, kind='buckshot', guild_id=guild.id)
        self._buckshot_sessions[guild.id] = session
        view = _BuckshotJoinView(self, guild.id, session, guild, timeout=30.0)
        session['view'] = view
        try:
            panel_message = await message.channel.send(view=view)
        except Exception:
            self._buckshot_sessions.pop(guild.id, None)
            await self._buckshot_refund_entry(guild.id, session, message.author.id, stake)
            return True
        session['lobby_message'] = panel_message
        await self._react_with_emoji(message, self._buckshot_reaction_emoji(session), keep=True)
        return True

    async def _handle_atirar_trigger(self, message: discord.Message) -> bool:
        return False
