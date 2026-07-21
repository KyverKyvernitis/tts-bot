import logging

import discord

from ..constants import CHIPS_DEFAULT, CHIPS_INITIAL


log = logging.getLogger(__name__)
BOT_OWNER_ID = 394316054433628160


def _is_configured_owner(user_id: int) -> bool:
    try:
        return int(user_id) == BOT_OWNER_ID
    except (TypeError, ValueError):
        return False


def _modal_label(text: str, component: discord.ui.Item, description: str | None = None) -> discord.ui.Label:
    return discord.ui.Label(
        text=str(text)[:45],
        description=(str(description)[:100] if description else None),
        component=component,
    )


class _AdminUserAdjustModal(discord.ui.Modal, title="Ajustar saldo"):
    def __init__(self, cog: "GincanaChipAdminMixin", opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = int(opener_id)

        self.target_select = discord.ui.UserSelect(
            custom_id="games_chip_admin_adjust_user",
            placeholder="Selecione um usuário",
            min_values=1,
            max_values=1,
            required=True,
        )
        self.chips_input = discord.ui.TextInput(
            custom_id="games_chip_admin_adjust_chips",
            placeholder="Deixe vazio para não alterar",
            required=False,
            max_length=16,
        )
        self.bonus_input = discord.ui.TextInput(
            custom_id="games_chip_admin_adjust_bonus",
            placeholder="Deixe vazio para não alterar",
            required=False,
            max_length=16,
        )

        self.add_item(_modal_label("Usuário", self.target_select, "Selecione quem terá o saldo alterado."))
        self.add_item(_modal_label("Fichas normais", self.chips_input, "Aceita números negativos."))
        self.add_item(_modal_label("Fichas bônus", self.bonus_input, "Deixe vazio para não alterar."))

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._chip_admin_validate_interaction(
            interaction,
            opener_id=self.opener_id,
            owner_only=False,
        ):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Use esse painel dentro de um servidor."], ok=False),
                ephemeral=True,
            )
            return

        member = await self.cog._chip_admin_selected_member(guild, self.target_select)
        if member is None:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Usuário inválido", ["Selecione um membro deste servidor."], ok=False),
                ephemeral=True,
            )
            return

        chips_raw = str(self.chips_input.value or "").strip()
        bonus_raw = str(self.bonus_input.value or "").strip()
        if not chips_raw and not bonus_raw:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice(
                    "Nada para alterar",
                    ["Preencha fichas normais, fichas bônus ou ambos."],
                    ok=False,
                ),
                ephemeral=True,
            )
            return
        try:
            chips_val = int(chips_raw) if chips_raw else None
            bonus_val = int(bonus_raw) if bonus_raw else None
        except (TypeError, ValueError):
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Valor inválido", ["Use números inteiros nas fichas."], ok=False),
                ephemeral=True,
            )
            return

        if chips_val is not None:
            await self.cog._set_user_chips_value(guild.id, member.id, chips_val, mark_activity=True)
        if bonus_val is not None:
            await self.cog.db.set_user_bonus_chips(guild.id, member.id, bonus_val)
            await self.cog._mark_chip_activity(guild.id, member.id)

        chips_now = self.cog.db.get_user_chips(guild.id, member.id, default=CHIPS_INITIAL)
        bonus_now = self.cog._get_user_bonus_chips(guild.id, member.id)
        lines = [f"{member.mention} agora tem {self.cog._chip_amount(chips_now)}"]
        if bonus_now > 0:
            lines[0] += f" • {self.cog._bonus_chip_amount(bonus_now)}"
        await interaction.response.send_message(
            view=self.cog._make_v2_notice("Saldo atualizado", lines, ok=True),
            ephemeral=True,
        )


