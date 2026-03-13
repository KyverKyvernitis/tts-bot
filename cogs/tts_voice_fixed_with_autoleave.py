import inspect
import contextlib
import asyncio
import time
import re
import weakref
import unicodedata
from urllib.parse import urlparse
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

try:
    from google.cloud import texttospeech_v1 as google_texttospeech
except Exception:  # pragma: no cover - dependência opcional em tempo de import
    google_texttospeech = None

import config
from tts_audio import GuildTTSState, QueueItem, TTSAudioMixin

from typing import Callable


_TTS_GUILD_OBJECTS = [discord.Object(id=guild_id) for guild_id in getattr(config, "GUILD_IDS", [])]


def _guild_scoped():
    return app_commands.guilds(*_TTS_GUILD_OBJECTS) if _TTS_GUILD_OBJECTS else (lambda f: f)


def _shorten(text: str, limit: int = 100) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _replace_custom_emojis_for_tts(text: str) -> str:
    return re.sub(r"<a?:([A-Za-z0-9_~]+):\d+>", lambda m: f"emoji {m.group(1)}", text)


_TTS_ABBREVIATION_MAP = {
    # comuns
    "tb": "também",
    "tbm": "também",
    "tmb": "também",
    "vc": "você",
    "vcs": "vocês",
    "pq": "porque",
    "pk": "porque",
    "q": "que",
    "blz": "beleza",
    "obg": "obrigado",
    "obgd": "obrigado",
    "pf": "por favor",
    "pfv": "por favor",
    "hj": "hoje",
    "dps": "depois",
    "gnt": "gente",
    "sdds": "saudades",
    "vdd": "verdade",
    "flw": "falou",
    "vlw": "valeu",
    "cmg": "comigo",
    "ctz": "certeza",
    "msg": "mensagem",
    # outras comuns
    "mds": "meu deus",
    "tmj": "tamo junto",
    "slk": "cê é louco",
    "pdc": "pode crer",
    "rlx": "relaxa",
    "sqn": "só que não",
    "ngm": "ninguém",
    "td": "tudo",
    "nd": "nada",
    "bjs": "beijos",
    "abs": "abraços",
    "kd": "cadê",
    "qnd": "quando",
    "fds": "foda-se",
    # ofensivas comuns
    "fdp": "filho da puta",
    "vsf": "vai se foder",
    "vtnc": "vai tomar no cu",
    "tmnc": "tomar no cu",
    "tnc": "tomar no cu",
    "pqp": "puta que pariu",
    "prr": "porra",
    "crl": "caralho",
    "krl": "caralho",
}

_TTS_ABBREVIATION_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_TTS_ABBREVIATION_MAP, key=len, reverse=True)) + r")\b",
    flags=re.IGNORECASE,
)


USER_MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
ROLE_MENTION_PATTERN = re.compile(r"<@&(\d+)>")
CHANNEL_MENTION_PATTERN = re.compile(r"<#(\d+)>")
URL_PATTERN = re.compile(r"https?://[^\s<>]+", flags=re.IGNORECASE)
DISCORD_CHANNEL_URL_PATTERN = re.compile(
    r"https?://(?:canary\.|ptb\.)?(?:www\.)?discord(?:app)?\.com/channels/(@me|\d+)/(\d+)(?:/\d+)?",
    flags=re.IGNORECASE,
)
_ATTACHMENT_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".svg", ".avif", ".heic", ".heif")
_ATTACHMENT_VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".wmv", ".flv", ".3gp")
_COMMON_MULTI_PART_TLDS = {"com", "net", "org", "gov", "edu", "co"}


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _speech_name(text: str) -> str:
    text = _normalize_spaces(text)
    if not text:
        return ""
    text = re.sub(r"[_\-.]+", " ", text)
    return _normalize_spaces(text)


def _looks_pronounceable_for_tts(text: str) -> bool:
    text = _normalize_spaces(text)
    if not text:
        return False

    non_space = [ch for ch in text if not ch.isspace()]
    if not non_space:
        return False

    alnum_count = sum(ch.isalnum() for ch in non_space)
    friendly_count = sum(ch.isalnum() or ch in "._-" for ch in non_space)
    symbol_count = sum(unicodedata.category(ch).startswith("S") for ch in non_space)
    hard_punct = {"[", "]", "{", "}", "(", ")", "<", ">", "~", "^", "`", "|", chr(92), "/", '"', "'", "*", "=", ":", ";", "+", ","}
    hard_punct_count = sum(ch in hard_punct for ch in non_space)

    if alnum_count == 0:
        return False
    if friendly_count / max(1, len(non_space)) < 0.6:
        return False
    if symbol_count >= 1:
        return False
    if hard_punct_count >= 2:
        return False
    return True


def _extract_primary_domain(hostname: str) -> str:
    host = str(hostname or "").strip().lower().strip(".")
    if not host:
        return ""
    parts = [part for part in host.split(".") if part]
    if not parts:
        return ""
    while len(parts) > 2 and parts[0] in {"www", "m", "ptb", "canary", "cdn", "media"}:
        parts = parts[1:]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _COMMON_MULTI_PART_TLDS:
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0]


def _expand_abbreviations_for_tts(text: str) -> str:
    if not text or not _TTS_ABBREVIATION_PATTERN.search(text):
        return text

    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        return _TTS_ABBREVIATION_MAP.get(token.lower(), token)

    return _TTS_ABBREVIATION_PATTERN.sub(repl, text)


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
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.message: discord.Message | None = None
        self.panel_kind: str = "user"
        self.target_user_id: int | None = target_user_id
        self.target_user_name: str | None = target_user_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
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
        if self.message is None:
            return
        try:
            for child in self.children:
                try:
                    child.disabled = True
                except Exception:
                    pass
            embed = await self.cog._build_expired_panel_embed(self.guild_id, self.panel_kind)
            await self.message.edit(embed=embed, view=self)
        except discord.NotFound:
            pass
        except Exception as e:
            print(f"[tts_panel_timeout] falha ao expirar painel: {e!r}")


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
        await _SimpleSelectView(self.cog, self._target_owner(interaction), self.guild_id, "Autor antes da frase", "Quando ativado, o bot fala 'nome disse, frase' quando muda o usuário que está falando pelos prefixos.", ToggleSelect(self.cog, "announce_author")).send(interaction)

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


def get_gtts_languages() -> dict[str, str]:
    try:
        from gtts.lang import tts_langs
        return tts_langs()
    except Exception:
        return {
            "pt-br": "Portuguese (Brazil)",
            "pt": "Portuguese",
            "en": "English",
            "es": "Spanish",
            "fr": "French",
            "de": "German",
            "it": "Italian",
            "ja": "Japanese",
        }


def build_gtts_language_aliases(languages: dict[str, str]) -> dict[str, str]:
    aliases: dict[str, str] = {
        "portugues": "pt",
        "português": "pt",
        "portugues brasil": "pt-br",
        "português brasil": "pt-br",
        "pt br": "pt-br",
        "pt-br": "pt-br",
        "ptbr": "pt-br",
        "brasileiro": "pt-br",
        "ingles": "en",
        "inglês": "en",
        "espanhol": "es",
        "frances": "fr",
        "francês": "fr",
        "alemao": "de",
        "alemão": "de",
        "italiano": "it",
        "japones": "ja",
        "japonês": "ja",
    }
    for code, name in (languages or {}).items():
        code_norm = str(code or "").strip().lower()
        if not code_norm:
            continue
        aliases.setdefault(code_norm, code_norm)
        aliases.setdefault(code_norm.replace("_", "-"), code_norm)
        aliases.setdefault(code_norm.replace("-", " "), code_norm)
        name_norm = str(name or "").strip().lower()
        if name_norm:
            aliases.setdefault(name_norm, code_norm)
            aliases.setdefault(name_norm.replace("(", " ").replace(")", " ").replace("-", " ").replace("_", " ").replace("  ", " ").strip(), code_norm)
    return aliases


def validate_mode(mode: str) -> str:
    value = str(mode or "").strip().lower()
    if value in {"edge", "gtts", "gcloud"}:
        return value
    return "gtts"


MODE_CHOICES = [
    app_commands.Choice(name="gtts — mais simples e compatível", value="gtts"),
    app_commands.Choice(name="edge — voz natural com voice, speed e pitch", value="edge"),
    app_commands.Choice(name="gcloud — Google Cloud TTS pelo prefixo configurado", value="gcloud"),
]

SPEED_CHOICES = [
    app_commands.Choice(name="-50% — bem mais devagar", value="-50%"),
    app_commands.Choice(name="-25% — mais devagar", value="-25%"),
    app_commands.Choice(name="+0% — normal", value="+0%"),
    app_commands.Choice(name="+25% — mais rápido", value="+25%"),
    app_commands.Choice(name="+50% — bem mais rápido", value="+50%"),
]

PITCH_CHOICES = [
    app_commands.Choice(name="-50Hz — mais grave", value="-50Hz"),
    app_commands.Choice(name="-25Hz — levemente grave", value="-25Hz"),
    app_commands.Choice(name="+0Hz — normal", value="+0Hz"),
    app_commands.Choice(name="+25Hz — levemente agudo", value="+25Hz"),
    app_commands.Choice(name="+50Hz — mais agudo", value="+50Hz"),
]


USER_CONFIG_ACTION_CHOICES = [
    app_commands.Choice(name="Abrir painel pessoal do usuário", value="panel"),
    app_commands.Choice(name="Alterar apelido falado do usuário", value="spoken_name"),
    app_commands.Choice(name="Resetar configurações do usuário para as do servidor", value="reset"),
]


STATUS_ACTION_CHOICES = [
    app_commands.Choice(name="Ver o meu status", value="self"),
    app_commands.Choice(name="Mostrar o status de outro usuário no chat", value="show_other"),
    app_commands.Choice(name="Copiar as configurações de outro usuário", value="copy_other"),
]


