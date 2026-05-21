import inspect
import contextlib
import asyncio
import time
import re
import weakref
import unicodedata
from urllib.parse import urlparse
from typing import Optional, Callable

import discord
from discord import app_commands
from discord.ext import commands

import config
from .common import _shorten, validate_mode
from .utils.embed import build_settings_panel_text_from_embed, human_voice_name, human_language_name, human_rate, human_pitch

TTS_PANEL_EXPIRE_AFTER_SECONDS = 180.0
TTS_PANEL_DISPATCH_TIMEOUT_SECONDS = 86400.0

class _BaseTTSView(discord.ui.View):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        timeout: float = 180,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        requested_timeout = max(1.0, float(timeout or TTS_PANEL_EXPIRE_AFTER_SECONDS))
        dispatch_timeout = max(requested_timeout, TTS_PANEL_DISPATCH_TIMEOUT_SECONDS)
        super().__init__(timeout=dispatch_timeout)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.message: discord.Message | None = None
        self.panel_kind: str = "user"
        self.target_user_id: int | None = target_user_id
        self.target_user_name: str | None = target_user_name
        self.expires_at_monotonic = time.monotonic() + requested_timeout

    def _is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at_monotonic

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._is_expired():
            try:
                message = await self.cog._build_expired_panel_message(self.guild_id, self.panel_kind)
            except Exception:
                message = (
                    "Essa interação já expirou porque esse comando ficou aberto por tempo demais.\n\n"
                    "Para continuar, abra o comando novamente e gere um painel novo."
                )
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return False

        if self.owner_id == 0:
            return True
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=self.cog._make_embed(
                    "Painel bloqueado",
                    "Só quem abriu esse painel pode usar esses botões e menus.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(
            f"[tts_panel_error] user={getattr(interaction.user, 'id', None)} "
            f"guild={getattr(interaction.guild, 'id', None)} "
            f"item={getattr(item, 'custom_id', None) or getattr(item, 'label', None) or type(item).__name__} "
            f"error={repr(error)}"
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=self.cog._make_embed(
                        "Erro no painel",
                        "Essa interação falhou. Abra o painel novamente.",
                        ok=False,
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    embed=self.cog._make_embed(
                        "Erro no painel",
                        "Essa interação falhou. Abra o painel novamente.",
                        ok=False,
                    ),
                    ephemeral=True,
                )
        except Exception as e:
            print(f"[tts_panel_error] falha ao responder erro: {e!r}")

    async def on_timeout(self) -> None:
        pass


class _SimpleSelectView(_BaseTTSView):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        title: str,
        description: str,
        select: discord.ui.Select,
        *,
        timeout: float = 180,
        source_panel_message: discord.Message | None = None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(cog, owner_id, guild_id, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)
        self.title = title
        self.description = description
        self.source_panel_message: discord.Message | None = source_panel_message
        try:
            select.guild_id = guild_id
            select.owner_id = owner_id
            select.target_user_id = target_user_id
            select.target_user_name = target_user_name
        except Exception:
            pass
        self.add_item(select)

    async def send(self, interaction: discord.Interaction):
        if self.source_panel_message is None:
            self.source_panel_message = getattr(interaction, "message", None)
        embed = self.cog._make_embed(self.title, self.description, ok=True)
        if interaction.response.is_done():
            msg = await interaction.followup.send(embed=embed, view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
            try:
                msg = await interaction.original_response()
            except Exception:
                msg = None
        self.message = msg


class ModeSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(label="gtts", description="Mais simples e compatível", value="gtts", emoji="🗣️"),
            discord.SelectOption(label="edge", description="Voz natural com voice, speed e pitch", value="edge", emoji="✨"),
            discord.SelectOption(label="gcloud", description="Google Cloud TTS com idioma, voz, velocidade e tom próprios", value="gcloud", emoji="☁️"),
        ]
        super().__init__(
            placeholder="Escolha o modo de TTS",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_mode_from_panel(interaction, self.values[0], server=self.server, source_panel_message=source_panel_message, target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None), target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None))


class LanguageSelect(discord.ui.Select):
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
        await self.cog._apply_language_from_panel(interaction, self.values[0], server=self.server, source_panel_message=source_panel_message, target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None), target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None))


class SpeedSelect(discord.ui.Select):
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
        await self.cog._apply_speed_from_panel(interaction, self.values[0], server=self.server, source_panel_message=source_panel_message, target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None), target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None))


class PitchSelect(discord.ui.Select):
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
        await self.cog._apply_pitch_from_panel(interaction, self.values[0], server=self.server, source_panel_message=source_panel_message, target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None), target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None))


class GCloudSpeedSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(label="0.25x", description="Extremamente devagar", value="0.25"),
            discord.SelectOption(label="0.50x", description="Bem mais devagar", value="0.5"),
            discord.SelectOption(label="0.75x", description="Um pouco mais devagar", value="0.75"),
            discord.SelectOption(label="1.00x", description="Velocidade normal", value="1.0"),
            discord.SelectOption(label="1.25x", description="Um pouco mais rápido", value="1.25"),
            discord.SelectOption(label="1.50x", description="Mais rápido", value="1.5"),
            discord.SelectOption(label="1.75x", description="Bem mais rápido", value="1.75"),
            discord.SelectOption(label="2.00x", description="Extremamente rápido", value="2.0"),
        ]
        super().__init__(placeholder="Escolha a velocidade do Google Cloud", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_gcloud_speed_from_panel(interaction, self.values[0], server=self.server, source_panel_message=source_panel_message, target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None), target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None))


class GCloudPitchSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = [
            discord.SelectOption(label="-20", description="Extremamente grave", value="-20"),
            discord.SelectOption(label="-15", description="Muito grave", value="-15"),
            discord.SelectOption(label="-10", description="Mais grave", value="-10"),
            discord.SelectOption(label="-5", description="Levemente grave", value="-5"),
            discord.SelectOption(label="0", description="Tom normal", value="0"),
            discord.SelectOption(label="+5", description="Levemente agudo", value="5"),
            discord.SelectOption(label="+10", description="Mais agudo", value="10"),
            discord.SelectOption(label="+15", description="Muito agudo", value="15"),
            discord.SelectOption(label="+20", description="Extremamente agudo", value="20"),
        ]
        super().__init__(placeholder="Escolha o tom do Google Cloud", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_gcloud_pitch_from_panel(interaction, self.values[0], server=self.server, source_panel_message=source_panel_message, target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None), target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None))



class GCloudLanguageSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool, options: list[discord.SelectOption]):
        self.cog = cog
        self.server = server
        super().__init__(
            placeholder="Escolha o idioma do Google Cloud",
            min_values=1,
            max_values=1,
            options=options[:25],
        )

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_gcloud_language_from_panel(
            interaction,
            self.values[0],
            server=self.server,
            source_panel_message=source_panel_message,
            target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None),
            target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None),
        )


class GCloudVoiceSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool, options: list[discord.SelectOption]):
        self.cog = cog
        self.server = server
        super().__init__(
            placeholder="Escolha a voz do Google Cloud",
            min_values=1,
            max_values=1,
            options=options[:25],
        )

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        await self.cog._apply_gcloud_voice_from_panel(
            interaction,
            self.values[0],
            server=self.server,
            source_panel_message=source_panel_message,
            target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None),
            target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None),
        )