class _AdminUserResetModal(discord.ui.Modal, title="Resetar usuário"):
    def __init__(self, cog: "GincanaChipAdminMixin", opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = int(opener_id)

        self.target_select = discord.ui.UserSelect(
            custom_id="games_chip_admin_reset_user",
            placeholder="Selecione um usuário",
            min_values=1,
            max_values=1,
            required=True,
        )
        self.confirm_checkbox = discord.ui.Checkbox(
            custom_id="games_chip_admin_reset_user_confirm",
            default=False,
        )
        self.add_item(_modal_label("Usuário", self.target_select, "Fichas, bônus e raça serão reiniciados."))
        self.add_item(
            _modal_label(
                "Confirmar reset",
                self.confirm_checkbox,
                "Restaura fichas, bônus e raça do usuário.",
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._chip_admin_validate_interaction(
            interaction,
            opener_id=self.opener_id,
            owner_only=False,
        ):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Use esse painel dentro de um servidor."], ok=False),
                ephemeral=True,
            )
            return

        if not bool(self.confirm_checkbox.value):
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Nada alterado", ["Marque a confirmação para resetar o usuário."], ok=False),
                ephemeral=True,
            )
            return

        member = await self.cog._chip_admin_selected_member(guild, self.target_select)
        if member is None:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Usuário inválido", ["Selecione um membro deste servidor."], ok=False),
                ephemeral=True,
            )
            return

        await self.cog._force_reset_chips(guild.id, member.id)
        lines = [f"{member.mention} voltou para {self.cog._chip_amount(CHIPS_DEFAULT)}."]
        await interaction.response.send_message(
            view=self.cog._make_v2_notice("Usuário resetado", lines, ok=True),
            ephemeral=True,
        )


class _AdminRaceModal(discord.ui.Modal, title="Gerenciar raça"):
    def __init__(self, cog: "GincanaChipAdminMixin", opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = int(opener_id)

        string_select_cls = getattr(discord.ui, "StringSelect", discord.ui.Select)
        self.target_select = discord.ui.UserSelect(
            custom_id="games_chip_admin_race_user",
            placeholder="Selecione um usuário",
            min_values=1,
            max_values=1,
            required=True,
        )
        self.operation_group = discord.ui.RadioGroup(
            custom_id="games_chip_admin_race_operation",
            required=True,
        )
        self.operation_group.add_option(
            label="Definir raça",
            value="set",
            description="Aplica a raça selecionada sem cobrar fichas.",
            default=True,
        )
        self.operation_group.add_option(
            label="Sortear raça",
            value="roll",
            description="Sorteia uma nova raça sem cobrar fichas.",
        )
        self.operation_group.add_option(
            label="Remover raça",
            value="clear",
            description="Remove a raça e limpa o progresso.",
        )
        self.operation_group.add_option(
            label="Ativar habilidade",
            value="activate",
            description="Reativa a raça atual do usuário.",
        )
        self.operation_group.add_option(
            label="Desativar habilidade",
            value="deactivate",
            description="Desativa a raça atual sem removê-la.",
        )

        self.race_select = string_select_cls(
            custom_id="games_chip_admin_race_key",
            placeholder="Selecione a raça para Definir raça",
            min_values=0,
            max_values=1,
            required=False,
        )
        for race_key, info in self.cog._race_catalog().items():
            emoji = str(info.get("emoji") or "").strip()
            name = str(info.get("name") or race_key).strip()
            effects = len(list(info.get("effects") or []))
            self.race_select.add_option(
                label=f"{emoji} {name}".strip(),
                value=str(race_key),
                description=f"{effects} habilidade{'s' if effects != 1 else ''}.",
            )

        self.options_group = discord.ui.CheckboxGroup(
            custom_id="games_chip_admin_race_options",
            required=False,
            min_values=0,
            max_values=3,
        )
        self.options_group.add_option(
            label="Ativar raça definida",
            value="activate_after",
            description="A raça entra ativa após definir ou sortear.",
            default=True,
        )
        self.options_group.add_option(
            label="Reiniciar ao reaplicar a mesma raça",
            value="reset_same",
            description="Limpa cargas e progresso da raça atual.",
            default=True,
        )
        self.options_group.add_option(
            label="Excluir painel de raça antigo",
            value="close_panel",
            description="Remove o painel público salvo do usuário.",
            default=True,
        )

        self.add_item(_modal_label("Usuário", self.target_select, "Selecione o alvo da alteração."))
        self.add_item(_modal_label("Operação", self.operation_group, "Escolha uma única ação administrativa."))
        self.add_item(_modal_label("Raça", self.race_select, "Obrigatória apenas para Definir raça."))
        self.add_item(_modal_label("Opções", self.options_group, "Aplicadas após definir ou sortear."))

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._chip_admin_validate_interaction(
            interaction,
            opener_id=self.opener_id,
            owner_only=False,
        ):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Use esse painel dentro de um servidor."], ok=False),
                ephemeral=True,
            )
            return

        member = await self.cog._chip_admin_selected_member(guild, self.target_select)
        if member is None or member.bot:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Usuário inválido", ["Selecione uma pessoa deste servidor."], ok=False),
                ephemeral=True,
            )
            return

        operation = str(getattr(self.operation_group, "value", "") or "").strip().lower()
        options = set(getattr(self.options_group, "values", None) or [])
        selected_races = list(getattr(self.race_select, "values", None) or [])
        selected_race = str(selected_races[0]).strip().lower() if selected_races else ""
        catalog = self.cog._race_catalog()

        if operation == "set" and selected_race not in catalog:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Raça não selecionada", ["Selecione a raça que será aplicada."], ok=False),
                ephemeral=True,
            )
            return
        if operation not in {"set", "roll", "clear", "activate", "deactivate"}:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Operação inválida", ["Escolha uma operação válida."], ok=False),
                ephemeral=True,
            )
            return

        old_key = self.cog._get_user_race_key(guild.id, member.id)
        old_active = self.cog._is_user_race_active(guild.id, member.id)
        reset_applied = False

        if operation in {"activate", "deactivate"} and not old_key:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Sem raça", [f"{member.mention} ainda não possui uma raça."], ok=False),
                ephemeral=True,
            )
            return

        try:
            async with self.cog._race_progress_lock(guild.id, member.id):
                if operation == "set":
                    reset_applied = old_key != selected_race or "reset_same" in options
                    await self.cog._set_user_race_key(
                        guild.id,
                        member.id,
                        selected_race,
                        reset_state=reset_applied,
                    )
                    await self.cog._set_user_race_active(
                        guild.id,
                        member.id,
                        "activate_after" in options,
                    )
                elif operation == "roll":
                    await self.cog._roll_user_race(guild.id, member.id, exclude_current=bool(old_key))
                    reset_applied = True
                    await self.cog._set_user_race_active(
                        guild.id,
                        member.id,
                        "activate_after" in options,
                    )
                elif operation == "clear":
                    await self.cog._clear_user_race(guild.id, member.id)
                    reset_applied = True
                elif operation == "activate":
                    await self.cog._set_user_race_active(guild.id, member.id, True)
                else:
                    await self.cog._set_user_race_active(guild.id, member.id, False)
        except Exception:
            log.exception(
                "games: falha ao alterar raça via economia guild=%s actor=%s target=%s operation=%s",
                guild.id,
                getattr(interaction.user, "id", 0),
                member.id,
                operation,
            )
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Falha ao alterar", ["A raça não foi alterada."], ok=False),
                ephemeral=True,
            )
            return

        new_key = self.cog._get_user_race_key(guild.id, member.id)
        new_active = self.cog._is_user_race_active(guild.id, member.id)
        old_label = self.cog._chip_admin_race_label(old_key, active=old_active)
        new_label = self.cog._chip_admin_race_label(new_key, active=new_active)
        operation_labels = {
            "set": "Raça definida",
            "roll": "Raça sorteada",
            "clear": "Raça removida",
            "activate": "Raça ativada",
            "deactivate": "Raça desativada",
        }
        lines = [
            f"**Usuário:** {member.mention}",
            f"**Anterior:** {old_label}",
            f"**Atual:** {new_label}",
            "**Custo:** 0 fichas",
        ]
        if reset_applied:
            lines.append("**Progresso:** reiniciado")

        log.info(
            "games: raça alterada via economia guild=%s actor=%s target=%s operation=%s old=%s new=%s active=%s",
            guild.id,
            getattr(interaction.user, "id", 0),
            member.id,
            operation,
            old_key or "none",
            new_key or "none",
            new_active,
        )
        await interaction.response.send_message(
            view=self.cog._make_v2_notice(operation_labels[operation], lines, ok=True),
            ephemeral=True,
        )
        if "close_panel" in options:
            await self.cog._delete_previous_race_panel_message(guild.id, member.id, interaction.channel)


