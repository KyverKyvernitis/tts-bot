"""Views da cog de formulários.

- FormView: mensagem persistente postada no canal de form. Botão único.
  custom_id usa `:{guild_id}` no fim pra resistir a reboots via
  bot.add_view() sem colisão entre guilds.
- CustomizationPanelView: 4 botões pra editar painel/modal/resposta + apagar
  sessão. Timeout 10min, não-persistente (não sobrevive reboot por design).
- SetupView: 2 ChannelSelect + Confirmar pro setup inicial. Timeout 10min.

Convenções:
- LayoutView+Container pra visual consistente com gincana/color_roles.
- Cada view "owned" guarda staff_id e usa interaction_check pra travar
  cliques de outros usuários.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import discord

from .constants import (
    BUTTON_LABEL_MAX,
    CID_CUST_DELETE_BTN,
    CID_CUST_MODAL_BTN,
    CID_CUST_PANEL_BTN,
    CID_CUST_RESPONSE_BTN,
    CID_SETUP_CONFIRM_BTN,
    CID_SETUP_FORM_SELECT,
    CID_SETUP_RESP_SELECT,
    CID_SUBMIT_PREFIX,
    CUSTOMIZATION_VIEW_TIMEOUT,
    SETUP_VIEW_TIMEOUT,
)

if TYPE_CHECKING:
    from .cog import FormsCog


def _truncate(text, limit: int) -> str:
    text = str(text or "")
    return text[:limit] if len(text) > limit else text


class FormView(discord.ui.LayoutView):
    """View persistente do form. Conteúdo (título, descrição, label do
    botão) é lido do DB na construção — então edições via 'c' aparecem
    quando a mensagem é re-renderizada (via _rerender_active_form no cog).

    O custom_id do botão inclui guild_id pra que múltiplas guilds com
    forms ativos não compartilhem a mesma chave persistente — o discord.py
    registra views persistentes por custom_id como chave global.
    """

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = int(guild_id)

        cfg = cog._get_config(guild_id)
        panel = cfg.get("panel") or {}
        title = str(panel.get("title") or "📝 Formulário")
        description = str(panel.get("description") or "Clique no botão abaixo.")
        button_label = _truncate(panel.get("button_label") or "Preencher formulário", BUTTON_LABEL_MAX)

        button = discord.ui.Button(
            label=button_label,
            emoji="📝",
            style=discord.ButtonStyle.primary,
            custom_id=f"{CID_SUBMIT_PREFIX}:{self.guild_id}",
        )
        button.callback = self._on_submit_click

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(f"# {title}"),
                discord.ui.TextDisplay(description),
                discord.ui.Separator(),
                discord.ui.ActionRow(button),
                accent_color=discord.Color.blurple(),
            )
        )

    async def _on_submit_click(self, interaction: discord.Interaction):
        await self.cog._handle_submit_click(interaction, self.guild_id)


class CustomizationPanelView(discord.ui.LayoutView):
    """Painel de customização aberto via 'c' ou /form_customizar.

    Travado pelo interaction_check pro staff que disparou — outros membros
    veem mensagem ephemeral explicando que não podem clicar.

    O botão "Apagar" e o on_timeout chamam o mesmo método no cog
    (_purge_previous_c_session) que limpa toda mensagem 'c' e painel
    registrados no DB pra essa guild — independente de quem disparou.
    """

    def __init__(
        self,
        cog: "FormsCog",
        *,
        guild_id: int,
        staff_id: int,
    ):
        super().__init__(timeout=float(CUSTOMIZATION_VIEW_TIMEOUT))
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.message: Optional[discord.Message] = None

        panel_btn = discord.ui.Button(
            label="Painel do form",
            emoji="📝",
            style=discord.ButtonStyle.primary,
            custom_id=CID_CUST_PANEL_BTN,
        )
        panel_btn.callback = self._on_panel
        modal_btn = discord.ui.Button(
            label="Modal de submissão",
            emoji="📋",
            style=discord.ButtonStyle.primary,
            custom_id=CID_CUST_MODAL_BTN,
        )
        modal_btn.callback = self._on_modal
        response_btn = discord.ui.Button(
            label="Mensagem de resposta",
            emoji="📨",
            style=discord.ButtonStyle.primary,
            custom_id=CID_CUST_RESPONSE_BTN,
        )
        response_btn.callback = self._on_response
        delete_btn = discord.ui.Button(
            label="Apagar",
            emoji="🗑️",
            style=discord.ButtonStyle.danger,
            custom_id=CID_CUST_DELETE_BTN,
        )
        delete_btn.callback = self._on_delete

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# ⚙️ Customização do formulário"),
                discord.ui.TextDisplay(
                    "Escolha o que editar abaixo. **Apagar** limpa a mensagem "
                    "`c` e o painel ativos da sessão atual."
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(panel_btn, modal_btn, response_btn),
                discord.ui.ActionRow(delete_btn),
                accent_color=discord.Color.gold(),
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            try:
                await interaction.response.send_message(
                    "Só quem abriu esse painel pode usar.", ephemeral=True
                )
            except discord.HTTPException:
                pass
            return False
        return True

    async def on_timeout(self):
        # Timeout aplica a mesma limpeza que o botão "Apagar" — a sessão 'c'
        # atual (mensagem trigger + painel) é apagada.
        await self.cog._purge_previous_c_session(self.guild_id)

    async def _on_panel(self, interaction: discord.Interaction):
        # import lazy pra evitar ciclo (modals importa cog -> views -> modals)
        from .modals import PanelEditModal
        await interaction.response.send_modal(PanelEditModal(self.cog, self.guild_id))

    async def _on_modal(self, interaction: discord.Interaction):
        from .modals import SubmissionModalEditModal
        await interaction.response.send_modal(SubmissionModalEditModal(self.cog, self.guild_id))

    async def _on_response(self, interaction: discord.Interaction):
        from .modals import ResponseEditModal
        await interaction.response.send_modal(ResponseEditModal(self.cog, self.guild_id))

    async def _on_delete(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        await self.cog._purge_previous_c_session(self.guild_id)
        self.stop()


class SetupView(discord.ui.LayoutView):
    """Wizard inicial: 2 ChannelSelect + Confirmar.

    Botão Confirmar começa disabled e habilita quando os 2 canais foram
    escolhidos. Após confirmar, o cog finaliza salvando os IDs no DB e
    posta o form no canal escolhido.
    """

    def __init__(
        self,
        cog: "FormsCog",
        *,
        guild_id: int,
        staff_id: int,
    ):
        super().__init__(timeout=float(SETUP_VIEW_TIMEOUT))
        self.cog = cog
        self.guild_id = int(guild_id)
        self.staff_id = int(staff_id)
        self.message: Optional[discord.Message] = None
        self.selected_form_channel_id: int = 0
        self.selected_resp_channel_id: int = 0

        self.form_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Canal de formulário",
            min_values=1,
            max_values=1,
            custom_id=CID_SETUP_FORM_SELECT,
        )
        self.form_select.callback = self._on_form_select

        self.resp_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Canal de respostas",
            min_values=1,
            max_values=1,
            custom_id=CID_SETUP_RESP_SELECT,
        )
        self.resp_select.callback = self._on_resp_select

        self.confirm_btn = discord.ui.Button(
            label="Confirmar",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=CID_SETUP_CONFIRM_BTN,
            disabled=True,
        )
        self.confirm_btn.callback = self._on_confirm

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# ⚙️ Configuração do formulário"),
                discord.ui.TextDisplay(
                    "Escolha o **canal de formulário** (onde o botão fica visível "
                    "pra todos) e o **canal de respostas** (pra onde as submissões "
                    "vão). Depois clique em Confirmar."
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(self.form_select),
                discord.ui.ActionRow(self.resp_select),
                discord.ui.ActionRow(self.confirm_btn),
                accent_color=discord.Color.blue(),
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.staff_id:
            try:
                await interaction.response.send_message(
                    "Só quem abriu essa configuração pode usar.", ephemeral=True
                )
            except discord.HTTPException:
                pass
            return False
        return True

    def _maybe_enable_confirm(self):
        self.confirm_btn.disabled = not (
            self.selected_form_channel_id and self.selected_resp_channel_id
        )

    async def _on_form_select(self, interaction: discord.Interaction):
        if not self.form_select.values:
            return
        self.selected_form_channel_id = int(self.form_select.values[0].id)
        self._maybe_enable_confirm()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass

    async def _on_resp_select(self, interaction: discord.Interaction):
        if not self.resp_select.values:
            return
        self.selected_resp_channel_id = int(self.resp_select.values[0].id)
        self._maybe_enable_confirm()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass

    async def _on_confirm(self, interaction: discord.Interaction):
        if not (self.selected_form_channel_id and self.selected_resp_channel_id):
            try:
                await interaction.response.send_message(
                    "Selecione os dois canais antes de confirmar.", ephemeral=True
                )
            except discord.HTTPException:
                pass
            return

        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass

        await self.cog._finalize_setup(
            interaction,
            setup_view=self,
            form_channel_id=self.selected_form_channel_id,
            resp_channel_id=self.selected_resp_channel_id,
        )
        self.stop()
