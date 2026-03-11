import inspect
import asyncio
import time
import re
import unicodedata
from urllib.parse import urlparse
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from tts_audio import GuildTTSState, QueueItem, TTSAudioMixin

from typing import Callable


def _shorten(text: str, limit: int = 100) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _replace_custom_emojis_for_tts(text: str) -> str:
    return re.sub(r"<a?:([A-Za-z0-9_~]+):\d+>", lambda m: f"emoji {m.group(1)}", text)


def _spoken_site_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except Exception:
        return "link"
    host = (parsed.netloc or "").lower().strip()
    if not host:
        return "link"
    if host.startswith("www."):
        host = host[4:]
    return host or "link"


def _spoken_link_from_url(url: str) -> str:
    host = _spoken_site_from_url(url)
    return f"link de {host}, https/{host}"


def _attachment_label(att: discord.Attachment) -> str:
    content_type = (getattr(att, "content_type", None) or "").lower()
    filename = (getattr(att, "filename", "") or "").lower()

    if "gif" in content_type or filename.endswith(".gif"):
        return "gif"
    if content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp")):
        return "anexo de imagem"
    if content_type.startswith("video/") or filename.endswith((".mp4", ".mov", ".webm", ".mkv", ".avi")):
        return "anexo de vídeo"
    return "anexo"


_ABBREVIATION_MAP = {
    "tmb": "também",
    "tbm": "também",
    "vc": "você",
    "vcs": "vocês",
    "pq": "porque",
    "pk": "porque",
    "q": "que",
    "blz": "beleza",
    "obg": "obrigado",
    "obgd": "obrigado",
    "msg": "mensagem",
    "fds": "foda-se",
    "mds": "meu deus",
    "pdc": "pode crer",
    "td": "tudo",
    "tds": "todos",
    "tb": "também",
    "nao": "não",
    "n": "não",
    "s": "sim",
    "pqp": "puta que pariu",
    "fdp": "filho da puta",
    "vsf": "vai se foder",
    "tmnc": "tomar no cu",
    "tnc": "tomar no cu",
    "krl": "caralho",
    "crl": "caralho",
    "prr": "porra", 
    "poha" :"porra",
}

_WORD_TOKEN_RE = re.compile(r"\b[\wÀ-ÿ]+\b", re.UNICODE)


