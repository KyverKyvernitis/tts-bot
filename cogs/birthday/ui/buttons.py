from __future__ import annotations

import discord


class _BackButton(discord.ui.Button):
    def __init__(self, panel: "BirthdayAdminView"):
        self.panel = panel
        super().__init__(label="Voltar", emoji="↩️", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        self.panel.go_back()
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _EntriesPageButton(discord.ui.Button):
    def __init__(self, panel: "BirthdayAdminView", *, direction: str):
        self.panel = panel
        self.direction = direction
        total_pages = max(1, (len(panel.entries_cache) + panel.entries_per_page - 1) // panel.entries_per_page)
        if direction == "prev":
            label = "Anterior"
            emoji = "⬅️"
            disabled = panel.entries_page <= 0
        else:
            label = "Próxima"
            emoji = "➡️"
            disabled = panel.entries_page >= total_pages - 1
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, disabled=disabled)

    async def callback(self, interaction: discord.Interaction):
        total_pages = max(1, (len(self.panel.entries_cache) + self.panel.entries_per_page - 1) // self.panel.entries_per_page)
        if self.direction == "prev":
            self.panel.entries_page = max(0, self.panel.entries_page - 1)
        else:
            self.panel.entries_page = min(total_pages - 1, self.panel.entries_page + 1)
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)
