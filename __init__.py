import discord
from discord import app_commands
from discord.ext import commands as dcommands

from config import GUILD_IDS
from .cog import AntiMzkCore
from .constants import _guild_scoped


class AntiMzkCog(AntiMzkCore, dcommands.Cog):
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
        await self._run_antimzk_command(interaction, action, role_id)

    @antimzk.error
    async def antimzk_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_antimzk_error(interaction, error)


    @dcommands.command(name="ficha", aliases=["fichas"])
    async def ficha(self, ctx: dcommands.Context):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        if GUILD_IDS and ctx.guild.id not in GUILD_IDS:
            await ctx.reply(embed=self._make_embed("Indisponível aqui", "Esse comando não está habilitado neste servidor", ok=False), mention_author=False)
            return
        embed = self._make_chip_balance_embed(ctx.author)
        await ctx.reply(embed=embed, mention_author=False)

    @dcommands.command(name="resetficha", aliases=["resetfichas", "rficha"])
    async def resetficha(self, ctx: dcommands.Context, member: discord.Member | None = None):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        if GUILD_IDS and ctx.guild.id not in GUILD_IDS:
            await ctx.reply(embed=self._make_embed("Indisponível aqui", "Esse comando não está habilitado neste servidor", ok=False), mention_author=False)
            return
        if not isinstance(ctx.author, discord.Member) or not self._is_staff_member(ctx.author):
            await ctx.reply(embed=self._make_embed("Sem permissão", "Você precisa ser staff do modo censura para resetar fichas manualmente.", ok=False), mention_author=False)
            return

        target = member or ctx.author
        new_balance = await self._force_reset_chips(ctx.guild.id, target.id)
        embed = discord.Embed(
            title="♻️ Fichas resetadas",
            description=(
                f"Jogador: {target.mention}\n"
                f"Novo saldo: {self._chip_amount(new_balance)}\n"
                f"Próxima recarga automática: **6 horas** quando faltar saldo."
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text="Reset manual aplicado pela staff")
        await ctx.reply(embed=embed, mention_author=False)

    @dcommands.command(name="rank", aliases=["leaderboard"])
    async def rank(self, ctx: dcommands.Context):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        if GUILD_IDS and ctx.guild.id not in GUILD_IDS:
            await ctx.reply(embed=self._make_embed("Indisponível aqui", "Esse comando não está habilitado neste servidor", ok=False), mention_author=False)
            return
        embed = self._make_chip_leaderboard_embed(ctx.guild, ctx.author)
        await ctx.reply(embed=embed, mention_author=False)

    @dcommands.Cog.listener()
    async def on_message(self, message: discord.Message):
        await self._handle_antimzk_message(message)


async def setup(bot: dcommands.Bot):
    await bot.add_cog(AntiMzkCog(bot, bot.settings_db))
