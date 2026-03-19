import asyncio
import re

import discord
from discord import app_commands
from discord.ext import commands

from config import GUILD_IDS, MUTE_TOGGLE_WORD, OFF_COLOR, ON_COLOR, TRIGGER_WORD
from db import SettingsDB


_GUILD_OBJECTS = [discord.Object(id=guild_id) for guild_id in GUILD_IDS]
_FOCUS_WORD_RE = re.compile(r"(?<!\w)focus(?!\w)", re.IGNORECASE)
_ROLE_TOGGLE_WORD_RE = re.compile(r"(?<!\w)pica(?!\w)", re.IGNORECASE)
_DJ_TOGGLE_WORD_RE = re.compile(r"(?<!\w)dj(?!\w)", re.IGNORECASE)
_RESPONSE_DELETE_AFTER = 20
_ROLE_TOGGLE_DELETE_AFTER = 5


def _guild_scoped():
    return app_commands.guilds(*_GUILD_OBJECTS) if _GUILD_OBJECTS else (lambda f: f)


class AntiMzkCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: SettingsDB):
        self.bot = bot
        self.db = db


    _ANTI_MZK_SUFFIXES = (" [ultra-censurado]", " [censurado]", " [antitts]")

    def _strip_antimzk_suffix(self, name: str) -> str:
        base = str(name or "").rstrip()
        lowered = base.casefold()
        for suffix in self._ANTI_MZK_SUFFIXES:
            if lowered.endswith(suffix.casefold()):
                return base[: -len(suffix)].rstrip()
        return base

    def _target_suffix(self, member: discord.Member, ignored_tts_role: discord.Role | None) -> str:
        is_muted = False
        voice_state = getattr(member, "voice", None)
        if voice_state is not None:
            try:
                is_muted = bool(getattr(voice_state, "mute", False))
            except Exception:
                is_muted = False

        ignores_tts = ignored_tts_role is not None and ignored_tts_role in getattr(member, "roles", [])
        if is_muted and ignores_tts:
            return " [ultra-censurado]"
        if is_muted:
            return " [censurado]"
        if ignores_tts:
            return " [antitts]"
        return ""

    async def _refresh_target_suffix_nickname(self, member: discord.Member, ignored_tts_role: discord.Role | None):
        me = member.guild.me
        if me is None:
            return

        perms = getattr(me.guild_permissions, "manage_nicknames", False)
        if not perms:
            return

        try:
            if member == member.guild.owner:
                return
            if getattr(me, "top_role", None) is not None and getattr(member, "top_role", None) is not None:
                if me.top_role <= member.top_role:
                    return
        except Exception:
            pass

        current_nick = member.nick
        current_display_name = str(getattr(member, "display_name", "") or "").strip()
        current_name = current_nick if current_nick is not None else current_display_name or member.name
        base_name = self._strip_antimzk_suffix(current_name) or self._strip_antimzk_suffix(current_display_name) or member.name
        suffix = self._target_suffix(member, ignored_tts_role)
        desired_full = f"{base_name}{suffix}".strip()

        current_nick_has_managed_suffix = bool(current_nick and self._strip_antimzk_suffix(current_nick) != current_nick)

        if current_nick is None:
            if not suffix:
                return
            if desired_full == current_display_name:
                return
            new_nick = desired_full
        else:
            if not suffix:
                if current_nick_has_managed_suffix:
                    new_nick = None
                elif base_name == member.name:
                    new_nick = None
                else:
                    return
            else:
                new_nick = desired_full

        if isinstance(new_nick, str) and len(new_nick) > 32:
            allowed = max(0, 32 - len(suffix))
            trimmed = base_name[:allowed].rstrip()
            new_nick = f"{trimmed}{suffix}".strip() if suffix else (trimmed or None)
            if current_nick is None and new_nick == member.name:
                return

        if new_nick == current_nick:
            return

        try:
            await member.edit(nick=new_nick, reason="modo censura atualizar sufixo do alvo")
        except Exception:
            pass

    async def _refresh_targets_suffix_nicknames(self, guild: discord.Guild, targets: list[discord.Member]):
        ignored_tts_role = None
        ignored_tts_role_id = 0
        try:
            ignored_tts_role_id = max(0, int(self.db.get_ignored_tts_role_id(guild.id) or 0))
        except Exception:
            ignored_tts_role_id = 0
        if ignored_tts_role_id:
            ignored_tts_role = guild.get_role(ignored_tts_role_id)

        for target in targets:
            await self._refresh_target_suffix_nickname(target, ignored_tts_role)

    def _make_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color(ON_COLOR) if ok else discord.Color(OFF_COLOR),
        )
        return embed

    async def _reject_if_not_allowed_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            embed = self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False)
        elif GUILD_IDS and interaction.guild.id not in GUILD_IDS:
            embed = self._make_embed("Indisponível aqui", "Esse comando não está habilitado neste servidor", ok=False)
        else:
            return False

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)
        return True

    def _anti_mzk_only_kick_members(self, guild_id: int) -> bool:
        guild_cache = getattr(self.db, "guild_cache", {}) or {}
        guild_doc = guild_cache.get(guild_id, {}) or {}
        return bool(guild_doc.get("anti_mzk_only_kick_members", False))

    def _get_staff_role(self, guild: discord.Guild) -> discord.Role | None:
        role_id = 0
        try:
            role_id = max(0, int(self.db.get_anti_mzk_staff_role_id(guild.id) or 0))
        except Exception:
            role_id = 0
        return guild.get_role(role_id) if role_id else None

    def _is_staff_member(self, member: discord.Member) -> bool:
        perms = getattr(member, "guild_permissions", None)
        if perms is not None and perms.kick_members:
            return True

        guild = member.guild
        staff_role = self._get_staff_role(guild)
        return staff_role is not None and staff_role in getattr(member, "roles", [])

    async def _set_anti_mzk_only_kick_members(self, guild_id: int, value: bool):
        if hasattr(self.db, "_get_guild_doc") and hasattr(self.db, "_save_guild_doc"):
            doc = self.db._get_guild_doc(guild_id)
            doc["anti_mzk_only_kick_members"] = bool(value)
            await self.db._save_guild_doc(guild_id, doc)
            return

        guild_cache = getattr(self.db, "guild_cache", None)
        coll = getattr(self.db, "coll", None)
        if guild_cache is not None:
            doc = guild_cache.get(guild_id, {"type": "guild", "guild_id": guild_id})
            doc["anti_mzk_only_kick_members"] = bool(value)
            guild_cache[guild_id] = doc
            if coll is not None:
                await coll.update_one(
                    {"type": "guild", "guild_id": guild_id},
                    {"$set": doc},
                    upsert=True,
                )

    def _iter_target_members(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> list[discord.Member]:
        targets: dict[int, discord.Member] = {}
        role_ids = set(self.db.get_anti_mzk_role_ids(guild.id))

        if not role_ids:
            return []

        for member in voice_channel.members:
            member_role_ids = {role.id for role in getattr(member, "roles", [])}
            if member_role_ids & role_ids:
                targets[member.id] = member

        return list(targets.values())

    def _iter_focused_members(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> list[discord.Member]:
        focus_map = self.db.get_modo_censura_focus_map(guild.id)
        if not focus_map:
            return []

        targets: dict[int, discord.Member] = {}
        for member in voice_channel.members:
            if member.id in focus_map:
                targets[member.id] = member
        return list(targets.values())

    def _resolve_targets(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> list[discord.Member]:
        focused = self._iter_focused_members(guild, voice_channel)
        if focused:
            return focused
        return self._iter_target_members(guild, voice_channel)

    def _format_focus_list(self, guild: discord.Guild) -> str:
        focus_map = self.db.get_modo_censura_focus_map(guild.id)
        if not focus_map:
            return "Nenhum membro está focado no momento."

        lines = []
        for uid in sorted(focus_map):
            member = guild.get_member(uid)
            label = member.mention if member else f"<@{uid}>"
            lines.append(f"• {label}")
        return "\n".join(lines)

    async def _send_focus_feedback(
        self,
        message: discord.Message,
        *,
        added_ids: list[int],
        removed_ids: list[int],
        errored_ids: list[int] | None = None,
        reset: bool = False,
    ):
        guild = message.guild
        assert guild is not None
        errored_ids = errored_ids or []

        def format_count(label: str, count: int, *, ok: bool = True, extra: str | None = None) -> str:
            icon = "✅" if ok else "⚠️"
            user_label = "usuário" if count == 1 else "usuários"
            base = f"{icon} **{count} {user_label}** {label}"
            if extra:
                base += f"\n{extra}"
            return base

        lines: list[str] = []
        if reset:
            title = "🧠 Modo foco resetado"
            description = "A lista de membros focados foi limpa com sucesso."
            color = discord.Color(OFF_COLOR)
            lines.append(description)
        else:
            title = "🧠 Modo foco atualizado"
            color = discord.Color(ON_COLOR)
            if added_ids:
                lines.append(format_count("adicionados à lista com sucesso.", len(added_ids)))
            if removed_ids:
                lines.append(format_count("removidos da lista com sucesso.", len(removed_ids)))
            if errored_ids:
                mention_list = ", ".join(guild.get_member(uid).mention if guild.get_member(uid) else f"<@{uid}>" for uid in errored_ids)
                lines.append(format_count(
                    "não puderam ser adicionados.",
                    len(errored_ids),
                    ok=False,
                    extra=f"Não posso entrar na própria lista de foco: {mention_list}",
                ))
            if not lines:
                lines.append("Nenhuma alteração foi feita na lista de foco.")

        focus_list = self._format_focus_list(guild)
        embed = discord.Embed(title=title, description="\n".join(lines), color=color)
        embed.add_field(name="📋 Lista atual", value=focus_list, inline=False)
        embed.set_footer(text="Nenhum membro focado no momento" if focus_list == "Nenhum membro está focado no momento." else "Lista de foco atualizada")
        await message.channel.send(embed=embed)

    async def _handle_focus_trigger(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None:
            return False

        content = (message.content or "")
        if not _FOCUS_WORD_RE.search(content):
            return False

        if GUILD_IDS and guild.id not in GUILD_IDS:
            return True

        if not isinstance(message.author, discord.Member):
            return True

        if not self._is_staff_member(message.author):
            return True

        mentions: list[discord.Member] = []
        seen: set[int] = set()

        raw_ids = list(getattr(message, "raw_mentions", []) or [])
        if raw_ids:
            for user_id in raw_ids:
                try:
                    uid = int(user_id)
                except (TypeError, ValueError):
                    continue
                if uid in seen:
                    continue
                member = guild.get_member(uid)
                if member is None:
                    try:
                        fetched = await guild.fetch_member(uid)
                    except Exception:
                        fetched = None
                    member = fetched if isinstance(fetched, discord.Member) else None
                if member is None:
                    continue
                mentions.append(member)
                seen.add(uid)
        else:
            for member in message.mentions:
                if member.id not in seen:
                    mentions.append(member)
                    seen.add(member.id)

        if not mentions:
            await self.db.clear_modo_censura_focus_users(guild.id)
            await self._send_focus_feedback(message, added_ids=[], removed_ids=[], errored_ids=[], reset=True)
            return True

        valid_user_ids: list[int] = []
        errored_ids: list[int] = []
        for member in mentions:
            if member.id == self.bot.user.id:
                errored_ids.append(member.id)
                continue
            valid_user_ids.append(member.id)

        added: list[int] = []
        removed: list[int] = []
        if valid_user_ids:
            added, removed, _ = await self.db.toggle_modo_censura_focus_users(
                guild.id,
                valid_user_ids,
            )

        await self._send_focus_feedback(
            message,
            added_ids=added,
            removed_ids=removed,
            errored_ids=errored_ids,
            reset=False,
        )
        return True

    async def _send_role_toggle_feedback(self, message: discord.Message, activated: bool):
        title = "🔇 TTS desativado para os alvos" if activated else "🔊 TTS reativado para os alvos"
        description = (
            "O cargo de ignorar TTS foi aplicado aos alvos atuais do modo censura."
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

        author_voice = getattr(message.author, "voice", None)
        voice_channel = getattr(author_voice, "channel", None)
        if not isinstance(voice_channel, discord.VoiceChannel):
            return True

        targets = self._resolve_targets(guild, voice_channel)
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

        should_activate = any(ignored_tts_role not in getattr(target, "roles", []) for target in targets)

        changed = False
        for target in targets:
            try:
                if should_activate:
                    if ignored_tts_role not in getattr(target, "roles", []):
                        await target.add_roles(ignored_tts_role, reason="modo censura role toggle")
                        changed = True
                else:
                    if ignored_tts_role in getattr(target, "roles", []):
                        await target.remove_roles(ignored_tts_role, reason="modo censura role toggle")
                        changed = True
            except Exception:
                pass

        if changed:
            await self._refresh_targets_suffix_nicknames(guild, targets)
            await self._send_role_toggle_feedback(message, should_activate)
            await self._react_success_temporarily(message)
        return True


    async def _send_dj_toggle_feedback(self, message: discord.Message, activated: bool, affected_count: int, voice_channel: discord.VoiceChannel):
        if activated:
            title = "🎛️ Efeitos sonoros bloqueados"
            description = (
                f"Os membros focados do modo censura ficaram **sem poder usar efeitos sonoros** em {voice_channel.mention}.\n\n"
                f"Afetados agora: **{affected_count}**"
            )
        else:
            title = "🎚️ Efeitos sonoros liberados"
            description = (
                f"Removi o bloqueio de **efeitos sonoros** dos membros focados em {voice_channel.mention}.\n\n"
                f"Afetados agora: **{affected_count}**"
            )
        embed = self._make_embed(title, description, ok=not activated)
        embed.set_footer(text="Trigger: dj • Staffs não são afetados")
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
        targets = [member for member in focus_targets if not self._is_staff_member(member)]

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

        def _is_denied(member: discord.Member) -> bool:
            overwrite = voice_channel.overwrites_for(member)
            return getattr(overwrite, "use_soundboard", None) is False

        should_activate = any(not _is_denied(target) for target in targets)

        changed = 0
        for target in targets:
            try:
                overwrite = voice_channel.overwrites_for(target)
                overwrite.use_soundboard = False if should_activate else None
                await voice_channel.set_permissions(target, overwrite=overwrite, reason="modo censura dj trigger")
                changed += 1
            except Exception:
                pass

        if changed:
            await self._send_dj_toggle_feedback(message, should_activate, changed, voice_channel)
            await self._react_success_temporarily(message)
        return True

    async def _react_success_temporarily(self, message: discord.Message):
        try:
            reaction = await message.add_reaction("✅")
        except Exception:
            return

        async def _cleanup():
            await asyncio.sleep(3)
            try:
                await message.remove_reaction("✅", self.bot.user)
            except Exception:
                pass

        asyncio.create_task(_cleanup())

    @_guild_scoped()
    @app_commands.command(name="modo_censura", description="Gerencia as roles e modos do modo censura")
    @app_commands.describe(
        action="Escolha o que fazer",
        role_id="ID da role para adicionar ou remover",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Adicionar role", value="add"),
        app_commands.Choice(name="Remover role", value="remove"),
        app_commands.Choice(name="Listar roles", value="list"),
        app_commands.Choice(name="Ativar ou desativar", value="toggle"),
        app_commands.Choice(name="Ativar ou desativar só para staff", value="toggle_kick_only"),
        app_commands.Choice(name="Definir cargo staff", value="set_staff_role"),
        app_commands.Choice(name="Remover cargo staff", value="clear_staff_role"),
    ])
    async def antimzk(
        self,
        interaction: discord.Interaction,
        action: str,
        role_id: str | None = None,
    ):
        if await self._reject_if_not_allowed_guild(interaction):
            return

        guild = interaction.guild
        if guild is None:
            return
        if not isinstance(interaction.user, discord.Member) or not self._is_staff_member(interaction.user):
            embed = self._make_embed("Sem permissão", "Você precisa ter o cargo staff do modo censura ou a permissão **Expulsar Membros** para usar esse comando.", ok=False)
            await interaction.response.send_message(embed=embed)
            return
        chosen = action

        if chosen == "toggle":
            current = self.db.anti_mzk_enabled(guild.id)
            new_value = not current
            await self.db.set_anti_mzk_enabled(guild.id, new_value)

            role_total = len(self.db.get_anti_mzk_role_ids(guild.id))
            embed = self._make_embed(
                "Modo censura atualizado",
                f"Status: **{'Ativado' if new_value else 'Desativado'}**\n"
                f"Roles cadastradas: **{role_total}**\n"
                f"Modo só para staff: **{'Ativado' if self._anti_mzk_only_kick_members(guild.id) else 'Desativado'}**",
                ok=new_value,
            )
            await interaction.response.send_message(embed=embed)
            return

        if chosen == "toggle_kick_only":
            current = self._anti_mzk_only_kick_members(guild.id)
            new_value = not current
            await self._set_anti_mzk_only_kick_members(guild.id, new_value)

            embed = self._make_embed(
                "Modo só para staff atualizado",
                f"Agora o modo censura está **{'limitado à staff do modo censura' if new_value else 'liberado para qualquer membro da call disparar'}**.",
                ok=True,
            )
            await interaction.response.send_message(embed=embed)
            return

        if chosen == "list":
            role_ids = self.db.get_anti_mzk_role_ids(guild.id)
            if not role_ids:
                embed = self._make_embed(
                    "Sem roles cadastradas",
                    f"Nenhuma role está cadastrada no modo censura no momento\n\n"
                    f"Status: **{'Ativado' if self.db.anti_mzk_enabled(guild.id) else 'Desativado'}**\n"
                    f"Modo só para staff: **{'Ativado' if self._anti_mzk_only_kick_members(guild.id) else 'Desativado'}**",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed)
                return

            lines = []
            for rid in role_ids:
                role = guild.get_role(rid)
                lines.append(role.mention if role else f"`{rid}`")

            embed = self._make_embed(
                "Roles do modo censura",
                "\n".join(lines)
                + f"\n\nStatus: **{'Ativado' if self.db.anti_mzk_enabled(guild.id) else 'Desativado'}**"
                + f"\nModo só para staff: **{'Ativado' if self._anti_mzk_only_kick_members(guild.id) else 'Desativado'}**",
                ok=True,
            )
            await interaction.response.send_message(embed=embed)
            return

        if chosen == "set_staff_role":
            if not role_id:
                embed = self._make_embed("ID obrigatório", "Você precisa informar o **ID da role** que será usada como cargo staff.", ok=False)
                await interaction.response.send_message(embed=embed)
                return
            try:
                parsed_role_id = int(role_id.strip())
            except (TypeError, ValueError):
                embed = self._make_embed("ID inválido", "Envie um **ID de role válido**.", ok=False)
                await interaction.response.send_message(embed=embed)
                return
            role = guild.get_role(parsed_role_id)
            if role is None:
                embed = self._make_embed("Role não encontrada", f"Não encontrei nenhuma role com o ID `{parsed_role_id}` neste servidor.", ok=False)
                await interaction.response.send_message(embed=embed)
                return
            await self.db.set_anti_mzk_staff_role_id(guild.id, parsed_role_id)
            embed = self._make_embed("Cargo staff atualizado", f"✅ {role.mention} agora é o cargo staff do modo censura.\n\nMembros com esse cargo podem usar os recursos de staff mesmo sem **Expulsar Membros**.")
            await interaction.response.send_message(embed=embed)
            return

        if chosen == "clear_staff_role":
            current_staff = self._get_staff_role(guild)
            await self.db.set_anti_mzk_staff_role_id(guild.id, 0)
            current_text = current_staff.mention if current_staff else "o cargo staff atual"
            embed = self._make_embed("Cargo staff removido", f"✅ Removi {current_text} da configuração de staff do modo censura.")
            await interaction.response.send_message(embed=embed)
            return

        if not role_id:
            embed = self._make_embed(
                "ID obrigatório",
                "Você precisa informar o **ID da role** para essa ação",
                ok=False,
            )
            await interaction.response.send_message(embed=embed)
            return

        try:
            parsed_role_id = int(role_id.strip())
        except (TypeError, ValueError):
            embed = self._make_embed(
                "ID inválido",
                "Envie um **ID de role válido**",
                ok=False,
            )
            await interaction.response.send_message(embed=embed)
            return

        role = guild.get_role(parsed_role_id)

        if chosen == "add":
            if role is None:
                embed = self._make_embed(
                    "Role não encontrada",
                    f"Não encontrei nenhuma role com o ID `{parsed_role_id}` neste servidor",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed)
                return

            added = await self.db.add_anti_mzk_role_id(guild.id, parsed_role_id)
            if not added:
                embed = self._make_embed(
                    "Role já cadastrada",
                    f"A role {role.mention} já está cadastrada no modo censura",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed)
                return

            total = len(self.db.get_anti_mzk_role_ids(guild.id))
            embed = self._make_embed(
                "Role adicionada",
                f"✅ Role {role.mention} adicionada ao modo censura\n\n"
                f"Agora há **{total}** role(s) cadastrada(s)\n"
                f"Status: **{'Ativado' if self.db.anti_mzk_enabled(guild.id) else 'Desativado'}**",
            )
            await interaction.response.send_message(embed=embed)
            return

        if chosen == "remove":
            removed = await self.db.remove_anti_mzk_role_id(guild.id, parsed_role_id)
            if not removed:
                embed = self._make_embed(
                    "Role não cadastrada",
                    f"A role com ID `{parsed_role_id}` não está cadastrada no modo censura",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed)
                return

            role_text = role.mention if role else f"`{parsed_role_id}`"
            total = len(self.db.get_anti_mzk_role_ids(guild.id))
            embed = self._make_embed(
                "Role removida",
                f"✅ Role {role_text} removida do modo censura\n\n"
                f"Roles restantes: **{total}**\n"
                f"Status: **{'Ativado' if self.db.anti_mzk_enabled(guild.id) else 'Desativado'}**",
                ok=True,
            )
            await interaction.response.send_message(embed=embed)
            return

    @antimzk.error
    async def antimzk_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            embed = self._make_embed(
                "Sem permissão",
                "Você precisa ter o cargo staff do modo censura ou a permissão **Expulsar Membros** para usar esse comando.",
                ok=False,
            )
        else:
            embed = self._make_embed(
                "Erro no modo censura",
                "Ocorreu um erro ao executar esse comando",
                ok=False,
            )

        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed)
            else:
                await interaction.response.send_message(embed=embed)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
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
        focus_map = self.db.get_modo_censura_focus_map(message.guild.id)
        author_is_focused = bool(focus_map and message.author.id in focus_map)
        author_can_use_triggers = (not author_is_focused) or self._is_staff_member(message.author)

        did_trigger_action = False

        if TRIGGER_WORD and TRIGGER_WORD in content:
            if not author_can_use_triggers:
                return
            did_trigger_action = True
            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        await target.move_to(None, reason="modo censura disconnect")
                    except Exception:
                        pass

        if MUTE_TOGGLE_WORD and MUTE_TOGGLE_WORD in content:
            if not author_can_use_triggers:
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


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiMzkCog(bot, bot.settings_db))
