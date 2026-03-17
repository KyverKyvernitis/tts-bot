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
from .common import _shorten

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
        if self.toggle_name == "only_target_user":
            await self.cog._apply_only_target_from_panel(interaction, enabled, source_panel_message=source_panel_message)
        elif self.toggle_name == "announce_author":
            await self.cog._apply_announce_author_from_panel(interaction, enabled, source_panel_message=source_panel_message)
        elif self.toggle_name == "auto_leave":
            await self.cog._apply_auto_leave_from_panel(interaction, enabled, source_panel_message=source_panel_message)
        else:
            await self.cog._apply_block_voice_bot_from_panel(interaction, enabled, source_panel_message=source_panel_message)




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


class TTSMainPanelView(_BaseTTSView):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, server: bool = False, timeout: float = 180, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__(cog, owner_id, guild_id, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)
        self.server = server
        self.panel_kind = "server" if server else "user"
        self.remove_item(self.mode_button)
        if self.server:
            self.remove_item(self.spoken_name_button)
        else:
            self.remove_item(self.bot_prefix_button)
            self.remove_item(self.gtts_prefix_button)
            self.remove_item(self.edge_prefix_button)
            self.remove_item(self.gcloud_prefix_button)
            self.remove_item(self.announce_author_button)
            self.remove_item(self.ignored_role_button)

    def _target_owner(self, interaction: discord.Interaction) -> int:
        return interaction.user.id if self.owner_id == 0 else self.owner_id

    @discord.ui.button(label="Modo", style=discord.ButtonStyle.secondary, emoji="🎛️", row=0)
    async def mode_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(f"[tts_panel] mode_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        await _SimpleSelectView(self.cog, self._target_owner(interaction), self.guild_id, "Escolha o modo", "Selecione como a fala vai funcionar.", ModeSelect(self.cog, server=self.server), source_panel_message=interaction.message, target_user_id=self.target_user_id, target_user_name=self.target_user_name).send(interaction)

    @discord.ui.button(label="Voz (Edge)", style=discord.ButtonStyle.secondary, emoji="🎙️", row=0)
    async def voice_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(f"[tts_panel] voice_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        view = _SimpleSelectView(self.cog, self._target_owner(interaction), self.guild_id, "Escolha a voz do Edge", "Primeiro escolha a região e depois selecione a voz que será usada nas mensagens com prefixo do Edge.", VoiceRegionSelect(self.cog, server=self.server), source_panel_message=interaction.message, target_user_id=self.target_user_id, target_user_name=self.target_user_name)
        await view.send(interaction)

    @discord.ui.button(label="Idioma (gTTS)", style=discord.ButtonStyle.secondary, emoji="🌐", row=0)
    async def language_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(f"[tts_panel] language_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        embed = discord.Embed(
            title="Escolha o idioma",
            description="Você pode digitar o código do idioma do gTTS aqui. Exemplos: `pt-br`, `en`, `es`, `fr`, `ja`",
            color=discord.Color.red(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=LanguageHelpView(
                self.cog,
                self._target_owner(interaction),
                self.guild_id,
                server=self.server,
                source_panel_message=interaction.message,
                target_user_id=self.target_user_id,
                target_user_name=self.target_user_name,
            ),
            ephemeral=True,
        )


    @discord.ui.button(label="Velocidade (Edge)", style=discord.ButtonStyle.secondary, emoji="⏩", row=1)
    async def speed_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(f"[tts_panel] speed_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        await _SimpleSelectView(self.cog, self._target_owner(interaction), self.guild_id, "Escolha a velocidade do Edge", "Selecione a velocidade usada nas mensagens com prefixo do Edge.", SpeedSelect(self.cog, server=self.server), source_panel_message=interaction.message, target_user_id=self.target_user_id, target_user_name=self.target_user_name).send(interaction)

    @discord.ui.button(label="Tom (Edge)", style=discord.ButtonStyle.secondary, emoji="🎚️", row=1)
    async def pitch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(f"[tts_panel] pitch_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        await _SimpleSelectView(self.cog, self._target_owner(interaction), self.guild_id, "Escolha o tom do Edge", "Selecione o tom usado nas mensagens com prefixo do Edge.", PitchSelect(self.cog, server=self.server), source_panel_message=interaction.message, target_user_id=self.target_user_id, target_user_name=self.target_user_name).send(interaction)

    @discord.ui.button(label="Apelido falado", style=discord.ButtonStyle.secondary, emoji="🪪", row=1)
    async def spoken_name_button(self, interaction: discord.Interaction, button: discord.ui.Button):
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

    @discord.ui.button(label="Autor + frase", style=discord.ButtonStyle.secondary, emoji="🗣️", row=1)
    async def announce_author_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(f"[tts_panel] announce_author_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        await _SimpleSelectView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            "Autor antes da frase",
            "Quando ativado, o bot fala 'nome disse, frase' quando muda o usuário que está falando pelos prefixos.",
            ToggleSelect(self.cog, "announce_author"),
            source_panel_message=interaction.message,
            target_user_id=self.target_user_id,
            target_user_name=self.target_user_name,
        ).send(interaction)

    @discord.ui.button(label="Prefixo do bot", style=discord.ButtonStyle.secondary, emoji="🤖", row=2)
    async def bot_prefix_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BotPrefixModal(self.cog, interaction.message, self._target_owner(interaction), self.guild_id))

    @discord.ui.button(label="Prefixo do gTTS", style=discord.ButtonStyle.secondary, emoji="🔤", row=2)
    async def gtts_prefix_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GTTSPrefixModal(self.cog, interaction.message, self._target_owner(interaction), self.guild_id))

    @discord.ui.button(label="Prefixo do Edge", style=discord.ButtonStyle.secondary, emoji="🔊", row=2)
    async def edge_prefix_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EdgePrefixModal(self.cog, interaction.message, self._target_owner(interaction), self.guild_id))

    @discord.ui.button(label="Prefixo do Google", style=discord.ButtonStyle.secondary, emoji="☁️", row=2)
    async def gcloud_prefix_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GCloudPrefixModal(self.cog, interaction.message, self._target_owner(interaction), self.guild_id))

    @discord.ui.button(label="Cargo ignorado", style=discord.ButtonStyle.secondary, emoji="🚫", row=3)
    async def ignored_role_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        print(f"[tts_panel] ignored_role_button | user={interaction.user.id} guild={interaction.guild.id if interaction.guild else None} server={self.server}")
        view = IgnoreRoleConfigView(
            self.cog,
            self._target_owner(interaction),
            self.guild_id,
            source_panel_message=interaction.message,
        )
        await view.send(interaction)

    @discord.ui.button(label="Idioma (Google)", style=discord.ButtonStyle.secondary, emoji="☁️", row=3)
    async def gcloud_language_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_target_user_id = int(self.target_user_id or interaction.user.id)
        current_value = self.cog._get_current_gcloud_language(self.guild_id, current_target_user_id, server=self.server)
        await self.cog._open_gcloud_language_picker(
            interaction,
            owner_id=self._target_owner(interaction),
            guild_id=self.guild_id,
            current_value=current_value,
            server=self.server,
            source_panel_message=interaction.message,
            target_user_id=None if self.owner_id == 0 and self.target_user_id is None else self.target_user_id,
            target_user_name=self.target_user_name,
        )

    @discord.ui.button(label="Voz (Google)", style=discord.ButtonStyle.secondary, emoji="🎙️", row=3)
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
            source_panel_message=interaction.message,
            target_user_id=None if self.owner_id == 0 and self.target_user_id is None else self.target_user_id,
            target_user_name=self.target_user_name,
        )

    @discord.ui.button(label="Velocidade (Google)", style=discord.ButtonStyle.secondary, emoji="⏩", row=4)
    async def gcloud_speed_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _SimpleSelectView(self.cog, self._target_owner(interaction), self.guild_id, "Escolha a velocidade do Google Cloud", "Selecione a velocidade usada nas mensagens com prefixo do Google Cloud.", GCloudSpeedSelect(self.cog, server=self.server), source_panel_message=interaction.message, target_user_id=self.target_user_id, target_user_name=self.target_user_name).send(interaction)

    @discord.ui.button(label="Tom (Google)", style=discord.ButtonStyle.secondary, emoji="🎚️", row=4)
    async def gcloud_pitch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _SimpleSelectView(self.cog, self._target_owner(interaction), self.guild_id, "Escolha o tom do Google Cloud", "Selecione o tom usado nas mensagens com prefixo do Google Cloud.", GCloudPitchSelect(self.cog, server=self.server), source_panel_message=interaction.message, target_user_id=self.target_user_id, target_user_name=self.target_user_name).send(interaction)


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
    @discord.ui.button(label="Bloqueio por outro bot", style=discord.ButtonStyle.secondary, emoji="🤖", row=0)
    async def block_voice_bot_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _SimpleSelectView(self.cog, self._target_owner(interaction), self.guild_id, "Bloqueio por outro bot", "Escolha se o bot deve sair ou bloquear quando o outro bot de voz entrar na call.", ToggleSelect(self.cog, "block_voice_bot")).send(interaction)

    @discord.ui.button(label="Modo Cuca", style=discord.ButtonStyle.secondary, emoji="👑", row=0)
    async def only_target_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _SimpleSelectView(self.cog, self._target_owner(interaction), self.guild_id, "Modo Cuca", "Quando ativado, a Cuca continua normal e os outros usuários são forçados para gtts.", ToggleSelect(self.cog, "only_target_user")).send(interaction)

    @discord.ui.button(label="Auto leave", style=discord.ButtonStyle.secondary, emoji="⏏️", row=1)
    async def auto_leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _SimpleSelectView(self.cog, self._target_owner(interaction), self.guild_id, "Auto leave", "Escolha se o bot deve sair da call quando ficar sozinho ou só com bots.", ToggleSelect(self.cog, "auto_leave")).send(interaction)
