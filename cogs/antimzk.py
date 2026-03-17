import discord
from discord import app_commands
from discord.ext import commands

from config import GUILD_IDS, MUTE_TOGGLE_WORD, OFF_COLOR, ON_COLOR, TRIGGER_WORD
from db import SettingsDB


_GUILD_OBJECTS = [discord.Object(id=guild_id) for guild_id in GUILD_IDS]


def _guild_scoped():
    return app_commands.guilds(*_GUILD_OBJECTS) if _GUILD_OBJECTS else (lambda f: f)


class AntiMzkCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: SettingsDB):
        self.bot = bot
        self.db = db

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

    @_guild_scoped()
    @app_commands.command(name="antimzk", description="Gerencia as roles e modos do anti-mzk")
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
    ])
    @app_commands.checks.has_permissions(kick_members=True)
    async def antimzk(
        self,
        interaction: discord.Interaction,
        action: str,
        role_id: str | None = None,
    ):
        if await self._reject_if_not_allowed_guild(interaction):
            return

        guild = interaction.guild
        chosen = action

        if chosen == "toggle":
            current = self.db.anti_mzk_enabled(guild.id)
            new_value = not current
            await self.db.set_anti_mzk_enabled(guild.id, new_value)

            role_total = len(self.db.get_anti_mzk_role_ids(guild.id))
            embed = self._make_embed(
                "Anti-mzk atualizado",
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
                f"Agora o anti-mzk está **{'limitado a quem tem Expulsar Membros' if new_value else 'liberado para qualquer membro da call disparar'}**",
                ok=True,
            )
            await interaction.response.send_message(embed=embed)
            return

        if chosen == "list":
            role_ids = self.db.get_anti_mzk_role_ids(guild.id)
            if not role_ids:
                embed = self._make_embed(
                    "Sem roles cadastradas",
                    f"Nenhuma role está cadastrada no anti-mzk no momento\n\n"
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
                "Roles do anti-mzk",
                "\n".join(lines)
                + f"\n\nStatus: **{'Ativado' if self.db.anti_mzk_enabled(guild.id) else 'Desativado'}**"
                + f"\nModo só para staff: **{'Ativado' if self._anti_mzk_only_kick_members(guild.id) else 'Desativado'}**",
                ok=True,
            )
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
                    f"A role {role.mention} já está cadastrada no anti-mzk",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed)
                return

            total = len(self.db.get_anti_mzk_role_ids(guild.id))
            embed = self._make_embed(
                "Role adicionada",
                f"✅ Role {role.mention} adicionada ao anti-mzk\n\n"
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
                    f"A role com ID `{parsed_role_id}` não está cadastrada no anti-mzk",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed)
                return

            role_text = role.mention if role else f"`{parsed_role_id}`"
            total = len(self.db.get_anti_mzk_role_ids(guild.id))
            embed = self._make_embed(
                "Role removida",
                f"✅ Role {role_text} removida do anti-mzk\n\n"
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
                "Você precisa da permissão **Expulsar Membros** para usar esse comando",
                ok=False,
            )
        else:
            embed = self._make_embed(
                "Erro no anti-mzk",
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

        if not self.db.anti_mzk_enabled(message.guild.id):
            return

        if self._anti_mzk_only_kick_members(message.guild.id):
            perms = getattr(message.author, "guild_permissions", None)
            if perms is None or not perms.kick_members:
                return

        if not TRIGGER_WORD and not MUTE_TOGGLE_WORD:
            return

        if not isinstance(message.channel, discord.VoiceChannel):
            return

        author_voice = getattr(message.author, "voice", None)
        if not author_voice or not author_voice.channel or author_voice.channel.id != message.channel.id:
            return

        content = (message.content or "").lower()
        targets = self._iter_target_members(message.guild, message.channel)

        if not targets:
            return

        target_ids = {member.id for member in targets}
        author_is_target = message.author.id in target_ids

        if TRIGGER_WORD and TRIGGER_WORD in content:
            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        await target.move_to(None, reason="anti-mzk disconnect")
                    except Exception:
                        pass

        if MUTE_TOGGLE_WORD and MUTE_TOGGLE_WORD in content:
            if author_is_target:
                return

            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        await target.edit(mute=not bool(target.voice.mute), reason="anti-mzk toggle mute")
                    except Exception:
                        pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiMzkCog(bot, bot.settings_db))
