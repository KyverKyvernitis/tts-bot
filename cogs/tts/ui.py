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
from .utils.embed import build_settings_panel_text_from_embed

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


def _maybe_add_radio_group(modal, attr_name: str, *, label: str, options: list[tuple[str, str, str]], default_value: str) -> bool:
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
    except Exception:
        return False


def _maybe_add_checkbox_group(modal, attr_name: str, *, options: list[tuple[str, str, str, bool]], min_values: int = 0, max_values: int | None = None) -> bool:
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
    except Exception:
        return False


def _make_optional_select(*, placeholder: str, options: list[discord.SelectOption]):
    kwargs = dict(placeholder=placeholder, min_values=0, max_values=1, options=options[:25])
    try:
        return discord.ui.Select(required=False, **kwargs)
    except TypeError:
        return discord.ui.Select(**kwargs)


def _top_edge_voice_options(cog: "TTSVoice", current: str = "") -> list[discord.SelectOption]:
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
    voices = []
    seen = set()
    if current:
        preferred.insert(0, current)
    for voice in preferred + list(cog.edge_voice_cache or []) + sorted(cog.edge_voice_names or []):
        voice = str(voice or "").strip()
        if not voice or voice in seen:
            continue
        if voice.startswith("pt-BR") or not voices:
            seen.add(voice)
            voices.append(voice)
        if len(voices) >= 25:
            break
    if not voices:
        voices = [current or "pt-BR-FranciscaNeural"]
    return [discord.SelectOption(label=_shorten(v, 100), description="Voz Edge", value=v) for v in voices[:25]]


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


