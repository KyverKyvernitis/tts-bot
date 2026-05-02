"""Modais da cog de formulários.

- FormSubmissionModal: aberto pelo botão do form. 2 fields — idade/pronome
  e descrição. Os labels/placeholders dele vêm do DB e são editáveis pelo staff.
- PanelEditModal: edita título/descrição/label do botão do form.
- SubmissionModalEditModal: edita título do modal + labels/placeholders dos
  2 fields que aparecem pro usuário.
- ResponseEditModal: edita o template da mensagem postada no canal de respostas.

Os 3 modais de edição abrem com os valores atuais no `default=` pra staff
editar incrementalmente em vez de redigitar tudo do zero.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from .constants import (
    AGE_PRONOUN_MAX,
    BUTTON_LABEL_MAX,
    DESCRIPTION_MAX,
    MODAL_TITLE_MAX,
    PANEL_DESCRIPTION_MAX,
    PANEL_TITLE_MAX,
    RESPONSE_BODY_MAX,
    RESPONSE_HEADER_MAX,
    TEXT_INPUT_LABEL_MAX,
    TEXT_INPUT_PLACEHOLDER_MAX,
)

if TYPE_CHECKING:
    from .cog import FormsCog


def _truncate(text, limit: int) -> str:
    """Trunca texto pro limite do Discord — usado em valores que vêm do DB
    e podem ter sido salvos antes de uma redução de limite."""
    text = str(text or "")
    return text[:limit] if len(text) > limit else text


class FormSubmissionModal(discord.ui.Modal):
    """Modal mostrado ao usuário ao clicar no botão do form.

    Os labels/placeholders são carregados do DB (config['modal']) na hora
    da construção — então edições via SubmissionModalEditModal aparecem
    no próximo clique sem precisar repostar a mensagem do form.
    """

    def __init__(self, cog: "FormsCog", guild_id: int):
        cfg = cog._get_config(guild_id)
        modal_cfg = cfg.get("modal") or {}
        title = _truncate(modal_cfg.get("title") or "Preencher formulário", MODAL_TITLE_MAX)
        super().__init__(title=title)
        self.cog = cog
        self.guild_id = int(guild_id)

        self.age_input = discord.ui.TextInput(
            label=_truncate(modal_cfg.get("age_label") or "Idade e pronome", TEXT_INPUT_LABEL_MAX),
            placeholder=_truncate(modal_cfg.get("age_placeholder") or "18, ele/dele", TEXT_INPUT_PLACEHOLDER_MAX),
            style=discord.TextStyle.short,
            max_length=AGE_PRONOUN_MAX,
            required=True,
        )
        self.desc_input = discord.ui.TextInput(
            label=_truncate(modal_cfg.get("desc_label") or "Descrição", TEXT_INPUT_LABEL_MAX),
            placeholder=_truncate(modal_cfg.get("desc_placeholder") or "Conta um pouco sobre você...", TEXT_INPUT_PLACEHOLDER_MAX),
            style=discord.TextStyle.paragraph,
            max_length=DESCRIPTION_MAX,
            required=True,
        )
        self.add_item(self.age_input)
        self.add_item(self.desc_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._handle_submission(
            interaction,
            age_pronoun=str(self.age_input.value or "").strip(),
            description=str(self.desc_input.value or "").strip(),
        )


class PanelEditModal(discord.ui.Modal):
    """Edita os textos do painel do form: título, descrição, label do botão.

    Após submit, o cog re-renderiza a mensagem ativa do form (se existir)
    pra refletir as mudanças imediatamente — staff não precisa repostar.
    """

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(title="Editar painel do form")
        self.cog = cog
        self.guild_id = int(guild_id)

        cfg = cog._get_config(guild_id)
        panel = cfg.get("panel") or {}

        self.title_input = discord.ui.TextInput(
            label="Título",
            default=_truncate(panel.get("title") or "", PANEL_TITLE_MAX),
            style=discord.TextStyle.short,
            max_length=PANEL_TITLE_MAX,
            required=True,
        )
        self.desc_input = discord.ui.TextInput(
            label="Descrição",
            default=_truncate(panel.get("description") or "", PANEL_DESCRIPTION_MAX),
            style=discord.TextStyle.paragraph,
            max_length=PANEL_DESCRIPTION_MAX,
            required=True,
        )
        self.button_input = discord.ui.TextInput(
            label="Label do botão",
            default=_truncate(panel.get("button_label") or "", BUTTON_LABEL_MAX),
            style=discord.TextStyle.short,
            max_length=BUTTON_LABEL_MAX,
            required=True,
        )

        self.add_item(self.title_input)
        self.add_item(self.desc_input)
        self.add_item(self.button_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._update_panel_config(
            interaction,
            title=str(self.title_input.value or "").strip(),
            description=str(self.desc_input.value or "").strip(),
            button_label=str(self.button_input.value or "").strip(),
        )


class SubmissionModalEditModal(discord.ui.Modal):
    """Edita o modal que aparece pros usuários ao clicar no botão do form.

    5 fields no max permitido pelo Discord — coube tudo num modal só.
    Placeholder é opcional; label é obrigatório.
    """

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(title="Editar modal de submissão")
        self.cog = cog
        self.guild_id = int(guild_id)

        cfg = cog._get_config(guild_id)
        modal = cfg.get("modal") or {}

        self.title_input = discord.ui.TextInput(
            label="Título do modal",
            default=_truncate(modal.get("title") or "", MODAL_TITLE_MAX),
            max_length=MODAL_TITLE_MAX,
            required=True,
        )
        self.age_label_input = discord.ui.TextInput(
            label="Label idade/pronome",
            default=_truncate(modal.get("age_label") or "", TEXT_INPUT_LABEL_MAX),
            max_length=TEXT_INPUT_LABEL_MAX,
            required=True,
        )
        self.age_placeholder_input = discord.ui.TextInput(
            label="Placeholder idade/pronome",
            default=_truncate(modal.get("age_placeholder") or "", TEXT_INPUT_PLACEHOLDER_MAX),
            max_length=TEXT_INPUT_PLACEHOLDER_MAX,
            required=False,
        )
        self.desc_label_input = discord.ui.TextInput(
            label="Label descrição",
            default=_truncate(modal.get("desc_label") or "", TEXT_INPUT_LABEL_MAX),
            max_length=TEXT_INPUT_LABEL_MAX,
            required=True,
        )
        self.desc_placeholder_input = discord.ui.TextInput(
            label="Placeholder descrição",
            default=_truncate(modal.get("desc_placeholder") or "", TEXT_INPUT_PLACEHOLDER_MAX),
            max_length=TEXT_INPUT_PLACEHOLDER_MAX,
            required=False,
        )

        self.add_item(self.title_input)
        self.add_item(self.age_label_input)
        self.add_item(self.age_placeholder_input)
        self.add_item(self.desc_label_input)
        self.add_item(self.desc_placeholder_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._update_modal_config(
            interaction,
            title=str(self.title_input.value or "").strip(),
            age_label=str(self.age_label_input.value or "").strip(),
            age_placeholder=str(self.age_placeholder_input.value or "").strip(),
            desc_label=str(self.desc_label_input.value or "").strip(),
            desc_placeholder=str(self.desc_placeholder_input.value or "").strip(),
        )


class ResponseEditModal(discord.ui.Modal):
    """Edita o template da mensagem postada no canal de respostas.

    Placeholders aceitos:
      {user}           — menção do usuário
      {idade_pronome}  — texto cru do campo 1 do modal de submissão
      {descricao}      — texto cru do campo 2 do modal de submissão

    Placeholders desconhecidos ficam intactos (não levantam erro).
    """

    def __init__(self, cog: "FormsCog", guild_id: int):
        super().__init__(title="Editar resposta")
        self.cog = cog
        self.guild_id = int(guild_id)

        cfg = cog._get_config(guild_id)
        response = cfg.get("response") or {}

        self.header_input = discord.ui.TextInput(
            label="Header (use {user}, {idade_pronome})",
            default=_truncate(response.get("header") or "", RESPONSE_HEADER_MAX),
            style=discord.TextStyle.paragraph,
            max_length=RESPONSE_HEADER_MAX,
            required=True,
        )
        self.body_input = discord.ui.TextInput(
            label="Corpo (use {descricao})",
            default=_truncate(response.get("body") or "", RESPONSE_BODY_MAX),
            style=discord.TextStyle.paragraph,
            max_length=RESPONSE_BODY_MAX,
            required=True,
        )

        self.add_item(self.header_input)
        self.add_item(self.body_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._update_response_config(
            interaction,
            header=str(self.header_input.value or "").strip(),
            body=str(self.body_input.value or "").strip(),
        )
