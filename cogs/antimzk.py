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

        if not role_ids:
            return []

        for member in voice_channel.members:
            if member.bot:
                continue

            member_role_ids = {role.id for role in getattr(member, "roles", [])}
            if member_role_ids & role_ids:
                targets[member.id] = member

        return list(targets.values())

    @_guild_scoped()
    @app_commands.command(name="antimzk", description="Gerencia as roles alvo do anti-mzk")
    @app_commands.describe(
        action="Escolha o que fazer",
        role_id="ID da role para adicionar ou remover",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Adicionar role", value="add"),
        app_commands.Choice(name="Remover role", value="remove"),
        app_commands.Choice(name="Listar roles", value="list"),
    ])
    @app_commands.checks.has_permissions(kick_members=True)
    async def antimzk(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        role_id: str | None = None,
    ):
        if await self._reject_if_not_allowed_guild(interaction):
            return

        guild = interaction.guild
        chosen = action.value

        if chosen == "list":
            role_ids = self.db.get_anti_mzk_role_ids(guild.id)
            if not role_ids:
                embed = self._make_embed(
                    "Sem roles cadastradas",
                    "Nenhuma role está cadastrada no anti-mzk no momento",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            lines = []
            for rid in role_ids:
                role = guild.get_role(rid)
                lines.append(role.mention if role else f"`{rid}`")

            embed = self._make_embed(
                "Roles do anti-mzk",
                "\n".join(lines) + f"\n\nStatus: **{'Ativado' if len(role_ids) > 0 else 'Desativado'}**",
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if not role_id:
            embed = self._make_embed(
                "ID obrigatório",
                "Você precisa informar o **ID da role** para essa ação",
                ok=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        try:
            parsed_role_id = int(role_id.strip())
        except (TypeError, ValueError):
            embed = self._make_embed(
                "ID inválido",
                "Envie um **ID de role válido**",
                ok=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        role = guild.get_role(parsed_role_id)

        if chosen == "add":
            if role is None:
                embed = self._make_embed(
                    "Role não encontrada",
                    f"Não encontrei nenhuma role com o ID `{parsed_role_id}` neste servidor",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            added = await self.db.add_anti_mzk_role_id(guild.id, parsed_role_id)
            if not added:
                embed = self._make_embed(
                    "Role já cadastrada",
                    f"A role {role.mention} já está cadastrada no anti-mzk",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            total = len(self.db.get_anti_mzk_role_ids(guild.id))
            embed = self._make_embed(
                "Role adicionada",
                f"✅ Role {role.mention} adicionada ao anti-mzk\n\nAgora há **{total}** role(s) cadastrada(s)",
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if chosen == "remove":
            removed = await self.db.remove_anti_mzk_role_id(guild.id, parsed_role_id)
            if not removed:
                embed = self._make_embed(
                    "Role não cadastrada",
                    f"A role com ID `{parsed_role_id}` não está cadastrada no anti-mzk",
                    ok=False,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            role_text = role.mention if role else f"`{parsed_role_id}`"
            total = len(self.db.get_anti_mzk_role_ids(guild.id))
            status = "Ativado" if total > 0 else "Desativado"

            embed = self._make_embed(
                "Role removida",
                f"✅ Role {role_text} removida do anti-mzk\n\nRoles restantes: **{total}**\nStatus: **{status}**",
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

    @antimzk.error
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

        role_ids = self.db.get_anti_mzk_role_ids(message.guild.id)
        anti_mzk_active = len(role_ids) > 0

        if not anti_mzk_active:
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

        member_debug = [
            {
                "id": member.id,
                "roles": [role.id for role in getattr(member, "roles", [])],
            }
            for member in message.channel.members
            if not member.bot
        ]

        print(
            f"[antimzk] guild={message.guild.id} "
            f"role_ids={self.db.get_anti_mzk_role_ids(message.guild.id)} "
            f"members={member_debug} "
            f"targets={[m.id for m in targets]}"
        )

        if not targets:
            return

        if TRIGGER_WORD and TRIGGER_WORD in content:
            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        print(f"[antimzk] disconnect target={target.id}")
                        await target.move_to(None, reason="anti-mzk disconnect")
                    except Exception as e:
                        print(f"[antimzk] failed disconnect target={target.id} error={e!r}")

        if MUTE_TOGGLE_WORD and MUTE_TOGGLE_WORD in content:
            for target in targets:
                if target.voice and target.voice.channel:
                    try:
                        print(f"[antimzk] toggle mute target={target.id}")
                        await target.edit(mute=not bool(target.voice.mute), reason="anti-mzk toggle mute")
                    except Exception as e:
                        print(f"[antimzk] failed mute target={target.id} error={e!r}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiMzkCog(bot, bot.settings_db))
