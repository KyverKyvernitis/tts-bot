import discord
from discord import app_commands
from discord.ext import commands as dcommands

from config import GUILD_IDS
from .cog import GincanaCore
from .constants import _guild_scoped


class GincanaCog(GincanaCore, dcommands.Cog):
    @_guild_scoped()
    @app_commands.command(name="gincana", description="Gerencia as roles e modos da gincana")
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
    async def gincana(
        self,
        interaction: discord.Interaction,
        action: str,
        role_id: str | None = None,
    ):
        await self._run_gincana_command(interaction, action, role_id)

    @gincana.error
    async def gincana_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await self._handle_gincana_error(interaction, error)


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


    @dcommands.command(name="daily", aliases=["bonus", "login"])
    async def daily(self, ctx: dcommands.Context):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        if GUILD_IDS and ctx.guild.id not in GUILD_IDS:
            await ctx.reply(embed=self._make_embed("Indisponível aqui", "Esse comando não está habilitado neste servidor", ok=False), mention_author=False)
            return
        claimed, new_balance, bonus, streak = await self.db.claim_daily_bonus(ctx.guild.id, ctx.author.id)
        if not claimed:
            await ctx.reply(embed=self._make_embed("🎁 Daily já resgatado", f"Você já pegou seu bônus de hoje. Streak atual: **{streak}**.", ok=False), mention_author=False)
            return
        await self._grant_weekly_points(ctx.guild.id, ctx.author.id, max(3, bonus // 2))
        embed = discord.Embed(
            title="🎁 Bônus diário resgatado",
            description=(
                f"Você ganhou {self._chip_amount(bonus)}\n"
                f"Streak atual: **{streak}**\n"
                f"Novo saldo: {self._chip_amount(new_balance)}"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text="Volte amanhã para manter a sequência")
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
            await ctx.reply(embed=self._make_embed("Sem permissão", "Você precisa ser staff da gincana para resetar fichas manualmente.", ok=False), mention_author=False)
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
        await self._handle_gincana_message(message)


async def setup(bot: dcommands.Bot):
    await bot.add_cog(GincanaCog(bot, bot.settings_db))