def _clean_name_for_tts(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = unicodedata.normalize("NFKC", raw)
    cleaned_chars: list[str] = []
    kept = 0
    for ch in raw:
        cat = unicodedata.category(ch)
        if ch.isalnum() or ch in " _-.":
            cleaned_chars.append(ch)
            kept += 1
        elif cat.startswith("Z"):
            cleaned_chars.append(" ")
        else:
            cleaned_chars.append(" ")
    cleaned = re.sub(r"\s+", " ", "".join(cleaned_chars)).strip(" ._-")
    if not cleaned:
        return ""
    if kept < max(2, len(raw) // 3):
        return ""
    letters_digits = sum(1 for ch in cleaned if ch.isalnum())
    if letters_digits < max(2, len(cleaned.replace(" ", "")) // 2):
        return ""
    return cleaned


def _display_name_for_tts(member: discord.abc.User | discord.Member | None) -> str:
    if member is None:
        return "usuário mencionado"
    preferred = _clean_name_for_tts(getattr(member, "display_name", None) or "")
    if preferred:
        return preferred
    fallback = _clean_name_for_tts(getattr(member, "name", None) or "")
    if fallback:
        return fallback
    return "usuário mencionado"


def _expand_abbreviations_for_tts(text: str) -> str:
    def repl(match: re.Match) -> str:
        token = match.group(0)
        replacement = _ABBREVIATION_MAP.get(token.lower())
        return replacement if replacement else token

    return _WORD_TOKEN_RE.sub(repl, text)


_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")




_DISCORD_CHANNEL_LINK_RE = re.compile(
    r"https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(?:@me|\d+)/(\d+)(?:/\d+)?",
    re.IGNORECASE,
)


def _spoken_link_for_tts(url: str, message: discord.Message) -> str:
    match = _DISCORD_CHANNEL_LINK_RE.match(url)
    guild = getattr(message, "guild", None)
    if match and guild:
        try:
            channel_id = int(match.group(1))
        except Exception:
            channel_id = None
        if channel_id:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                return f"canal {channel.name}"
    return _spoken_link_from_url(url)
def _replace_discord_mentions_for_tts(text: str, message: discord.Message) -> str:
    guild = message.guild

    def user_sub(match: re.Match) -> str:
        member_id = int(match.group(1))
        member = guild.get_member(member_id) if guild else None
        if member:
            return f"@{_display_name_for_tts(member)}"
        user = message.client.get_user(member_id)
        if user:
            return f"@{_display_name_for_tts(user)}"
        return "@usuário mencionado"

    def role_sub(match: re.Match) -> str:
        role_id = int(match.group(1))
        role = guild.get_role(role_id) if guild else None
        return f"cargo {role.name}" if role else "cargo mencionado"

    def channel_sub(match: re.Match) -> str:
        channel_id = int(match.group(1))
        channel = guild.get_channel(channel_id) if guild else None
        return f"canal {channel.name}" if channel else "canal mencionado"

    text = _USER_MENTION_RE.sub(user_sub, text)
    text = _ROLE_MENTION_RE.sub(role_sub, text)
    text = _CHANNEL_MENTION_RE.sub(channel_sub, text)
    return text


def _append_links_and_attachments_for_tts(text: str, message: discord.Message) -> str:
    extras: list[str] = []
    seen_urls: set[str] = set()

    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        if url and url not in seen_urls:
            seen_urls.add(url)
            extras.append(_spoken_link_for_tts(url, message))

    for att in getattr(message, "attachments", []) or []:
        extras.append(_attachment_label(att))

    if not extras:
        return text.strip()

    base = text.strip()
    suffix = ". " + ". ".join(extras)
    return (base + suffix) if base else ". ".join(extras)


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
        msg = await self.cog._respond(
            interaction,
            embed=self.cog._make_embed(self.title, self.description, ok=True),
            view=self,
            ephemeral=True,
        )
        self.message = msg


class ModeSelect(discord.ui.Select):
    def __init__(self, cog: "TTSVoice", *, server: bool):
        self.cog = cog
        self.server = server
        options = [
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


class SpeechLimitModal(discord.ui.Modal, title="Alterar limite de fala"):
    new_limit = discord.ui.TextInput(
        label="Novo limite em segundos",
        placeholder="Ex.: 30 ou 30s",
        required=True,
        min_length=1,
        max_length=6,
    )

    def __init__(self, cog: "TTSVoice", panel_message: discord.Message, owner_id: int, guild_id: int):
        super().__init__()
        self.cog = cog
        self.panel_message = panel_message
        self.owner_id = owner_id
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._apply_server_speech_limit_from_modal(
            interaction,
            limit_value=str(self.new_limit),
            panel_message=self.panel_message,
        )


class TTSMainPanelView(_BaseTTSView):
    def __init__(self, cog: "TTSVoice", owner_id: int, guild_id: int, *, server: bool = False, timeout: float = 180, target_user_id: int | None = None, target_user_name: str | None = None):
        super().__init__(cog, owner_id, guild_id, timeout=timeout, target_user_id=target_user_id, target_user_name=target_user_name)
        self.server = server
        self.panel_kind = "server" if server else "user"
        self.remove_item(self.mode_button)
        if not self.server:
            self.remove_item(self.bot_prefix_button)
            self.remove_item(self.gtts_prefix_button)
            self.remove_item(self.edge_prefix_button)
            self.remove_item(self.speech_limit_button)

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


    @discord.ui.button(label="Prefixo do bot", style=discord.ButtonStyle.secondary, emoji="🤖", row=2)
    async def bot_prefix_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BotPrefixModal(self.cog, interaction.message, self._target_owner(interaction), self.guild_id))

    @discord.ui.button(label="Prefixo do gTTS", style=discord.ButtonStyle.secondary, emoji="🔤", row=2)
    async def gtts_prefix_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(GTTSPrefixModal(self.cog, interaction.message, self._target_owner(interaction), self.guild_id))

    @discord.ui.button(label="Prefixo do Edge", style=discord.ButtonStyle.secondary, emoji="🔊", row=2)
    async def edge_prefix_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EdgePrefixModal(self.cog, interaction.message, self._target_owner(interaction), self.guild_id))

    @discord.ui.button(label="Entrar na call", style=discord.ButtonStyle.secondary, emoji="📥", row=2)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._join_from_panel(interaction)

    @discord.ui.button(label="Sair da call", style=discord.ButtonStyle.secondary, emoji="📤", row=2)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._leave_from_panel(interaction)


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
    return "edge" if str(mode or "").strip().lower() == "edge" else "gtts"


MODE_CHOICES = [
    app_commands.Choice(name="gtts — mais simples e compatível", value="gtts"),
    app_commands.Choice(name="edge — voz natural com voice, speed e pitch", value="edge"),
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
    app_commands.Choice(name="Resetar configurações do usuário para as do servidor", value="reset"),
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
        self._prefix_command_alias_cache: dict[str, dict[str, object]] = {}
        self._edge_voice_load_task: asyncio.Task | None = None
        self._voice_preconnect_tasks: dict[int, asyncio.Task] = {}

    async def cog_load(self):
        self._ensure_edge_voice_load_started()

    def _get_db(self):
        return getattr(self.bot, "settings_db", None)

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
        existing = self._edge_voice_load_task
        if existing is not None and not existing.done():
            await existing
            return

        async def _runner():
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

        task = asyncio.create_task(_runner())
        self._edge_voice_load_task = task
        try:
            await task
        finally:
            if self._edge_voice_load_task is task:
                self._edge_voice_load_task = None

    def _ensure_edge_voice_load_started(self) -> None:
        task = self._edge_voice_load_task
        if task is None or task.done():
            self._edge_voice_load_task = asyncio.create_task(self._load_edge_voices())

    def _start_voice_preconnect(self, guild: discord.Guild, voice_channel) -> None:
        if guild is None or voice_channel is None:
            return
        current = self._voice_preconnect_tasks.get(guild.id)
        if current is not None and not current.done():
            return

        async def _runner():
            try:
                await self._ensure_connected(guild, voice_channel)
            except Exception:
                pass
            finally:
                task = self._voice_preconnect_tasks.get(guild.id)
                if task is asyncio.current_task():
                    self._voice_preconnect_tasks.pop(guild.id, None)

        self._voice_preconnect_tasks[guild.id] = asyncio.create_task(_runner())

    def _make_embed(self, title: str, description: str, *, ok: bool = True) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=discord.Color.green() if ok else discord.Color.red())

    def _get_prefix_command_aliases(self, bot_prefix: str) -> dict[str, object]:
        cached = self._prefix_command_alias_cache.get(bot_prefix)
        if cached is not None:
            return cached

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
        aliases = {
            "clear": f"{bot_prefix}clear",
            "leave": f"{bot_prefix}leave",
            "join": f"{bot_prefix}join",
            "reset": f"{bot_prefix}reset",
            "set_lang": f"{bot_prefix}set lang",
            "set_limit": f"{bot_prefix}set limit",
            "panel_aliases": panel_aliases,
            "server_aliases": server_aliases,
            "toggle_aliases": toggle_aliases,
        }
        self._prefix_command_alias_cache[bot_prefix] = aliases
        return aliases

    async def _prepare_message_text_for_tts(self, message: discord.Message, active_prefix: str | None) -> str:
        text = str(getattr(message, "content", "") or "")
        if active_prefix and text.startswith(active_prefix):
            text = text[len(active_prefix):]
        text = text.strip()

        if text:
            text = _replace_custom_emojis_for_tts(text)
            text = _replace_discord_mentions_for_tts(text, message)
            text = _expand_abbreviations_for_tts(text)

        text = _append_links_and_attachments_for_tts(text, message)
        return text.strip()

    def _parse_speech_limit_seconds(self, raw: str) -> int | None:
        value = str(raw or "").strip().lower().replace(" ", "")
        if value.endswith("s"):
            value = value[:-1]
        if not value or not value.isdigit():
            return None
        seconds = int(value)
        if seconds < 1 or seconds > 600:
            return None
        return seconds

    def _estimate_tts_seconds(self, text: str, *, rate: str = "+0%") -> float:
        clean = str(text or "").strip()
        if not clean:
            return 0.0
        base_chars_per_second = 14.0
        rate_value = 0
        m = re.match(r"^([+-]?)(\d+)%$", str(rate or "").strip())
        if m:
            sign = -1 if m.group(1) == "-" else 1
            rate_value = sign * int(m.group(2))
        speed_factor = max(0.35, 1.0 + (rate_value / 100.0))
        return len(clean) / (base_chars_per_second * speed_factor)

    def _truncate_tts_text_to_seconds(self, text: str, limit_seconds: int, *, rate: str = "+0%") -> str:
        clean = str(text or "").strip()
        if not clean:
            return ""
        if limit_seconds <= 0:
            return clean
        if self._estimate_tts_seconds(clean, rate=rate) <= limit_seconds:
            return clean

        words = clean.split()
        if not words:
            return clean

        kept: list[str] = []
        for word in words:
            candidate = (" ".join(kept + [word])).strip()
            if kept and self._estimate_tts_seconds(candidate, rate=rate) > limit_seconds:
                break
            kept.append(word)
        trimmed = " ".join(kept).strip()
        if not trimmed:
            approx_chars = max(1, int(limit_seconds * 12))
            trimmed = clean[:approx_chars].strip()
        return trimmed or clean

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
            return await interaction.followup.send(
                content=content,
                embed=embed,
                view=view,
                ephemeral=ephemeral,
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
        vc = guild.voice_client
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
        vc = guild.voice_client
        if vc is None or not vc.is_connected() or vc.channel is None:
            return
        if self._voice_channel_has_only_bots_or_is_empty(vc.channel):
            print(f"[tts_voice] saindo da call | sozinho ou só com bots | guild={guild.id} channel={vc.channel.id}")
            await self._disconnect_and_clear(guild)

    async def _ensure_connected(self, guild: discord.Guild, voice_channel) -> Optional[discord.VoiceClient]:
        if voice_channel is None:
            print(f"[tts_voice] _ensure_connected recebeu canal None | guild={guild.id}")
            return None

        lock = self._get_voice_connect_lock(guild.id)
        async with lock:
            def _current_connected_vc() -> Optional[discord.VoiceClient]:
                current = guild.voice_client
                if current is not None and current.is_connected():
                    return current
                return None

            vc = _current_connected_vc()
            if vc and vc.channel and vc.channel.id == voice_channel.id:
                return vc

            async def _fresh_connect() -> Optional[discord.VoiceClient]:
                new_vc = await voice_channel.connect(self_deaf=True)
                print(f"[tts_voice] Conectado no canal {voice_channel.id} na guild {guild.id}")
                return new_vc

            try:
                if vc is not None:
                    try:
                        await vc.move_to(voice_channel)
                        print(f"[tts_voice] Movido para canal {voice_channel.id} na guild {guild.id}")
                        return _current_connected_vc() or vc
                    except Exception as move_err:
                        msg = str(move_err).lower()
                        current_vc = _current_connected_vc()
                        if current_vc and current_vc.channel and current_vc.channel.id == voice_channel.id:
                            return current_vc
                        if "already connected" in msg and current_vc is not None:
                            try:
                                await current_vc.move_to(voice_channel)
                            except Exception:
                                pass
                            current_vc = _current_connected_vc()
                            if current_vc and current_vc.channel and current_vc.channel.id == voice_channel.id:
                                print(f"[tts_voice] Reaproveitando conexão já existente na guild {guild.id}")
                                return current_vc
                        if "closing transport" in msg or "not connected to voice" in msg:
                            try:
                                await vc.disconnect(force=True)
                            except Exception:
                                pass
                            current_vc = _current_connected_vc()
                            if current_vc and current_vc.channel and current_vc.channel.id == voice_channel.id:
                                return current_vc
                            return await _fresh_connect()
                        raise

                return await _fresh_connect()

            except Exception as e:
                msg = str(e).lower()
                current_vc = _current_connected_vc()

                if current_vc and current_vc.channel and current_vc.channel.id == voice_channel.id:
                    print(f"[tts_voice] Reaproveitando voice_client já conectado na guild {guild.id}")
                    return current_vc

                if "already connected" in msg and current_vc and current_vc.is_connected():
                    try:
                        await current_vc.move_to(voice_channel)
                        print(f"[tts_voice] Movido para canal {voice_channel.id} na guild {guild.id}")
                        return _current_connected_vc() or current_vc
                    except Exception:
                        current_vc = _current_connected_vc()
                        if current_vc and current_vc.channel and current_vc.channel.id == voice_channel.id:
                            print(f"[tts_voice] Reaproveitando conexão após already connected | guild={guild.id}")
                            return current_vc

                if "closing transport" in msg or "not connected to voice" in msg:
                    try:
                        if current_vc:
                            await current_vc.disconnect(force=True)
                    except Exception:
                        pass
                    current_vc = _current_connected_vc()
                    if current_vc and current_vc.channel and current_vc.channel.id == voice_channel.id:
                        return current_vc
                    try:
                        return await _fresh_connect()
                    except Exception as retry_err:
                        print(f"[tts_voice] Erro ao reconectar na guild {guild.id}: {retry_err}")
                        current_vc = _current_connected_vc()
                        if current_vc and current_vc.channel and current_vc.channel.id == voice_channel.id:
                            return current_vc
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
        guild_defaults = guild_defaults or {}
        content = str(message.content)
        stripped_content = content.strip()
        lowered = stripped_content.lower()

        gtts_prefix = str(guild_defaults.get("gtts_prefix", guild_defaults.get("tts_prefix", ".")) or ".")
        edge_prefix = str(guild_defaults.get("edge_prefix", ",") or ",")
        bot_prefix = str(guild_defaults.get("bot_prefix", "_") or "_")

        aliases = self._get_prefix_command_aliases(bot_prefix)
        clear_command = str(aliases["clear"])
        leave_command = str(aliases["leave"])
        join_command = str(aliases["join"])
        reset_command = str(aliases["reset"])
        set_lang_command = str(aliases["set_lang"])
        set_limit_command = str(aliases["set_limit"])
        panel_aliases = set(aliases["panel_aliases"])
        server_aliases = set(aliases["server_aliases"])
        toggle_aliases = set(aliases["toggle_aliases"])

        is_prefix_command = (
            lowered == clear_command
            or lowered == leave_command
            or lowered == join_command
            or lowered == reset_command
            or lowered.startswith(reset_command + " ")
            or lowered == set_lang_command
            or lowered.startswith(set_lang_command + " ")
            or lowered == set_limit_command
            or lowered.startswith(set_limit_command + " ")
            or lowered in panel_aliases
            or lowered in server_aliases
            or lowered in toggle_aliases
        )

        if is_prefix_command:
            if self._was_tts_message_seen(message.id):
                return
            self._mark_tts_message_seen(message.id)

        if lowered == clear_command:
            await self._prefix_clear(message)
            return
        if lowered == leave_command:
            await self._prefix_leave(message)
            return
        if lowered == join_command:
            await self._prefix_join(message)
            return
        if lowered == reset_command or lowered.startswith(reset_command + " "):
            raw_target = content[len(reset_command):].strip()
            await self._prefix_reset_user(message, raw_target)
            return
        if lowered == set_lang_command or lowered.startswith(set_lang_command + " "):
            raw_language = content[len(set_lang_command):].strip()
            await self._prefix_set_lang(message, raw_language)
            return
        if lowered == set_limit_command or lowered.startswith(set_limit_command + " "):
            raw_limit = content[len(set_limit_command):].strip()
            await self._prefix_set_limit(message, raw_limit)
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

        self._start_voice_preconnect(message.guild, voice_channel)

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
            resolved["language"] = resolved.get("language") or getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt")
            resolved["voice"] = ""
            resolved["rate"] = "+0%"
            resolved["pitch"] = "+0Hz"
            forced_gtts = True

        if forced_engine == "gtts":
            resolved["engine"] = "gtts"
            resolved["language"] = resolved.get("language") or getattr(config, "GTTS_DEFAULT_LANGUAGE", "pt")
        elif forced_engine == "edge":
            self._ensure_edge_voice_load_started()
            resolved["engine"] = "edge"
            resolved["voice"] = resolved.get("voice") or "pt-BR-FranciscaNeural"
            resolved["rate"] = resolved.get("rate") or "+0%"
            resolved["pitch"] = resolved.get("pitch") or "+0Hz"

        text = await self._prepare_message_text_for_tts(message, active_prefix)
        if not text:
            print("[tts_voice] ignorado | texto vazio após prefixo")
            return

        state = self._get_state(message.guild.id)
        state.last_text_channel_id = getattr(message.channel, "id", None)
        item = QueueItem(guild_id=message.guild.id, channel_id=voice_channel.id, author_id=message.author.id, text=text, engine=resolved["engine"], voice=resolved["voice"], language=resolved["language"], rate=resolved["rate"], pitch=resolved["pitch"])
        self._ensure_worker(message.guild.id)
        _, dropped = await self._enqueue_tts_item(message.guild.id, item)
        print(f"[tts_voice] trigger TTS | guild={message.guild.id} channel_type={type(message.channel).__name__} user={message.author.id} raw={message.content!r}")
        print(f"[tts_voice] enfileirada | guild={message.guild.id} user={message.author.id} canal_voz={voice_channel.id} engine={resolved['engine']} forced_gtts={forced_gtts} dropped={dropped}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        vc = guild.voice_client
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
            title, desc = "Modo padrão atualizado", f"O modo padrão do servidor agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos gTTS e Edge continuam escolhendo o motor por mensagem."
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, effective_user_id, engine=value))
            title, desc = "Modo atualizado", f"O seu modo de TTS agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos gTTS e Edge continuam escolhendo o motor por mensagem."
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
            await self._maybe_await(db.set_user_tts(interaction.guild.id, effective_user_id, voice=voice))
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
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, language=value))
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
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, rate=value))
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
            await self._maybe_await(db.set_user_tts(interaction.guild.id, interaction.user.id, pitch=value))
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


    async def _prefix_set_limit(self, message: discord.Message, raw_limit: str):
        if message.guild is None:
            return
        if not getattr(message.author.guild_permissions, "kick_members", False):
            await message.channel.send(embed=self._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para alterar o limite de fala do servidor.", ok=False))
            return

        seconds = self._parse_speech_limit_seconds(raw_limit)
        if seconds is None:
            await message.channel.send(embed=self._make_embed("Limite inválido", "Use esse comando assim: `_set limit 30` ou `_set limit 30s`.", ok=False))
            return

        db = self._get_db()
        if db is None or not hasattr(db, "set_guild_tts_defaults"):
            await message.channel.send(embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora para alterar o limite de fala.", ok=False))
            return

        await self._maybe_await(db.set_guild_tts_defaults(message.guild.id, speech_limit_seconds=seconds))
        if hasattr(db, "set_guild_panel_last_change"):
            history_entry = f"{self._member_panel_name(message.author)} alterou o limite de fala do bot para {seconds}s"
            await self._maybe_await(db.set_guild_panel_last_change(message.guild.id, server_last_change=history_entry))

        await message.channel.send(embed=self._make_embed("Limite atualizado", f"O limite de fala do servidor agora é `{seconds}s`.", ok=True))

    async def _prefix_set_lang(self, message: discord.Message, raw_language: str):
        if message.guild is None:
            return

        value = str(raw_language or "").strip()
        if not value:
            await message.channel.send(embed=self._make_embed("Idioma obrigatório", "Use esse comando assim: `_set lang português`, `_set lang pt` ou `_set lang pt @usuário`.", ok=False))
            return

        target_member: discord.Member | None = None
        language_value = value
        code, language_name = self._resolve_gtts_language_input(language_value)

        if code is None:
            parts = value.split()
            for i in range(len(parts) - 1, 0, -1):
                possible_language = " ".join(parts[:i]).strip()
                possible_target = " ".join(parts[i:]).strip()
                resolved_code, resolved_name = self._resolve_gtts_language_input(possible_language)
                if resolved_code is None or not possible_target:
                    continue
                member = await self._resolve_member_from_text(message.guild, possible_target)
                if member is None:
                    continue
                language_value = possible_language
                code, language_name = resolved_code, resolved_name
                target_member = member
                break

        if code is None:
            invalid_embed = self._make_embed(
                "Idioma inválido",
                "Não reconheci esse idioma do gTTS. Use um código como `pt-br`, `pt`, `en`, `es` ou um nome em português como `português` e `espanhol`.",
                ok=False,
            )
            try:
                invalid_view = LanguageHelpView(self, message.author.id, message.guild.id, server=False)
            except Exception:
                invalid_view = None

            sent = None
            try:
                sent = await message.reply(
                    embed=invalid_embed,
                    view=invalid_view,
                    mention_author=False,
                )
            except Exception:
                try:
                    sent = await message.channel.send(
                        embed=invalid_embed,
                        view=invalid_view,
                    )
                except Exception:
                    try:
                        await message.reply(embed=invalid_embed, mention_author=False)
                    except Exception:
                        try:
                            await message.channel.send(embed=invalid_embed)
                        except Exception:
                            pass
            return

        if target_member is None:
            target_member = message.author if isinstance(message.author, discord.Member) else None

        if target_member is None:
            await message.channel.send(embed=self._make_embed("Usuário inválido", "Não consegui identificar o usuário que terá o idioma alterado.", ok=False))
            return

        if target_member.id != message.author.id and not getattr(message.author.guild_permissions, "kick_members", False):
            await message.channel.send(embed=self._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para alterar o idioma do gTTS de outro usuário.", ok=False))
            return

        db = self._get_db()
        if db is None or not hasattr(db, "set_user_tts"):
            await message.channel.send(embed=self._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora para alterar o idioma do gTTS.", ok=False))
            return

        await self._maybe_await(db.set_user_tts(message.guild.id, target_member.id, language=code))
        if hasattr(db, "set_user_panel_last_change"):
            if target_member.id == message.author.id:
                history_entry = f"Você alterou o próprio idioma para {code}"
            else:
                history_entry = f"{self._member_panel_name(message.author)} alterou o idioma de {self._member_panel_name(target_member)} para {code}"
            await self._maybe_await(db.set_user_panel_last_change(message.guild.id, target_member.id, history_entry))

        pretty_name = language_name or code
        if target_member.id == message.author.id:
            description = f"Seu idioma pessoal do gTTS agora é `{code}` ({pretty_name})."
        else:
            description = f"O idioma pessoal do gTTS de {self._member_panel_name(target_member)} agora é `{code}` ({pretty_name})."
        await message.channel.send(embed=self._make_embed("Idioma atualizado", description, ok=True))

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

        await self._maybe_await(db.reset_user_tts(message.guild.id, member.id))
        if hasattr(db, "set_user_panel_last_change"):
            history_entry = f"{self._member_panel_name(message.author)} resetou as configurações de TTS de {self._member_panel_name(member)} para os padrões do servidor"
            await self._maybe_await(db.set_user_panel_last_change(message.guild.id, member.id, history_entry))

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
        history_text = self._format_history_entries(last_changes or [], viewer_user_id=viewer_user_id or user_id, message_id=message_id)
        if history_text:
            embed.add_field(name="Últimas alterações", value=history_text, inline=False)
        return embed

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
            description = "Use os botões abaixo para ajustar os padrões do servidor. Mensagens com o prefixo do gTTS usam idioma do gTTS. Mensagens com o prefixo do Edge usam voz, velocidade e tom do Edge."
        elif target_user_name and int(user_id or 0) != int(viewer_user_id or user_id or 0):
            title = f"Painel de TTS de {target_user_name}"
            description = f"Use os botões abaixo para alterar as configurações de {target_user_name}. O idioma vale para o prefixo do gTTS, e voz, velocidade e tom valem para o prefixo do Edge."
        else:
            title = "Painel de TTS"
            description = "Use os botões abaixo para alterar as suas configurações. O idioma vale para o prefixo do gTTS, e voz, velocidade e tom valem para o prefixo do Edge."

        embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
        embed.add_field(name="Voz do Edge", value=f"`{resolved.get('voice', 'Não definido')}`", inline=True)
        embed.add_field(name="Idioma do gTTS", value=f"`{resolved.get('language', 'Não definido')}`", inline=True)
        embed.add_field(name="Velocidade do Edge", value=f"`{resolved.get('rate', '+0%')}`", inline=True)
        embed.add_field(name="Tom do Edge", value=f"`{resolved.get('pitch', '+0Hz')}`", inline=True)
        if server:
            embed.add_field(name="Prefixo do bot", value=f"`{guild_defaults.get('bot_prefix', '_')}`", inline=True)
            embed.add_field(name="Prefixo do modo gTTS", value=f"`{guild_defaults.get('gtts_prefix', guild_defaults.get('tts_prefix', '.'))}`", inline=True)
            embed.add_field(name="Prefixo do modo Edge", value=f"`{guild_defaults.get('edge_prefix', ',')}`", inline=True)
            embed.add_field(name="Limite de fala", value=f"`{int(guild_defaults.get('speech_limit_seconds', 30) or 30)}s`", inline=True)
        history_text = self._format_history_entries(last_changes or [], viewer_user_id=viewer_user_id or user_id, message_id=message_id)
        if history_text:
            embed.add_field(name="Últimas alterações", value=history_text, inline=False)

        embed.set_footer(text="Os ajustes do gTTS e do Edge ficam salvos no banco." if server or panel_kind == "toggle" else "As alterações desse painel ficam salvas para o usuário correspondente.")
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
            desc = f"O modo padrão do servidor agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos gTTS e Edge continuam escolhendo o motor por mensagem."
            history_entry = self._server_history_text(interaction, "o modo padrão do servidor", value)
            await self._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
            self._append_public_panel_history(message_id, history_entry)
            last_changes = list((await self._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
        else:
            await self._maybe_await(db.set_user_tts(interaction.guild.id, effective_user_id, engine=value))
            desc = f"O modo de TTS de {effective_user_name} agora é `{value}`." if effective_user_id != interaction.user.id else f"O seu modo de TTS agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos gTTS e Edge continuam escolhendo o motor por mensagem."
            history_entry = self._user_history_text(interaction, "o próprio modo" if effective_user_id == interaction.user.id else "o modo", value, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._maybe_await(db.set_user_panel_last_change(interaction.guild.id, effective_user_id, history_entry))
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
            await self._maybe_await(db.set_user_tts(interaction.guild.id, effective_user_id, voice=voice))
            desc = f"A voz do Edge de {effective_user_name} agora é `{voice}`." if effective_user_id != interaction.user.id else f"A sua voz do Edge agora é `{voice}`."
            history_entry = self._user_history_text(interaction, "a própria voz" if effective_user_id == interaction.user.id else "a voz", voice, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._maybe_await(db.set_user_panel_last_change(interaction.guild.id, effective_user_id, history_entry))
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
            await self._maybe_await(db.set_user_tts(interaction.guild.id, effective_user_id, language=language))
            desc = f"O idioma do gtts de {effective_user_name} agora é `{language}`." if effective_user_id != interaction.user.id else f"O seu idioma do gtts agora é `{language}`."
            history_entry = self._user_history_text(interaction, "o próprio idioma" if effective_user_id == interaction.user.id else "o idioma", language, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._maybe_await(db.set_user_panel_last_change(interaction.guild.id, effective_user_id, history_entry))
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
            await self._maybe_await(db.set_user_tts(interaction.guild.id, effective_user_id, rate=speed))
            desc = f"A velocidade do Edge de {effective_user_name} agora é `{speed}`." if effective_user_id != interaction.user.id else f"A sua velocidade do Edge agora é `{speed}`."
            history_entry = self._user_history_text(interaction, "a própria velocidade" if effective_user_id == interaction.user.id else "a velocidade", speed, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._maybe_await(db.set_user_panel_last_change(interaction.guild.id, effective_user_id, history_entry))
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
            await self._maybe_await(db.set_user_tts(interaction.guild.id, effective_user_id, pitch=pitch))
            desc = f"O tom do Edge de {effective_user_name} agora é `{pitch}`." if effective_user_id != interaction.user.id else f"O seu tom do Edge agora é `{pitch}`."
            history_entry = self._user_history_text(interaction, "o próprio tom" if effective_user_id == interaction.user.id else "o tom", pitch, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
            await self._maybe_await(db.set_user_panel_last_change(interaction.guild.id, effective_user_id, history_entry))
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



    @app_commands.command(name="usuario", description="Reseta ou abre o painel pessoal de TTS de um usuário")
    @app_commands.describe(usuario="Usuário que terá as configurações alteradas", acao="Escolha se quer resetar ou abrir o painel")
    @app_commands.choices(acao=USER_CONFIG_ACTION_CHOICES)
    async def usuario(self, interaction: discord.Interaction, usuario: discord.Member, acao: app_commands.Choice[str]):
        await self._defer_ephemeral(interaction)
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

        if action_value == "reset":
            if not hasattr(db, "reset_user_tts"):
                await self._respond(interaction, embed=self._make_embed("Função indisponível", "Esse banco ainda não suporta resetar as configurações do usuário.", ok=False), ephemeral=True)
                return
            await self._maybe_await(db.reset_user_tts(interaction.guild.id, usuario.id))
            history_entry = f"{self._panel_actor_name(interaction)} resetou as configurações de TTS de {target_name} para os padrões do servidor"
            if hasattr(db, "set_user_panel_last_change"):
                await self._maybe_await(db.set_user_panel_last_change(interaction.guild.id, usuario.id, history_entry))
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
        view.message = msg


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
