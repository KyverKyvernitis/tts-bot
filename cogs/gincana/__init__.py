import discord
from discord import app_commands
from discord.ext import commands as dcommands

from .cog import GincanaCore
from .constants import CHIPS_DEFAULT, _guild_scoped


class GincanaCog(dcommands.Cog, GincanaCore):
    def __init__(self, bot: dcommands.Bot, db):
        dcommands.Cog.__init__(self)
        GincanaCore.__init__(self, bot, db)

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
        embed = self._make_chip_balance_embed(ctx.author)
        await ctx.reply(embed=embed, mention_author=False)


    @dcommands.command(name="daily", aliases=["bonus", "login"])
    async def daily(self, ctx: dcommands.Context):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        claimed, new_balance, bonus, streak = await self._claim_daily_bonus_with_activity(ctx.guild.id, ctx.author.id)
        if not claimed:
            await ctx.reply(embed=self._make_embed("🎁 Daily já resgatado", f"Você já pegou seu bônus de hoje. Streak atual: **{streak}**.", ok=False), mention_author=False)
            return
        await self._grant_weekly_points(ctx.guild.id, ctx.author.id, max(3, bonus // 2))
        spin_granted, _spin_state = await self._grant_daily_roleta_spin(ctx.guild.id, ctx.author.id)
        carta_spin_granted, _carta_spin_state = await self._grant_daily_carta_spin(ctx.guild.id, ctx.author.id)
        spin_text = "Você ganhou **+1 giro de roleta**" if spin_granted else "Seu giro extra da roleta já estava disponível"
        carta_spin_text = "Você ganhou **+1 giro de cartas**" if carta_spin_granted else "Seu giro extra de cartas já estava disponível"
        embed = discord.Embed(
            title="🎁 Bônus diário resgatado",
            description=(
                f"Você ganhou {self._chip_amount(bonus)}\n"
                f"{spin_text}\n"
                f"{carta_spin_text}\n"
                f"Streak atual: **{streak}**\n"
                f"Novo saldo: {self._chip_amount(new_balance)}"
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text="Volte amanhã para manter a sequência")
        await ctx.reply(embed=embed, mention_author=False)

    @dcommands.command(name="recarga", aliases=["recarrega"])
    async def recarga(self, ctx: dcommands.Context):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        used, new_balance, note = await self._try_use_chip_recharge(ctx.guild.id, ctx.author.id)
        title = "🔋 Recarga concluída" if used else "🔋 Recarga indisponível"
        description = f"{note}\nSaldo atual: {self._chip_amount(new_balance)}"
        await ctx.reply(embed=self._make_embed(title, description, ok=used), mention_author=False)

    @dcommands.command(name="resetficha", aliases=["resetfichas", "rficha"])
    async def resetficha(self, ctx: dcommands.Context, member: discord.Member | None = None):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
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
                "A recarga manual por **recarga** tem cooldown de **12 horas** e só libera abaixo de **15** fichas."
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
        embed = self._make_chip_leaderboard_embed(ctx.guild, ctx.author)
        await ctx.reply(embed=embed, mention_author=False)

    @dcommands.command(name="resetfichasservidor", aliases=["resetfichastodos", "resetallfichas", "rfichasall"])
    async def resetfichasservidor(self, ctx: dcommands.Context):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        if not isinstance(ctx.author, discord.Member) or not self._is_staff_member(ctx.author):
            await ctx.reply(embed=self._make_embed("Sem permissão", "Você precisa ser staff da gincana para resetar as fichas e o histórico do servidor.", ok=False), mention_author=False)
            return

        user_ids = self._iter_active_chip_user_ids(ctx.guild.id)
        if not user_ids:
            await ctx.reply(
                embed=self._make_embed(
                    "Nada para resetar",
                    "Não há perfis de ficha alterados ou com histórico de jogos para resetar neste servidor.",
                    ok=False,
                ),
                mention_author=False,
            )
            return

        total = 0
        for user_id in user_ids:
            await self._force_full_reset_ficha_profile(ctx.guild.id, user_id, amount=CHIPS_DEFAULT)
            total += 1

        embed = discord.Embed(
            title="♻️ Fichas do servidor resetadas",
            description=(
                f"Perfis afetados: **{total}**\n"
                f"Novo saldo padrão: **{CHIPS_DEFAULT} {self._CHIP_EMOJI}**\n"
                "Resumo, taxa de vitórias, histórico, recarga e login diário foram resetados só para quem já tinha movimentação na gincana."
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text="Reset em massa aplicado pela staff")
        await ctx.reply(embed=embed, mention_author=False)



    @dcommands.command(name="gincanahelp", aliases=["helpgincana", "jogoshelp"])
    async def gincanahelp(self, ctx: dcommands.Context):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        embed = discord.Embed(
            title="🎲 Help da gincana",
            description=(
                "Jogos, fichas e atalhos da gincana em um lugar só.\n\n"
                f"{self._CHIP_EMOJI} **Economia**\n"
                f"• `{ctx.clean_prefix}ficha` — mostra seu saldo e seus destaques\n"
                f"• `{ctx.clean_prefix}daily` — resgata o bônus diário\n"
                "• `recarga` — restaura o saldo quando ele ficar abaixo de 15\n"
                f"• `{ctx.clean_prefix}rank` — ranking dos maiores saldos\n"
                "• `pay @usuário valor` — transfere fichas\n\n"
                "🎮 **Jogos**\n"
                "• `roleta` — aposta rápida com jackpot\n"
                "• `buckshot` — rodada de sobrevivência\n"
                "• `alvo` — disputa de mira\n"
                "• `corrida` — corrida de cavalos\n"
                "• `poker` — mesa de poker\n\n"
                "🕹️ **Como entra**\n"
                "• alguns jogos abrem um lobby com botão\n"
                "• `atirar` fecha o buckshot\n"
                "• use os botões dos lobbies para começar os jogos\n\n"
                "A gincana agora pode ser usada fora de call também."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Use os triggers sozinhos na mensagem para abrir os jogos")
        await ctx.reply(embed=embed, mention_author=False)

    @dcommands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            await self._handle_gincana_message(message)
        except Exception as e:
            print(f"[gincana] erro no on_message: {e!r}")

    @dcommands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        try:
            await self._handle_payment_reaction_event(payload, added=True)
        except Exception as e:
            print(f"[gincana] erro no on_raw_reaction_add: {e!r}")

    @dcommands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        try:
            await self._handle_payment_reaction_event(payload, added=False)
        except Exception as e:
            print(f"[gincana] erro no on_raw_reaction_remove: {e!r}")


async def setup(bot: dcommands.Bot):
    await bot.add_cog(GincanaCog(bot, bot.settings_db))
