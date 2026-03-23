import re

import discord

from config import GUILD_IDS


class _PaymentConfirmView(discord.ui.View):
    def __init__(self, cog: "GincanaPaymentMixin", session_key: tuple[int, int], *, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.session_key = session_key

    @discord.ui.button(label="✅ Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_payment_confirmation(interaction, self.session_key, action="confirm")

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_payment_confirmation(interaction, self.session_key, action="cancel")

    async def on_timeout(self):
        await self.cog._expire_payment_session(self.session_key)


class GincanaPaymentMixin:
    def _parse_pay_request(self, message: discord.Message):
        raw_content = str(message.content or "").strip()
        if not raw_content.casefold().startswith("pay"):
            return None, None
        mentions = [m for m in getattr(message, "mentions", []) if not getattr(m, "bot", False)]
        if len(mentions) != 1:
            return None, None
        target = mentions[0]

        remainder = raw_content
        remainder = re.sub(r"^\s*pay\b", "", remainder, flags=re.IGNORECASE).strip()
        remainder = re.sub(rf"<@!?{target.id}>", "", remainder).strip()
        amount_match = re.search(r"\b(\d+)\b", remainder)
        inline_amount = None
        if amount_match:
            try:
                inline_amount = int(amount_match.group(1))
            except Exception:
                inline_amount = None
        return target, inline_amount

    def _build_payment_confirm_embed(self, guild: discord.Guild, payer_id: int, session: dict) -> discord.Embed:
        payer = guild.get_member(payer_id)
        target = guild.get_member(int(session.get("target_id") or 0))
        gross_amount = int(session.get("amount") or 0)
        fee = int(session.get("fee") or 0)
        net_amount = int(session.get("net_amount") or 0)

        payer_text = payer.mention if payer else "Pagador"
        target_text = target.mention if target else "Destinatário"
        desc = (
            f"{payer_text} → {target_text}\n\n"
            f"Transferência: {self._chip_amount(gross_amount)}\n"
            f"Imposto: {self._chip_amount(fee)}\n"
            f"Recebe: {self._chip_amount(net_amount)}"
        )
        return discord.Embed(title="💸 Confirmar pagamento", description=desc, color=discord.Color.blurple())

    async def _send_payment_prompt(self, message: discord.Message, target: discord.Member) -> bool:
        guild = message.guild
        if guild is None:
            return True
        self._payment_sessions[(guild.id, message.author.id)] = {
            "target_id": target.id,
            "state": "awaiting_amount",
            "channel_id": message.channel.id,
        }
        await message.channel.send(
            embed=discord.Embed(
                title="💸 Pagamento",
                description=(
                    f"Quanto você quer enviar para {target.mention}?\n"
                    "Envie só o valor no chat ou digite **cancelar**."
                ),
                color=discord.Color.blurple(),
            )
        )
        return True

    async def _start_payment_confirmation(self, message: discord.Message, *, target: discord.Member, amount: int) -> bool:
        guild = message.guild
        if guild is None:
            return True
        if amount <= 0:
            await message.channel.send(embed=self._make_embed("💸 Valor inválido", "O valor precisa ser maior que zero.", ok=False))
            return True
        fee = max(1, int(round(amount * 0.02)))
        net_amount = amount - fee
        if net_amount <= 0:
            await message.channel.send(embed=self._make_embed("💸 Valor inválido", "O valor é muito baixo para essa transferência.", ok=False))
            return True
        total = amount
        ok, _bal, note = await self._ensure_action_chips(guild.id, message.author.id, total)
        if not ok:
            await message.channel.send(embed=self._make_embed("💸 Saldo insuficiente", note or "Você não tem saldo suficiente para esse pagamento.", ok=False))
            self._payment_sessions.pop((guild.id, message.author.id), None)
            return True

        session_key = (guild.id, message.author.id)
        pending = self._payment_sessions.setdefault(session_key, {
            "target_id": target.id,
            "channel_id": message.channel.id,
        })
        pending.update({
            "target_id": target.id,
            "amount": amount,
            "fee": fee,
            "net_amount": net_amount,
            "total": total,
            "state": "awaiting_both_confirm",
            "payer_confirmed": False,
            "target_confirmed": False,
        })
        view = _PaymentConfirmView(self, session_key, timeout=60.0)
        pending["view"] = view
        confirm_message = await message.channel.send(
            embed=self._build_payment_confirm_embed(guild, message.author.id, pending),
            view=view,
        )
        pending["confirm_message"] = confirm_message
        return True

    async def _handle_payment_message(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None or message.author.bot:
            return False
        if GUILD_IDS and guild.id not in GUILD_IDS:
            return False

        target, inline_amount = self._parse_pay_request(message)
        if target is not None:
            if target.id == message.author.id:
                await message.channel.send(embed=self._make_embed("💸 Pagamento inválido", "Você não pode enviar fichas para si mesmo.", ok=False))
                return True
            if target.bot:
                await message.channel.send(embed=self._make_embed("💸 Pagamento inválido", "Bots não podem receber fichas.", ok=False))
                return True
            self._payment_sessions.pop((guild.id, message.author.id), None)
            if inline_amount is not None:
                return await self._start_payment_confirmation(message, target=target, amount=inline_amount)
            return await self._send_payment_prompt(message, target)

        pending = self._payment_sessions.get((guild.id, message.author.id))
        if pending and pending.get("state") == "awaiting_amount":
            content = str(message.content or "").strip()
            if content.casefold() == "cancelar":
                self._payment_sessions.pop((guild.id, message.author.id), None)
                await message.channel.send(embed=self._make_embed("💸 Pagamento cancelado", "A transferência foi cancelada.", ok=False))
                return True
            amount_match = re.search(r"\b(\d+)\b", content)
            if not amount_match:
                await message.channel.send(embed=self._make_embed("💸 Valor inválido", "Envie um valor inteiro positivo ou digite **cancelar**.", ok=False))
                return True
            try:
                amount = int(amount_match.group(1))
            except Exception:
                await message.channel.send(embed=self._make_embed("💸 Valor inválido", "Envie um valor inteiro positivo ou digite **cancelar**.", ok=False))
                return True
            target_member = guild.get_member(int(pending["target_id"]))
            if target_member is None:
                self._payment_sessions.pop((guild.id, message.author.id), None)
                await message.channel.send(embed=self._make_embed("💸 Pagamento cancelado", "O destinatário não está mais disponível.", ok=False))
                return True
            return await self._start_payment_confirmation(message, target=target_member, amount=amount)

        return False

    async def _handle_payment_confirmation(self, interaction: discord.Interaction, session_key: tuple[int, int], *, action: str):
        guild = interaction.guild
        if guild is None:
            return
        session = self._payment_sessions.get(session_key)
        if not session:
            try:
                await interaction.response.send_message("Esse pagamento já foi encerrado.", ephemeral=True)
            except Exception:
                pass
            return

        payer_id = int(session_key[1])
        target_id = int(session.get("target_id") or 0)
        payer = guild.get_member(payer_id)
        target = guild.get_member(target_id)
        if payer is None or target is None:
            self._payment_sessions.pop(session_key, None)
            try:
                await interaction.response.send_message("Pagamento cancelado porque um dos usuários não está mais disponível.", ephemeral=True)
            except Exception:
                pass
            return

        if action == "cancel":
            if interaction.user.id not in {payer_id, target_id}:
                try:
                    await interaction.response.send_message("Só as duas partes podem cancelar esse pagamento.", ephemeral=True)
                except Exception:
                    pass
                return
            self._payment_sessions.pop(session_key, None)
            try:
                await interaction.response.edit_message(
                    embed=self._make_embed("💸 Pagamento cancelado", "A transferência foi cancelada.", ok=False),
                    view=None,
                )
            except Exception:
                pass
            return

        if action != "confirm":
            return

        if interaction.user.id == payer_id:
            if session.get("payer_confirmed"):
                try:
                    await interaction.response.send_message("Você já confirmou o envio.", ephemeral=True)
                except Exception:
                    pass
                return
            session["payer_confirmed"] = True
            ack_text = "Envio confirmado."
        elif interaction.user.id == target_id:
            if session.get("target_confirmed"):
                try:
                    await interaction.response.send_message("Você já confirmou o recebimento.", ephemeral=True)
                except Exception:
                    pass
                return
            session["target_confirmed"] = True
            ack_text = "Recebimento confirmado."
        else:
            try:
                await interaction.response.send_message("Só as duas partes podem confirmar esse pagamento.", ephemeral=True)
            except Exception:
                pass
            return

        if not (session.get("payer_confirmed") and session.get("target_confirmed")):
            try:
                await interaction.response.edit_message(
                    embed=self._build_payment_confirm_embed(guild, payer_id, session),
                    view=session.get("view"),
                )
            except Exception:
                pass
            try:
                await interaction.followup.send(ack_text, ephemeral=True)
            except Exception:
                pass
            return

        amount = int(session.get("amount") or 0)
        fee = int(session.get("fee") or 0)
        net_amount = int(session.get("net_amount") or 0)
        total = int(session.get("total") or 0)
        ok, _balance, note = await self._ensure_action_chips(guild.id, payer.id, total)
        if not ok:
            self._payment_sessions.pop(session_key, None)
            try:
                await interaction.response.edit_message(
                    embed=self._make_embed("💸 Saldo insuficiente", note or "O pagador não tem mais saldo suficiente.", ok=False),
                    view=None,
                )
            except Exception:
                pass
            return

        await self.db.add_user_chips(guild.id, payer.id, -total)
        await self.db.add_user_chips(guild.id, target.id, net_amount)
        await self.db.add_user_game_stat(guild.id, payer.id, "payments_sent", 1)
        await self.db.add_user_game_stat(guild.id, target.id, "payments_received", 1)
        await self.db.add_user_game_stat(guild.id, payer.id, "chips_sent_total", total)
        await self.db.add_user_game_stat(guild.id, target.id, "chips_received_total", net_amount)
        await self._grant_weekly_points(guild.id, payer.id, max(1, total // 10))
        await self._grant_weekly_points(guild.id, target.id, max(1, net_amount // 20))
        self._payment_sessions.pop(session_key, None)
        try:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="✅ Pagamento concluído",
                    description=(
                        f"{payer.mention} enviou {self._chip_amount(amount)} para {target.mention}.\n"
                        f"Taxa: {self._chip_amount(fee)}"
                    ),
                    color=discord.Color.green(),
                ),
                view=None,
            )
        except Exception:
            pass
        try:
            if interaction.user.id == payer_id:
                await interaction.followup.send("Envio confirmado.", ephemeral=True)
            elif interaction.user.id == target_id:
                await interaction.followup.send("Recebimento confirmado.", ephemeral=True)
        except Exception:
            pass

    async def _expire_payment_session(self, session_key: tuple[int, int]):
        session = self._payment_sessions.pop(session_key, None)
        if not session:
            return
        message = session.get("confirm_message")
        if message is not None:
            try:
                await message.edit(embed=self._make_embed("💸 Pagamento expirado", "O pagamento não foi confirmado a tempo.", ok=False), view=None)
            except Exception:
                pass