class VoiceRegionSelect(discord.ui.Select):
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
        view = _SimpleSelectView(
            self.cog,
            interaction.user.id,
            self.guild_id if hasattr(self, "guild_id") else interaction.guild.id,
            "Escolha a voz",
            f"Região selecionada: `{region}`",
            VoiceSelect(self.cog, server=self.server, voices=voices),
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


class VoiceSelect(discord.ui.Select):
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
        await self.cog._apply_voice_from_panel(interaction, self.values[0], server=self.server, source_panel_message=source_panel_message, target_user_id=getattr(getattr(self, 'view', None), 'target_user_id', None), target_user_name=getattr(getattr(self, 'view', None), 'target_user_name', None))


class ToggleSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", toggle_name: str):
        self.cog = cog
        self.toggle_name = toggle_name
        desc = "Ativar" if toggle_name == "only_target_user" else "Ativar"
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




class LanguageCodeModal(discord.ui.Modal, title="Selecionar idioma"):
    language_code = discord.ui.TextInput(
        label="Digite um dos códigos",
        placeholder="pt-br, en, es, fr, ja",
        required=True,
        min_length=2,
        max_length=10,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = server
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_language_from_panel(
            interaction,
            str(self.language_code).strip(),
            server=self.server,
            source_panel_message=self.panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class LanguageHelpView(discord.ui.View):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        server: bool = False,
        source_panel_message: discord.Message | None = None,
        timeout: float = 180,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.server = server
        self.source_panel_message = source_panel_message
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        target_owner = interaction.user.id if self.owner_id == 0 else self.owner_id
        if interaction.user.id != target_owner:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Sem permissão", "Esse painel pertence a outro usuário.", ok=False),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Ver lista de idiomas", style=discord.ButtonStyle.secondary, emoji="📚", row=0)
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        items = sorted(self.cog.gtts_languages.items())
        if not items:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Idiomas disponíveis", "Nenhum idioma encontrado.", ok=False),
                ephemeral=True,
            )
            return

        rows = []
        for i in range(0, len(items), 2):
            left_code, left_name = items[i]
            left = f"`{left_code}` — {left_name}"
            if i + 1 < len(items):
                right_code, right_name = items[i + 1]
                right = f"`{right_code}` — {right_name}"
                rows.append(f"{left}  |  {right}")
            else:
                rows.append(left)

        description = "\n".join(rows)
        embed = discord.Embed(
            title="Idiomas disponíveis",
            description=description,
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Selecionar idioma", style=discord.ButtonStyle.secondary, emoji="🌐", row=0)
    async def select_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            LanguageCodeModal(
                self.cog,
                self.source_panel_message,
                server=self.server,
                target_user_id=self.target_user_id,
                target_user_name=self.target_user_name,
            )
        )


class BotPrefixModal(discord.ui.Modal, title="Alterar prefixo do bot"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo do bot",
        placeholder="Ex.: _",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="bot",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )


class GTTSPrefixModal(discord.ui.Modal, title="Alterar prefixo do modo gTTS"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo do modo gTTS",
        placeholder="Ex.: .",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="gtts",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )


class EdgePrefixModal(discord.ui.Modal, title="Alterar prefixo do modo Edge"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo do modo Edge",
        placeholder="Ex.: ,",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="edge",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )


class GCloudPrefixModal(discord.ui.Modal, title="Alterar prefixo do Google Cloud"):
    new_prefix = discord.ui.TextInput(
        label="Novo prefixo do Google Cloud",
        placeholder="Ex.: '",
        required=True,
        min_length=1,
        max_length=8,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_prefix_from_modal(
            interaction,
            prefix_kind="gcloud",
            prefix=str(self.new_prefix),
            panel_message=self.panel_message,
        )


class GCloudLanguageModal(discord.ui.Modal, title="Alterar idioma do Google Cloud"):
    language = discord.ui.TextInput(
        label="Idioma do Google Cloud",
        placeholder="Ex.: pt-BR",
        required=True,
        max_length=16,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None, current_value: str | None = None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = server
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        if current_value is not None:
            self.language.default = str(current_value or "")[:16]

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_gcloud_language_from_modal(
            interaction,
            str(self.language),
            server=self.server,
            panel_message=self.panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class GCloudVoiceModal(discord.ui.Modal, title="Alterar voz do Google Cloud"):
    voice_name = discord.ui.TextInput(
        label="Voz do Google Cloud",
        placeholder="Ex.: pt-BR-Standard-A",
        required=True,
        max_length=64,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None, current_value: str | None = None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = server
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        if current_value is not None:
            self.voice_name.default = str(current_value or "")[:64]

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_gcloud_voice_from_modal(
            interaction,
            str(self.voice_name),
            server=self.server,
            panel_message=self.panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class IgnoredRoleSelect(discord.ui.RoleSelect):
    def __init__(self, cog: "TTSVoice"):
        self.cog = cog
        super().__init__(
            placeholder="Selecione um cargo para ignorar no TTS",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None)
        selected_role = self.values[0] if getattr(self, "values", None) else None
        if not isinstance(selected_role, discord.Role):
            await interaction.response.send_message(
                embed=self.cog._make_embed("Cargo inválido", "Não consegui identificar o cargo selecionado.", ok=False),
                ephemeral=True,
            )
            return
        await self.cog._apply_ignored_tts_role_from_panel(
            interaction,
            selected_role,
            source_panel_message=source_panel_message,
        )


class IgnoreRoleConfigView(_BaseTTSView):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        timeout: float = 180,
        source_panel_message: discord.Message | None = None,
    ):
        super().__init__(cog, owner_id, guild_id, timeout=timeout)
        self.panel_kind = "server"
        self.source_panel_message = source_panel_message
        self.add_item(IgnoredRoleSelect(cog))

    async def send(self, interaction: discord.Interaction):
        if self.source_panel_message is None:
            self.source_panel_message = getattr(interaction, "message", None)
        embed = self.cog._make_embed(
            "Cargo ignorado no TTS",
            "Selecione um cargo para ignorar mensagens de TTS dos usuários que estiverem nele, ou remova o cargo configurado.",
            ok=True,
        )
        if interaction.response.is_done():
            msg = await interaction.followup.send(embed=embed, view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)
            try:
                msg = await interaction.original_response()
            except Exception:
                msg = None
        self.message = msg

    @discord.ui.button(label="Remover cargo", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def remove_role_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._remove_ignored_tts_role_from_panel(
            interaction,
            source_panel_message=self.source_panel_message,
        )


class SpokenNameModal(discord.ui.Modal, title="Alterar apelido falado"):
    spoken_name = discord.ui.TextInput(
        label="Apelido falado",
        placeholder="Digite um apelido pronunciável ou deixe vazio para limpar",
        required=False,
        max_length=32,
    )

    def __init__(
        self,
        cog: "TTSVoice",
        panel_message: discord.Message | None,
        *,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
        current_value: str | None = None,
    ):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        if current_value is not None:
            self.spoken_name.default = str(current_value or "")[:32]

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_spoken_name_from_modal(
            interaction,
            str(self.spoken_name),
            panel_message=self.panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )



def _current_tts_value(cog: "TTSVoice", guild_id: int, user_id: int, key: str, default: str = "", *, server: bool = False) -> str:
    db = cog._get_db()
    try:
        if server and db is not None and hasattr(db, "get_guild_tts_defaults"):
            data = db.get_guild_tts_defaults(guild_id) or {}
            return str((data or {}).get(key) or default or "")
        if db is not None and hasattr(db, "resolve_tts"):
            data = db.resolve_tts(guild_id, user_id) or {}
            return str((data or {}).get(key) or default or "")
    except Exception:
        pass
    return str(default or "")


def _select_values(item) -> list[str]:
    try:
        return [str(v) for v in (getattr(item, "values", None) or []) if str(v or "").strip()]
    except Exception:
        return []


def _item_value(item, default: str = "") -> str:
    try:
        value = getattr(item, "value", None)
        if value is None:
            return str(default or "")
        return str(value or "").strip()
    except Exception:
        return str(default or "")


def _experimental_modal_components_enabled() -> bool:
    # Selects/radio/checkbox dentro de modal ainda variam bastante entre
    # versões da lib/cliente. Mantemos desligado por padrão para não quebrar
    # a interação do painel; o painel continua usando selects na mensagem e
    # modais seguros com TextInput.
    return bool(getattr(config, "TTS_EXPERIMENTAL_MODAL_COMPONENTS", False))


def _maybe_add_radio_group(modal, attr_name: str, *, label: str, options: list[tuple[str, str, str]], default_value: str) -> bool:
    if not _experimental_modal_components_enabled():
        return False
    group_cls = getattr(discord.ui, "RadioGroup", None)
    if group_cls is None:
        return False
    try:
        group = group_cls(custom_id=attr_name, required=True, options=[])
        for opt_label, value, description in options:
            group.add_option(
                label=opt_label,
                value=value,
                description=description or None,
                default=(str(value) == str(default_value)),
            )
        modal.add_item(group)
        setattr(modal, attr_name, group)
        return True
    except Exception as e:
        print(f"[tts_modal] RadioGroup desativado/falhou: {e!r}")
        return False


def _maybe_add_checkbox_group(modal, attr_name: str, *, options: list[tuple[str, str, str, bool]], min_values: int = 0, max_values: int | None = None) -> bool:
    if not _experimental_modal_components_enabled():
        return False
    group_cls = getattr(discord.ui, "CheckboxGroup", None)
    if group_cls is None:
        return False
    try:
        group = group_cls(custom_id=attr_name, required=False, min_values=min_values, max_values=max_values or len(options), options=[])
        for opt_label, value, description, selected in options:
            group.add_option(
                label=opt_label,
                value=value,
                description=description or None,
                default=bool(selected),
            )
        modal.add_item(group)
        setattr(modal, attr_name, group)
        return True
    except Exception as e:
        print(f"[tts_modal] CheckboxGroup desativado/falhou: {e!r}")
        return False


def _make_optional_select(*, placeholder: str, options: list[discord.SelectOption]):
    kwargs = dict(placeholder=placeholder, min_values=0, max_values=1, options=options[:25])
    try:
        return discord.ui.Select(required=False, **kwargs)
    except TypeError:
        return discord.ui.Select(**kwargs)


def _single_component_value(item, default: str = "") -> str:
    values = _select_values(item)
    if values:
        return str(values[0] or "").strip()
    return _item_value(item, default)


def _with_default_option(options: list[discord.SelectOption], current: str) -> list[discord.SelectOption]:
    current = str(current or "").strip()
    seen = set()
    fixed: list[discord.SelectOption] = []
    if current:
        fixed.append(discord.SelectOption(label=_shorten(current, 100), description="Valor atual", value=current, default=True))
        seen.add(current)
    for option in options or []:
        value = str(getattr(option, "value", "") or "").strip()
        if not value or value in seen:
            continue
        try:
            option.default = (value == current)
        except Exception:
            pass
        fixed.append(option)
        seen.add(value)
        if len(fixed) >= 25:
            break
    return fixed[:25]


def _modal_label_available() -> bool:
    return bool(hasattr(discord.ui, "Label"))


def _make_modal_text_input(*, label: str, placeholder: str, current: str = "", max_length: int = 80, required: bool = False):
    item = discord.ui.TextInput(label=label, placeholder=placeholder, required=required, max_length=max_length)
    try:
        item.default = str(current or "")[:max_length]
    except Exception:
        try:
            item.value = str(current or "")[:max_length]
        except Exception:
            pass
    return item


def _add_modal_text_input(modal, attr_name: str, *, label: str, placeholder: str, current: str = "", max_length: int = 80, required: bool = False) -> None:
    item = _make_modal_text_input(label=label, placeholder=placeholder, current=current, max_length=max_length, required=required)
    modal.add_item(item)
    setattr(modal, attr_name, item)


def _add_modal_label_item(modal, attr_name: str, *, text: str, description: str, component) -> bool:
    label_cls = getattr(discord.ui, "Label", None)
    if label_cls is None:
        return False
    try:
        modal.add_item(label_cls(text=text[:45], description=(description or None), component=component))
        setattr(modal, attr_name, component)
        return True
    except Exception as e:
        print(f"[tts_modal] Label desativado/falhou: {e!r}")
        return False


def _make_modal_select(custom_id: str, *, placeholder: str, options: list[discord.SelectOption], required: bool = True):
    kwargs = dict(custom_id=custom_id, placeholder=placeholder[:150], min_values=1 if required else 0, max_values=1, options=options[:25])
    try:
        return discord.ui.Select(required=required, **kwargs)
    except TypeError:
        return discord.ui.Select(**kwargs)


def _radio_value_matches(left: object, right: object) -> bool:
    a = str(left or "").strip().replace("+", "")
    b = str(right or "").strip().replace("+", "")
    if a == b:
        return True
    try:
        na = float(a.lower().replace("hz", "").replace("%", ""))
        nb = float(b.lower().replace("hz", "").replace("%", ""))
        return abs(na - nb) < 0.001
    except Exception:
        return False


def _make_modal_radio(custom_id: str, *, options: list[tuple[str, str, str]], default_value: str):
    group_cls = getattr(discord.ui, "RadioGroup", None)
    if group_cls is None:
        return None
    try:
        group = group_cls(custom_id=custom_id, required=True, options=[])
        default_seen = any(_radio_value_matches(value, default_value) for _, value, _ in options)
        for label, value, description in options:
            is_default = _radio_value_matches(value, default_value) if default_seen else label.casefold() == "normal"
            group.add_option(
                label=label[:100],
                value=str(value)[:100],
                description=(description or None),
                default=is_default,
            )
        return group
    except Exception as e:
        print(f"[tts_modal] RadioGroup desativado/falhou: {e!r}")
        return None


def _add_modal_radio(modal, attr_name: str, *, text: str, description: str, options: list[tuple[str, str, str]], current: str) -> bool:
    group = _make_modal_radio(attr_name, options=options, default_value=str(current or ""))
    if group is None:
        return False
    return _add_modal_label_item(modal, attr_name, text=text, description=description, component=group)


def _make_modal_checkbox_group(custom_id: str, *, options: list[tuple[str, str, str, bool]], min_values: int = 0, max_values: int | None = None):
    group_cls = getattr(discord.ui, "CheckboxGroup", None)
    if group_cls is None:
        return None
    try:
        group = group_cls(custom_id=custom_id, required=False, min_values=min_values, max_values=max_values or len(options), options=[])
        for label, value, description, default in options:
            group.add_option(label=label[:100], value=str(value)[:100], description=(description or None), default=bool(default))
        return group
    except Exception as e:
        print(f"[tts_modal] CheckboxGroup desativado/falhou: {e!r}")
        return None


def _edge_language_from_voice(voice: str, default: str = "pt-BR") -> str:
    voice = str(voice or "").strip()
    parts = voice.split("-")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}-{parts[1]}"
    return str(default or "pt-BR")


def _edge_voice_matches_language(voice: str, language: str) -> bool:
    voice = str(voice or "").strip()
    language = str(language or "").strip()
    return bool(voice and language and voice.startswith(language + "-"))


def _edge_language_options(cog: "TTSVoice", current: str = "") -> list[discord.SelectOption]:
    current = str(current or "pt-BR").strip() or "pt-BR"
    preferred = [current, "pt-BR", "pt-PT", "en-US", "en-GB", "es-ES", "es-MX", "fr-FR", "de-DE", "it-IT", "ja-JP", "ko-KR", "zh-CN"]
    discovered = sorted({
        _edge_language_from_voice(v)
        for v in list(getattr(cog, "edge_voice_cache", []) or []) + sorted(getattr(cog, "edge_voice_names", set()) or set())
        if str(v or "").strip()
    })
    seen: set[str] = set()
    options: list[discord.SelectOption] = []
    for code in preferred + discovered:
        code = str(code or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        options.append(discord.SelectOption(label=_shorten(code, 100), description="Idioma Edge", value=code, default=(code == current)))
        if len(options) >= 25:
            break
    return options or [discord.SelectOption(label="pt-BR", description="Idioma Edge", value="pt-BR", default=True)]


def _edge_voice_options_for_language(cog: "TTSVoice", *, language: str, current: str = "") -> list[discord.SelectOption]:
    language = str(language or _edge_language_from_voice(current)).strip() or "pt-BR"
    current = str(current or "").strip()
    voices_source = list(getattr(cog, "edge_voice_cache", []) or []) + sorted(getattr(cog, "edge_voice_names", set()) or set())
    preferred = [
        "pt-BR-FranciscaNeural",
        "pt-BR-AntonioNeural",
        "pt-BR-BrendaNeural",
        "pt-BR-DonatoNeural",
        "pt-BR-ElzaNeural",
        "pt-BR-FabioNeural",
        "pt-BR-GiovannaNeural",
        "pt-BR-HumbertoNeural",
        "pt-BR-JulioNeural",
        "pt-BR-LeilaNeural",
        "pt-BR-LeticiaNeural",
        "pt-BR-ManuelaNeural",
        "pt-BR-NicolauNeural",
        "pt-BR-ValerioNeural",
        "pt-BR-YaraNeural",
    ]
    candidates: list[str] = []
    if current and _edge_voice_matches_language(current, language):
        candidates.append(current)
    candidates.extend([v for v in preferred if _edge_voice_matches_language(v, language)])
    candidates.extend([v for v in voices_source if _edge_voice_matches_language(str(v), language)])
    seen: set[str] = set()
    voices: list[str] = []
    for voice in candidates:
        voice = str(voice or "").strip()
        if not voice or voice in seen:
            continue
        seen.add(voice)
        voices.append(voice)
        if len(voices) >= 25:
            break
    if not voices and current:
        voices = [current]
    if not voices:
        voices = ["pt-BR-FranciscaNeural"]
    has_current = current and any(v == current for v in voices)
    options: list[discord.SelectOption] = []
    for idx, voice in enumerate(voices[:25]):
        options.append(
            discord.SelectOption(
                label=_shorten(voice, 100),
                description="Voz Edge",
                value=voice,
                default=(voice == current if has_current else idx == 0),
            )
        )
    return options


def _pick_first_edge_voice_for_language(cog: "TTSVoice", language: str, current: str = "") -> str:
    options = _edge_voice_options_for_language(cog, language=language, current=current)
    if not options:
        return str(current or "pt-BR-FranciscaNeural")
    return str(getattr(options[0], "value", None) or getattr(options[0], "label", None) or current or "pt-BR-FranciscaNeural")


def _top_edge_voice_options(cog: "TTSVoice", current: str = "") -> list[discord.SelectOption]:
    language = _edge_language_from_voice(current)
    return _edge_voice_options_for_language(cog, language=language, current=current)


def _top_gtts_language_options(cog: "TTSVoice", current: str = "") -> list[discord.SelectOption]:
    preferred = ["pt-br", "pt", "en", "es", "fr", "ja", "it", "de"]
    items = []
    seen = set()
    if current:
        preferred.insert(0, current)
    langs = dict(cog.gtts_languages or {})
    for code in preferred + sorted(langs):
        code = str(code or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        name = langs.get(code) or code
        items.append(discord.SelectOption(label=_shorten(f"{code} — {name}", 100), description="Idioma gTTS", value=code))
        if len(items) >= 25:
            break
    return items or [discord.SelectOption(label="pt-br", description="Português Brasil", value="pt-br")]


def _top_gcloud_language_options(current: str = "") -> list[discord.SelectOption]:
    preferred = [current or "pt-BR", "pt-BR", "en-US", "es-ES", "ja-JP", "fr-FR", "it-IT", "de-DE"]
    seen = set()
    options = []
    for code in preferred:
        code = str(code or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        options.append(discord.SelectOption(label=code, description="Idioma Google Cloud", value=code))
    return options


def _gcloud_voice_options_for_language(cog: "TTSVoice", *, language: str, current: str = "") -> list[discord.SelectOption]:
    language = str(language or "pt-BR").strip() or "pt-BR"
    current = str(current or "").strip()
    catalog = list(getattr(cog, "_gcloud_voices_cache", []) or [])
    options: list[discord.SelectOption] = []
    try:
        if catalog:
            safe_current = current if cog._gcloud_voice_matches_language(current, language) else ""
            options = cog._build_gcloud_voice_options_from_catalog(catalog, language, current_value=safe_current)
    except Exception as e:
        print(f"[tts_modal] falha ao filtrar vozes Google por idioma: {e!r}")
        options = []

    if not options:
        families = ["Standard", "Wavenet", "Neural2"]
        letters = ["A", "B", "C", "D", "E"]
        names: list[str] = []
        if current and current.startswith(language + "-"):
            names.append(current)
        for family in families:
            for letter in letters:
                names.append(f"{language}-{family}-{letter}")
        seen: set[str] = set()
        for name in names:
            if not name or name in seen or not name.startswith(language + "-"):
                continue
            seen.add(name)
            options.append(discord.SelectOption(label=_shorten(name, 100), description="Voz Google", value=name))
            if len(options) >= 25:
                break

    if not options:
        options = [discord.SelectOption(label=f"{language}-Standard-A", description="Voz Google", value=f"{language}-Standard-A")]

    current_matches = current and cog._gcloud_voice_matches_language(current, language)
    seen_values = {str(getattr(opt, "value", "") or "") for opt in options}
    if current_matches and current not in seen_values:
        options.insert(0, discord.SelectOption(label=_shorten(current, 100), description="Voz Google", value=current))

    has_default = False
    for index, option in enumerate(options[:25]):
        value = str(getattr(option, "value", "") or "")
        try:
            option.default = (value == current) if current_matches else (index == 0)
            has_default = has_default or bool(option.default)
        except Exception:
            pass
    if not has_default and options:
        try:
            options[0].default = True
        except Exception:
            pass
    return options[:25]


def _top_gcloud_voice_options(cog: "TTSVoice", current: str = "", language: str = "pt-BR") -> list[discord.SelectOption]:
    return _gcloud_voice_options_for_language(cog, language=language, current=current)


async def _save_tts_modal_updates(
    cog: "TTSVoice",
    interaction: discord.Interaction,
    *,
    source_panel_message: discord.Message | None,
    server: bool,
    updates: dict[str, object],
    history_label: str,
    history_value: str,
    success_title: str,
    success_description: str,
    target_user_id: int | None = None,
    target_user_name: str | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Comando indisponível", "Esse ajuste só pode ser usado dentro de um servidor.", ok=False),
            ephemeral=True,
        )
        return
    if server and not getattr(getattr(interaction.user, "guild_permissions", None), "kick_members", False):
        await interaction.response.send_message(
            embed=cog._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para alterar o TTS do servidor.", ok=False),
            ephemeral=True,
        )
        return
    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    clean_updates = {k: v for k, v in (updates or {}).items() if v is not None}
    if not clean_updates:
        await interaction.response.send_message(
            embed=cog._make_embed("Nada mudou", "Nenhum ajuste foi alterado.", ok=True),
            ephemeral=True,
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(
        interaction,
        server=server,
        message_id=message_id,
        target_user_id=target_user_id,
        target_user_name=target_user_name,
    )

    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, **clean_updates))
        history_entry = cog._server_history_text(interaction, history_label, history_value)
        await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        cog._append_public_panel_history(message_id, history_entry)
        history_user_id = interaction.user.id
        panel_kind = "server"
    else:
        history_entry = cog._user_history_text(
            interaction,
            history_label,
            history_value,
            message_id=message_id,
            target_user_id=effective_user_id,
            target_user_name=effective_user_name,
        )
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, history_entry=history_entry, **clean_updates)
        cog._append_public_panel_history(message_id, history_entry)
        history_user_id = effective_user_id
        panel_kind = "user"

    state = cog._public_panel_states.get(message_id or 0, {}) if message_id else {}
    if panel_message is not None and state.get("panel_kind") == "launcher":
        history_text = cog._format_history_entries(
            cog._get_public_panel_history(message_id),
            viewer_user_id=None,
            message_id=message_id,
        ) or "• Nada recente."
        view = cog._build_public_tts_launcher_view(interaction.guild.id, timeout=300, history_text=history_text)
        view.message = panel_message
        await cog._panel_update_after_change(
            interaction,
            embed=cog._make_embed("TTS", "Cada prefixo escolhe um modo de voz.", ok=True),
            view=view,
            title=success_title,
            description=success_description,
            target_message=panel_message,
        )
        if server:
            await cog._announce_panel_change(interaction, title=success_title, description=success_description, target_message=panel_message)
        return

    should_edit_panel = bool(panel_message is not None)
    if should_edit_panel:
        panel_history = await cog._maybe_await(db.get_panel_history(interaction.guild.id, history_user_id)) if hasattr(db, "get_panel_history") else {}
        key = "server_last_changes" if server else "user_last_changes"
        last_changes = list((panel_history or {}).get(key, []) or [])
        embed = await cog._build_settings_embed(
            interaction.guild.id,
            effective_user_id if not server else interaction.user.id,
            server=server,
            panel_kind=panel_kind,
            last_changes=last_changes,
            message_id=message_id,
            target_user_name=effective_user_name if not server else None,
            viewer_user_id=interaction.user.id,
        )
        view_target_user_id = None if server or is_public_user_panel else effective_user_id
        view_target_user_name = None if server or is_public_user_panel else effective_user_name
        view = cog._build_panel_view(
            0 if message_id in cog._public_panel_states else interaction.user.id,
            interaction.guild.id,
            server=server,
            target_user_id=view_target_user_id,
            target_user_name=view_target_user_name,
        )
        if panel_message is not None:
            view.message = panel_message
        await cog._panel_update_after_change(
            interaction,
            embed=embed,
            view=view,
            title=success_title,
            description=success_description,
            target_message=panel_message,
        )
    else:
        await interaction.response.send_message(
            embed=cog._make_embed(success_title, success_description, ok=True),
            ephemeral=True,
        )

    if server:
        await cog._announce_panel_change(interaction, title=success_title, description=success_description, target_message=panel_message)


class EdgeSettingsModal(discord.ui.Modal, title="Editar Edge"):
    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None, force_text_fallback: bool = False):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.force_text_fallback = bool(force_text_fallback)
        user_id = int(target_user_id or 0)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        self.current_voice = _current_tts_value(cog, guild_id, user_id, "voice", str(getattr(config, "EDGE_TTS_VOICE", "pt-BR-FranciscaNeural") or "pt-BR-FranciscaNeural"), server=server)
        self.current_language = _edge_language_from_voice(self.current_voice)
        self.current_rate = _current_tts_value(cog, guild_id, user_id, "rate", "+0%", server=server)
        self.current_pitch = _current_tts_value(cog, guild_id, user_id, "pitch", "+0Hz", server=server)
        if self.force_text_fallback or not self._build_guided_modal():
            self._build_text_fallback()

    def _build_guided_modal(self) -> bool:
        if not _modal_label_available():
            return False
        try:
            language_select = _make_modal_select(
                "edge_language",
                placeholder="Idioma Edge",
                options=_edge_language_options(self.cog, self.current_language),
            )
            voice_select = _make_modal_select(
                "edge_voice",
                placeholder="Escolha a voz Edge",
                options=_edge_voice_options_for_language(self.cog, language=self.current_language, current=self.current_voice),
            )
            ok = _add_modal_label_item(
                self,
                "language",
                text="Idioma Edge",
                description="",
                component=language_select,
            )
            ok = ok and _add_modal_label_item(
                self,
                "voice",
                text="Voz Edge",
                description="",
                component=voice_select,
            )
            ok = ok and _add_modal_radio(
                self,
                "rate",
                text="Velocidade Edge",
                description="",
                current=self.current_rate,
                options=[
                    ("Bem mais lenta", "-50%", ""),
                    ("Mais lenta", "-25%", ""),
                    ("Normal", "+0%", ""),
                    ("Mais rápida", "+25%", ""),
                    ("Bem mais rápida", "+50%", ""),
                ],
            )
            ok = ok and _add_modal_radio(
                self,
                "pitch",
                text="Tom Edge",
                description="",
                current=self.current_pitch,
                options=[
                    ("Bem mais grave", "-50Hz", ""),
                    ("Mais grave", "-25Hz", ""),
                    ("Normal", "+0Hz", ""),
                    ("Mais agudo", "+25Hz", ""),
                    ("Bem mais agudo", "+50Hz", ""),
                ],
            )
            return bool(ok)
        except Exception as e:
            print(f"[tts_modal] Edge guiado falhou: {e!r}")
            try:
                self.clear_items()
            except Exception:
                pass
            return False

    def _build_text_fallback(self) -> None:
        _add_modal_text_input(
            self,
            "language",
            label="Idioma Edge",
            placeholder="Ex.: pt-BR, en-US, es-ES",
            current=self.current_language,
            max_length=16,
        )
        _add_modal_text_input(
            self,
            "voice",
            label="Voz Edge",
            placeholder="Voz usada com ,texto. Ex.: pt-BR-FranciscaNeural",
            current=self.current_voice,
            max_length=80,
        )
        _add_modal_text_input(
            self,
            "rate",
            label="Velocidade Edge",
            placeholder="Use +0% normal, -25% lenta ou +25% rápida",
            current=self.current_rate,
            max_length=8,
        )
        _add_modal_text_input(
            self,
            "pitch",
            label="Tom Edge",
            placeholder="Use +0Hz normal, -25Hz grave ou +25Hz agudo",
            current=self.current_pitch,
            max_length=8,
        )

    async def on_submit(self, interaction: discord.Interaction):
        updates: dict[str, object] = {}
        details: list[str] = []
        history_bits: list[str] = []

        language = _single_component_value(getattr(self, "language", None), self.current_language)
        selected_language = str(language or self.current_language or "pt-BR").strip() or "pt-BR"
        voice = _single_component_value(getattr(self, "voice", None), self.current_voice)
        selected_voice = str(voice or self.current_voice or "").strip()
        adjusted_voice = ""
        if selected_language and selected_voice and not _edge_voice_matches_language(selected_voice, selected_language):
            adjusted_voice = _pick_first_edge_voice_for_language(self.cog, selected_language, self.current_voice)
            selected_voice = adjusted_voice
        if selected_voice and str(selected_voice) != str(self.current_voice):
            if selected_voice not in self.cog.edge_voice_names and selected_voice not in self.cog.edge_voice_cache:
                await interaction.response.send_message(embed=self.cog._make_embed("Voz inválida", "Essa voz Edge não foi encontrada.", ok=False), ephemeral=True)
                return
            updates["voice"] = selected_voice
            if selected_language != self.current_language and adjusted_voice:
                details.append(f"• Idioma: {selected_language}")
                details.append(f"• Voz ajustada: {human_voice_name(selected_voice)}")
                history_bits.append("idioma e voz atualizados")
            else:
                details.append(f"• Voz: {human_voice_name(selected_voice)}")
                history_bits.append(f"voz {human_voice_name(selected_voice)}")
        elif selected_language != self.current_language:
            picked_voice = _pick_first_edge_voice_for_language(self.cog, selected_language, self.current_voice)
            if picked_voice and picked_voice != self.current_voice:
                updates["voice"] = picked_voice
                details.append(f"• Idioma: {selected_language}")
                details.append(f"• Voz ajustada: {human_voice_name(picked_voice)}")
                history_bits.append("idioma e voz atualizados")

        rate = _single_component_value(getattr(self, "rate", None), self.current_rate)
        if rate:
            normalized = self.cog._normalize_rate_value(rate)
            if normalized is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Velocidade inválida", "Use opções como `+0%`, `-25%` ou `+25%`.", ok=False), ephemeral=True)
                return
            current_rate = self.cog._normalize_rate_value(self.current_rate) or self.current_rate
            if str(normalized) != str(current_rate):
                updates["rate"] = normalized
                details.append(f"• Velocidade: {human_rate(normalized)}")
                history_bits.append(f"velocidade {human_rate(normalized)}")

        pitch = _single_component_value(getattr(self, "pitch", None), self.current_pitch)
        if pitch:
            normalized = self.cog._normalize_pitch_value(pitch)
            if normalized is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Tom inválido", "Use opções como `+0Hz`, `-25Hz` ou `+25Hz`.", ok=False), ephemeral=True)
                return
            current_pitch = self.cog._normalize_pitch_value(self.current_pitch) or self.current_pitch
            if str(normalized) != str(current_pitch):
                updates["pitch"] = normalized
                details.append(f"• Tom: {human_pitch(normalized)}")
                history_bits.append(f"tom {human_pitch(normalized)}")

        if len(history_bits) == 1 and history_bits[0] != "idioma e voz atualizados":
            key = history_bits[0].split(' ', 1)[0]
            article = {"voz": "a", "velocidade": "a", "tom": "o"}.get(key, "o")
            history_label = f"{article} {'própria ' if not self.server else ''}{key} do Edge"
            history_value = history_bits[0].split(' ', 1)[1] if ' ' in history_bits[0] else "atualizado"
        else:
            history_label = "o Edge" if self.server else "o próprio Edge"
            if "idioma e voz atualizados" in history_bits:
                history_value = "idioma e voz atualizados"
            else:
                history_value = "leitura atualizada" if updates and all(k in updates for k in ("rate", "pitch")) and "voice" not in updates else "configurações atualizadas"

        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=self.server,
            updates=updates,
            history_label=history_label,
            history_value=history_value,
            success_title="Edge atualizado",
            success_description="\n".join(details) if details else "Nada mudou.",
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class GTTSSettingsModal(discord.ui.Modal, title="Editar gTTS"):
    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None, force_text_fallback: bool = False):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.force_text_fallback = bool(force_text_fallback)
        user_id = int(target_user_id or 0)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        self.current_language = _current_tts_value(cog, guild_id, user_id, "language", "pt-br", server=server)
        if self.force_text_fallback or not self._build_guided_modal():
            self._build_text_fallback()

    def _build_guided_modal(self) -> bool:
        if not _modal_label_available():
            return False
        try:
            language_select = _make_modal_select(
                "gtts_language",
                placeholder="Idioma gTTS",
                options=_with_default_option(_top_gtts_language_options(self.cog, self.current_language), self.current_language),
            )
            ok = _add_modal_label_item(
                self,
                "language",
                text="Idioma gTTS",
                description="",
                component=language_select,
            )
            manual_input = _make_modal_text_input(
                label="Outro idioma",
                placeholder="Opcional: pt-br, en, es, ja",
                current="",
                max_length=10,
                required=False,
            )
            self.add_item(manual_input)
            self.manual_language = manual_input
            return bool(ok)
        except Exception as e:
            print(f"[tts_modal] gTTS guiado falhou: {e!r}")
            try:
                self.clear_items()
            except Exception:
                pass
            return False

    def _build_text_fallback(self) -> None:
        _add_modal_text_input(
            self,
            "language",
            label="Idioma gTTS",
            placeholder="Ex.: pt-br, en, es, fr, ja",
            current=self.current_language,
            max_length=10,
        )

    async def on_submit(self, interaction: discord.Interaction):
        selected = _single_component_value(getattr(self, "language", None), self.current_language)
        manual = _item_value(getattr(self, "manual_language", None))
        raw_value = manual or selected
        code, _language_name = self.cog._resolve_gtts_language_input(raw_value)
        if code is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Idioma inválido", "Use algo como `pt-br`, `en`, `es` ou `ja`.", ok=False),
                ephemeral=True,
            )
            return
        updates = {"language": code} if str(code) != str(self.current_language) else {}
        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=self.server,
            updates=updates,
            history_label="o idioma do modo gTTS" if self.server else "o próprio idioma do gTTS",
            history_value=code,
            success_title="gTTS atualizado",
            success_description=f"• Idioma: {human_language_name(code)}" if updates else "Nada mudou.",
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class GoogleSettingsModal(discord.ui.Modal, title="Editar Google"):
    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None, force_text_fallback: bool = False):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.force_text_fallback = bool(force_text_fallback)
        user_id = int(target_user_id or 0)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        self.current_language = cog._get_current_gcloud_language(guild_id, user_id, server=server) if guild_id else str(getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR")
        self.current_voice = cog._get_current_gcloud_voice(guild_id, user_id, server=server) if guild_id else str(getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A")
        self.current_rate = _current_tts_value(cog, guild_id, user_id, "gcloud_rate", "1.0", server=server)
        self.current_pitch = _current_tts_value(cog, guild_id, user_id, "gcloud_pitch", "0.0", server=server)
        if self.force_text_fallback or not self._build_guided_modal():
            self._build_text_fallback()

    def _build_guided_modal(self) -> bool:
        if not _modal_label_available():
            return False
        try:
            language_select = _make_modal_select(
                "gcloud_language",
                placeholder="Idioma Google",
                options=_with_default_option(_top_gcloud_language_options(self.current_language), self.current_language),
            )
            voice_select = _make_modal_select(
                "gcloud_voice",
                placeholder="Voz Google",
                options=_gcloud_voice_options_for_language(self.cog, language=self.current_language, current=self.current_voice),
            )
            ok = _add_modal_label_item(
                self,
                "language",
                text="Idioma Google",
                description="",
                component=language_select,
            )
            ok = ok and _add_modal_label_item(
                self,
                "voice",
                text="Voz Google",
                description="",
                component=voice_select,
            )
            ok = ok and _add_modal_radio(
                self,
                "rate",
                text="Velocidade Google",
                description="",
                current=self.current_rate,
                options=[
                    ("Mais lenta", "0.75", ""),
                    ("Normal", "1.0", ""),
                    ("Mais rápida", "1.25", ""),
                    ("Bem mais rápida", "1.5", ""),
                ],
            )
            ok = ok and _add_modal_radio(
                self,
                "pitch",
                text="Tom Google",
                description="",
                current=self.current_pitch,
                options=[
                    ("Mais grave", "-5", ""),
                    ("Normal", "0", ""),
                    ("Mais agudo", "5", ""),
                    ("Bem mais agudo", "10", ""),
                ],
            )
            return bool(ok)
        except Exception as e:
            print(f"[tts_modal] Google guiado falhou: {e!r}")
            try:
                self.clear_items()
            except Exception:
                pass
            return False

    def _build_text_fallback(self) -> None:
        _add_modal_text_input(
            self,
            "language",
            label="Idioma Google",
            placeholder="Idioma da voz Google. Ex.: pt-BR, en-US, es-ES",
            current=self.current_language,
            max_length=16,
        )
        _add_modal_text_input(
            self,
            "voice",
            label="Voz Google",
            placeholder="Voz usada com 'texto. Ex.: pt-BR-Standard-A",
            current=self.current_voice,
            max_length=80,
        )
        _add_modal_text_input(
            self,
            "rate",
            label="Velocidade Google",
            placeholder="Use 1.0 normal, 0.75 lenta ou 1.25 rápida",
            current=self.current_rate,
            max_length=8,
        )
        _add_modal_text_input(
            self,
            "pitch",
            label="Tom Google",
            placeholder="Use 0 normal, -5 grave ou 5 agudo",
            current=self.current_pitch,
            max_length=8,
        )

    async def on_submit(self, interaction: discord.Interaction):
        updates: dict[str, object] = {}
        details: list[str] = []
        history_bits: list[str] = []

        language = _single_component_value(getattr(self, "language", None), self.current_language)
        selected_language = self.current_language
        if language:
            value, error = self.cog._validate_gcloud_language_input(language)
            if error or value is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Idioma inválido", error or "Idioma inválido.", ok=False), ephemeral=True)
                return
            selected_language = value
            if str(value) != str(self.current_language):
                updates["gcloud_language"] = value
                details.append(f"• Idioma: {value}")
                history_bits.append(f"idioma {value}")

        voice = _single_component_value(getattr(self, "voice", None), self.current_voice)
        selected_voice = str(voice or self.current_voice or "").strip()
        if selected_voice:
            value, error = self.cog._validate_gcloud_voice_input(selected_voice)
            if error or value is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Voz inválida", error or "Voz inválida.", ok=False), ephemeral=True)
                return
            selected_voice = value

        adjusted_voice = ""
        if selected_language and selected_voice and not self.cog._gcloud_voice_matches_language(selected_voice, selected_language):
            catalog = await self.cog._load_gcloud_voices()
            adjusted_voice = self.cog._pick_first_gcloud_voice_for_language(catalog, selected_language) if catalog else f"{selected_language}-Standard-A"
            selected_voice = adjusted_voice

        if selected_voice and str(selected_voice) != str(self.current_voice):
            updates["gcloud_voice"] = selected_voice
            label = "Voz ajustada" if adjusted_voice else "Voz"
            details.append(f"• {label}: {selected_voice}")
            history_bits.append("voz ajustada" if adjusted_voice else f"voz {human_voice_name(selected_voice)}")

        rate = _single_component_value(getattr(self, "rate", None), self.current_rate)
        if rate:
            value = self.cog._normalize_gcloud_rate_value(rate)
            current_rate = self.cog._normalize_gcloud_rate_value(self.current_rate)
            if str(value) != str(current_rate):
                updates["gcloud_rate"] = value
                details.append(f"• Velocidade: {human_rate(value)}")
                history_bits.append(f"velocidade {human_rate(value)}")

        pitch = _single_component_value(getattr(self, "pitch", None), self.current_pitch)
        if pitch:
            value = self.cog._normalize_gcloud_pitch_value(pitch)
            current_pitch = self.cog._normalize_gcloud_pitch_value(self.current_pitch)
            if str(value) != str(current_pitch):
                updates["gcloud_pitch"] = value
                details.append(f"• Tom: {human_pitch(value)}")
                history_bits.append(f"tom {human_pitch(value)}")

        if len(history_bits) == 1:
            bit = history_bits[0]
            if bit == "voz ajustada":
                history_label = "o Google" if self.server else "o próprio Google"
                history_value = "voz ajustada"
            else:
                key, _, value = bit.partition(" ")
                article = {"voz": "a", "velocidade": "a", "tom": "o", "idioma": "o"}.get(key, "o")
                poss = {"voz": "própria", "velocidade": "própria", "tom": "próprio", "idioma": "próprio"}.get(key, "próprio")
                history_label = f"{article} {poss + ' ' if not self.server else ''}{key} do Google"
                history_value = value or "atualizado"
        else:
            history_label = "o Google" if self.server else "o próprio Google"
            if "gcloud_language" in updates and "gcloud_voice" in updates:
                history_value = "idioma e voz atualizados"
            elif updates:
                history_value = "leitura atualizada" if set(updates) <= {"gcloud_rate", "gcloud_pitch"} else "configurações atualizadas"
            else:
                history_value = "sem alterações"

        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=self.server,
            updates=updates,
            history_label=history_label,
            history_value=history_value,
            success_title="Google atualizado",
            success_description="\n".join(details) if details else "Nada mudou.",
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class ServerPrefixesModal(discord.ui.Modal, title="Prefixos do servidor"):
    bot_prefix = discord.ui.TextInput(label="Prefixo do bot", placeholder="Ex.: _", required=False, max_length=8)
    gtts_prefix = discord.ui.TextInput(label="Prefixo do gTTS", placeholder="Ex.: .", required=False, max_length=8)
    edge_prefix = discord.ui.TextInput(label="Prefixo do Edge", placeholder="Ex.: ,", required=False, max_length=8)
    google_prefix = discord.ui.TextInput(label="Prefixo do Google", placeholder="Ex.: '", required=False, max_length=8)

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        try:
            db = cog._get_db()
            defaults = db.get_guild_tts_defaults(guild_id) if db is not None and guild_id else {}
        except Exception:
            defaults = {}
        self.bot_prefix.default = str((defaults or {}).get("bot_prefix") or getattr(config, "PREFIX", "_") or "_")[:8]
        self.gtts_prefix.default = str((defaults or {}).get("tts_prefix") or (defaults or {}).get("gtts_prefix") or getattr(config, "TTS_PREFIX", ".") or ".")[:8]
        self.edge_prefix.default = str((defaults or {}).get("edge_prefix") or getattr(config, "EDGE_TTS_PREFIX", ",") or ",")[:8]
        self.google_prefix.default = str((defaults or {}).get("gcloud_prefix") or getattr(config, "GOOGLE_CLOUD_TTS_PREFIX", "'") or "'")[:8]

    async def on_submit(self, interaction: discord.Interaction):
        updates = {}
        parts = []
        for field_name, label, keys in [
            ("bot_prefix", "bot", ("bot_prefix",)),
            ("gtts_prefix", "gTTS", ("gtts_prefix", "tts_prefix")),
            ("edge_prefix", "Edge", ("edge_prefix",)),
            ("google_prefix", "Google", ("gcloud_prefix",)),
        ]:
            value = _item_value(getattr(self, field_name, None))
            if value:
                for key in keys:
                    updates[key] = value[:8]
                parts.append(f"{label}: {value[:8]}")
        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=True,
            updates=updates,
            history_label="os prefixos do servidor",
            history_value=", ".join(parts) if parts else "sem alterações",
            success_title="Prefixos atualizados",
            success_description="Salvo: " + (", ".join(parts) if parts else "sem alterações") + ".",
        )


class TTSServerRulesModal(discord.ui.Modal, title="Regras do TTS"):
    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        try:
            db = cog._get_db()
            defaults = db.get_guild_tts_defaults(guild_id) if db is not None and guild_id else {}
        except Exception:
            defaults = {}
        self.current_announce_author = bool((defaults or {}).get("announce_author"))
        role_id = int((defaults or {}).get("ignored_tts_role_id") or 0)
        self.current_ignored_role = str(role_id) if role_id else ""
        if not self._build_guided_modal():
            self._build_text_fallback()

    def _build_guided_modal(self) -> bool:
        if not _modal_label_available():
            return False
        try:
            rules = _make_modal_checkbox_group(
                "tts_rules",
                options=[
                    (
                        "Autor antes da frase",
                        "announce_author",
                        "O bot fala quem mandou antes do texto.",
                        self.current_announce_author,
                    ),
                ],
                min_values=0,
                max_values=1,
            )
            if rules is None:
                return False
            ok = _add_modal_label_item(
                self,
                "rules",
                text="Opções do TTS",
                description="Marque apenas o que deve ficar ligado no servidor.",
                component=rules,
            )
            role_input = _make_modal_text_input(
                label="Cargo ignorado",
                placeholder="Mencione, cole o ID/nome ou use 0 para remover",
                current=self.current_ignored_role,
                max_length=80,
                required=False,
            )
            ok = ok and _add_modal_label_item(
                self,
                "ignored_role",
                text="Cargo ignorado",
                description="Usuários com esse cargo não ativam TTS. Deixe vazio para manter.",
                component=role_input,
            )
            return bool(ok)
        except Exception as e:
            print(f"[tts_modal] regras guiadas falharam: {e!r}")
            try:
                self.clear_items()
            except Exception:
                pass
            return False

    def _build_text_fallback(self) -> None:
        _add_modal_text_input(
            self,
            "announce_author",
            label="Autor antes da frase",
            placeholder="sim para falar o autor antes; não para desligar",
            current="sim" if self.current_announce_author else "não",
            max_length=8,
        )
        _add_modal_text_input(
            self,
            "ignored_role",
            label="Cargo ignorado",
            placeholder="Mencione, cole o ID/nome ou use 0 para remover",
            current=self.current_ignored_role,
            max_length=80,
        )

    def _find_role(self, guild: discord.Guild | None, raw: str) -> discord.Role | None:
        if guild is None:
            return None
        value = str(raw or "").strip()
        if not value:
            return None
        match = re.fullmatch(r"<@&(\d+)>", value)
        role_id = int(match.group(1)) if match else int(value) if value.isdigit() else 0
        if role_id:
            return guild.get_role(role_id)
        lowered = value.casefold()
        for role in getattr(guild, "roles", []) or []:
            if str(getattr(role, "name", "")).casefold() == lowered:
                return role
        return None

    async def on_submit(self, interaction: discord.Interaction):
        updates: dict[str, object] = {}
        parts = []
        if hasattr(self, "rules"):
            values = set(_select_values(getattr(self, "rules", None)))
            enabled = "announce_author" in values
            updates["announce_author"] = enabled
            parts.append("autor antes da frase ligado" if enabled else "autor antes da frase desligado")
        else:
            text = _item_value(getattr(self, "announce_author", None)).lower()
            if text:
                enabled = text in {"sim", "s", "yes", "y", "true", "1", "on", "ativo", "ativado", "ligado"}
                updates["announce_author"] = enabled
                parts.append("autor antes da frase ligado" if enabled else "autor antes da frase desligado")
        raw_role = _item_value(getattr(self, "ignored_role", None))
        if raw_role:
            if raw_role.strip().lower() in {"0", "nenhum", "remover", "remove", "off", "desativar"}:
                updates["ignored_tts_role_id"] = 0
                parts.append("cargo ignorado removido")
            else:
                role = self._find_role(getattr(interaction, "guild", None), raw_role)
                if role is None:
                    await interaction.response.send_message(embed=self.cog._make_embed("Cargo não encontrado", "Use menção, ID ou nome exato do cargo.", ok=False), ephemeral=True)
                    return
                updates["ignored_tts_role_id"] = int(getattr(role, "id", 0) or 0)
                parts.append(f"cargo ignorado {getattr(role, 'mention', None) or getattr(role, 'name', 'cargo')}")
        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=True,
            updates=updates,
            history_label="as regras do TTS",
            history_value=", ".join(parts) if parts else "sem alterações",
            success_title="Regras atualizadas",
            success_description="Salvo: " + (", ".join(parts) if parts else "sem alterações") + ".",
        )


async def _send_settings_modal_with_fallback(interaction: discord.Interaction, guided_factory, fallback_factory, *, context: str) -> None:
    try:
        await interaction.response.send_modal(guided_factory())
        return
    except Exception as e:
        print(f"[tts_modal] modal guiado falhou em {context}: {e!r}")
        if interaction.response.is_done():
            try:
                await interaction.followup.send(
                    embed=guided_factory().cog._make_embed(
                        "Erro no formulário",
                        "Não consegui abrir esse formulário agora. Tente abrir o painel novamente.",
                        ok=False,
                    ),
                    ephemeral=True,
                )
            except Exception:
                pass
            return
    try:
        await interaction.response.send_modal(fallback_factory())
    except Exception as e:
        print(f"[tts_modal] fallback também falhou em {context}: {e!r}")
        try:
            await interaction.response.send_message("Não consegui abrir esse formulário agora.", ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send("Não consegui abrir esse formulário agora.", ephemeral=True)
            except Exception:
                pass


class TTSPublicLauncherSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Edge", description="Idioma, voz, velocidade e tom do Edge", value="edge", emoji="🔊"),
            discord.SelectOption(label="gTTS", description="Idioma do gTTS", value="gtts", emoji="🔤"),
            discord.SelectOption(label="Google", description="Idioma, voz e leitura Google", value="gcloud", emoji="☁️"),
            discord.SelectOption(label="Apelido", description="Nome que o bot fala por você", value="spoken_name", emoji="🪪"),
        ]
        super().__init__(placeholder="Abrir ajuste", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        panel = getattr(self, "view", None)
        if panel is None or interaction.guild is None:
            await interaction.response.send_message("Esse painel não está disponível agora.", ephemeral=True)
            return
        value = self.values[0]
        target_name = panel.cog._member_panel_name(interaction.user)
        if value == "edge":
            await _send_settings_modal_with_fallback(
                interaction,
                lambda: EdgeSettingsModal(panel.cog, getattr(interaction, "message", None), server=False, target_user_id=interaction.user.id, target_user_name=target_name),
                lambda: EdgeSettingsModal(panel.cog, getattr(interaction, "message", None), server=False, target_user_id=interaction.user.id, target_user_name=target_name, force_text_fallback=True),
                context="public-edge",
            )
        elif value == "gtts":
            await _send_settings_modal_with_fallback(
                interaction,
                lambda: GTTSSettingsModal(panel.cog, getattr(interaction, "message", None), server=False, target_user_id=interaction.user.id, target_user_name=target_name),
                lambda: GTTSSettingsModal(panel.cog, getattr(interaction, "message", None), server=False, target_user_id=interaction.user.id, target_user_name=target_name, force_text_fallback=True),
                context="public-gtts",
            )
        elif value == "gcloud":
            await _send_settings_modal_with_fallback(
                interaction,
                lambda: GoogleSettingsModal(panel.cog, getattr(interaction, "message", None), server=False, target_user_id=interaction.user.id, target_user_name=target_name),
                lambda: GoogleSettingsModal(panel.cog, getattr(interaction, "message", None), server=False, target_user_id=interaction.user.id, target_user_name=target_name, force_text_fallback=True),
                context="public-google",
            )
        elif value == "spoken_name":
            current_value = panel.cog._get_saved_spoken_name(interaction.guild.id, interaction.user.id)
            await interaction.response.send_modal(SpokenNameModal(panel.cog, getattr(interaction, "message", None), target_user_id=interaction.user.id, target_user_name=target_name, current_value=current_value))
        else:
            await interaction.response.send_message("Opção indisponível.", ephemeral=True)



_TTS_LAYOUT_VIEW_CLS = getattr(discord.ui, "LayoutView", discord.ui.View)


class _BaseTTSLayoutView(_TTS_LAYOUT_VIEW_CLS):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        timeout: float = 180,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        requested_timeout = max(1.0, float(timeout or TTS_PANEL_EXPIRE_AFTER_SECONDS))
        dispatch_timeout = max(requested_timeout, TTS_PANEL_DISPATCH_TIMEOUT_SECONDS)
        super().__init__(timeout=dispatch_timeout)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.message: discord.Message | None = None
        self.panel_kind: str = "user"
        self.target_user_id: int | None = target_user_id
        self.target_user_name: str | None = target_user_name
        self.expires_at_monotonic = time.monotonic() + requested_timeout

    def _is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at_monotonic

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._is_expired():
            try:
                message = await self.cog._build_expired_panel_message(self.guild_id, self.panel_kind)
            except Exception:
                message = (
                    "Essa interação já expirou porque esse comando ficou aberto por tempo demais.\n\n"
                    "Para continuar, abra o comando novamente e gere um painel novo."
                )
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return False

        if self.owner_id == 0:
            return True
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=self.cog._make_embed(
                    "Painel bloqueado",
                    "Só quem abriu esse painel pode usar esses botões e menus.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        print(
            f"[tts_panel_error] user={getattr(interaction.user, 'id', None)} "
            f"guild={getattr(interaction.guild, 'id', None)} "
            f"item={getattr(item, 'custom_id', None) or getattr(item, 'label', None) or type(item).__name__} "
            f"error={repr(error)}"
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    embed=self.cog._make_embed(
                        "Erro no painel",
                        "Essa interação falhou. Abra o painel novamente.",
                        ok=False,
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    embed=self.cog._make_embed(
                        "Erro no painel",
                        "Essa interação falhou. Abra o painel novamente.",
                        ok=False,
                    ),
                    ephemeral=True,
                )
        except Exception as e:
            print(f"[tts_panel_error] falha ao responder erro: {e!r}")

    async def on_timeout(self) -> None:
        pass


class TTSPublicLauncherView(_BaseTTSLayoutView):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, timeout: float = 300, history_text: str = ""):
        super().__init__(cog, owner_id, guild_id, timeout=timeout)
        self.panel_kind = "launcher"
        self.history_text = str(history_text or "").strip()
        self._rebuild_items()

    def is_components_v2_panel(self) -> bool:
        return bool(
            hasattr(discord.ui, "LayoutView")
            and isinstance(self, getattr(discord.ui, "LayoutView"))
            and hasattr(discord.ui, "Container")
            and hasattr(discord.ui, "TextDisplay")
            and hasattr(discord.ui, "ActionRow")
        )

    def _launcher_text(self) -> str:
        history = self.history_text or "• Nada recente."
        lines = [
            "### TTS",
            "**Como funciona**",
            "Cada prefixo escolhe um modo de voz.",
            "",
            "**Edge**",
            "Voz natural. Use: `,texto`",
            "",
            "**gTTS**",
            "Voz simples. Use: `.texto`",
            "",
            "**Google**",
            "Google Cloud. Use: `'texto`",
            "",
            "**Últimas alterações**",
            history,
        ]
        return "\n".join(lines).strip()

    def _make_button(self, label: str, callback: Callable[[discord.Interaction], object], *, emoji: str | None = None, style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> discord.ui.Button:
        button = discord.ui.Button(label=label, emoji=emoji, style=style)
        async def wrapped(interaction: discord.Interaction):
            result = callback(interaction)
            if inspect.isawaitable(result):
                await result
        button.callback = wrapped
        return button

    def _make_action_row(self, *buttons: discord.ui.Button):
        if self.is_components_v2_panel():
            row = discord.ui.ActionRow()
            for button in buttons:
                row.add_item(button)
            return row
        return list(buttons)

    def _add_control_row(self, container, *buttons: discord.ui.Button) -> None:
        row = self._make_action_row(*buttons)
        if self.is_components_v2_panel():
            container.add_item(row)
        else:
            for button in row:
                self.add_item(button)

    def _rebuild_items(self) -> None:
        try:
            self.clear_items()
        except Exception:
            pass

        launcher_select = TTSPublicLauncherSelect()

        if self.is_components_v2_panel():
            container = discord.ui.Container(discord.ui.TextDisplay(self._launcher_text()), accent_color=discord.Color.blurple())
            try:
                container.add_item(discord.ui.Separator(visible=True))
            except TypeError:
                container.add_item(discord.ui.Separator())
            row = discord.ui.ActionRow()
            row.add_item(launcher_select)
            container.add_item(row)
            self.add_item(container)
            return

        self.add_item(launcher_select)

    async def _open_my_tts(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Comando indisponível", "Esse painel só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return
        target_name = self.cog._member_panel_name(interaction.user)
        embed = await self.cog._build_settings_embed(
            interaction.guild.id,
            interaction.user.id,
            server=False,
            panel_kind="user",
            target_user_name=target_name,
            viewer_user_id=interaction.user.id,
        )
        view = self.cog._build_panel_view(
            interaction.user.id,
            interaction.guild.id,
            server=False,
            target_user_id=interaction.user.id,
            target_user_name=target_name,
        )
        msg = await self.cog._respond(interaction, embed=embed, view=view, ephemeral=True)
        view.message = msg

    async def _open_server_tts(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Comando indisponível", "Esse painel só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return
        if not getattr(getattr(interaction.user, "guild_permissions", None), "kick_members", False):
            await interaction.response.send_message(
                embed=self.cog._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para abrir o painel do servidor.", ok=False),
                ephemeral=True,
            )
            return
        embed = await self.cog._build_settings_embed(
            interaction.guild.id,
            interaction.user.id,
            server=True,
            panel_kind="server",
            viewer_user_id=interaction.user.id,
        )
        view = self.cog._build_panel_view(interaction.user.id, interaction.guild.id, server=True)
        msg = await self.cog._respond(interaction, embed=embed, view=view, ephemeral=True)
        view.message = msg

    async def _open_help(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=self.cog._make_embed(
                "Ajuda do TTS",
                "Edge, gTTS e Google são modos de voz. O prefixo é só o símbolo digitado antes da frase.\n\n"
                "Exemplos:\n"
                "• `,bom dia` usa Edge.\n"
                "• `.bom dia` usa gTTS.\n"
                "• `'bom dia` usa Google.",
                ok=True,
            ),
            ephemeral=True,
        )


class TTSReadingQuickView(_BaseTTSView):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        server: bool,
        source_panel_message: discord.Message | None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(cog, owner_id, guild_id, timeout=180, target_user_id=target_user_id, target_user_name=target_user_name)
        self.server = server
        self.source_panel_message = source_panel_message
        self.add_item(SpeedSelect(cog, server=server))
        self.add_item(PitchSelect(cog, server=server))
        for item in self.children:
            try:
                item.source_panel_message = source_panel_message
                item.target_user_id = target_user_id
                item.target_user_name = target_user_name
            except Exception:
                pass

    async def send(self, interaction: discord.Interaction):
        embed = self.cog._make_embed(
            "Leitura",
            "Muda velocidade e tom do modo Edge. O painel principal será atualizado depois de salvar.",
            ok=True,
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)


class TTSModeActionsView(_BaseTTSView):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        mode: str,
        server: bool,
        source_panel_message: discord.Message | None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(cog, owner_id, guild_id, timeout=180, target_user_id=target_user_id, target_user_name=target_user_name)
        self.mode = str(mode or "edge")
        self.server = server
        self.source_panel_message = source_panel_message
        self.panel_kind = "server" if server else "user"
        self._build_buttons()

    def _target_owner(self, interaction: discord.Interaction) -> int:
        return interaction.user.id if self.owner_id == 0 else self.owner_id

    def _make_button(self, label: str, callback: Callable[[discord.Interaction], object], *, emoji: str | None = None, style: discord.ButtonStyle = discord.ButtonStyle.secondary, row: int | None = None) -> discord.ui.Button:
        button = discord.ui.Button(label=label, emoji=emoji, style=style, row=row)
        async def wrapped(interaction: discord.Interaction):
            result = callback(interaction)
            if inspect.isawaitable(result):
                await result
        button.callback = wrapped
        return button

    def _build_buttons(self) -> None:
        try:
            self.clear_items()
        except Exception:
            pass
        self.add_item(TTSModeActionSelect(self.mode))
        self.add_item(self._make_button("Voltar", self._back_to_main_panel, emoji="⬅️", row=1))

    def _prefix_for_mode(self) -> str:
        # Texto curto de orientação. O painel principal mostra os prefixos reais
        # vindos do banco; aqui usamos os padrões para evitar consulta assíncrona
        # dentro de uma função de renderização simples.
        if self.mode == "edge":
            return ","
        if self.mode == "gcloud":
            return str(getattr(config, "GOOGLE_CLOUD_TTS_PREFIX", "'") or "'")
        return "."

    def _mode_title_description(self) -> tuple[str, str]:
        prefix = self._prefix_for_mode()
        target = "do servidor" if self.server else "seus"
        if self.mode == "edge":
            return "Edge", f"Usado quando a mensagem começa com `{prefix}texto`. Escolha no menu o que quer mudar."
        if self.mode == "gtts":
            return "gTTS", f"Usado quando a mensagem começa com `{prefix}texto`. Escolha no menu o que quer mudar."
        return "Google", f"Usado quando a mensagem começa com `{prefix}texto`. Escolha no menu o que quer mudar."

    async def send(self, interaction: discord.Interaction):
        title, description = self._mode_title_description()
        embed = self.cog._make_embed(title, description, ok=True)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=self, ephemeral=True, wait=True)
        else:
            await interaction.response.send_message(embed=embed, view=self, ephemeral=True)

    async def _open_edge_voice(self, interaction: discord.Interaction):
        view = _SimpleSelectView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            "Voz Edge",
            "Muda a voz usada pelo modo Edge.",
            VoiceRegionSelect(self.cog, server=self.server),
            source_panel_message=self.source_panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )
        await view.send(interaction)

    async def _open_edge_reading(self, interaction: discord.Interaction):
        await TTSReadingQuickView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            server=self.server,
            source_panel_message=self.source_panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        ).send(interaction)

    async def _open_gtts_language(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="Idioma gTTS",
            description="Muda o idioma usado pelo modo gTTS. Exemplos: `pt-br`, `en`, `es`, `fr`, `ja`.",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=LanguageHelpView(
                self.cog,
                self._target_owner(interaction),
                self.guild_id,
                server=self.server,
                source_panel_message=self.source_panel_message,
                target_user_id=self.target_user_id,
                target_user_name=self.target_user_name,
            ),
            ephemeral=True,
        )

    async def _open_google_language(self, interaction: discord.Interaction):
        current_target_user_id = int(self.target_user_id or interaction.user.id)
        current_value = self.cog._get_current_gcloud_language(self.guild_id, current_target_user_id, server=self.server)
        await self.cog._open_gcloud_language_picker(
            interaction,
            owner_id=self._target_owner(interaction),
            guild_id=self.guild_id,
            current_value=current_value,
            server=self.server,
            source_panel_message=self.source_panel_message,
            target_user_id=None if self.owner_id == 0 and self.target_user_id is None else self.target_user_id,
            target_user_name=self.target_user_name,
        )

    async def _open_google_voice(self, interaction: discord.Interaction):
        current_target_user_id = int(self.target_user_id or interaction.user.id)
        current_language = self.cog._get_current_gcloud_language(self.guild_id, current_target_user_id, server=self.server)
        current_value = self.cog._get_current_gcloud_voice(self.guild_id, current_target_user_id, server=self.server)
        await self.cog._open_gcloud_voice_picker(
            interaction,
            owner_id=self._target_owner(interaction),
            guild_id=self.guild_id,
            language_code=current_language,
            current_value=current_value,
            server=self.server,
            source_panel_message=self.source_panel_message,
            target_user_id=None if self.owner_id == 0 and self.target_user_id is None else self.target_user_id,
            target_user_name=self.target_user_name,
        )

    async def _open_google_reading(self, interaction: discord.Interaction):
        view = _SimpleSelectView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            "Leitura Google",
            "Muda velocidade e tom usados pelo modo Google.",
            GCloudSpeedSelect(self.cog, server=self.server),
            source_panel_message=self.source_panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )
        pitch_select = GCloudPitchSelect(self.cog, server=self.server)
        pitch_select.source_panel_message = self.source_panel_message
        pitch_select.target_user_id = self.target_user_id
        pitch_select.target_user_name = self.target_user_name
        view.add_item(pitch_select)
        await view.send(interaction)

    async def _back_to_main_panel(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        target_id = int(self.target_user_id or interaction.user.id)
        target_name = str(self.target_user_name or self.cog._member_panel_name(interaction.user))
        embed = await self.cog._build_settings_embed(
            interaction.guild.id,
            target_id if not self.server else interaction.user.id,
            server=self.server,
            panel_kind="server" if self.server else "user",
            target_user_name=target_name if not self.server else None,
            viewer_user_id=interaction.user.id,
        )
        view = self.cog._build_panel_view(
            self._target_owner(interaction),
            interaction.guild.id,
            server=self.server,
            target_user_id=None if self.server else target_id,
            target_user_name=None if self.server else target_name,
        )
        content, edit_embed, edit_view = self.cog._prepare_panel_payload(embed=embed, view=view)
        await interaction.response.edit_message(content=content, embed=edit_embed, view=edit_view)
        view.message = getattr(interaction, "message", None)






class PrefixTargetSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice"):
        self.cog = cog
        options = [
            discord.SelectOption(label="Bot", description="Símbolo usado nos comandos do bot. Exemplo: _panel", value="bot", emoji="🤖"),
            discord.SelectOption(label="gTTS", description="Símbolo antes da frase para usar gTTS. Exemplo: .bom dia", value="gtts", emoji="🔤"),
            discord.SelectOption(label="Edge", description="Símbolo antes da frase para usar Edge. Exemplo: ,bom dia", value="edge", emoji="🔊"),
            discord.SelectOption(label="Google", description="Símbolo antes da frase para usar Google. Exemplo: 'bom dia", value="gcloud", emoji="☁️"),
        ]
        super().__init__(placeholder="Escolha qual prefixo alterar", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        source_panel_message = getattr(getattr(self, "view", None), "source_panel_message", None) or getattr(interaction, "message", None)
        owner_id = getattr(getattr(self, "view", None), "owner_id", interaction.user.id)
        guild_id = getattr(getattr(self, "view", None), "guild_id", interaction.guild.id if interaction.guild else 0)
        value = self.values[0]
        if value == "bot":
            await interaction.response.send_modal(BotPrefixModal(self.cog, source_panel_message, owner_id, guild_id))
        elif value == "edge":
            await interaction.response.send_modal(EdgePrefixModal(self.cog, source_panel_message, owner_id, guild_id))
        elif value == "gcloud":
            await interaction.response.send_modal(GCloudPrefixModal(self.cog, source_panel_message, owner_id, guild_id))
        else:
            await interaction.response.send_modal(GTTSPrefixModal(self.cog, source_panel_message, owner_id, guild_id))


class TTSAdvancedActionsView(_BaseTTSView):
    def __init__(
        self,
        cog: "TTSVoice",
        owner_id: int,
        guild_id: int,
        *,
        server: bool,
        source_panel_message: discord.Message | None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        super().__init__(cog, owner_id, guild_id, timeout=180, target_user_id=target_user_id, target_user_name=target_user_name)
        self.server = server
        self.source_panel_message = source_panel_message
        self.panel_kind = "server" if server else "user"
        if not server:
            self.remove_item(self.ignored_role_button)
            # O apelido já fica no painel principal; aqui deixam só ajustes técnicos.
            self.remove_item(self.spoken_name_button)
        else:
            self.remove_item(self.spoken_name_button)

    def _target_owner(self, interaction: discord.Interaction) -> int:
        return interaction.user.id if self.owner_id == 0 else self.owner_id

    @discord.ui.button(label="Idioma gTTS", style=discord.ButtonStyle.secondary, emoji="🌐", row=0)
    async def gtts_language_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Idioma gTTS",
            description="Muda o idioma usado pelo modo gTTS. Exemplos: `pt-br`, `en`, `es`, `fr`, `ja`.",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=LanguageHelpView(
                self.cog,
                self._target_owner(interaction),
                self.guild_id,
                server=self.server,
                source_panel_message=self.source_panel_message,
                target_user_id=self.target_user_id,
                target_user_name=self.target_user_name,
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Idioma Google", style=discord.ButtonStyle.secondary, emoji="☁️", row=0)
    async def gcloud_language_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_target_user_id = int(self.target_user_id or interaction.user.id)
        current_value = self.cog._get_current_gcloud_language(self.guild_id, current_target_user_id, server=self.server)
        await self.cog._open_gcloud_language_picker(
            interaction,
            owner_id=self._target_owner(interaction),
            guild_id=self.guild_id,
            current_value=current_value,
            server=self.server,
            source_panel_message=self.source_panel_message,
            target_user_id=None if self.owner_id == 0 and self.target_user_id is None else self.target_user_id,
            target_user_name=self.target_user_name,
        )

    @discord.ui.button(label="Voz Google", style=discord.ButtonStyle.secondary, emoji="🎙️", row=0)
    async def gcloud_voice_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_target_user_id = int(self.target_user_id or interaction.user.id)
        current_language = self.cog._get_current_gcloud_language(self.guild_id, current_target_user_id, server=self.server)
        current_value = self.cog._get_current_gcloud_voice(self.guild_id, current_target_user_id, server=self.server)
        await self.cog._open_gcloud_voice_picker(
            interaction,
            owner_id=self._target_owner(interaction),
            guild_id=self.guild_id,
            language_code=current_language,
            current_value=current_value,
            server=self.server,
            source_panel_message=self.source_panel_message,
            target_user_id=None if self.owner_id == 0 and self.target_user_id is None else self.target_user_id,
            target_user_name=self.target_user_name,
        )

    @discord.ui.button(label="Leitura Google", style=discord.ButtonStyle.secondary, emoji="🎚️", row=1)
    async def gcloud_reading_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = _SimpleSelectView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            "Leitura Google",
            "Muda velocidade e tom usados pelo modo Google.",
            GCloudSpeedSelect(self.cog, server=self.server),
            source_panel_message=self.source_panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )
        pitch_select = GCloudPitchSelect(self.cog, server=self.server)
        pitch_select.source_panel_message = self.source_panel_message
        pitch_select.target_user_id = self.target_user_id
        pitch_select.target_user_name = self.target_user_name
        view.add_item(pitch_select)
        await view.send(interaction)

    @discord.ui.button(label="Cargo ignorado", style=discord.ButtonStyle.secondary, emoji="🚫", row=1)
    async def ignored_role_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = IgnoreRoleConfigView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            source_panel_message=self.source_panel_message,
        )
        await view.send(interaction)


    @discord.ui.button(label="Modo de TTS", style=discord.ButtonStyle.secondary, emoji="🎛️", row=2)
    async def mode_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = _SimpleSelectView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            "Modo de TTS",
            "Escolhe o motor padrão usado por comandos antigos. Os prefixos Edge, gTTS e Google continuam escolhendo o motor por mensagem.",
            ModeSelect(self.cog, server=self.server),
            source_panel_message=self.source_panel_message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )
        await view.send(interaction)

    @discord.ui.button(label="Apelido", style=discord.ButtonStyle.secondary, emoji="🪪", row=1)
    async def spoken_name_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_target_user_id = int(self.target_user_id or interaction.user.id)
        current_value = self.cog._get_saved_spoken_name(self.guild_id, current_target_user_id)
        await interaction.response.send_modal(
            SpokenNameModal(
                self.cog,
                self.source_panel_message,
                target_user_id=None if self.owner_id == 0 and self.target_user_id is None else self.target_user_id,
                target_user_name=self.target_user_name,
                current_value=current_value,
            )
        )


class TTSMainPanelSelect(discord.ui.Select):
    def __init__(self, *, server: bool):
        self.server = bool(server)
        if self.server:
            options = [
                discord.SelectOption(label="Prefixos", description="Símbolos do bot, Edge, gTTS e Google", value="prefixes", emoji="⌨️"),
                discord.SelectOption(label="Edge", description="Idioma, voz e leitura Edge padrão do servidor", value="edge", emoji="🔊"),
                discord.SelectOption(label="gTTS", description="Idioma gTTS padrão do servidor", value="gtts", emoji="🔤"),
                discord.SelectOption(label="Google", description="Idioma, voz e leitura Google padrão", value="gcloud", emoji="☁️"),
                discord.SelectOption(label="Regras", description="Autor antes da frase e cargo ignorado", value="rules", emoji="☑️"),
            ]
            placeholder = "Escolha o ajuste do servidor"
        else:
            options = [
                discord.SelectOption(label="Edge", description="Voz natural: idioma, voz, velocidade e tom", value="edge", emoji="🔊"),
                discord.SelectOption(label="gTTS", description="Voz simples: idioma usado no gTTS", value="gtts", emoji="🔤"),
                discord.SelectOption(label="Google", description="Google Cloud: idioma, voz e leitura", value="gcloud", emoji="☁️"),
                discord.SelectOption(label="Apelido", description="Nome que o bot fala por você", value="spoken_name", emoji="🪪"),
            ]
            placeholder = "Escolha o que editar"
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        panel = getattr(self, "view", None)
        if panel is None:
            await interaction.response.send_message("Esse painel não está disponível agora.", ephemeral=True)
            return
        value = self.values[0]
        if value == "edge":
            await panel._open_edge_panel(interaction)
        elif value == "gtts":
            await panel._open_gtts_panel(interaction)
        elif value == "gcloud":
            await panel._open_google_panel(interaction)
        elif value == "spoken_name":
            await panel._open_spoken_name_modal(interaction)
        elif value == "prefixes":
            await panel._open_prefixes_panel(interaction)
        elif value == "rules":
            await panel._open_rules_panel(interaction)
        else:
            await interaction.response.send_message("Opção indisponível.", ephemeral=True)


class TTSModeActionSelect(discord.ui.Select):
    def __init__(self, mode: str):
        self.mode = str(mode or "edge")
        if self.mode == "edge":
            options = [
                discord.SelectOption(label="Voz Edge", description="Escolhe a voz usada no prefixo Edge", value="edge_voice", emoji="🎙️"),
                discord.SelectOption(label="Leitura Edge", description="Velocidade e tom do Edge", value="edge_reading", emoji="🎚️"),
            ]
            placeholder = "Editar Edge"
        elif self.mode == "gtts":
            options = [
                discord.SelectOption(label="Idioma gTTS", description="Idioma usado no prefixo gTTS", value="gtts_language", emoji="🌐"),
            ]
            placeholder = "Editar gTTS"
        else:
            options = [
                discord.SelectOption(label="Idioma Google", description="Idioma da voz Google", value="google_language", emoji="☁️"),
                discord.SelectOption(label="Voz Google", description="Voz do Google Cloud", value="google_voice", emoji="🎙️"),
                discord.SelectOption(label="Leitura Google", description="Velocidade e tom do Google", value="google_reading", emoji="🎚️"),
            ]
            placeholder = "Editar Google"
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        panel = getattr(self, "view", None)
        if panel is None:
            await interaction.response.send_message("Esse painel não está disponível agora.", ephemeral=True)
            return
        value = self.values[0]
        if value == "edge_voice":
            await panel._open_edge_voice(interaction)
        elif value == "edge_reading":
            await panel._open_edge_reading(interaction)
        elif value == "gtts_language":
            await panel._open_gtts_language(interaction)
        elif value == "google_language":
            await panel._open_google_language(interaction)
        elif value == "google_voice":
            await panel._open_google_voice(interaction)
        elif value == "google_reading":
            await panel._open_google_reading(interaction)
        else:
            await interaction.response.send_message("Opção indisponível.", ephemeral=True)


class TTSMainPanelView(_BaseTTSLayoutView):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, server: bool = False, timeout: float = 180, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__(cog, owner_id, guild_id, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)
        self.server = server
        self.panel_kind = "server" if server else "user"
        self._panel_embed: discord.Embed | None = None
        self._fallback_buttons_ready = False
        self._rebuild_items()

    def _target_owner(self, interaction: discord.Interaction) -> int:
        return interaction.user.id if self.owner_id == 0 else self.owner_id

    def is_components_v2_panel(self) -> bool:
        return bool(
            hasattr(discord.ui, "LayoutView")
            and isinstance(self, getattr(discord.ui, "LayoutView"))
            and hasattr(discord.ui, "Container")
            and hasattr(discord.ui, "TextDisplay")
            and hasattr(discord.ui, "ActionRow")
        )

    def set_panel_embed(self, embed: discord.Embed | None) -> None:
        self._panel_embed = embed
        self._rebuild_items()

    def _panel_text(self) -> str:
        if self._panel_embed is None:
            return "### TTS do servidor\nCarregando painel." if self.server else "### TTS\nCarregando painel."
        try:
            return build_settings_panel_text_from_embed(self._panel_embed, server=self.server)
        except Exception as e:
            print(f"[tts_panel] falha ao renderizar painel v2: {e!r}")
            return str(getattr(self._panel_embed, "description", "") or "Painel de TTS")[:4000]

    def _make_button(self, label: str, callback: Callable[[discord.Interaction], object], *, emoji: str | None = None, style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> discord.ui.Button:
        button = discord.ui.Button(label=label, emoji=emoji, style=style)
        async def wrapped(interaction: discord.Interaction):
            result = callback(interaction)
            if inspect.isawaitable(result):
                await result
        button.callback = wrapped
        return button

    def _make_action_row(self, *buttons: discord.ui.Button):
        if self.is_components_v2_panel():
            row = discord.ui.ActionRow()
            for button in buttons:
                row.add_item(button)
            return row
        return list(buttons)

    def _add_control_row(self, container, *buttons: discord.ui.Button) -> None:
        row = self._make_action_row(*buttons)
        if self.is_components_v2_panel():
            container.add_item(row)
        else:
            for button in row:
                self.add_item(button)

    def _rebuild_items(self) -> None:
        try:
            self.clear_items()
        except Exception:
            pass

        if self.is_components_v2_panel():
            container = discord.ui.Container(
                discord.ui.TextDisplay(self._panel_text()),
                accent_color=discord.Color.blurple(),
            )
            try:
                container.add_item(discord.ui.Separator(visible=True))
            except TypeError:
                container.add_item(discord.ui.Separator())

            row = discord.ui.ActionRow()
            row.add_item(TTSMainPanelSelect(server=self.server))
            container.add_item(row)
            self.add_item(container)
            return

        # Fallback se a lib em produção ainda não tiver LayoutView/Components V2.
        self.add_item(TTSMainPanelSelect(server=self.server))

    async def _open_mode_panel(self, interaction: discord.Interaction, mode: str):
        print(f"[tts_panel] mode_select | mode={mode} user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        panel_message = getattr(interaction, "message", None)
        target_user_id = self.target_user_id
        target_user_name = self.target_user_name
        if not self.server and target_user_id is None:
            target_user_id = interaction.user.id
            target_user_name = self.cog._member_panel_name(interaction.user)

        if mode == "edge":
            await _send_settings_modal_with_fallback(
                interaction,
                lambda: EdgeSettingsModal(self.cog, panel_message, server=self.server, target_user_id=target_user_id, target_user_name=target_user_name),
                lambda: EdgeSettingsModal(self.cog, panel_message, server=self.server, target_user_id=target_user_id, target_user_name=target_user_name, force_text_fallback=True),
                context="panel-edge",
            )
        elif mode == "gtts":
            await _send_settings_modal_with_fallback(
                interaction,
                lambda: GTTSSettingsModal(self.cog, panel_message, server=self.server, target_user_id=target_user_id, target_user_name=target_user_name),
                lambda: GTTSSettingsModal(self.cog, panel_message, server=self.server, target_user_id=target_user_id, target_user_name=target_user_name, force_text_fallback=True),
                context="panel-gtts",
            )
        else:
            await _send_settings_modal_with_fallback(
                interaction,
                lambda: GoogleSettingsModal(self.cog, panel_message, server=self.server, target_user_id=target_user_id, target_user_name=target_user_name),
                lambda: GoogleSettingsModal(self.cog, panel_message, server=self.server, target_user_id=target_user_id, target_user_name=target_user_name, force_text_fallback=True),
                context="panel-google",
            )


    async def _open_edge_panel(self, interaction: discord.Interaction):
        await self._open_mode_panel(interaction, "edge")

    async def _open_gtts_panel(self, interaction: discord.Interaction):
        await self._open_mode_panel(interaction, "gtts")

    async def _open_google_panel(self, interaction: discord.Interaction):
        await self._open_mode_panel(interaction, "gcloud")

    async def _open_voice_panel(self, interaction: discord.Interaction):
        print(f"[tts_panel] voice_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        view = _SimpleSelectView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            "Voz",
            "Escolha a região e depois a voz. Os nomes técnicos ficam só dentro desta lista.",
            VoiceRegionSelect(self.cog, server=self.server),
            source_panel_message=interaction.message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )
        await view.send(interaction)

    async def _open_reading_panel(self, interaction: discord.Interaction):
        print(f"[tts_panel] reading_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        await TTSReadingQuickView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            server=self.server,
            source_panel_message=interaction.message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        ).send(interaction)

    async def _open_spoken_name_modal(self, interaction: discord.Interaction):
        print(f"[tts_panel] spoken_name_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        current_target_user_id = int(self.target_user_id or interaction.user.id)
        current_value = self.cog._get_saved_spoken_name(self.guild_id, current_target_user_id)
        await interaction.response.send_modal(
            SpokenNameModal(
                self.cog,
                interaction.message,
                target_user_id=None if self.owner_id == 0 and self.target_user_id is None else self.target_user_id,
                target_user_name=self.target_user_name,
                current_value=current_value,
            )
        )

    async def _open_prefixes_panel(self, interaction: discord.Interaction):
        if not self.server:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Indisponível", "Prefixos são ajustes do servidor.", ok=False),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ServerPrefixesModal(self.cog, getattr(interaction, "message", None)))

    async def _open_rules_panel(self, interaction: discord.Interaction):
        if not self.server:
            return await self._open_advanced_panel(interaction)
        await interaction.response.send_modal(TTSServerRulesModal(self.cog, getattr(interaction, "message", None)))

    async def _open_advanced_panel(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=self.cog._make_embed(
                "Opção removida",
                "Esse painel foi simplificado. Use Edge, gTTS, Google, Apelido ou o comando separado do TTS do servidor.",
                ok=True,
            ),
            ephemeral=True,
        )


class TTSStatusView(_BaseTTSView):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, timeout: float = 180, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__(cog, owner_id, guild_id, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)
        self.panel_kind = "status"

    def attach_message(self, message: discord.Message | None) -> None:
        self.message = message
        self.cog._register_status_view(self)

    async def refresh_from_config_change(self) -> None:
        if self.message is None or self.is_finished():
            return
        try:
            guild = self.cog.bot.get_guild(self.guild_id)
            if guild is None:
                self.cog._unregister_status_view(self)
                return
            target_user_id = int(self.target_user_id or self.owner_id or 0)
            member = guild.get_member(target_user_id) if target_user_id else None
            target_user_name = str(self.target_user_name or self.cog._member_panel_name(member))
            refreshed = await self.cog._build_status_embed(
                self.guild_id,
                target_user_id,
                viewer_user_id=self.owner_id,
                target_user_name=target_user_name,
                public=False,
            )
            await self.message.edit(embed=refreshed, view=self)
        except discord.NotFound:
            self.cog._unregister_status_view(self)
            self.stop()
        except Exception as e:
            print(f"[tts_status_refresh] falha ao atualizar status: {e!r}")

    async def on_timeout(self) -> None:
        self.cog._unregister_status_view(self)
        await super().on_timeout()

    @discord.ui.button(label="Abrir painel", style=discord.ButtonStyle.secondary, emoji="⚙️", row=0)
    async def open_panel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Comando indisponível", "Esse botão só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return

        target_user_id = int(self.target_user_id or interaction.user.id)
        target_user_name = str(self.target_user_name or self.cog._member_panel_name(interaction.user))
        embed = await self.cog._build_settings_embed(
            interaction.guild.id,
            target_user_id,
            server=False,
            panel_kind="user",
            target_user_name=target_user_name,
            viewer_user_id=interaction.user.id,
        )
        view = self.cog._build_panel_view(
            interaction.user.id,
            interaction.guild.id,
            server=False,
            target_user_id=target_user_id,
            target_user_name=target_user_name,
        )
        msg = await self.cog._respond(interaction, embed=embed, view=view, ephemeral=True)
        view.message = msg

    @discord.ui.button(label="Resetar para o padrão do servidor", style=discord.ButtonStyle.danger, emoji="♻️", row=0)
    async def reset_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self.cog._make_embed("Comando indisponível", "Esse botão só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return

        db = self.cog._get_db()
        if db is None or not hasattr(db, "reset_user_tts"):
            await interaction.response.send_message(
                embed=self.cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora para resetar as suas configurações.", ok=False),
                ephemeral=True,
            )
            return

        target_user_id = int(self.target_user_id or interaction.user.id)
        target_user_name = str(self.target_user_name or self.cog._member_panel_name(interaction.user))
        history_entry = f"{self.cog._panel_actor_name(interaction)} resetou as próprias configurações de TTS para os padrões do servidor"
        await self.cog._reset_user_tts_and_refresh(interaction.guild.id, target_user_id, history_entry=history_entry)

        refreshed = await self.cog._build_status_embed(
            interaction.guild.id,
            target_user_id,
            viewer_user_id=interaction.user.id,
            target_user_name=target_user_name,
            public=False,
        )
        await interaction.response.edit_message(embed=refreshed, view=self)
        await interaction.followup.send(
            embed=self.cog._make_embed("Configurações resetadas", f"As suas configurações de TTS agora seguem os padrões do servidor.", ok=True),
            ephemeral=True,
        )

class TTSTogglePanelView(_BaseTTSView):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, timeout: float = 180):
        super().__init__(cog, owner_id, guild_id, timeout=timeout)
        self.panel_kind = "toggle"

    def _target_owner(self, interaction: discord.Interaction) -> int:
        return interaction.user.id if self.owner_id == 0 else self.owner_id

    @discord.ui.button(label="Auto leave", style=discord.ButtonStyle.secondary, emoji="⏏️", row=1)
    async def auto_leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _SimpleSelectView(self.cog, self._target_owner(interaction), self.guild_id, "Auto leave", "Escolha se o bot deve sair da call quando ficar sozinho ou só com bots.", ToggleSelect(self.cog, "auto_leave")).send(interaction)
