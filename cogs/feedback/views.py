from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from .cog import FeedbackCog

from .components import (
    build_feedback_starter_text,
    category_info,
    notice_view,
    protocol_of,
    status_label,
)
from .constants import STATUS_IN_REVIEW, STATUS_OPEN


class _ReviewButton(discord.ui.Button):
    def __init__(self, cog: "FeedbackCog", feedback: dict[str, Any]):
        self.cog = cog
        self.protocol = protocol_of(feedback)
        super().__init__(
            label="Marcar em análise",
            emoji="🔎",
            style=discord.ButtonStyle.primary,
            custom_id=f"feedback:review:{self.protocol}",
            disabled=str(feedback.get("status") or STATUS_OPEN) == STATUS_IN_REVIEW,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.mark_in_review(interaction, self.protocol)


class _ResolveButton(discord.ui.Button):
    def __init__(self, cog: "FeedbackCog", feedback: dict[str, Any]):
        self.cog = cog
        self.protocol = protocol_of(feedback)
        super().__init__(
            label="Marcar como resolvido",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"feedback:resolve:{self.protocol}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.ask_resolve_confirmation(interaction, self.protocol)


class FeedbackThreadView(discord.ui.LayoutView):
    def __init__(self, cog: "FeedbackCog", feedback: dict[str, Any]):
        super().__init__(timeout=None)
        self.cog = cog
        self.protocol = protocol_of(feedback)
        info = category_info(feedback)
        status = status_label(str(feedback.get("status") or STATUS_OPEN))
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(build_feedback_starter_text(feedback)),
                discord.ui.Separator(),
                discord.ui.TextDisplay(f"**Status atual**\n{status}"),
                discord.ui.Separator(),
                discord.ui.ActionRow(
                    _ReviewButton(cog, feedback),
                    _ResolveButton(cog, feedback),
                ),
                accent_color=info["accent"],
            )
        )


class _ConfirmResolveButton(discord.ui.Button):
    def __init__(self, view: "ResolveConfirmationView"):
        self.owner_view = view
        super().__init__(
            label="Resolver e excluir tópico",
            emoji="✅",
            style=discord.ButtonStyle.danger,
            custom_id=f"feedback:confirm_resolve:{view.protocol}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.owner_view.cog.resolve_feedback(
            interaction, self.owner_view.protocol
        )


class _CancelResolveButton(discord.ui.Button):
    def __init__(self, view: "ResolveConfirmationView"):
        self.owner_view = view
        super().__init__(
            label="Cancelar",
            style=discord.ButtonStyle.secondary,
            custom_id=f"feedback:cancel_resolve:{view.protocol}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=notice_view(
                "Resolução cancelada", "O feedback continua aberto.", ok=True
            )
        )


class ResolveConfirmationView(discord.ui.LayoutView):
    def __init__(self, cog: "FeedbackCog", protocol: str, *, actor_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.protocol = str(protocol)
        self.actor_id = int(actor_id)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "# Confirmar resolução\n"
                    f"O tópico de `{self.protocol}` será excluído imediatamente. "
                    "O registro mínimo do atendimento continuará salvo."
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(
                    _ConfirmResolveButton(self),
                    _CancelResolveButton(self),
                ),
                accent_color=discord.Color.red(),
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.actor_id:
            return True
        await interaction.response.send_message(
            view=notice_view(
                "Confirmação privada",
                "Somente quem iniciou a resolução pode usar estes botões.",
                ok=False,
            ),
            ephemeral=True,
        )
        return False


class _FeedbackSwitchSelect(discord.ui.Select):
    def __init__(
        self, owner_view: "FeedbackSwitchView", feedbacks: list[dict[str, Any]]
    ):
        self.owner_view = owner_view
        options: list[discord.SelectOption] = []
        for feedback in feedbacks[:25]:
            info = category_info(feedback)
            protocol = protocol_of(feedback)
            guild_name = str(feedback.get("guild_name") or "Servidor")[:80]
            options.append(
                discord.SelectOption(
                    label=f"{protocol} · {guild_name}"[:100],
                    value=protocol,
                    description=f"{info['label']} · {status_label(str(feedback.get('status') or STATUS_OPEN))}"[
                        :100
                    ],
                    emoji=str(info["emoji"]),
                    default=bool(feedback.get("dm_active")),
                )
            )
        super().__init__(
            placeholder="Selecione o atendimento que receberá suas mensagens",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"feedback:switch:{owner_view.owner_id}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = str(self.values[0]) if self.values else ""
        await self.owner_view.cog.switch_active_feedback(
            interaction, selected, owner_id=self.owner_view.owner_id
        )


class FeedbackSwitchView(discord.ui.LayoutView):
    def __init__(
        self, cog: "FeedbackCog", *, owner_id: int, feedbacks: list[dict[str, Any]]
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "# Trocar atendimento\n"
                    "Escolha para qual feedback as próximas mensagens iniciadas com `_` serão enviadas."
                ),
                discord.ui.Separator(),
                discord.ui.ActionRow(_FeedbackSwitchSelect(self, feedbacks)),
                accent_color=discord.Color.blurple(),
            )
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.owner_id:
            return True
        await interaction.response.send_message(
            view=notice_view(
                "Seleção privada", "Este seletor pertence a outro usuário.", ok=False
            ),
            ephemeral=True,
        )
        return False