class TTSVoice(TTSAudioMixin, commands.GroupCog, group_name="tts", group_description="Comandos de texto para fala"):
    server = app_commands.Group(name="server", description="Configurações padrão do servidor")
    voices = app_commands.Group(name="voices", description="Listas de vozes e idiomas")
    toggle = app_commands.Group(name="toggle", description="Atalhos e modos especiais")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.guild_states: dict[int, GuildTTSState] = {}
        self.edge_voice_cache: list[str] = []
        self.edge_voice_names: set[str] = set()
        self.gtts_languages: dict[str, str] = get_gtts_languages()
        self.gtts_language_aliases: dict[str, str] = build_gtts_language_aliases(self.gtts_languages)
        self._recent_tts_message_ids: dict[int, float] = {}
        self._voice_connect_locks: dict[int, asyncio.Lock] = {}
        self._prefix_panel_cooldowns: dict[tuple[int, int, str], float] = {}
        self._active_prefix_panels: dict[tuple[int, int, str], tuple[discord.Message, discord.ui.View]] = {}
        self._public_panel_states: dict[int, dict] = {}
        self._status_views_by_target: dict[tuple[int, int], weakref.WeakSet[TTSStatusView]] = {}
        self._status_refresh_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._last_announced_author_by_guild: dict[int, int] = {}
        self._gcloud_voices_cache: list[dict[str, object]] = []
        self._gcloud_voices_cache_loaded_at: float = 0.0
        self._gcloud_voices_cache_lock = asyncio.Lock()

    async def cog_load(self):
        await self._load_edge_voices()

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

    async def _set_user_tts_and_refresh(self, guild_id: int, user_id: int, *, history_entry: str | None = None, **kwargs):
        db = self._get_db()
        if db is None:
            raise RuntimeError("settings db unavailable")
        result = await self._maybe_await(db.set_user_tts(guild_id, user_id, **kwargs))
        if history_entry and hasattr(db, "set_user_panel_last_change"):
            await self._maybe_await(db.set_user_panel_last_change(guild_id, user_id, history_entry))
        await self._notify_status_views_changed(guild_id, user_id)
        return result

    async def _reset_user_tts_and_refresh(self, guild_id: int, user_id: int, *, history_entry: str | None = None):
        db = self._get_db()
        if db is None:
            raise RuntimeError("settings db unavailable")
        result = await self._maybe_await(db.reset_user_tts(guild_id, user_id))
        if history_entry and hasattr(db, "set_user_panel_last_change"):
            await self._maybe_await(db.set_user_panel_last_change(guild_id, user_id, history_entry))
        await self._notify_status_views_changed(guild_id, user_id)
        return result

    def _register_status_view(self, view: TTSStatusView) -> None:
        if view.message is None:
            return
        target_user_id = int(view.target_user_id or view.owner_id or 0)
        if not target_user_id:
            return
        key = (int(view.guild_id), target_user_id)
        views = self._status_views_by_target.get(key)
        if views is None:
            views = weakref.WeakSet()
            self._status_views_by_target[key] = views
        views.add(view)

    def _unregister_status_view(self, view: TTSStatusView) -> None:
        target_user_id = int(view.target_user_id or view.owner_id or 0)
        if not target_user_id:
            return
        key = (int(view.guild_id), target_user_id)
        views = self._status_views_by_target.get(key)
        if not views:
            return
        views.discard(view)
        if not list(views):
            self._status_views_by_target.pop(key, None)
            self._status_refresh_locks.pop(key, None)

    async def _notify_status_views_changed(self, guild_id: int, user_id: int) -> None:
        key = (int(guild_id), int(user_id))
        views = self._status_views_by_target.get(key)
        if not views:
            return
        active_views = [view for view in list(views) if getattr(view, "message", None) is not None and not view.is_finished()]
        if not active_views:
            self._status_views_by_target.pop(key, None)
            self._status_refresh_locks.pop(key, None)
            return
        lock = self._status_refresh_locks.setdefault(key, asyncio.Lock())
        async with lock:
            for view in list(active_views):
                await view.refresh_from_config_change()

    def _cleanup_guild_runtime_state(self, guild_id: int) -> None:
        self._last_announced_author_by_guild.pop(int(guild_id), None)

    def _guild_announce_author_enabled(self, guild_defaults: dict | None) -> bool:
        return bool((guild_defaults or {}).get("announce_author", False))

    def _apply_author_prefix_if_needed(self, guild_id: int, author: discord.abc.User | None, text: str, *, enabled: bool) -> str:
        text = str(text or "").strip()
        if not enabled or not text:
            return text
        author_id = int(getattr(author, "id", 0) or 0)
        if not author_id:
            return text
        last_author_id = int(self._last_announced_author_by_guild.get(int(guild_id), 0) or 0)
        self._last_announced_author_by_guild[int(guild_id)] = author_id
        if last_author_id == author_id:
            return text
        speaker = self._tts_user_reference(author, guild_id=guild_id)
        return f"{speaker} disse, {text}" if speaker else text


    def _panel_actor_name(self, interaction: discord.Interaction) -> str:
        member = getattr(interaction, "user", None)
        return self._member_actor_name(member)

    def _member_actor_name(self, member) -> str:
        if member is None:
            return "@usuário"

        name = getattr(member, "name", None) or getattr(member, "display_name", None) or "usuário"
        if not str(name).startswith("@"):
            return f"@{name}"
        return str(name)

    def _encode_public_owner_history(self, owner_id: int, actor_name: str, action_text: str) -> str:
        safe_actor = str(actor_name or "@usuário").replace("|", "/")
        safe_action = str(action_text or "").replace("|", "/")
        return f"__PUBLIC_OWNER_SELF__|{int(owner_id)}|{safe_actor}|{safe_action}"

    def _decode_public_owner_history(self, entry: str) -> tuple[int, str, str] | None:
        raw = str(entry or "")
        prefix = "__PUBLIC_OWNER_SELF__|"
        if not raw.startswith(prefix):
            return None
        try:
            _, owner_id, actor_name, action_text = raw.split("|", 3)
            return int(owner_id), actor_name, action_text
        except (TypeError, ValueError):
            return None

    def _render_history_entry(self, entry: str, *, viewer_user_id: int | None = None, message_id: int | None = None) -> str:
        decoded = self._decode_public_owner_history(entry)
        if not decoded:
            return str(entry or "")

        owner_id, actor_name, action_text = decoded
        state = self._public_panel_states.get(message_id or 0, {}) if message_id else {}
        is_public_user_panel = bool(state and state.get("panel_kind") == "user")
        public_panel_owner_id = int(state.get("owner_id", 0) or 0) if state else 0

        if viewer_user_id == owner_id:
            if is_public_user_panel:
                if public_panel_owner_id == owner_id:
                    return f"Você ({actor_name}) {action_text}"
                return f"{actor_name} {action_text}"
            return f"Você {action_text}"

        return f"{actor_name} {action_text}"

    def _quote_value(self, value: str) -> str:
        return f'"{value}"'

    def _format_history_entries(self, entries: list[str], *, viewer_user_id: int | None = None, message_id: int | None = None) -> str:
        entries = [str(x) for x in (entries or []) if str(x or "").strip()]
        if not entries:
            return ""
        lines = []
        for idx, entry in enumerate(entries):
            rendered = self._render_history_entry(entry, viewer_user_id=viewer_user_id, message_id=message_id)
            safe = rendered.replace("`", "'")
            line = f"`{safe}`"
            if idx == len(entries) - 1:
                line = f"**{line}**"
            lines.append(line)
        return "\n".join(lines)

    def _format_status_history_entries(self, entries: list[str], *, viewer_user_id: int | None = None) -> str:
        entries = [str(x) for x in (entries or []) if str(x or "").strip()]
        if not entries:
            return ""
        lines = []
        recent_entries = entries[-2:]
        for idx, entry in enumerate(recent_entries):
            rendered = self._render_history_entry(entry, viewer_user_id=viewer_user_id, message_id=None)
            safe = rendered.replace("`", "'")
            line = f"• {safe}"
            if idx == len(recent_entries) - 1:
                line = f"**{line}**"
            lines.append(line)
        return "\n".join(lines)

    def _get_public_panel_history(self, message_id: int | None) -> list[str]:
        if not message_id:
            return []
        state = self._public_panel_states.get(message_id, {}) or {}
        return [str(x) for x in (state.get("history", []) or []) if str(x or "").strip()]

    def _merge_history_entries(self, *groups: list[str] | tuple[str, ...]) -> list[str]:
        merged: list[str] = []
        for group in groups:
            for entry in (group or []):
                clean = str(entry or "").strip()
                if not clean:
                    continue
                merged.append(clean)
        return merged[-3:]

    def _append_public_panel_history(self, message_id: int | None, text: str):
        if not message_id:
            return
        state = self._public_panel_states.get(message_id)
        if state is None:
            state = {"history": []}
            self._public_panel_states[message_id] = state
        history = self._merge_history_entries(state.get("history", []) or [], [text] if text else [])
        state["history"] = history

    def _resolve_last_changes(self, *, stored_changes: list[str] | None = None, message_id: int | None = None) -> list[str]:
        stored = [str(x) for x in (stored_changes or []) if str(x or "").strip()]
        if not message_id or message_id not in self._public_panel_states:
            return stored
        public_history = self._get_public_panel_history(message_id)
        return self._merge_history_entries(stored, public_history)

    def _resolve_public_panel_message(self, interaction: discord.Interaction, source_panel_message: discord.Message | None = None) -> tuple[discord.Message | None, int | None]:
        direct_message = getattr(interaction, "message", None)
        direct_id = getattr(direct_message, "id", None)
        if direct_id in self._public_panel_states:
            return direct_message, direct_id

        source_id = getattr(source_panel_message, "id", None)
        if source_id in self._public_panel_states:
            return source_panel_message, source_id

        if source_panel_message is not None:
            return source_panel_message, source_id

        return direct_message, direct_id

    async def _maybe_await(self, value):
        if inspect.isawaitable(value):
            return await value
        return value

    def _get_voice_connect_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._voice_connect_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._voice_connect_locks[guild_id] = lock
        return lock


    def _prefix_panel_key(self, guild_id: int, user_id: int, panel_kind: str) -> tuple[int, int, str]:
        return (guild_id, user_id, panel_kind)

    async def _delete_prefix_panel(self, guild_id: int, user_id: int, panel_kind: str):
        key = self._prefix_panel_key(guild_id, user_id, panel_kind)
        message = self._active_prefix_panels.pop(key, None)
        if not message:
            return
        self._public_panel_states.pop(getattr(message, "id", None), None)
        try:
            await message.delete()
        except Exception:
            pass

    async def _check_prefix_panel_cooldown(self, message: discord.Message, panel_kind: str) -> bool:
        if not message.guild:
            return False

        now = time.monotonic()
        key = self._prefix_panel_key(message.guild.id, message.author.id, panel_kind)
        expires_at = self._prefix_panel_cooldowns.get(key, 0.0)

        if expires_at > now:
            remaining = max(1, int(expires_at - now + 0.999))
            embed = discord.Embed(
                title="Calma aí",
                description=f"Você precisa esperar **{remaining}s** para usar esse comando de painel novamente",
                color=discord.Color.red(),
            )
            await message.channel.send(embed=embed)
            return True

        self._prefix_panel_cooldowns[key] = now + 5.0

        stale = [k for k, ts in self._prefix_panel_cooldowns.items() if ts < now - 60.0]
        for stale_key in stale:
            self._prefix_panel_cooldowns.pop(stale_key, None)

        return False

    async def _send_prefix_panel(
        self,
        message: discord.Message,
        *,
        panel_kind: str,
        embed: discord.Embed,
        view: discord.ui.View,
    ):
        if not message.guild:
            return

        if await self._check_prefix_panel_cooldown(message, panel_kind):
            return

        await self._delete_prefix_panel(message.guild.id, message.author.id, panel_kind)

        sent = await message.channel.send(embed=embed, view=view)
        view.message = sent
        db = self._get_db()
        initial_history: list[str] = []
        if db and hasattr(db, "get_panel_history"):
            panel_history = await self._maybe_await(db.get_panel_history(message.guild.id, message.author.id))
            if panel_kind == "server":
                initial_history = list((panel_history or {}).get("server_last_changes", []) or [])
            elif panel_kind == "toggle":
                initial_history = list((panel_history or {}).get("toggle_last_changes", []) or [])
            else:
                initial_history = list((panel_history or {}).get("user_last_changes", []) or [])
        self._public_panel_states[sent.id] = {"panel_kind": panel_kind, "history": self._merge_history_entries(initial_history), "owner_id": message.author.id}
        self._active_prefix_panels[self._prefix_panel_key(message.guild.id, message.author.id, panel_kind)] = sent

    def _mark_tts_message_seen(self, message_id: int) -> None:
        now = time.monotonic()
        self._recent_tts_message_ids[message_id] = now
        cutoff = now - 30.0
        stale = [mid for mid, ts in self._recent_tts_message_ids.items() if ts < cutoff]
        for mid in stale:
            self._recent_tts_message_ids.pop(mid, None)

    def _was_tts_message_seen(self, message_id: int) -> bool:
        ts = self._recent_tts_message_ids.get(message_id)
        if ts is None:
            return False
        if time.monotonic() - ts > 30.0:
            self._recent_tts_message_ids.pop(message_id, None)
            return False
        return True

    async def _load_edge_voices(self):
        try:
            import edge_tts
            voices = await edge_tts.list_voices()
            names = sorted({v["ShortName"] for v in voices if "ShortName" in v})
            self.edge_voice_cache = names
            self.edge_voice_names = set(names)
            print(f"[tts_voice] {len(names)} vozes edge carregadas.")
        except Exception as e:
            print(f"[tts_voice] Falha ao carregar vozes edge: {e}")
            self.edge_voice_cache = []
            self.edge_voice_names = set()

    async def _load_gcloud_voices(self, *, force: bool = False) -> list[dict[str, object]]:
        if google_texttospeech is None:
            return []
        now = time.monotonic()
        if not force and self._gcloud_voices_cache and (now - self._gcloud_voices_cache_loaded_at) < 1800:
            return list(self._gcloud_voices_cache)
        async with self._gcloud_voices_cache_lock:
            now = time.monotonic()
            if not force and self._gcloud_voices_cache and (now - self._gcloud_voices_cache_loaded_at) < 1800:
                return list(self._gcloud_voices_cache)

            def _worker() -> list[dict[str, object]]:
                client = google_texttospeech.TextToSpeechClient()
                try:
                    response = client.list_voices(request={})
                    voices: list[dict[str, object]] = []
                    for voice in list(getattr(response, 'voices', []) or []):
                        name = str(getattr(voice, 'name', '') or '')
                        language_codes = [str(code) for code in list(getattr(voice, 'language_codes', []) or []) if str(code or '').strip()]
                        if not name or not language_codes:
                            continue
                        voices.append({
                            'name': name,
                            'language_codes': language_codes,
                            'ssml_gender': int(getattr(voice, 'ssml_gender', 0) or 0),
                        })
                    return voices
                finally:
                    with contextlib.suppress(Exception):
                        client.transport.close()

            try:
                voices = await asyncio.to_thread(_worker)
            except Exception as e:
                print(f"[tts_voice] Falha ao carregar vozes do Google Cloud: {e!r}")
                return list(self._gcloud_voices_cache)

            self._gcloud_voices_cache = list(voices)
            self._gcloud_voices_cache_loaded_at = time.monotonic()
            return list(self._gcloud_voices_cache)

    def _gcloud_language_priority(self, code: str) -> tuple[int, str]:
        value = str(code or '').strip()
        preferred = {
            'pt-BR': 0,
            'pt-PT': 1,
            'en-US': 2,
            'es-ES': 3,
            'es-US': 4,
            'fr-FR': 5,
            'de-DE': 6,
            'it-IT': 7,
            'ja-JP': 8,
        }
        base = value.split('-', 1)[0].lower() if value else ''
        base_order = {
            'pt': 0,
            'en': 1,
            'es': 2,
            'fr': 3,
            'de': 4,
            'it': 5,
            'ja': 6,
        }
        return (preferred.get(value, 100 + base_order.get(base, 100)), value.lower())

    def _build_gcloud_language_options_from_catalog(self, catalog: list[dict[str, object]], current_value: str | None = None) -> list[discord.SelectOption]:
        seen: set[str] = set()
        ordered_codes: list[str] = []
        preferred = [
            str(current_value or '').strip(),
            str(getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR') or 'pt-BR').strip(),
            'pt-BR', 'pt-PT', 'en-US', 'es-ES', 'es-US', 'fr-FR', 'de-DE', 'it-IT', 'ja-JP'
        ]
        for code in preferred:
            if code and code not in seen:
                seen.add(code)
                ordered_codes.append(code)
        discovered = sorted({str(code) for entry in catalog for code in list(entry.get('language_codes', []) or []) if str(code or '').strip()}, key=self._gcloud_language_priority)
        for code in discovered:
            if code not in seen:
                seen.add(code)
                ordered_codes.append(code)
        options: list[discord.SelectOption] = []
        for code in ordered_codes[:25]:
            desc = 'Idioma disponível no Google Cloud'
            if code.startswith('pt-'):
                desc = 'Português disponível no Google Cloud'
            elif code.startswith('en-'):
                desc = 'Inglês disponível no Google Cloud'
            elif code.startswith('es-'):
                desc = 'Espanhol disponível no Google Cloud'
            options.append(discord.SelectOption(label=_shorten(code, 100), description=_shorten(desc, 100), value=code, default=(code == current_value)))
        return options

    def _gcloud_voice_priority(self, voice_name: str) -> tuple[int, str]:
        value = str(voice_name or '').strip()
        order = [('Studio', 0), ('Neural2', 1), ('Wavenet', 2), ('Standard', 3), ('Chirp3-HD', 4), ('Chirp3', 4)]
        family_rank = 99
        for token, rank in order:
            if token.lower() in value.lower():
                family_rank = rank
                break
        return (family_rank, value.lower())

    def _split_gcloud_voice_name(self, voice_name: str) -> tuple[str, str]:
        value = str(voice_name or '').strip()
        family = 'Google Cloud'
        for token, label in [('Studio', 'Studio'), ('Neural2', 'Neural2'), ('Wavenet', 'WaveNet'), ('Standard', 'Standard'), ('Chirp3-HD', 'Chirp 3 HD'), ('Chirp3', 'Chirp 3')]:
            if token.lower() in value.lower():
                family = label
                break
        tail = value.rsplit('-', 1)[-1] if '-' in value else ''
        variant = f'variante {tail}' if len(tail) <= 3 and tail.isalnum() else value
        return family, variant

    def _describe_gcloud_voice(self, voice_name: str) -> str:
        family, variant = self._split_gcloud_voice_name(voice_name)
        return _shorten(f'{family} · {variant}', 100)

    def _build_gcloud_voice_options_from_catalog(self, catalog: list[dict[str, object]], language_code: str, current_value: str | None = None) -> list[discord.SelectOption]:
        language_code = str(language_code or '').strip() or str(getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR') or 'pt-BR')
        filtered_names = sorted({str(entry.get('name') or '') for entry in catalog if language_code in list(entry.get('language_codes', []) or []) and str(entry.get('name') or '').strip()}, key=self._gcloud_voice_priority)
        ordered_names: list[str] = []
        seen: set[str] = set()
        preferred = [
            str(current_value or '').strip(),
            str(getattr(config, 'GOOGLE_CLOUD_TTS_VOICE_NAME', 'pt-BR-Standard-A') or 'pt-BR-Standard-A').strip(),
        ]
        for name in preferred:
            if name and name not in seen:
                seen.add(name)
                ordered_names.append(name)
        for name in filtered_names:
            if name not in seen:
                seen.add(name)
                ordered_names.append(name)
        options: list[discord.SelectOption] = []
        for name in ordered_names[:25]:
            family, variant = self._split_gcloud_voice_name(name)
            label = _shorten(f'{family} — {variant}', 100)
            options.append(discord.SelectOption(label=label, description=_shorten(name, 100), value=name, default=(name == current_value)))
        return options

    def _gcloud_voice_matches_language(self, voice_name: str, language_code: str) -> bool:
        voice = str(voice_name or '').strip().lower()
        language = str(language_code or '').strip().lower()
        if not voice or not language:
            return False
        return voice.startswith(language + '-')

    def _pick_first_gcloud_voice_for_language(self, catalog: list[dict[str, object]], language_code: str) -> str:
        options = self._build_gcloud_voice_options_from_catalog(catalog, language_code, current_value=None)
        return str(options[0].value if options else '')

    async def _open_gcloud_language_picker(self, interaction: discord.Interaction, *, owner_id: int, guild_id: int, current_value: str, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        catalog = await self._load_gcloud_voices()
        options = self._build_gcloud_language_options_from_catalog(catalog, current_value=current_value)
        if not options:
            await self._respond(interaction, embed=self._make_embed('Google Cloud indisponível', 'Não consegui listar os idiomas do Google Cloud agora. Confira as credenciais e tente novamente.', ok=False), ephemeral=True)
            return
        description = 'Selecione um idioma disponível do Google Cloud. A lista é carregada das vozes que a sua conta consegue usar.'
        await _SimpleSelectView(self, owner_id, guild_id, 'Escolha o idioma do Google Cloud', description, GCloudLanguageSelect(self, server=server, options=options), source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name).send(interaction)

    async def _open_gcloud_voice_picker(self, interaction: discord.Interaction, *, owner_id: int, guild_id: int, language_code: str, current_value: str, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        effective_language = str(language_code or '').strip() or str(getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR') or 'pt-BR')
        catalog = await self._load_gcloud_voices()
        options = self._build_gcloud_voice_options_from_catalog(catalog, effective_language, current_value=current_value)
        if not options:
            await self._respond(interaction, embed=self._make_embed('Nenhuma voz encontrada', f'Não encontrei vozes do Google Cloud para o idioma `{effective_language}`. Ajuste o idioma e tente de novo.', ok=False), ephemeral=True)
            return
        description = f'Selecione uma voz disponível para `{effective_language}`. O título mostra a família da voz e a variante; abaixo aparece o nome técnico completo.'
        await _SimpleSelectView(self, owner_id, guild_id, 'Escolha a voz do Google Cloud', description, GCloudVoiceSelect(self, server=server, options=options), source_panel_message=source_panel_message, target_user_id=target_user_id, target_user_name=target_user_name).send(interaction)

    def _make_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.green() if ok else discord.Color.red())

    async def _respond(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = True,
    ):
        if interaction.response.is_done():
            response_type = getattr(interaction.response, "type", None)
            if response_type == discord.InteractionResponseType.deferred_channel_message:
                await interaction.edit_original_response(
                    content=content,
                    embed=embed,
                    view=view,
                )
                try:
                    return await interaction.original_response()
                except Exception:
                    return None

            return await interaction.followup.send(
                content=content,
                embed=embed,
                view=view,
                ephemeral=ephemeral,
                wait=True,
            )
        await interaction.response.send_message(
            content=content,
            embed=embed,
            view=view,
            ephemeral=ephemeral,
        )
        try:
            return await interaction.original_response()
        except Exception:
            return None

    async def _defer_ephemeral(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    async def _require_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.guild:
            return True
        await self._respond(interaction, embed=self._make_embed("Comando indisponível", "Esse comando só pode ser usado dentro de um servidor.", ok=False), ephemeral=True)
        return False

    async def _require_manage_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.manage_guild:
            return True
        await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Gerenciar Servidor` para alterar as configurações do servidor.", ok=False), ephemeral=True)
        return False

    async def _require_kick_members(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.kick_members:
            return True
        await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para usar esse comando.", ok=False), ephemeral=True)
        return False

    async def _require_staff_or_kick_members(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms and (perms.kick_members or perms.manage_guild or perms.administrator):
            return True
        await self._respond(interaction, embed=self._make_embed("Sem permissão", "Você precisa ser staff ou ter a permissão `Expulsar Membros` para usar esse comando.", ok=False), ephemeral=True)
        return False

    async def _require_toggle_allowed_guild(self, interaction: discord.Interaction) -> bool:
        guild_ids = getattr(config, "GUILD_IDS", []) or []
        if not guild_ids:
            return True
        guild = getattr(interaction, "guild", None)
        if guild and guild.id in guild_ids:
            return True
        await self._respond(interaction, embed=self._make_embed("Indisponível aqui", "Esse comando só está habilitado nos servidores definidos na env.", ok=False), ephemeral=True)
        return False

    def _normalize_rate_value(self, raw: str) -> str | None:
        value = str(raw).strip().replace("％", "%").replace("−", "-").replace("–", "-").replace("—", "-").replace(" ", "")
        if value.endswith("%"):
            value = value[:-1]
        if not value:
            return None
        if value[0] not in "+-":
            value = f"+{value}"
        if not value[1:].isdigit():
            return None
        return f"{value[0]}{value[1:]}%"

    def _normalize_pitch_value(self, raw: str) -> str | None:
        value = str(raw).strip().replace("−", "-").replace("–", "-").replace("—", "-").replace(" ", "")
        if value.lower().endswith("hz"):
            value = value[:-2]
        if not value:
            return None
        if value[0] not in "+-":
            value = f"+{value}"
        if not value[1:].isdigit():
            return None
        return f"{value[0]}{value[1:]}Hz"

    async def _only_target_user_enabled(self, guild_id: int) -> bool:
        db = self._get_db()
        if db is None:
            return False
        try:
            data = db.get_guild_tts_defaults(guild_id)
            data = await self._maybe_await(data)
            return bool((data or {}).get("only_target_user", False))
        except Exception as e:
            print(f"[tts_voice] Erro ao ler only_target_user da guild {guild_id}: {e}")
            return False

    async def _block_voice_bot_enabled(self, guild_id: int) -> bool:
        db = self._get_db()
        if db is None:
            return False
        try:
            data = db.get_guild_tts_defaults(guild_id)
            data = await self._maybe_await(data)
            return bool((data or {}).get("block_voice_bot", False))
        except Exception as e:
            print(f"[tts_voice] Erro ao ler block_voice_bot da guild {guild_id}: {e}")
            return False

    def _target_voice_bot_id(self) -> Optional[int]:
        for name in ("VOICE_BOT_ID", "BLOCK_VOICE_BOT_ID"):
            value = getattr(config, name, None)
            if value:
                try:
                    return int(value)
                except Exception:
                    pass
        return None

    def _target_voice_bot_in_channel(self, voice_channel) -> bool:
        target_bot_id = self._target_voice_bot_id()
        if not target_bot_id or voice_channel is None:
            return False
        return any(member.id == target_bot_id for member in getattr(voice_channel, "members", []))


    def _get_voice_client_for_guild(self, guild: discord.Guild | None) -> Optional[discord.VoiceClient]:
        if guild is None:
            return None

        for vc in self.bot.voice_clients:
            try:
                if vc.guild and vc.guild.id == guild.id:
                    return vc
            except Exception:
                continue

        return guild.voice_client

    async def _should_block_for_voice_bot(self, guild: discord.Guild, voice_channel) -> bool:
        return await self._block_voice_bot_enabled(guild.id) and self._target_voice_bot_in_channel(voice_channel)

    async def _disconnect_and_clear(self, guild: discord.Guild):
        state = self._get_state(guild.id)
        try:
            while not state.queue.empty():
                state.queue.get_nowait()
                state.queue.task_done()
        except Exception:
            pass
        self._last_announced_author_by_guild.pop(int(guild.id), None)
        vc = self._get_voice_client_for_guild(guild)
        if vc and vc.is_connected():
            try:
                if vc.is_playing():
                    vc.stop()
            except Exception:
                pass
            try:
                await vc.disconnect(force=False)
            except Exception as e:
                print(f"[tts_voice] erro ao desconectar guild {guild.id}: {e}")

    async def _disconnect_if_blocked(self, guild: discord.Guild):
        await self._disconnect_and_clear(guild)

    def _voice_channel_has_only_bots_or_is_empty(self, voice_channel) -> bool:
        if voice_channel is None:
            return True
        members = list(getattr(voice_channel, "members", []))
        return not any(not m.bot for m in members)

    async def _disconnect_if_alone_or_only_bots(self, guild: discord.Guild):
        db = self._get_db()
        guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild.id)) if db else {}
        if guild_defaults is not None and not bool((guild_defaults or {}).get("auto_leave", True)):
            return

        vc = self._get_voice_client_for_guild(guild)
        if vc is None or not vc.is_connected() or vc.channel is None:
            return
        if self._voice_channel_has_only_bots_or_is_empty(vc.channel):
            print(f"[tts_voice] saindo da call | sozinho ou só com bots | guild={guild.id} channel={vc.channel.id}")
            await self._disconnect_and_clear(guild)

    async def _ensure_connected(self, guild: discord.Guild, voice_channel) -> Optional[discord.VoiceClient]:
        if voice_channel is None:
            print(f"[tts_voice] _ensure_connected recebeu canal None | guild={guild.id}")
            return None

        async def _ensure_self_deaf() -> None:
            try:
                me = getattr(guild, "me", None)
                me_voice = getattr(me, "voice", None)
                if me_voice and not getattr(me_voice, "self_deaf", False):
                    await guild.change_voice_state(channel=voice_channel, self_deaf=True)
            except Exception:
                pass

        lock = self._get_voice_connect_lock(guild.id)
        async with lock:
            vc = self._get_voice_client_for_guild(guild)

            if vc and vc.is_connected() and vc.channel and vc.channel.id == voice_channel.id:
                await _ensure_self_deaf()
                return vc

            async def _fresh_connect() -> Optional[discord.VoiceClient]:
                new_vc = await voice_channel.connect(self_deaf=True)
                await _ensure_self_deaf()
                print(f"[tts_voice] Conectado no canal {voice_channel.id} na guild {guild.id}")
                return new_vc

            try:
                if vc and vc.is_connected():
                    try:
                        await vc.move_to(voice_channel)
                        await _ensure_self_deaf()
                        print(f"[tts_voice] Movido para canal {voice_channel.id} na guild {guild.id}")
                        return vc
                    except Exception as move_err:
                        msg = str(move_err).lower()
                        if "closing transport" in msg or "not connected to voice" in msg:
                            try:
                                await vc.disconnect(force=True)
                            except Exception:
                                pass
                            return await _fresh_connect()
                        raise

                return await _fresh_connect()

            except Exception as e:
                msg = str(e).lower()
                current_vc = self._get_voice_client_for_guild(guild)

                if "already connected" in msg and current_vc and current_vc.is_connected():
                    if current_vc.channel and current_vc.channel.id == voice_channel.id:
                        await _ensure_self_deaf()
                        return current_vc
                    try:
                        await current_vc.move_to(voice_channel)
                        await _ensure_self_deaf()
                        print(f"[tts_voice] Movido para canal {voice_channel.id} na guild {guild.id}")
                        return current_vc
                    except Exception:
                        pass

                if "closing transport" in msg or "not connected to voice" in msg:
                    try:
                        if current_vc:
                            await current_vc.disconnect(force=True)
                    except Exception:
                        pass
                    try:
                        return await _fresh_connect()
                    except Exception as retry_err:
                        print(f"[tts_voice] Erro ao reconectar na guild {guild.id}: {retry_err}")
                        return None

                print(f"[tts_voice] Erro ao conectar na guild {guild.id}: {e}")
                return None

    def _chunk_lines(self, lines: list[str], max_chars: int = 3500) -> list[str]:
        chunks, current, size = [], [], 0
        for line in lines:
            extra = len(line) + 1
            if current and size + extra > max_chars:
                chunks.append("\n".join(current))
                current, size = [line], extra
            else:
                current.append(line)
                size += extra
        if current:
            chunks.append("\n".join(current))
        return chunks

    async def _send_list_embeds(self, interaction: discord.Interaction, *, title: str, lines: list[str], footer: str):
        chunks = self._chunk_lines(lines)
        if not chunks:
            await self._respond(interaction, embed=self._make_embed(title, "Nenhum item encontrado.", ok=False), ephemeral=True)
            return
        for index, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(title=title if len(chunks) == 1 else f"{title} ({index}/{len(chunks)})", description=f"```{chunk}```", color=discord.Color.blurple())
            embed.set_footer(text=footer)
            await self._respond(interaction, embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not getattr(config, "TTS_ENABLED", True):
            return
        if message.author.bot or not message.guild or not message.content:
            return

        db = self._get_db()
        guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(message.guild.id)) if db else {}
        gtts_prefix = str((guild_defaults or {}).get("gtts_prefix", (guild_defaults or {}).get("tts_prefix", ".")) or ".")
        edge_prefix = str((guild_defaults or {}).get("edge_prefix", ",") or ",")
        gcloud_prefix = str((guild_defaults or {}).get("gcloud_prefix", getattr(config, "GOOGLE_CLOUD_TTS_PREFIX", "'")) or getattr(config, "GOOGLE_CLOUD_TTS_PREFIX", "'"))

        bot_prefix = str((guild_defaults or {}).get("bot_prefix", "_") or "_")

        lowered = message.content.strip().lower()
        server_aliases = {
            f"{bot_prefix}panel_server", f"{bot_prefix}panel-server", f"{bot_prefix}panelserver",
            f"{bot_prefix}server_panel", f"{bot_prefix}server-panel", f"{bot_prefix}serverpanel",
            f"{bot_prefix}painel_server", f"{bot_prefix}painel-server", f"{bot_prefix}painelserver",
            f"{bot_prefix}servidor_panel", f"{bot_prefix}servidor-panel", f"{bot_prefix}servidorpanel",
        }
        toggle_aliases = {
            f"{bot_prefix}panel_toggle", f"{bot_prefix}panel-toggle", f"{bot_prefix}paneltoggle",
            f"{bot_prefix}panel_toggles", f"{bot_prefix}panel-toggles", f"{bot_prefix}paneltoggles",
            f"{bot_prefix}toggle_panel", f"{bot_prefix}toggle-panel", f"{bot_prefix}togglepanel",
            f"{bot_prefix}toggles_panel", f"{bot_prefix}toggles-panel", f"{bot_prefix}togglespanel",
        }
        panel_aliases = {f"{bot_prefix}panel", f"{bot_prefix}painel"}

        is_prefix_command = (
            lowered == f"{bot_prefix}clear"
            or lowered == f"{bot_prefix}leave"
            or lowered == f"{bot_prefix}join"
            or lowered == f"{bot_prefix}reset"
            or lowered.startswith(f"{bot_prefix}reset ")
            or lowered == f"{bot_prefix}set lang"
            or lowered.startswith(f"{bot_prefix}set lang ")
            or lowered in panel_aliases
            or lowered in server_aliases
            or lowered in toggle_aliases
        )

        if is_prefix_command:
            if self._was_tts_message_seen(message.id):
                return
            self._mark_tts_message_seen(message.id)

        reset_command = f"{bot_prefix}reset"
        set_lang_command = f"{bot_prefix}set lang"

        if lowered == f"{bot_prefix}clear":
            await self._prefix_clear(message)
            return
        if lowered == f"{bot_prefix}leave":
            await self._prefix_leave(message)
            return
        if lowered == f"{bot_prefix}join":
            await self._prefix_join(message)
            return
        if lowered == reset_command or lowered.startswith(reset_command + " "):
            raw_target = message.content[len(reset_command):].strip()
            await self._prefix_reset_user(message, raw_target)
            return
        if lowered == set_lang_command or lowered.startswith(set_lang_command + " "):
            raw_language = message.content[len(set_lang_command):].strip()
            await self._prefix_set_lang(message, raw_language)
            return
        if lowered in panel_aliases:
            await self._send_prefix_panel(message, panel_type="user")
            return
        if lowered in server_aliases:
            await self._send_prefix_panel(message, panel_type="server")
            return
        if lowered in toggle_aliases:
            await self._send_prefix_panel(message, panel_type="toggle")
            return

        forced_engine = None
        active_prefix = None
        if message.content.startswith(edge_prefix):
            forced_engine = "edge"
            active_prefix = edge_prefix
        elif message.content.startswith(gtts_prefix):
            forced_engine = "gtts"
            active_prefix = gtts_prefix
        elif message.content.startswith(gcloud_prefix):
            forced_engine = "gcloud"
            active_prefix = gcloud_prefix
        else:
            return
        if self._was_tts_message_seen(message.id):
            return
        self._mark_tts_message_seen(message.id)
        author_voice = getattr(message.author, "voice", None)
        if author_voice is None or author_voice.channel is None:
            print("[tts_voice] ignorado | autor não está em call")
            return
        voice_channel = author_voice.channel

        blocked = await self._should_block_for_voice_bot(message.guild, voice_channel)
        if blocked:
            print(f"[tts_voice] bloqueado | outro bot de voz detectado | guild={message.guild.id} canal_voz={voice_channel.id}")
            await self._disconnect_and_clear(message.guild)
            return

        db = self._get_db()
        if db is None:
            print("[tts_voice] ignorado | settings_db indisponível")
            return

        try:
            resolved = await self._maybe_await(db.resolve_tts(message.guild.id, message.author.id))
        except Exception as e:
            print(f"[tts_voice] erro em resolve_tts | guild={message.guild.id} user={message.author.id} erro={e}")
            return

        only_target_enabled = await self._only_target_user_enabled(message.guild.id)
        target_user_id = getattr(config, "ONLY_TTS_USER_ID", 0)
        forced_gtts = False
        if only_target_enabled and target_user_id and message.author.id != target_user_id:
            resolved["engine"] = "gtts"
            resolved["language"] = resolved.get("language") or getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt-br")
            resolved["voice"] = ""
            resolved["rate"] = "+0%"
            resolved["pitch"] = "+0Hz"
            forced_gtts = True

        if forced_engine == "gtts":
            resolved["engine"] = "gtts"
            resolved["language"] = resolved.get("language") or getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt-br")
        elif forced_engine == "edge":
            resolved["engine"] = "edge"
            resolved["voice"] = resolved.get("voice") or "pt-BR-FranciscaNeural"
            resolved["rate"] = resolved.get("rate") or "+0%"
            resolved["pitch"] = resolved.get("pitch") or "+0Hz"
        elif forced_engine == "gcloud":
            resolved["engine"] = "gcloud"
            resolved["language"] = resolved.get("gcloud_language") or str(getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR")
            resolved["voice"] = resolved.get("gcloud_voice") or str(getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A")
            resolved["rate"] = resolved.get("gcloud_rate") or str(getattr(config, "GOOGLE_CLOUD_TTS_SPEAKING_RATE", 1.0) or 1.0)
            resolved["pitch"] = resolved.get("gcloud_pitch") or str(getattr(config, "GOOGLE_CLOUD_TTS_PITCH", 0.0) or 0.0)

        text = self._render_tts_text(message, message.content[len(active_prefix):].strip())
        text = self._apply_author_prefix_if_needed(
            message.guild.id,
            message.author,
            text,
            enabled=self._guild_announce_author_enabled(guild_defaults),
        )
        if not text:
            print("[tts_voice] ignorado | texto vazio após prefixo")
            return

        state = self._get_state(message.guild.id)
        state.last_text_channel_id = getattr(message.channel, "id", None)
        await state.queue.put(QueueItem(guild_id=message.guild.id, channel_id=voice_channel.id, author_id=message.author.id, text=text, engine=resolved["engine"], voice=resolved["voice"], language=resolved["language"], rate=resolved["rate"], pitch=resolved["pitch"]))
        print(f"[tts_voice] trigger TTS | guild={message.guild.id} channel_type={type(message.channel).__name__} user={message.author.id} raw={message.content!r}")
        print(f"[tts_voice] enfileirada | guild={message.guild.id} user={message.author.id} canal_voz={voice_channel.id} engine={resolved['engine']} forced_gtts={forced_gtts}")
        self._ensure_worker(message.guild.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        vc = self._get_voice_client_for_guild(guild)
        if vc is None or not vc.is_connected() or vc.channel is None:
            return
        if await self._block_voice_bot_enabled(guild.id) and self._target_voice_bot_in_channel(vc.channel):
            print(f"[tts_voice] Bot de voz alvo detectado na call | guild={guild.id} channel={vc.channel.id} target_bot_id={self._target_voice_bot_id()}")
            await self._disconnect_and_clear(guild)
            return
        await self._disconnect_if_alone_or_only_bots(guild)


    async def voice_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip().lower()
        voices = self.edge_voice_cache or sorted(self.edge_voice_names)
        voices = [voice for voice in voices if voice.lower().startswith("pt-")]

        results: list[app_commands.Choice[str]] = []
        for voice in voices:
            if current and current not in voice.lower():
                continue
            results.append(app_commands.Choice(name=voice[:100], value=voice))
            if len(results) >= 25:
                break
        return results

    async def language_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        current = (current or "").strip().lower()

        results: list[app_commands.Choice[str]] = []
        for code, name in sorted(self.gtts_languages.items()):
            label = f"{code} — {name}"
            haystack = f"{code} {name}".lower()
            if current and current not in haystack:
                continue
            results.append(app_commands.Choice(name=label[:100], value=code))
            if len(results) >= 25:
                break
        return results


    async def _set_mode_common(self, interaction: discord.Interaction, *, mode: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = validate_mode(mode)
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, engine=value))
            title, desc = "Modo padrão atualizado", f"O modo padrão do servidor agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos gTTS, Edge e Google Cloud continuam escolhendo o motor por mensagem."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, engine=value)
            title, desc = "Modo atualizado", f"O seu modo de TTS agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos gTTS, Edge e Google Cloud continuam escolhendo o motor por mensagem."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_voice_common(self, interaction: discord.Interaction, *, voice: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        if voice not in self.edge_voice_names:
            await self._respond(interaction, embed=self._make_embed("Voz inválida", "Essa voz não foi encontrada na lista do Edge. Use `/tts voices edge` para ver as opções.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, voice=voice))
            title, desc = "Voz padrão atualizada", f"A voz padrão do servidor agora é `{voice}`."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, voice=voice)
            title, desc = "Voz atualizada", f"A sua voz do Edge agora é `{voice}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_language_common(self, interaction: discord.Interaction, *, language: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = str(language or "").strip().lower()
        if value not in self.gtts_languages:
            await self._respond(interaction, embed=self._make_embed("Idioma inválido", "Esse código não foi encontrado na lista do gTTS. Toque em **Ver lista de idiomas** ou tente um destes exemplos: `pt-br`, `en`, `es`, `fr`, `ja`.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, language=value))
            title, desc = "Idioma padrão atualizado", f"O idioma padrão do servidor agora é `{value}`."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, interaction.user.id, language=value)
            title, desc = "Idioma atualizado", f"O seu idioma do gTTS agora é `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_speed_common(self, interaction: discord.Interaction, *, speed: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = self._normalize_rate_value(speed)
        if value is None:
            await self._respond(interaction, embed=self._make_embed("Velocidade inválida", "Use um valor como `10%`, `+10%` ou `-10%`.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, rate=value))
            title, desc = "Velocidade padrão atualizada", f"A velocidade padrão do servidor agora é `{value}`."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, interaction.user.id, rate=value)
            title, desc = "Velocidade atualizada", f"A sua velocidade do Edge agora é `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)

    async def _set_pitch_common(self, interaction: discord.Interaction, *, pitch: str, server: bool):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if server and not await self._require_manage_guild(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        value = self._normalize_pitch_value(pitch)
        if value is None:
            await self._respond(interaction, embed=self._make_embed("Tom inválido", "Use um valor como `10Hz`, `+10Hz` ou `-10Hz`.", ok=False), ephemeral=True)
            return
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, pitch=value))
            title, desc = "Tom padrão atualizado", f"O tom padrão do servidor agora é `{value}`."
        else:
            await self._set_user_tts_and_refresh(interaction.guild.id, interaction.user.id, pitch=value)
            title, desc = "Tom atualizado", f"O seu tom do Edge agora é `{value}`."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)


    @app_commands.command(name="menu", description="Abre um painel guiado para configurar o seu TTS")
    async def menu(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        embed = await self._build_settings_embed(
            interaction.guild.id,
            interaction.user.id,
            server=False,
            panel_kind="user",
            viewer_user_id=interaction.user.id,
        )
        view = self._build_panel_view(interaction.user.id, interaction.guild.id, server=False)
        msg = await self._respond(interaction, embed=embed, view=view, ephemeral=True)
        if isinstance(view, TTSStatusView):
            view.attach_message(msg)
        else:
            view.message = msg



    async def _get_panel_command_mention(self, guild_id: int, panel_kind: str) -> str:
        command_path = {
            "user": "tts menu",
            "server": "tts server menu",
            "toggle": "tts toggle menu",
        }.get(panel_kind, "tts menu")

        try:
            commands_list = await self.bot.tree.fetch_commands()
            for cmd in commands_list:
                if getattr(cmd, "name", None) == "tts":
                    cmd_id = getattr(cmd, "id", None)
                    if cmd_id:
                        return f"</{command_path}:{cmd_id}>"
        except Exception as e:
            print(f"[tts_panel_timeout] falha ao buscar menção do comando: {e!r}")

        return f"`/{command_path}`"

    async def _get_panel_prefix_hint(self, guild_id: int, panel_kind: str) -> str:
        prefix_command = {
            "user": "panel",
            "server": "panel_server",
            "toggle": "panel_toggles",
        }.get(panel_kind, "panel")
        bot_prefix = getattr(config, "BOT_PREFIX", "_")

        db = self._get_db()
        if db is not None and hasattr(db, "get_guild_tts_defaults"):
            try:
                guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild_id))
                bot_prefix = str((guild_defaults or {}).get("bot_prefix") or bot_prefix)
            except Exception:
                pass

        return f"`{bot_prefix}{prefix_command}`"

    async def _build_expired_panel_embed(self, guild_id: int, panel_kind: str) -> discord.Embed:
        slash_mention = await self._get_panel_command_mention(guild_id, panel_kind)
        prefix_hint = await self._get_panel_prefix_hint(guild_id, panel_kind)

        return self._make_embed(
            "Esse painel expirou",
            f"Use o comando de barra {slash_mention} ou prefixo {prefix_hint} para abrir outro painel.",
            ok=False,
        )

    def _build_panel_view(self, owner_id: int, guild_id: int, *, server: bool = False, timeout: float = 180, target_user_id: int | None = None, target_user_name: str | None = None) -> discord.ui.View:
        return TTSMainPanelView(self, owner_id, guild_id, server=server, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)

    def _member_panel_name(self, member: discord.abc.User | None) -> str:
        if member is None:
            return "@usuário"
        name = getattr(member, "name", None) or getattr(member, "display_name", None) or str(member)
        return name if str(name).startswith("@") else f"@{name}"

    async def _resolve_member_from_text(self, guild: discord.Guild, raw: str) -> discord.Member | None:
        query = str(raw or "").strip()
        if not query:
            return None

        mention_match = re.fullmatch(r"<@!?(\d+)>", query)
        if mention_match:
            member_id = int(mention_match.group(1))
            member = guild.get_member(member_id)
            if member is not None:
                return member
            try:
                return await guild.fetch_member(member_id)
            except Exception:
                return None

        if query.isdigit():
            member_id = int(query)
            member = guild.get_member(member_id)
            if member is not None:
                return member
            try:
                return await guild.fetch_member(member_id)
            except Exception:
                return None

        lowered = query.lower()
        exact_matches: list[discord.Member] = []
        fuzzy_matches: list[discord.Member] = []
        for member in guild.members:
            candidates = [
                str(member),
                getattr(member, "display_name", "") or "",
                getattr(member, "global_name", "") or "",
                getattr(member, "name", "") or "",
            ]
            candidate_values = [c.strip() for c in candidates if str(c).strip()]
            if any(c.lower() == lowered for c in candidate_values):
                exact_matches.append(member)
                continue
            if any(lowered in c.lower() for c in candidate_values):
                fuzzy_matches.append(member)

        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0]
        return None

    def _normalize_language_query(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKD", str(value or "").strip().lower())
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        normalized = re.sub(r"[^a-z0-9\-\s]", " ", normalized)
        return re.sub(r"\s+", " ", normalized).strip()

    def _resolve_gtts_language_input(self, raw_language: str) -> tuple[str | None, str | None]:
        value = str(raw_language or "").strip()
        if not value:
            return None, None

        normalized = self._normalize_language_query(value)
        candidates = [normalized]
        if normalized:
            candidates.extend({normalized.replace("_", "-"), normalized.replace(" ", "-"), normalized.replace("-", " ")})

        for candidate in candidates:
            code = self.gtts_language_aliases.get(candidate)
            if code and code in self.gtts_languages:
                return code, self.gtts_languages.get(code)

        raw_code = value.strip().lower().replace("_", "-")
        if raw_code in self.gtts_languages:
            return raw_code, self.gtts_languages.get(raw_code)

        return None, None

    async def _prefix_set_lang(self, message: discord.Message, raw_language: str):
        if message.guild is None:
            return

        value = str(raw_language or "").strip()
        if not value:
            await message.channel.send(embed=self._make_embed("Idioma obrigatório", f"Use esse comando assim: `_set lang português` ou `_set lang pt-br`.", ok=False))
            return

        code, language_name = self._resolve_gtts_language_input(value)
        if code is None:
            await message.channel.send(embed=self._make_embed("Idioma inválido", "Não reconheci esse idioma do gTTS. Use um código como `pt-br`, `pt`, `en`, `es` ou um nome em português como `português` e `espanhol`.", ok=False))
            return

        db = self._get_db()
        if db is None or not hasattr(db, "set_user_tts"):
            await message.channel.send(embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora para alterar o idioma do gTTS.", ok=False))
            return

        history_entry = f"Você alterou o próprio idioma para {code}"
        await self._set_user_tts_and_refresh(message.guild.id, message.author.id, language=code, history_entry=history_entry)

        pretty_name = language_name or code
        await message.channel.send(embed=self._make_embed("Idioma atualizado", f"Seu idioma pessoal do gTTS agora é `{code}` ({pretty_name}).", ok=True))

    async def _prefix_reset_user(self, message: discord.Message, raw_target: str):
        if message.guild is None:
            return
        if not getattr(message.author.guild_permissions, "kick_members", False):
            await message.channel.send(embed=self._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para resetar as configurações de TTS de outro usuário.", ok=False))
            return

        target_text = str(raw_target or "").strip()
        if not target_text:
            await message.channel.send(embed=self._make_embed("Usuário obrigatório", "Use esse comando assim: `reset @usuário`, `reset ID` ou `reset tag`.", ok=False))
            return

        db = self._get_db()
        if db is None or not hasattr(db, "reset_user_tts"):
            await message.channel.send(embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora para resetar as configurações.", ok=False))
            return

        member = await self._resolve_member_from_text(message.guild, target_text)
        if member is None:
            await message.channel.send(embed=self._make_embed("Usuário não encontrado", "Não consegui encontrar esse usuário. Use menção, ID ou tag exata do usuário no servidor.", ok=False))
            return

        history_entry = f"{self._member_panel_name(message.author)} resetou as configurações de TTS de {self._member_panel_name(member)} para os padrões do servidor"
        await self._reset_user_tts_and_refresh(message.guild.id, member.id, history_entry=history_entry)

        await message.channel.send(embed=self._make_embed("Configurações resetadas", f"As configurações de TTS de {self._member_panel_name(member)} agora seguem os padrões do servidor.", ok=True))

    def _resolve_target_user(self, interaction: discord.Interaction, target_user_id: int | None = None, target_user_name: str | None = None) -> tuple[int, str]:
        resolved_id = int(target_user_id or getattr(getattr(interaction, "user", None), "id", 0) or 0)
        resolved_name = str(target_user_name or self._member_panel_name(getattr(interaction, "user", None)))
        return resolved_id, resolved_name

    def _resolve_panel_target_user(
        self,
        interaction: discord.Interaction,
        *,
        server: bool,
        message_id: int | None = None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ) -> tuple[int, str, bool]:
        resolved_id, resolved_name = self._resolve_target_user(interaction, target_user_id, target_user_name)

        if server or not message_id or message_id not in self._public_panel_states:
            return resolved_id, resolved_name, False

        state = self._public_panel_states.get(message_id, {}) or {}
        if state.get("panel_kind") != "user":
            return resolved_id, resolved_name, False

        actor_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
        explicit_target = target_user_id is not None or bool(str(target_user_name or "").strip())

        if explicit_target and resolved_id != actor_id:
            return resolved_id, resolved_name, False

        return actor_id, self._member_panel_name(getattr(interaction, "user", None)), True

    def _build_toggle_view(self, owner_id: int, guild_id: int, *, timeout: float = 180) -> discord.ui.View:
        return TTSTogglePanelView(self, owner_id, guild_id, timeout=timeout)


    async def _announce_panel_change(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        description: str,
    ):
        channel = interaction.channel
        if channel is None:
            return

        try:
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.blurple(),
            )
            if interaction.user and getattr(interaction.user, "display_avatar", None):
                embed.set_author(
                    name=str(interaction.user),
                    icon_url=interaction.user.display_avatar.url,
                )
            embed.set_footer(text="Alteração feita pelo painel de TTS")
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[tts_voice] Falha ao anunciar alteração do painel: {e}")


    def _get_saved_spoken_name(self, guild_id: int | None, user_id: int | None) -> str:
        if not guild_id or not user_id:
            return ""
        db = self._get_db()
        if db is None or not hasattr(db, "get_user_tts"):
            return ""
        try:
            data = db.get_user_tts(int(guild_id), int(user_id)) or {}
        except Exception:
            return ""
        return _normalize_spaces(str((data or {}).get("speaker_name", "") or ""))

    def _get_current_gcloud_voice(self, guild_id: int, user_id: int, *, server: bool = False) -> str:
        db = self._get_db()
        if db is None:
            return str(getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A")
        try:
            if server and hasattr(db, "get_guild_tts_defaults"):
                defaults = db.get_guild_tts_defaults(guild_id) or {}
                return str(defaults.get("gcloud_voice") or getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A")
            resolved = db.resolve_tts(guild_id, user_id) or {}
            return str(resolved.get("gcloud_voice") or getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A")
        except Exception:
            return str(getattr(config, "GOOGLE_CLOUD_TTS_VOICE_NAME", "pt-BR-Standard-A") or "pt-BR-Standard-A")

    def _get_current_gcloud_language(self, guild_id: int, user_id: int, *, server: bool = False) -> str:
        db = self._get_db()
        if db is None:
            return str(getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR")
        try:
            if server and hasattr(db, "get_guild_tts_defaults"):
                defaults = db.get_guild_tts_defaults(guild_id) or {}
                return str(defaults.get("gcloud_language") or getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR")
            resolved = db.resolve_tts(guild_id, user_id) or {}
            return str(resolved.get("gcloud_language") or getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR")
        except Exception:
            return str(getattr(config, "GOOGLE_CLOUD_TTS_LANGUAGE_CODE", "pt-BR") or "pt-BR")

    def _validate_gcloud_language_input(self, raw_value: str) -> tuple[str | None, str | None]:
        value = _normalize_spaces(str(raw_value or "")).replace(" ", "")
        if not value:
            return None, "O idioma do Google Cloud não pode ficar vazio."
        if len(value) > 16 or not re.fullmatch(r"[A-Za-z0-9-]+", value):
            return None, "Use um código de idioma válido, como `pt-BR` ou `en-US`."
        return value, None

    def _validate_gcloud_voice_input(self, raw_value: str) -> tuple[str | None, str | None]:
        value = _normalize_spaces(str(raw_value or ""))
        if not value:
            return None, "A voz do Google Cloud não pode ficar vazia."
        value = value.replace(" ", "")
        if len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            return None, "Use um nome de voz válido, como `pt-BR-Standard-A`."
        return value, None

    def _normalize_gcloud_rate_value(self, raw_value: str | float) -> str:
        try:
            numeric = float(str(raw_value).strip().replace(",", "."))
        except Exception:
            numeric = float(getattr(config, "GOOGLE_CLOUD_TTS_SPEAKING_RATE", 1.0) or 1.0)
        numeric = max(0.25, min(2.0, numeric))
        return f"{numeric:.2f}".rstrip("0").rstrip(".")

    def _normalize_gcloud_pitch_value(self, raw_value: str | float) -> str:
        try:
            numeric = float(str(raw_value).strip().replace(",", "."))
        except Exception:
            numeric = float(getattr(config, "GOOGLE_CLOUD_TTS_PITCH", 0.0) or 0.0)
        numeric = max(-20.0, min(20.0, numeric))
        if abs(numeric - round(numeric)) < 1e-9:
            return str(int(round(numeric)))
        return f"{numeric:.2f}".rstrip("0").rstrip(".")

    def _validate_spoken_name_input(self, raw_value: str) -> tuple[str | None, str | None]:
        value = _normalize_spaces(str(raw_value or ""))
        if not value:
            return "", None
        if not _looks_pronounceable_for_tts(value):
            return None, "Esse apelido tem caracteres que o TTS não consegue pronunciar bem. Use letras, números, espaço, ponto, traço ou underline."
        spoken = _speech_name(value)
        if not spoken or not _looks_pronounceable_for_tts(spoken):
            return None, "Esse apelido não ficou pronunciável depois da normalização do TTS."
        return spoken[:32], None

    def _resolve_spoken_name(self, member: discord.abc.User | None, *, guild_id: int | None = None) -> tuple[str, str]:
        if member is None:
            return "usuário", "padrão"

        saved_spoken_name = self._get_saved_spoken_name(guild_id, getattr(member, "id", None))
        if saved_spoken_name:
            spoken = _speech_name(saved_spoken_name)
            if spoken and _looks_pronounceable_for_tts(spoken):
                return spoken, "personalizado"

        display_name = _normalize_spaces(getattr(member, "display_name", None) or "")
        username = _normalize_spaces(getattr(member, "name", None) or "")

        if _looks_pronounceable_for_tts(display_name):
            spoken = _speech_name(display_name)
            if spoken:
                return spoken, "apelido do servidor"

        if _looks_pronounceable_for_tts(username):
            spoken = _speech_name(username)
            if spoken:
                return spoken, "nome de usuário"

        return "usuário", "padrão"

    def _tts_user_reference(self, member: discord.abc.User | None, *, guild_id: int | None = None) -> str:
        spoken, _ = self._resolve_spoken_name(member, guild_id=guild_id)
        return spoken

    def _tts_role_reference(self, role: discord.Role | None) -> str:
        name = _normalize_spaces(getattr(role, "name", None) or "")
        if _looks_pronounceable_for_tts(name):
            spoken = _speech_name(name)
            if spoken:
                return f"cargo {spoken}"
        return "cargo do discord"

    def _tts_channel_reference(self, channel) -> str:
        name = _normalize_spaces(getattr(channel, "name", None) or "")
        if _looks_pronounceable_for_tts(name):
            spoken = _speech_name(name)
            if spoken:
                return f"canal {spoken}"
        return "canal do discord"

    def _tts_link_reference(self, url: str, *, guild: discord.Guild | None = None) -> str:
        cleaned_url = str(url or "").strip().rstrip(".,!?)]}")
        match = DISCORD_CHANNEL_URL_PATTERN.fullmatch(cleaned_url)
        if match and guild is not None:
            channel_id = int(match.group(2))
            channel = guild.get_channel(channel_id)
            return self._tts_channel_reference(channel)

        try:
            parsed = urlparse(cleaned_url)
        except Exception:
            return "link"

        domain = _extract_primary_domain(parsed.hostname or "")
        if _looks_pronounceable_for_tts(domain):
            spoken = _speech_name(domain)
            if spoken:
                return f"link do {spoken}"
        return "link"

    def _tts_attachment_descriptions(self, attachments) -> list[str]:
        descriptions: list[str] = []
        for attachment in attachments or []:
            content_type = str(getattr(attachment, "content_type", "") or "").lower()
            filename = str(getattr(attachment, "filename", "") or "").lower()
            if content_type == "image/gif" or filename.endswith(".gif"):
                descriptions.append("Anexo em GIF")
            elif content_type.startswith("image/") or filename.endswith(_ATTACHMENT_IMAGE_EXTENSIONS):
                descriptions.append("Anexo de imagem")
            elif content_type.startswith("video/") or filename.endswith(_ATTACHMENT_VIDEO_EXTENSIONS):
                descriptions.append("Anexo de vídeo")
        return descriptions

    def _append_tts_descriptions(self, text: str, descriptions: list[str]) -> str:
        text = _normalize_spaces(text)
        descriptions = [_normalize_spaces(item) for item in (descriptions or []) if _normalize_spaces(item)]
        if not descriptions:
            return text
        suffix = ". ".join(descriptions)
        if not text:
            return suffix
        if text.endswith((".", "!", "?", "…")):
            return f"{text} {suffix}"
        return f"{text}. {suffix}"

    def _render_tts_text(self, message: discord.Message, raw_text: str) -> str:
        text = _replace_custom_emojis_for_tts(raw_text)

        def replace_user(match: re.Match[str]) -> str:
            member_id = int(match.group(1))
            member = message.guild.get_member(member_id) if message.guild else None
            if member is None:
                member = next((m for m in getattr(message, "mentions", []) if getattr(m, "id", None) == member_id), None)
            return self._tts_user_reference(member, guild_id=getattr(message.guild, "id", None))

        def replace_role(match: re.Match[str]) -> str:
            role_id = int(match.group(1))
            role = message.guild.get_role(role_id) if message.guild else None
            if role is None:
                role = next((r for r in getattr(message, "role_mentions", []) if getattr(r, "id", None) == role_id), None)
            return self._tts_role_reference(role)

        def replace_channel(match: re.Match[str]) -> str:
            channel_id = int(match.group(1))
            channel = message.guild.get_channel(channel_id) if message.guild else None
            return self._tts_channel_reference(channel)

        def replace_url(match: re.Match[str]) -> str:
            return self._tts_link_reference(match.group(0), guild=message.guild)

        text = USER_MENTION_PATTERN.sub(replace_user, text)
        text = ROLE_MENTION_PATTERN.sub(replace_role, text)
        text = CHANNEL_MENTION_PATTERN.sub(replace_channel, text)
        text = URL_PATTERN.sub(replace_url, text)
        text = _expand_abbreviations_for_tts(text)
        return self._append_tts_descriptions(text, self._tts_attachment_descriptions(getattr(message, "attachments", [])))

    def _user_history_text(self, interaction: discord.Interaction, what: str, value: str, *, message_id: int | None = None, target_user_id: int | None = None, target_user_name: str | None = None) -> str:
        actor_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
        target_id = int(target_user_id or actor_id or 0)
        target_name = str(target_user_name or self._panel_actor_name(interaction))
        action_text = f"alterou {what} para {value}"
        if target_id == actor_id:
            return self._encode_public_owner_history(actor_id, self._panel_actor_name(interaction), action_text)
        return f"{self._panel_actor_name(interaction)} alterou {what} de {target_name} para {value}"

    def _server_history_text(self, interaction: discord.Interaction, what: str, value: str) -> str:
        return f"{self._panel_actor_name(interaction)} alterou {what} para {value}"

    def _toggle_history_text(self, interaction: discord.Interaction, text: str) -> str:
        return f"{self._panel_actor_name(interaction)} {text}"


    async def _build_toggle_embed(
        self,
        guild_id: int,
        user_id: int,
        *,
        last_changes: list[str] | None = None,
        message_id: int | None = None,
        target_user_name: str | None = None,
        viewer_user_id: int | None = None,
    ) -> discord.Embed:
        db = self._get_db()
        panel_history = await self._maybe_await(db.get_panel_history(guild_id, user_id)) if db and hasattr(db, "get_panel_history") else {}
        stored_last_changes = list((panel_history or {}).get("toggle_last_changes", []) or [])
        if not stored_last_changes:
            stored_last = str((panel_history or {}).get("toggle_last_change", "") or "")
            stored_last_changes = [stored_last] if stored_last else []
        if last_changes is None:
            last_changes = stored_last_changes
        last_changes = self._resolve_last_changes(stored_changes=last_changes, message_id=message_id)

        guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild_id)) if db else {}
        guild_defaults = guild_defaults or {}

        embed = discord.Embed(
            title="Painel de toggles do TTS",
            description="Use os botões abaixo para ligar ou desligar os modos especiais do TTS.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Bloqueio por outro bot",
            value="`Ativado`" if bool(guild_defaults.get("block_voice_bot", False)) else "`Desativado`",
            inline=True,
        )
        embed.add_field(
            name="Modo Cuca",
            value="`Ativado`" if bool(guild_defaults.get("only_target_user", False)) else "`Desativado`",
            inline=True,
        )
        embed.add_field(
            name="Auto leave",
            value="`Ativado`" if bool(guild_defaults.get("auto_leave", True)) else "`Desativado`",
            inline=True,
        )
        history_text = self._format_history_entries(last_changes or [], viewer_user_id=viewer_user_id or user_id, message_id=message_id)
        if history_text:
            embed.add_field(name="Últimas alterações", value=history_text, inline=False)
        return embed

    def _setting_origin_label(self, user_settings: dict, key: str) -> str:
        return "Usuário" if str((user_settings or {}).get(key, "") or "").strip() else "Servidor"

    def _status_bool(self, value: bool) -> str:
        return "Ativado" if bool(value) else "Desativado"

    def _status_badge(self, value: bool, *, on: str = "Ativo", off: str = "Inativo") -> str:
        return f"🟢 {on}" if bool(value) else f"⚫ {off}"

    def _status_source_badge(self, source: str) -> str:
        source = str(source or "Servidor")
        return f"👤 {source}" if source == "Usuário" else f"🏠 {source}"

    def _status_engine_label(self, engine: str) -> str:
        value = str(engine or "gtts").lower()
        if value == "edge":
            return "🗣️ Edge"
        if value == "gcloud":
            return "☁️ Google Cloud"
        return "🌐 gTTS"

    def _status_voice_channel_text(self, guild: discord.Guild | None, target_user_id: int) -> str:
        if guild is None:
            return "Não disponível"
        member = guild.get_member(int(target_user_id or 0))
        voice_state = getattr(member, "voice", None)
        channel = getattr(voice_state, "channel", None)
        if channel is None:
            return "Fora de call"
        return getattr(channel, "mention", None) or f"`{getattr(channel, 'name', 'Desconhecido')}`"

    def _spoken_name_status_text(self, guild_id: int, member: discord.abc.User | None, *, resolved: dict | None = None) -> tuple[str, str]:
        active_name, active_source = self._resolve_spoken_name(member, guild_id=guild_id)
        custom_name = _normalize_spaces(str((resolved or {}).get("speaker_name", "") or ""))
        if custom_name:
            return f"`{active_name}` (personalizado)", active_source
        if active_source == "apelido do servidor":
            return f"`{active_name}` (apelido do servidor)", active_source
        if active_source == "nome de usuário":
            return f"`{active_name}` (nome de usuário)", active_source
        return f"`{active_name}` (padrão)", active_source

    async def _build_status_embed(
        self,
        guild_id: int,
        user_id: int,
        *,
        viewer_user_id: int | None = None,
        target_user_name: str | None = None,
        public: bool = False,
    ) -> discord.Embed:
        db = self._get_db()
        user_settings = await self._maybe_await(db.get_user_tts(guild_id, user_id)) if db else {}
        resolved = await self._maybe_await(db.resolve_tts(guild_id, user_id)) if db else {}

        user_settings = user_settings or {}
        resolved = resolved or {}

        guild = self.bot.get_guild(guild_id)
        vc = self._get_voice_client_for_guild(guild)
        state = self.guild_states.get(guild_id)
        queue_size = int(getattr(getattr(state, "queue", None), "qsize", lambda: 0)() if state else 0)
        is_connected = bool(vc and vc.is_connected())
        is_playing = bool(vc and (vc.is_playing() or vc.is_paused()))
        bot_channel = getattr(getattr(vc, "channel", None), "mention", None) or (f"`{getattr(getattr(vc, 'channel', None), 'name', 'Desconhecido')}`" if getattr(vc, "channel", None) is not None else "Desconectado")
        user_channel = self._status_voice_channel_text(guild, user_id)
        member = guild.get_member(user_id) if guild else None
        target_name = str(target_user_name or self._member_panel_name(member))

        if public:
            title = f"📡 Status de TTS de {target_name}"
            description = f"Resumo público das configurações atuais de TTS de {target_name}."
        elif int(user_id or 0) != int(viewer_user_id or user_id or 0):
            title = f"📡 Status de TTS de {target_name}"
            description = f"Resumo das configurações atuais de TTS de {target_name}."
        else:
            title = "📡 Status do TTS"
            description = "Resumo das suas configurações atuais de TTS neste servidor."

        color = discord.Color.green() if is_playing else (discord.Color.blurple() if is_connected else discord.Color.orange())
        embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())

        if member is not None:
            avatar = getattr(getattr(member, "display_avatar", None), "url", None)
            if avatar:
                embed.set_thumbnail(url=avatar)

        queue_label = f"{queue_size} item" + ("" if queue_size == 1 else "s")
        summary_bits = [
            self._status_badge(is_connected, on="Conectado", off="Desconectado"),
            self._status_badge(is_playing, on="Falando", off="Em espera"),
            f"📚 Fila: `{queue_label}`",
            f"🎙️ Engine: `{resolved.get('engine', 'gtts')}`",
        ]
        embed.add_field(name="Resumo rápido", value=" • ".join(summary_bits), inline=False)

        customized_keys = [
            label
            for key, label in (
                ("engine", "engine"),
                ("voice", "voz do Edge"),
                ("language", "idioma do gTTS"),
                ("rate", "velocidade do Edge"),
                ("pitch", "tom do Edge"),
                ("gcloud_voice", "voz do Google"),
                ("gcloud_language", "idioma do Google"),
                ("gcloud_rate", "velocidade do Google"),
                ("gcloud_pitch", "tom do Google"),
                ("speaker_name", "apelido falado"),
            )
            if str((user_settings or {}).get(key, "") or "").strip()
        ]
        source_line = (
            "**Origem:** usando padrões do servidor"
            if not customized_keys
            else "**Personalizado:** " + ", ".join(customized_keys)
        )

        spoken_name_text, _ = self._spoken_name_status_text(guild_id, member, resolved=resolved)
        embed.add_field(
            name="🎛️ Configuração ativa",
            value=(
                f"**Engine:** {self._status_engine_label(str(resolved.get('engine', 'gtts')))}\n"
                f"**gTTS idioma:** `{resolved.get('gtts_language', resolved.get('language', 'Não definido'))}`\n"
                f"**Edge voz:** `{resolved.get('edge_voice', resolved.get('voice', 'Não definido'))}`\n"
                f"**Edge velocidade:** `{resolved.get('edge_rate', resolved.get('rate', '+0%'))}`\n"
                f"**Edge tom:** `{resolved.get('edge_pitch', resolved.get('pitch', '+0Hz'))}`\n"
                f"**Google idioma:** `{resolved.get('gcloud_language', getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR'))}`\n"
                f"**Google voz:** `{resolved.get('gcloud_voice', getattr(config, 'GOOGLE_CLOUD_TTS_VOICE_NAME', 'pt-BR-Standard-A'))}`\n"
                f"**Google velocidade:** `{resolved.get('gcloud_rate', str(getattr(config, 'GOOGLE_CLOUD_TTS_SPEAKING_RATE', 1.0)))}`\n"
                f"**Google tom:** `{resolved.get('gcloud_pitch', str(getattr(config, 'GOOGLE_CLOUD_TTS_PITCH', 0.0)))}`\n"
                f"**Apelido falado:** {spoken_name_text}\n"
                f"{source_line}"
            ),
            inline=False,
        )
        embed.add_field(
            name="🛰️ Estado atual",
            value=(
                f"**Você:** {user_channel}\n"
                f"**Bot:** {bot_channel}\n"
                f"**Conexão:** {self._status_badge(is_connected, on='Conectado', off='Desconectado')}\n"
                f"**Reprodução:** {self._status_badge(is_playing, on='Falando agora', off='Parado')}\n"
                f"**Fila:** `{queue_label}`"
            ),
            inline=False,
        )

        panel_history = await self._maybe_await(db.get_panel_history(guild_id, user_id)) if db and hasattr(db, "get_panel_history") else {}
        stored_last_changes = list((panel_history or {}).get("user_last_changes", []) or [])
        if not stored_last_changes:
            stored_last = str((panel_history or {}).get("user_last_change", "") or "")
            stored_last_changes = [stored_last] if stored_last else []
        history_text = self._format_status_history_entries(stored_last_changes or [], viewer_user_id=viewer_user_id or user_id)
        if history_text:
            embed.add_field(name="🕘 Últimas alterações", value=history_text, inline=False)

        footer_text = "Sincronizado com o histórico do seu tts menu." if not public and int(user_id or 0) == int(viewer_user_id or 0) else "Sincronizado com o histórico do tts menu."
        embed.set_footer(text=footer_text)
        return embed

    def _build_status_view(self, owner_id: int, guild_id: int, *, target_user_id: int | None = None, target_user_name: str | None = None, timeout: float = 180) -> discord.ui.View:
        return TTSStatusView(self, owner_id, guild_id, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)

    async def _build_settings_embed(
        self,
        guild_id: int,
        user_id: int,
        *,
        server: bool = False,
        panel_kind: str = "user",
        last_changes: list[str] | None = None,
        message_id: int | None = None,
        target_user_name: str | None = None,
        viewer_user_id: int | None = None,
    ) -> discord.Embed:
        db = self._get_db()
        guild_defaults = await self._maybe_await(db.get_guild_tts_defaults(guild_id)) if db else {}
        user_settings = await self._maybe_await(db.get_user_tts(guild_id, user_id)) if db else {}
        resolved = await self._maybe_await(db.resolve_tts(guild_id, user_id)) if db else {}

        guild_defaults = guild_defaults or {}
        user_settings = user_settings or {}
        resolved = resolved or {}

        panel_history = await self._maybe_await(db.get_panel_history(guild_id, user_id)) if db and hasattr(db, "get_panel_history") else {}
        stored_last_changes: list[str] = []
        if panel_kind == "server":
            stored_last_changes = list((panel_history or {}).get("server_last_changes", []) or [])
            if not stored_last_changes:
                stored_last = str((panel_history or {}).get("server_last_change", "") or "")
                stored_last_changes = [stored_last] if stored_last else []
        elif panel_kind == "toggle":
            stored_last_changes = list((panel_history or {}).get("toggle_last_changes", []) or [])
            if not stored_last_changes:
                stored_last = str((panel_history or {}).get("toggle_last_change", "") or "")
                stored_last_changes = [stored_last] if stored_last else []
        else:
            stored_last_changes = list((panel_history or {}).get("user_last_changes", []) or [])
            if not stored_last_changes:
                stored_last = str((panel_history or {}).get("user_last_change", "") or "")
                stored_last_changes = [stored_last] if stored_last else []

        if last_changes is None:
            last_changes = stored_last_changes
        last_changes = self._resolve_last_changes(stored_changes=last_changes, message_id=message_id)

        if server:
            title = "Painel de TTS do servidor"
            description = "Use os botões abaixo para ajustar os padrões do servidor. gTTS, Edge e Google Cloud têm controles separados por prefixo."
        elif target_user_name and int(user_id or 0) != int(viewer_user_id or user_id or 0):
            title = f"Painel de TTS de {target_user_name}"
            description = f"Use os botões abaixo para alterar as configurações de {target_user_name}. gTTS, Edge e Google Cloud têm controles separados por prefixo."
        else:
            title = "Painel de TTS"
            description = "Use os botões abaixo para alterar as suas configurações. gTTS, Edge e Google Cloud têm controles separados por prefixo."

        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        embed.add_field(name="Voz do Edge", value=f"`{resolved.get('edge_voice', resolved.get('voice', 'Não definido'))}`", inline=True)
        embed.add_field(name="Idioma do gTTS", value=f"`{resolved.get('gtts_language', resolved.get('language', 'Não definido'))}`", inline=True)
        embed.add_field(name="Velocidade do Edge", value=f"`{resolved.get('edge_rate', resolved.get('rate', '+0%'))}`", inline=True)
        embed.add_field(name="Tom do Edge", value=f"`{resolved.get('edge_pitch', resolved.get('pitch', '+0Hz'))}`", inline=True)
        embed.add_field(name="Idioma do Google", value=f"`{resolved.get('gcloud_language', getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR'))}`", inline=True)
        embed.add_field(name="Voz do Google", value=f"`{resolved.get('gcloud_voice', getattr(config, 'GOOGLE_CLOUD_TTS_VOICE_NAME', 'pt-BR-Standard-A'))}`", inline=True)
        embed.add_field(name="Velocidade do Google", value=f"`{resolved.get('gcloud_rate', str(getattr(config, 'GOOGLE_CLOUD_TTS_SPEAKING_RATE', 1.0)))}`", inline=True)
        embed.add_field(name="Tom do Google", value=f"`{resolved.get('gcloud_pitch', str(getattr(config, 'GOOGLE_CLOUD_TTS_PITCH', 0.0)))}`", inline=True)
        if not server:
            member = self.bot.get_guild(guild_id).get_member(user_id) if self.bot.get_guild(guild_id) else None
            spoken_name_text, _ = self._spoken_name_status_text(guild_id, member, resolved=resolved)
            embed.add_field(name="Apelido falado", value=spoken_name_text, inline=True)
        if server:
            embed.add_field(name="Prefixo do bot", value=f"`{guild_defaults.get('bot_prefix', '_')}`", inline=True)
            embed.add_field(name="Prefixo do modo gTTS", value=f"`{guild_defaults.get('gtts_prefix', guild_defaults.get('tts_prefix', '.'))}`", inline=True)
            embed.add_field(name="Prefixo do modo Edge", value=f"`{guild_defaults.get('edge_prefix', ',')}`", inline=True)
            google_prefix = guild_defaults.get('gcloud_prefix', getattr(config, 'GOOGLE_CLOUD_TTS_PREFIX', "'"))
            embed.add_field(name="Prefixo do Google", value=f"`{google_prefix}`", inline=True)
            embed.add_field(name="Autor antes da frase", value="`Ativado`" if bool(guild_defaults.get('announce_author', False)) else "`Desativado`", inline=True)
        history_text = self._format_history_entries(last_changes or [], viewer_user_id=viewer_user_id or user_id, message_id=message_id)
        if history_text:
            embed.add_field(name="Últimas alterações", value=history_text, inline=False)

        embed.set_footer(text="Os ajustes de gTTS, Edge e Google Cloud ficam salvos no banco." if server or panel_kind == "toggle" else "As alterações desse painel ficam salvas para o usuário correspondente.")
        return embed


    async def _apply_server_prefix_from_modal(
        self,
        interaction: discord.Interaction,
        *,
        prefix_kind: str,
        prefix: str,
        panel_message: discord.Message,
    ):
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para alterar os prefixos do servidor por esse painel.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        cleaned = (prefix or "").strip()
        if not cleaned:
            await interaction.response.send_message(
                embed=self._make_embed("Prefixo inválido", "O prefixo não pode ficar vazio.", ok=False),
                ephemeral=True,
            )
            return

        cleaned = cleaned[:8]

        if prefix_kind == "bot":
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, bot_prefix=cleaned))
            desc = f"O prefixo do bot do servidor agora é `{cleaned}`"
            history_entry = self._server_history_text(interaction, "o prefixo dos comandos", self._quote_value(cleaned))
            title = "Prefixo do bot atualizado"
        elif prefix_kind == "edge":
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, edge_prefix=cleaned))
            desc = f"O prefixo do modo Edge do servidor agora é `{cleaned}`"
            history_entry = self._server_history_text(interaction, "o prefixo do modo Edge", self._quote_value(cleaned))
            title = "Prefixo do modo Edge atualizado"
        elif prefix_kind == "gcloud":
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, gcloud_prefix=cleaned))
            desc = f"O prefixo do Google Cloud do servidor agora é `{cleaned}`"
            history_entry = self._server_history_text(interaction, "o prefixo do Google Cloud", self._quote_value(cleaned))
            title = "Prefixo do Google Cloud atualizado"
        else:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, gtts_prefix=cleaned, tts_prefix=cleaned))
            desc = f"O prefixo do modo gTTS do servidor agora é `{cleaned}`"
            history_entry = self._server_history_text(interaction, "o prefixo do modo gTTS", self._quote_value(cleaned))
            title = "Prefixo do modo gTTS atualizado"

        await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        self._append_public_panel_history(getattr(panel_message, "id", None), history_entry)
        last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
        embed = await self._build_settings_embed(
            interaction.guild.id,
            interaction.user.id,
            server=True,
            panel_kind="server",
            last_changes=last_changes,
            message_id=getattr(panel_message, "id", None),
        )
        view = self._build_panel_view(0 if getattr(panel_message, "id", None) in self._public_panel_states else interaction.user.id, interaction.guild.id, server=True)
        view.message = panel_message
        edited = False
        try:
            if getattr(interaction, "message", None) is not None and getattr(interaction.message, "id", None) == getattr(panel_message, "id", None):
                await interaction.response.edit_message(embed=embed, view=view)
                edited = True
            else:
                await panel_message.edit(embed=embed, view=view)
                edited = True
        except discord.NotFound:
            print("[tts_panel] painel antigo não existe mais; seguindo sem editar")
        except Exception as e:
            print(f"[tts_panel] falha ao editar painel: {e!r}")

        if edited:
            await interaction.followup.send(
                embed=self._make_embed(title, desc, ok=True),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=self._make_embed(title, desc, ok=True),
                ephemeral=True,
            )
        await self._announce_panel_change(
            interaction,
            title=title,
            description=desc,
            target_message=panel_message,
        )

    async def _panel_update_after_change(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
        view: discord.ui.View,
        title: str,
        description: str,
        target_message: discord.Message | None = None,
    ):
        edited = False
        message_to_edit = target_message or getattr(interaction, "message", None)
        current_interaction_message = getattr(interaction, "message", None)

        if message_to_edit is not None and hasattr(view, "message"):
            view.message = message_to_edit

        try:
            if (
                message_to_edit is not None
                and current_interaction_message is not None
                and getattr(current_interaction_message, "id", None) == getattr(message_to_edit, "id", None)
                and not interaction.response.is_done()
            ):
                await interaction.response.edit_message(embed=embed, view=view)
                edited = True
        except discord.NotFound as e:
            print(f"[tts_panel] falha ao editar via interaction.response.edit_message: {e!r}")
        except Exception as e:
            print(f"[tts_panel] falha ao editar via interaction.response.edit_message: {e!r}")

        if not edited and message_to_edit is not None:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True, thinking=False)
                await message_to_edit.edit(embed=embed, view=view)
                edited = True
            except discord.NotFound as e:
                print(f"[tts_panel] painel alvo não existe mais via message.edit: {e!r}")
            except Exception as e:
                print(f"[tts_panel] falha ao editar painel alvo via message.edit: {e!r}")

        if not edited and message_to_edit is not None:
            try:
                await interaction.followup.edit_message(message_id=message_to_edit.id, embed=embed, view=view)
                edited = True
            except discord.NotFound as e:
                print(f"[tts_panel] painel alvo não existe mais via followup.edit_message: {e!r}")
            except Exception as e:
                print(f"[tts_panel] falha ao editar painel alvo via followup.edit_message: {e!r}")

        if not edited and current_interaction_message is not None:
            try:
                if hasattr(view, "message"):
                    view.message = current_interaction_message
                if not interaction.response.is_done():
                    await interaction.response.edit_message(embed=embed, view=view)
                else:
                    await interaction.followup.edit_message(message_id=current_interaction_message.id, embed=embed, view=view)
                edited = True
            except discord.NotFound as e:
                print(f"[tts_panel] falha ao editar a mensagem atual: {e!r}")
            except Exception as e:
                print(f"[tts_panel] falha ao editar a mensagem atual: {e!r}")

        if not edited:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        embed=embed,
                        view=view,
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        embed=embed,
                        view=view,
                        ephemeral=True,
                    )
            except Exception as e:
                print(f"[tts_panel] falha ao responder followup: {e!r}")



    async def _apply_mode_from_panel(self, interaction: discord.Interaction, mode: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        if server and not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        value = validate_mode(mode)
        panel_message, message_id = self._resolve_public_panel_message(interaction, source_panel_message)
        effective_user_id, effective_user_name, is_public_user_panel = self._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, engine=value))
            desc = f"O modo padrão do servidor agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos gTTS, Edge e Google Cloud continuam escolhendo o motor por mensagem."
            history_entry = self._server_history_text(interaction, "o modo padrão do servidor", value)
            await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
        else:
            history_entry = self._user_history_text(interaction, "o próprio modo" if effective_user_id == interaction.user.id else "o modo", value, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, engine=value, history_entry=history_entry)
            desc = f"O modo de TTS de {effective_user_name} agora é `{value}`." if effective_user_id != interaction.user.id else f"O seu modo de TTS agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos gTTS, Edge e Google Cloud continuam escolhendo o motor por mensagem."
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get("user_last_changes", []) or [])

        embed = await self._build_settings_embed(
            interaction.guild.id,
            effective_user_id if not server else interaction.user.id,
            server=server,
            panel_kind="server" if server else "user",
            last_changes=last_changes,
            message_id=message_id,
            target_user_name=effective_user_name if not server else None,
            viewer_user_id=interaction.user.id,
        )
        view_target_user_id = None if server or is_public_user_panel else effective_user_id
        view_target_user_name = None if server or is_public_user_panel else effective_user_name
        view = self._build_panel_view(0 if message_id in self._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
        if panel_message is not None:
            view.message = panel_message
        await self._panel_update_after_change(
            interaction,
            embed=embed,
            view=view,
            title="Modo atualizado",
            description=desc,
            target_message=panel_message,
        )
        if server:
            await self._announce_panel_change(interaction, title="Modo atualizado", description=desc)


    async def _apply_voice_from_panel(self, interaction: discord.Interaction, voice: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        if server and not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        if voice not in self.edge_voice_names and voice not in self.edge_voice_cache:
            await interaction.response.send_message(
                embed=self._make_embed("Voz inválida", "Essa voz não foi encontrada na lista do Edge.", ok=False),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        panel_message, message_id = self._resolve_public_panel_message(interaction, source_panel_message)
        effective_user_id, effective_user_name, is_public_user_panel = self._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, voice=voice))
            desc = f"A voz padrão do servidor agora é `{voice}`."
            history_entry = self._server_history_text(interaction, "a voz padrão do servidor", voice)
            await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
        else:
            history_entry = self._user_history_text(interaction, "a própria voz" if effective_user_id == interaction.user.id else "a voz", voice, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, voice=voice, history_entry=history_entry)
            desc = f"A voz do Edge de {effective_user_name} agora é `{voice}`." if effective_user_id != interaction.user.id else f"A sua voz do Edge agora é `{voice}`."
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get("user_last_changes", []) or [])

        embed = await self._build_settings_embed(
            interaction.guild.id,
            effective_user_id if not server else interaction.user.id,
            server=server,
            panel_kind="server" if server else "user",
            last_changes=last_changes,
            message_id=message_id,
            target_user_name=effective_user_name if not server else None,
            viewer_user_id=interaction.user.id,
        )
        view_target_user_id = None if server or is_public_user_panel else effective_user_id
        view_target_user_name = None if server or is_public_user_panel else effective_user_name
        view = self._build_panel_view(0 if message_id in self._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
        if panel_message is not None:
            view.message = panel_message
        await self._panel_update_after_change(
            interaction,
            embed=embed,
            view=view,
            title="Configuração de TTS atualizada",
            description=desc,
            target_message=panel_message,
        )
        if server:
            await self._announce_panel_change(interaction, title="Configuração de TTS atualizada", description=desc)


    async def _apply_language_from_panel(self, interaction: discord.Interaction, language: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        if server and not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        panel_message, message_id = self._resolve_public_panel_message(interaction, source_panel_message)
        effective_user_id, effective_user_name, is_public_user_panel = self._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, language=language))
            desc = f"O idioma padrão do servidor agora é `{language}`."
            history_entry = self._server_history_text(interaction, "o idioma padrão do servidor", language)
            await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
        else:
            history_entry = self._user_history_text(interaction, "o próprio idioma" if effective_user_id == interaction.user.id else "o idioma", language, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, language=language, history_entry=history_entry)
            desc = f"O idioma do gtts de {effective_user_name} agora é `{language}`." if effective_user_id != interaction.user.id else f"O seu idioma do gtts agora é `{language}`."
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get("user_last_changes", []) or [])

        embed = await self._build_settings_embed(
            interaction.guild.id,
            effective_user_id if not server else interaction.user.id,
            server=server,
            panel_kind="server" if server else "user",
            last_changes=last_changes,
            message_id=message_id,
            target_user_name=effective_user_name if not server else None,
            viewer_user_id=interaction.user.id,
        )
        view_target_user_id = None if server or is_public_user_panel else effective_user_id
        view_target_user_name = None if server or is_public_user_panel else effective_user_name
        view = self._build_panel_view(0 if message_id in self._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
        if panel_message is not None:
            view.message = panel_message
        await self._panel_update_after_change(
            interaction,
            embed=embed,
            view=view,
            title="Configuração de TTS atualizada",
            description=desc,
            target_message=panel_message,
        )
        if server:
            await self._announce_panel_change(interaction, title="Configuração de TTS atualizada", description=desc)


    async def _apply_speed_from_panel(self, interaction: discord.Interaction, speed: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        if server and not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        panel_message, message_id = self._resolve_public_panel_message(interaction, source_panel_message)
        effective_user_id, effective_user_name, is_public_user_panel = self._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, rate=speed))
            desc = f"A velocidade padrão do servidor agora é `{speed}`."
            history_entry = self._server_history_text(interaction, "a velocidade padrão do servidor", speed)
            await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
        else:
            history_entry = self._user_history_text(interaction, "a própria velocidade" if effective_user_id == interaction.user.id else "a velocidade", speed, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, rate=speed, history_entry=history_entry)
            desc = f"A velocidade do Edge de {effective_user_name} agora é `{speed}`." if effective_user_id != interaction.user.id else f"A sua velocidade do Edge agora é `{speed}`."
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get("user_last_changes", []) or [])

        embed = await self._build_settings_embed(
            interaction.guild.id,
            effective_user_id if not server else interaction.user.id,
            server=server,
            panel_kind="server" if server else "user",
            last_changes=last_changes,
            message_id=message_id,
            target_user_name=effective_user_name if not server else None,
            viewer_user_id=interaction.user.id,
        )
        view_target_user_id = None if server or is_public_user_panel else effective_user_id
        view_target_user_name = None if server or is_public_user_panel else effective_user_name
        view = self._build_panel_view(0 if message_id in self._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
        if panel_message is not None:
            view.message = panel_message
        await self._panel_update_after_change(
            interaction,
            embed=embed,
            view=view,
            title="Configuração de TTS atualizada",
            description=desc,
            target_message=panel_message,
        )
        if server:
            await self._announce_panel_change(interaction, title="Configuração de TTS atualizada", description=desc)


    async def _apply_pitch_from_panel(self, interaction: discord.Interaction, pitch: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        if server and not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        panel_message, message_id = self._resolve_public_panel_message(interaction, source_panel_message)
        effective_user_id, effective_user_name, is_public_user_panel = self._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, pitch=pitch))
            desc = f"O tom padrão do servidor agora é `{pitch}`."
            history_entry = self._server_history_text(interaction, "o tom padrão do servidor", pitch)
            await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
        else:
            history_entry = self._user_history_text(interaction, "o próprio tom" if effective_user_id == interaction.user.id else "o tom", pitch, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, pitch=pitch, history_entry=history_entry)
            desc = f"O tom do Edge de {effective_user_name} agora é `{pitch}`." if effective_user_id != interaction.user.id else f"O seu tom do Edge agora é `{pitch}`."
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get("user_last_changes", []) or [])

        embed = await self._build_settings_embed(
            interaction.guild.id,
            effective_user_id if not server else interaction.user.id,
            server=server,
            panel_kind="server" if server else "user",
            last_changes=last_changes,
            message_id=message_id,
            target_user_name=effective_user_name if not server else None,
            viewer_user_id=interaction.user.id,
        )
        view_target_user_id = None if server or is_public_user_panel else effective_user_id
        view_target_user_name = None if server or is_public_user_panel else effective_user_name
        view = self._build_panel_view(0 if message_id in self._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
        if panel_message is not None:
            view.message = panel_message
        await self._panel_update_after_change(
            interaction,
            embed=embed,
            view=view,
            title="Configuração de TTS atualizada",
            description=desc,
            target_message=panel_message,
        )
        if server:
            await self._announce_panel_change(interaction, title="Configuração de TTS atualizada", description=desc)


    async def _apply_gcloud_language_from_modal(self, interaction: discord.Interaction, language: str, *, server: bool, panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        await self._apply_gcloud_language_from_panel(interaction, language, server=server, source_panel_message=panel_message, target_user_id=target_user_id, target_user_name=target_user_name)

    async def _apply_gcloud_voice_from_modal(self, interaction: discord.Interaction, voice_name: str, *, server: bool, panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        await self._apply_gcloud_voice_from_panel(interaction, voice_name, server=server, source_panel_message=panel_message, target_user_id=target_user_id, target_user_name=target_user_name)

    async def _apply_gcloud_language_from_panel(self, interaction: discord.Interaction, language: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        if server and not interaction.user.guild_permissions.kick_members:
            await self._respond(interaction, embed=self._make_embed('Sem permissão', 'Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.', ok=False), ephemeral=True)
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed('Banco indisponível', 'Não consegui acessar o banco de dados agora.', ok=False), ephemeral=True)
            return
        value, error = self._validate_gcloud_language_input(language)
        if error or value is None:
            await self._respond(interaction, embed=self._make_embed('Idioma inválido', error or 'Idioma inválido.', ok=False), ephemeral=True)
            return
        panel_message, message_id = self._resolve_public_panel_message(interaction, source_panel_message)
        effective_user_id, effective_user_name, is_public_user_panel = self._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)

        catalog = await self._load_gcloud_voices()
        current_voice = self._get_current_gcloud_voice(interaction.guild.id, effective_user_id, server=server)
        updates: dict[str, str] = {'gcloud_language': value}
        adjusted_voice = ''
        if catalog and not self._gcloud_voice_matches_language(current_voice, value):
            adjusted_voice = self._pick_first_gcloud_voice_for_language(catalog, value)
            if adjusted_voice:
                updates['gcloud_voice'] = adjusted_voice

        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, **updates))
            desc = f"O idioma do Google Cloud do servidor agora é `{value}`."
            history_entry = self._server_history_text(interaction, 'o idioma do Google Cloud do servidor', value)
            if adjusted_voice:
                desc += f" A voz do Google foi ajustada para `{adjusted_voice}` para combinar com o idioma."
                history_entry = self._server_history_text(interaction, 'o idioma do Google Cloud do servidor', f'{value} (voz ajustada para {adjusted_voice})')
            await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get('server_last_changes', []) or [])
        else:
            history_entry = self._user_history_text(interaction, 'o próprio idioma do Google' if effective_user_id == interaction.user.id else 'o idioma do Google', value, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            if adjusted_voice:
                history_entry = self._user_history_text(interaction, 'o próprio idioma do Google' if effective_user_id == interaction.user.id else 'o idioma do Google', f'{value} (voz ajustada para {adjusted_voice})', message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, history_entry=history_entry, **updates)
            desc = f"O idioma do Google Cloud de {effective_user_name} agora é `{value}`." if effective_user_id != interaction.user.id else f"O seu idioma do Google Cloud agora é `{value}`."
            if adjusted_voice:
                desc += f" A voz do Google foi ajustada para `{adjusted_voice}` para combinar com o idioma."
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get('user_last_changes', []) or [])
        embed = await self._build_settings_embed(interaction.guild.id, effective_user_id if not server else interaction.user.id, server=server, panel_kind='server' if server else 'user', last_changes=last_changes, message_id=message_id, target_user_name=effective_user_name if not server else None, viewer_user_id=interaction.user.id)
        view_target_user_id = None if server or is_public_user_panel else effective_user_id
        view_target_user_name = None if server or is_public_user_panel else effective_user_name
        view = self._build_panel_view(0 if message_id in self._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
        if panel_message is not None:
            view.message = panel_message
        await self._panel_update_after_change(interaction, embed=embed, view=view, title='Configuração de TTS atualizada', description=desc, target_message=panel_message)
        if server:
            await self._announce_panel_change(interaction, title='Configuração de TTS atualizada', description=desc)

    async def _apply_gcloud_voice_from_panel(self, interaction: discord.Interaction, voice_name: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        if server and not interaction.user.guild_permissions.kick_members:
            await self._respond(interaction, embed=self._make_embed('Sem permissão', 'Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.', ok=False), ephemeral=True)
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed('Banco indisponível', 'Não consegui acessar o banco de dados agora.', ok=False), ephemeral=True)
            return
        value, error = self._validate_gcloud_voice_input(voice_name)
        if error or value is None:
            await self._respond(interaction, embed=self._make_embed('Voz inválida', error or 'Voz inválida.', ok=False), ephemeral=True)
            return
        panel_message, message_id = self._resolve_public_panel_message(interaction, source_panel_message)
        effective_user_id, effective_user_name, is_public_user_panel = self._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, gcloud_voice=value))
            desc = f"A voz do Google Cloud do servidor agora é `{value}`."
            history_entry = self._server_history_text(interaction, 'a voz do Google Cloud do servidor', value)
            await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get('server_last_changes', []) or [])
        else:
            history_entry = self._user_history_text(interaction, 'a própria voz do Google' if effective_user_id == interaction.user.id else 'a voz do Google', value, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, gcloud_voice=value, history_entry=history_entry)
            desc = f"A voz do Google Cloud de {effective_user_name} agora é `{value}`." if effective_user_id != interaction.user.id else f"A sua voz do Google Cloud agora é `{value}`."
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get('user_last_changes', []) or [])
        embed = await self._build_settings_embed(interaction.guild.id, effective_user_id if not server else interaction.user.id, server=server, panel_kind='server' if server else 'user', last_changes=last_changes, message_id=message_id, target_user_name=effective_user_name if not server else None, viewer_user_id=interaction.user.id)
        view_target_user_id = None if server or is_public_user_panel else effective_user_id
        view_target_user_name = None if server or is_public_user_panel else effective_user_name
        view = self._build_panel_view(0 if message_id in self._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
        if panel_message is not None:
            view.message = panel_message
        await self._panel_update_after_change(interaction, embed=embed, view=view, title='Configuração de TTS atualizada', description=desc, target_message=panel_message)
        if server:
            await self._announce_panel_change(interaction, title='Configuração de TTS atualizada', description=desc)

    async def _apply_gcloud_speed_from_panel(self, interaction: discord.Interaction, speed: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        if server and not interaction.user.guild_permissions.kick_members:
            await self._respond(interaction, embed=self._make_embed('Sem permissão', 'Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.', ok=False), ephemeral=True)
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed('Banco indisponível', 'Não consegui acessar o banco de dados agora.', ok=False), ephemeral=True)
            return
        value = self._normalize_gcloud_rate_value(speed)
        panel_message, message_id = self._resolve_public_panel_message(interaction, source_panel_message)
        effective_user_id, effective_user_name, is_public_user_panel = self._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, gcloud_rate=value))
            desc = f"A velocidade do Google Cloud do servidor agora é `{value}`."
            history_entry = self._server_history_text(interaction, 'a velocidade do Google Cloud do servidor', value)
            await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get('server_last_changes', []) or [])
        else:
            history_entry = self._user_history_text(interaction, 'a própria velocidade do Google' if effective_user_id == interaction.user.id else 'a velocidade do Google', value, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, gcloud_rate=value, history_entry=history_entry)
            desc = f"A velocidade do Google Cloud de {effective_user_name} agora é `{value}`." if effective_user_id != interaction.user.id else f"A sua velocidade do Google Cloud agora é `{value}`."
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get('user_last_changes', []) or [])
        embed = await self._build_settings_embed(interaction.guild.id, effective_user_id if not server else interaction.user.id, server=server, panel_kind='server' if server else 'user', last_changes=last_changes, message_id=message_id, target_user_name=effective_user_name if not server else None, viewer_user_id=interaction.user.id)
        view_target_user_id = None if server or is_public_user_panel else effective_user_id
        view_target_user_name = None if server or is_public_user_panel else effective_user_name
        view = self._build_panel_view(0 if message_id in self._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
        if panel_message is not None:
            view.message = panel_message
        await self._panel_update_after_change(interaction, embed=embed, view=view, title='Configuração de TTS atualizada', description=desc, target_message=panel_message)
        if server:
            await self._announce_panel_change(interaction, title='Configuração de TTS atualizada', description=desc)

    async def _apply_gcloud_pitch_from_panel(self, interaction: discord.Interaction, pitch: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
        if server and not interaction.user.guild_permissions.kick_members:
            await self._respond(interaction, embed=self._make_embed('Sem permissão', 'Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.', ok=False), ephemeral=True)
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed('Banco indisponível', 'Não consegui acessar o banco de dados agora.', ok=False), ephemeral=True)
            return
        value = self._normalize_gcloud_pitch_value(pitch)
        panel_message, message_id = self._resolve_public_panel_message(interaction, source_panel_message)
        effective_user_id, effective_user_name, is_public_user_panel = self._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
        if server:
            await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, gcloud_pitch=value))
            desc = f"O tom do Google Cloud do servidor agora é `{value}`."
            history_entry = self._server_history_text(interaction, 'o tom do Google Cloud do servidor', value)
            await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get('server_last_changes', []) or [])
        else:
            history_entry = self._user_history_text(interaction, 'o próprio tom do Google' if effective_user_id == interaction.user.id else 'o tom do Google', value, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, gcloud_pitch=value, history_entry=history_entry)
            desc = f"O tom do Google Cloud de {effective_user_name} agora é `{value}`." if effective_user_id != interaction.user.id else f"O seu tom do Google Cloud agora é `{value}`."
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get('user_last_changes', []) or [])
        embed = await self._build_settings_embed(interaction.guild.id, effective_user_id if not server else interaction.user.id, server=server, panel_kind='server' if server else 'user', last_changes=last_changes, message_id=message_id, target_user_name=effective_user_name if not server else None, viewer_user_id=interaction.user.id)
        view_target_user_id = None if server or is_public_user_panel else effective_user_id
        view_target_user_name = None if server or is_public_user_panel else effective_user_name
        view = self._build_panel_view(0 if message_id in self._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
        if panel_message is not None:
            view.message = panel_message
        await self._panel_update_after_change(interaction, embed=embed, view=view, title='Configuração de TTS atualizada', description=desc, target_message=panel_message)
        if server:
            await self._announce_panel_change(interaction, title='Configuração de TTS atualizada', description=desc)

    async def _apply_spoken_name_from_modal(
        self,
        interaction: discord.Interaction,
        spoken_name: str,
        *,
        panel_message: discord.Message | None = None,
        target_user_id: int | None = None,
        target_user_name: str | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=self._make_embed("Comando indisponível", "Esse ajuste só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        panel_message, message_id = self._resolve_public_panel_message(interaction, panel_message)
        effective_user_id, effective_user_name, is_public_user_panel = self._resolve_panel_target_user(
            interaction,
            server=False,
            message_id=message_id,
            target_user_id=target_user_id,
            target_user_name=target_user_name,
        )

        validated_name, validation_error = self._validate_spoken_name_input(spoken_name)
        if validation_error:
            await interaction.response.send_message(
                embed=self._make_embed("Apelido inválido", validation_error, ok=False),
                ephemeral=True,
            )
            return

        if validated_name:
            history_entry = self._user_history_text(
                interaction,
                "o apelido falado" if effective_user_id != interaction.user.id else "o próprio apelido falado",
                f"`{validated_name}`",
                message_id=message_id,
                target_user_id=effective_user_id,
                target_user_name=effective_user_name,
            )
            await self._set_user_tts_and_refresh(
                interaction.guild.id,
                effective_user_id,
                speaker_name=validated_name,
                history_entry=history_entry,
            )
            desc = f"O apelido falado de {effective_user_name} agora é `{validated_name}`." if effective_user_id != interaction.user.id else f"O seu apelido falado agora é `{validated_name}`."
        else:
            if effective_user_id == interaction.user.id:
                history_entry = self._encode_public_owner_history(
                    effective_user_id,
                    self._panel_actor_name(interaction),
                    "removeu o próprio apelido falado personalizado",
                )
                desc = "O seu apelido falado voltou para o modo automático."
            else:
                history_entry = f"{self._panel_actor_name(interaction)} removeu o apelido falado personalizado de {effective_user_name}"
                desc = f"O apelido falado de {effective_user_name} voltou para o modo automático."
            await self._set_user_tts_and_refresh(
                interaction.guild.id,
                effective_user_id,
                speaker_name="",
                history_entry=history_entry,
            )

        self._append_public_panel_history(message_id, history_entry)
        last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get("user_last_changes", []) or [])
        embed = await self._build_settings_embed(
            interaction.guild.id,
            effective_user_id,
            server=False,
            panel_kind="user",
            last_changes=last_changes,
            message_id=message_id,
            target_user_name=effective_user_name,
            viewer_user_id=interaction.user.id,
        )
        view_target_user_id = None if is_public_user_panel else effective_user_id
        view_target_user_name = None if is_public_user_panel else effective_user_name
        view = self._build_panel_view(
            0 if message_id in self._public_panel_states else interaction.user.id,
            interaction.guild.id,
            server=False,
            target_user_id=view_target_user_id,
            target_user_name=view_target_user_name,
        )
        if panel_message is not None:
            view.message = panel_message
        await self._panel_update_after_change(
            interaction,
            embed=embed,
            view=view,
            title="Apelido falado atualizado",
            description=desc,
            target_message=panel_message,
        )

    async def _apply_announce_author_from_panel(self, interaction: discord.Interaction, enabled: bool, source_panel_message: discord.Message | None = None):
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para usar esse comando.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        panel_message = source_panel_message or getattr(interaction, "message", None)
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, announce_author=bool(enabled)))
        desc = "Autor antes da frase ativado." if enabled else "Autor antes da frase desativado."
        history_entry = self._toggle_history_text(interaction, "ativou o Autor antes da frase" if enabled else "desativou o Autor antes da frase")
        await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, toggle_last_change=history_entry))
        self._append_public_panel_history(getattr(panel_message, "id", None), history_entry)
        last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("toggle_last_changes", []) or [])
        embed = await self._build_toggle_embed(interaction.guild.id, interaction.user.id, last_changes=last_changes, message_id=getattr(panel_message, "id", None))
        view = self._build_toggle_view(0 if getattr(panel_message, "id", None) in self._public_panel_states else interaction.user.id, interaction.guild.id)
        if panel_message is not None:
            view.message = panel_message
        await self._panel_update_after_change(
            interaction,
            embed=embed,
            view=view,
            title="Modo de TTS atualizado",
            description=desc,
            target_message=panel_message,
        )
        await self._announce_panel_change(interaction, title="Modo de TTS atualizado", description=desc)


    async def _apply_only_target_from_panel(self, interaction: discord.Interaction, enabled: bool, source_panel_message: discord.Message | None = None):
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para usar esse comando.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        panel_message = source_panel_message or getattr(interaction, "message", None)
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, only_target_user=bool(enabled)))
        desc = "Modo Cuca ativado." if enabled else "Modo Cuca desativado."
        history_entry = self._toggle_history_text(interaction, "ativou o Modo Cuca" if enabled else "desativou o Modo Cuca")
        await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, toggle_last_change=history_entry))
        self._append_public_panel_history(getattr(getattr(interaction, "message", None), "id", None), history_entry)
        last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("toggle_last_changes", []) or [])
        embed = await self._build_toggle_embed(interaction.guild.id, interaction.user.id, last_changes=last_changes, message_id=getattr(getattr(interaction, "message", None), "id", None))
        view = self._build_toggle_view(0 if getattr(getattr(interaction, "message", None), "id", None) in self._public_panel_states else interaction.user.id, interaction.guild.id)
        await self._panel_update_after_change(
            interaction,
            embed=embed,
            view=view,
            title="Modo de TTS atualizado",
            description=desc,
            target_message=panel_message,
        )
        await self._announce_panel_change(interaction, title="Modo de TTS atualizado", description=desc)


    async def _apply_block_voice_bot_from_panel(self, interaction: discord.Interaction, enabled: bool, source_panel_message: discord.Message | None = None):
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message(
                embed=self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para usar esse painel.",
                    ok=False,
                ),
                ephemeral=True,
            )
            return

        db = self._get_db()
        if db is None:
            await interaction.response.send_message(
                embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
                ephemeral=True,
            )
            return

        panel_message = source_panel_message or getattr(interaction, "message", None)
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, block_voice_bot=bool(enabled)))
        desc = f"Bloqueio por outro bot {'ativado' if enabled else 'desativado'}."
        history_entry = self._toggle_history_text(interaction, "ativou o Bloqueio por outro bot" if enabled else "desativou o Bloqueio por outro bot")
        await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, toggle_last_change=history_entry))
        self._append_public_panel_history(getattr(getattr(interaction, "message", None), "id", None), history_entry)
        last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("toggle_last_changes", []) or [])
        embed = await self._build_toggle_embed(interaction.guild.id, interaction.user.id, last_changes=last_changes, message_id=getattr(getattr(interaction, "message", None), "id", None))
        view = self._build_toggle_view(0 if getattr(getattr(interaction, "message", None), "id", None) in self._public_panel_states else interaction.user.id, interaction.guild.id)
        await self._panel_update_after_change(
            interaction,
            embed=embed,
            view=view,
            title="Modo de TTS atualizado",
            description=desc,
            target_message=panel_message,
        )
        await self._announce_panel_change(interaction, title="Modo de TTS atualizado", description=desc)

        if enabled:
            await self._disconnect_if_blocked(interaction.guild)

    async def _join_from_panel(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                embed=self._make_embed("Comando indisponível", "Esse botão só pode ser usado dentro de um servidor.", ok=False),
                ephemeral=True,
            )
            return

        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel is None:
            await interaction.response.send_message(
                embed=self._make_embed("Entre em uma call", "Você precisa estar em uma call para usar esse botão.", ok=False),
                ephemeral=True,
            )
            return

        blocked = await self._should_block_for_voice_bot(interaction.guild, user_voice.channel)
        if blocked:
            await interaction.response.send_message(
                embed=self._make_embed("Bloqueado", "Não posso entrar porque o outro bot de voz já está nessa call.", ok=False),
                ephemeral=True,
            )
            return

        vc = await self._ensure_connected(interaction.guild, user_voice.channel)
        if vc is None or not vc.is_connected():
            await interaction.response.send_message(
                embed=self._make_embed("Falha ao conectar", "Não consegui entrar na call agora.", ok=False),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=self._make_embed("Bot conectado", f"Entrei na call `{user_voice.channel.name}`.", ok=True),
            ephemeral=True,
        )



    async def _clear_queue_only(self, guild: discord.Guild | None, *, stop_playback: bool = True) -> int:
        if guild is None:
            return 0

        state = self._get_state(guild.id)
        cleared = 0

        while True:
            try:
                state.queue.get_nowait()
                state.queue.task_done()
                cleared += 1
            except Exception:
                break

        vc = self._get_voice_client_for_guild(guild)
        if stop_playback and vc and vc.is_connected():
            try:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
            except Exception:
                pass

        task = getattr(state, "worker_task", None)
        if task and not task.done():
            task.cancel()
            state.worker_task = None

        return cleared

    async def _prefix_leave(self, message: discord.Message):
        if not message.guild:
            return

        vc = self._get_voice_client_for_guild(message.guild)
        await self._clear_queue_only(message.guild, stop_playback=True)

        if vc and vc.is_connected():
            try:
                await vc.disconnect(force=False)
            except Exception:
                pass

        embed = discord.Embed(
            title="Saindo da call",
            description="Saí da call e limpei a fila do TTS",
            color=discord.Color.red(),
        )
        await message.channel.send(embed=embed)

    async def _prefix_clear(self, message: discord.Message):
        if not message.guild:
            return

        await self._clear_queue_only(message.guild, stop_playback=True)

        try:
            await message.add_reaction("<:r_dot:1480307087522140331>")
        except Exception:
            try:
                await message.add_reaction("🟥")
            except Exception:
                pass

    async def _prefix_join(self, message: discord.Message):
        if not message.guild:
            return

        author_voice = getattr(message.author, "voice", None)
        if author_voice is None or author_voice.channel is None:
            embed = self._make_embed("Entre em uma call", "Você precisa estar em uma call para usar esse comando", ok=False)
            await message.channel.send(embed=embed)
            return

        blocked = await self._should_block_for_voice_bot(message.guild, author_voice.channel)
        if blocked:
            embed = self._make_embed("Entrada bloqueada", "Não posso entrar porque o outro bot de voz já está nessa call", ok=False)
            await message.channel.send(embed=embed)
            return

        vc = await self._ensure_connected(message.guild, author_voice.channel)
        if vc is None or not vc.is_connected():
            embed = self._make_embed("Falha ao conectar", "Não consegui entrar na call agora", ok=False)
            await message.channel.send(embed=embed)
            return

        embed = self._make_embed("Entrei na call com sucesso", f"Entrei na call `{author_voice.channel.name}`", ok=True)
        await message.channel.send(embed=embed)

    async def _send_prefix_panel(self, message: discord.Message, *, panel_type: str):
        if not message.guild:
            return

        panel_kind = "user"
        if panel_type == "server":
            panel_kind = "server"
            if not message.author.guild_permissions.kick_members:
                embed = self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para abrir o painel do servidor",
                    ok=False,
                )
                await message.channel.send(embed=embed)
                return
            embed = await self._build_settings_embed(
                message.guild.id,
                message.author.id,
                server=True,
                panel_kind="server",
            )
            view = self._build_panel_view(0, message.guild.id, server=True, timeout=300)
        elif panel_type == "toggle":
            panel_kind = "toggle"
            if not message.author.guild_permissions.kick_members:
                embed = self._make_embed(
                    "Sem permissão",
                    "Você precisa da permissão `Expulsar Membros` para abrir o painel de toggles",
                    ok=False,
                )
                await message.channel.send(embed=embed)
                return
            embed = await self._build_toggle_embed(message.guild.id, message.author.id)
            view = self._build_toggle_view(0, message.guild.id, timeout=300)
        else:
            embed = await self._build_settings_embed(
                message.guild.id,
                message.author.id,
                server=False,
                panel_kind="user",
            )
            view = self._build_panel_view(0, message.guild.id, server=False, timeout=300)

        if await self._check_prefix_panel_cooldown(message, panel_kind):
            return

        await self._delete_prefix_panel(message.guild.id, message.author.id, panel_kind)

        sent = await message.channel.send(embed=embed, view=view)
        view.message = sent
        self._public_panel_states[sent.id] = {"panel_kind": panel_kind, "history": [], "owner_id": message.author.id}
        self._active_prefix_panels[self._prefix_panel_key(message.guild.id, message.author.id, panel_kind)] = sent

    async def _leave_from_panel(self, interaction: discord.Interaction):
        vc = self._get_voice_client_for_guild(interaction.guild)
        if vc is None or not vc.is_connected():
            await interaction.response.send_message(
                embed=self._make_embed("Nada para desconectar", "O bot não está conectado em nenhum canal de voz agora.", ok=False),
                ephemeral=True,
            )
            return

        user_voice = getattr(interaction.user, "voice", None)
        if user_voice is None or user_voice.channel is None:
            await interaction.response.send_message(
                embed=self._make_embed("Entre em uma call", "Você precisa estar em uma call para usar esse botão.", ok=False),
                ephemeral=True,
            )
            return

        if vc.channel and user_voice.channel.id != vc.channel.id and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                embed=self._make_embed("Canal diferente", "Você precisa estar na mesma call do bot, ou ter `Gerenciar Servidor`.", ok=False),
                ephemeral=True,
            )
            return

        await self._disconnect_and_clear(interaction.guild)
        await interaction.response.send_message(
            embed=self._make_embed("Bot desconectado", "Saí da call e limpei a fila de TTS.", ok=True),
            ephemeral=True,
        )



    @app_commands.command(name="status", description="Mostra o status atual do TTS ou copia a configuração de outro usuário")
    @app_commands.describe(acao="Escolha se quer ver o seu status, mostrar o de outro usuário ou copiar a configuração dele", usuario="Usuário alvo quando a ação envolver outro usuário")
    @app_commands.choices(acao=STATUS_ACTION_CHOICES)
    async def status(
        self,
        interaction: discord.Interaction,
        acao: app_commands.Choice[str] | None = None,
        usuario: discord.Member | None = None,
    ):
        if not await self._require_guild(interaction):
            return

        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return

        action_value = str(getattr(acao, "value", "self") or "self")
        if action_value == "self":
            await self._defer_ephemeral(interaction)
        elif not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        if action_value == "show_other":
            if usuario is None:
                await self._respond(interaction, embed=self._make_embed("Usuário obrigatório", "Escolha um usuário para mostrar o status público dele no chat.", ok=False), ephemeral=True)
                return
            embed = await self._build_status_embed(
                interaction.guild.id,
                usuario.id,
                viewer_user_id=interaction.user.id,
                target_user_name=self._member_panel_name(usuario),
                public=True,
            )
            embed.description = f"{self._member_panel_name(interaction.user)} mostrou no chat o status de TTS de {self._member_panel_name(usuario)}."
            await self._respond(interaction, embed=embed, ephemeral=False)
            return

        if action_value == "copy_other":
            if usuario is None:
                await self._respond(interaction, embed=self._make_embed("Usuário obrigatório", "Escolha um usuário para copiar as configurações de TTS dele.", ok=False), ephemeral=True)
                return
            if usuario.id == interaction.user.id:
                await self._respond(interaction, embed=self._make_embed("Escolha outro usuário", "Você já está usando as suas próprias configurações. Escolha outro usuário para copiar as configurações dele.", ok=False), ephemeral=True)
                return

            resolved = await self._maybe_await(db.resolve_tts(interaction.guild.id, usuario.id))
            resolved = resolved or {}
            history_entry = f"{self._panel_actor_name(interaction)} copiou as configurações de TTS de {self._member_panel_name(usuario)}"
            await self._set_user_tts_and_refresh(
                interaction.guild.id,
                interaction.user.id,
                engine=str(resolved.get('engine', 'gtts') or 'gtts'),
                voice=str(resolved.get('edge_voice', resolved.get('voice', 'pt-BR-FranciscaNeural')) or 'pt-BR-FranciscaNeural'),
                language=str(resolved.get('gtts_language', resolved.get('language', 'pt-br')) or 'pt-br'),
                rate=str(resolved.get('edge_rate', resolved.get('rate', '+0%')) or '+0%'),
                pitch=str(resolved.get('edge_pitch', resolved.get('pitch', '+0Hz')) or '+0Hz'),
                gcloud_voice=str(resolved.get('gcloud_voice', getattr(config, 'GOOGLE_CLOUD_TTS_VOICE_NAME', 'pt-BR-Standard-A')) or getattr(config, 'GOOGLE_CLOUD_TTS_VOICE_NAME', 'pt-BR-Standard-A')),
                gcloud_language=str(resolved.get('gcloud_language', getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR')) or getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR')),
                gcloud_rate=str(resolved.get('gcloud_rate', str(getattr(config, 'GOOGLE_CLOUD_TTS_SPEAKING_RATE', 1.0))) or str(getattr(config, 'GOOGLE_CLOUD_TTS_SPEAKING_RATE', 1.0))),
                gcloud_pitch=str(resolved.get('gcloud_pitch', str(getattr(config, 'GOOGLE_CLOUD_TTS_PITCH', 0.0))) or str(getattr(config, 'GOOGLE_CLOUD_TTS_PITCH', 0.0))),
                history_entry=history_entry,
            )

            embed = self._make_embed(
                "Configurações copiadas",
                f"{self._member_panel_name(interaction.user)} copiou as configurações de TTS de {self._member_panel_name(usuario)}.",
                ok=True,
            )
            embed.add_field(name="Engine", value=f"`{resolved.get('engine', 'gtts')}`", inline=True)
            embed.add_field(name="Voz do Edge", value=f"`{resolved.get('edge_voice', resolved.get('voice', 'pt-BR-FranciscaNeural'))}`", inline=True)
            embed.add_field(name="Idioma do gTTS", value=f"`{resolved.get('gtts_language', resolved.get('language', 'pt-br'))}`", inline=True)
            embed.add_field(name="Velocidade do Edge", value=f"`{resolved.get('edge_rate', resolved.get('rate', '+0%'))}`", inline=True)
            embed.add_field(name="Tom do Edge", value=f"`{resolved.get('edge_pitch', resolved.get('pitch', '+0Hz'))}`", inline=True)
            embed.add_field(name="Idioma do Google", value=f"`{resolved.get('gcloud_language', getattr(config, 'GOOGLE_CLOUD_TTS_LANGUAGE_CODE', 'pt-BR'))}`", inline=True)
            embed.add_field(name="Voz do Google", value=f"`{resolved.get('gcloud_voice', getattr(config, 'GOOGLE_CLOUD_TTS_VOICE_NAME', 'pt-BR-Standard-A'))}`", inline=True)
            embed.add_field(name="Velocidade do Google", value=f"`{resolved.get('gcloud_rate', str(getattr(config, 'GOOGLE_CLOUD_TTS_SPEAKING_RATE', 1.0)))}`", inline=True)
            embed.add_field(name="Tom do Google", value=f"`{resolved.get('gcloud_pitch', str(getattr(config, 'GOOGLE_CLOUD_TTS_PITCH', 0.0)))}`", inline=True)
            await self._respond(interaction, embed=embed, ephemeral=False)
            return

        embed = await self._build_status_embed(
            interaction.guild.id,
            interaction.user.id,
            viewer_user_id=interaction.user.id,
            target_user_name=self._member_panel_name(interaction.user),
            public=False,
        )
        view = self._build_status_view(
            interaction.user.id,
            interaction.guild.id,
            target_user_id=interaction.user.id,
            target_user_name=self._member_panel_name(interaction.user),
        )
        msg = await self._respond(interaction, embed=embed, view=view, ephemeral=True)
        if isinstance(view, TTSStatusView):
            view.attach_message(msg)
        else:
            view.message = msg


    @app_commands.command(name="usuario", description="Abre o painel, reseta ou altera o apelido falado de um usuário")
    @app_commands.describe(usuario="Usuário que terá as configurações alteradas", acao="Escolha se quer abrir o painel, alterar o apelido falado ou resetar")
    @app_commands.choices(acao=USER_CONFIG_ACTION_CHOICES)
    async def usuario(self, interaction: discord.Interaction, usuario: discord.Member, acao: app_commands.Choice[str]):
        if not await self._require_guild(interaction):
            return
        if not await self._require_kick_members(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return

        target_name = self._member_panel_name(usuario)
        action_value = str(getattr(acao, "value", "") or "")

        if action_value == "spoken_name":
            current_value = self._get_saved_spoken_name(interaction.guild.id, usuario.id)
            await interaction.response.send_modal(
                SpokenNameModal(
                    self,
                    None,
                    target_user_id=usuario.id,
                    target_user_name=target_name,
                    current_value=current_value,
                )
            )
            return

        await self._defer_ephemeral(interaction)

        if action_value == "reset":
            if not hasattr(db, "reset_user_tts"):
                await self._respond(interaction, embed=self._make_embed("Função indisponível", "Esse banco ainda não suporta resetar as configurações do usuário.", ok=False), ephemeral=True)
                return
            history_entry = f"{self._panel_actor_name(interaction)} resetou as configurações de TTS de {target_name} para os padrões do servidor"
            await self._reset_user_tts_and_refresh(interaction.guild.id, usuario.id, history_entry=history_entry)
            embed = await self._build_settings_embed(
                interaction.guild.id,
                usuario.id,
                server=False,
                panel_kind="user",
                target_user_name=target_name,
                viewer_user_id=interaction.user.id,
            )
            await self._respond(interaction, embed=embed, ephemeral=True)
            await interaction.followup.send(
                embed=self._make_embed("Configurações resetadas", f"As configurações de TTS de {target_name} agora seguem os padrões do servidor.", ok=True),
                ephemeral=True,
            )
            return

        embed = await self._build_settings_embed(
            interaction.guild.id,
            usuario.id,
            server=False,
            panel_kind="user",
            target_user_name=target_name,
            viewer_user_id=interaction.user.id,
        )
        view = self._build_panel_view(
            interaction.user.id,
            interaction.guild.id,
            server=False,
            target_user_id=usuario.id,
            target_user_name=target_name,
        )
        msg = await self._respond(interaction, embed=embed, view=view, ephemeral=True)
        if isinstance(view, TTSStatusView):
            view.attach_message(msg)
        else:
            view.message = msg


    @server.command(name="menu", description="Abre um painel guiado para configurar o TTS do servidor")
    async def server_menu(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if not await self._require_kick_members(interaction):
            return

        embed = await self._build_settings_embed(
            interaction.guild.id,
            interaction.user.id,
            server=True,
            panel_kind="server",
        )
        view = self._build_panel_view(interaction.user.id, interaction.guild.id, server=True)
        msg = await self._respond(
            interaction,
            embed=embed,
            view=view,
            ephemeral=True,
        )
        view.message = msg

    @toggle.command(name="menu", description="Abre um painel guiado para os toggles de TTS")
    async def toggle_menu(self, interaction: discord.Interaction):
        await self._defer_ephemeral(interaction)
        if not await self._require_guild(interaction):
            return
        if not await self._require_kick_members(interaction):
            return
        embed = await self._build_toggle_embed(interaction.guild.id, interaction.user.id)
        view = self._build_toggle_view(interaction.user.id, interaction.guild.id)
        msg = await self._respond(interaction, embed=embed, view=view, ephemeral=True)
        if isinstance(view, TTSStatusView):
            view.attach_message(msg)
        else:
            view.message = msg


    @toggle.command(name="auto_leave", description="Ativa ou desativa o auto leave quando o bot ficar sozinho ou só com bots")
    @app_commands.describe(enabled="true para ativar, false para desativar")
    async def toggle_auto_leave(self, interaction: discord.Interaction, enabled: bool):
        if not await self._require_guild(interaction):
            return
        if not await self._require_toggle_allowed_guild(interaction):
            return
        if not await self._require_staff_or_kick_members(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return

        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, auto_leave=bool(enabled)))
        history_entry = self._toggle_history_text(interaction, "ativou o Auto leave" if enabled else "desativou o Auto leave")
        if hasattr(db, "set_guild_panel_last_change"):
            await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, toggle_last_change=history_entry))
        title = "Auto leave atualizado"
        desc = "Auto leave ativado: o bot vai sair automaticamente quando ficar sozinho ou só com bots na call." if enabled else "Auto leave desativado: o bot não vai mais sair automaticamente quando ficar sozinho ou só com bots na call."
        await self._respond(interaction, embed=self._make_embed(title, desc, ok=True), ephemeral=True)


    @app_commands.describe(enabled="true para ativar, false para desativar")
    async def toggle_only_target_user(self, interaction: discord.Interaction, enabled: bool):
        if not await self._require_guild(interaction):
            return
        if not await self._require_kick_members(interaction):
            return
        db = self._get_db()
        if db is None:
            await self._respond(interaction, embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False), ephemeral=True)
            return
        await self._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, only_target_user=bool(enabled)))
        target_user_id = getattr(config, "ONLY_TTS_USER_ID", 0)
        if enabled:
            desc = "Só a Cuca pode falar nesse caralho.\n\n" + f"Todo mundo que não for o ID `{target_user_id}` será forçado para `gtts`."
        else:
            desc = "Agora os betinhas podem usar também.\n\nTodo mundo voltou a usar as próprias configurações."
        await self._respond(interaction, embed=self._make_embed("Modo Cuca atualizado", desc, ok=True), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TTSVoice(bot))
