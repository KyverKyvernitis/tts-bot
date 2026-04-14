import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands as dcommands

from .cog import GincanaCore
from .constants import (
    CHIPS_DEFAULT,
    CHIPS_INITIAL,
    CHIPS_MENDIGAR_COOLDOWN_SECONDS,
    CHIPS_MENDIGAR_TIMEOUT_SECONDS,
    CHIPS_PAY_MIN_BALANCE,
    CHIPS_PAY_RECEIVER_MAX_BALANCE,
    _guild_scoped,
)


class _MendigarRequestView(discord.ui.LayoutView):
    def __init__(
        self,
        cog: "GincanaCog",
        *,
        guild_id: int,
        author_id: int,
        author_mention: str,
        amount: int,
        target_id: int | None,
        target_mention: str | None,
        timeout: float = CHIPS_MENDIGAR_TIMEOUT_SECONDS,
    ):
        super().__init__(timeout=float(timeout))
        self.cog = cog
        self.guild_id = int(guild_id)
        self.author_id = int(author_id)
        self.author_mention = str(author_mention)
        self.amount = int(amount)
        self.target_id = int(target_id) if target_id else None
        self.target_mention = str(target_mention) if target_mention else None
        self.fulfilled = False
        self.message: discord.Message | None = None

        donate_button = discord.ui.Button(
            label=f"Dar {self.amount} fichas",
            emoji="💸",
            style=discord.ButtonStyle.success,
        )
        donate_button.callback = self._handle_donate
        row = discord.ui.ActionRow(donate_button)

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("\n".join(self._build_header_lines())),
                discord.ui.Separator(),
                discord.ui.TextDisplay("\n".join(self._build_info_lines())),
                row,
                accent_color=discord.Color.orange(),
            )
        )

    def _build_header_lines(self) -> list[str]:
        if self.target_mention:
            return [
                "# 🥺 Mendigar",
                f"{self.target_mention}, {self.author_mention} está mendigando {self.cog._chip_amount(self.amount)}, deseja dar esmola pra esse coitado?",
            ]
        return [
            "# 🥺 Mendigar",
            f"Este pobre usuário necessitado está pedindo uma esmola de {self.cog._chip_amount(self.amount)}.",
        ]

    def _build_info_lines(self) -> list[str]:
        lines = [f"**Pedinte:** {self.author_mention}"]
        if self.target_mention:
            lines.append(f"**Convocado para ajudar:** {self.target_mention}")
            lines.append("Somente a pessoa marcada pode clicar no botão abaixo.")
        else:
            lines.append("Qualquer alma bondosa com fichas normais suficientes pode ajudar no botão abaixo.")
        lines.append(f"A esmola expira em **{int(CHIPS_MENDIGAR_TIMEOUT_SECONDS // 60)} minutos**.")
        return lines

    async def _handle_donate(self, interaction: discord.Interaction):
        if self.fulfilled:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("💸 Esmola encerrada", ["Esse pedido já foi atendido."], ok=False),
                ephemeral=True,
            )
            return
        if interaction.guild is None or int(interaction.guild.id) != self.guild_id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("💸 Esmola", ["Esse pedido só pode ser usado no servidor original."], ok=False),
                ephemeral=True,
            )
            return
        donor = interaction.user if isinstance(interaction.user, discord.Member) else interaction.guild.get_member(interaction.user.id)
        recipient = interaction.guild.get_member(self.author_id)
        if donor is None or recipient is None:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("💸 Esmola", ["Não consegui localizar todo mundo para concluir essa esmola."], ok=False),
                ephemeral=True,
            )
            return
        if donor.bot:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("💸 Esmola", ["Bots não podem dar esmola."], ok=False),
                ephemeral=True,
            )
            return
        if donor.id == recipient.id:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("💸 Esmola", ["Você não pode dar esmola para si mesmo."], ok=False),
                ephemeral=True,
            )
            return
        if self.target_id and int(donor.id) != int(self.target_id):
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("💸 Esmola reservada", ["Essa esmola foi direcionada para outra pessoa decidir."], ok=False),
                ephemeral=True,
            )
            return

        await self.cog._maybe_execute_due_chip_season_reset(self.guild_id)
        donor_chips = int(self.cog.db.get_user_chips(self.guild_id, donor.id, default=CHIPS_INITIAL) or 0)
        recipient_chips = int(self.cog.db.get_user_chips(self.guild_id, recipient.id, default=CHIPS_INITIAL) or 0)
        if donor_chips < CHIPS_PAY_MIN_BALANCE:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice(
                    "💸 Esmola bloqueada",
                    [f"Você precisa ter pelo menos **{CHIPS_PAY_MIN_BALANCE} fichas normais** para dar esmola."],
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        if donor_chips < self.amount:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice(
                    "💸 Esmola bloqueada",
                    ["Você não tem fichas normais suficientes para cobrir essa esmola.", "Fichas bônus não entram nessa conta."],
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        if recipient_chips + self.amount > CHIPS_PAY_RECEIVER_MAX_BALANCE:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice(
                    "💸 Esmola bloqueada",
                    [f"{recipient.mention} passaria de **{CHIPS_PAY_RECEIVER_MAX_BALANCE} fichas normais** com essa esmola."],
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        await self.cog._transfer_user_chips(self.guild_id, donor.id, recipient.id, total=self.amount, net_amount=self.amount)
        self.fulfilled = True
        self.stop()
        result = discord.ui.LayoutView(timeout=None)
        result.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "\n".join(
                        [
                            "# 💸 Esmola entregue",
                            f"{donor.mention} deu {self.cog._chip_amount(self.amount)} para {recipient.mention}.",
                            f"Saldo de {recipient.mention}: {self.cog._format_compact_chip_balance(self.guild_id, recipient.id)}",
                        ]
                    )
                ),
                accent_color=discord.Color.dark_green(),
            )
        )
        await interaction.response.edit_message(view=result)

    async def on_timeout(self):
        if self.fulfilled or self.message is None:
            return
        try:
            await self.message.edit(
                view=self.cog._make_v2_notice(
                    "💸 Mendigar",
                    ["O pedido de esmola esfriou e expirou sem ninguém ajudar."],
                    ok=False,
                    accent_color=discord.Color.red(),
                )
            )
        except Exception:
            pass


