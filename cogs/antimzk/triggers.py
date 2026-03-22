import asyncio
import random
import time
from pathlib import Path

import discord

from config import GUILD_IDS, MUTE_TOGGLE_WORD, OFF_COLOR, TRIGGER_WORD

from .constants import (
    _ATIRAR_WORD_RE,
    _BUCKSHOT_WORD_RE,
    _DJ_DURATION_SECONDS,
    _DJ_TOGGLE_WORD_RE,
    _PICA_DURATION_SECONDS,
    _POKER_WORD_RE,
    _ROLETA_WORD_RE,
    _ROLE_TOGGLE_WORD_RE,
    BUCKSHOT_STAKE,
    ROLETA_COST,
    ROLETA_JACKPOT_CHIPS,
)



class _BuckshotJoinView(discord.ui.View):
    def __init__(self, cog: "AntiMzkTriggerMixin", guild_id: int, *, timeout: float = 30.0):
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


class AntiMzkTriggerMixin:
    async def _expire_pica_role_later(self, guild_id: int, user_id: int, role_id: int, delay: float):
        try:
            await asyncio.sleep(max(0.0, delay))
        except Exception:
            return

        key = (guild_id, user_id)
        expires_at = self._pica_expirations.get(key)
        now = time.time()
        if expires_at is None or expires_at > now + 1.0:
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            self._pica_expirations.pop(key, None)
            return

        member = guild.get_member(user_id)
        role = guild.get_role(role_id) if role_id else None
        if member is None or role is None:
            self._pica_expirations.pop(key, None)
            return

        try:
            if role in getattr(member, "roles", []):
                await member.remove_roles(role, reason="modo censura pica expirado")
        except Exception:
            return
        finally:
            self._pica_expirations.pop(key, None)

        try:
            await self._refresh_target_suffix_nickname(member, role)
        except Exception:
            pass

    async def _expire_dj_block_later(self, guild_id: int, channel_id: int, user_id: int, delay: float):
        try:
            await asyncio.sleep(max(0.0, delay))
        except Exception:
            return

        key = (guild_id, channel_id, user_id)
        expires_at = self._dj_expirations.get(key)
        now = time.time()
        if expires_at is None or expires_at > now + 1.0:
            return

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            self._dj_expirations.pop(key, None)
            return

        channel = guild.get_channel(channel_id)
        member = guild.get_member(user_id)
        if not isinstance(channel, discord.VoiceChannel) or member is None:
            self._dj_expirations.pop(key, None)
            return

        try:
            overwrite = channel.overwrites_for(member)
            overwrite.use_soundboard = None
            if overwrite.is_empty():
                await channel.set_permissions(member, overwrite=None, reason="modo censura dj expirado")
            else:
                await channel.set_permissions(member, overwrite=overwrite, reason="modo censura dj expirado")
        except Exception:
            return
        finally:
            self._dj_expirations.pop(key, None)

    def _tracked_pica_targets(self, guild: discord.Guild, current_targets: list[discord.Member]) -> list[discord.Member]:
        targets: dict[int, discord.Member] = {member.id: member for member in current_targets}
        for tracked_guild_id, tracked_user_id in list(self._pica_expirations.keys()):
            if tracked_guild_id != guild.id:
                continue
            member = guild.get_member(tracked_user_id)
            if member is not None:
                targets[member.id] = member
        return list(targets.values())

    def _tracked_dj_targets(self, guild: discord.Guild, voice_channel: discord.VoiceChannel, current_targets: list[discord.Member]) -> list[discord.Member]:
        targets: dict[int, discord.Member] = {member.id: member for member in current_targets}
        for tracked_guild_id, tracked_channel_id, tracked_user_id in list(self._dj_expirations.keys()):
            if tracked_guild_id != guild.id or tracked_channel_id != voice_channel.id:
                continue
            member = guild.get_member(tracked_user_id)
            if member is not None:
                targets[member.id] = member
        return list(targets.values())

    async def _send_role_toggle_feedback(self, message: discord.Message, activated: bool):
        title = "🔇 TTS desativado para os alvos" if activated else "🔊 TTS reativado para os alvos"
        description = (
            "Por **`2 horas`** o cargo de ignorar TTS foi aplicado aos alvos atuais do modo censura."
            if activated
            else "O cargo de ignorar TTS foi removido dos alvos atuais do modo censura."
        )
        embed = self._make_embed(title, description, ok=not activated)
        try:
            await message.channel.send(embed=embed)
        except Exception:
            pass

    async def _handle_role_toggle_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = (message.content or "")
        if not _ROLE_TOGGLE_WORD_RE.search(content):
            return False

        if GUILD_IDS and guild.id not in GUILD_IDS:
            return True

        if not self.db.anti_mzk_enabled(guild.id):
            return True

        if self._anti_mzk_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True

        if self._is_focused_non_staff_member(message.author):
            return True

        author_voice = getattr(message.author, "voice", None)
        voice_channel = getattr(author_voice, "channel", None)
        if not isinstance(voice_channel, discord.VoiceChannel):
            return True

        base_targets = self._resolve_targets(guild, voice_channel)
        targets = self._tracked_pica_targets(guild, base_targets)
        if not targets:
            return True

        ignored_tts_role = None
        ignored_tts_role_id = 0
        try:
            ignored_tts_role_id = max(0, int(self.db.get_ignored_tts_role_id(guild.id) or 0))
        except Exception:
            ignored_tts_role_id = 0
        if ignored_tts_role_id:
            ignored_tts_role = guild.get_role(ignored_tts_role_id)

        if ignored_tts_role is None:
            embed = self._make_embed(
                "Cargo ignorado não configurado",
                "Defina primeiro o cargo ignorado do TTS no painel do servidor para usar a trigger **pica**.",
                ok=False,
            )
            try:
                await message.channel.send(embed=embed)
            except Exception:
                pass
            return True

        tracked_ids = {user_id for tracked_guild_id, user_id in self._pica_expirations.keys() if tracked_guild_id == guild.id}
        should_activate = any(target.id not in tracked_ids for target in base_targets) or not tracked_ids

        changed = False
        now = time.time()
        role_id = int(getattr(ignored_tts_role, "id", 0) or 0)
        for target in targets:
            try:
                key = (guild.id, target.id)
                if should_activate:
                    if ignored_tts_role not in getattr(target, "roles", []):
                        await target.add_roles(ignored_tts_role, reason="modo censura role toggle")
                    self._pica_expirations[key] = now + _PICA_DURATION_SECONDS
                    self.bot.loop.create_task(self._expire_pica_role_later(guild.id, target.id, role_id, _PICA_DURATION_SECONDS))
                    changed = True
                else:
                    self._pica_expirations.pop(key, None)
                    if ignored_tts_role in getattr(target, "roles", []):
                        await target.remove_roles(ignored_tts_role, reason="modo censura role toggle")
                    changed = True
            except Exception:
                pass

        if changed:
            await self._refresh_targets_suffix_nicknames(guild, targets)
            await self._send_role_toggle_feedback(message, should_activate)
            await self._react_with_emoji(message, "✅", keep=False)
        return True

    async def _send_dj_toggle_feedback(self, message: discord.Message, activated: bool, affected_count: int, voice_channel: discord.VoiceChannel):
        if activated:
            title = "🎛️ Efeitos sonoros bloqueados"
            description = (
                f"Os membros focados do modo censura ficaram **sem poder usar efeitos sonoros por `6 horas`** em {voice_channel.mention}.\n\n"
                "Staffs não são afetados"
            )
        else:
            title = "🎚️ Efeitos sonoros liberados"
            description = f"Removi o bloqueio de **efeitos sonoros** dos membros focados em {voice_channel.mention}."
        embed = self._make_embed(title, description, ok=not activated)
        try:
            await message.channel.send(embed=embed)
        except Exception:
            pass

    async def _handle_dj_toggle_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = (message.content or "")
        if not _DJ_TOGGLE_WORD_RE.search(content):
            return False

        if GUILD_IDS and guild.id not in GUILD_IDS:
            return True

        if not self.db.anti_mzk_enabled(guild.id):
            return True

        if self._anti_mzk_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True

        author_voice = getattr(message.author, "voice", None)
        voice_channel = getattr(author_voice, "channel", None)
        if not isinstance(voice_channel, discord.VoiceChannel):
            return True

        focus_targets = self._iter_focused_members(guild, voice_channel)
        current_targets = [member for member in focus_targets if not self._is_staff_member(member)]
        targets = self._tracked_dj_targets(guild, voice_channel, current_targets)

        if not targets:
            embed = self._make_embed(
                "Nenhum alvo para a trigger dj",
                "Não há membros focados elegíveis nesse canal de voz. Staffs são ignorados por essa trigger.",
                ok=False,
            )
            try:
                await message.channel.send(embed=embed)
            except Exception:
                pass
            return True

        tracked_ids = {user_id for tracked_guild_id, tracked_channel_id, user_id in self._dj_expirations.keys() if tracked_guild_id == guild.id and tracked_channel_id == voice_channel.id}
        should_activate = any(target.id not in tracked_ids for target in current_targets) or not tracked_ids

        changed = 0
        now = time.time()
        for target in targets:
            try:
                overwrite = voice_channel.overwrites_for(target)
                key = (guild.id, voice_channel.id, target.id)
                if should_activate:
                    overwrite.use_soundboard = False
                    await voice_channel.set_permissions(target, overwrite=overwrite, reason="modo censura dj trigger")
                    self._dj_expirations[key] = now + _DJ_DURATION_SECONDS
                    self.bot.loop.create_task(self._expire_dj_block_later(guild.id, voice_channel.id, target.id, _DJ_DURATION_SECONDS))
                else:
                    self._dj_expirations.pop(key, None)
                    overwrite.use_soundboard = None
                    if overwrite.is_empty():
                        await voice_channel.set_permissions(target, overwrite=None, reason="modo censura dj trigger")
                    else:
                        await voice_channel.set_permissions(target, overwrite=overwrite, reason="modo censura dj trigger")
                changed += 1
            except Exception:
                pass

        if changed:
            await self._send_dj_toggle_feedback(message, should_activate, changed, voice_channel)
            await self._react_success_temporarily(message)
        return True


    def _sfx_path(self, filename: str) -> Path:
        return Path(__file__).resolve().parents[2] / "assets" / "sfx" / filename

    def _buckshot_sfx_path(self) -> Path:
        return self._sfx_path("buckshot.mp3")

    def _pinto_sfx_path(self) -> Path:
        return self._sfx_path("pinto.mp3")

    def _roleta_sfx_path(self) -> Path:
        return self._sfx_path("roleta777.mp3")

    async def _play_sfx_file(self, guild: discord.Guild, voice_channel: discord.VoiceChannel, sfx_path: Path) -> bool:
        if not sfx_path.exists():
            return False

        voice_client = guild.voice_client
        connected_here = False

        try:
            if voice_client is None or not getattr(voice_client, "is_connected", lambda: False)():
                voice_client = await voice_channel.connect(self_deaf=True)
                connected_here = True
            elif getattr(voice_client, "channel", None) != voice_channel:
                await voice_client.move_to(voice_channel)

            if voice_client is None:
                return False

            try:
                if voice_client.is_playing() or voice_client.is_paused():
                    voice_client.stop()
            except Exception:
                pass

            source = discord.FFmpegPCMAudio(str(sfx_path))
            voice_client.play(source)
            return True
        except Exception:
            return False
        finally:
            if connected_here and voice_client is not None:
                async def _delayed_disconnect(vc: discord.VoiceClient):
                    await asyncio.sleep(2.0)
                    try:
                        if vc.is_connected() and not vc.is_playing():
                            await vc.disconnect(force=False)
                    except Exception:
                        pass

                asyncio.create_task(_delayed_disconnect(voice_client))

    async def _play_buckshot_sfx(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> bool:
        return await self._play_sfx_file(guild, voice_channel, self._buckshot_sfx_path())

    async def _play_pinto_sfx(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> bool:
        return await self._play_sfx_file(guild, voice_channel, self._pinto_sfx_path())

    async def _play_roleta_sfx(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> bool:
        return await self._play_sfx_file(guild, voice_channel, self._roleta_sfx_path())

    def _build_roleta_column(self, middle: int | None = None) -> list[int]:
        return [random.randint(1, 9), middle if middle is not None else random.randint(1, 9), random.randint(1, 9)]

    def _spin_roleta_column(self, column: list[int]):
        column.insert(0, random.randint(1, 9))
        del column[3:]

    def _format_roleta_row(self, row: list[int], *, compact: bool = False) -> str:
        if compact:
            return f" {row[0]}  {row[1]}  {row[2]} "
        return f"  {row[0]}  {row[1]}  {row[2]}  "

    def _render_roleta_board(self, columns: list[list[int]]) -> str:
        rows = [[columns[0][i], columns[1][i], columns[2][i]] for i in range(3)]
        top_row = self._format_roleta_row(rows[0])
        middle_row = self._format_roleta_row(rows[1], compact=True)
        bottom_row = self._format_roleta_row(rows[2])
        lines = [
            "┌───────────┐",
            f"│{top_row}│",
            "├───────────┤",
            f"»│{middle_row}│«",
            "├───────────┤",
            f"│{bottom_row}│",
            "└───────────┘",
        ]
        return "```text\n" + "\n".join(lines) + "\n```"

    def _make_roleta_spin_embed(self, board: str) -> discord.Embed:
        return discord.Embed(
            title="🎰 Girando...",
            description=f"{self._CHIP_LOSS_EMOJI} Custo: **{ROLETA_COST} fichas**\n{self._CHIP_GAIN_EMOJI} Jackpot: **{ROLETA_JACKPOT_CHIPS} fichas**\n\n{board}",
            color=discord.Color.blurple(),
        )

    def _make_roleta_result_embed(self, title: str, summary: str, board: str, *, success: bool) -> discord.Embed:
        color = discord.Color.green() if success else discord.Color(OFF_COLOR)
        return discord.Embed(
            title=title,
            description=f"{summary}\n\n{board}",
            color=color,
        )

    async def _set_roleta_reaction(self, message: discord.Message, emoji: str, *, keep: bool):
        await self._react_with_emoji(message, emoji, keep=keep)

    async def _clear_roleta_reaction(self, message: discord.Message, emoji: str):
        reaction_emoji = emoji
        try:
            if isinstance(emoji, str) and emoji.startswith("<") and emoji.endswith(">"):
                reaction_emoji = discord.PartialEmoji.from_str(emoji)
            await message.remove_reaction(reaction_emoji, self.bot.user)
        except Exception:
            pass

    async def _react_with_emoji(self, message: discord.Message, emoji: str, *, keep: bool, delay: float = 3.0):
        reaction_emoji = emoji
        try:
            if isinstance(emoji, str) and emoji.startswith("<") and emoji.endswith(">"):
                reaction_emoji = discord.PartialEmoji.from_str(emoji)
            await message.add_reaction(reaction_emoji)
        except Exception:
            return

        if keep:
            return

        async def _cleanup():
            await asyncio.sleep(max(0.0, delay))
            try:
                await message.remove_reaction(reaction_emoji, self.bot.user)
            except Exception:
                pass

        asyncio.create_task(_cleanup())

    async def _animate_roleta_spin(self, message: discord.Message, *, target_middle: list[int]) -> tuple[discord.Message | None, list[list[int]] | None]:
        columns = [self._build_roleta_column() for _ in range(3)]
        try:
            spin_message = await message.channel.send(embed=self._make_roleta_spin_embed(self._render_roleta_board(columns)))
        except Exception:
            return None, None

        target_duration = 5.0
        intervals = [0.22, 0.26, 0.30, 0.35, 0.41, 0.49, 0.60, 0.74, 0.90, 1.05]
        scale = target_duration / sum(intervals)
        intervals = [step * scale for step in intervals]
        lock_steps = [len(intervals) - 3, len(intervals) - 2, len(intervals) - 1]
        locked_columns: set[int] = set()

        for index, delay in enumerate(intervals):
            await asyncio.sleep(delay)

            if index in lock_steps:
                lock_index = lock_steps.index(index)
                locked_columns.add(lock_index)
                columns[lock_index] = self._build_roleta_column(target_middle[lock_index])

            for column_index in range(3):
                if column_index not in locked_columns:
                    self._spin_roleta_column(columns[column_index])

            try:
                await spin_message.edit(embed=self._make_roleta_spin_embed(self._render_roleta_board(columns)))
            except Exception:
                pass

        return spin_message, columns

    async def _handle_roleta_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = (message.content or "")
        if not _ROLETA_WORD_RE.search(content):
            return False

        if GUILD_IDS and guild.id not in GUILD_IDS:
            return True

        if not self.db.anti_mzk_enabled(guild.id):
            return True

        if self._anti_mzk_only_kick_members(guild.id) and not self._is_staff_member(message.author):
            return True

        if guild.id in self._roleta_running_guilds:
            return True

        author_voice = getattr(message.author, "voice", None)
        voice_channel = getattr(author_voice, "channel", None)
        if not isinstance(voice_channel, discord.VoiceChannel):
            return True

        targets = self._resolve_targets(guild, voice_channel)
        if not targets:
            embed = self._make_embed(
                "🎲 Roleta sem alvos",
                "Não há usuários alvo do modo censura nesse canal de voz para usar a trigger **roleta**.",
                ok=False,
            )
            try:
                await message.channel.send(embed=embed)
            except Exception:
                pass
            return True

        paid, _balance, chip_note = await self._try_consume_chips(guild.id, message.author.id, ROLETA_COST)
        if not paid:
            try:
                await message.channel.send(embed=self._make_embed("🎰 Fichas insuficientes", chip_note or "Você não tem fichas suficientes.", ok=False))
            except Exception:
                pass
            return True

        self._roleta_running_guilds.add(guild.id)
        spinning_emoji = "<:emoji_63:1485041721573249135>"
        win_emoji = "<:emoji_64:1485043651292827788>"
        lose_emoji = "<:emoji_65:1485043671077228786>"
        try:
            await self._set_roleta_reaction(message, spinning_emoji, keep=True)
            success = random.randint(1, 10) == 1

            if success:
                target_middle = [7, 7, 7]
            else:
                while True:
                    target_middle = [random.randint(1, 9) for _ in range(3)]
                    if target_middle != [7, 7, 7]:
                        break

            spin_message, final_columns = await self._animate_roleta_spin(message, target_middle=target_middle)

            if final_columns is None:
                final_columns = [
                    self._build_roleta_column(target_middle[0]),
                    self._build_roleta_column(target_middle[1]),
                    self._build_roleta_column(target_middle[2]),
                ]

            try:
                board = self._render_roleta_board(final_columns)

                if success:
                    chosen_channel = voice_channel
                    try:
                        await self._play_roleta_sfx(guild, chosen_channel)
                    except Exception:
                        pass
                    await asyncio.sleep(0.20)
                    for target in targets:
                        if target.voice and target.voice.channel:
                            try:
                                await target.move_to(None, reason="modo censura roleta")
                            except Exception:
                                pass
                    await self.db.add_user_chips(guild.id, message.author.id, ROLETA_JACKPOT_CHIPS)
                    await self.db.add_user_game_stat(guild.id, message.author.id, "roleta_jackpots", 1)
                    summary = f"{self._CHIP_GAIN_EMOJI} Você ganhou **{ROLETA_JACKPOT_CHIPS} fichas** e os alvos foram tirados da call."
                    if chip_note:
                        summary = f"{chip_note}\n{summary}"
                    embed = self._make_roleta_result_embed(
                        "💥🎰 JACKPOT!!",
                        summary,
                        board,
                        success=True,
                    )
                else:
                    summary = f"{self._CHIP_LOSS_EMOJI} Você perdeu **{ROLETA_COST} fichas**."
                    if chip_note:
                        summary = f"{chip_note}\n{summary}"
                    embed = self._make_roleta_result_embed(
                        "🎰 Não foi dessa vez...",
                        summary,
                        board,
                        success=False,
                    )
            except Exception:
                if success:
                    fallback_title = "💥🎰 JACKPOT!!"
                    fallback_text = f"{self._CHIP_GAIN_EMOJI} Você ganhou **{ROLETA_JACKPOT_CHIPS} fichas** e os alvos foram tirados da call."
                    if chip_note:
                        fallback_text = f"{chip_note}\n{fallback_text}"
                else:
                    fallback_title = "🎰 Não foi dessa vez..."
                    fallback_text = f"{self._CHIP_LOSS_EMOJI} Você perdeu **{ROLETA_COST} fichas**."
                    if chip_note:
                        fallback_text = f"{chip_note}\n{fallback_text}"
                embed = self._make_embed(
                    fallback_title,
                    fallback_text,
                    ok=not success,
                )

            delivered = False
            if spin_message is not None:
                try:
                    await spin_message.edit(embed=embed)
                    delivered = True
                except Exception:
                    pass
            if not delivered:
                try:
                    await message.channel.send(embed=embed)
                except Exception:
                    pass

            await self._clear_roleta_reaction(message, spinning_emoji)
            if success:
                await self._set_roleta_reaction(message, win_emoji, keep=True)
            else:
                await self._set_roleta_reaction(message, lose_emoji, keep=True)
            return True
        finally:
            self._roleta_running_guilds.discard(guild.id)

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
                f"{self._CHIP_LOSS_EMOJI} Entrada travada: **{BUCKSHOT_STAKE} fichas** por jogador\n"
                f"{self._CHIP_EMOJI} Participantes pagos: **{len(participants)}**\n"
                f"{self._CHIP_GAIN_EMOJI} Pote atual: **{payout_total} fichas**"
            )
            color = discord.Color.blurple()
        embed = discord.Embed(title=title, description=description, color=color)
        if not final_text:
            embed.add_field(name="Rodada", value="Entrou e pagou, ficou valendo até o fim.", inline=False)
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
                await interaction.response.send_message(note or "Você não tem fichas suficientes para entrar.", ephemeral=True)
            except Exception:
                pass
            return

        manual_participants = set(session.get("manual_participants", set()) or set())
        manual_participants.add(member.id)
        session["manual_participants"] = manual_participants
        locked_participants.add(member.id)
        session["locked_participants"] = locked_participants

        await self._refresh_buckshot_message(guild.id)

        note = f"{self._CHIP_LOSS_EMOJI} Você entrou na rodada e pagou sua entrada." if not note else f"{note} {self._CHIP_LOSS_EMOJI} Você entrou na rodada e pagou sua entrada."
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
                await chosen.move_to(None, reason="modo censura buckshot")
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
            await self.db.add_user_game_stat(guild.id, chosen.id, "buckshot_eliminations", 1)
            chosen_text = chosen.mention if chosen is not None else "Alguém"
            if winners:
                final_text = (
                    f"<:gunforward:1484655577836683434> **{chosen_text}** foi tirado da call.\n"
                    f"{self._CHIP_GAIN_EMOJI} O pote de **{payout_total} fichas** foi dividido entre os sobreviventes."
                )
            else:
                final_text = (
                    f"<:gunforward:1484655577836683434> **{chosen_text}** foi tirado da call.\n"
                    f"{self._CHIP_LOSS_EMOJI} O pote de **{payout_total} fichas** foi perdido."
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
        if not _BUCKSHOT_WORD_RE.search(content):
            return False

        if GUILD_IDS and guild.id not in GUILD_IDS:
            return True

        if not self.db.anti_mzk_enabled(guild.id):
            return True

        if self._anti_mzk_only_kick_members(guild.id) and not self._is_staff_member(message.author):
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
        if not _ATIRAR_WORD_RE.search(content):
            return False

        if GUILD_IDS and guild.id not in GUILD_IDS:
            return True

        if not self.db.anti_mzk_enabled(guild.id):
            return True

        if self._anti_mzk_only_kick_members(guild.id) and not self._is_staff_member(message.author):
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

    async def _handle_antimzk_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if GUILD_IDS and message.guild.id not in GUILD_IDS:
            return

        if await self._handle_focus_trigger(message):
            return

        if await self._handle_role_toggle_trigger(message):
            return

        if await self._handle_dj_toggle_trigger(message):
            return

        if await self._handle_buckshot_trigger(message):
            return

        if await self._handle_atirar_trigger(message):
            return

        if await self._handle_poker_trigger(message):
            return

        if await self._handle_roleta_trigger(message):
            return

        if not self.db.anti_mzk_enabled(message.guild.id):
            return

        if self._anti_mzk_only_kick_members(message.guild.id) and not self._is_staff_member(message.author):
            return

        if not TRIGGER_WORD and not MUTE_TOGGLE_WORD:
            return

        author_voice = getattr(message.author, "voice", None)
        voice_channel = getattr(author_voice, "channel", None)
        if not isinstance(voice_channel, discord.VoiceChannel):
            return

        content = (message.content or "").lower()
        targets = self._resolve_targets(message.guild, voice_channel)

        if not targets:
            return

        target_ids = {member.id for member in targets}
        author_is_target = message.author.id in target_ids
        author_is_focused_non_staff = self._is_focused_non_staff_member(message.author)

        did_trigger_action = False

        if TRIGGER_WORD and TRIGGER_WORD in content:
            did_trigger_action = True
            trigger_voice_channel = None
            for target in targets:
                target_channel = getattr(getattr(target, "voice", None), "channel", None)
                if isinstance(target_channel, discord.VoiceChannel):
                    trigger_voice_channel = target_channel
                    break

            if trigger_voice_channel is not None:
                try:
                    await self._play_pinto_sfx(message.guild, trigger_voice_channel)
                except Exception:
                    pass
                try:
                    await asyncio.sleep(0.20)
                except Exception:
                    pass

            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        await target.move_to(None, reason="modo censura disconnect")
                    except Exception:
                        pass

        if MUTE_TOGGLE_WORD and MUTE_TOGGLE_WORD in content:
            if author_is_focused_non_staff:
                return
            did_trigger_action = True
            if author_is_target:
                return

            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        new_muted = not bool(target.voice.mute)
                        await target.edit(mute=new_muted, reason="modo censura toggle mute")
                    except Exception:
                        pass

            await self._refresh_targets_suffix_nicknames(message.guild, targets)

        if did_trigger_action:
            await self._react_success_temporarily(message)
