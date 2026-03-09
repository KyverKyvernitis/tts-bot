import discord
from discord import app_commands
from discord.ext import commands

from config import GUILD_IDS, MUTE_TOGGLE_WORD, OFF_COLOR, ON_COLOR, TARGET_USER_ID, TRIGGER_WORD
from db import SettingsDB


_GUILD_OBJECTS = [discord.Object(id=guild_id) for guild_id in GUILD_IDS]


def _guild_scoped():
    return app_commands.guilds(*_GUILD_OBJECTS) if _GUILD_OBJECTS else (lambda f: f)


class AntiMzkCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: SettingsDB):
        self.bot = bot
        self.db = db

    def _make_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        return discord.Embed(
            title=title,
            description=description,
            color=discord.Color(ON_COLOR) if ok else discord.Color(OFF_COLOR),
        )

    async def _reject_if_not_allowed_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            embed = self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False)
        elif GUILD_IDS and interaction.guild.id not in GUILD_IDS:
            embed = self._make_embed("Indisponível aqui", "Esse comando não está habilitado neste servidor", ok=False)
        else:
            return False

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return True

    def _iter_target_members(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> list[discord.Member]:
        targets: dict[int, discord.Member] = {}

        role_ids = set(self.db.get_anti_mzk_role_ids(guild.id))
        if role_ids:
            for member in voice_channel.members:
                if member.bot:
                    continue
                member_role_ids = {role.id for role in getattr(member, "roles", [])}
                if member_role_ids & role_ids:
                    targets[member.id] = member

        if TARGET_USER_ID:
            member = guild.get_member(TARGET_USER_ID)
            if member and member.voice and member.voice.channel and member.voice.channel.id == voice_channel.id:
                targets[member.id] = member

        return list(targets.values())

    @_guild_scoped()
    @app_commands.command(name="antimzk", description="Ativa ou desativa a censura anti-mzk (voz)")
    @app_commands.checks.has_permissions(kick_members=True)
    async def antimzk(self, interaction: discord.Interaction):
        if await self._reject_if_not_allowed_guild(interaction):
            return

        gid = interaction.guild.id
        new_value = not self.db.anti_mzk_enabled(gid)
        await self.db.set_anti_mzk_enabled(gid, new_value)

        role_ids = self.db.get_anti_mzk_role_ids(gid)
        extra = f"\nRoles alvo cadastradas: **{len(role_ids)}**" if role_ids else ""

        embed = self._make_embed(
            "Anti-mzk atualizado",
            ("✅ Censura anti-mzk ativada" if new_value else "❌ Censura anti-mzk desativada") + extra,
            ok=new_value,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @_guild_scoped()
    @app_commands.command(name="antimzk_add_role", description="Adiciona uma role alvo por ID para o anti-mzk")
    @app_commands.checks.has_permissions(kick_members=True)
    async def antimzk_add_role(self, interaction: discord.Interaction, role_id: str):
        if await self._reject_if_not_allowed_guild(interaction):
            return

        try:
            parsed_role_id = int(role_id.strip())
        except (TypeError, ValueError):
            await interaction.response.send_message(
                embed=self._make_embed("ID inválido", "Envie um ID de role válido", ok=False),
                ephemeral=True,
            )
            return

        role = interaction.guild.get_role(parsed_role_id)
        if role is None:
            await interaction.response.send_message(
                embed=self._make_embed("Role não encontrada", f"Não encontrei nenhuma role com o ID `{parsed_role_id}` neste servidor", ok=False),
                ephemeral=True,
            )
            return

        added = await self.db.add_anti_mzk_role_id(interaction.guild.id, parsed_role_id)
        if not added:
            await interaction.response.send_message(
                embed=self._make_embed("Role já cadastrada", f"A role {role.mention} já está cadastrada no anti-mzk", ok=False),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=self._make_embed("Role adicionada", f"✅ Role {role.mention} adicionada ao anti-mzk"),
            ephemeral=True,
        )

    @_guild_scoped()
    @app_commands.command(name="antimzk_remove_role", description="Remove uma role alvo por ID do anti-mzk")
    @app_commands.checks.has_permissions(kick_members=True)
    async def antimzk_remove_role(self, interaction: discord.Interaction, role_id: str):
        if await self._reject_if_not_allowed_guild(interaction):
            return

        try:
            parsed_role_id = int(role_id.strip())
        except (TypeError, ValueError):
            await interaction.response.send_message(
                embed=self._make_embed("ID inválido", "Envie um ID de role válido", ok=False),
                ephemeral=True,
            )
            return

        removed = await self.db.remove_anti_mzk_role_id(interaction.guild.id, parsed_role_id)
        if not removed:
            await interaction.response.send_message(
                embed=self._make_embed("Role não cadastrada", f"A role com ID `{parsed_role_id}` não está cadastrada no anti-mzk", ok=False),
                ephemeral=True,
            )
            return

        role = interaction.guild.get_role(parsed_role_id)
        role_text = role.mention if role else f"`{parsed_role_id}`"
        await interaction.response.send_message(
            embed=self._make_embed("Role removida", f"✅ Role {role_text} removida do anti-mzk"),
            ephemeral=True,
        )

    @_guild_scoped()
    @app_commands.command(name="antimzk_list_roles", description="Mostra as roles alvo cadastradas no anti-mzk")
    @app_commands.checks.has_permissions(kick_members=True)
    async def antimzk_list_roles(self, interaction: discord.Interaction):
        if await self._reject_if_not_allowed_guild(interaction):
            return

        role_ids = self.db.get_anti_mzk_role_ids(interaction.guild.id)
        if not role_ids:
            await interaction.response.send_message(
                embed=self._make_embed("Sem roles", "Nenhuma role cadastrada no anti-mzk", ok=False),
                ephemeral=True,
            )
            return

        lines = []
        for role_id in role_ids:
            role = interaction.guild.get_role(role_id)
            lines.append(role.mention if role else f"`{role_id}`")

        embed = self._make_embed("Roles do anti-mzk", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @antimzk.error
    @antimzk_add_role.error
    @antimzk_remove_role.error
    @antimzk_list_roles.error
    async def antimzk_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        print(f"Erro no anti-mzk: {repr(error)}")

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
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as followup_error:
            print(f"Falha ao responder erro do anti-mzk: {repr(followup_error)}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if GUILD_IDS and message.guild.id not in GUILD_IDS:
            return

        if not self.db.anti_mzk_enabled(message.guild.id):
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

        if TRIGGER_WORD and TRIGGER_WORD in content:
            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        await target.move_to(None, reason="anti-mzk disconnect")
                    except Exception:
                        pass

        if MUTE_TOGGLE_WORD and MUTE_TOGGLE_WORD in content:
            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        await target.edit(mute=not bool(target.voice.mute), reason="anti-mzk toggle mute")
                    except Exception:
                        pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiMzkCog(bot, bot.settings_db))