class GincanaCog(dcommands.Cog, GincanaCore):
    def __init__(self, bot: dcommands.Bot, db):
        dcommands.Cog.__init__(self)
        GincanaCore.__init__(self, bot, db)

    def _format_wait_compact(self, seconds: float) -> str:
        remaining = max(0, int(seconds))
        hours = remaining // 3600
        minutes = (remaining % 3600) // 60
        if hours > 0:
            return f"{hours}h {minutes:02d}min"
        return f"{minutes}min"

    async def _start_mendigar_request(self, message: discord.Message, *, amount: int, target: discord.Member | None) -> bool:
        guild = message.guild
        if guild is None:
            return True
        await self._maybe_execute_due_chip_season_reset(guild.id)
        if amount <= 0:
            await message.channel.send(view=self._make_v2_notice("🥺 Mendigar", ["Use um valor maior que zero."], ok=False))
            return True
        current_balance = int(self.db.get_user_chips(guild.id, message.author.id, default=CHIPS_INITIAL) or 0)
        if current_balance + amount > CHIPS_PAY_RECEIVER_MAX_BALANCE:
            await message.channel.send(
                view=self._make_v2_notice(
                    "🥺 Mendigar",
                    [f"Esse pedido deixaria você acima de **{CHIPS_PAY_RECEIVER_MAX_BALANCE} fichas normais**. Ajuste o valor e tente de novo."],
                    ok=False,
                )
            )
            return True

        user_doc = self.db._get_user_doc(guild.id, message.author.id)
        now = time.time()
        last_used = float(user_doc.get("last_mendigar_at", 0) or 0)
        remaining = (last_used + CHIPS_MENDIGAR_COOLDOWN_SECONDS) - now
        if remaining > 0:
            await message.channel.send(
                view=self._make_v2_notice(
                    "🥺 Mendigar",
                    ["Você já pediu esmola demais por agora.", f"Tente novamente em **{self._format_wait_compact(remaining)}**."],
                    ok=False,
                )
            )
            return True

        if target is not None:
            if target.bot:
                await message.channel.send(view=self._make_v2_notice("🥺 Mendigar", ["Bots não entram nesse esquema de esmola."], ok=False))
                return True
            if target.id == message.author.id:
                await message.channel.send(view=self._make_v2_notice("🥺 Mendigar", ["Pedir esmola para si mesmo já é sacanagem demais."], ok=False))
                return True

        user_doc["last_mendigar_at"] = float(now)
        await self.db._save_user_doc(guild.id, message.author.id, user_doc)

        view = _MendigarRequestView(
            self,
            guild_id=guild.id,
            author_id=message.author.id,
            author_mention=message.author.mention,
            amount=amount,
            target_id=getattr(target, "id", None),
            target_mention=getattr(target, "mention", None),
        )
        sent = await message.channel.send(view=view)
        view.message = sent
        return True

    async def _handle_mendigar_trigger(self, message: discord.Message) -> bool:
        content = str(message.content or "").strip()
        if content.casefold().startswith("_"):
            return False
        if not re.match(r"^\s*mendigar\b", content, re.IGNORECASE):
            return False
        if message.guild is None:
            return True

        match = re.fullmatch(r"\s*mendigar\s+(\d+)(?:\s+<@!?(\d+)>)?\s*", content, re.IGNORECASE)
        if match is None:
            await message.channel.send(
                view=self._make_v2_notice(
                    "🥺 Mendigar",
                    [
                        "Use `mendigar 40` para pedir uma esmola geral.",
                        "Use `mendigar 40 @usuário` para pedir esmola a alguém específico.",
                    ],
                    ok=False,
                )
            )
            return True

        amount = int(match.group(1) or 0)
        target_id_raw = match.group(2)
        target: discord.Member | None = None
        if target_id_raw:
            target_id = int(target_id_raw)
            mentioned_members = [member for member in getattr(message, "mentions", []) if isinstance(member, discord.Member)]
            target = next((member for member in mentioned_members if int(member.id) == target_id), None)
            if target is None:
                target = message.guild.get_member(target_id)
            if target is None:
                await message.channel.send(view=self._make_v2_notice("🥺 Mendigar", ["Não encontrei essa pessoa no servidor para pedir esmola."], ok=False))
                return True
        elif getattr(message, "mentions", None):
            await message.channel.send(view=self._make_v2_notice("🥺 Mendigar", ["Use no formato `mendigar valor @usuário`."], ok=False))
            return True

        return await self._start_mendigar_request(message, amount=amount, target=target)

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
        embed = await self._make_chip_leaderboard_embed_async(ctx.guild, ctx.author)
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
        adoc = self.db._get_user_doc(guild.id, author.id)
        now = time.time()
        last = float(adoc.get('last_robbery_at', 0) or 0)
        remaining = max(0, int((last + 21600) - now))
        if remaining > 0:
            wait = self._format_wait_compact(remaining)
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
                "• `recarga` — entrega 100 fichas bônus quando seu saldo total fica abaixo de 15\n"
                "• `pay @usuário valor` — transfere só fichas normais\n"
                "• `mendigar valor` — pede uma esmola geral\n"
                "• `mendigar valor @usuário` — pede esmola para alguém específico\n"
                "• fichas bônus saem primeiro nas apostas\n"
                "• ganhos quitam a dívida antes de voltar ao saldo normal\n"
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
