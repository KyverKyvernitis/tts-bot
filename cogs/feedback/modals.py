from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .cog import FeedbackCog

from .constants import CATEGORY_OPTIONS, DESCRIPTION_MAX_LENGTH, DESCRIPTION_MIN_LENGTH


class FeedbackModal(discord.ui.Modal, title="Enviar feedback"):
    def __init__(self, cog: "FeedbackCog", *, guild_id: int, opener_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = int(guild_id)
        self.opener_id = int(opener_id)

        select_cls = getattr(discord.ui, "StringSelect", discord.ui.Select)
        self.category_select = select_cls(
            custom_id="feedback_modal_category",
            placeholder="Selecione o tipo de feedback",
            min_values=1,
            max_values=1,
            required=True,
        )
        for key, info in CATEGORY_OPTIONS.items():
            self.category_select.add_option(
                label=str(info["label"]),
                value=key,
                description=str(info["description"])[:100],
                emoji=str(info["emoji"]),
            )

        self.description_input = discord.ui.TextInput(
            custom_id="feedback_modal_description",
            placeholder="Explique a situação com todos os detalhes que possam ajudar.",
            style=discord.TextStyle.paragraph,
            required=True,
            min_length=DESCRIPTION_MIN_LENGTH,
            max_length=DESCRIPTION_MAX_LENGTH,
        )

        self.add_item(
            discord.ui.Label(
                text="Tipo de feedback",
                description="Escolha a categoria que melhor representa a solicitação.",
                component=self.category_select,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Descrição",
                description="Informe o que aconteceu, o que você esperava e detalhes relevantes.",
                component=self.description_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        selected = list(getattr(self.category_select, "values", None) or [])
        category = str(selected[0]).strip() if selected else ""
        await self.cog.handle_feedback_submit(
            interaction,
            source_guild_id=self.guild_id,
            opener_id=self.opener_id,
            category=category,
            description=str(self.description_input.value or ""),
        )