def _top_gcloud_voice_options(cog: "TTSVoice", current: str = "", language: str = "pt-BR") -> list[discord.SelectOption]:
    preferred = [
        current or "",
        "pt-BR-Standard-A",
        "pt-BR-Standard-B",
        "pt-BR-Standard-C",
        "pt-BR-Wavenet-A",
        "pt-BR-Wavenet-B",
        "pt-BR-Wavenet-C",
        "pt-BR-Neural2-A",
        "pt-BR-Neural2-B",
        "pt-BR-Neural2-C",
    ]
    seen = set()
    options = []
    for voice in preferred:
        voice = str(voice or "").strip()
        if not voice or voice in seen:
            continue
        seen.add(voice)
        options.append(discord.SelectOption(label=_shorten(voice, 100), description="Voz Google Cloud", value=voice))
    return options or [discord.SelectOption(label="pt-BR-Standard-A", description="Voz Google Cloud", value="pt-BR-Standard-A")]


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
            embed=cog._make_embed("Nada para salvar", "Nenhum ajuste foi alterado.", ok=True),
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
        history_user_id = effective_user_id
        panel_kind = "user"

    state = cog._public_panel_states.get(message_id or 0, {}) if message_id else {}
    should_edit_panel = bool(panel_message is not None and state.get("panel_kind") != "launcher")
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
    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        user_id = int(target_user_id or 0)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        current_voice = _current_tts_value(cog, guild_id, user_id, "voice", str(getattr(config, "EDGE_TTS_VOICE", "pt-BR-FranciscaNeural") or "pt-BR-FranciscaNeural"), server=server)
        current_rate = _current_tts_value(cog, guild_id, user_id, "rate", "+0%", server=server)
        current_pitch = _current_tts_value(cog, guild_id, user_id, "pitch", "+0Hz", server=server)
        self.voice_select = _make_optional_select(placeholder="Voz Edge", options=_top_edge_voice_options(cog, current_voice))
        self.add_item(self.voice_select)
        if not _maybe_add_radio_group(
            self,
            "rate_group",
            label="Velocidade Edge",
            default_value=current_rate,
            options=[
                ("Mais lenta", "-25%", "Reduz a velocidade"),
                ("Normal", "+0%", "Velocidade padrão"),
                ("Mais rápida", "+25%", "Aumenta a velocidade"),
            ],
        ):
            self.rate_input = discord.ui.TextInput(label="Velocidade Edge", placeholder="Ex.: +0%, -25%, +25%", required=False, max_length=8, default=str(current_rate or "+0%"))
            self.add_item(self.rate_input)
        if not _maybe_add_radio_group(
            self,
            "pitch_group",
            label="Tom Edge",
            default_value=current_pitch,
            options=[
                ("Mais grave", "-25Hz", "Voz mais grave"),
                ("Normal", "+0Hz", "Tom padrão"),
                ("Mais agudo", "+25Hz", "Voz mais aguda"),
            ],
        ):
            self.pitch_input = discord.ui.TextInput(label="Tom Edge", placeholder="Ex.: +0Hz, -25Hz, +25Hz", required=False, max_length=8, default=str(current_pitch or "+0Hz"))
            self.add_item(self.pitch_input)

    async def on_submit(self, interaction: discord.Interaction):
        voice_values = _select_values(self.voice_select)
        updates: dict[str, object] = {}
        parts: list[str] = []
        if voice_values:
            voice = voice_values[0]
            if voice not in self.cog.edge_voice_names and voice not in self.cog.edge_voice_cache:
                await interaction.response.send_message(embed=self.cog._make_embed("Voz inválida", "Essa voz Edge não foi encontrada.", ok=False), ephemeral=True)
                return
            updates["voice"] = voice
            parts.append(f"voz {voice}")
        rate = _item_value(getattr(self, "rate_group", None), "") or _item_value(getattr(self, "rate_input", None), "")
        pitch = _item_value(getattr(self, "pitch_group", None), "") or _item_value(getattr(self, "pitch_input", None), "")
        if rate:
            normalized = self.cog._normalize_rate_value(rate)
            if normalized is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Velocidade inválida", "Use valores como `+0%`, `-25%` ou `+25%`.", ok=False), ephemeral=True)
                return
            updates["rate"] = normalized
            parts.append(f"velocidade {normalized}")
        if pitch:
            normalized = self.cog._normalize_pitch_value(pitch)
            if normalized is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Tom inválido", "Use valores como `+0Hz`, `-25Hz` ou `+25Hz`.", ok=False), ephemeral=True)
                return
            updates["pitch"] = normalized
            parts.append(f"tom {normalized}")
        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=self.server,
            updates=updates,
            history_label="o Edge" if self.server else "o próprio Edge",
            history_value=", ".join(parts) if parts else "sem alterações",
            success_title="Edge atualizado",
            success_description="Salvo: " + (", ".join(parts) if parts else "sem alterações") + ".",
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class GTTSSettingsModal(discord.ui.Modal, title="Editar gTTS"):
    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        user_id = int(target_user_id or 0)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        current = _current_tts_value(cog, guild_id, user_id, "language", "pt-br", server=server)
        self.language_select = _make_optional_select(placeholder="Idioma gTTS", options=_top_gtts_language_options(cog, current))
        self.add_item(self.language_select)
        self.custom_language = discord.ui.TextInput(label="Outro idioma gTTS", placeholder="Opcional. Ex.: pt-br, en, es, fr, ja", required=False, max_length=10, default="")
        self.add_item(self.custom_language)

    async def on_submit(self, interaction: discord.Interaction):
        selected = _select_values(self.language_select)
        value = str(self.custom_language or "").strip() or (selected[0] if selected else "")
        if not value:
            updates = {}
        else:
            if value not in self.cog.gtts_languages:
                await interaction.response.send_message(embed=self.cog._make_embed("Idioma inválido", "Esse código não está na lista do gTTS. Exemplos: `pt-br`, `en`, `es`.", ok=False), ephemeral=True)
                return
            updates = {"language": value}
        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=self.server,
            updates=updates,
            history_label="o idioma do modo gTTS" if self.server else "o próprio idioma do gTTS",
            history_value=value,
            success_title="gTTS atualizado",
            success_description=f"Salvo: idioma `{value}`." if value else "Nenhum idioma foi alterado.",
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class GoogleSettingsModal(discord.ui.Modal, title="Editar Google"):
    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        user_id = int(target_user_id or 0)
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        current_language = cog._get_current_gcloud_language(guild_id, user_id, server=server) if guild_id else str(getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR")
        current_voice = cog._get_current_gcloud_voice(guild_id, user_id, server=server) if guild_id else str(getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A")
        current_rate = _current_tts_value(cog, guild_id, user_id, "gcloud_rate", "1.0", server=server)
        current_pitch = _current_tts_value(cog, guild_id, user_id, "gcloud_pitch", "0.0", server=server)
        self.language_select = _make_optional_select(placeholder="Idioma Google", options=_top_gcloud_language_options(current_language))
        self.add_item(self.language_select)
        self.voice_select = _make_optional_select(placeholder="Voz Google", options=_top_gcloud_voice_options(cog, current_voice, current_language))
        self.add_item(self.voice_select)
        if not _maybe_add_radio_group(
            self,
            "rate_group",
            label="Velocidade Google",
            default_value=str(current_rate),
            options=[
                ("Mais lenta", "0.85", "Reduz a velocidade"),
                ("Normal", "1.0", "Velocidade padrão"),
                ("Mais rápida", "1.15", "Aumenta a velocidade"),
            ],
        ):
            self.rate_input = discord.ui.TextInput(label="Velocidade Google", placeholder="Ex.: 1.0, 0.85, 1.15", required=False, max_length=8, default=str(current_rate or "1.0"))
            self.add_item(self.rate_input)
        if not _maybe_add_radio_group(
            self,
            "pitch_group",
            label="Tom Google",
            default_value=str(current_pitch),
            options=[
                ("Mais grave", "-2.0", "Voz mais grave"),
                ("Normal", "0.0", "Tom padrão"),
                ("Mais agudo", "2.0", "Voz mais aguda"),
            ],
        ):
            self.pitch_input = discord.ui.TextInput(label="Tom Google", placeholder="Ex.: 0.0, -2.0, 2.0", required=False, max_length=8, default=str(current_pitch or "0.0"))
            self.add_item(self.pitch_input)

    async def on_submit(self, interaction: discord.Interaction):
        updates: dict[str, object] = {}
        parts: list[str] = []
        lang_values = _select_values(self.language_select)
        voice_values = _select_values(self.voice_select)
        if lang_values:
            value, error = self.cog._validate_gcloud_language_input(lang_values[0])
            if error or value is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Idioma inválido", error or "Idioma inválido.", ok=False), ephemeral=True)
                return
            updates["gcloud_language"] = value
            parts.append(f"idioma {value}")
        if voice_values:
            value, error = self.cog._validate_gcloud_voice_input(voice_values[0])
            if error or value is None:
                await interaction.response.send_message(embed=self.cog._make_embed("Voz inválida", error or "Voz inválida.", ok=False), ephemeral=True)
                return
            updates["gcloud_voice"] = value
            parts.append(f"voz {value}")
        rate = _item_value(getattr(self, "rate_group", None), "") or _item_value(getattr(self, "rate_input", None), "")
        pitch = _item_value(getattr(self, "pitch_group", None), "") or _item_value(getattr(self, "pitch_input", None), "")
        if rate:
            value = self.cog._normalize_gcloud_rate_value(rate)
            updates["gcloud_rate"] = value
            parts.append(f"velocidade {value}")
        if pitch:
            value = self.cog._normalize_gcloud_pitch_value(pitch)
            updates["gcloud_pitch"] = value
            parts.append(f"tom {value}")
        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=self.server,
            updates=updates,
            history_label="o Google Cloud" if self.server else "o próprio Google Cloud",
            history_value=", ".join(parts) if parts else "sem alterações",
            success_title="Google atualizado",
            success_description="Salvo: " + (", ".join(parts) if parts else "sem alterações") + ".",
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        )


class MoreTTSOptionsModal(discord.ui.Modal, title="Mais opções"):
    def __init__(self, cog: "TTSVoice", panel_message: discord.Message | None, *, server: bool, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.server = bool(server)
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        current = "edge"
        guild_id = int(getattr(panel_message, "guild", None).id) if getattr(panel_message, "guild", None) else 0
        if guild_id:
            current = _current_tts_value(cog, guild_id, int(target_user_id or 0), "engine", "edge", server=server)
        if not _maybe_add_radio_group(
            self,
            "mode_group",
            label="Modo de TTS",
            default_value=current,
            options=[
                ("Edge", "edge", "Modo natural"),
                ("gTTS", "gtts", "Modo simples"),
                ("Google", "gcloud", "Google Cloud"),
            ],
        ):
            self.mode_input = discord.ui.TextInput(label="Modo de TTS", placeholder="edge, gtts ou gcloud", required=False, max_length=12, default=str(current or "edge"))
            self.add_item(self.mode_input)

    async def on_submit(self, interaction: discord.Interaction):
        mode = _item_value(getattr(self, "mode_group", None), "") or _item_value(getattr(self, "mode_input", None), "")
        if not mode:
            updates = {}
            value = ""
        else:
            value = validate_mode(mode)
            updates = {"engine": value}
        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=self.server,
            updates=updates,
            history_label="o modo padrão do servidor" if self.server else "o próprio modo",
            history_value=value,
            success_title="Modo de TTS atualizado",
            success_description=f"Salvo: modo `{value}`." if value else "Nenhum modo foi alterado.",
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
            value = str(getattr(self, field_name) or "").strip()
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
            history_value=", ".join(parts),
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
        announce = bool((defaults or {}).get("announce_author"))
        if not _maybe_add_checkbox_group(
            self,
            "rules_group",
            options=[("Autor antes da frase", "announce_author", "Fala quem enviou a mensagem", announce)],
            min_values=0,
            max_values=1,
        ):
            self.announce_author = discord.ui.TextInput(label="Autor antes da frase", placeholder="sim ou não", required=False, max_length=8, default="sim" if announce else "não")
            self.add_item(self.announce_author)
        role_select_cls = getattr(discord.ui, "RoleSelect", None)
        if role_select_cls is not None:
            try:
                self.role_select = role_select_cls(placeholder="Cargo ignorado", min_values=0, max_values=1, required=False)
                self.add_item(self.role_select)
            except Exception:
                self.role_select = None
        else:
            self.role_select = None

    async def on_submit(self, interaction: discord.Interaction):
        updates: dict[str, object] = {}
        selected_rules = set(_select_values(getattr(self, "rules_group", None)))
        if hasattr(self, "rules_group"):
            updates["announce_author"] = "announce_author" in selected_rules
        else:
            text = str(getattr(self, "announce_author", "") or "").strip().lower()
            if text:
                updates["announce_author"] = text in {"sim", "s", "yes", "y", "true", "1", "on", "ativo", "ativado"}
        role_values = getattr(getattr(self, "role_select", None), "values", None) or []
        if role_values:
            role = role_values[0]
            updates["ignored_tts_role_id"] = int(getattr(role, "id", 0) or 0)
            role_text = getattr(role, "mention", None) or getattr(role, "name", "cargo")
        else:
            role_text = ""
        parts = []
        if "announce_author" in updates:
            parts.append("autor antes da frase ligado" if updates["announce_author"] else "autor antes da frase desligado")
        if role_text:
            parts.append(f"cargo ignorado {role_text}")
        await _save_tts_modal_updates(
            self.cog,
            interaction,
            source_panel_message=self.panel_message,
            server=True,
            updates=updates,
            history_label="as regras do TTS",
            history_value=", ".join(parts),
            success_title="Regras atualizadas",
            success_description="Salvo: " + (", ".join(parts) if parts else "sem alterações") + ".",
        )


class TTSPublicLauncherSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Edge", description="Voz, velocidade e tom do Edge", value="edge", emoji="🔊"),
            discord.SelectOption(label="gTTS", description="Idioma do gTTS", value="gtts", emoji="🔤"),
            discord.SelectOption(label="Google", description="Idioma, voz e leitura Google", value="gcloud", emoji="☁️"),
            discord.SelectOption(label="Apelido", description="Nome que o bot fala por você", value="spoken_name", emoji="🪪"),
            discord.SelectOption(label="Mais opções", description="Modo de TTS e ajustes extras", value="advanced", emoji="⚙️"),
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
            await interaction.response.send_modal(EdgeSettingsModal(panel.cog, getattr(interaction, "message", None), server=False, target_user_id=interaction.user.id, target_user_name=target_name))
        elif value == "gtts":
            await interaction.response.send_modal(GTTSSettingsModal(panel.cog, getattr(interaction, "message", None), server=False, target_user_id=interaction.user.id, target_user_name=target_name))
        elif value == "gcloud":
            await interaction.response.send_modal(GoogleSettingsModal(panel.cog, getattr(interaction, "message", None), server=False, target_user_id=interaction.user.id, target_user_name=target_name))
        elif value == "spoken_name":
            current_value = panel.cog._get_saved_spoken_name(interaction.guild.id, interaction.user.id)
            await interaction.response.send_modal(SpokenNameModal(panel.cog, getattr(interaction, "message", None), target_user_id=interaction.user.id, target_user_name=target_name, current_value=current_value))
        else:
            await interaction.response.send_modal(MoreTTSOptionsModal(panel.cog, getattr(interaction, "message", None), server=False, target_user_id=interaction.user.id, target_user_name=target_name))



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
        lines = [
            "### TTS",
            "Abra seus ajustes abaixo. O painel é público, mas cada pessoa altera só o próprio TTS.",
            "",
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
        ]
        if self.history_text:
            lines.extend(["", "**Últimas alterações**", self.history_text])
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
                discord.SelectOption(label="Edge", description="Voz e leitura Edge padrão do servidor", value="edge", emoji="🔊"),
                discord.SelectOption(label="gTTS", description="Idioma gTTS padrão do servidor", value="gtts", emoji="🔤"),
                discord.SelectOption(label="Google", description="Idioma, voz e leitura Google padrão", value="gcloud", emoji="☁️"),
                discord.SelectOption(label="Regras", description="Autor antes da frase e cargo ignorado", value="rules", emoji="☑️"),
                discord.SelectOption(label="Mais opções", description="Modo de TTS e ajustes menos usados", value="advanced", emoji="⚙️"),
            ]
            placeholder = "Escolha o ajuste do servidor"
        else:
            options = [
                discord.SelectOption(label="Edge", description="Voz natural: voz, velocidade e tom", value="edge", emoji="🔊"),
                discord.SelectOption(label="gTTS", description="Voz simples: idioma usado no gTTS", value="gtts", emoji="🔤"),
                discord.SelectOption(label="Google", description="Google Cloud: idioma, voz e leitura", value="gcloud", emoji="☁️"),
                discord.SelectOption(label="Apelido", description="Nome que o bot fala por você", value="spoken_name", emoji="🪪"),
                discord.SelectOption(label="Mais opções", description="Modo de TTS e ajustes menos usados", value="advanced", emoji="⚙️"),
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
            await panel._open_advanced_panel(interaction)


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
        if mode == "edge":
            await interaction.response.send_modal(
                EdgeSettingsModal(
                    self.cog,
                    panel_message,
                    server=self.server,
                    target_user_id=self.target_user_id,
                    target_user_name=self.target_user_name,
                )
            )
        elif mode == "gtts":
            await interaction.response.send_modal(
                GTTSSettingsModal(
                    self.cog,
                    panel_message,
                    server=self.server,
                    target_user_id=self.target_user_id,
                    target_user_name=self.target_user_name,
                )
            )
        else:
            await interaction.response.send_modal(
                GoogleSettingsModal(
                    self.cog,
                    panel_message,
                    server=self.server,
                    target_user_id=self.target_user_id,
                    target_user_name=self.target_user_name,
                )
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
        print(f"[tts_panel] advanced_select | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        await interaction.response.send_modal(
            MoreTTSOptionsModal(
                self.cog,
                getattr(interaction, "message", None),
                server=self.server,
                target_user_id=self.target_user_id,
                target_user_name=self.target_user_name,
            )
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
