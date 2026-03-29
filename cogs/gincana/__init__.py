import random
import time
import discord
from discord import app_commands
from discord.ext import commands as dcommands

from .cog import GincanaCore
from .constants import CHIPS_DEFAULT, CHIPS_INITIAL, _guild_scoped


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
        view = self._make_chip_balance_view(ctx.author)
        await ctx.reply(view=view, mention_author=False)


    @dcommands.command(name="daily", aliases=["bonus", "login"])
    async def daily(self, ctx: dcommands.Context):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        claimed, new_balance, bonus, bonus_bonus, streak = await self._claim_daily_bonus_with_activity(ctx.guild.id, ctx.author.id)
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
                f"Você ganhou {self._bonus_chip_amount(bonus_bonus)} em fichas bônus\nAs fichas bônus serão usadas antes das normais.\n"
                f"{spin_text}\n"
                f"{carta_spin_text}\n"
                f"Streak atual: **{streak}**\n"
                f"Saldo atual: {self._format_compact_chip_balance(ctx.guild.id, ctx.author.id)}"
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
        await ctx.reply(view=self._make_chip_recharge_view(ctx.guild.id, ctx.author.id, used, new_balance, note), mention_author=False)

    @dcommands.command(name="painelficha", aliases=["fichapainel", "adminficha"])
    async def painelficha(self, ctx: dcommands.Context):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        if not isinstance(ctx.author, discord.Member) or not self._is_staff_member(ctx.author):
            await ctx.reply(view=self._make_v2_notice("Sem permissão", ["Esse painel é exclusivo da staff."], ok=False), mention_author=False)
            return
        await ctx.reply(view=self._make_chip_admin_panel_view(ctx.author.id), mention_author=False)

    @dcommands.command(name="rank", aliases=["leaderboard"])
    async def rank(self, ctx: dcommands.Context):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        embed = self._make_chip_leaderboard_embed(ctx.guild, ctx.author)
        await ctx.reply(embed=embed, mention_author=False)


    @dcommands.command(name="poker")
    async def poker_command(self, ctx: dcommands.Context, opponent: discord.Member | None = None):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        if opponent is None:
            await ctx.reply(embed=self._make_embed("🃏 Poker", "Use `poker @usuário` para iniciar uma partida.", ok=False), mention_author=False)
            return
        fake = type("_Msg", (), {})()
        fake.guild = ctx.guild
        fake.author = ctx.author
        fake.channel = ctx.channel
        fake.content = f"poker {opponent.mention}"
        fake.mentions = [opponent]
        handled = await self._handle_poker_trigger(fake)
        if not handled:
            await ctx.reply(embed=self._make_embed("🃏 Poker", "Não foi possível iniciar a partida agora.", ok=False), mention_author=False)

    @dcommands.command(name="truco")
    async def truco_command(self, ctx: dcommands.Context, opponent: discord.Member | None = None):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        if opponent is None:
            await ctx.reply(embed=self._make_embed("🃏 Truco", "Use `truco @usuário` para desafiar alguém.", ok=False), mention_author=False)
            return
        fake = type("_Msg", (), {})()
        fake.guild = ctx.guild
        fake.author = ctx.author
        fake.channel = ctx.channel
        fake.content = f"truco {opponent.mention}"
        fake.mentions = [opponent]
        handled = await self._handle_truco_trigger(fake)
        if not handled:
            await ctx.reply(embed=self._make_embed("🃏 Truco", "Não foi possível iniciar a mão agora.", ok=False), mention_author=False)

    @dcommands.command(name="truco2", aliases=["truco2v2"])
    async def truco2_command(self, ctx: dcommands.Context):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        fake = type("_Msg", (), {})()
        fake.guild = ctx.guild
        fake.author = ctx.author
        fake.channel = ctx.channel
        fake.content = "truco2"
        fake.mentions = []
        handled = await self._handle_truco_trigger(fake)
        if not handled:
            await ctx.reply(embed=self._make_embed("🃏 Truco 2v2", "Não foi possível abrir o lobby agora.", ok=False), mention_author=False)
    async def _run_robbery(self, channel: discord.abc.Messageable, guild: discord.Guild, author: discord.Member, target: discord.Member):
        if target.bot:
            await channel.send(view=self._make_v2_notice("🕵️ Roubo", ["Você tentou roubar um bot. Isso foi longe demais."], ok=False))
            return True
        if target.id == author.id:
            await channel.send(view=self._make_v2_notice("🕵️ Roubo", ["Tentar roubar a si mesmo já é demais."], ok=False))
            return True
        if int(self.db.get_user_chips(guild.id, author.id, default=CHIPS_INITIAL) or 0) < 0:
            await channel.send(view=self._make_v2_notice("🕵️ Roubo", ["Você já está devendo (coitado). Quite a dívida antes de tentar roubar alguém."], ok=False))
            return True
        # cooldown
        adoc = self.db._get_user_doc(guild.id, author.id)
        now = time.time()
        last = float(adoc.get('last_robbery_at', 0) or 0)
        remaining = max(0, int((last + 21600) - now))
        if remaining > 0:
            h = remaining // 3600
            m = (remaining % 3600) // 60
            wait = f"{h}h {m}min" if h else f"{m}min"
            await channel.send(view=self._make_v2_notice("🕵️ Roubo", ["Você já aprontou demais por agora.", f"Tente de novo em **{wait}**."], ok=False))
            return True
        target_chips = int(self.db.get_user_chips(guild.id, target.id, default=CHIPS_INITIAL) or 0)
        target_bonus = int(self.db.get_user_bonus_chips(guild.id, target.id) or 0)
        if target_chips < 0:
            await channel.send(view=self._make_v2_notice("🕵️ Roubo", [f"Você tentou roubar {target.mention}, mas esse usuário já está devendo (coitado)."], ok=False))
            return True
        if target_chips < 20:
            if target_bonus > 0 and target_chips <= 0:
                await channel.send(view=self._make_v2_notice("🕵️ Roubo", [f"Você tentou roubar {target.mention}, mas esse usuário só tem fichas bônus."], ok=False))
            else:
                await channel.send(view=self._make_v2_notice("🕵️ Roubo", [f"Você tentou roubar {target.mention}, mas esse usuário é muito **pobre** pra ser roubado."], ok=False))
            return True
        # consume cooldown on attempt
        adoc['last_robbery_at'] = float(now)
        await self.db._save_user_doc(guild.id, author.id, adoc)
        success = random.random() < 0.40
        if success:
            amount = random.randint(5, min(30, max(5, target_chips)))
            await self._change_user_chips(guild.id, target.id, -amount, mark_activity=True)
            await self._change_user_chips(guild.id, author.id, amount, mark_activity=True)
            flavor = random.choice([
                f"Você roubou {self._chip_text(amount, kind='gain')} de {target.mention}.",
                f"O golpe encaixou. Você levou {self._chip_text(amount, kind='gain')} de {target.mention}.",
                f"Você passou a mão em {self._chip_text(amount, kind='gain')} de {target.mention}."
            ])
            await channel.send(view=self._make_v2_notice("🕵️ Roubo", [flavor], ok=True, accent_color=discord.Color.dark_green()))
            return True
        penalty = 10
        await self._change_user_chips(guild.id, author.id, -penalty, mark_activity=True)
        lines = [
            f"Você tentou roubar {target.mention}, mas foi pego no flagra.",
            f"Você perdeu {self._chip_text(penalty, kind='loss')}."
        ]
        await channel.send(view=self._make_v2_notice("🕵️ Deu ruim", lines, ok=False, accent_color=discord.Color.red()))
        return True

    @dcommands.command(name="roubar", aliases=["rob"])
    async def roubar_command(self, ctx: dcommands.Context, target: discord.Member | None = None):
        if ctx.guild is None:
            await ctx.reply(embed=self._make_embed("Servidor inválido", "Use esse comando dentro de um servidor", ok=False), mention_author=False)
            return
        if target is None:
            await ctx.reply(view=self._make_v2_notice("🕵️ Roubo", [f"Use `{ctx.clean_prefix}roubar @usuário` para tentar a sorte."], ok=False), mention_author=False)
            return
        await self._run_robbery(ctx.channel, ctx.guild, ctx.author, target)

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
                "• `recarga` — entrega 100 fichas bônus quando seu saldo total fica abaixo de 15\n• `pay @usuário valor` — transfere só fichas normais\n• fichas bônus saem primeiro nas apostas\n• ganhos quitam a dívida antes de voltar ao saldo normal\n"
                f"• `{ctx.clean_prefix}rank` — ranking dos maiores saldos\n"
                "\n"
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
