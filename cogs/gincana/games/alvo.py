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

class _TargetJoinView(discord.ui.View):
    def __init__(self, cog: "GincanaTriggerMixin", guild_id: int, *, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id
        self.join_button = discord.ui.Button(style=discord.ButtonStyle.success, label="🎯 Entrar (0)")
        self.join_button.callback = self._join_round
        self.add_item(self.join_button)

    async def _join_round(self, interaction: discord.Interaction):
        await self.cog._handle_target_button(interaction, self)

    async def on_timeout(self):
        try:
            await self.cog._finish_target_round(self.guild_id, reason="timeout")
        except Exception:
            pass


class GincanaAlvoMixin:
        def _get_target_session(self, guild_id: int) -> dict | None:
            session = self._target_sessions.get(guild_id)
            if session and session.get("ended"):
                self._target_sessions.pop(guild_id, None)
                return None
            return session
        def _get_target_voice_channel(self, guild: discord.Guild, session: dict) -> discord.VoiceChannel | None:
            channel = guild.get_channel(int(session.get("voice_channel_id") or 0))
            return channel if isinstance(channel, discord.VoiceChannel) else None
        def _get_target_participants(self, guild: discord.Guild, session: dict) -> list[discord.Member]:
            voice_channel = self._get_target_voice_channel(guild, session)
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
        def _describe_target_zone(self, score: int) -> str:
            return {3: "centro", 2: "anel interno", 1: "anel externo", 0: "errou"}.get(int(score), "errou")
        def _roll_target_modifier(self) -> dict:
            roll = random.random()
            if roll < 0.08:
                return {
                    "key": "small",
                    "name": "Alvo pequeno",
                    "description": "Centro mais raro, mas cada bullseye rende bônus extra.",
                    "bullseye_bonus": 4,
                }
            if roll < 0.16:
                return {
                    "key": "unstable",
                    "name": "Alvo instável",
                    "description": "O alvo balança mais e aumenta a chance de erro.",
                }
            if roll < 0.24:
                return {
                    "key": "generous",
                    "name": "Alvo generoso",
                    "description": "Fica mais fácil acertar os anéis internos.",
                }
            if roll < 0.32:
                return {
                    "key": "windy",
                    "name": "Vento forte",
                    "description": "Rajadas atrapalham a precisão, mas o prêmio sobe.",
                    "pot_bonus": 6,
                }
            if roll < 0.40:
                return {
                    "key": "golden",
                    "name": "Alvo dourado",
                    "description": "Bullseyes valem mais e a rodada fica brilhando.",
                    "pot_bonus": 8,
                    "bullseye_bonus": 6,
                }
            return {
                "key": "normal",
                "name": "Alvo padrão",
                "description": "Rodada normal.",
            }
        def _roll_target_score(self, modifier_key: str = "normal") -> int:
            roll = random.random()
            if modifier_key == "small":
                if roll < 0.04:
                    return 3
                if roll < 0.20:
                    return 2
                if roll < 0.52:
                    return 1
                return 0
            if modifier_key == "unstable":
                if roll < 0.05:
                    return 3
                if roll < 0.19:
                    return 2
                if roll < 0.44:
                    return 1
                return 0
            if modifier_key == "generous":
                if roll < 0.09:
                    return 3
                if roll < 0.31:
                    return 2
                if roll < 0.67:
                    return 1
                return 0
            if modifier_key == "windy":
                if roll < 0.05:
                    return 3
                if roll < 0.18:
                    return 2
                if roll < 0.46:
                    return 1
                return 0
            if modifier_key == "golden":
                if roll < 0.10:
                    return 3
                if roll < 0.27:
                    return 2
                if roll < 0.58:
                    return 1
                return 0
            if roll < 0.07:
                return 3
            if roll < 0.25:
                return 2
            if roll < 0.55:
                return 1
            return 0
        def _allocate_target_rewards(self, participants: list[discord.Member], scores: dict[int, int], pot_total: int) -> tuple[dict[int, int], list[tuple[str, list[discord.Member], int]]]:
            rewards: dict[int, int] = {}
            placement_groups: list[tuple[str, list[discord.Member], int]] = []
            if not participants or pot_total <= 0:
                return rewards, placement_groups

            if len(participants) == 2:
                best_score = max(scores.get(member.id, 0) for member in participants)
                top_members = [member for member in participants if scores.get(member.id, 0) == best_score]
                winner = random.choice(top_members)
                rewards[winner.id] = pot_total
                placement_groups.append(("🥇", [winner], pot_total))
                return rewards, placement_groups

            ordered_scores = sorted({scores.get(member.id, 0) for member in participants}, reverse=True)
            first_members = [member for member in participants if scores.get(member.id, 0) == ordered_scores[0]]
            remaining_pool = pot_total

            if len(ordered_scores) > 1:
                first_pool = int(round(pot_total * 0.6))
                second_pool = pot_total - first_pool
                second_members = [member for member in participants if scores.get(member.id, 0) == ordered_scores[1]]
            else:
                first_pool = pot_total
                second_pool = 0
                second_members = []

            def split_pool(members: list[discord.Member], total: int):
                if not members or total <= 0:
                    return
                each = total // len(members)
                remainder = total % len(members)
                for index, member in enumerate(members):
                    rewards[member.id] = rewards.get(member.id, 0) + each + (1 if index < remainder else 0)

            split_pool(first_members, first_pool)
            placement_groups.append(("🥇", first_members, first_pool))
            remaining_pool -= first_pool

            if second_members and second_pool > 0:
                split_pool(second_members, second_pool)
                placement_groups.append(("🥈", second_members, second_pool))
                remaining_pool -= second_pool

            if remaining_pool > 0 and first_members:
                split_pool(first_members, remaining_pool)
                placement_groups[0] = (placement_groups[0][0], placement_groups[0][1], placement_groups[0][2] + remaining_pool)

            return rewards, placement_groups
        def _target_zone_style(self, score: int) -> tuple[str, str]:
            score = int(score)
            if score >= 3:
                return "🎯", "CENTRO!"
            if score == 2:
                return "🟠", "anel interno"
            if score == 1:
                return "🟡", "anel externo"
            return "💨", "errou"
        def _format_target_participants(self, participants: list[discord.Member]) -> str:
            if not participants:
                return "Ninguém entrou ainda."
            mentions = [member.mention for member in participants[:8]]
            text = ", ".join(mentions)
            if len(participants) > 8:
                text += f" e mais **{len(participants) - 8}**"
            return text
        def _target_opening_text(self, participants: list[discord.Member]) -> str:
            if len(participants) >= 3:
                return "Os **2 melhores tiros** levam o prêmio."
            if len(participants) == 2:
                return "Com **2 participantes**, só **1** leva o prêmio."
            return "Use o botão para entrar e a trigger **disparar** para fechar a rodada."
        def _target_bonus_for_participants(self, count: int) -> int:
            if count >= 7:
                return 10
            if count >= 5:
                return 5
            return 0
        def _build_target_special_lines(self, participants: list[discord.Member], scores: dict[int, int], placements: list[tuple[str, list[discord.Member], int]]) -> list[str]:
            special: list[str] = []
            bullseyes = [member for member in participants if scores.get(member.id, 0) == 3]
            misses = [member for member in participants if scores.get(member.id, 0) <= 0]
            if len(misses) == len(participants):
                special.append("💨 Ninguém acertou o alvo. A rodada virou um desastre completo.")
            if len(bullseyes) >= 2:
                special.append(f"🎯 Chuva de bullseyes: {', '.join(member.mention for member in bullseyes)}!")
            elif len(bullseyes) == 1 and len(participants) >= 4:
                special.append(f"🏅 {bullseyes[0].mention} dominou a rodada com um bullseye raro.")
            if placements:
                top_badge, top_members, _ = placements[0]
                if len(top_members) > 1:
                    special.append(f"🤝 O topo terminou empatado entre {', '.join(member.mention for member in top_members)}.")
                elif top_members and scores.get(top_members[0].id, 0) >= 2 and all(scores.get(m.id, 0) < scores.get(top_members[0].id, 0) for m in participants if m.id != top_members[0].id):
                    special.append(f"🔥 {top_members[0].mention} levou a melhor com folga.")
            return special
        def _make_target_embed(self, guild: discord.Guild, session: dict, *, final_text: str | None = None, aiming: bool = False) -> discord.Embed:
            participants = self._get_target_participants(guild, session)
            locked_ids = set(session.get("locked_participants", set()))
            pot_total = len(locked_ids) * ALVO_STAKE
            owner_id = int(session.get("owner_id") or 0)
            owner = guild.get_member(owner_id) if owner_id else None
            modifier = session.get("modifier") or {"key": "normal", "name": "Alvo padrão", "description": "Rodada normal."}
            bonus = int(session.get("bonus_chips") or 0)

            if final_text:
                embed = discord.Embed(
                    title="🎯 Resultado do alvo",
                    description=final_text,
                    color=discord.Color.blurple(),
                )
            elif aiming:
                embed = discord.Embed(
                    title="🎯 Mirando...",
                    description=(
                        f"Participantes: **{len(participants)}**\n"
                        f"{self._CHIP_GAIN_EMOJI} Pote: {self._chip_amount(pot_total)}\n\n"
                        "Os tiros estão sendo alinhados..."
                    ),
                    color=discord.Color.blurple(),
                )
                embed.add_field(name="Na mira", value=self._format_target_participants(participants), inline=False)
            else:
                total_bonus = bonus + int(modifier.get("pot_bonus", 0) or 0)
                embed = discord.Embed(
                    title="🎯 Tiro ao alvo aberto",
                    description=(
                        f"Entrada: {self._chip_amount(ALVO_STAKE)} por jogador\n"
                        f"Participantes: **{len(participants)}**\n"
                        f"{self._CHIP_GAIN_EMOJI} Pote atual: {self._chip_amount(pot_total + total_bonus)}\n\n"
                        f"{self._target_opening_text(participants)}"
                    ),
                    color=discord.Color.blurple(),
                )
                embed.add_field(name="🎯 Na mira", value=self._format_target_participants(participants), inline=False)
                embed.add_field(name="🌪️ Condição da rodada", value=f"**{modifier.get('name','Alvo padrão')}**\n{modifier.get('description','Rodada normal.')}", inline=False)
                embed.add_field(name="Como dispara", value="Entre pelo botão verde. Depois use a trigger **disparar** na call da rodada ou espere o tempo acabar.", inline=False)
                embed.set_footer(text="Entrou, pagou e a entrada fica travada até o fim")

            if owner is not None:
                embed.set_author(name=f"Rodada aberta por {owner.display_name}", icon_url=owner.display_avatar.url)
            return embed
        async def _refresh_target_message(self, guild_id: int):
            session = self._get_target_session(guild_id)
            if session is None or session.get("ended"):
                return
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            message = session.get("message")
            view = session.get("view")
            if message is None or view is None:
                return
            participants = self._get_target_participants(guild, session)
            view.join_button.label = f"🎯 Entrar ({len(participants)})"
            view.join_button.style = discord.ButtonStyle.success
            try:
                await message.edit(embed=self._make_target_embed(guild, session), view=view)
            except Exception:
                pass
        async def _handle_target_button(self, interaction: discord.Interaction, view: _TargetJoinView):
            guild = interaction.guild
            user = interaction.user
            if guild is None or not isinstance(user, discord.Member):
                try:
                    await interaction.response.send_message("Não foi possível entrar nessa rodada agora.", ephemeral=True)
                except Exception:
                    pass
                return

            session = self._get_target_session(guild.id)
            if session is None or session.get("view") is not view or session.get("ended"):
                try:
                    await interaction.response.send_message("Essa rodada já terminou.", ephemeral=True)
                except Exception:
                    pass
                return

            voice_channel = self._get_target_voice_channel(guild, session)
            if voice_channel is None:
                await self._finish_target_round(guild.id, reason="channel_missing")
                try:
                    await interaction.response.send_message("A rodada foi encerrada porque o canal de voz sumiu.", ephemeral=True)
                except Exception:
                    pass
                return

            if getattr(user.voice, "channel", None) != voice_channel:
                try:
                    await interaction.response.send_message("Você precisa estar na mesma call da rodada para entrar.", ephemeral=True)
                except Exception:
                    pass
                return

            locked = session.setdefault("locked_participants", set())
            if user.id in locked:
                try:
                    await interaction.response.send_message("Você já entrou nessa rodada e sua entrada ficou travada até o fim.", ephemeral=True)
                except Exception:
                    pass
                return

            paid, _balance, chip_note = await self._try_consume_chips(guild.id, user.id, ALVO_STAKE)
            if not paid:
                try:
                    await interaction.response.send_message(chip_note or "Você não tem saldo suficiente para entrar nessa rodada.", ephemeral=True)
                except Exception:
                    pass
                return

            locked.add(user.id)
            try:
                await interaction.response.send_message(chip_note or f"Você entrou na rodada pagando {self._chip_amount(ALVO_STAKE)}.", ephemeral=True)
            except Exception:
                pass
            await self._refresh_target_message(guild.id)
        async def _finish_target_round(self, guild_id: int, *, reason: str) -> bool:
            session = self._get_target_session(guild_id)
            if session is None or session.get("ended"):
                return False
            session["ended"] = True
            self._target_last_used[guild_id] = time.time()

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                self._target_sessions.pop(guild_id, None)
                return False

            message = session.get("message")
            view = session.get("view")
            if isinstance(view, discord.ui.View):
                for child in view.children:
                    child.disabled = True
                try:
                    view.stop()
                except Exception:
                    pass

            participants = self._get_target_participants(guild, session)
            locked_ids = set(session.get("locked_participants", set()))

            if len(locked_ids) == 1:
                only_id = next(iter(locked_ids))
                await self.db.add_user_chips(guild.id, only_id, ALVO_STAKE)
                only_member = guild.get_member(only_id)
                final_text = f"A rodada foi cancelada porque só {only_member.mention if only_member else '1 participante'} entrou. A entrada foi reembolsada."
            elif len(participants) < 2:
                for user_id in locked_ids:
                    await self.db.add_user_chips(guild.id, user_id, ALVO_STAKE)
                final_text = "A rodada foi cancelada porque não ficaram participantes suficientes na call. As entradas foram reembolsadas."
            else:
                pot_total = len(locked_ids) * ALVO_STAKE
                bonus_chips = int(session.get("bonus_chips") or 0)
                modifier = session.get("modifier") or {"key": "normal", "name": "Alvo padrão", "description": "Rodada normal."}
                bonus_chips += int(modifier.get("pot_bonus", 0) or 0)
                if message is not None:
                    try:
                        await message.edit(embed=self._make_target_embed(guild, session, aiming=True), view=view)
                        await asyncio.sleep(1.35)
                    except Exception:
                        pass

                scores = {member.id: self._roll_target_score(str(modifier.get("key", "normal"))) for member in participants}
                rewards, placements = self._allocate_target_rewards(participants, scores, pot_total + bonus_chips)
                result_lines = [f"💥 Os tiros foram disparados. {self._CHIP_GAIN_EMOJI} Pote final: {self._chip_amount(pot_total)}", ""]
                bullseye_members: list[discord.Member] = []
                for member in sorted(participants, key=lambda m: (-scores.get(m.id, 0), m.display_name.casefold())):
                    score = scores.get(member.id, 0)
                    icon, zone = self._target_zone_style(score)
                    await self.db.add_user_game_stat(guild.id, member.id, "alvo_shots", 1)
                    await self._record_game_played(guild.id, member.id, weekly_points=4 + score)
                    if score > 0:
                        await self.db.add_user_game_stat(guild.id, member.id, "alvo_hits", 1)
                    result_lines.append(f"{icon} {member.mention} acertou **{zone}**.")
                    if score == 3:
                        bullseye_members.append(member)
                        await self.db.add_user_game_stat(guild.id, member.id, "alvo_bullseyes", 1)

                if bullseye_members:
                    names = ", ".join(member.mention for member in bullseye_members)
                    result_lines.append("")
                    result_lines.append(f"🎯 Bullseye de destaque: {names}!")
                    bull_bonus = int(modifier.get("bullseye_bonus", 0) or 0)
                    if bull_bonus > 0:
                        for member in bullseye_members:
                            await self.db.add_user_chips(guild.id, member.id, bull_bonus)
                            await self._grant_weekly_points(guild.id, member.id, bull_bonus)
                        result_lines.append(f"✨ Cada bullseye recebeu um bônus de {self._chip_amount(bull_bonus)}.")

                if rewards:
                    result_lines.append("")
                    for badge, members, total in placements:
                        if not members or total <= 0:
                            continue
                        member_mentions = ", ".join(member.mention for member in members)
                        result_lines.append(f"{badge} {member_mentions} — {self._chip_amount(total)}")
                    for user_id, amount in rewards.items():
                        if amount > 0:
                            await self.db.add_user_chips(guild.id, user_id, amount)
                            await self.db.add_user_game_stat(guild.id, user_id, "alvo_wins", 1)
                            await self._grant_weekly_points(guild.id, user_id, max(5, amount // 4))
                final_text = "\n".join(result_lines)

            embed = self._make_target_embed(guild, session, final_text=final_text)
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

            self._target_sessions.pop(guild_id, None)
            return True
        async def _handle_target_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False

            content = (message.content or "")
            if not self._matches_exact_trigger(content, "alvo"):
                return False

            if GUILD_IDS and guild.id not in GUILD_IDS:
                return True

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            if self._get_target_session(guild.id) is not None:
                return True

            last_used = float(self._target_last_used.get(guild.id, 0.0) or 0.0)
            cooldown_remaining = max(0.0, (last_used + 6.0) - time.time())
            if cooldown_remaining > 0:
                try:
                    await message.channel.send(embed=self._make_embed("🎯 Aguarde um pouco", f"Espere **{int(cooldown_remaining) + 1}s** para abrir outra rodada de alvo.", ok=False))
                except Exception:
                    pass
                return True

            author_voice = getattr(message.author, "voice", None)
            voice_channel = getattr(author_voice, "channel", None)
            if not isinstance(voice_channel, discord.VoiceChannel):
                return True

            paid, _balance, chip_note = await self._try_consume_chips(guild.id, message.author.id, ALVO_STAKE)
            if not paid:
                try:
                    await message.channel.send(embed=self._make_embed("🎯 Saldo insuficiente", chip_note or "Você não tem saldo suficiente.", ok=False))
                except Exception:
                    pass
                return True

            view = _TargetJoinView(self, guild.id, timeout=30.0)
            participants_now = len([m for m in voice_channel.members if not getattr(m, "bot", False)])
            session = {
                "voice_channel_id": voice_channel.id,
                "text_channel_id": message.channel.id,
                "owner_id": message.author.id,
                "locked_participants": {message.author.id},
                "modifier": self._roll_target_modifier(),
                "bonus_chips": self._target_bonus_for_participants(participants_now),
                "message": None,
                "view": view,
                "ended": False,
                "timeout_task": None,
            }
            self._target_sessions[guild.id] = session

            view.join_button.label = f"🎯 Entrar ({len(self._get_target_participants(guild, session))})"
            embed = self._make_target_embed(guild, session)
            if chip_note:
                embed.set_footer(text=f"{chip_note} Entrou, pagou e a entrada fica travada até o fim.")
            try:
                panel_message = await message.channel.send(embed=embed, view=view)
            except Exception:
                self._target_sessions.pop(guild.id, None)
                await self.db.add_user_chips(guild.id, message.author.id, ALVO_STAKE)
                return True

            session["message"] = panel_message
            session["timeout_task"] = self.bot.loop.create_task(view.wait())
            await self._react_with_emoji(message, "🎯", keep=True)
            return True
        async def _handle_disparar_trigger(self, message: discord.Message) -> bool:
            guild = message.guild
            if guild is None:
                return False

            content = (message.content or "")
            if not self._matches_exact_trigger(content, "disparar"):
                return False

            if GUILD_IDS and guild.id not in GUILD_IDS:
                return True

            if not self.db.gincana_enabled(guild.id):
                return True

            if self._gincana_only_kick_members(guild.id) and not self._is_staff_member(message.author):
                return True

            session = self._get_target_session(guild.id)
            if session is None:
                return True

            voice_channel = self._get_target_voice_channel(guild, session)
            if voice_channel is None:
                await self._finish_target_round(guild.id, reason="channel_missing")
                return True

            if getattr(message.author.voice, "channel", None) != voice_channel:
                return True

            participants = self._get_target_participants(guild, session)
            if len(participants) < 2:
                try:
                    await message.channel.send(embed=self._make_embed("🎯 Ainda faltam jogadores", "O alvo precisa de pelo menos **2 participantes** na call para disparar.", ok=False))
                except Exception:
                    pass
                return True

            await self._finish_target_round(guild.id, reason="manual")
            await self._react_with_emoji(message, "💥", keep=True)
            return True