class _AdminServerResetModal(discord.ui.Modal, title="Resetar servidor"):
    def __init__(self, cog: "GincanaChipAdminMixin", opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.opener_id = int(opener_id)
        self.confirm_checkbox = discord.ui.Checkbox(
            custom_id="games_chip_admin_reset_server_confirm",
            default=False,
        )
        self.add_item(
            _modal_label(
                "Confirmar reset do servidor",
                self.confirm_checkbox,
                "Apaga fichas, raças e progresso de todos os perfis ativos.",
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.cog._chip_admin_validate_interaction(
            interaction,
            opener_id=self.opener_id,
            owner_only=False,
        ):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Servidor inválido", ["Use esse painel dentro de um servidor."], ok=False),
                ephemeral=True,
            )
            return
        if not bool(self.confirm_checkbox.value):
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Nada alterado", ["Marque a confirmação para resetar o servidor."], ok=False),
                ephemeral=True,
            )
            return

        user_ids = self.cog._iter_active_chip_user_ids(guild.id)
        if not user_ids:
            await interaction.response.send_message(
                view=self.cog._make_v2_notice("Nada para resetar", ["Não há perfis com movimentação neste servidor."], ok=False),
                ephemeral=True,
            )
            return

        total = 0
        for user_id in user_ids:
            await self.cog._force_full_reset_ficha_profile(guild.id, user_id, amount=CHIPS_DEFAULT)
            total += 1
        lines = [
            f"Perfis afetados: **{total}**",
            f"Novo saldo padrão: {self.cog._chip_amount(CHIPS_DEFAULT)}",
        ]
        await interaction.response.send_message(
            view=self.cog._make_v2_notice("Servidor resetado", lines, ok=True),
            ephemeral=True,
        )


class GincanaChipAdminMixin:
    async def _chip_admin_is_bot_owner(self, user: discord.abc.User | None) -> bool:
        if user is None:
            return False
        user_id = int(getattr(user, "id", 0) or 0)
        if _is_configured_owner(user_id):
            return True

        bot = getattr(self, "bot", None)
        checker = getattr(bot, "is_owner", None)
        if callable(checker):
            try:
                if await checker(user):
                    return True
            except Exception:
                pass

        try:
            if user_id == int(getattr(bot, "owner_id", 0) or 0):
                return True
        except (TypeError, ValueError):
            pass
        try:
            return user_id in {int(value) for value in (getattr(bot, "owner_ids", None) or ())}
        except (TypeError, ValueError):
            return False

    async def _chip_admin_selected_member(
        self,
        guild: discord.Guild,
        select: discord.ui.UserSelect,
    ) -> discord.Member | None:
        values = list(getattr(select, "values", None) or [])
        if len(values) != 1:
            return None
        selected = values[0]
        try:
            user_id = int(getattr(selected, "id", 0) or 0)
        except (TypeError, ValueError):
            return None
        if user_id <= 0:
            return None
        if isinstance(selected, discord.Member) and int(selected.guild.id) == int(guild.id):
            return selected
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except Exception:
            return None

    def _chip_admin_race_label(self, race_key: str, *, active: bool) -> str:
        info = self._get_race_info_by_key(race_key) if race_key else None
        if not info:
            return "Sem raça"
        emoji = str(info.get("emoji") or "").strip()
        name = str(info.get("name") or race_key).strip()
        label = f"{emoji} {name}".strip()
        if not active:
            label += " (desativada)"
        return label

    async def _chip_admin_validate_interaction(
        self,
        interaction: discord.Interaction,
        *,
        opener_id: int,
        owner_only: bool,
        send_response: bool = True,
    ) -> bool:
        user = interaction.user
        if user is None or int(user.id) != int(opener_id):
            if send_response and not interaction.response.is_done():
                await interaction.response.send_message(
                    view=self._make_v2_notice("Sem permissão", ["Esse painel pertence a outra pessoa."], ok=False),
                    ephemeral=True,
                )
            return False

        is_owner = await self._chip_admin_is_bot_owner(user)
        guild = interaction.guild
        allowed = is_owner
        economy_allowed = getattr(self, "_economy_user_allowed", None)
        if not allowed and guild is not None and callable(economy_allowed):
            try:
                allowed = bool(await economy_allowed(guild, user))
            except Exception:
                allowed = False
        if not allowed:
            member = user if isinstance(user, discord.Member) else guild.get_member(user.id) if guild else None
            allowed = member is not None and self._is_staff_member(member)
        if not allowed:
            if send_response and not interaction.response.is_done():
                await interaction.response.send_message(
                    view=self._make_v2_notice("Sem permissão", ["Você não pode usar os controles administrativos."], ok=False),
                    ephemeral=True,
                )
            return False
        if owner_only and not is_owner:
            if send_response and not interaction.response.is_done():
                await interaction.response.send_message(
                    view=self._make_v2_notice("Sem permissão", ["Essa ação é exclusiva do dono do bot."], ok=False),
                    ephemeral=True,
                )
            return False
        return True

    def _make_chip_adjust_modal(self, opener_id: int) -> discord.ui.Modal:
        return _AdminUserAdjustModal(self, opener_id)

    def _make_chip_race_modal(self, opener_id: int) -> discord.ui.Modal:
        return _AdminRaceModal(self, opener_id)

    def _make_chip_user_reset_modal(self, opener_id: int) -> discord.ui.Modal:
        return _AdminUserResetModal(self, opener_id)

    def _make_chip_server_reset_modal(self, opener_id: int) -> discord.ui.Modal:
        return _AdminServerResetModal(self, opener_id)
