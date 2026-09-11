from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..common import _shorten
from .visoes_base import VisaoSelecaoSimples

if TYPE_CHECKING:
    from ..cog import TTSVoice


class SeletorModo(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(label="ATTS", description="Android TTS nativo do worker", value="android_native", emoji="📱"),
            discord.SelectOption(label="gtts", description="Mais simples e compatível", value="gtts", emoji="🗣️"),
            discord.SelectOption(label="edge", description="Voz natural com voice, speed e pitch", value="edge", emoji="✨"),
        ]
        super().__init__(
            placeholder="Escolha o modo de TTS",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_mode_from_panel(
            interaction,
            self.values[0],
            server=self.server,
            source_panel_message=source_panel_message,
            target_user_id=getattr(getattr(self, "view", None), "target_user_id", None),
            target_user_name=getattr(getattr(self, "view", None), "target_user_name", None),
        )


class SeletorIdioma(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = []
        for code, name in list(sorted(cog.gtts_languages.items()))[:25]:
            options.append(
                discord.SelectOption(
                    label=_shorten(f"{code} — {name}"),
                    description="Idioma do modo gtts",
                    value=code,
                )
            )
        super().__init__(
            placeholder="Escolha um idioma do gtts",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_language_from_panel(
            interaction,
            self.values[0],
            server=self.server,
            source_panel_message=source_panel_message,
            target_user_id=getattr(getattr(self, "view", None), "target_user_id", None),
            target_user_name=getattr(getattr(self, "view", None), "target_user_name", None),
        )


class SeletorVelocidade(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(label="-100%", description="Extremamente devagar", value="-100%"),
            discord.SelectOption(label="-75%", description="Muito mais devagar", value="-75%"),
            discord.SelectOption(label="-50%", description="Bem mais devagar", value="-50%"),
            discord.SelectOption(label="-25%", description="Mais devagar", value="-25%"),
            discord.SelectOption(label="+0%", description="Velocidade normal", value="+0%"),
            discord.SelectOption(label="+25%", description="Mais rápido", value="+25%"),
            discord.SelectOption(label="+50%", description="Bem mais rápido", value="+50%"),
            discord.SelectOption(label="+75%", description="Muito mais rápido", value="+75%"),
            discord.SelectOption(label="+100%", description="Extremamente rápido", value="+100%"),
        ]
        super().__init__(placeholder="Escolha uma velocidade", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_speed_from_panel(
            interaction,
            self.values[0],
            server=self.server,
            source_panel_message=source_panel_message,
            target_user_id=getattr(getattr(self, "view", None), "target_user_id", None),
            target_user_name=getattr(getattr(self, "view", None), "target_user_name", None),
        )


class SeletorTom(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(label="-100Hz", description="Extremamente grave", value="-100Hz"),
            discord.SelectOption(label="-75Hz", description="Muito grave", value="-75Hz"),
            discord.SelectOption(label="-50Hz", description="Mais grave", value="-50Hz"),
            discord.SelectOption(label="-25Hz", description="Levemente grave", value="-25Hz"),
            discord.SelectOption(label="+0Hz", description="Tom normal", value="+0Hz"),
            discord.SelectOption(label="+25Hz", description="Levemente agudo", value="+25Hz"),
            discord.SelectOption(label="+50Hz", description="Mais agudo", value="+50Hz"),
            discord.SelectOption(label="+75Hz", description="Muito agudo", value="+75Hz"),
            discord.SelectOption(label="+100Hz", description="Extremamente agudo", value="+100Hz"),
        ]
        super().__init__(placeholder="Escolha um tom", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_pitch_from_panel(
            interaction,
            self.values[0],
            server=self.server,
            source_panel_message=source_panel_message,
            target_user_id=getattr(getattr(self, "view", None), "target_user_id", None),
            target_user_name=getattr(getattr(self, "view", None), "target_user_name", None),
        )


class SeletorRegiaoVoz(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        regions = sorted({voice.rsplit("-", 1)[0] for voice in (cog.edge_voice_cache or []) if voice.lower().startswith("pt-")})
        if not regions:
            regions = ["pt-BR"]
        options = [
            discord.SelectOption(
                label=_shorten(region),
                description="Abre a lista de vozes dessa região",
                value=region,
            )
            for region in regions[:25]
        ]
        super().__init__(placeholder="Escolha a região da voz", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        region = self.values[0]
        voices = [v for v in (self.cog.edge_voice_cache or []) if v.startswith(region + "-")]
        if not voices:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Nenhuma voz encontrada", "Não encontrei vozes para essa região.", ok=False),
                ephemeral=True,
            )
            return
        view = VisaoSelecaoSimples(
            self.cog,
            interaction.user.id,
            self.guild_id if hasattr(self, "guild_id") else interaction.guild.id,
            "Escolha a voz",
            f"Região selecionada: `{region}`",
            SeletorVoz(self.cog, server=self.server, voices=voices),
            target_user_id=getattr(getattr(self, "view", None), "target_user_id", None),
            target_user_name=getattr(getattr(self, "view", None), "target_user_name", None),
        )
        try:
            view.source_panel_message = getattr(self.view, "source_panel_message", None)
        except Exception:
            pass
        await interaction.response.send_message(
            embed=self.cog._make_embed("Escolha a voz", f"Região selecionada: `{region}`", ok=True),
            view=view,
            ephemeral=True,
        )


class SeletorVoz(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool, voices: list[str]):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(
                label=_shorten(voice),
                description="Voz do modo edge",
                value=voice,
            )
            for voice in voices[:25]
        ]
        super().__init__(placeholder="Escolha uma voz do edge", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_voice_from_panel(
            interaction,
            self.values[0],
            server=self.server,
            source_panel_message=source_panel_message,
            target_user_id=getattr(getattr(self, "view", None), "target_user_id", None),
            target_user_name=getattr(getattr(self, "view", None), "target_user_name", None),
        )


class SeletorToggle(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", toggle_name: str):
        self.cog = cog
        self.toggle_name = toggle_name
        options = [
            discord.SelectOption(label="Ativar", description="Liga essa função", value="true", emoji="✅"),
            discord.SelectOption(label="Desativar", description="Desliga essa função", value="false", emoji="⛔"),
        ]
        super().__init__(placeholder="Escolha se quer ativar ou desativar", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        enabled = self.values[0] == "true"
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        if self.toggle_name == "announce_author":
            await self.cog._apply_announce_author_from_panel(interaction, enabled, source_panel_message=source_panel_message)
        else:
            await self.cog._apply_auto_leave_from_panel(interaction, enabled, source_panel_message=source_panel_message)
