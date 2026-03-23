import re

import discord

from config import GUILD_IDS


class _PaymentConfirmView(discord.ui.View):
    def __init__(self, cog: "GincanaPaymentMixin", session_key: tuple[int, int], *, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.session_key = session_key

    @discord.ui.button(label="✅ Aceitar pagamento", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_payment_confirmation(interaction, self.session_key, accepted=True)

    @discord.ui.button(label="❌ Recusar", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_payment_confirmation(interaction, self.session_key, accepted=False)

    async def on_timeout(self):
        await self.cog._expire_payment_session(self.session_key)


class GincanaPaymentMixin:
    def _parse_pay_request(self, message: discord.Message):
        raw_content = str(message.content or "").strip()
        if not raw_content.casefold().startswith("pay"):
            return None, None
        if len(message.mentions) != 1:
            return None, None
        target = message.mentions[0]
        content = re.sub(r"<@!?\d+>", "", raw_content).strip()
        if not content.casefold().startswith("pay"):
            return None, None
        remainder = content[3:].strip()
        if not remainder:
            return target, None
        match = re.search(r"(?<!\d)(\d+)(?!\d)", remainder)
        if match:
            try:
                return target, int(match.group(1))
            except Exception:
                return target, None
        return target, None

    async def _start_payment_confirmation(self, message: discord.Message, *, target: discord.Member, amount: int) -> bool:
        guild = message.guild
        if guild is None:
            return True
        if amount <= 0:
            await message.channel.send(embed=self._make_embed("💸 Valor inválido", "O valor precisa ser maior que zero.", ok=False))
            return True
        fee = max(1, int(round(amount * 0.02)))
        total = amount + fee
        ok, _bal, note = await self._ensure_action_chips(guild.id, message.author.id, total)
        if not ok:
            await message.channel.send(embed=self._make_embed("💸 Saldo insuficiente", note or "Você não tem saldo suficiente para esse pagamento.", ok=False))
            self._payment_sessions.pop((guild.id, message.author.id), None)
            return True

        pending = self._payment_sessions.setdefault((guild.id, message.author.id), {
            "target_id": target.id,
            "channel_id": message.channel.id,
        })
        pending["target_id"] = target.id
        pending["amount"] = amount
        pending["fee"] = fee
        pending["total"] = total
        pending["state"] = "awaiting_target_confirm"
        view = _PaymentConfirmView(self, (guild.id, message.author.id), timeout=60.0)
        pending["view"] = view
        desc = (
            f"{target.mention} precisa confirmar o recebimento.\n"
            f"Valor: {self._chip_amount(amount)}\n"
            f"Taxa (2%): {self._chip_amount(fee)}\n"
            f"Total debitado de {message.author.mention}: {self._chip_amount(total)}"
        )
        confirm_message = await message.channel.send(
            embed=discord.Embed(title="💸 Confirmação de pagamento", description=desc, color=discord.Color.blurple()),
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

        pending = self._payment_sessions.get((guild.id, message.author.id))
        if pending and pending.get("state") == "awaiting_amount":
            content = str(message.content or "").strip()
            if content.casefold() == "cancelar":
                self._payment_sessions.pop((guild.id, message.author.id), None)
                await message.channel.send(embed=self._make_embed("💸 Pagamento cancelado", "A transferência foi cancelada antes de informar o valor.", ok=False))
                return True
            amount_match = re.search(r"(?<!\d)(\d+)(?!\d)", content)
            if not amount_match:
                await message.channel.send(embed=self._make_embed("💸 Valor inválido", "Envie um valor inteiro positivo ou digite **cancelar**.", ok=False))
                return True
            try:
                amount = int(amount_match.group(1))
            except Exception:
                await message.channel.send(embed=self._make_embed("💸 Valor inválido", "Envie um valor inteiro positivo ou digite **cancelar**.", ok=False))
                return True
            if amount <= 0:
                await message.channel.send(embed=self._make_embed("💸 Valor inválido", "O valor precisa ser maior que zero.", ok=False))
                return True
            fee = max(1, int(round(amount * 0.02)))
            total = amount + fee
            ok, _bal, note = await self._ensure_action_chips(guild.id, message.author.id, total)
            if not ok:
                await message.channel.send(embed=self._make_embed("💸 Saldo insuficiente", note or "Você não tem saldo suficiente para esse pagamento.", ok=False))
                self._payment_sessions.pop((guild.id, message.author.id), None)
                return True

            pending["amount"] = amount
            pending["fee"] = fee
            pending["total"] = total
            pending["state"] = "awaiting_target_confirm"
            view = _PaymentConfirmView(self, (guild.id, message.author.id), timeout=60.0)
            pending["view"] = view
            target = guild.get_member(int(pending["target_id"]))
            desc = (
                f"{target.mention if target else 'O destinatário'} precisa confirmar o recebimento.\n"
                f"Valor: {self._chip_amount(amount)}\n"
                f"Taxa (2%): {self._chip_amount(fee)}\n"
                f"Total debitado de {message.author.mention}: {self._chip_amount(total)}"
            )
            confirm_message = await message.channel.send(
                embed=discord.Embed(title="💸 Confirmação de pagamento", description=desc, color=discord.Color.blurple()),
                view=view,
            )
            pending["confirm_message"] = confirm_message
            return True

        target, inline_amount = self._parse_pay_request(message)
        if target is None:
            return False

        if target.id == message.author.id:
            await message.channel.send(embed=self._make_embed("💸 Pagamento inválido", "Você não pode enviar fichas para si mesmo.", ok=False))
            return True
        if target.bot:
            await message.channel.send(embed=self._make_embed("💸 Pagamento inválido", "Bots não podem receber fichas.", ok=False))
            return True

        if inline_amount is not None:
            return await self._start_payment_confirmation(message, target=target, amount=inline_amount)

        self._payment_sessions[(guild.id, message.author.id)] = {
            "target_id": target.id,
            "state": "awaiting_amount",
            "channel_id": message.channel.id,
        }
        await message.channel.send(
            embed=discord.Embed(
                title="💸 Transferência iniciada",
                description=f"{message.author.mention}, quanto você quer enviar para {target.mention}?\nEnvie apenas o valor no chat. Digite **cancelar** para desistir.",
                color=discord.Color.blurple(),
            )
        )
        return True

    async def _handle_payment_confirmation(self, interaction: discord.Interaction, session_key: tuple[int, int], *, accepted: bool):
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
        if interaction.user.id != int(session.get("target_id") or 0):
            try:
                await interaction.response.send_message("A confirmação é só para o destinatário desse pagamento.", ephemeral=True)
            except Exception:
                pass
            return

        payer_id = int(session_key[1])
        amount = int(session.get("amount") or 0)
        fee = int(session.get("fee") or 0)
        total = int(session.get("total") or 0)
        payer = guild.get_member(payer_id)
        target = guild.get_member(int(session.get("target_id") or 0))
        if payer is None or target is None:
            self._payment_sessions.pop(session_key, None)
            try:
                await interaction.response.send_message("Pagamento cancelado porque um dos usuários não está mais disponível.", ephemeral=True)
            except Exception:
                pass
            return

        if not accepted:
            self._payment_sessions.pop(session_key, None)
            try:
                await interaction.response.edit_message(
                    embed=self._make_embed("💸 Pagamento recusado", f"{target.mention} recusou receber {self._chip_amount(amount)}.", ok=False),
                    view=None,
                )
            except Exception:
                pass
            return

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
        await self.db.add_user_chips(guild.id, target.id, amount)
        await self.db.add_user_game_stat(guild.id, payer.id, "payments_sent", 1)
        await self.db.add_user_game_stat(guild.id, target.id, "payments_received", 1)
        await self.db.add_user_game_stat(guild.id, payer.id, "chips_sent_total", amount)
        await self.db.add_user_game_stat(guild.id, target.id, "chips_received_total", amount)
        await self._grant_weekly_points(guild.id, payer.id, max(1, amount // 10))
        await self._grant_weekly_points(guild.id, target.id, max(1, amount // 20))
        self._payment_sessions.pop(session_key, None)
        try:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="✅ Pagamento concluído",
                    description=(
                        f"{payer.mention} enviou {self._chip_amount(amount)} para {target.mention}.\n"
                        f"Taxa cobrada: {self._chip_amount(fee)}"
                    ),
                    color=discord.Color.green(),
                ),
                view=None,
            )
        except Exception:
            pass

    async def _expire_payment_session(self, session_key: tuple[int, int]):
        session = self._payment_sessions.pop(session_key, None)
        if not session:
            return
        message = session.get("confirm_message")
        if message is not None:
            try:
                await message.edit(embed=self._make_embed("💸 Pagamento expirado", "O destinatário não confirmou a tempo.", ok=False), view=None)
            except Exception:
                pass
