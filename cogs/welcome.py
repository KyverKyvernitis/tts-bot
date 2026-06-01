from __future__ import annotations

import asyncio
import base64
import contextlib
import colorsys
import json
import os
from io import BytesIO
from pathlib import Path
import logging
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from typing import Any

import discord
from discord.ext import commands

try:
    from PIL import Image, ImageSequence
except Exception:  # pragma: no cover - fallback if Pillow is unavailable
    Image = None
    ImageSequence = None

log = logging.getLogger(__name__)

WELCOME_DOC_CONFIG = "welcome_config"
WELCOME_DOC_SENT = "welcome_sent_message"
WELCOME_DOC_EMOJI = "welcome_temp_emoji"
MAX_TEXT_DISPLAY = 3900
MAX_TEMPLATE_LENGTH = 1800
MAX_FOOTER_LENGTH = 300
MAX_AUTO_ROLES = 10
MAX_SPECIAL_RULES = 15
MAX_RULE_NAME = 80
MAX_WELCOME_VARIANTS = 3
MAX_VARIANT_NAME = 60
VAR_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")
HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
INVITE_CODE_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:discord\.gg/|discord\.com/invite/)?([A-Za-z0-9_-]{2,64})/?$", re.IGNORECASE)
CUSTOM_EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})>")
DEFAULT_DECORATIVE_EMOJI_LIMIT = 2
OWNER_GUILD_DECORATIVE_EMOJI_LIMIT = 4
MAX_DECORATIVE_EMOJIS = OWNER_GUILD_DECORATIVE_EMOJI_LIMIT
OWNER_PRESENCE_CACHE_SECONDS = 600.0
DISCORD_EMOJI_MAX_BYTES = 256 * 1024

STAR_SEPARATOR_ASSET = Path(__file__).resolve().parents[1] / "assets" / "welcome" / "star_separator.png"
STAR_SEPARATOR_FILENAME = "welcome-stars.png"

DEFAULT_ACCENT = "#5865F2"
DEFAULT_WEBHOOK_NAME = "Boas-vindas"
DEFAULT_PUBLIC = {
    "title": "Bem-vindo(a)!",
    "body": "Olá, {membro_mencao}. Seja bem-vindo(a) ao **{servidor}**.",
    "footer": "Você é o membro #{contador}.",
}
DEFAULT_DM = {
    "title": "Bem-vindo(a) ao {servidor}!",
    "body": "Que bom ter você por aqui, {membro}. Aproveite o servidor.",
    "footer": "",
}

DEFAULT_EMBED = {
    "content": "",
    "author_name": "",
    "author_icon_mode": "none",
    "author_icon_url": "",
    "author_url": "",
    "title": "",
    "title_url": "",
    "description": "",
    "color": "",
    "color_mode": "fixed",
    "thumbnail_mode": "none",
    "thumbnail_url": "",
    "image_mode": "custom",
    "image_url": "",
    "footer_text": "",
    "footer_icon_mode": "none",
    "footer_icon_url": "",
}

PRESETS: dict[str, dict[str, str]] = {
    "simple": {
        "label": "Simples",
        "emoji": "🌱",
        "title": "Bem-vindo(a)!",
        "body": "Olá, {membro_mencao}. Seja bem-vindo(a) ao **{servidor}**.",
        "footer": "Você é o membro #{contador}.",
    },
    "community": {
        "label": "Comunidade",
        "emoji": "✨",
        "title": "Bem-vindo(a) ao {servidor}!",
        "body": "Ei, {membro_mencao}! Entre, fique à vontade e aproveite o servidor.",
        "footer": "Membro #{contador}",
    },
    "gamer": {
        "label": "Gamer",
        "emoji": "🎮",
        "title": "Novo membro entrou na party",
        "body": "{membro_mencao} acabou de chegar no **{servidor}**.",
        "footer": "Agora somos {contador} membros.",
    },
    "compact": {
        "label": "Compacto",
        "emoji": "💫",
        "title": "Bem-vindo(a), {membro}!",
        "body": "Aproveite o **{servidor}**.",
        "footer": "",
    },
    "invite": {
        "label": "Com convite",
        "emoji": "🎁",
        "title": "Bem-vindo(a), {membro}!",
        "body": "{membro_mencao} chegou pelo convite de {convidador_mencao}.",
        "footer": "Convite: {convite_codigo}",
    },
}

VARIABLE_HELP: dict[str, str] = {
    "membro": "nome exibido do membro",
    "membro_mencao": "menção do membro",
    "usuario": "nome de usuário",
    "usuario_id": "ID do membro",
    "membro_id": "ID do membro",
    "membro_avatar": "avatar do membro",
    "servidor": "nome do servidor",
    "servidor_id": "ID do servidor",
    "servidor_icone": "ícone do servidor",
    "contador": "quantidade atual de membros",
    "criado_em": "data de criação da conta",
    "criado_relativo": "há quanto tempo a conta foi criada",
    "entrou_em": "horário da entrada no servidor",
    "convite_codigo": "código do convite usado",
    "convite": "mesmo valor de {convite_codigo}",
    "convite_canal": "nome do canal do convite",
    "convite_canal_mencao": "menção do canal do convite",
    "convite_usos": "quantidade de usos do convite",
    "convidador": "nome de quem convidou",
    "convidador_nome": "nome de quem convidou",
    "convidador_mencao": "menção de quem convidou",
    "convidador_avatar": "avatar de quem convidou",
    "bot_avatar": "avatar do bot",
    "convite_desconhecido": "texto curto quando o convite não for detectado",
}

STYLE_LABELS = {
    "complete": "Completo",
    "simple": "Simples",
    "compact": "Compacto",
}

RENDER_MODE_LABELS = {
    "components_v2": "Components V2",
    "embed": "Embed",
    "normal": "Mensagem normal",
}

RENDER_MODE_DESCRIPTIONS = {
    "components_v2": "Visual moderno com containers e texto V2",
    "embed": "Visual clássico com embed",
    "normal": "Mensagem leve em texto comum",
}

COLOR_MODE_LABELS = {
    "fixed": "Cor fixa",
    "member_avatar": "Combina com a foto do membro",
}

WEBHOOK_AVATAR_LABELS = {
    "server": "Avatar do servidor",
    "member": "Avatar do membro",
    "inviter": "Avatar de quem convidou",
    "custom": "Avatar por link",
}

EMBED_IMAGE_MODE_LABELS = {
    "none": "Sem imagem",
    "member": "Avatar do membro",
    "inviter": "Avatar de quem convidou",
    "server": "Ícone do servidor",
    "bot": "Avatar do bot",
    "custom": "Link personalizado",
}

EMBED_MAIN_IMAGE_MODE_LABELS = {
    **EMBED_IMAGE_MODE_LABELS,
    "avatar_stars": "Estrelas combinando com o membro",
}

MEDIA_MODE_LABELS = {
    "custom": "Link personalizado",
    "avatar_stars": "Estrelas combinando com o membro",
}

WEBHOOK_NAME_LABELS = {
    "fixed": "Nome personalizado",
    "server": "Nome do servidor",
    "member": "Nome do membro",
    "inviter": "Nome de quem convidou",
}

RULE_TYPE_LABELS = {
    "invite_code": "Convite específico",
    "inviter": "Quem convidou",
    "invite_channel": "Canal do convite",
}

RULE_PRIORITY = ("invite_code", "inviter", "invite_channel")


def _trim(text: Any, limit: int = MAX_TEXT_DISPLAY) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 20)].rstrip() + "\n…"


def _channel_mention(channel_id: int | None) -> str:
    try:
        cid = int(channel_id or 0)
    except Exception:
        cid = 0
    return f"<#{cid}>" if cid else "não escolhido"


def _role_list(guild: discord.Guild | None, role_ids: list[int], *, empty: str = "nenhum") -> str:
    values: list[str] = []
    for role_id in role_ids:
        role = guild.get_role(int(role_id)) if guild is not None else None
        values.append(role.mention if role is not None else f"cargo {role_id}")
    return ", ".join(values) if values else empty


def _user_mention(user_id: int | str | None) -> str:
    try:
        uid = int(user_id or 0)
    except Exception:
        uid = 0
    return f"<@{uid}>" if uid else "não escolhido"


def _parse_hex(value: Any, fallback: str = DEFAULT_ACCENT) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = fallback
    if not HEX_RE.fullmatch(raw):
        raw = fallback
    raw = raw.upper()
    if not raw.startswith("#"):
        raw = f"#{raw}"
    return raw


def _color_from_hex(value: Any, fallback: str = DEFAULT_ACCENT) -> discord.Color:
    raw = _parse_hex(value, fallback).lstrip("#")
    try:
        return discord.Color(int(raw, 16))
    except Exception:
        return discord.Color.blurple()


def _clean_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("attachment://"):
        return raw[:1000]
    if not URL_RE.fullmatch(raw):
        return ""
    return raw[:1000]


def _image_mode(value: Any, *, fallback: str = "none") -> str:
    mode = str(value or fallback).strip().lower()
    return mode if mode in EMBED_MAIN_IMAGE_MODE_LABELS else fallback


def _media_mode(value: Any, *, fallback: str = "custom") -> str:
    mode = str(value or fallback).strip().lower()
    return mode if mode in MEDIA_MODE_LABELS else fallback


def _has_custom_embed(embed: dict[str, Any] | None) -> bool:
    data = dict(embed or {}) if isinstance(embed, dict) else {}
    for key, default in DEFAULT_EMBED.items():
        if str(data.get(key) or "") != str(default or ""):
            return True
    return False


def _clean_invite_code(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = INVITE_CODE_RE.match(raw)
    if not match:
        return ""
    return match.group(1)[:64]


def _status_label(value: bool) -> str:
    return "Ligado" if value else "Desligado"


def _template_changed(cfg: dict[str, Any]) -> bool:
    public = dict(cfg.get("public") or {})
    return any(str(public.get(k) or "") != str(DEFAULT_PUBLIC.get(k) or "") for k in DEFAULT_PUBLIC)


def _safe_webhook_name(value: Any, fallback: str = DEFAULT_WEBHOOK_NAME) -> str:
    raw = str(value or "").strip() or fallback
    raw = re.sub(r"\s+", " ", raw)
    raw = raw.replace("discord", "disc0rd").replace("Discord", "Disc0rd")
    raw = raw.replace("clyde", "cly.de").replace("Clyde", "Cly.de")
    return raw[:80] or fallback


def _new_rule_id() -> str:
    return uuid.uuid4().hex[:10]


def _make_notice_view(title: str, body: str | list[str], *, ok: bool = True) -> discord.ui.LayoutView:
    body_text = "\n".join(str(item) for item in body) if isinstance(body, list) else str(body or "")
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(_trim(f"# {title}\n{body_text}")),
        accent_color=discord.Color.green() if ok else discord.Color.red(),
    ))
    return view



def _advanced_modal_supported(*components: str) -> bool:
    needed = components or ("Label", "RadioGroup", "CheckboxGroup")
    return all(hasattr(discord.ui, name) for name in needed)


def _modal_values(component: Any) -> list[str]:
    values = getattr(component, "values", None)
    if values is None:
        value = getattr(component, "value", None)
        if value is None:
            return []
        return [str(value)]
    if isinstance(values, (str, int)):
        return [str(values)]
    try:
        return [str(item) for item in values if str(item)]
    except TypeError:
        return [str(values)] if values else []


def _modal_value(component: Any, default: str = "") -> str:
    values = _modal_values(component)
    return values[0] if values else default


def _id_from_text(value: Any) -> int:
    raw = str(value or "").strip()
    match = re.search(r"(\d{15,25})", raw)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except Exception:
        return 0


class _BackButton(discord.ui.Button):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(label="Voltar", emoji="↩️", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        self.panel.go_back()
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _CloseButton(discord.ui.Button):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(label="Fechar", emoji="✖️", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        self.panel.stop()
        with contextlib.suppress(Exception):
            if not interaction.response.is_done():
                await interaction.response.defer()
        targets = [self.panel.message, self.panel.command_message]
        for message in targets:
            if message is None:
                continue
            with contextlib.suppress(discord.HTTPException, discord.NotFound, discord.Forbidden):
                await message.delete()


class _PreviewButton(discord.ui.Button):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(label="Preview", emoji="👁️", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await self.panel.send_preview(interaction)


class _MainSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        options = [
            discord.SelectOption(label="Boas-vindas", value="message", emoji="📢", description="Editar mensagem, modo e variações"),
            discord.SelectOption(label="Canal de envio", value="channel", emoji="📍", description="Onde a mensagem vai aparecer"),
            discord.SelectOption(label="Webhook de boas-vindas", value="webhook", emoji="🪝", description="Nome e avatar próprios para receber membros"),
            discord.SelectOption(label="Mensagem privada", value="dm", emoji="💬", description="Mensagem opcional no privado"),
            discord.SelectOption(label="Cargos automáticos", value="roles", emoji="🎭", description="Cargos entregues ao entrar"),
            discord.SelectOption(label="Visual da mensagem", value="visual", emoji="🖼️", description="Estilo, cor, imagem e emojis"),
            discord.SelectOption(label="Variáveis", value="variables", emoji="🧬", description="Palavras que o bot troca sozinho"),
            discord.SelectOption(label="Boas-vindas especiais", value="special", emoji="🎁", description="Estilos diferentes por convite"),
            discord.SelectOption(label="Configurações", value="settings", emoji="⚙️", description="Ligar, pausar e apagar ao sair"),
        ]
        super().__init__(placeholder="O que você quer configurar?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        value = str(self.values[0])
        self.panel.notice = ""
        if value == "message":
            self.panel.go_to("message")
            self.panel.notice = ""
            self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
            await interaction.response.edit_message(view=self.panel)
            return
        if value == "settings":
            await interaction.response.send_modal(WelcomeSettingsModal(self.panel))
            return
        self.panel.go_to(value)
        if self.panel.screen == "webhook_existing":
            await self.panel.load_webhooks(interaction.guild)
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _MessageActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        mode = str(panel.config.get("render_mode") or "components_v2")
        if mode == "embed":
            options = [
                discord.SelectOption(label="Trocar modo", value="change_mode", emoji="🎨", description="Components V2, Embed ou mensagem normal"),
                discord.SelectOption(label="Mensagem acima", value="embed_content", emoji="📝", description="Texto normal antes do embed"),
                discord.SelectOption(label="Author", value="embed_author", emoji="👤", description="Nome, ícone e link do author"),
                discord.SelectOption(label="Título e descrição", value="embed_text", emoji="🏷️", description="Título, descrição, link e cor"),
                discord.SelectOption(label="Imagens", value="embed_images", emoji="🖼️", description="Thumbnail e imagem principal"),
                discord.SelectOption(label="Footer do embed", value="embed_footer", emoji="📌", description="Texto pequeno nativo do embed"),
                discord.SelectOption(label="Variações da mensagem", value="variants", emoji="🎲", description="Até 3 mensagens com chance própria"),
                discord.SelectOption(label="Escolher preset", value="presets", emoji="✨", description="Usar uma base pronta"),
            ]
            placeholder = "O que deseja editar no embed?"
        elif mode == "normal":
            options = [
                discord.SelectOption(label="Trocar modo", value="change_mode", emoji="🎨", description="Components V2, Embed ou mensagem normal"),
                discord.SelectOption(label="Editar texto", value="normal_edit", emoji="✏️", description="Mensagem normal em texto comum"),
                discord.SelectOption(label="Variações da mensagem", value="variants", emoji="🎲", description="Até 3 mensagens com chance própria"),
                discord.SelectOption(label="Escolher preset", value="presets", emoji="✨", description="Usar uma base pronta"),
                discord.SelectOption(label="Restaurar texto padrão", value="restore", emoji="↩️", description="Voltar para o texto inicial"),
            ]
            placeholder = "O que deseja editar no texto?"
        else:
            options = [
                discord.SelectOption(label="Trocar modo", value="change_mode", emoji="🎨", description="Components V2, Embed ou mensagem normal"),
                discord.SelectOption(label="Editar texto V2", value="v2_edit", emoji="✏️", description="Título, texto principal e texto final"),
                discord.SelectOption(label="Visual e imagem V2", value="v2_visual", emoji="🖼️", description="Estilo, cor e imagem do container"),
                discord.SelectOption(label="Variações da mensagem", value="variants", emoji="🎲", description="Até 3 mensagens com chance própria"),
                discord.SelectOption(label="Escolher preset", value="presets", emoji="✨", description="Usar uma base pronta"),
                discord.SelectOption(label="Restaurar mensagem padrão", value="restore", emoji="↩️", description="Voltar para o texto inicial"),
            ]
            placeholder = "O que deseja editar na mensagem V2?"
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "change_mode":
            await interaction.response.send_modal(WelcomeModeModal(self.panel))
            return
        if action == "v2_edit":
            await interaction.response.send_modal(WelcomeMessageModal(self.panel))
            return
        if action == "normal_edit":
            await interaction.response.send_modal(WelcomeNormalMessageModal(self.panel))
            return
        if action == "v2_visual":
            await interaction.response.send_modal(WelcomeVisualModal(self.panel))
            return
        if action == "embed_content":
            await interaction.response.send_modal(WelcomeEmbedContentModal(self.panel))
            return
        if action == "embed_text":
            await interaction.response.send_modal(WelcomeEmbedTextModal(self.panel))
            return
        if action == "embed_author":
            await interaction.response.send_modal(WelcomeEmbedAuthorModal(self.panel))
            return
        if action == "embed_images":
            await interaction.response.send_modal(WelcomeEmbedImagesModal(self.panel))
            return
        if action == "embed_footer":
            await interaction.response.send_modal(WelcomeEmbedFooterModal(self.panel))
            return
        if action == "variants":
            self.panel.go_to("variants")
            self.panel.notice = ""
        elif action == "presets":
            self.panel.go_to("presets")
            self.panel.notice = ""
        elif action == "restore":
            cfg = deepcopy(self.panel.config)
            cfg["public"] = dict(DEFAULT_PUBLIC)
            await self.panel.save_config(cfg, "Mensagem padrão restaurada.")
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _PresetSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        options = []
        for key, preset in PRESETS.items():
            options.append(discord.SelectOption(
                label=str(preset["label"]),
                value=key,
                emoji=str(preset["emoji"]),
                description="Aplicar este estilo de texto",
            ))
        super().__init__(placeholder="Escolha um preset", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        key = str(self.values[0])
        preset = PRESETS.get(key)
        if preset is None:
            await interaction.response.send_message(
                view=_make_notice_view("Preset indisponível", "Escolha uma opção da lista.", ok=False),
                ephemeral=True,
            )
            return
        cfg = deepcopy(self.panel.config)
        cfg["public"] = {
            "title": str(preset.get("title") or DEFAULT_PUBLIC["title"]),
            "body": str(preset.get("body") or DEFAULT_PUBLIC["body"]),
            "footer": str(preset.get("footer") or ""),
        }
        await self.panel.save_config(cfg, f"Preset **{preset['label']}** aplicado.")
        self.panel.screen = "message"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _EmbedActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        options = [
            discord.SelectOption(label="Mensagem acima", value="content", emoji="📝", description="Texto normal antes do embed"),
            discord.SelectOption(label="Author", value="author", emoji="👤", description="Nome, ícone e link do author"),
            discord.SelectOption(label="Título e descrição", value="text", emoji="🏷️", description="Título, descrição, link e cor"),
            discord.SelectOption(label="Imagens", value="images", emoji="🖼️", description="Thumbnail e imagem principal"),
            discord.SelectOption(label="Footer do embed", value="footer", emoji="📌", description="Texto pequeno nativo do embed"),
        ]
        super().__init__(placeholder="O que deseja editar no embed?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "content":
            await interaction.response.send_modal(WelcomeEmbedContentModal(self.panel))
            return
        if action == "text":
            await interaction.response.send_modal(WelcomeEmbedTextModal(self.panel))
            return
        if action == "author":
            await interaction.response.send_modal(WelcomeEmbedAuthorModal(self.panel))
            return
        if action == "images":
            await interaction.response.send_modal(WelcomeEmbedImagesModal(self.panel))
            return
        if action == "footer":
            await interaction.response.send_modal(WelcomeEmbedFooterModal(self.panel))
            return


class _RenderModeSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        current = str(panel.config.get("render_mode") or "components_v2")
        options = [
            discord.SelectOption(label=label, value=key, emoji=("✨" if key == "components_v2" else "🧾" if key == "embed" else "💬"), description=RENDER_MODE_DESCRIPTIONS[key], default=current == key)
            for key, label in RENDER_MODE_LABELS.items()
        ]
        super().__init__(placeholder="Escolha o modo da mensagem pública", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        mode = str(self.values[0])
        cfg = self.panel.cog._switch_public_mode(self.panel.config, mode)
        await self.panel.save_config(cfg, f"Modo ajustado para **{RENDER_MODE_LABELS.get(mode, 'Components V2')}**.")
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _ModeActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        options = [discord.SelectOption(label="Voltar ao início", value="home", emoji="↩️")]
        super().__init__(placeholder="Mais opções do modo", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.panel.screen = "home"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder="Escolha o canal de boas-vindas",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0] if self.values else None
        channel = await self.panel.cog._resolve_text_channel(interaction.guild, selected)
        if channel is None:
            await interaction.response.send_message(
                view=_make_notice_view("Canal inválido", "Escolha um canal de texto.", ok=False),
                ephemeral=True,
            )
            return
        missing = self.panel.cog._missing_channel_permissions(channel)
        if missing:
            await interaction.response.send_message(
                view=_make_notice_view("Não consigo usar esse canal", missing, ok=False),
                ephemeral=True,
            )
            return
        cfg = deepcopy(self.panel.config)
        cfg["channel_id"] = int(channel.id)
        await self.panel.save_config(cfg, f"Pronto, as boas-vindas vão aparecer em {channel.mention}.")
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _ChannelActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="Mais opções do canal",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Remover canal salvo", value="clear", emoji="🧹")],
        )

    async def callback(self, interaction: discord.Interaction):
        cfg = deepcopy(self.panel.config)
        cfg["channel_id"] = 0
        cfg["enabled"] = False
        await self.panel.save_config(cfg, "Canal removido. As boas-vindas ficaram desligadas por enquanto.")
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _WebhookNameModeSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        webhook = dict(panel.config.get("webhook") or {})
        current = str(webhook.get("name_mode") or "fixed")
        options = [
            discord.SelectOption(label=label, value=key, emoji=("✏️" if key == "fixed" else "🏠" if key == "server" else "👤" if key == "member" else "🎁"), default=current == key)
            for key, label in WEBHOOK_NAME_LABELS.items()
        ]
        super().__init__(placeholder="Escolha o nome usado pelo webhook", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        cfg = deepcopy(self.panel.config)
        webhook = dict(cfg.get("webhook") or {})
        webhook["name_mode"] = str(self.values[0])
        cfg["webhook"] = webhook
        await self.panel.save_config(cfg, f"Nome do webhook: **{WEBHOOK_NAME_LABELS.get(webhook['name_mode'], 'Nome personalizado')}**.")
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _WebhookActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        options = [
            discord.SelectOption(label="Configurar webhook", value="setup", emoji="🪝", description="Bot, webhook novo ou existente"),
            discord.SelectOption(label="Aparência do envio", value="appearance", emoji="🎭", description="Nome e avatar usados nas boas-vindas"),
            discord.SelectOption(label="Remover ou desativar", value="clear", emoji="🧹", description="Voltar para envio simples pelo bot"),
        ]
        super().__init__(placeholder="O que deseja ajustar no webhook?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "setup":
            if not _advanced_modal_supported("Label"):
                await interaction.response.send_message(
                    view=_make_notice_view("Ainda não disponível", "Essa versão da biblioteca não abriu o formulário moderno de webhook.", ok=False),
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(WelcomeWebhookSetupModal(self.panel))
            return
        if action == "appearance":
            await interaction.response.send_modal(WelcomeWebhookAppearanceModal(self.panel))
            return
        cfg = deepcopy(self.panel.config)
        cfg["webhook"] = self.panel.cog._default_webhook_config()
        await self.panel.save_config(cfg, "Envio pelo bot ativado.")
        self.panel.screen = "webhook"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _WebhookExistingSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        options: list[discord.SelectOption] = []
        for item in panel.webhook_choices[:25]:
            wid = str(item.get("id") or "")
            name = str(item.get("name") or f"Webhook {wid}")[:100]
            options.append(discord.SelectOption(label=name, value=wid, emoji="🪝", description=f"ID {wid}"[:100]))
        if not options:
            options = [discord.SelectOption(label="Nenhum webhook encontrado", value="none", emoji="🪝", description="Crie um webhook primeiro")]
        super().__init__(placeholder="Escolha o webhook", min_values=1, max_values=1, options=options, disabled=not panel.webhook_choices)

    async def callback(self, interaction: discord.Interaction):
        wid = str(self.values[0])
        selected = next((item for item in self.panel.webhook_choices if str(item.get("id")) == wid), None)
        if selected is None:
            await interaction.response.send_message(
                view=_make_notice_view("Webhook indisponível", "Escolha uma opção válida.", ok=False),
                ephemeral=True,
            )
            return
        cfg = deepcopy(self.panel.config)
        webhook_cfg = dict(cfg.get("webhook") or {})
        webhook_cfg.update({
            "enabled": True,
            "channel_id": int(selected.get("channel_id") or cfg.get("channel_id") or 0),
            "webhook_id": int(selected.get("id") or 0),
            "webhook_token": str(selected.get("token") or webhook_cfg.get("webhook_token") or ""),
        })
        cfg["webhook"] = webhook_cfg
        await self.panel.save_config(cfg, "Webhook escolhido.")
        self.panel.screen = "webhook"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _DmActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        options = [
            discord.SelectOption(label="Configurar privado", value="config", emoji="⚙️", description="Ligar, desligar e escolher o modo"),
            discord.SelectOption(label="Editar texto", value="edit", emoji="✏️", description="Título, mensagem e texto final"),
            discord.SelectOption(label="Restaurar mensagem padrão", value="restore", emoji="↩️"),
        ]
        super().__init__(placeholder="O que deseja ajustar no privado?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "edit":
            await interaction.response.send_modal(WelcomeDmModal(self.panel))
            return
        if action == "config":
            if not _advanced_modal_supported("Label", "RadioGroup", "CheckboxGroup"):
                await interaction.response.send_message(
                    view=_make_notice_view("Ainda não disponível", "Essa versão da biblioteca não abriu as opções modernas de privado.", ok=False),
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(WelcomeDmOptionsModal(self.panel))
            return
        cfg = deepcopy(self.panel.config)
        if action == "restore":
            cfg["dm"] = dict(DEFAULT_DM)
            await self.panel.save_config(cfg, "Mensagem privada restaurada.")
            self.panel.screen = "dm"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _DmRenderModeSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        current = str(panel.config.get("dm_render_mode") or panel.config.get("render_mode") or "components_v2")
        options = [
            discord.SelectOption(label=label, value=key, emoji=("✨" if key == "components_v2" else "🧾" if key == "embed" else "💬"), description=RENDER_MODE_DESCRIPTIONS[key], default=current == key)
            for key, label in RENDER_MODE_LABELS.items()
        ]
        super().__init__(placeholder="Escolha o modo da mensagem privada", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        cfg = deepcopy(self.panel.config)
        cfg["dm_render_mode"] = str(self.values[0])
        await self.panel.save_config(cfg, f"Mensagem privada em **{RENDER_MODE_LABELS.get(cfg['dm_render_mode'], 'Components V2')}**.")
        self.panel.screen = "dm"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _RoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="Escolha os cargos automáticos",
            min_values=0,
            max_values=MAX_AUTO_ROLES,
        )

    async def callback(self, interaction: discord.Interaction):
        role_ids, skipped = self.panel.cog._safe_role_ids(interaction.guild, list(self.values or []))
        cfg = deepcopy(self.panel.config)
        cfg["auto_role_ids"] = role_ids
        if skipped:
            notice = "Salvei os cargos possíveis. Alguns precisam ficar abaixo do meu cargo."
        elif role_ids:
            notice = "Pronto, esses cargos serão entregues quando alguém entrar."
        else:
            notice = "Nenhum cargo automático ficou salvo."
        await self.panel.save_config(cfg, notice)
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _RoleActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(
            placeholder="Mais opções de cargos",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Limpar cargos automáticos", value="clear", emoji="🧹")],
        )

    async def callback(self, interaction: discord.Interaction):
        cfg = deepcopy(self.panel.config)
        cfg["auto_role_ids"] = []
        await self.panel.save_config(cfg, "Cargos automáticos removidos.")
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _VisualStyleSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        current = str(panel.config.get("style") or "complete")
        options = []
        for key, label in STYLE_LABELS.items():
            options.append(discord.SelectOption(label=label, value=key, emoji="🖼️", default=current == key))
        super().__init__(placeholder="Escolha o estilo da mensagem", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        cfg = deepcopy(self.panel.config)
        cfg["style"] = str(self.values[0])
        await self.panel.save_config(cfg, f"Visual ajustado para **{STYLE_LABELS.get(cfg['style'], 'Completo')}**.")
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _VisualActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        options = [
            discord.SelectOption(label="Editar visual", value="edit", emoji="🎨", description="Estilo, cor e imagem"),
            discord.SelectOption(label="Emojis decorativos", value="decorative_emojis", emoji="✨", description="Colorir os emojis da mensagem"),
            discord.SelectOption(label="Remover imagem", value="clear_image", emoji="🧹"),
        ]
        super().__init__(placeholder="Mais opções do visual", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "edit":
            await interaction.response.send_modal(WelcomeVisualModal(self.panel))
            return
        if action == "decorative_emojis":
            if not _advanced_modal_supported("Label", "CheckboxGroup"):
                await interaction.response.send_message(
                    view=_make_notice_view("Ainda não disponível", "Essa versão da biblioteca não abriu o formulário moderno de emojis.", ok=False),
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(WelcomeDecorativeEmojiModal(self.panel))
            return
        if action == "clear_image":
            cfg = deepcopy(self.panel.config)
            cfg["media_url"] = ""
            cfg["media_mode"] = "custom"
            await self.panel.save_config(cfg, "Imagem removida.")
            self.panel.screen = "visual"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _SpecialMainSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        rules = panel.config.get("special_rules") or []
        options = [
            discord.SelectOption(label="Criar regra especial", value="create", emoji="🎁", description="Por convite, convidador ou canal"),
            discord.SelectOption(label="Editar regras existentes", value="list", emoji="✏️", description=f"{len(rules)} regra(s) criada(s)"),
            discord.SelectOption(label="Atualizar convites agora", value="refresh", emoji="🔄", description="Ajuda a reconhecer próximos convites"),
        ]
        super().__init__(placeholder="O que deseja configurar nas boas-vindas especiais?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "create":
            if not _advanced_modal_supported("Label"):
                await interaction.response.send_modal(SpecialInviteRuleModal(self.panel))
                return
            await interaction.response.send_modal(SpecialRuleCreateModal(self.panel))
            return
        if action == "list":
            if not (self.panel.config.get("special_rules") or []):
                self.panel.notice = "Ainda não há regras especiais."
            else:
                self.panel.go_to("special_list")
                self.panel.notice = "Escolha uma regra para editar."
        elif action == "refresh":
            ok = await self.panel.cog._refresh_invite_cache_for_guild(interaction.guild, self.panel.config)
            self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
            self.panel.notice = "Convites atualizados." if ok else "Não consegui ver os convites agora."
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _SpecialRuleListSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        options: list[discord.SelectOption] = []
        for rule in (panel.config.get("special_rules") or [])[:25]:
            label = str(rule.get("name") or "Regra especial")[:100]
            match_label = panel.cog._rule_match_summary(rule)
            options.append(discord.SelectOption(label=label, value=str(rule.get("id") or ""), emoji="🎁", description=match_label[:100]))
        super().__init__(placeholder="Escolha uma regra", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.panel.selected_rule_id = str(self.values[0])
        self.panel.go_to("special_rule")
        self.panel.notice = ""
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _SpecialInviterSelect(discord.ui.UserSelect):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(placeholder="Escolha o convidador", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        user = self.values[0] if self.values else None
        if user is None:
            await interaction.response.send_message(view=_make_notice_view("Pessoa inválida", "Escolha uma pessoa da lista.", ok=False), ephemeral=True)
            return
        cfg = deepcopy(self.panel.config)
        rules = list(cfg.get("special_rules") or [])
        if len(rules) >= MAX_SPECIAL_RULES:
            await interaction.response.send_message(view=_make_notice_view("Limite atingido", "Remova uma regra antiga antes de criar outra.", ok=False), ephemeral=True)
            return
        rule = self.panel.cog._make_rule(
            name=f"Convites de {getattr(user, 'display_name', getattr(user, 'name', 'membro'))}",
            match_type="inviter",
            match_value=str(int(getattr(user, "id", 0) or 0)),
        )
        rules.append(rule)
        cfg["special_rules"] = rules
        await self.panel.save_config(cfg, "Regra criada.")
        self.panel.selected_rule_id = rule["id"]
        self.panel.screen = "special_rule"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _SpecialInviteChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        channel_types = [discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.voice, discord.ChannelType.stage_voice]
        super().__init__(channel_types=channel_types, placeholder="Escolha o canal do convite", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0] if self.values else None
        if channel is None:
            await interaction.response.send_message(view=_make_notice_view("Canal inválido", "Escolha um canal da lista.", ok=False), ephemeral=True)
            return
        cfg = deepcopy(self.panel.config)
        rules = list(cfg.get("special_rules") or [])
        if len(rules) >= MAX_SPECIAL_RULES:
            await interaction.response.send_message(view=_make_notice_view("Limite atingido", "Remova uma regra antiga antes de criar outra.", ok=False), ephemeral=True)
            return
        rule = self.panel.cog._make_rule(
            name=f"Convites de #{getattr(channel, 'name', 'canal')}",
            match_type="invite_channel",
            match_value=str(int(getattr(channel, "id", 0) or 0)),
        )
        rules.append(rule)
        cfg["special_rules"] = rules
        await self.panel.save_config(cfg, "Regra criada.")
        self.panel.selected_rule_id = rule["id"]
        self.panel.screen = "special_rule"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _SpecialRuleActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView", rule: dict[str, Any]):
        self.panel = panel
        self.rule_id = str(rule.get("id") or "")
        options = [
            discord.SelectOption(label="Editar texto especial", value="edit_text", emoji="✏️"),
            discord.SelectOption(label="Modo da mensagem", value="mode", emoji="🎨"),
            discord.SelectOption(label="Visual especial", value="visual", emoji="🖼️"),
            discord.SelectOption(label="Canal especial", value="channel", emoji="📍"),
            discord.SelectOption(label="Cargos extras", value="roles", emoji="🎭"),
            discord.SelectOption(label="Perfil do webhook", value="webhook", emoji="🪝"),
            discord.SelectOption(label="Ligar regra" if not rule.get("enabled", True) else "Pausar regra", value="toggle", emoji="⏸️"),
            discord.SelectOption(label="Limpar ajustes da regra", value="clear", emoji="🧹"),
            discord.SelectOption(label="Remover regra", value="delete", emoji="🗑️"),
        ]
        super().__init__(placeholder="O que deseja ajustar nessa regra?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "edit_text":
            rule = self.panel.cog._find_rule(self.panel.config, self.rule_id)
            await interaction.response.send_modal(SpecialRuleTextModal(self.panel, rule))
            return
        if action == "visual":
            rule = self.panel.cog._find_rule(self.panel.config, self.rule_id)
            await interaction.response.send_modal(SpecialRuleVisualModal(self.panel, rule))
            return
        if action == "webhook":
            rule = self.panel.cog._find_rule(self.panel.config, self.rule_id)
            await interaction.response.send_modal(SpecialRuleWebhookModal(self.panel, rule))
            return
        if action == "mode":
            self.panel.go_to("special_rule_mode")
        elif action == "channel":
            self.panel.go_to("special_rule_channel")
        elif action == "roles":
            self.panel.go_to("special_rule_roles")
        elif action in {"toggle", "clear", "delete"}:
            cfg = deepcopy(self.panel.config)
            rules = list(cfg.get("special_rules") or [])
            idx = next((i for i, item in enumerate(rules) if str(item.get("id")) == self.rule_id), -1)
            if idx < 0:
                self.panel.screen = "special"
                self.panel.notice = "Essa regra não existe mais."
            elif action == "toggle":
                rules[idx]["enabled"] = not bool(rules[idx].get("enabled", True))
                cfg["special_rules"] = rules
                await self.panel.save_config(cfg, "Regra ligada." if rules[idx]["enabled"] else "Regra pausada.")
            elif action == "clear":
                old = rules[idx]
                rules[idx] = self.panel.cog._make_rule(
                    name=str(old.get("name") or "Regra especial"),
                    match_type=str(old.get("match_type") or "invite_code"),
                    match_value=str(old.get("match_value") or ""),
                    rule_id=str(old.get("id") or self.rule_id),
                    enabled=bool(old.get("enabled", True)),
                )
                cfg["special_rules"] = rules
                await self.panel.save_config(cfg, "Ajustes da regra limpos.")
            elif action == "delete":
                del rules[idx]
                cfg["special_rules"] = rules
                await self.panel.save_config(cfg, "Regra removida.")
                self.panel.selected_rule_id = ""
                self.panel.screen = "special"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _SpecialRuleModeSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView", rule: dict[str, Any]):
        self.panel = panel
        current = str(rule.get("render_mode") or "inherit")
        options = [discord.SelectOption(label="Usar modo padrão", value="inherit", emoji="↩️", default=current == "inherit")]
        for key, label in RENDER_MODE_LABELS.items():
            options.append(discord.SelectOption(label=label, value=key, emoji=("✨" if key == "components_v2" else "🧾" if key == "embed" else "💬"), default=current == key))
        super().__init__(placeholder="Modo da regra", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await self.panel.update_selected_rule({"render_mode": str(self.values[0])}, "Modo da regra atualizado.")
        self.panel.screen = "special_rule"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _SpecialRuleChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(channel_types=[discord.ChannelType.text, discord.ChannelType.news], placeholder="Canal especial de envio", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        channel = await self.panel.cog._resolve_text_channel(interaction.guild, self.values[0] if self.values else None)
        if channel is None:
            await interaction.response.send_message(view=_make_notice_view("Canal inválido", "Escolha um canal de texto.", ok=False), ephemeral=True)
            return
        missing = self.panel.cog._missing_channel_permissions(channel)
        if missing:
            await interaction.response.send_message(view=_make_notice_view("Não consigo usar esse canal", missing, ok=False), ephemeral=True)
            return
        await self.panel.update_selected_rule({"channel_id": int(channel.id)}, f"Regra vai enviar em {channel.mention}.")
        self.panel.screen = "special_rule"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _SpecialRuleChannelActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(placeholder="Mais opções do canal especial", min_values=1, max_values=1, options=[discord.SelectOption(label="Usar canal padrão", value="clear", emoji="↩️")])

    async def callback(self, interaction: discord.Interaction):
        await self.panel.update_selected_rule({"channel_id": 0}, "A regra voltou a usar o canal padrão.")
        self.panel.screen = "special_rule"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _SpecialRuleRoleSelect(discord.ui.RoleSelect):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(placeholder="Cargos extras dessa regra", min_values=0, max_values=MAX_AUTO_ROLES)

    async def callback(self, interaction: discord.Interaction):
        role_ids, skipped = self.panel.cog._safe_role_ids(interaction.guild, list(self.values or []))
        notice = "Cargos extras salvos."
        if skipped:
            notice = "Salvei os cargos possíveis. Alguns precisam ficar abaixo do meu cargo."
        elif not role_ids:
            notice = "Nenhum cargo extra ficou salvo nessa regra."
        await self.panel.update_selected_rule({"auto_role_ids": role_ids}, notice)
        self.panel.screen = "special_rule"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _StatusSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        enabled = bool(panel.config.get("enabled", False))
        delete_on_leave = bool(panel.config.get("delete_on_leave_enabled", False))
        options = [
            discord.SelectOption(
                label="Ligar boas-vindas",
                value="enable",
                emoji="✅",
                description="Já está ligado" if enabled else "Começar a enviar quando alguém entrar",
            ),
            discord.SelectOption(
                label="Desligar boas-vindas",
                value="disable",
                emoji="⏸️",
                description="Pausar as mensagens" if enabled else "Já está desligado",
            ),
            discord.SelectOption(
                label="Apagar quando sair",
                value="delete_on",
                emoji="🧹",
                description="Ativo · até 24 horas" if delete_on_leave else "Apagar se sair em até 24 horas",
            ),
            discord.SelectOption(
                label="Manter quando sair",
                value="delete_off",
                emoji="💬",
                description="Ativo" if not delete_on_leave else "Não apagar a mensagem ao sair",
            ),
        ]
        # Não marcamos nenhuma opção como default aqui: este select mistura duas
        # escolhas independentes (status e apagar ao sair). Um select de valor
        # único com dois defaults gera HTTP 400 Invalid Form Body no Discord.
        super().__init__(placeholder="Escolha o que ajustar", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        cfg = deepcopy(self.panel.config)
        if action in {"delete_on", "delete_off"}:
            cfg["delete_on_leave_enabled"] = action == "delete_on"
            await self.panel.save_config(
                cfg,
                "Vou apagar a boas-vindas se o membro sair em até 24 horas." if cfg["delete_on_leave_enabled"] else "Vou manter a boas-vindas mesmo se o membro sair.",
            )
        else:
            want_enable = action == "enable"
            if want_enable and not int(cfg.get("channel_id") or 0):
                self.panel.notice = "Escolha um canal antes de ligar."
            else:
                cfg["enabled"] = want_enable
                await self.panel.save_config(cfg, "Boas-vindas ligadas." if want_enable else "Boas-vindas pausadas.")
                if want_enable:
                    asyncio.create_task(self.panel.cog._refresh_invite_cache_for_guild(interaction.guild, cfg))
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)



class _VariantListSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        variants = [panel.cog._normalize_variant(v) for v in panel.config.get("variants") or []]
        percentages = panel.cog._variant_percentages(variants)
        options: list[discord.SelectOption] = []
        for index, variant in enumerate(variants, start=1):
            vid = str(variant.get("id"))
            percent = percentages.get(vid, 0.0)
            status = "pausada" if not bool(variant.get("enabled", True)) else f"{percent:.0f}%"
            options.append(discord.SelectOption(
                label=str(variant.get("name") or f"Variação {index}")[:80],
                value=vid,
                emoji="🎲",
                description=f"Peso {int(variant.get('weight') or 1)} · {status}",
            ))
        if not options:
            options.append(discord.SelectOption(label="Nenhuma variação", value="none", emoji="🎲", description="Crie uma variação primeiro"))
        super().__init__(placeholder="Escolha uma variação", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        value = str(self.values[0])
        if value == "none":
            await interaction.response.send_message(view=_make_notice_view("Nenhuma variação", "Crie uma variação primeiro.", ok=False), ephemeral=True)
            return
        self.panel.selected_variant_id = value
        self.panel.go_to("variant_detail")
        self.panel.notice = ""
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _VariantActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        variants = list(panel.config.get("variants") or [])
        if len(variants) < MAX_WELCOME_VARIANTS:
            options = [discord.SelectOption(label="Criar variação", value="create", emoji="➕", description=f"Até {MAX_WELCOME_VARIANTS} variações")]
        else:
            options = [discord.SelectOption(label="Limite de variações atingido", value="noop", emoji="🎲", description=f"Máximo: {MAX_WELCOME_VARIANTS}")]
        super().__init__(placeholder="O que deseja fazer?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "create":
            await interaction.response.send_modal(WelcomeVariantCreateModal(self.panel))
            return
        self.panel.notice = "Você já chegou ao limite de variações." if action == "noop" else ""
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _VariantEditActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        mode = str(panel.config.get("render_mode") or "components_v2")
        if mode == "embed":
            edit_label = "Editar embed da variação"
            edit_desc = "Mensagem acima, título, descrição e footer"
        elif mode == "normal":
            edit_label = "Editar texto da variação"
            edit_desc = "Mensagem simples"
        else:
            edit_label = "Editar mensagem V2 da variação"
            edit_desc = "Título, texto e texto final"
        options = [
            discord.SelectOption(label=edit_label, value="content", emoji="✏️", description=edit_desc),
            discord.SelectOption(label="Nome e chance", value="settings", emoji="🎚️", description="Nome, peso e ativação"),
            discord.SelectOption(label="Visual da variação", value="visual", emoji="🖼️", description="Cor, imagem e estilo"),
            discord.SelectOption(label="Remover variação", value="remove", emoji="🧹", description="Apaga esta variação"),
        ]
        super().__init__(placeholder="O que deseja ajustar?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "content":
            await interaction.response.send_modal(WelcomeVariantContentModal(self.panel))
            return
        if action == "settings":
            await interaction.response.send_modal(WelcomeVariantSettingsModal(self.panel))
            return
        if action == "visual":
            await interaction.response.send_modal(WelcomeVariantVisualModal(self.panel))
            return
        if action == "remove":
            cfg = deepcopy(self.panel.config)
            cfg["variants"] = [v for v in cfg.get("variants") or [] if str(v.get("id")) != str(self.panel.selected_variant_id)]
            await self.panel.save_config(cfg, "Variação removida.")
            self.panel.selected_variant_id = ""
            self.panel.screen = "variants"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeVariantCreateModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Nova variação")
        self.panel = panel
        self.name_input = discord.ui.TextInput(label="Nome da variação", placeholder="Ex.: Mensagem divertida", default=f"Variação {len(panel.config.get('variants') or []) + 1}", max_length=MAX_VARIANT_NAME, required=True)
        self.weight_input = discord.ui.TextInput(label="Chance / peso", placeholder="1, 2, 3...", default="1", max_length=3, required=True)
        self.add_item(self.name_input)
        self.add_item(self.weight_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            weight = max(1, min(100, int(str(self.weight_input.value or "1").strip())))
        except Exception:
            await interaction.response.send_message(view=_make_notice_view("Chance inválida", "Use um número entre 1 e 100.", ok=False), ephemeral=True)
            return
        cfg = deepcopy(self.panel.config)
        variants = [self.panel.cog._normalize_variant(v) for v in cfg.get("variants") or []]
        if len(variants) >= MAX_WELCOME_VARIANTS:
            await interaction.response.send_message(view=_make_notice_view("Limite atingido", f"Use até {MAX_WELCOME_VARIANTS} variações.", ok=False), ephemeral=True)
            return
        base_public = self.panel.cog._normalize_public_block(cfg.get("public"), default=DEFAULT_PUBLIC, allow_empty=True)
        base_embed = self.panel.cog._normalize_embed_config(cfg.get("embed"))
        variant = self.panel.cog._normalize_variant({
            "id": _new_variant_id(),
            "name": str(self.name_input.value or "Variação").strip() or "Variação",
            "weight": weight,
            "enabled": True,
            "public": base_public,
            "embed": base_embed,
            "style": "inherit",
            "accent_color": "",
            "accent_color_mode": "inherit",
            "media_url": "",
        })
        variants.append(variant)
        cfg["variants"] = variants[:MAX_WELCOME_VARIANTS]
        await self.panel.save_config(cfg, "Variação criada.")
        self.panel.selected_variant_id = str(variant.get("id"))
        self.panel.screen = "variant_detail"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeVariantSettingsModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Nome e chance")
        self.panel = panel
        variant = panel.cog._find_variant(panel.config, panel.selected_variant_id) or {}
        self.enabled_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.enabled_group = discord.ui.RadioGroup(required=True)
            enabled = bool(variant.get("enabled", True))
            self.enabled_group.add_option(label="Ativa", value="yes", default=enabled)
            self.enabled_group.add_option(label="Pausada", value="no", default=not enabled)
            self.add_item(discord.ui.Label(text="Status", component=self.enabled_group))
        self.name_input = discord.ui.TextInput(label="Nome da variação", default=str(variant.get("name") or "Variação")[:MAX_VARIANT_NAME], max_length=MAX_VARIANT_NAME, required=True)
        self.weight_input = discord.ui.TextInput(label="Chance / peso", placeholder="1, 2, 3...", default=str(int(variant.get("weight") or 1)), max_length=3, required=True)
        self.add_item(self.name_input)
        self.add_item(self.weight_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            weight = max(1, min(100, int(str(self.weight_input.value or "1").strip())))
        except Exception:
            await interaction.response.send_message(view=_make_notice_view("Chance inválida", "Use um número entre 1 e 100.", ok=False), ephemeral=True)
            return
        enabled = _modal_value(self.enabled_group, "yes") != "no" if self.enabled_group is not None else True
        await self.panel.update_variant(self.panel.selected_variant_id, {
            "name": str(self.name_input.value or "Variação").strip() or "Variação",
            "weight": weight,
            "enabled": enabled,
        }, "Variação atualizada.")
        self.panel.screen = "variant_detail"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeVariantContentModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        mode = str(panel.config.get("render_mode") or "components_v2")
        title = "Embed da variação" if mode == "embed" else "Mensagem da variação"
        super().__init__(title=title)
        self.panel = panel
        self.mode = mode
        variant = panel.cog._find_variant(panel.config, panel.selected_variant_id) or {}
        public = panel.cog._normalize_public_block(variant.get("public"), default=DEFAULT_PUBLIC, allow_empty=True)
        embed = panel.cog._normalize_embed_config(variant.get("embed"))
        if mode == "embed":
            self.content_input = discord.ui.TextInput(label="Mensagem acima", default=str(embed.get("content") or "")[:1200], style=discord.TextStyle.paragraph, max_length=1200, required=False)
            self.title_input = discord.ui.TextInput(label="Título do embed", placeholder="Deixe vazio para não mostrar título", default=str(embed.get("title") or "")[:256], max_length=256, required=False)
            self.description_input = discord.ui.TextInput(label="Descrição do embed", default=str(embed.get("description") or "")[:MAX_TEMPLATE_LENGTH], style=discord.TextStyle.paragraph, max_length=MAX_TEMPLATE_LENGTH, required=False)
            self.footer_input = discord.ui.TextInput(label="Footer do embed", default=str(embed.get("footer_text") or "")[:MAX_FOOTER_LENGTH], style=discord.TextStyle.paragraph, max_length=MAX_FOOTER_LENGTH, required=False)
            for item in (self.content_input, self.title_input, self.description_input, self.footer_input):
                self.add_item(item)
        else:
            self.title_input = discord.ui.TextInput(label="Título", default=str(public.get("title") or "")[:256], max_length=256, required=False)
            self.body_input = discord.ui.TextInput(label="Mensagem", default=str(public.get("body") or "")[:MAX_TEMPLATE_LENGTH], style=discord.TextStyle.paragraph, max_length=MAX_TEMPLATE_LENGTH, required=True)
            self.footer_input = discord.ui.TextInput(label="Texto final V2", default=str(public.get("footer") or "")[:MAX_FOOTER_LENGTH], style=discord.TextStyle.paragraph, max_length=MAX_FOOTER_LENGTH, required=False)
            self.add_item(self.title_input)
            self.add_item(self.body_input)
            if mode == "components_v2":
                self.add_item(self.footer_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.mode == "embed":
            await self.panel.update_variant(self.panel.selected_variant_id, {"embed": {
                "content": str(self.content_input.value or "").strip(),
                "title": str(self.title_input.value or "").strip(),
                "description": str(self.description_input.value or "").strip(),
                "footer_text": str(self.footer_input.value or "").strip(),
            }}, "Mensagem da variação salva.")
        else:
            await self.panel.update_variant(self.panel.selected_variant_id, {"public": {
                "title": str(self.title_input.value or "").strip(),
                "body": str(self.body_input.value or "").strip(),
                "footer": str(getattr(self, "footer_input", None).value or "").strip() if hasattr(self, "footer_input") else "",
            }}, "Mensagem da variação salva.")
        self.panel.screen = "variant_detail"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeVariantVisualModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Visual da variação")
        self.panel = panel
        variant = panel.cog._find_variant(panel.config, panel.selected_variant_id) or {}
        self.style_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.style_group = discord.ui.RadioGroup(required=True)
            current_style = str(variant.get("style") or "inherit")
            self.style_group.add_option(label="Usar visual padrão", value="inherit", default=current_style == "inherit")
            for key, label in STYLE_LABELS.items():
                self.style_group.add_option(label=label, value=key, default=current_style == key)
            self.add_item(discord.ui.Label(text="Estilo", component=self.style_group))
        self.color_mode_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.color_mode_group = discord.ui.RadioGroup(required=True)
            current_color_mode = str(variant.get("accent_color_mode") or "inherit")
            self.color_mode_group.add_option(label="Usar cor padrão", value="inherit", default=current_color_mode == "inherit")
            for key, label in COLOR_MODE_LABELS.items():
                self.color_mode_group.add_option(label=label, value=key, default=current_color_mode == key)
            self.add_item(discord.ui.Label(text="Cor", component=self.color_mode_group))
        self.media_mode_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.media_mode_group = discord.ui.RadioGroup(required=True)
            current_media_mode = _media_mode(variant.get("media_mode"))
            for key, label in MEDIA_MODE_LABELS.items():
                self.media_mode_group.add_option(label=label, value=key, default=current_media_mode == key)
            self.add_item(discord.ui.Label(text="Imagem/banner", component=self.media_mode_group))
        self.color_input = discord.ui.TextInput(label="Cor fixa em hex", placeholder="#5865F2", default=str(variant.get("accent_color") or "")[:7], max_length=7, required=False)
        self.image_input = discord.ui.TextInput(label="Imagem/banner opcional", placeholder="https://exemplo.com/imagem.png", default=str(variant.get("media_url") or "")[:1000], max_length=1000, required=False)
        self.add_item(self.color_input)
        self.add_item(self.image_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_hex = str(self.color_input.value or "").strip()
        raw_url = str(self.image_input.value or "").strip()
        if raw_hex and not HEX_RE.fullmatch(raw_hex):
            await interaction.response.send_message(view=_make_notice_view("Cor inválida", "Use uma cor no formato #5865F2.", ok=False), ephemeral=True)
            return
        if raw_url and not URL_RE.fullmatch(raw_url):
            await interaction.response.send_message(view=_make_notice_view("Imagem inválida", "Use um link começando com http:// ou https://.", ok=False), ephemeral=True)
            return
        style = _modal_value(self.style_group, "inherit") if self.style_group is not None else "inherit"
        if style not in {"inherit", *STYLE_LABELS.keys()}:
            style = "inherit"
        color_mode = _modal_value(self.color_mode_group, "inherit") if self.color_mode_group is not None else "inherit"
        if color_mode not in {"inherit", *COLOR_MODE_LABELS.keys()}:
            color_mode = "inherit"
        await self.panel.update_variant(self.panel.selected_variant_id, {
            "style": style,
            "accent_color": _parse_hex(raw_hex) if raw_hex else "",
            "accent_color_mode": color_mode,
            "media_url": _clean_url(raw_url),
            "media_mode": _media_mode(_modal_value(self.media_mode_group, "custom") if self.media_mode_group is not None else "custom"),
        }, "Visual da variação salvo.")
        self.panel.screen = "variant_detail"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeModeModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Modo da mensagem")
        self.panel = panel
        self.mode_group = None
        current_mode = str(panel.config.get("render_mode") or "components_v2")
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.mode_group = discord.ui.RadioGroup(required=True)
            for key, label in RENDER_MODE_LABELS.items():
                self.mode_group.add_option(
                    label=label,
                    value=key,
                    description=RENDER_MODE_DESCRIPTIONS.get(key, ""),
                    default=current_mode == key,
                )
            self.add_item(discord.ui.Label(text="Como a mensagem deve aparecer", component=self.mode_group))
        else:
            self.mode_input = discord.ui.TextInput(
                label="Modo: components_v2, embed ou normal",
                default=current_mode,
                max_length=20,
                required=True,
            )
            self.add_item(self.mode_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.mode_group is not None:
            mode = _modal_value(self.mode_group, str(self.panel.config.get("render_mode") or "components_v2"))
        else:
            mode = str(self.mode_input.value or "components_v2").strip().lower()
        if mode not in RENDER_MODE_LABELS:
            mode = str(self.panel.config.get("render_mode") or "components_v2")
        cfg = self.panel.cog._switch_public_mode(self.panel.config, mode)
        await self.panel.save_config(cfg, f"Modo ajustado para **{RENDER_MODE_LABELS.get(mode, 'Components V2')}**.")
        self.panel.go_to("message")
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)

class WelcomeSettingsModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Configurações")
        self.panel = panel
        self.status_group = None
        self.flags_group = None
        enabled = bool(panel.config.get("enabled", False))
        delete_on_leave = bool(panel.config.get("delete_on_leave_enabled", False))
        if _advanced_modal_supported("Label", "RadioGroup", "CheckboxGroup"):
            self.status_group = discord.ui.RadioGroup(required=True)
            self.status_group.add_option(label="Boas-vindas ligadas", value="on", description="Enviar quando alguém entrar", default=enabled)
            self.status_group.add_option(label="Boas-vindas desligadas", value="off", description="Pausar as mensagens", default=not enabled)
            self.flags_group = discord.ui.CheckboxGroup(min_values=0, max_values=1, required=False)
            self.flags_group.add_option(
                label="Apagar se sair em até 24 horas",
                value="delete_on_leave",
                description="Só apaga se o membro sair nesse período",
                default=delete_on_leave,
            )
            self.add_item(discord.ui.Label(text="Status", component=self.status_group))
            self.add_item(discord.ui.Label(text="Opções", component=self.flags_group))
        else:
            self.status_input = discord.ui.TextInput(label="Status: ligado ou desligado", default="ligado" if enabled else "desligado", max_length=20, required=True)
            self.delete_input = discord.ui.TextInput(label="Apagar se sair em até 24 horas? sim ou não", default="sim" if delete_on_leave else "não", max_length=10, required=True)
            self.add_item(self.status_input)
            self.add_item(self.delete_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = deepcopy(self.panel.config)
        if self.status_group is not None:
            enabled = _modal_value(self.status_group, "off") == "on"
            delete_on_leave = "delete_on_leave" in set(_modal_values(self.flags_group))
        else:
            enabled = str(self.status_input.value or "").strip().lower() in {"ligado", "on", "sim", "s", "true", "1"}
            delete_on_leave = str(self.delete_input.value or "").strip().lower() in {"sim", "s", "yes", "y", "true", "1", "ligado", "on"}
        if enabled and not int(cfg.get("channel_id") or 0):
            cfg["enabled"] = False
            cfg["delete_on_leave_enabled"] = delete_on_leave
            notice = "Escolha um canal antes de ligar."
        else:
            cfg["enabled"] = enabled
            cfg["delete_on_leave_enabled"] = delete_on_leave
            leave = "apaga se o membro sair em até 24 horas" if delete_on_leave else "mantém a mensagem ao sair"
            notice = f"Configurações salvas: boas-vindas {'ligadas' if enabled else 'desligadas'} · {leave}."
        await self.panel.save_config(cfg, notice)
        if cfg.get("enabled"):
            asyncio.create_task(self.panel.cog._refresh_invite_cache_for_guild(interaction.guild, cfg))
        self.panel.screen = "home"
        self.panel.screen_history.clear()
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeMessageModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Mensagem Components V2")
        self.panel = panel
        public = dict(panel.config.get("public") or {})
        self.title_input = discord.ui.TextInput(
            label="Título",
            default=str(public.get("title") or DEFAULT_PUBLIC["title"])[:256],
            max_length=256,
            required=True,
        )
        self.body_input = discord.ui.TextInput(
            label="Mensagem",
            style=discord.TextStyle.paragraph,
            default=str(public.get("body") or DEFAULT_PUBLIC["body"])[:MAX_TEMPLATE_LENGTH],
            max_length=MAX_TEMPLATE_LENGTH,
            required=True,
        )
        self.footer_input = discord.ui.TextInput(
            label="Texto final da mensagem V2",
            placeholder="Opcional. Aparece no final do container.",
            style=discord.TextStyle.paragraph,
            default=str(public.get("footer") or "")[:MAX_FOOTER_LENGTH],
            max_length=MAX_FOOTER_LENGTH,
            required=False,
        )
        self.add_item(self.title_input)
        self.add_item(self.body_input)
        self.add_item(self.footer_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = deepcopy(self.panel.config)
        cfg["public"] = {
            "title": str(self.title_input.value or "").strip() or DEFAULT_PUBLIC["title"],
            "body": str(self.body_input.value or "").strip() or DEFAULT_PUBLIC["body"],
            "footer": str(self.footer_input.value or "").strip(),
        }
        await self.panel.save_config(cfg, "Mensagem V2 atualizada.")
        self.panel.screen = "message"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeNormalMessageModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Mensagem normal")
        self.panel = panel
        public = dict(panel.config.get("public") or {})
        current = "\n\n".join(part for part in (
            str(public.get("title") or "").strip(),
            str(public.get("body") or "").strip(),
        ) if part).strip()
        if not current:
            current = DEFAULT_PUBLIC["body"]
        self.content_input = discord.ui.TextInput(
            label="Mensagem",
            placeholder="Texto que será enviado no canal.",
            style=discord.TextStyle.paragraph,
            default=current[:1900],
            max_length=1900,
            required=True,
        )
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = deepcopy(self.panel.config)
        cfg["public"] = {
            "title": "",
            "body": str(self.content_input.value or "").strip() or DEFAULT_PUBLIC["body"],
            "footer": "",
        }
        await self.panel.save_config(cfg, "Mensagem normal atualizada.")
        self.panel.screen = "message"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeEmbedContentModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Mensagem acima")
        self.panel = panel
        embed = panel.cog._normalize_embed_config(panel.config.get("embed"))
        self.content_input = discord.ui.TextInput(
            label="Texto acima do embed",
            placeholder="Ex.: {membro_mencao} chegou no servidor 👋",
            style=discord.TextStyle.paragraph,
            default=str(embed.get("content") or "")[:1800],
            max_length=1800,
            required=False,
        )
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = deepcopy(self.panel.config)
        embed = self.panel.cog._normalize_embed_config(cfg.get("embed"))
        embed["content"] = str(self.content_input.value or "").strip()
        cfg["embed"] = embed
        await self.panel.save_config(cfg, "Mensagem acima salva.")
        self.panel.screen = "embed_editor"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeEmbedTextModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Título e descrição")
        self.panel = panel
        embed = panel.cog._normalize_embed_config(panel.config.get("embed"))
        self.title_input = discord.ui.TextInput(label="Título do embed", placeholder="Deixe vazio para não mostrar título", default=str(embed.get("title") or "")[:256], max_length=256, required=False)
        self.description_input = discord.ui.TextInput(label="Descrição do embed", placeholder="Vazio usa a mensagem principal", style=discord.TextStyle.paragraph, default=str(embed.get("description") or "")[:MAX_TEMPLATE_LENGTH], max_length=MAX_TEMPLATE_LENGTH, required=False)
        self.title_url_input = discord.ui.TextInput(label="Link do título opcional", placeholder="https://exemplo.com", default=str(embed.get("title_url") or "")[:1000], max_length=1000, required=False)
        self.color_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.color_group = discord.ui.RadioGroup(required=True)
            current_color_mode = str(embed.get("color_mode") or "fixed")
            for key, label in COLOR_MODE_LABELS.items():
                self.color_group.add_option(label=label, value=key, default=current_color_mode == key)
            self.add_item(discord.ui.Label(text="Cor do embed", component=self.color_group))
        self.color_input = discord.ui.TextInput(label="Cor do embed em hex", placeholder="Exemplo: #5865F2", default=str(embed.get("color") or "")[:7], max_length=7, required=False)
        self.add_item(self.title_input)
        self.add_item(self.description_input)
        self.add_item(self.title_url_input)
        self.add_item(self.color_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_title_url = str(self.title_url_input.value or "").strip()
        raw_color = str(self.color_input.value or "").strip()
        if raw_title_url and not URL_RE.fullmatch(raw_title_url):
            await interaction.response.send_message(view=_make_notice_view("Link inválido", "Use um link começando com http:// ou https://.", ok=False), ephemeral=True)
            return
        if raw_color and not HEX_RE.fullmatch(raw_color):
            await interaction.response.send_message(view=_make_notice_view("Cor inválida", "Use uma cor em hex, como #5865F2.", ok=False), ephemeral=True)
            return
        cfg = deepcopy(self.panel.config)
        embed = self.panel.cog._normalize_embed_config(cfg.get("embed"))
        embed["title"] = str(self.title_input.value or "").strip()
        embed["description"] = str(self.description_input.value or "").strip()
        embed["title_url"] = _clean_url(raw_title_url)
        color_mode = _modal_value(self.color_group, "fixed") if self.color_group is not None else str(embed.get("color_mode") or "fixed")
        if color_mode not in COLOR_MODE_LABELS:
            color_mode = "fixed"
        embed["color_mode"] = color_mode
        embed["color"] = _parse_hex(raw_color) if raw_color else ""
        cfg["embed"] = embed
        await self.panel.save_config(cfg, "Título e descrição salvos.")
        self.panel.screen = "embed_editor"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeEmbedAuthorModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Author do embed")
        self.panel = panel
        embed = panel.cog._normalize_embed_config(panel.config.get("embed"))
        self.icon_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.icon_group = discord.ui.RadioGroup(required=True)
            current = _image_mode(embed.get("author_icon_mode"), fallback="none")
            for key, label in EMBED_IMAGE_MODE_LABELS.items():
                self.icon_group.add_option(label=label, value=key, default=current == key)
            self.add_item(discord.ui.Label(text="Ícone do author", component=self.icon_group))
        else:
            self.icon_mode_input = discord.ui.TextInput(label="Ícone: none, member, inviter, server, bot ou custom", default=str(embed.get("author_icon_mode") or "none")[:20], max_length=20, required=True)
            self.add_item(self.icon_mode_input)
        self.name_input = discord.ui.TextInput(label="Nome do author", placeholder="Ex.: {membro}", default=str(embed.get("author_name") or "")[:256], max_length=256, required=False)
        self.icon_url_input = discord.ui.TextInput(label="Ícone por link opcional", placeholder="https://exemplo.com/avatar.png", default=str(embed.get("author_icon_url") or "")[:1000], max_length=1000, required=False)
        self.author_url_input = discord.ui.TextInput(label="Link do author opcional", placeholder="https://exemplo.com", default=str(embed.get("author_url") or "")[:1000], max_length=1000, required=False)
        self.add_item(self.name_input)
        self.add_item(self.icon_url_input)
        self.add_item(self.author_url_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_icon_url = str(self.icon_url_input.value or "").strip()
        raw_author_url = str(self.author_url_input.value or "").strip()
        if raw_icon_url and not URL_RE.fullmatch(raw_icon_url):
            await interaction.response.send_message(view=_make_notice_view("Ícone inválido", "Use um link começando com http:// ou https://.", ok=False), ephemeral=True)
            return
        if raw_author_url and not URL_RE.fullmatch(raw_author_url):
            await interaction.response.send_message(view=_make_notice_view("Link inválido", "Use um link começando com http:// ou https://.", ok=False), ephemeral=True)
            return
        mode = _modal_value(self.icon_group, "none") if self.icon_group is not None else str(self.icon_mode_input.value or "none").strip().lower()
        mode = _image_mode(mode, fallback="none")
        if mode == "custom" and not raw_icon_url:
            await interaction.response.send_message(view=_make_notice_view("Ícone incompleto", "Coloque um link quando usar ícone personalizado.", ok=False), ephemeral=True)
            return
        cfg = deepcopy(self.panel.config)
        embed = self.panel.cog._normalize_embed_config(cfg.get("embed"))
        embed["author_name"] = str(self.name_input.value or "").strip()
        embed["author_icon_mode"] = mode
        embed["author_icon_url"] = _clean_url(raw_icon_url)
        embed["author_url"] = _clean_url(raw_author_url)
        cfg["embed"] = embed
        await self.panel.save_config(cfg, "Author do embed salvo.")
        self.panel.screen = "embed_editor"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeEmbedImagesModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Imagens do embed")
        self.panel = panel
        embed = panel.cog._normalize_embed_config(panel.config.get("embed"))
        self.thumbnail_group = None
        self.image_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.thumbnail_group = discord.ui.RadioGroup(required=True)
            current_thumb = _image_mode(embed.get("thumbnail_mode"), fallback="none")
            for key, label in EMBED_IMAGE_MODE_LABELS.items():
                self.thumbnail_group.add_option(label=label, value=key, default=current_thumb == key)
            self.image_group = discord.ui.RadioGroup(required=True)
            current_image = _image_mode(embed.get("image_mode"), fallback="custom")
            for key, label in EMBED_MAIN_IMAGE_MODE_LABELS.items():
                self.image_group.add_option(label=label, value=key, default=current_image == key)
            self.add_item(discord.ui.Label(text="Thumbnail / imagem lateral", component=self.thumbnail_group))
            self.add_item(discord.ui.Label(text="Imagem principal / banner", component=self.image_group))
        else:
            self.thumbnail_mode_input = discord.ui.TextInput(label="Thumbnail: none, member, inviter, server, bot ou custom", default=str(embed.get("thumbnail_mode") or "none")[:20], max_length=20, required=True)
            self.image_mode_input = discord.ui.TextInput(label="Imagem: none, member, inviter, server, bot, custom ou avatar_stars", default=str(embed.get("image_mode") or "custom")[:20], max_length=20, required=True)
            self.add_item(self.thumbnail_mode_input)
            self.add_item(self.image_mode_input)
        self.thumbnail_url_input = discord.ui.TextInput(label="Thumbnail por link opcional", placeholder="https://exemplo.com/avatar.png", default=str(embed.get("thumbnail_url") or "")[:1000], max_length=1000, required=False)
        self.image_url_input = discord.ui.TextInput(label="Imagem por link opcional", placeholder="https://exemplo.com/banner.png", default=str(embed.get("image_url") or panel.config.get("media_url") or "")[:1000], max_length=1000, required=False)
        self.add_item(self.thumbnail_url_input)
        self.add_item(self.image_url_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_thumb = str(self.thumbnail_url_input.value or "").strip()
        raw_image = str(self.image_url_input.value or "").strip()
        for label, raw in (("Thumbnail", raw_thumb), ("Imagem", raw_image)):
            if raw and not URL_RE.fullmatch(raw):
                await interaction.response.send_message(view=_make_notice_view(f"{label} inválida", "Use um link começando com http:// ou https://.", ok=False), ephemeral=True)
                return
        thumb_mode = _modal_value(self.thumbnail_group, "none") if self.thumbnail_group is not None else str(self.thumbnail_mode_input.value or "none").strip().lower()
        image_mode = _modal_value(self.image_group, "custom") if self.image_group is not None else str(self.image_mode_input.value or "custom").strip().lower()
        thumb_mode = _image_mode(thumb_mode, fallback="none")
        image_mode = _image_mode(image_mode, fallback="custom")
        if thumb_mode == "custom" and not raw_thumb:
            await interaction.response.send_message(view=_make_notice_view("Thumbnail incompleta", "Coloque um link ou escolha outra origem.", ok=False), ephemeral=True)
            return
        if image_mode == "custom" and not raw_image:
            await interaction.response.send_message(view=_make_notice_view("Imagem incompleta", "Coloque um link ou escolha outra origem.", ok=False), ephemeral=True)
            return
        cfg = deepcopy(self.panel.config)
        embed = self.panel.cog._normalize_embed_config(cfg.get("embed"))
        embed["thumbnail_mode"] = thumb_mode
        embed["thumbnail_url"] = _clean_url(raw_thumb)
        embed["image_mode"] = image_mode
        embed["image_url"] = _clean_url(raw_image)
        cfg["embed"] = embed
        await self.panel.save_config(cfg, "Imagens do embed salvas.")
        self.panel.screen = "embed_editor"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeEmbedFooterModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Footer do embed")
        self.panel = panel
        embed = panel.cog._normalize_embed_config(panel.config.get("embed"))
        self.icon_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.icon_group = discord.ui.RadioGroup(required=True)
            current = _image_mode(embed.get("footer_icon_mode"), fallback="none")
            for key, label in EMBED_IMAGE_MODE_LABELS.items():
                self.icon_group.add_option(label=label, value=key, default=current == key)
            self.add_item(discord.ui.Label(text="Ícone do footer", component=self.icon_group))
        else:
            self.icon_mode_input = discord.ui.TextInput(label="Ícone: none, member, inviter, server, bot ou custom", default=str(embed.get("footer_icon_mode") or "none")[:20], max_length=20, required=True)
            self.add_item(self.icon_mode_input)
        self.footer_text_input = discord.ui.TextInput(label="Texto do footer", placeholder="Ex.: ID do usuário: {membro_id}", style=discord.TextStyle.paragraph, default=str(embed.get("footer_text") or "")[:2048], max_length=2048, required=False)
        self.footer_icon_url_input = discord.ui.TextInput(label="Ícone por link opcional", placeholder="https://exemplo.com/icone.png", default=str(embed.get("footer_icon_url") or "")[:1000], max_length=1000, required=False)
        self.add_item(self.footer_text_input)
        self.add_item(self.footer_icon_url_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_icon = str(self.footer_icon_url_input.value or "").strip()
        if raw_icon and not URL_RE.fullmatch(raw_icon):
            await interaction.response.send_message(view=_make_notice_view("Ícone inválido", "Use um link começando com http:// ou https://.", ok=False), ephemeral=True)
            return
        mode = _modal_value(self.icon_group, "none") if self.icon_group is not None else str(self.icon_mode_input.value or "none").strip().lower()
        mode = _image_mode(mode, fallback="none")
        if mode == "custom" and not raw_icon:
            await interaction.response.send_message(view=_make_notice_view("Ícone incompleto", "Coloque um link quando usar ícone personalizado.", ok=False), ephemeral=True)
            return
        cfg = deepcopy(self.panel.config)
        embed = self.panel.cog._normalize_embed_config(cfg.get("embed"))
        embed["footer_text"] = str(self.footer_text_input.value or "").strip()
        embed["footer_icon_mode"] = mode
        embed["footer_icon_url"] = _clean_url(raw_icon)
        cfg["embed"] = embed
        await self.panel.save_config(cfg, "Footer do embed salvo.")
        self.panel.screen = "embed_editor"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeDmModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Mensagem privada")
        self.panel = panel
        dm = dict(panel.config.get("dm") or {})
        self.title_input = discord.ui.TextInput(
            label="Título",
            default=str(dm.get("title") or DEFAULT_DM["title"])[:256],
            max_length=256,
            required=True,
        )
        self.body_input = discord.ui.TextInput(
            label="Mensagem",
            style=discord.TextStyle.paragraph,
            default=str(dm.get("body") or DEFAULT_DM["body"])[:MAX_TEMPLATE_LENGTH],
            max_length=MAX_TEMPLATE_LENGTH,
            required=True,
        )
        self.footer_input = discord.ui.TextInput(
            label="Texto final opcional",
            style=discord.TextStyle.paragraph,
            default=str(dm.get("footer") or "")[:MAX_FOOTER_LENGTH],
            max_length=MAX_FOOTER_LENGTH,
            required=False,
        )
        self.add_item(self.title_input)
        self.add_item(self.body_input)
        self.add_item(self.footer_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = deepcopy(self.panel.config)
        cfg["dm"] = {
            "title": str(self.title_input.value or "").strip() or DEFAULT_DM["title"],
            "body": str(self.body_input.value or "").strip() or DEFAULT_DM["body"],
            "footer": str(self.footer_input.value or "").strip(),
        }
        await self.panel.save_config(cfg, "Mensagem privada atualizada.")
        self.panel.screen = "dm"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)



class WelcomeDmOptionsModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Configurar privado")
        self.panel = panel
        cfg = panel.config
        self.mode_group = discord.ui.RadioGroup(required=True)
        current_mode = str(cfg.get("dm_render_mode") or cfg.get("render_mode") or "components_v2")
        for key, label in RENDER_MODE_LABELS.items():
            self.mode_group.add_option(label=label, value=key, description=RENDER_MODE_DESCRIPTIONS[key], default=current_mode == key)
        self.flags_group = discord.ui.CheckboxGroup(required=False, min_values=0, max_values=2)
        self.flags_group.add_option(label="Enviar mensagem privada", value="dm_enabled", description="Manda no privado quando alguém entrar.", default=bool(cfg.get("dm_enabled", False)))
        self.flags_group.add_option(label="Usar o mesmo modo da pública", value="same_mode", description="A DM acompanha o modo público.", default=str(cfg.get("dm_render_mode") or "") == str(cfg.get("render_mode") or "components_v2"))
        self.add_item(discord.ui.Label(text="Modo da mensagem privada", component=self.mode_group))
        self.add_item(discord.ui.Label(text="Opções", description="Marque o que fica ativo.", component=self.flags_group))

    async def on_submit(self, interaction: discord.Interaction):
        selected = set(_modal_values(self.flags_group))
        cfg = deepcopy(self.panel.config)
        cfg["dm_enabled"] = "dm_enabled" in selected
        if "same_mode" in selected:
            cfg["dm_render_mode"] = str(cfg.get("render_mode") or "components_v2")
        else:
            cfg["dm_render_mode"] = _modal_value(self.mode_group, "components_v2")
        await self.panel.save_config(cfg, "Mensagem privada configurada.")
        self.panel.screen = "dm"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeVisualModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Visual da mensagem")
        self.panel = panel
        self.style_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.style_group = discord.ui.RadioGroup(required=True)
            current_style = str(panel.config.get("style") or "complete")
            for key, label in STYLE_LABELS.items():
                self.style_group.add_option(label=label, value=key, description="Escolha este estilo visual.", default=current_style == key)
            self.add_item(discord.ui.Label(text="Estilo", component=self.style_group))
        else:
            self.style_input = discord.ui.TextInput(
                label="Estilo: complete, simple ou compact",
                default=str(panel.config.get("style") or "complete")[:20],
                max_length=20,
                required=True,
            )
            self.add_item(self.style_input)
        self.color_mode_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.color_mode_group = discord.ui.RadioGroup(required=True)
            current_color_mode = str(panel.config.get("accent_color_mode") or "fixed")
            for key, label in COLOR_MODE_LABELS.items():
                self.color_mode_group.add_option(label=label, value=key, default=current_color_mode == key)
            self.add_item(discord.ui.Label(text="Cor da mensagem", component=self.color_mode_group))
        self.media_mode_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.media_mode_group = discord.ui.RadioGroup(required=True)
            current_media_mode = _media_mode(panel.config.get("media_mode"))
            for key, label in MEDIA_MODE_LABELS.items():
                self.media_mode_group.add_option(label=label, value=key, default=current_media_mode == key)
            self.add_item(discord.ui.Label(text="Imagem/banner", component=self.media_mode_group))
        self.accent_input = discord.ui.TextInput(
            label="Cor fixa em HEX",
            placeholder="#5865F2",
            default=_parse_hex(panel.config.get("accent_color")),
            max_length=7,
            required=True,
        )
        self.image_input = discord.ui.TextInput(
            label="Imagem ou banner opcional",
            placeholder="https://exemplo.com/imagem.png",
            default=str(panel.config.get("media_url") or "")[:1000],
            max_length=1000,
            required=False,
        )
        self.add_item(self.accent_input)
        self.add_item(self.image_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_hex = str(self.accent_input.value or "").strip()
        if not HEX_RE.fullmatch(raw_hex):
            await interaction.response.send_message(
                view=_make_notice_view("Cor inválida", "Use uma cor no formato #5865F2.", ok=False),
                ephemeral=True,
            )
            return
        raw_url = str(self.image_input.value or "").strip()
        if raw_url and not URL_RE.fullmatch(raw_url):
            await interaction.response.send_message(
                view=_make_notice_view("Imagem inválida", "Use um link começando com http:// ou https://.", ok=False),
                ephemeral=True,
            )
            return
        if self.style_group is not None:
            style = _modal_value(self.style_group, "complete")
        else:
            style = str(getattr(self, "style_input").value or "complete").strip().lower()
        if style not in STYLE_LABELS:
            style = "complete"
        cfg = deepcopy(self.panel.config)
        cfg["style"] = style
        color_mode = _modal_value(self.color_mode_group, "fixed") if self.color_mode_group is not None else str(cfg.get("accent_color_mode") or "fixed")
        if color_mode not in COLOR_MODE_LABELS:
            color_mode = "fixed"
        cfg["accent_color_mode"] = color_mode
        cfg["accent_color"] = _parse_hex(raw_hex)
        media_mode = _modal_value(self.media_mode_group, "custom") if self.media_mode_group is not None else str(cfg.get("media_mode") or "custom")
        cfg["media_mode"] = _media_mode(media_mode)
        cfg["media_url"] = _clean_url(raw_url)
        await self.panel.save_config(cfg, "Visual atualizado.")
        self.panel.screen = "visual"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeDecorativeEmojiModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Emojis decorativos")
        self.panel = panel
        self.limit = panel.cog._decorative_emoji_limit_for_guild_id(panel.guild_id)
        self.flags_group = discord.ui.CheckboxGroup(required=False, min_values=0, max_values=1)
        self.flags_group.add_option(
            label=f"Colorir até {self.limit} emojis da mensagem",
            value="decorative_emoji_enabled",
            description="Se não conseguir colorir, mantém o emoji original.",
            default=bool(panel.config.get("decorative_emoji_enabled", False)),
        )
        self.add_item(discord.ui.Label(
            text="Emojis decorativos",
            description=f"Neste servidor: até {self.limit}. Se falhar, os emojis originais continuam.",
            component=self.flags_group,
        ))

    async def on_submit(self, interaction: discord.Interaction):
        selected = set(_modal_values(self.flags_group))
        cfg = deepcopy(self.panel.config)
        cfg["decorative_emoji_enabled"] = "decorative_emoji_enabled" in selected
        await self.panel.save_config(
            cfg,
            "Emojis decorativos ligados." if cfg["decorative_emoji_enabled"] else "Emojis decorativos desligados.",
        )
        self.panel.screen = "visual"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeWebhookSetupModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Configurar webhook")
        self.panel = panel
        webhook = dict(panel.config.get("webhook") or {})
        self.send_select = None
        if hasattr(discord.ui, "Label"):
            current = "existing" if webhook.get("enabled") and webhook.get("webhook_id") else "bot"
            options = [
                discord.SelectOption(label="Enviar pelo bot", value="bot", emoji="🤖", description="Usa o próprio bot", default=current == "bot"),
                discord.SelectOption(label="Criar webhook no canal salvo", value="create", emoji="🪝", description="Cria um envio personalizado", default=current == "create"),
                discord.SelectOption(label="Escolher webhook existente", value="existing", emoji="📌", description="Mostra webhooks do canal salvo", default=current == "existing"),
            ]
            self.send_select = discord.ui.Select(placeholder="Como deseja enviar?", min_values=1, max_values=1, options=options)
            self.add_item(discord.ui.Label(text="Envio das boas-vindas", component=self.send_select))
        else:
            self.mode_input = discord.ui.TextInput(label="Envio: bot, create ou existing", default="bot", max_length=20, required=True)
            self.add_item(self.mode_input)

    async def on_submit(self, interaction: discord.Interaction):
        action = _modal_value(self.send_select, "bot") if self.send_select is not None else str(self.mode_input.value or "bot").strip().lower()
        cfg = deepcopy(self.panel.config)
        webhook_cfg = dict(cfg.get("webhook") or {})
        if action == "bot":
            webhook_cfg["enabled"] = False
            cfg["webhook"] = webhook_cfg
            await self.panel.save_config(cfg, "Envio pelo bot ativado.")
            self.panel.screen = "webhook"
            self.panel._rebuild()
            await interaction.response.edit_message(view=self.panel)
            return
        if action == "existing":
            await self.panel.load_webhooks(interaction.guild)
            self.panel.go_to("webhook_existing")
            self.panel.notice = "Escolha um webhook da lista."
            self.panel._rebuild()
            await interaction.response.edit_message(view=self.panel)
            return
        if action != "create":
            self.panel.notice = "Escolha uma opção válida."
            self.panel.screen = "webhook"
            self.panel._rebuild()
            await interaction.response.edit_message(view=self.panel)
            return
        channel = await self.panel.cog._configured_channel(interaction.guild, cfg)
        if channel is None:
            self.panel.notice = "Escolha um canal antes de criar o webhook."
        else:
            webhook = await self.panel.cog._create_or_get_welcome_webhook(channel, webhook_cfg)
            if webhook is None:
                self.panel.notice = "Não consegui criar o webhook nesse canal. Veja se posso gerenciar webhooks."
            else:
                webhook_cfg.update({
                    "enabled": True,
                    "channel_id": int(getattr(channel, "id", 0) or 0),
                    "webhook_id": int(getattr(webhook, "id", 0) or 0),
                    "webhook_token": str(getattr(webhook, "token", None) or webhook_cfg.get("webhook_token") or ""),
                })
                cfg["webhook"] = webhook_cfg
                await self.panel.save_config(cfg, "Webhook pronto para as boas-vindas.")
        self.panel.screen = "webhook"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class WelcomeWebhookAppearanceModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Aparência do envio")
        self.panel = panel
        webhook = dict(panel.config.get("webhook") or {})
        self.name_group = None
        self.avatar_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.name_group = discord.ui.RadioGroup(required=True)
            current_name = str(webhook.get("name_mode") or "fixed")
            for key, label in WEBHOOK_NAME_LABELS.items():
                self.name_group.add_option(label=label, value=key, default=current_name == key)
            self.avatar_group = discord.ui.RadioGroup(required=True)
            current_avatar = str(webhook.get("avatar_mode") or "server")
            for key, label in WEBHOOK_AVATAR_LABELS.items():
                self.avatar_group.add_option(label=label, value=key, default=current_avatar == key)
            self.add_item(discord.ui.Label(text="Nome usado no envio", component=self.name_group))
            self.add_item(discord.ui.Label(text="Avatar usado no envio", component=self.avatar_group))
        else:
            self.name_mode_input = discord.ui.TextInput(label="Nome: fixed, server, member ou inviter", default=str(webhook.get("name_mode") or "fixed")[:20], max_length=20, required=True)
            self.avatar_mode_input = discord.ui.TextInput(label="Avatar: server, member, inviter ou custom", default=str(webhook.get("avatar_mode") or "server")[:20], max_length=20, required=True)
            self.add_item(self.name_mode_input)
            self.add_item(self.avatar_mode_input)
        self.name_input = discord.ui.TextInput(
            label="Nome personalizado",
            placeholder="Boas-vindas",
            default=str(webhook.get("name") or DEFAULT_WEBHOOK_NAME)[:80],
            max_length=80,
            required=True,
        )
        self.avatar_input = discord.ui.TextInput(
            label="Avatar por link opcional",
            placeholder="https://exemplo.com/avatar.png",
            default=str(webhook.get("avatar_url") or "")[:1000],
            max_length=1000,
            required=False,
        )
        self.add_item(self.name_input)
        self.add_item(self.avatar_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_url = str(self.avatar_input.value or "").strip()
        if raw_url and not URL_RE.fullmatch(raw_url):
            await interaction.response.send_message(
                view=_make_notice_view("Avatar inválido", "Use um link começando com http:// ou https://.", ok=False),
                ephemeral=True,
            )
            return
        if self.name_group is not None:
            name_mode = _modal_value(self.name_group, "fixed")
            avatar_mode = _modal_value(self.avatar_group, "server")
        else:
            name_mode = str(self.name_mode_input.value or "fixed").strip().lower()
            avatar_mode = str(self.avatar_mode_input.value or "server").strip().lower()
        if name_mode not in WEBHOOK_NAME_LABELS:
            name_mode = "fixed"
        if avatar_mode not in WEBHOOK_AVATAR_LABELS:
            avatar_mode = "server"
        if avatar_mode == "custom" and not raw_url:
            await interaction.response.send_message(
                view=_make_notice_view("Avatar incompleto", "Escolha um link quando usar avatar personalizado.", ok=False),
                ephemeral=True,
            )
            return
        avatar_url = _clean_url(raw_url)
        cfg = deepcopy(self.panel.config)
        webhook = dict(cfg.get("webhook") or {})
        webhook["name"] = _safe_webhook_name(self.name_input.value)
        webhook["name_mode"] = name_mode
        webhook["avatar_mode"] = avatar_mode
        webhook["avatar_url"] = avatar_url
        cfg["webhook"] = webhook
        await self.panel.save_config(cfg, "Aparência do envio salva.")
        self.panel.screen = "webhook"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)



class WelcomeWebhookModal(WelcomeWebhookAppearanceModal):
    pass


class WelcomeQuickOptionsModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Ajuste rápido")
        self.panel = panel
        cfg = panel.config
        self.mode_group = discord.ui.RadioGroup(required=True)
        current_mode = str(cfg.get("render_mode") or "components_v2")
        for key, label in RENDER_MODE_LABELS.items():
            self.mode_group.add_option(label=label, value=key, description=RENDER_MODE_DESCRIPTIONS[key], default=current_mode == key)
        self.flags_group = discord.ui.CheckboxGroup(required=False, min_values=0, max_values=2)
        self.flags_group.add_option(label="Boas-vindas ligadas", value="enabled", description="Envia quando alguém entrar.", default=bool(cfg.get("enabled", False)))
        self.flags_group.add_option(label="Mensagem privada ligada", value="dm_enabled", description="Também manda no privado.", default=bool(cfg.get("dm_enabled", False)))
        self.add_item(discord.ui.Label(text="Modo da mensagem pública", component=self.mode_group))
        self.add_item(discord.ui.Label(text="Opções básicas", component=self.flags_group))

    async def on_submit(self, interaction: discord.Interaction):
        selected = set(_modal_values(self.flags_group))
        cfg = deepcopy(self.panel.config)
        cfg["render_mode"] = str(_modal_value(self.mode_group, "components_v2"))
        cfg["enabled"] = "enabled" in selected
        cfg["dm_enabled"] = "dm_enabled" in selected
        if cfg["enabled"] and not int(cfg.get("channel_id") or 0):
            cfg["enabled"] = False
            notice = "Modo salvo. Escolha um canal antes de ligar."
        else:
            notice = "Ajuste rápido salvo."
        await self.panel.save_config(cfg, notice)
        self.panel.screen = "mode"
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)



class SpecialRuleCreateModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Criar regra especial")
        self.panel = panel
        self.type_select = None
        if hasattr(discord.ui, "Label"):
            options = [
                discord.SelectOption(label="Código de convite", value="invite_code", emoji="🔗", description="Ex.: abc123 ou link do convite"),
                discord.SelectOption(label="Pessoa que convidou", value="inviter", emoji="👤", description="Use ID ou menção da pessoa"),
                discord.SelectOption(label="Canal do convite", value="invite_channel", emoji="📍", description="Use ID ou menção do canal"),
            ]
            self.type_select = discord.ui.Select(placeholder="Quando essa regra deve valer?", min_values=1, max_values=1, options=options)
            self.add_item(discord.ui.Label(text="Tipo da regra", component=self.type_select))
        else:
            self.type_input = discord.ui.TextInput(label="Tipo: invite_code, inviter ou invite_channel", max_length=30, required=True)
            self.add_item(self.type_input)
        self.name_input = discord.ui.TextInput(label="Nome da regra", placeholder="Convite do evento", max_length=MAX_RULE_NAME, required=False)
        self.value_input = discord.ui.TextInput(label="Convite, pessoa ou canal", placeholder="Código/link do convite, menção ou ID", max_length=120, required=True)
        self.add_item(self.name_input)
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        match_type = _modal_value(self.type_select, "invite_code") if self.type_select is not None else str(self.type_input.value or "invite_code").strip().lower()
        raw_value = str(self.value_input.value or "").strip()
        if match_type == "invite_code":
            match_value = _clean_invite_code(raw_value)
            default_name = f"Convite {match_value}" if match_value else "Convite especial"
            if not match_value:
                await interaction.response.send_message(view=_make_notice_view("Convite inválido", "Coloque o código ou link de um convite.", ok=False), ephemeral=True)
                return
        elif match_type == "inviter":
            user_id = _id_from_text(raw_value)
            if not user_id:
                await interaction.response.send_message(view=_make_notice_view("Pessoa inválida", "Use uma menção ou ID da pessoa que convidou.", ok=False), ephemeral=True)
                return
            match_value = str(user_id)
            default_name = f"Convites de {raw_value[:40]}"
        elif match_type == "invite_channel":
            channel_id = _id_from_text(raw_value)
            if not channel_id:
                await interaction.response.send_message(view=_make_notice_view("Canal inválido", "Use uma menção ou ID do canal do convite.", ok=False), ephemeral=True)
                return
            match_value = str(channel_id)
            default_name = f"Convites do canal {raw_value[:40]}"
        else:
            await interaction.response.send_message(view=_make_notice_view("Tipo inválido", "Escolha convite, convidador ou canal.", ok=False), ephemeral=True)
            return
        cfg = deepcopy(self.panel.config)
        rules = list(cfg.get("special_rules") or [])
        if len(rules) >= MAX_SPECIAL_RULES:
            await interaction.response.send_message(view=_make_notice_view("Limite atingido", "Remova uma regra antiga antes de criar outra.", ok=False), ephemeral=True)
            return
        rule = self.panel.cog._make_rule(
            name=str(self.name_input.value or default_name).strip(),
            match_type=match_type,
            match_value=match_value,
        )
        rules.append(rule)
        cfg["special_rules"] = rules
        await self.panel.save_config(cfg, "Regra criada.")
        self.panel.selected_rule_id = rule["id"]
        self.panel.screen = "special_rule"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class SpecialInviteRuleModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Regra por convite")
        self.panel = panel
        self.name_input = discord.ui.TextInput(label="Nome da regra", placeholder="Convite do evento", max_length=MAX_RULE_NAME, required=False)
        self.code_input = discord.ui.TextInput(label="Código ou link do convite", placeholder="abc123 ou https://discord.gg/abc123", max_length=120, required=True)
        self.add_item(self.name_input)
        self.add_item(self.code_input)

    async def on_submit(self, interaction: discord.Interaction):
        code = _clean_invite_code(self.code_input.value)
        if not code:
            await interaction.response.send_message(view=_make_notice_view("Convite inválido", "Coloque o código ou link de um convite.", ok=False), ephemeral=True)
            return
        cfg = deepcopy(self.panel.config)
        rules = list(cfg.get("special_rules") or [])
        if len(rules) >= MAX_SPECIAL_RULES:
            await interaction.response.send_message(view=_make_notice_view("Limite atingido", "Remova uma regra antiga antes de criar outra.", ok=False), ephemeral=True)
            return
        rule = self.panel.cog._make_rule(name=str(self.name_input.value or f"Convite {code}").strip(), match_type="invite_code", match_value=code)
        rules.append(rule)
        cfg["special_rules"] = rules
        await self.panel.save_config(cfg, "Regra criada.")
        self.panel.selected_rule_id = rule["id"]
        self.panel.screen = "special_rule"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class SpecialRuleTextModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView", rule: dict[str, Any] | None):
        super().__init__(title="Texto especial")
        self.panel = panel
        self.rule_id = str((rule or {}).get("id") or panel.selected_rule_id)
        public = dict((rule or {}).get("public") or {})
        self.name_input = discord.ui.TextInput(label="Nome da regra", default=str((rule or {}).get("name") or "Regra especial")[:MAX_RULE_NAME], max_length=MAX_RULE_NAME, required=True)
        self.title_input = discord.ui.TextInput(label="Título", default=str(public.get("title") or "")[:256], placeholder="Vazio usa o título padrão", max_length=256, required=False)
        self.body_input = discord.ui.TextInput(label="Mensagem", style=discord.TextStyle.paragraph, default=str(public.get("body") or "")[:MAX_TEMPLATE_LENGTH], placeholder="Vazio usa a mensagem padrão", max_length=MAX_TEMPLATE_LENGTH, required=False)
        self.footer_input = discord.ui.TextInput(label="Texto final V2 opcional", style=discord.TextStyle.paragraph, default=str(public.get("footer") or "")[:MAX_FOOTER_LENGTH], max_length=MAX_FOOTER_LENGTH, required=False)
        self.add_item(self.name_input)
        self.add_item(self.title_input)
        self.add_item(self.body_input)
        self.add_item(self.footer_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.panel.update_rule(self.rule_id, {
            "name": str(self.name_input.value or "Regra especial").strip()[:MAX_RULE_NAME],
            "public": {
                "title": str(self.title_input.value or "").strip(),
                "body": str(self.body_input.value or "").strip(),
                "footer": str(self.footer_input.value or "").strip(),
            },
        }, "Texto especial salvo.")
        self.panel.screen = "special_rule"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class SpecialRuleVisualModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView", rule: dict[str, Any] | None):
        super().__init__(title="Visual especial")
        self.panel = panel
        self.rule_id = str((rule or {}).get("id") or panel.selected_rule_id)
        self.style_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.style_group = discord.ui.RadioGroup(required=True)
            current = str((rule or {}).get("style") or "inherit")
            self.style_group.add_option(label="Usar visual padrão", value="inherit", default=current == "inherit")
            for key, label in STYLE_LABELS.items():
                self.style_group.add_option(label=label, value=key, default=current == key)
            self.add_item(discord.ui.Label(text="Estilo da regra", component=self.style_group))
        else:
            self.style_input = discord.ui.TextInput(label="Estilo: inherit, complete, simple ou compact", default=str((rule or {}).get("style") or "inherit")[:20], max_length=20, required=True)
            self.add_item(self.style_input)
        self.media_mode_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.media_mode_group = discord.ui.RadioGroup(required=True)
            current_media_mode = _media_mode((rule or {}).get("media_mode"))
            for key, label in MEDIA_MODE_LABELS.items():
                self.media_mode_group.add_option(label=label, value=key, default=current_media_mode == key)
            self.add_item(discord.ui.Label(text="Imagem/banner", component=self.media_mode_group))
        self.accent_input = discord.ui.TextInput(label="Cor em HEX opcional", placeholder="#5865F2 ou vazio para padrão", default=str((rule or {}).get("accent_color") or "")[:7], max_length=7, required=False)
        self.image_input = discord.ui.TextInput(label="Imagem/banner opcional", placeholder="https://exemplo.com/imagem.png", default=str((rule or {}).get("media_url") or "")[:1000], max_length=1000, required=False)
        self.add_item(self.accent_input)
        self.add_item(self.image_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw_hex = str(self.accent_input.value or "").strip()
        if raw_hex and not HEX_RE.fullmatch(raw_hex):
            await interaction.response.send_message(view=_make_notice_view("Cor inválida", "Use uma cor no formato #5865F2 ou deixe vazio.", ok=False), ephemeral=True)
            return
        raw_url = str(self.image_input.value or "").strip()
        if raw_url and not URL_RE.fullmatch(raw_url):
            await interaction.response.send_message(view=_make_notice_view("Imagem inválida", "Use um link começando com http:// ou https://.", ok=False), ephemeral=True)
            return
        if self.style_group is not None:
            style = _modal_value(self.style_group, "inherit")
        else:
            style = str(self.style_input.value or "inherit").strip().lower()
        if style not in {"inherit", *STYLE_LABELS.keys()}:
            style = "inherit"
        await self.panel.update_rule(self.rule_id, {
            "accent_color": _parse_hex(raw_hex) if raw_hex else "",
            "media_url": _clean_url(raw_url),
            "media_mode": _media_mode(_modal_value(self.media_mode_group, "custom") if self.media_mode_group is not None else "custom"),
            "style": style,
        }, "Visual especial salvo.")
        self.panel.screen = "special_rule"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class SpecialRuleWebhookModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView", rule: dict[str, Any] | None):
        super().__init__(title="Webhook da regra")
        self.panel = panel
        self.rule_id = str((rule or {}).get("id") or panel.selected_rule_id)
        webhook = dict((rule or {}).get("webhook") or {})
        self.mode_group = None
        self.avatar_group = None
        if _advanced_modal_supported("Label", "RadioGroup"):
            self.mode_group = discord.ui.RadioGroup(required=True)
            current_mode = str(webhook.get("mode") or "inherit")
            for value, label in (("inherit", "Usar envio padrão"), ("bot", "Enviar pelo bot"), ("webhook", "Enviar pelo webhook")):
                self.mode_group.add_option(label=label, value=value, default=current_mode == value)
            self.avatar_group = discord.ui.RadioGroup(required=True)
            current_avatar = str(webhook.get("avatar_mode") or "inherit")
            self.avatar_group.add_option(label="Usar avatar padrão", value="inherit", default=current_avatar == "inherit")
            for key, label in WEBHOOK_AVATAR_LABELS.items():
                self.avatar_group.add_option(label=label, value=key, default=current_avatar == key)
            self.add_item(discord.ui.Label(text="Envio dessa regra", component=self.mode_group))
            self.add_item(discord.ui.Label(text="Avatar dessa regra", component=self.avatar_group))
        else:
            self.mode_input = discord.ui.TextInput(label="Uso: inherit, bot ou webhook", placeholder="inherit", default=str(webhook.get("mode") or "inherit")[:20], max_length=20, required=True)
            self.avatar_mode_input = discord.ui.TextInput(label="Avatar: inherit, server, member, inviter ou custom", placeholder="inherit", default=str(webhook.get("avatar_mode") or "inherit")[:20], max_length=20, required=True)
            self.add_item(self.mode_input)
            self.add_item(self.avatar_mode_input)
        self.name_input = discord.ui.TextInput(label="Nome opcional", placeholder="Vazio usa o padrão", default=str(webhook.get("name") or "")[:80], max_length=80, required=False)
        self.avatar_url_input = discord.ui.TextInput(label="Avatar por link opcional", placeholder="https://exemplo.com/avatar.png", default=str(webhook.get("avatar_url") or "")[:1000], max_length=1000, required=False)
        self.add_item(self.name_input)
        self.add_item(self.avatar_url_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.mode_group is not None:
            mode = _modal_value(self.mode_group, "inherit")
            avatar_mode = _modal_value(self.avatar_group, "inherit")
        else:
            mode = str(self.mode_input.value or "inherit").strip().lower()
            avatar_mode = str(self.avatar_mode_input.value or "inherit").strip().lower()
        if mode not in {"inherit", "bot", "webhook"}:
            mode = "inherit"
        if avatar_mode not in {"inherit", *WEBHOOK_AVATAR_LABELS.keys()}:
            avatar_mode = "inherit"
        raw_url = str(self.avatar_url_input.value or "").strip()
        if raw_url and not URL_RE.fullmatch(raw_url):
            await interaction.response.send_message(view=_make_notice_view("Avatar inválido", "Use um link começando com http:// ou https://.", ok=False), ephemeral=True)
            return
        if avatar_mode == "custom" and not raw_url:
            await interaction.response.send_message(view=_make_notice_view("Avatar incompleto", "Escolha um link quando usar avatar personalizado.", ok=False), ephemeral=True)
            return
        await self.panel.update_rule(self.rule_id, {
            "webhook": {
                "mode": mode,
                "name": _safe_webhook_name(self.name_input.value, "") if str(self.name_input.value or "").strip() else "",
                "avatar_mode": avatar_mode,
                "avatar_url": _clean_url(raw_url),
            }
        }, "Webhook da regra salvo.")
        self.panel.screen = "special_rule"
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class WelcomeAdminView(discord.ui.LayoutView):
    def __init__(self, cog: "WelcomeCog", *, owner_id: int, guild_id: int, config: dict[str, Any]):
        super().__init__(timeout=900)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.guild_id = int(guild_id)
        self.config = cog._normalize_config(config)
        self.screen = "home"
        self.screen_history: list[str] = []
        self.notice = ""
        self.message: discord.Message | None = None
        self.command_message: discord.Message | None = None
        self._preview_member: discord.Member | None = None
        self.webhook_choices: list[dict[str, Any]] = []
        self.selected_rule_id = ""
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(getattr(interaction.user, "id", 0) or 0) != self.owner_id:
            try:
                await interaction.response.send_message(
                    view=_make_notice_view("Painel em uso", "Esse painel pertence a quem abriu o comando.", ok=False),
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return False
        if not self.cog._can_manage(interaction.user):
            try:
                await interaction.response.send_message(
                    view=_make_notice_view("Sem permissão", "Você precisa gerenciar o servidor para usar esse painel.", ok=False),
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return False
        return True

    def go_to(self, screen: str, *, remember: bool = True):
        if remember and self.screen != screen:
            self.screen_history.append(self.screen)
        self.screen = screen

    def go_back(self):
        previous = self.screen_history.pop() if self.screen_history else "home"
        self.screen = previous
        self.notice = ""

    async def save_config(self, cfg: dict[str, Any], notice: str) -> bool:
        cfg = self.cog._sync_active_mode_config(cfg)
        ok = await self.cog._save_config(self.guild_id, cfg)
        self.config = await self.cog._get_config(self.guild_id)
        self.notice = notice if ok else "Não consegui salvar agora. Tente novamente em alguns segundos."
        return ok

    async def load_webhooks(self, guild: discord.Guild | None):
        self.webhook_choices = await self.cog._list_channel_webhooks(guild, self.config)

    async def send_preview(self, interaction: discord.Interaction, *, dm: bool = False, variant_id: str = ""):
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.NotFound:
            log.warning("preview de boas-vindas ignorado: interação expirou antes do defer")
            return
        except Exception as exc:
            log.warning("não consegui responder ao preview de boas-vindas a tempo: %r", exc)
            return

        cfg = self.config
        if not dm:
            if variant_id:
                cfg = self.cog._apply_variant(cfg, self.cog._find_variant(cfg, variant_id))
            else:
                cfg = self.cog._apply_variant(cfg, self.cog._pick_variant(cfg))
        cfg = await self.cog._with_dynamic_colors(cfg, member=member)
        mode = str(cfg.get("dm_render_mode") if dm else cfg.get("render_mode") or "components_v2")
        try:
            cfg = await self.cog._prepare_decorative_emojis(cfg, member=member, mode=mode, dm=dm, invite_info=None, preview=True)
        except Exception as exc:
            log.warning("falha ao preparar emojis do preview de boas-vindas; mantendo originais: %r", exc)
        try:
            cfg, files = await self.cog._prepare_dynamic_media(cfg, member=member, mode=mode, dm=dm)
        except Exception as exc:
            log.warning("falha ao montar mídia do preview de boas-vindas; usando preview sem imagem dinâmica: %r", exc)
            cfg, files = self.cog._drop_dynamic_star_media(cfg, mode=mode), []
        allowed = discord.AllowedMentions.none()
        try:
            if mode == "embed":
                content, embed = self.cog._make_embed_payload(cfg, member=member, guild_id=self.guild_id, dm=dm)
                kwargs: dict[str, Any] = {"embed": embed, "ephemeral": True, "allowed_mentions": allowed}
                if content:
                    kwargs["content"] = content
                if files:
                    kwargs["files"] = files
                await interaction.followup.send(**kwargs)
                return
            if mode == "normal":
                content = self.cog._make_normal_content(cfg, member=member, guild_id=self.guild_id, dm=dm)
                await interaction.followup.send(content=content, ephemeral=True, allowed_mentions=allowed)
                return
            view = discord.ui.LayoutView(timeout=None)
            view.add_item(self.cog._make_welcome_container(cfg, member=member, guild_id=self.guild_id, dm=dm))
            kwargs: dict[str, Any] = {"view": view, "ephemeral": True, "allowed_mentions": allowed}
            if files:
                kwargs["files"] = files
            await interaction.followup.send(**kwargs)
        except Exception as exc:
            log.exception("falha ao enviar preview de boas-vindas")
            with contextlib.suppress(Exception):
                await interaction.followup.send(
                    view=_make_notice_view("Preview indisponível", "Não consegui montar a prévia agora. A mensagem real continua protegida por fallback." , ok=False),
                    ephemeral=True,
                )

    async def update_rule(self, rule_id: str, updates: dict[str, Any], notice: str) -> bool:
        cfg = deepcopy(self.config)
        rules = list(cfg.get("special_rules") or [])
        for idx, rule in enumerate(rules):
            if str(rule.get("id")) == str(rule_id):
                merged = deepcopy(rule)
                for key, value in updates.items():
                    if isinstance(value, dict) and isinstance(merged.get(key), dict):
                        nested = dict(merged.get(key) or {})
                        nested.update(value)
                        merged[key] = nested
                    else:
                        merged[key] = value
                rules[idx] = self.cog._normalize_rule(merged)
                cfg["special_rules"] = rules
                ok = await self.save_config(cfg, notice)
                self.selected_rule_id = str(rule_id)
                return ok
        self.notice = "Essa regra não existe mais."
        return False

    async def update_selected_rule(self, updates: dict[str, Any], notice: str) -> bool:
        return await self.update_rule(self.selected_rule_id, updates, notice)

    async def update_variant(self, variant_id: str, updates: dict[str, Any], notice: str) -> bool:
        cfg = deepcopy(self.config)
        variants = [self.cog._normalize_variant(v) for v in cfg.get("variants") or []]
        for idx, variant in enumerate(variants):
            if str(variant.get("id")) == str(variant_id):
                merged = deepcopy(variant)
                for key, value in updates.items():
                    if isinstance(value, dict) and isinstance(merged.get(key), dict):
                        nested = dict(merged.get(key) or {})
                        nested.update(value)
                        merged[key] = nested
                    else:
                        merged[key] = value
                variants[idx] = self.cog._normalize_variant(merged)
                cfg["variants"] = variants[:MAX_WELCOME_VARIANTS]
                ok = await self.save_config(cfg, notice)
                self.selected_variant_id = str(variant_id)
                return ok
        self.notice = "Essa variação não existe mais."
        return False

    def _clear(self):
        for item in list(self.children):
            self.remove_item(item)

    def _home_lines(self) -> list[str]:
        cfg = self.config
        role_count = len([int(r) for r in cfg.get("auto_role_ids") or []])
        rules_count = len(list(cfg.get("special_rules") or []))
        variants_count = len([v for v in cfg.get("variants") or [] if bool(v.get("enabled", True))])
        webhook_cfg = dict(cfg.get("webhook") or {})
        enabled = bool(cfg.get("enabled", False))
        channel_id = int(cfg.get("channel_id") or 0)
        mode = RENDER_MODE_LABELS.get(str(cfg.get("render_mode") or "components_v2"), "Components V2")
        send_label = "envio pelo webhook" if webhook_cfg.get("enabled") else "envio pelo bot"
        dm_label = "DM ligada" if bool(cfg.get("dm_enabled", False)) else "DM desligada"
        delete_label = "apaga se sair em até 24 horas" if bool(cfg.get("delete_on_leave_enabled", False)) else "mantém se sair"
        emoji_label = "emojis coloridos" if bool(cfg.get("decorative_emoji_enabled", False)) else "emojis normais"
        role_label = f"{role_count} cargo{'s' if role_count != 1 else ''}" if role_count else "sem cargos"
        rule_label = f"{rules_count} regra{'s' if rules_count != 1 else ''} especial{'is' if rules_count != 1 else ''}" if rules_count else "sem regras especiais"
        variant_label = f"{variants_count} variaç{'ões' if variants_count != 1 else 'ão'}" if variants_count else "sem variações"
        if enabled and channel_id:
            first = f"Tudo pronto. Novos membros serão recebidos em {_channel_mention(channel_id)}."
        elif enabled:
            first = "Boas-vindas ligadas, mas ainda falta escolher um canal."
        else:
            first = "Boas-vindas desligadas."
        second = "Nenhum canal escolhido ainda." if not channel_id else ""
        lines = [
            "# 🌟 Boas-vindas",
            "Receba novos membros com uma mensagem feita para o seu servidor.",
            "",
            first,
        ]
        if second:
            lines.append(second)
        lines.extend([
            "",
            f"Mensagem em {mode} · {send_label}",
            f"{dm_label} · {delete_label} · {emoji_label} · {role_label} · {variant_label} · {rule_label}",
        ])
        if self.notice:
            lines.extend(["", self.notice])
        lines.extend(["", "Escolha abaixo o que quer ajustar."])
        return lines


    def _rebuild(self, *, member: discord.Member | None = None):
        if member is not None:
            self._preview_member = member
        self._clear()
        builders = {
            "home": self._build_home,
            "message": self._build_message,
            "embed_editor": self._build_embed_editor,
            "presets": self._build_presets,
            "variants": self._build_variants,
            "variant_detail": self._build_variant_detail,
            "mode": self._build_mode,
            "channel": self._build_channel,
            "webhook": self._build_webhook,
            "webhook_existing": self._build_webhook_existing,
            "dm": self._build_dm,
            "dm_mode": self._build_dm_mode,
            "dm_preview": self._build_dm_preview,
            "roles": self._build_roles,
            "visual": self._build_visual,
            "variables": self._build_variables,
            "special": self._build_special,
            "special_list": self._build_special_list,
            "special_create_inviter": self._build_special_create_inviter,
            "special_create_channel": self._build_special_create_channel,
            "special_rule": self._build_special_rule,
            "special_rule_mode": self._build_special_rule_mode,
            "special_rule_channel": self._build_special_rule_channel,
            "special_rule_roles": self._build_special_rule_roles,
            "status": self._build_status,
            "preview": self._build_preview,
        }
        builder = builders.get(self.screen)
        if builder is None:
            self.screen = "home"
            builder = self._build_home
        builder()

    def _build_home(self):
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(self._home_lines())),
            discord.ui.Separator(),
            discord.ui.ActionRow(_MainSelect(self)),
            discord.ui.ActionRow(_PreviewButton(self), _CloseButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_message(self):
        mode = str(self.config.get("render_mode") or "components_v2")
        if mode == "embed":
            self._build_embed_editor()
            return
        public = dict(self.config.get("public") or {})
        if mode == "normal":
            body = str(public.get("body") or DEFAULT_PUBLIC["body"]).strip()
            title = str(public.get("title") or "").strip()
            lines = [
                "# 💬 Editor de texto",
                "Edite a mensagem simples enviada no canal.",
                "",
            ]
            if title:
                lines.extend(["**Título**", _trim(title, 400), ""])
            lines.extend(["**Mensagem**", _trim(body, 1200)])
        else:
            title = str(public.get("title") or DEFAULT_PUBLIC["title"]).strip()
            body = str(public.get("body") or DEFAULT_PUBLIC["body"]).strip()
            final_text = str(public.get("footer") or "").strip()
            lines = [
                "# ✨ Editor Components V2",
                "Monte a mensagem moderna com container e texto V2.",
                "",
                "**Título da mensagem**",
                _trim(title, 400),
                "",
                "**Texto principal**",
                _trim(body, 900),
            ]
            if final_text:
                lines.extend(["", "**Texto final da mensagem V2**", _trim(final_text, 300)])
            media_label = "imagem configurada" if _clean_url(self.config.get("media_url")) else "sem imagem"
            lines.extend([
                "",
                f"Visual: {STYLE_LABELS.get(str(self.config.get('style') or 'complete'), 'Completo')} · cor `{_parse_hex(self.config.get('accent_color'))}` · {media_label}",
            ])
        if self.notice:
            lines.extend(["", self.notice])
        lines.extend(["", "Escolha uma parte para editar."])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_MessageActionSelect(self)),
            discord.ui.ActionRow(_PreviewButton(self), _BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_embed_editor(self):
        if str(self.config.get("render_mode") or "components_v2") != "embed":
            self.screen = "message"
            self._build_message()
            return
        embed = self.cog._normalize_embed_config(self.config.get("embed"))
        has_content = bool(str(embed.get("content") or "").strip())
        has_title = bool(str(embed.get("title") or "").strip())
        has_desc = bool(str(embed.get("description") or "").strip())
        thumb_mode = _image_mode(embed.get("thumbnail_mode"))
        image_mode = _image_mode(embed.get("image_mode"), fallback="custom")
        thumb = EMBED_IMAGE_MODE_LABELS.get(thumb_mode, "Sem imagem")
        image = EMBED_MAIN_IMAGE_MODE_LABELS.get(image_mode, "Link personalizado")
        if thumb_mode == "custom" and not _clean_url(embed.get("thumbnail_url")):
            thumb = "Sem imagem"
        if image_mode == "custom" and not (_clean_url(embed.get("image_url")) or _clean_url(self.config.get("media_url"))):
            image = "Sem imagem"
        footer = str(embed.get("footer_text") or "").strip()
        color = str(embed.get("color") or self.config.get("accent_color") or DEFAULT_ACCENT)
        lines = [
            "# 🧾 Editor de embed",
            "Monte a mensagem clássica do modo Embed.",
            "",
            f"Mensagem acima: {'configurada' if has_content else 'sem texto acima'}",
            f"Texto do embed: {'título próprio' if has_title else 'sem título'} · {'descrição própria' if has_desc else 'usa mensagem principal'}",
            f"Imagens: thumbnail {thumb.lower()} · principal {image.lower()}",
            f"Footer do embed: {_trim(footer, 120) if footer else 'sem footer'}",
            f"Cor do embed: `{_parse_hex(color)}`",
            "",
            "Escolha uma parte para editar.",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_MessageActionSelect(self)),
            discord.ui.ActionRow(_PreviewButton(self), _BackButton(self)),
            accent_color=_color_from_hex(embed.get("color") or self.config.get("accent_color")),
        ))


    def _build_variants(self):
        variants = [self.cog._normalize_variant(v) for v in self.config.get("variants") or []]
        percentages = self.cog._variant_percentages(variants)
        lines = [
            "# 🎲 Variações da mensagem",
            "O bot escolhe uma mensagem quando alguém entra.",
            "",
            f"Variações: {len(variants)}/{MAX_WELCOME_VARIANTS}",
        ]
        if variants:
            lines.append("")
            for idx, variant in enumerate(variants, start=1):
                vid = str(variant.get("id"))
                if bool(variant.get("enabled", True)):
                    chance = percentages.get(vid, 0.0)
                    status = f"{chance:.0f}% aprox."
                else:
                    status = "pausada"
                lines.append(f"**{idx}. {variant.get('name') or 'Variação'}** · peso {int(variant.get('weight') or 1)} · {status}")
        else:
            lines.extend(["", "Nenhuma variação ainda. A mensagem padrão será usada."])
        if self.notice:
            lines.extend(["", self.notice])
        rows: list[discord.ui.Item[Any]] = [discord.ui.TextDisplay(_trim("\n".join(lines))), discord.ui.Separator()]
        if variants:
            rows.append(discord.ui.ActionRow(_VariantListSelect(self)))
        rows.append(discord.ui.ActionRow(_VariantActionSelect(self)))
        rows.append(discord.ui.ActionRow(_BackButton(self)))
        self.add_item(discord.ui.Container(*rows, accent_color=_color_from_hex(self.config.get("accent_color"))))

    def _build_variant_detail(self):
        variant = self.cog._find_variant(self.config, self.selected_variant_id)
        if variant is None:
            self.screen = "variants"
            self._build_variants()
            return
        variant = self.cog._normalize_variant(variant)
        mode = str(self.config.get("render_mode") or "components_v2")
        percentages = self.cog._variant_percentages([self.cog._normalize_variant(v) for v in self.config.get("variants") or []])
        chance = percentages.get(str(variant.get("id")), 0.0)
        public = dict(variant.get("public") or {})
        embed = self.cog._normalize_embed_config(variant.get("embed"))
        if mode == "embed":
            content_bits = []
            if str(embed.get("content") or "").strip():
                content_bits.append("mensagem acima")
            if str(embed.get("title") or "").strip():
                content_bits.append("título")
            if str(embed.get("description") or "").strip():
                content_bits.append("descrição")
            if str(embed.get("footer_text") or "").strip():
                content_bits.append("footer")
            content_label = ", ".join(content_bits) if content_bits else "usa o embed padrão"
        else:
            title = str(public.get("title") or "").strip()
            body = str(public.get("body") or "").strip()
            footer = str(public.get("footer") or "").strip()
            content_label = "configurada" if title or body or footer else "usa a mensagem padrão"
        style = STYLE_LABELS.get(str(variant.get("style") or "inherit"), "Usa o padrão")
        color_mode = str(variant.get("accent_color_mode") or "inherit")
        if color_mode == "inherit":
            color = "usa a cor padrão"
        elif color_mode == "member_avatar":
            color = "combina com a foto do membro"
        else:
            color = f"cor fixa `{variant.get('accent_color') or self.config.get('accent_color') or DEFAULT_ACCENT}`"
        lines = [
            f"# 🎲 {variant.get('name') or 'Variação'}",
            f"{'Ativa' if bool(variant.get('enabled', True)) else 'Pausada'} · peso {int(variant.get('weight') or 1)} · {chance:.0f}% aprox.",
            "",
            f"Mensagem: {content_label}",
            f"Visual: {style} · {color}",
            f"Imagem: {MEDIA_MODE_LABELS.get(_media_mode(variant.get('media_mode')), 'Link personalizado') if _media_mode(variant.get('media_mode')) != 'custom' else ('configurada' if _clean_url(variant.get('media_url')) else 'usa a padrão')}",
            "",
            "Escolha o que deseja ajustar.",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_VariantEditActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(variant.get("accent_color") or self.config.get("accent_color")),
        ))

    def _build_presets(self):
        lines = ["# ✨ Presets", "Escolha uma base e edite depois como quiser."]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_PresetSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_mode(self):
        mode = str(self.config.get("render_mode") or "components_v2")
        lines = [
            "# 🎨 Modo da mensagem",
            "Escolha como a mensagem pública deve aparecer.",
            "",
            f"**Atual**\n{RENDER_MODE_LABELS.get(mode, 'Components V2')}",
            "",
            "Components V2 é o visual mais completo. Embed é o visual clássico. Mensagem normal é mais simples.",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_RenderModeSelect(self)),
            discord.ui.ActionRow(_PreviewButton(self), _BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_channel(self):
        channel_id = int(self.config.get("channel_id") or 0)
        lines = ["# 📍 Canal de envio", "Onde a mensagem deve aparecer quando alguém entrar?", "", f"**Canal atual**\n{_channel_mention(channel_id)}"]
        if self.notice:
            lines.extend(["", self.notice])
        rows: list[discord.ui.Item[Any]] = [discord.ui.TextDisplay("\n".join(lines)), discord.ui.Separator(), discord.ui.ActionRow(_ChannelSelect(self))]
        if channel_id:
            rows.append(discord.ui.ActionRow(_ChannelActionSelect(self)))
        rows.append(discord.ui.ActionRow(_BackButton(self)))
        self.add_item(discord.ui.Container(*rows, accent_color=_color_from_hex(self.config.get("accent_color"))))

    def _build_webhook(self):
        webhook = dict(self.config.get("webhook") or {})
        lines = [
            "# 🪝 Webhook de boas-vindas",
            "Deixe a recepção com um nome e avatar próprios.",
            "",
            f"{'Enviando pelo webhook' if webhook.get('enabled') else 'Enviando pelo bot'} · {webhook.get('webhook_id') or 'nenhum webhook salvo'}",
            f"{WEBHOOK_NAME_LABELS.get(str(webhook.get('name_mode') or 'fixed'), 'Nome personalizado')} · {_safe_webhook_name(webhook.get('name'))}",
            f"{WEBHOOK_AVATAR_LABELS.get(str(webhook.get('avatar_mode') or 'server'), 'Avatar do servidor')}",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_WebhookActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_webhook_existing(self):
        lines = ["# 📌 Webhooks encontrados", "Escolha um webhook para usar nas boas-vindas."]
        if not self.webhook_choices:
            lines.append("\nNão encontrei webhooks no canal salvo.")
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_WebhookExistingSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_dm(self):
        dm = dict(self.config.get("dm") or {})
        mode = str(self.config.get("dm_render_mode") or self.config.get("render_mode") or "components_v2")
        lines = [
            "# 💬 Mensagem privada",
            "Você pode mandar uma mensagem no privado quando alguém entrar.",
            "",
            f"**Status**\n{_status_label(bool(self.config.get('dm_enabled', False)))}",
            "",
            f"**Modo**\n{RENDER_MODE_LABELS.get(mode, 'Components V2')}",
            "",
            "**Título**",
            _trim(dm.get("title") or DEFAULT_DM["title"], 400),
            "",
            "**Mensagem**",
            _trim(dm.get("body") or DEFAULT_DM["body"], 700),
        ]
        footer = str(dm.get("footer") or "").strip()
        if footer:
            lines.extend(["", "**Texto final**", _trim(footer, 300)])
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_DmActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_dm_mode(self):
        mode = str(self.config.get("dm_render_mode") or self.config.get("render_mode") or "components_v2")
        lines = ["# 🎨 Modo da mensagem privada", "Escolha como a DM deve aparecer.", "", f"**Atual**\n{RENDER_MODE_LABELS.get(mode, 'Components V2')}"]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_DmRenderModeSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_dm_preview(self):
        member = self._preview_member
        self.add_item(discord.ui.Container(discord.ui.TextDisplay("# 💬 Preview da mensagem privada\nFicaria assim no privado."), accent_color=_color_from_hex(self.config.get("accent_color"))))
        self.cog._append_render_preview(self, self.config, member=member, guild_id=self.guild_id, dm=True)
        self.add_item(discord.ui.ActionRow(_BackButton(self)))

    def _build_roles(self):
        role_ids = [int(r) for r in self.config.get("auto_role_ids") or []]
        lines = ["# 🎭 Cargos automáticos", "Escolha os cargos entregues quando alguém entrar.", "", f"**Atuais**\n{_role_list(self.cog.bot.get_guild(self.guild_id), role_ids)}"]
        if self.notice:
            lines.extend(["", self.notice])
        rows: list[discord.ui.Item[Any]] = [discord.ui.TextDisplay("\n".join(lines)), discord.ui.Separator(), discord.ui.ActionRow(_RoleSelect(self))]
        if role_ids:
            rows.append(discord.ui.ActionRow(_RoleActionSelect(self)))
        rows.append(discord.ui.ActionRow(_BackButton(self)))
        self.add_item(discord.ui.Container(*rows, accent_color=_color_from_hex(self.config.get("accent_color"))))

    def _build_visual(self):
        emoji_status = "ligados" if bool(self.config.get("decorative_emoji_enabled", False)) else "desligados"
        lines = [
            "# 🖼️ Visual da mensagem",
            "Escolha como a mensagem vai aparecer.",
            "",
            f"**Estilo**\n{STYLE_LABELS.get(str(self.config.get('style') or 'complete'), 'Completo')}",
            "",
            f"**Cor**\n`{_parse_hex(self.config.get('accent_color'))}`",
            "",
            f"**Imagem**\n{MEDIA_MODE_LABELS.get(_media_mode(self.config.get('media_mode')), 'Link personalizado') if _media_mode(self.config.get('media_mode')) != 'custom' else ('configurada' if _clean_url(self.config.get('media_url')) else 'sem imagem')}",
            "",
            f"**Emojis decorativos**\n{emoji_status} · até {self.cog._decorative_emoji_limit_for_guild_id(self.guild_id)} emojis neste servidor",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_VisualActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_variables(self):
        lines = ["# 🧬 Variáveis", "Use essas palavras no texto. O bot troca sozinho quando alguém entra.", ""]
        for name, description in VARIABLE_HELP.items():
            lines.append(f"`{{{name}}}` — {description}")
        lines.extend(["", "Para descobrir o convite usado, preciso conseguir ver os convites do servidor. Se não der, a mensagem sai normal."])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_special(self):
        rules = list(self.config.get("special_rules") or [])
        lines = [
            "# 🎁 Boas-vindas especiais",
            "Mude a recepção dependendo do convite usado.",
            "",
            f"**Regras criadas**\n{len(rules)} de {MAX_SPECIAL_RULES}",
            "",
            "Prioridade: convite específico, depois convidador, depois canal do convite.",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_SpecialMainSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_special_list(self):
        rules = list(self.config.get("special_rules") or [])
        lines = ["# ✏️ Regras especiais", "Escolha uma regra para editar."]
        if self.notice:
            lines.extend(["", self.notice])
        rows = [discord.ui.TextDisplay("\n".join(lines)), discord.ui.Separator()]
        if rules:
            rows.append(discord.ui.ActionRow(_SpecialRuleListSelect(self)))
        rows.append(discord.ui.ActionRow(_BackButton(self)))
        self.add_item(discord.ui.Container(*rows, accent_color=_color_from_hex(self.config.get("accent_color"))))

    def _build_special_create_inviter(self):
        lines = ["# 👤 Regra por convidador", "Quando essa pessoa convidar alguém, a recepção pode mudar."]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_SpecialInviterSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_special_create_channel(self):
        lines = ["# 📍 Regra por canal do convite", "Quando o convite for desse canal, a recepção pode mudar."]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_SpecialInviteChannelSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_special_rule(self):
        rule = self.cog._find_rule(self.config, self.selected_rule_id)
        if not rule:
            self.screen = "special"
            self.notice = "Essa regra não existe mais."
            self._build_special()
            return
        lines = [
            f"# 🎁 {str(rule.get('name') or 'Regra especial')}",
            self.cog._rule_match_summary(rule),
            "",
            f"**Status**\n{_status_label(bool(rule.get('enabled', True)))}",
            "",
            f"**Modo**\n{RENDER_MODE_LABELS.get(str(rule.get('render_mode') or 'inherit'), 'Usar padrão') if rule.get('render_mode') != 'inherit' else 'usar padrão'}",
            "",
            f"**Canal**\n{_channel_mention(rule.get('channel_id')) if int(rule.get('channel_id') or 0) else 'usar canal padrão'}",
            "",
            f"**Cargos extras**\n{len(rule.get('auto_role_ids') or [])} cargo(s)",
            "",
            f"**Texto especial**\n{'configurado' if any(str(v or '').strip() for v in dict(rule.get('public') or {}).values()) else 'usa o texto padrão'}",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_SpecialRuleActionSelect(self, rule)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(rule.get("accent_color") or self.config.get("accent_color")),
        ))

    def _build_special_rule_mode(self):
        rule = self.cog._find_rule(self.config, self.selected_rule_id)
        if not rule:
            self.screen = "special"
            self._build_special()
            return
        lines = ["# 🎨 Modo da regra", "Escolha se essa regra usa outro modo de mensagem."]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_SpecialRuleModeSelect(self, rule)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(rule.get("accent_color") or self.config.get("accent_color")),
        ))

    def _build_special_rule_channel(self):
        rule = self.cog._find_rule(self.config, self.selected_rule_id) or {}
        lines = ["# 📍 Canal especial", "Escolha outro canal só para essa regra.", "", f"**Atual**\n{_channel_mention(rule.get('channel_id')) if int(rule.get('channel_id') or 0) else 'usar canal padrão'}"]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_SpecialRuleChannelSelect(self)),
            discord.ui.ActionRow(_SpecialRuleChannelActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(rule.get("accent_color") or self.config.get("accent_color")),
        ))

    def _build_special_rule_roles(self):
        rule = self.cog._find_rule(self.config, self.selected_rule_id) or {}
        role_ids = [int(r) for r in rule.get("auto_role_ids") or []]
        lines = ["# 🎭 Cargos extras", "Esses cargos entram junto com os cargos padrão.", "", f"**Atuais**\n{_role_list(self.cog.bot.get_guild(self.guild_id), role_ids)}"]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_SpecialRuleRoleSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(rule.get("accent_color") or self.config.get("accent_color")),
        ))

    def _build_status(self):
        channel_id = int(self.config.get("channel_id") or 0)
        delete_label = "apaga só se sair em até 24 horas" if bool(self.config.get("delete_on_leave_enabled", False)) else "mantém a mensagem"
        lines = [
            "# ⚙️ Configurações",
            "Ligue, pause e escolha o que acontece quando alguém sair.",
            "",
            f"**Status atual**\n{_status_label(bool(self.config.get('enabled', False)))}",
            "",
            f"**Canal**\n{_channel_mention(channel_id)}",
            "",
            f"**Quando o membro sair**\n{delete_label}",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_StatusSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_preview(self):
        member = self._preview_member
        lines = ["# 👁️ Preview", self.notice or "Ficaria assim quando alguém entrar."]
        self.add_item(discord.ui.Container(discord.ui.TextDisplay("\n".join(lines)), accent_color=_color_from_hex(self.config.get("accent_color"))))
        self.cog._append_render_preview(self, self.config, member=member, guild_id=self.guild_id, dm=False)
        self.add_item(discord.ui.ActionRow(_BackButton(self)))


class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._warmup_task: asyncio.Task | None = None
        self._emoji_purge_task: asyncio.Task | None = None
        self._emoji_worker_active: dict[str, int] = {}
        self._avatar_color_cache: dict[str, str] = {}
        self._avatar_palette_cache: dict[str, list[tuple[int, int, int]]] = {}
        self._star_image_cache: dict[str, bytes] = {}
        self._bot_owner_ids: set[int] = self._collect_known_bot_owner_ids()
        self._owner_presence_cache: dict[int, tuple[float, bool]] = {}

    @property
    def db(self):
        return getattr(self.bot, "settings_db", None)

    def _collect_known_bot_owner_ids(self) -> set[int]:
        ids: set[int] = set()

        def add(value: Any) -> None:
            try:
                parsed = int(value or 0)
            except Exception:
                return
            if parsed > 0:
                ids.add(parsed)

        add(getattr(self.bot, "owner_id", 0))
        for raw in (getattr(self.bot, "owner_ids", None) or []):
            add(raw)
        for env_name in ("BOT_OWNER_ID", "OWNER_ID", "TTS_VOICE_FAILURE_DM_USER_ID", "VOICE_FAILURE_DM_USER_ID"):
            add(os.getenv(env_name))
        with contextlib.suppress(Exception):
            import config as bot_config  # type: ignore
            for attr in ("BOT_OWNER_ID", "OWNER_ID", "TTS_VOICE_FAILURE_DM_USER_ID", "VOICE_FAILURE_DM_USER_ID"):
                add(getattr(bot_config, attr, 0))
        return ids

    async def _refresh_bot_owner_ids(self) -> None:
        ids = self._collect_known_bot_owner_ids()
        try:
            app = await self.bot.application_info()
        except Exception:
            self._bot_owner_ids = ids
            return
        owner = getattr(app, "owner", None)
        if owner is not None:
            with contextlib.suppress(Exception):
                ids.add(int(owner.id))
        team = getattr(app, "team", None)
        for member in getattr(team, "members", None) or []:
            user = getattr(member, "user", member)
            with contextlib.suppress(Exception):
                ids.add(int(user.id))
        self._bot_owner_ids = ids

    def _guild_has_bot_owner_cached(self, guild: discord.Guild | None) -> bool:
        if guild is None or not self._bot_owner_ids:
            return False
        cached = self._owner_presence_cache.get(int(guild.id))
        if cached and (time.monotonic() - float(cached[0])) < OWNER_PRESENCE_CACHE_SECONDS:
            return bool(cached[1])
        for owner_id in self._bot_owner_ids:
            if int(getattr(guild, "owner_id", 0) or 0) == int(owner_id):
                self._owner_presence_cache[int(guild.id)] = (time.monotonic(), True)
                return True
            if guild.get_member(int(owner_id)) is not None:
                self._owner_presence_cache[int(guild.id)] = (time.monotonic(), True)
                return True
        return False

    def _decorative_emoji_limit_for_guild_id(self, guild_id: int | None) -> int:
        guild = self.bot.get_guild(int(guild_id or 0)) if guild_id else None
        return OWNER_GUILD_DECORATIVE_EMOJI_LIMIT if self._guild_has_bot_owner_cached(guild) else DEFAULT_DECORATIVE_EMOJI_LIMIT

    async def _decorative_emoji_limit_for_member(self, member: discord.Member | None) -> int:
        guild = getattr(member, "guild", None)
        if guild is None or not self._bot_owner_ids:
            return DEFAULT_DECORATIVE_EMOJI_LIMIT
        if self._guild_has_bot_owner_cached(guild):
            return OWNER_GUILD_DECORATIVE_EMOJI_LIMIT
        cached = self._owner_presence_cache.get(int(guild.id))
        if cached and (time.monotonic() - float(cached[0])) < OWNER_PRESENCE_CACHE_SECONDS:
            return OWNER_GUILD_DECORATIVE_EMOJI_LIMIT if bool(cached[1]) else DEFAULT_DECORATIVE_EMOJI_LIMIT
        present = False
        for owner_id in self._bot_owner_ids:
            try:
                await asyncio.wait_for(guild.fetch_member(int(owner_id)), timeout=1.5)
                present = True
                break
            except Exception:
                continue
        self._owner_presence_cache[int(guild.id)] = (time.monotonic(), present)
        return OWNER_GUILD_DECORATIVE_EMOJI_LIMIT if present else DEFAULT_DECORATIVE_EMOJI_LIMIT

    async def cog_load(self):
        await self._refresh_bot_owner_ids()
        await self._ensure_indexes()
        self._warmup_task = asyncio.create_task(self._warmup_invites())
        self._emoji_purge_task = asyncio.create_task(self._emoji_midnight_purge_loop())

    async def cog_unload(self):
        if self._warmup_task is not None:
            self._warmup_task.cancel()
        if self._emoji_purge_task is not None:
            self._emoji_purge_task.cancel()

    async def _ensure_indexes(self):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        try:
            # Não criamos mais um índice simples (type, guild_id), porque outras cogs
            # já podem ter criado o mesmo padrão com outro nome. Isso evita aviso inútil
            # de IndexOptionsConflict a cada restart.
            await db.coll.create_index([("type", 1), ("guild_id", 1), ("member_id", 1)], name="welcome_sent_member")
            await db.coll.create_index([("type", 1), ("expires_at", 1)], name="welcome_sent_expires")
            await db.coll.create_index([("type", 1), ("delete_after", 1)], name="welcome_temp_emoji_purge")
        except Exception as exc:
            text = str(exc)
            if "IndexOptionsConflict" in text or "Index already exists" in text:
                log.debug("índice de boas-vindas já existe com outro nome: %s", exc)
            else:
                log.warning("falha ao criar índice de boas-vindas: %s", exc)
        await self._migrate_welcome_tracking_user_ids()

    def _default_webhook_config(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "channel_id": 0,
            "webhook_id": 0,
            "webhook_token": "",
            "name": DEFAULT_WEBHOOK_NAME,
            "name_mode": "fixed",
            "avatar_mode": "server",
            "avatar_url": "",
        }

    def _default_config(self, guild_id: int | None = None) -> dict[str, Any]:
        cfg = {
            "type": WELCOME_DOC_CONFIG,
            "enabled": False,
            "channel_id": 0,
            "dm_enabled": False,
            "delete_on_leave_enabled": False,
            "decorative_emoji_enabled": False,
            "auto_role_ids": [],
            "style": "complete",
            "render_mode": "components_v2",
            "dm_render_mode": "components_v2",
            "accent_color": DEFAULT_ACCENT,
            "accent_color_mode": "fixed",
            "media_url": "",
            "media_mode": "custom",
            "variants": [],
            "mode_configs": {},
            "public": dict(DEFAULT_PUBLIC),
            "embed": dict(DEFAULT_EMBED),
            "dm": dict(DEFAULT_DM),
            "webhook": self._default_webhook_config(),
            "invite_cache": {},
            "special_rules": [],
        }
        if guild_id is not None:
            cfg["guild_id"] = int(guild_id)
        return cfg

    def _normalize_embed_config(self, value: Any) -> dict[str, Any]:
        data = dict(value or {}) if isinstance(value, dict) else {}
        result = dict(DEFAULT_EMBED)
        for key in DEFAULT_EMBED:
            raw = str(data.get(key) or "")
            if key in {"content", "description"}:
                result[key] = raw[:MAX_TEMPLATE_LENGTH]
            elif key in {"title", "author_name"}:
                result[key] = raw[:256]
            elif key == "color":
                result[key] = _parse_hex(raw) if raw and HEX_RE.fullmatch(raw) else ""
            elif key == "footer_text":
                result[key] = raw[:2048]
            elif key.endswith("_url") or key in {"author_url", "title_url"}:
                result[key] = _clean_url(raw)
            elif key == "color_mode":
                result[key] = raw if raw in COLOR_MODE_LABELS else "fixed"
            elif key.endswith("_mode"):
                fallback = "custom" if key == "image_mode" else "none"
                result[key] = _image_mode(raw, fallback=fallback)
            else:
                result[key] = raw
        return result

    def _normalize_public_block(self, value: Any, *, default: dict[str, str], allow_empty: bool = False) -> dict[str, str]:
        result = {"title": "", "body": "", "footer": ""} if allow_empty else dict(default)
        incoming = dict(value or {}) if isinstance(value, dict) else {}
        for key in ("title", "body", "footer"):
            raw = str(incoming.get(key) or "")
            if key == "footer":
                raw = raw[:MAX_FOOTER_LENGTH]
            elif key == "title":
                raw = raw[:256]
            else:
                raw = raw[:MAX_TEMPLATE_LENGTH]
            if allow_empty:
                result[key] = raw
            elif key == "footer":
                result[key] = raw if key in incoming else str(default.get(key) or "")
            else:
                result[key] = raw or str(default.get(key) or "")
        return result

    def _normalize_role_ids(self, values: Any, *, limit: int = MAX_AUTO_ROLES) -> list[int]:
        role_ids: list[int] = []
        for raw in values or []:
            try:
                role_id = int(raw)
            except Exception:
                continue
            if role_id > 0 and role_id not in role_ids:
                role_ids.append(role_id)
            if len(role_ids) >= limit:
                break
        return role_ids

    def _normalize_webhook_config(self, value: Any) -> dict[str, Any]:
        base = self._default_webhook_config()
        data = dict(value or {}) if isinstance(value, dict) else {}
        base.update(data)
        base["enabled"] = bool(base.get("enabled", False))
        for key in ("channel_id", "webhook_id"):
            try:
                base[key] = int(base.get(key) or 0)
            except Exception:
                base[key] = 0
        base["webhook_token"] = str(base.get("webhook_token") or "")[:200]
        base["name"] = _safe_webhook_name(base.get("name"))
        if str(base.get("name_mode") or "fixed") not in WEBHOOK_NAME_LABELS:
            base["name_mode"] = "fixed"
        if str(base.get("avatar_mode") or "server") not in WEBHOOK_AVATAR_LABELS:
            base["avatar_mode"] = "server"
        base["avatar_url"] = _clean_url(base.get("avatar_url"))
        return base

    def _normalize_variant(self, variant: dict[str, Any] | None) -> dict[str, Any]:
        data = dict(variant or {})
        try:
            weight = int(data.get("weight") or 1)
        except Exception:
            weight = 1
        weight = max(1, min(100, weight))
        return {
            "id": str(data.get("id") or _new_variant_id())[:40],
            "name": str(data.get("name") or "Variação")[:MAX_VARIANT_NAME],
            "enabled": bool(data.get("enabled", True)),
            "weight": weight,
            "public": self._normalize_public_block(data.get("public"), default=DEFAULT_PUBLIC, allow_empty=True),
            "embed": self._normalize_embed_config(data.get("embed")),
            "style": str(data.get("style") or "inherit") if str(data.get("style") or "inherit") in {"inherit", *STYLE_LABELS.keys()} else "inherit",
            "accent_color": _parse_hex(data.get("accent_color")) if data.get("accent_color") else "",
            "accent_color_mode": str(data.get("accent_color_mode") or "inherit") if str(data.get("accent_color_mode") or "inherit") in {"inherit", *COLOR_MODE_LABELS.keys()} else "inherit",
            "media_url": _clean_url(data.get("media_url")),
        }

    def _variant_percentages(self, variants: list[dict[str, Any]]) -> dict[str, float]:
        active = [self._normalize_variant(v) for v in variants if bool(v.get("enabled", True))]
        total = sum(int(v.get("weight") or 1) for v in active) or 0
        if total <= 0:
            return {}
        return {str(v.get("id")): (int(v.get("weight") or 1) * 100.0 / total) for v in active}

    def _pick_variant(self, cfg: dict[str, Any]) -> dict[str, Any] | None:
        variants = [self._normalize_variant(v) for v in cfg.get("variants") or [] if bool(v.get("enabled", True))]
        if not variants:
            return None
        try:
            return random.choices(variants, weights=[max(1, int(v.get("weight") or 1)) for v in variants], k=1)[0]
        except Exception:
            return random.choice(variants)

    def _apply_variant(self, config: dict[str, Any], variant: dict[str, Any] | None) -> dict[str, Any]:
        cfg = self._normalize_config(config)
        if not variant:
            return cfg
        variant = self._normalize_variant(variant)
        public = dict(cfg.get("public") or DEFAULT_PUBLIC)
        for key, value in dict(variant.get("public") or {}).items():
            if str(value or "").strip():
                public[key] = str(value)
        cfg["public"] = public
        vembed = self._normalize_embed_config(variant.get("embed"))
        if _has_custom_embed(vembed):
            embed = self._normalize_embed_config(cfg.get("embed"))
            for key, value in vembed.items():
                if str(value or "") != str(DEFAULT_EMBED.get(key) or ""):
                    embed[key] = value
            cfg["embed"] = embed
        if str(variant.get("style") or "inherit") != "inherit":
            cfg["style"] = str(variant.get("style"))
        if variant.get("accent_color"):
            cfg["accent_color"] = _parse_hex(variant.get("accent_color"))
        if str(variant.get("accent_color_mode") or "inherit") != "inherit":
            cfg["accent_color_mode"] = str(variant.get("accent_color_mode"))
        if variant.get("media_url"):
            cfg["media_url"] = _clean_url(variant.get("media_url"))
        if str(variant.get("media_mode") or "custom") != "custom":
            cfg["media_mode"] = _media_mode(variant.get("media_mode"))
        return self._normalize_config(cfg)

    def _normalize_mode_state(self, mode: str, value: Any) -> dict[str, Any]:
        data = dict(value or {}) if isinstance(value, dict) else {}
        style = str(data.get("style") or "complete")
        if style not in STYLE_LABELS:
            style = "complete"
        accent_mode = str(data.get("accent_color_mode") or "fixed")
        if accent_mode not in COLOR_MODE_LABELS:
            accent_mode = "fixed"
        variants: list[dict[str, Any]] = []
        for raw in data.get("variants") or []:
            if isinstance(raw, dict):
                variants.append(self._normalize_variant(raw))
            if len(variants) >= MAX_WELCOME_VARIANTS:
                break
        return {
            "public": self._normalize_public_block(data.get("public"), default=DEFAULT_PUBLIC),
            "embed": self._normalize_embed_config(data.get("embed")),
            "style": style,
            "accent_color": _parse_hex(data.get("accent_color")),
            "accent_color_mode": accent_mode,
            "media_url": _clean_url(data.get("media_url")),
            "media_mode": _media_mode(data.get("media_mode")),
            "decorative_emoji_enabled": bool(data.get("decorative_emoji_enabled", False)),
            "variants": variants,
        }

    def _normalize_mode_configs(self, value: Any) -> dict[str, dict[str, Any]]:
        raw = dict(value or {}) if isinstance(value, dict) else {}
        return {mode: self._normalize_mode_state(mode, raw.get(mode)) for mode in RENDER_MODE_LABELS}

    def _extract_mode_state(self, cfg: dict[str, Any], mode: str | None = None) -> dict[str, Any]:
        return self._normalize_mode_state(str(mode or cfg.get("render_mode") or "components_v2"), {
            "public": cfg.get("public"),
            "embed": cfg.get("embed"),
            "style": cfg.get("style"),
            "accent_color": cfg.get("accent_color"),
            "accent_color_mode": cfg.get("accent_color_mode"),
            "media_url": cfg.get("media_url"),
            "media_mode": cfg.get("media_mode"),
            "decorative_emoji_enabled": cfg.get("decorative_emoji_enabled"),
            "variants": cfg.get("variants"),
        })

    def _mode_state_is_empty(self, state: dict[str, Any], mode: str) -> bool:
        state = self._normalize_mode_state(mode, state)
        variants = list(state.get("variants") or [])
        public_default = not _template_changed({"public": state.get("public") or {}})
        embed_default = not _has_custom_embed(state.get("embed"))
        common_default = (
            not variants
            and not bool(state.get("decorative_emoji_enabled", False))
            and str(state.get("style") or "complete") == "complete"
            and _parse_hex(state.get("accent_color")) == _parse_hex(DEFAULT_ACCENT)
            and str(state.get("accent_color_mode") or "fixed") == "fixed"
            and not _clean_url(state.get("media_url"))
            and _media_mode(state.get("media_mode")) == "custom"
        )
        if mode == "embed":
            return common_default and embed_default
        return common_default and public_default

    def _adapt_mode_state(self, cfg: dict[str, Any], source_mode: str, target_mode: str) -> dict[str, Any]:
        source = self._extract_mode_state(cfg, source_mode)
        public = dict(source.get("public") or DEFAULT_PUBLIC)
        embed = self._normalize_embed_config(source.get("embed"))
        if source_mode == "embed" and target_mode in {"components_v2", "normal"}:
            public = {
                "title": str(embed.get("title") or public.get("title") or DEFAULT_PUBLIC["title"]),
                "body": str(embed.get("description") or public.get("body") or DEFAULT_PUBLIC["body"]),
                "footer": str(embed.get("footer_text") or public.get("footer") or ""),
            }
            source["public"] = self._normalize_public_block(public, default=DEFAULT_PUBLIC)
            if embed.get("color"):
                source["accent_color"] = _parse_hex(embed.get("color"))
            image_url = _clean_url(embed.get("image_url"))
            if image_url:
                source["media_url"] = image_url
                source["media_mode"] = "custom"
            elif str(embed.get("image_mode") or "") == "avatar_stars":
                source["media_mode"] = "avatar_stars"
        elif source_mode in {"components_v2", "normal"} and target_mode == "embed":
            source["embed"] = self._normalize_embed_config({
                "content": "",
                "title": public.get("title") or "",
                "description": public.get("body") or "",
                "footer_text": public.get("footer") or "",
                "color": source.get("accent_color") or DEFAULT_ACCENT,
                "image_mode": source.get("media_mode") if str(source.get("media_mode") or "custom") == "avatar_stars" else "custom",
                "image_url": source.get("media_url") or "",
            })
        return self._normalize_mode_state(target_mode, source)

    def _apply_mode_state(self, cfg: dict[str, Any], mode: str, state: dict[str, Any]) -> dict[str, Any]:
        out = dict(cfg or {})
        state = self._normalize_mode_state(mode, state)
        out["render_mode"] = mode if mode in RENDER_MODE_LABELS else "components_v2"
        for key in ("public", "embed", "style", "accent_color", "accent_color_mode", "media_url", "media_mode", "decorative_emoji_enabled", "variants"):
            out[key] = deepcopy(state.get(key))
        return out

    def _sync_active_mode_config(self, config: dict[str, Any]) -> dict[str, Any]:
        cfg = self._normalize_config(config)
        mode = str(cfg.get("render_mode") or "components_v2")
        modes = self._normalize_mode_configs(cfg.get("mode_configs"))
        modes[mode] = self._extract_mode_state(cfg, mode)
        cfg["mode_configs"] = modes
        return cfg

    def _switch_public_mode(self, config: dict[str, Any], target_mode: str) -> dict[str, Any]:
        target_mode = str(target_mode or "components_v2")
        if target_mode not in RENDER_MODE_LABELS:
            target_mode = "components_v2"
        cfg = self._normalize_config(config)
        current_mode = str(cfg.get("render_mode") or "components_v2")
        modes = self._normalize_mode_configs(cfg.get("mode_configs"))
        modes[current_mode] = self._extract_mode_state(cfg, current_mode)
        target_state = modes.get(target_mode) or self._normalize_mode_state(target_mode, {})
        if self._mode_state_is_empty(target_state, target_mode):
            target_state = self._adapt_mode_state(cfg, current_mode, target_mode)
            modes[target_mode] = target_state
        cfg["mode_configs"] = modes
        cfg = self._apply_mode_state(cfg, target_mode, target_state)
        cfg["mode_configs"] = modes
        return self._normalize_config(cfg)

    def _normalize_rule(self, rule: dict[str, Any] | None) -> dict[str, Any]:
        data = dict(rule or {})
        match_type = str(data.get("match_type") or "invite_code")
        if match_type not in RULE_TYPE_LABELS:
            match_type = "invite_code"
        render_mode = str(data.get("render_mode") or "inherit")
        if render_mode not in {"inherit", *RENDER_MODE_LABELS.keys()}:
            render_mode = "inherit"
        style = str(data.get("style") or "inherit")
        if style not in {"inherit", *STYLE_LABELS.keys()}:
            style = "inherit"
        webhook = dict(data.get("webhook") or {})
        mode = str(webhook.get("mode") or "inherit")
        if mode not in {"inherit", "bot", "webhook"}:
            mode = "inherit"
        avatar_mode = str(webhook.get("avatar_mode") or "inherit")
        if avatar_mode not in {"inherit", *WEBHOOK_AVATAR_LABELS.keys()}:
            avatar_mode = "inherit"
        try:
            channel_id = int(data.get("channel_id") or 0)
        except Exception:
            channel_id = 0
        return {
            "id": str(data.get("id") or _new_rule_id())[:40],
            "name": str(data.get("name") or "Regra especial")[:MAX_RULE_NAME],
            "enabled": bool(data.get("enabled", True)),
            "match_type": match_type,
            "match_value": str(data.get("match_value") or "")[:100],
            "render_mode": render_mode,
            "channel_id": channel_id,
            "auto_role_ids": self._normalize_role_ids(data.get("auto_role_ids") or []),
            "style": style,
            "accent_color": _parse_hex(data.get("accent_color")) if data.get("accent_color") else "",
            "media_url": _clean_url(data.get("media_url")),
            "media_mode": _media_mode(data.get("media_mode")),
            "public": self._normalize_public_block(data.get("public"), default=DEFAULT_PUBLIC, allow_empty=True),
            "embed": self._normalize_embed_config(data.get("embed")),
            "webhook": {
                "mode": mode,
                "name": _safe_webhook_name(webhook.get("name"), "") if str(webhook.get("name") or "").strip() else "",
                "avatar_mode": avatar_mode,
                "avatar_url": _clean_url(webhook.get("avatar_url")),
            },
        }

    def _make_rule(self, *, name: str, match_type: str, match_value: str, rule_id: str | None = None, enabled: bool = True) -> dict[str, Any]:
        return self._normalize_rule({
            "id": rule_id or _new_rule_id(),
            "name": name or "Regra especial",
            "enabled": enabled,
            "match_type": match_type,
            "match_value": match_value,
        })

    def _normalize_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        base = self._default_config()
        cfg = dict(config or {})
        merged = {**base, **cfg}
        merged["public"] = self._normalize_public_block(merged.get("public"), default=DEFAULT_PUBLIC)
        merged["embed"] = self._normalize_embed_config(merged.get("embed"))
        merged["dm"] = self._normalize_public_block(merged.get("dm"), default=DEFAULT_DM)
        merged["auto_role_ids"] = self._normalize_role_ids(merged.get("auto_role_ids") or [])
        merged["enabled"] = bool(merged.get("enabled", False))
        merged["dm_enabled"] = bool(merged.get("dm_enabled", False))
        merged["delete_on_leave_enabled"] = bool(merged.get("delete_on_leave_enabled", False))
        merged["decorative_emoji_enabled"] = bool(merged.get("decorative_emoji_enabled", False))
        try:
            merged["channel_id"] = int(merged.get("channel_id") or 0)
        except Exception:
            merged["channel_id"] = 0
        merged["style"] = str(merged.get("style") or "complete") if str(merged.get("style") or "complete") in STYLE_LABELS else "complete"
        merged["render_mode"] = str(merged.get("render_mode") or "components_v2") if str(merged.get("render_mode") or "components_v2") in RENDER_MODE_LABELS else "components_v2"
        merged["dm_render_mode"] = str(merged.get("dm_render_mode") or merged["render_mode"]) if str(merged.get("dm_render_mode") or merged["render_mode"]) in RENDER_MODE_LABELS else merged["render_mode"]
        merged["accent_color"] = _parse_hex(merged.get("accent_color"))
        merged["accent_color_mode"] = str(merged.get("accent_color_mode") or "fixed") if str(merged.get("accent_color_mode") or "fixed") in COLOR_MODE_LABELS else "fixed"
        merged["media_url"] = _clean_url(merged.get("media_url"))
        merged["media_mode"] = _media_mode(merged.get("media_mode"))
        variants: list[dict[str, Any]] = []
        for raw in merged.get("variants") or []:
            if isinstance(raw, dict):
                variants.append(self._normalize_variant(raw))
            if len(variants) >= MAX_WELCOME_VARIANTS:
                break
        merged["variants"] = variants
        modes = self._normalize_mode_configs(merged.get("mode_configs"))
        active_mode = str(merged.get("render_mode") or "components_v2")
        raw_modes = merged.get("mode_configs") if isinstance(merged.get("mode_configs"), dict) else {}
        if active_mode not in raw_modes:
            modes[active_mode] = self._extract_mode_state(merged, active_mode)
        merged["mode_configs"] = modes
        merged["webhook"] = self._normalize_webhook_config(merged.get("webhook"))
        merged["invite_cache"] = self._normalize_invite_cache(merged.get("invite_cache"))
        rules: list[dict[str, Any]] = []
        for raw in merged.get("special_rules") or []:
            if isinstance(raw, dict):
                rules.append(self._normalize_rule(raw))
            if len(rules) >= MAX_SPECIAL_RULES:
                break
        merged["special_rules"] = rules
        merged["type"] = WELCOME_DOC_CONFIG
        return merged

    def _normalize_invite_cache(self, value: Any) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        if not isinstance(value, dict):
            return result
        for code, data in value.items():
            code_s = str(code or "")[:64]
            if not code_s:
                continue
            item = dict(data or {}) if isinstance(data, dict) else {}
            try:
                uses = int(item.get("uses") or 0)
            except Exception:
                uses = 0
            result[code_s] = {
                "uses": max(0, uses),
                "inviter_id": int(item.get("inviter_id") or 0),
                "inviter_name": str(item.get("inviter_name") or ""),
                "channel_id": int(item.get("channel_id") or 0),
                "channel_name": str(item.get("channel_name") or ""),
            }
        return result

    async def _get_config(self, guild_id: int) -> dict[str, Any]:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return self._normalize_config({"guild_id": int(guild_id)})
        doc = await db.coll.find_one({"type": WELCOME_DOC_CONFIG, "guild_id": int(guild_id)}, {"_id": 0})
        cfg = self._normalize_config(doc or {"guild_id": int(guild_id)})
        cfg["guild_id"] = int(guild_id)
        return cfg

    async def _save_config(self, guild_id: int, config: dict[str, Any]) -> bool:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return False
        cfg = self._sync_active_mode_config(config)
        cfg = self._normalize_config(cfg)
        cfg["guild_id"] = int(guild_id)
        cfg["type"] = WELCOME_DOC_CONFIG
        await db.coll.update_one({"type": WELCOME_DOC_CONFIG, "guild_id": int(guild_id)}, {"$set": cfg}, upsert=True)
        return True

    def _can_manage(self, member: Any) -> bool:
        perms = getattr(member, "guild_permissions", None)
        return bool(getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False))

    async def _resolve_text_channel(self, guild: discord.Guild | None, selected: Any) -> discord.TextChannel | discord.Thread | None:
        if guild is None or selected is None:
            return None
        if isinstance(selected, (discord.TextChannel, discord.Thread)):
            return selected
        channel_id = int(getattr(selected, "id", selected) or 0)
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    async def _configured_channel(self, guild: discord.Guild | None, cfg: dict[str, Any]) -> discord.TextChannel | discord.Thread | None:
        channel_id = int(cfg.get("channel_id") or 0)
        return await self._resolve_text_channel(guild, channel_id) if channel_id else None

    def _missing_channel_permissions(self, channel: discord.TextChannel | discord.Thread) -> str:
        guild = getattr(channel, "guild", None)
        me = getattr(guild, "me", None) if guild is not None else None
        if me is None:
            return "Não consegui conferir minhas permissões nesse canal."
        perms = channel.permissions_for(me)
        missing: list[str] = []
        if not perms.view_channel:
            missing.append("ver o canal")
        if not perms.send_messages:
            missing.append("enviar mensagens")
        if not perms.embed_links:
            missing.append("usar links/imagens")
        if missing:
            return "Preciso conseguir " + ", ".join(missing) + "."
        return ""

    def _safe_role_ids(self, guild: discord.Guild | None, roles: list[Any]) -> tuple[list[int], list[str]]:
        safe_role_ids: list[int] = []
        skipped: list[str] = []
        bot_member = guild.me if guild is not None else None
        for role in roles[:MAX_AUTO_ROLES]:
            if not isinstance(role, discord.Role):
                continue
            if role.is_default() or role.managed:
                skipped.append(role.mention)
                continue
            if bot_member is not None and role >= bot_member.top_role:
                skipped.append(role.mention)
                continue
            safe_role_ids.append(int(role.id))
        return safe_role_ids, skipped

    def _member_values(self, member: discord.Member | None, *, guild_id: int | None = None, invite_info: dict[str, Any] | None = None) -> dict[str, str]:
        guild = getattr(member, "guild", None) if member is not None else None
        if guild is None and guild_id:
            guild = self.bot.get_guild(int(guild_id))
        now_ts = int(datetime.now(timezone.utc).timestamp())
        created_at = getattr(member, "created_at", None) if member is not None else None
        created_ts = int(created_at.timestamp()) if created_at else now_ts
        invite = dict(invite_info or {})
        code = str(invite.get("code") or "convite desconhecido")
        inviter_id = int(invite.get("inviter_id") or 0)
        channel_id = int(invite.get("channel_id") or 0)
        inviter_name = str(invite.get("inviter_name") or "quem convidou")
        channel_name = str(invite.get("channel_name") or "canal")
        inviter_member = guild.get_member(inviter_id) if guild is not None and inviter_id else None
        guild_icon = getattr(guild, "icon", None) if guild is not None else None
        bot_user = getattr(self.bot, "user", None)
        member_avatar = str(member.display_avatar.url) if member is not None else ""
        inviter_avatar = str(inviter_member.display_avatar.url) if inviter_member is not None else ""
        server_icon = str(guild_icon.url) if guild_icon else ""
        bot_avatar = str(bot_user.display_avatar.url) if bot_user is not None else ""
        return {
            "membro": str(getattr(member, "display_name", None) or getattr(member, "name", None) or "novo membro"),
            "membro_mencao": str(getattr(member, "mention", None) or "@membro"),
            "usuario": str(getattr(member, "name", None) or getattr(member, "display_name", None) or "membro"),
            "usuario_id": str(getattr(member, "id", "") or ""),
            "membro_id": str(getattr(member, "id", "") or ""),
            "membro_avatar": member_avatar,
            "servidor": str(getattr(guild, "name", None) or "servidor"),
            "servidor_id": str(getattr(guild, "id", "") or guild_id or ""),
            "servidor_icone": server_icon,
            "contador": str(getattr(guild, "member_count", None) or ""),
            "criado_em": f"<t:{created_ts}:D>",
            "criado_relativo": f"<t:{created_ts}:R>",
            "entrou_em": f"<t:{now_ts}:F>",
            "convite_codigo": code,
            "convite": code,
            "convite_canal": channel_name if channel_id else "convite desconhecido",
            "convite_canal_mencao": _channel_mention(channel_id) if channel_id else "convite desconhecido",
            "convite_usos": str(invite.get("uses") or ""),
            "convidador": inviter_name if inviter_id else "convite desconhecido",
            "convidador_nome": inviter_name if inviter_id else "convite desconhecido",
            "convidador_mencao": _user_mention(inviter_id) if inviter_id else "convite desconhecido",
            "convidador_avatar": inviter_avatar,
            "bot_avatar": bot_avatar,
            "convite_desconhecido": "convite desconhecido",
        }

    def _replace_vars(self, text: str, values: dict[str, str]) -> str:
        def repl(match: re.Match[str]) -> str:
            key = match.group(1)
            return values.get(key, match.group(0))
        return VAR_RE.sub(repl, str(text or ""))

    def _build_welcome_text(self, cfg: dict[str, Any], *, member: discord.Member | None, guild_id: int | None, dm: bool = False, invite_info: dict[str, Any] | None = None) -> tuple[str, str, str]:
        values = self._member_values(member, guild_id=guild_id, invite_info=invite_info)
        source = dict(cfg.get("dm") or DEFAULT_DM) if dm else dict(cfg.get("public") or DEFAULT_PUBLIC)
        title = self._replace_vars(str(source.get("title") or ""), values).strip()
        body = self._replace_vars(str(source.get("body") or ""), values).strip()
        footer = self._replace_vars(str(source.get("footer") or ""), values).strip()
        return title, body, footer

    def _is_monochrome_pixels(self, pixels: list[tuple[int, int, int, float, float, float]]) -> bool:
        if not pixels:
            return False
        low_sat = sum(1 for _, _, _, brightness, sat, _ in pixels if sat <= 0.18)
        very_low_sat = sum(1 for _, _, _, brightness, sat, _ in pixels if sat <= 0.10)
        # Se a foto é majoritariamente preto/branco/cinza, isso é o estilo dela.
        # Não force uma cor colorida só porque existe um pequeno ruído saturado no avatar.
        return (low_sat / len(pixels)) >= 0.70 or (very_low_sat / len(pixels)) >= 0.58

    def _monochrome_color_from_pixels(self, pixels: list[tuple[int, int, int, float, float, float]], fallback: str = DEFAULT_ACCENT) -> tuple[int, int, int]:
        if not pixels:
            return self._rgb_from_hex(fallback)
        # Usa apenas pixels pouco saturados quando possível para respeitar imagens P&B.
        mono = [(r, g, b, brightness) for r, g, b, brightness, sat, _ in pixels if sat <= 0.20]
        source = mono or [(r, g, b, brightness) for r, g, b, brightness, _, _ in pixels]
        # Remove só extremos minúsculos de ruído; se preto/branco forem dominantes, ficam.
        source.sort(key=lambda item: item[3])
        cut = max(0, min(len(source) // 12, 24))
        trimmed = source[cut: len(source) - cut] if len(source) > cut * 2 + 4 else source
        if not trimmed:
            trimmed = source
        r = int(sum(px[0] for px in trimmed) / len(trimmed))
        g = int(sum(px[1] for px in trimmed) / len(trimmed))
        b = int(sum(px[2] for px in trimmed) / len(trimmed))
        # Mantém monocromático de verdade, sem puxar para azul/verde por ruído de compressão.
        gray = int(round((r * 0.299 + g * 0.587 + b * 0.114)))
        return (max(0, min(255, gray)), max(0, min(255, gray)), max(0, min(255, gray)))

    def _monochrome_palette_from_rgb(self, rgb: tuple[int, int, int], *, limit: int = 6) -> list[tuple[int, int, int]]:
        base = int(round((rgb[0] * 0.299 + rgb[1] * 0.587 + rgb[2] * 0.114)))
        # Pequenas variações de luz/sombra mantendo a família preto/branco/cinza.
        values = [base, min(255, int(base * 1.22 + 10)), max(0, int(base * 0.72)), min(255, int(base * 1.45 + 6)), max(0, int(base * 0.48)), min(255, int(base * 1.08 + 3))]
        palette: list[tuple[int, int, int]] = []
        for value in values:
            value = max(0, min(255, int(value)))
            item = (value, value, value)
            if item not in palette:
                palette.append(item)
            if len(palette) >= limit:
                break
        return palette or [rgb]

    async def _member_avatar_color(self, member: discord.Member | None, fallback: str = DEFAULT_ACCENT) -> str:
        if member is None or Image is None:
            return _parse_hex(fallback)
        asset = member.display_avatar.replace(size=64, static_format="png")
        cache_key = str(getattr(asset, "key", None) or asset.url)
        cached = self._avatar_color_cache.get(cache_key)
        if cached:
            return cached
        try:
            data = await asset.read()
            with Image.open(BytesIO(data)) as img:
                img = img.convert("RGBA").resize((32, 32))
                candidates: list[tuple[float, int, int, int]] = []
                fallback_pixels: list[tuple[int, int, int]] = []
                opaque_pixels: list[tuple[int, int, int, float, float, float]] = []
                for r, g, b, a in img.getdata():
                    if a < 90:
                        continue
                    brightness = (r + g + b) / 3
                    h, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                    opaque_pixels.append((r, g, b, brightness, sat, val))
                    if 8 <= brightness <= 248:
                        fallback_pixels.append((r, g, b))
                    if 28 <= brightness <= 232 and sat >= 0.22 and val >= 0.18:
                        score = sat * 0.72 + val * 0.28
                        candidates.append((score, r, g, b))
                if self._is_monochrome_pixels(opaque_pixels):
                    r, g, b = self._monochrome_color_from_pixels(opaque_pixels, fallback)
                elif candidates:
                    candidates.sort(reverse=True)
                    top = candidates[: max(12, len(candidates) // 6)]
                    r = int(sum(px[1] for px in top) / len(top))
                    g = int(sum(px[2] for px in top) / len(top))
                    b = int(sum(px[3] for px in top) / len(top))
                elif fallback_pixels:
                    r = int(sum(px[0] for px in fallback_pixels) / len(fallback_pixels))
                    g = int(sum(px[1] for px in fallback_pixels) / len(fallback_pixels))
                    b = int(sum(px[2] for px in fallback_pixels) / len(fallback_pixels))
                else:
                    return _parse_hex(fallback)
                result = f"#{r:02X}{g:02X}{b:02X}"
                self._avatar_color_cache[cache_key] = result
                if len(self._avatar_color_cache) > 256:
                    self._avatar_color_cache.pop(next(iter(self._avatar_color_cache)), None)
                return result
        except Exception:
            return _parse_hex(fallback)

    async def _with_dynamic_colors(self, config: dict[str, Any], *, member: discord.Member | None) -> dict[str, Any]:
        cfg = self._normalize_config(config)
        if str(cfg.get("accent_color_mode") or "fixed") == "member_avatar":
            cfg["accent_color"] = await self._member_avatar_color(member, cfg.get("accent_color") or DEFAULT_ACCENT)
        embed = self._normalize_embed_config(cfg.get("embed"))
        if str(embed.get("color_mode") or "fixed") == "member_avatar":
            embed["color"] = await self._member_avatar_color(member, embed.get("color") or cfg.get("accent_color") or DEFAULT_ACCENT)
            cfg["embed"] = embed
        return cfg

    def _rgb_from_hex(self, value: Any, fallback: str = DEFAULT_ACCENT) -> tuple[int, int, int]:
        raw = _parse_hex(value, fallback).lstrip("#")
        try:
            return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
        except Exception:
            return (88, 101, 242)

    async def _member_avatar_palette(self, member: discord.Member | None, fallback: str = DEFAULT_ACCENT, *, limit: int = 6) -> list[tuple[int, int, int]]:
        if member is None or Image is None:
            return [self._rgb_from_hex(fallback)]
        asset = member.display_avatar.replace(size=128, static_format="png")
        cache_key = str(getattr(asset, "key", None) or asset.url)
        cached = self._avatar_palette_cache.get(cache_key)
        if cached:
            return cached[:limit]
        try:
            data = await asset.read()
            with Image.open(BytesIO(data)) as img:
                resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
                img = img.convert("RGBA").resize((64, 64), resampling)
                buckets: dict[tuple[int, int, int], list[float]] = {}
                fallback_pixels: list[tuple[int, int, int]] = []
                opaque_pixels: list[tuple[int, int, int, float, float, float]] = []
                for r, g, b, a in img.getdata():
                    if a < 90:
                        continue
                    brightness = (r + g + b) / 3
                    h, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                    opaque_pixels.append((r, g, b, brightness, sat, val))
                    if 8 <= brightness <= 248:
                        fallback_pixels.append((r, g, b))
                    if 28 <= brightness <= 232 and sat >= 0.16 and val >= 0.16:
                        key = (r // 32, g // 32, b // 32)
                        item = buckets.setdefault(key, [0.0, 0.0, 0.0, 0.0, 0.0])
                        item[0] += 1
                        item[1] += r
                        item[2] += g
                        item[3] += b
                        item[4] += sat * 1.35 + val * 0.35
                if self._is_monochrome_pixels(opaque_pixels):
                    mono_base = self._monochrome_color_from_pixels(opaque_pixels, fallback)
                    palette = self._monochrome_palette_from_rgb(mono_base, limit=limit)
                    self._avatar_palette_cache[cache_key] = palette[:limit]
                    if len(self._avatar_palette_cache) > 128:
                        self._avatar_palette_cache.pop(next(iter(self._avatar_palette_cache)), None)
                    return palette[:limit]
                colors: list[tuple[float, int, int, int]] = []
                for item in buckets.values():
                    count = max(1.0, item[0])
                    r = int(item[1] / count)
                    g = int(item[2] / count)
                    b = int(item[3] / count)
                    score = count * 0.45 + item[4] * 18.0
                    colors.append((score, r, g, b))
                colors.sort(reverse=True)
                palette: list[tuple[int, int, int]] = []
                used_hues: list[float] = []
                for _, r, g, b in colors:
                    hue, sat, val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                    if any(abs(hue - old) < 0.045 or abs(abs(hue - old) - 1.0) < 0.045 for old in used_hues) and len(palette) >= 3:
                        continue
                    palette.append((r, g, b))
                    used_hues.append(hue)
                    if len(palette) >= limit:
                        break
                if not palette and fallback_pixels:
                    r = int(sum(px[0] for px in fallback_pixels) / len(fallback_pixels))
                    g = int(sum(px[1] for px in fallback_pixels) / len(fallback_pixels))
                    b = int(sum(px[2] for px in fallback_pixels) / len(fallback_pixels))
                    palette = [(r, g, b)]
                if not palette:
                    palette = [self._rgb_from_hex(fallback)]
                while len(palette) < 3:
                    base = palette[-1]
                    h, sat, val = colorsys.rgb_to_hsv(base[0] / 255, base[1] / 255, base[2] / 255)
                    if sat <= 0.10:
                        # Não transforme uma paleta cinza em azul/verde só para preencher slots.
                        palette.extend(self._monochrome_palette_from_rgb(base, limit=max(3, limit))[len(palette):])
                        break
                    h = (h + 0.09 * len(palette)) % 1.0
                    sat = min(1.0, max(0.35, sat + 0.08))
                    val = min(1.0, max(0.45, val + 0.05))
                    rr, gg, bb = colorsys.hsv_to_rgb(h, sat, val)
                    palette.append((int(rr * 255), int(gg * 255), int(bb * 255)))
                self._avatar_palette_cache[cache_key] = palette[:limit]
                if len(self._avatar_palette_cache) > 128:
                    self._avatar_palette_cache.pop(next(iter(self._avatar_palette_cache)), None)
                return palette[:limit]
        except Exception:
            return [self._rgb_from_hex(fallback)]

    def _recolor_star_template(self, palette: list[tuple[int, int, int]]) -> bytes | None:
        if Image is None or not STAR_SEPARATOR_ASSET.exists():
            return None
        try:
            with Image.open(STAR_SEPARATOR_ASSET) as src:
                img = src.convert("RGBA")
            width, height = img.size
            pixels = img.load()
            visited = bytearray(width * height)
            components: list[tuple[float, list[tuple[int, int]]]] = []
            for y in range(height):
                for x in range(width):
                    idx = y * width + x
                    if visited[idx]:
                        continue
                    visited[idx] = 1
                    if pixels[x, y][3] <= 18:
                        continue
                    stack = [(x, y)]
                    points: list[tuple[int, int]] = []
                    sx = 0
                    while stack:
                        px, py = stack.pop()
                        points.append((px, py))
                        sx += px
                        for nx, ny in ((px + 1, py), (px - 1, py), (px, py + 1), (px, py - 1)):
                            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                                continue
                            nidx = ny * width + nx
                            if visited[nidx]:
                                continue
                            visited[nidx] = 1
                            if pixels[nx, ny][3] > 18:
                                stack.append((nx, ny))
                    if points:
                        components.append((sx / max(1, len(points)), points))
            components.sort(key=lambda item: item[0])
            palette = palette or [self._rgb_from_hex(DEFAULT_ACCENT)]
            for idx, (_, points) in enumerate(components):
                r, g, b = palette[idx % len(palette)]
                for x, y in points:
                    _, _, _, a = pixels[x, y]
                    pixels[x, y] = (r, g, b, a)
            out = BytesIO()
            img.save(out, format="PNG", optimize=True)
            return out.getvalue()
        except Exception as exc:
            log.debug("não consegui recolorir estrelas de boas-vindas: %r", exc)
            return None

    async def _star_separator_file(self, member: discord.Member | None, fallback: str = DEFAULT_ACCENT) -> discord.File | None:
        if not STAR_SEPARATOR_ASSET.exists():
            return None
        avatar_key = "default"
        if member is not None:
            asset = member.display_avatar.replace(size=128, static_format="png")
            avatar_key = str(getattr(asset, "key", None) or asset.url)
        cache_key = f"{avatar_key}:{_parse_hex(fallback)}"
        data = self._star_image_cache.get(cache_key)
        if data is None:
            palette = await self._member_avatar_palette(member, fallback)
            data = self._recolor_star_template(palette)
            if data is None:
                try:
                    data = STAR_SEPARATOR_ASSET.read_bytes()
                except Exception:
                    return None
            self._star_image_cache[cache_key] = data
            if len(self._star_image_cache) > 128:
                self._star_image_cache.pop(next(iter(self._star_image_cache)), None)
        return discord.File(BytesIO(data), filename=STAR_SEPARATOR_FILENAME)

    def _drop_dynamic_star_media(self, config: dict[str, Any], *, mode: str) -> dict[str, Any]:
        cfg = self._normalize_config(config)
        if mode == "components_v2" and _media_mode(cfg.get("media_mode")) == "avatar_stars":
            cfg["media_mode"] = "none"
            cfg["media_url"] = ""
        embed = self._normalize_embed_config(cfg.get("embed"))
        if mode == "embed" and str(embed.get("image_mode") or "") == "avatar_stars":
            embed["image_mode"] = "none"
            embed["image_url"] = ""
            cfg["embed"] = embed
        return cfg

    async def _prepare_dynamic_media(self, config: dict[str, Any], *, member: discord.Member | None, mode: str, dm: bool = False) -> tuple[dict[str, Any], list[discord.File]]:
        cfg = self._normalize_config(config)
        if dm:
            return cfg, []
        files: list[discord.File] = []
        needs_stars = False
        if mode == "components_v2" and _media_mode(cfg.get("media_mode")) == "avatar_stars":
            needs_stars = True
            cfg["media_url"] = f"attachment://{STAR_SEPARATOR_FILENAME}"
            cfg["media_mode"] = "custom"
        embed = self._normalize_embed_config(cfg.get("embed"))
        if mode == "embed" and str(embed.get("image_mode") or "") == "avatar_stars":
            needs_stars = True
            embed["image_mode"] = "custom"
            embed["image_url"] = f"attachment://{STAR_SEPARATOR_FILENAME}"
            cfg["embed"] = embed
        if needs_stars:
            star_file = None
            try:
                star_file = await self._star_separator_file(member, cfg.get("accent_color") or DEFAULT_ACCENT)
            except Exception as exc:
                log.warning("falha ao preparar preset de estrelas de boas-vindas; enviando sem imagem dinâmica: %r", exc)
            if star_file is not None:
                files.append(star_file)
            else:
                # Imagem decorativa nunca pode impedir a mensagem de boas-vindas.
                if mode == "components_v2":
                    cfg["media_url"] = ""
                    cfg["media_mode"] = "none"
                elif mode == "embed":
                    embed = self._normalize_embed_config(cfg.get("embed"))
                    if str(embed.get("image_url") or "").startswith("attachment://"):
                        embed["image_url"] = ""
                        embed["image_mode"] = "none"
                        cfg["embed"] = embed
        return cfg, files

    def _make_welcome_container(self, config: dict[str, Any], *, member: discord.Member | None, guild_id: int | None = None, dm: bool = False, invite_info: dict[str, Any] | None = None) -> discord.ui.Container:
        cfg = self._normalize_config(config)
        title, body, footer = self._build_welcome_text(cfg, member=member, guild_id=guild_id, dm=dm, invite_info=invite_info)
        style = str(cfg.get("style") or "complete")
        children: list[discord.ui.Item[Any]] = []
        if title:
            children.append(discord.ui.TextDisplay(_trim(f"# {title}", 900)))
        if body:
            children.append(discord.ui.TextDisplay(_trim(body, 1800 if style != "compact" else 900)))
        media_url = _clean_url(cfg.get("media_url")) if not dm else ""
        if media_url and style == "complete":
            children.extend([discord.ui.Separator(), discord.ui.MediaGallery(discord.MediaGalleryItem(media_url))])
        if footer and style != "compact":
            children.extend([discord.ui.Separator(), discord.ui.TextDisplay(_trim(footer, 500))])
        if not children:
            children.append(discord.ui.TextDisplay("# Bem-vindo(a)!"))
        return discord.ui.Container(*children, accent_color=_color_from_hex(cfg.get("accent_color")))

    def _make_components_view(self, config: dict[str, Any], *, member: discord.Member, dm: bool = False, invite_info: dict[str, Any] | None = None) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(self._make_welcome_container(config, member=member, guild_id=int(member.guild.id), dm=dm, invite_info=invite_info))
        return view

    def _image_url_from_mode(self, mode: str, custom_url: str, *, member: discord.Member | None, guild_id: int | None = None, invite_info: dict[str, Any] | None = None) -> str:
        mode = _image_mode(mode, fallback="none")
        if mode == "none":
            return ""
        if mode == "custom":
            return _clean_url(custom_url)
        guild = getattr(member, "guild", None) if member is not None else None
        if guild is None and guild_id:
            guild = self.bot.get_guild(int(guild_id))
        if mode == "member" and member is not None:
            return str(member.display_avatar.url)
        if mode == "inviter" and guild is not None:
            inviter_id = int((invite_info or {}).get("inviter_id") or 0)
            inviter = guild.get_member(inviter_id) if inviter_id else None
            if inviter is not None:
                return str(inviter.display_avatar.url)
        if mode == "server" and guild is not None and getattr(guild, "icon", None):
            return str(guild.icon.url)
        if mode == "bot":
            bot_user = getattr(self.bot, "user", None)
            if bot_user is not None:
                return str(bot_user.display_avatar.url)
        return ""

    def _make_embed_payload(self, config: dict[str, Any], *, member: discord.Member | None, guild_id: int | None = None, dm: bool = False, invite_info: dict[str, Any] | None = None) -> tuple[str, discord.Embed]:
        cfg = self._normalize_config(config)
        title, body, footer = self._build_welcome_text(cfg, member=member, guild_id=guild_id, dm=dm, invite_info=invite_info)
        values = self._member_values(member, guild_id=guild_id, invite_info=invite_info)
        embed_cfg = self._normalize_embed_config(cfg.get("embed")) if not dm else dict(DEFAULT_EMBED)
        content = self._replace_vars(str(embed_cfg.get("content") or ""), values).strip() if not dm else ""
        embed_title = self._replace_vars(str(embed_cfg.get("title") or ""), values).strip()
        embed_desc = self._replace_vars(str(embed_cfg.get("description") or body), values).strip()
        embed_footer_source = footer if dm else str(embed_cfg.get("footer_text") or "")
        embed_footer = self._replace_vars(str(embed_footer_source or ""), values).strip()
        embed_color = embed_cfg.get("color") or cfg.get("accent_color")
        embed = discord.Embed(title=_trim(embed_title, 256) or None, description=_trim(embed_desc, 4000) or None, color=_color_from_hex(embed_color))
        title_url = _clean_url(self._replace_vars(str(embed_cfg.get("title_url") or ""), values))
        if title_url:
            embed.url = title_url
        author_name = self._replace_vars(str(embed_cfg.get("author_name") or ""), values).strip()
        if author_name:
            author_icon = self._image_url_from_mode(str(embed_cfg.get("author_icon_mode") or "none"), self._replace_vars(str(embed_cfg.get("author_icon_url") or ""), values), member=member, guild_id=guild_id, invite_info=invite_info)
            author_url = _clean_url(self._replace_vars(str(embed_cfg.get("author_url") or ""), values))
            kwargs: dict[str, Any] = {"name": _trim(author_name, 256)}
            if author_icon:
                kwargs["icon_url"] = author_icon
            if author_url:
                kwargs["url"] = author_url
            embed.set_author(**kwargs)
        thumbnail_url = self._image_url_from_mode(str(embed_cfg.get("thumbnail_mode") or "none"), self._replace_vars(str(embed_cfg.get("thumbnail_url") or ""), values), member=member, guild_id=guild_id, invite_info=invite_info)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        custom_image = self._replace_vars(str(embed_cfg.get("image_url") or cfg.get("media_url") or ""), values)
        image_url = self._image_url_from_mode(str(embed_cfg.get("image_mode") or "custom"), custom_image, member=member, guild_id=guild_id, invite_info=invite_info)
        if image_url:
            embed.set_image(url=image_url)
        if embed_footer:
            footer_icon = self._image_url_from_mode(str(embed_cfg.get("footer_icon_mode") or "none"), self._replace_vars(str(embed_cfg.get("footer_icon_url") or ""), values), member=member, guild_id=guild_id, invite_info=invite_info)
            if footer_icon:
                embed.set_footer(text=_trim(embed_footer, 2048), icon_url=footer_icon)
            else:
                embed.set_footer(text=_trim(embed_footer, 2048))
        return _trim(content, 1990), embed

    def _make_embed(self, config: dict[str, Any], *, member: discord.Member | None, guild_id: int | None = None, dm: bool = False, invite_info: dict[str, Any] | None = None) -> discord.Embed:
        return self._make_embed_payload(config, member=member, guild_id=guild_id, dm=dm, invite_info=invite_info)[1]

    def _make_normal_content(self, config: dict[str, Any], *, member: discord.Member | None, guild_id: int | None = None, dm: bool = False, invite_info: dict[str, Any] | None = None) -> str:
        title, body, footer = self._build_welcome_text(config, member=member, guild_id=guild_id, dm=dm, invite_info=invite_info)
        parts = []
        if title:
            parts.append(f"**{title}**")
        if body:
            parts.append(body)
        if dm and footer:
            parts.append(footer)
        return _trim("\n\n".join(parts) or "Bem-vindo(a)!", 1990)

    def _append_render_preview(self, view: discord.ui.LayoutView, config: dict[str, Any], *, member: discord.Member | None, guild_id: int, dm: bool = False):
        cfg = self._normalize_config(config)
        mode = str(cfg.get("dm_render_mode") if dm else cfg.get("render_mode") or "components_v2")
        if mode == "components_v2":
            view.add_item(self._make_welcome_container(cfg, member=member, guild_id=guild_id, dm=dm))
            return
        if mode == "normal":
            content = self._make_normal_content(cfg, member=member, guild_id=guild_id, dm=dm)
            view.add_item(discord.ui.Container(discord.ui.TextDisplay(_trim("## Mensagem normal\n" + content)), accent_color=_color_from_hex(cfg.get("accent_color"))))
            return
        view.add_item(discord.ui.Container(
            discord.ui.TextDisplay("## Embed\nO preview real em embed é enviado como uma mensagem separada, para aparecer igual ao Discord mostra."),
            accent_color=_color_from_hex((cfg.get("embed") or {}).get("color") or cfg.get("accent_color")),
        ))

    def _emoji_tokens_from_config(self, cfg: dict[str, Any], *, mode: str, dm: bool = False, limit: int = DEFAULT_DECORATIVE_EMOJI_LIMIT) -> list[dict[str, Any]]:
        """Detecta emojis customizados usados na mensagem.

        O limite vale para emojis base diferentes, não para quantas vezes o mesmo emoji
        aparece. Se o mesmo emoji aparece em várias linhas, criamos um emoji temporário
        só e substituímos todas as aparições conhecidas dele. Também agrupamos pelo ID
        do emoji, porque o mesmo emoji pode aparecer com nomes diferentes no texto salvo.
        """
        if dm:
            return []
        try:
            effective_limit = max(0, min(MAX_DECORATIVE_EMOJIS, int(limit or DEFAULT_DECORATIVE_EMOJI_LIMIT)))
        except Exception:
            effective_limit = DEFAULT_DECORATIVE_EMOJI_LIMIT
        if effective_limit <= 0:
            return []
        mode = str(mode or cfg.get("render_mode") or "components_v2")
        texts: list[str] = []
        public = dict(cfg.get("public") or {})
        embed = self._normalize_embed_config(cfg.get("embed"))
        if mode == "embed":
            for key in ("content", "author_name", "title", "description", "footer_text"):
                texts.append(str(embed.get(key) or ""))
            # Se a descrição do embed estiver vazia, o corpo público vira fallback.
            if not str(embed.get("description") or ""):
                texts.extend(str(public.get(key) or "") for key in ("title", "body", "footer"))
        else:
            texts.extend(str(public.get(key) or "") for key in ("title", "body", "footer"))

        found: list[dict[str, Any]] = []
        by_key: dict[str, dict[str, Any]] = {}
        for text in texts:
            for match in CUSTOM_EMOJI_RE.finditer(str(text or "")):
                raw = match.group(0)
                emoji_id = str(match.group(3) or "")
                key = f"{'a' if bool(match.group(1)) else 's'}:{emoji_id}"
                item = by_key.get(key)
                if item is not None:
                    raws = item.setdefault("raw_variants", [])
                    if raw not in raws:
                        raws.append(raw)
                    continue
                if len(found) >= effective_limit:
                    # O restante fica original. Nunca removemos nem trocamos por texto vazio.
                    continue
                item = {
                    "raw": raw,
                    "raw_variants": [raw],
                    "key": key,
                    "animated": bool(match.group(1)),
                    "name": str(match.group(2) or "emoji")[:32],
                    "id": emoji_id,
                }
                by_key[key] = item
                found.append(item)
        return found

    def _replace_emoji_tokens_in_config(self, cfg: dict[str, Any], replacements: dict[str, str], *, mode: str, dm: bool = False) -> dict[str, Any]:
        """Substitui emojis decorativos sem corromper fallback.

        `replacements` aceita duas formas:
        - chave raw (`<:nome:id>` / `<a:nome:id>`) para compatibilidade;
        - chave `id:<emoji_id>` para trocar globalmente qualquer ocorrência daquele ID,
          mesmo que o nome salvo no texto seja diferente.

        Se um emoji não tiver replacement confirmado, o texto fica exatamente como estava.
        """
        if not replacements or dm:
            return cfg
        out = self._normalize_config(cfg)
        id_replacements: dict[str, str] = {}
        raw_replacements: dict[str, str] = {}
        for old, new in (replacements or {}).items():
            old_s = str(old or "")
            new_s = str(new or "")
            if not old_s or not new_s:
                continue
            if old_s.startswith("id:"):
                emoji_id = old_s[3:]
                if re.fullmatch(r"\d{15,25}", emoji_id):
                    id_replacements[emoji_id] = new_s
            else:
                raw_replacements[old_s] = new_s

        def repl(text: Any) -> str:
            value = str(text or "")
            if id_replacements:
                def by_id(match: re.Match[str]) -> str:
                    emoji_id = str(match.group(3) or "")
                    return id_replacements.get(emoji_id, match.group(0))
                value = CUSTOM_EMOJI_RE.sub(by_id, value)
            # Raw fallback para qualquer variação não coberta por ID.
            for old, new in raw_replacements.items():
                value = value.replace(old, new)
            return value

        public = dict(out.get("public") or {})
        for key in ("title", "body", "footer"):
            public[key] = repl(public.get(key))
        out["public"] = public
        embed = self._normalize_embed_config(out.get("embed"))
        for key in ("content", "author_name", "title", "description", "footer_text"):
            embed[key] = repl(embed.get(key))
        out["embed"] = embed
        return out

    def _emoji_cdn_url(self, emoji: dict[str, Any]) -> str:
        ext = "gif" if bool(emoji.get("animated")) else "png"
        return f"https://cdn.discordapp.com/emojis/{emoji.get('id')}.{ext}?size=128&quality=lossless"

    def _fetch_url_bytes_sync(self, url: str, *, timeout: float = 4.0, limit: int = 900_000) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "CoreWelcomeBot/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(limit + 1)
        if len(data) > limit:
            raise RuntimeError("asset grande demais")
        return data

    async def _fetch_custom_emoji_bytes(self, emoji: dict[str, Any]) -> bytes:
        return await asyncio.to_thread(self._fetch_url_bytes_sync, self._emoji_cdn_url(emoji), timeout=4.0, limit=900_000)

    def _mix_rgb(self, first: tuple[int, int, int], second: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
        amount = max(0.0, min(1.0, float(amount)))
        return (
            max(0, min(255, int(round(first[0] * (1.0 - amount) + second[0] * amount)))),
            max(0, min(255, int(round(first[1] * (1.0 - amount) + second[1] * amount)))),
            max(0, min(255, int(round(first[2] * (1.0 - amount) + second[2] * amount)))),
        )

    def _adjust_rgb_hsv(self, rgb: tuple[int, int, int], *, sat_mul: float = 1.0, val_mul: float = 1.0, hue_shift: float = 0.0) -> tuple[int, int, int]:
        h, sat, val = colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)
        h = (h + hue_shift) % 1.0
        sat = max(0.0, min(1.0, sat * sat_mul))
        val = max(0.0, min(1.0, val * val_mul))
        r, g, b = colorsys.hsv_to_rgb(h, sat, val)
        return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))

    def _subtle_emoji_palette(self, base_rgb: tuple[int, int, int], avatar_palette: list[tuple[int, int, int]] | None = None) -> list[tuple[int, int, int]]:
        """Cria uma paleta coerente com a cor final da mensagem.

        A cor principal do emoji é sempre a cor efetiva do embed/visual. As cores do
        avatar entram só como nuances discretas, para não virar arco-íris nem fugir do
        tema escolhido pelo servidor.
        """
        palette: list[tuple[int, int, int]] = [base_rgb]
        palette.append(self._adjust_rgb_hsv(base_rgb, sat_mul=0.92, val_mul=1.24))
        palette.append(self._adjust_rgb_hsv(base_rgb, sat_mul=1.06, val_mul=0.68))
        base_h, base_s, base_v = colorsys.rgb_to_hsv(base_rgb[0] / 255, base_rgb[1] / 255, base_rgb[2] / 255)
        for raw in avatar_palette or []:
            try:
                ah, asat, aval = colorsys.rgb_to_hsv(raw[0] / 255, raw[1] / 255, raw[2] / 255)
            except Exception:
                continue
            # Mantém a variação perto da cor principal. Mesmo se o avatar tiver uma cor
            # muito diferente, usamos só uma influência pequena.
            diff = ((ah - base_h + 0.5) % 1.0) - 0.5
            hue_shift = max(-0.035, min(0.035, diff * 0.18))
            sat_mul = 0.96 + max(-0.10, min(0.10, (asat - base_s) * 0.18))
            val_mul = 0.96 + max(-0.12, min(0.12, (aval - base_v) * 0.22))
            candidate = self._adjust_rgb_hsv(base_rgb, sat_mul=sat_mul, val_mul=val_mul, hue_shift=hue_shift)
            if candidate not in palette:
                palette.append(candidate)
            if len(palette) >= 6:
                break
        while len(palette) < 4:
            shift = 0.018 * len(palette)
            palette.append(self._adjust_rgb_hsv(base_rgb, sat_mul=1.0, val_mul=1.0 + (0.06 if len(palette) % 2 else -0.06), hue_shift=shift))
        return palette[:6]

    def _palette_is_mostly_monochrome(self, palette: list[tuple[int, int, int]] | None) -> bool:
        values = list(palette or [])[:6]
        if not values:
            return False
        low_sat = 0
        for r, g, b in values:
            try:
                _h, sat, _val = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            except Exception:
                continue
            if sat < 0.14:
                low_sat += 1
        return low_sat >= max(1, len(values) - 1)

    def _hex_palette_from_rgb(self, palette: list[tuple[int, int, int]]) -> list[str]:
        return [f"#{r:02X}{g:02X}{b:02X}" for r, g, b in (palette or [])]

    def _palette_from_hex_list(self, values: Any, fallback: tuple[int, int, int]) -> list[tuple[int, int, int]]:
        result: list[tuple[int, int, int]] = []
        if isinstance(values, list):
            for item in values[:8]:
                try:
                    result.append(self._rgb_from_hex(item, f"#{fallback[0]:02X}{fallback[1]:02X}{fallback[2]:02X}"))
                except Exception:
                    continue
        return result or [fallback]

    def _fit_emoji_canvas_frame(self, frame: Any, *, canvas_size: int = 128) -> Any:
        """Ajusta o canvas inteiro para 128x128 preservando padding/posição.

        Não recorta a área visível. Isso é importante porque alguns emojis têm espaço
        transparente proposital; o recolorido deve manter o tamanho visual original e
        não crescer nem virar pontinho por causa de geometria diferente.
        """
        if Image is None:
            raise RuntimeError("Pillow indisponível")
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
        rgba = frame.convert("RGBA")
        if rgba.size == (canvas_size, canvas_size):
            return rgba.copy()
        width, height = rgba.size
        if width <= 0 or height <= 0:
            return Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        scale = min(canvas_size / float(width), canvas_size / float(height))
        new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        resized = rgba.resize(new_size, resampling)
        canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        canvas.alpha_composite(resized, ((canvas_size - resized.width) // 2, (canvas_size - resized.height) // 2))
        return canvas

    def _recolor_rgba_image(self, img: Any, rgb: tuple[int, int, int], palette: list[tuple[int, int, int]] | None = None) -> Any:
        img = img.convert("RGBA")
        px = img.load()
        base = rgb
        usable_palette = palette or [base]
        light = usable_palette[1] if len(usable_palette) > 1 else self._adjust_rgb_hsv(base, sat_mul=0.92, val_mul=1.22)
        dark = usable_palette[2] if len(usable_palette) > 2 else self._adjust_rgb_hsv(base, sat_mul=1.04, val_mul=0.68)
        accents = usable_palette[3:] or [base]
        width, height = img.size
        for y in range(height):
            for x in range(width):
                r, g, b, a = px[x, y]
                if a < 8:
                    continue
                lum = max(0.0, min(1.0, (r * 0.299 + g * 0.587 + b * 0.114) / 255.0))
                if lum < 0.50:
                    target = self._mix_rgb(dark, base, lum / 0.50)
                else:
                    target = self._mix_rgb(base, light, (lum - 0.50) / 0.50)
                # Pequena nuance da paleta perto da cor principal. A influência é baixa
                # para preservar um tema único baseado na cor do embed.
                try:
                    oh, osat, oval = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                    if accents and osat > 0.08:
                        accent = accents[(int(oh * 12) + (x // 24) + (y // 24)) % len(accents)]
                        target = self._mix_rgb(target, accent, 0.10)
                except Exception:
                    pass
                px[x, y] = (target[0], target[1], target[2], a)
        return img

    def _save_static_emoji_png(self, img: Any) -> bytes:
        candidate = self._fit_emoji_canvas_frame(img, canvas_size=128)
        out = BytesIO()
        candidate.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        if len(data) <= DISCORD_EMOJI_MAX_BYTES:
            return data
        # PNG de 128x128 normalmente fica muito abaixo disso; se não ficar, reduz cores
        # sem mexer na geometria visual.
        quantized = candidate.convert("P", palette=Image.Palette.ADAPTIVE, colors=96).convert("RGBA") if Image is not None else candidate
        out = BytesIO()
        quantized.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        if len(data) <= DISCORD_EMOJI_MAX_BYTES:
            return data
        raise RuntimeError("emoji estático ficou maior que 256 KiB")

    def _save_animated_emoji_gif(self, frames: list[Any], durations: list[int]) -> bytes | None:
        if not frames:
            return None
        normalized_frames = [self._fit_emoji_canvas_frame(frame, canvas_size=128) for frame in frames]
        for step in (1, 2, 3, 4, 5, 6, 8, 10):
            selected = [frame for idx, frame in enumerate(normalized_frames) if idx % step == 0]
            selected_durations = [max(20, min(500, int((durations[idx] if idx < len(durations) else 80) * step))) for idx in range(len(normalized_frames)) if idx % step == 0]
            if not selected:
                continue
            out = BytesIO()
            selected[0].save(
                out,
                format="GIF",
                save_all=True,
                append_images=selected[1:],
                duration=selected_durations,
                loop=0,
                optimize=True,
                disposal=2,
            )
            data = out.getvalue()
            if len(data) <= DISCORD_EMOJI_MAX_BYTES:
                return data
        return None

    def _normalize_emoji_upload_bytes_sync(self, raw: bytes, *, animated: bool) -> tuple[bytes, str]:
        """Garante formato aceito pelo Discord preservando o tamanho visual original."""
        if Image is None:
            raise RuntimeError("Pillow indisponível")
        with Image.open(BytesIO(raw)) as img:
            if animated and getattr(img, "is_animated", False) and ImageSequence is not None:
                raw_frames = [frame.convert("RGBA") for frame in ImageSequence.Iterator(img)]
                durations = [int(getattr(frame, "info", {}).get("duration") or img.info.get("duration") or 80) for frame in ImageSequence.Iterator(img)]
                frames = [self._fit_emoji_canvas_frame(frame) for frame in raw_frames]
                data = self._save_animated_emoji_gif(frames, durations)
                if data is not None:
                    return data, "gif"
                return self._save_static_emoji_png(frames[0]), "png"
            return self._save_static_emoji_png(img.convert("RGBA")), "png"

    async def _normalize_emoji_upload_item(self, item: dict[str, Any]) -> dict[str, Any] | None:
        try:
            raw_b64 = str(item.get("data_b64") or "")
            if not raw_b64:
                return None
            raw = base64.b64decode(raw_b64)
            animated = str(item.get("format") or "").lower() == "gif" or bool(item.get("animated"))
            data, fmt = await asyncio.to_thread(self._normalize_emoji_upload_bytes_sync, raw, animated=animated)
            return {**item, "data_b64": base64.b64encode(data).decode("ascii"), "format": fmt, "animated": fmt == "gif", "size": len(data)}
        except Exception as exc:
            log.debug("não consegui normalizar emoji temporário antes do upload: %r", exc)
            return None

    def _recolor_emoji_bytes_local_sync(self, raw: bytes, *, animated: bool, color_hex: str, palette: list[tuple[int, int, int]] | None = None) -> tuple[bytes, str]:
        if Image is None:
            raise RuntimeError("Pillow indisponível")
        base_rgb = self._rgb_from_hex(color_hex)
        subtle_palette = palette or self._subtle_emoji_palette(base_rgb, [])
        with Image.open(BytesIO(raw)) as img:
            if animated and getattr(img, "is_animated", False) and ImageSequence is not None:
                raw_frames = [frame.convert("RGBA") for frame in ImageSequence.Iterator(img)]
                frames = [self._recolor_rgba_image(self._fit_emoji_canvas_frame(frame), base_rgb, subtle_palette) for frame in raw_frames]
                durations = [int(getattr(frame, "info", {}).get("duration") or img.info.get("duration") or 80) for frame in ImageSequence.Iterator(img)]
                data = self._save_animated_emoji_gif(frames, durations)
                if data is not None:
                    return data, "gif"
                return self._save_static_emoji_png(frames[0]), "png"
            fitted = self._fit_emoji_canvas_frame(img.convert("RGBA"))
            out_img = self._recolor_rgba_image(fitted, base_rgb, subtle_palette)
            return self._save_static_emoji_png(out_img), "png"

    async def _recolor_emoji_bytes_local(self, raw: bytes, *, animated: bool, color_hex: str, palette: list[tuple[int, int, int]] | None = None) -> tuple[bytes, str]:
        return await asyncio.to_thread(self._recolor_emoji_bytes_local_sync, raw, animated=animated, color_hex=color_hex, palette=palette)

    def _phone_worker_base_url(self) -> str:
        enabled = str(os.getenv("PHONE_WORKER_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on", "sim"}
        host = str(os.getenv("PHONE_WORKER_HOST") or "").strip()
        if not enabled or not host:
            return ""
        scheme = str(os.getenv("PHONE_WORKER_SCHEME") or "http").strip() or "http"
        try:
            port = int(str(os.getenv("PHONE_WORKER_PORT") or "8766"))
        except Exception:
            port = 8766
        return f"{scheme}://{host}:{port}"

    async def _worker_recolor_emojis(self, emojis: list[dict[str, Any]], *, color_hex: str, palette_hex: list[str] | None = None, limit: int = DEFAULT_DECORATIVE_EMOJI_LIMIT, monochrome: bool = False) -> list[dict[str, Any]] | None:
        base_url = self._phone_worker_base_url()
        token = str(os.getenv("PHONE_WORKER_TOKEN") or "").strip()
        if not base_url or not token or not emojis:
            return None
        worker_key = base_url
        if int(self._emoji_worker_active.get(worker_key, 0) or 0) >= 2:
            return None
        self._emoji_worker_active[worker_key] = int(self._emoji_worker_active.get(worker_key, 0) or 0) + 1
        try:
            effective_limit = max(0, min(MAX_DECORATIVE_EMOJIS, int(limit or DEFAULT_DECORATIVE_EMOJI_LIMIT)))
            payload = json.dumps({"task": "emoji_recolor", "color": color_hex, "palette": palette_hex or [], "monochrome": bool(monochrome), "emojis": emojis[:effective_limit]}).encode("utf-8")
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
            def post() -> dict[str, Any]:
                req = urllib.request.Request(f"{base_url}/task", data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=7.0) as resp:
                    return json.loads(resp.read().decode("utf-8") or "{}")
            data = await asyncio.to_thread(post)
            if not isinstance(data, dict) or data.get("ok") is False:
                return None
            items = data.get("items") if isinstance(data.get("items"), list) else []
            return [item for item in items if isinstance(item, dict)]
        except Exception as exc:
            log.debug("worker turbo não recoloriu emojis de boas-vindas: %r", exc)
            return None
        finally:
            self._emoji_worker_active[worker_key] = max(0, int(self._emoji_worker_active.get(worker_key, 1) or 1) - 1)

    async def _local_recolor_emojis(self, emojis: list[dict[str, Any]], *, color_hex: str, palette: list[tuple[int, int, int]] | None = None, limit: int = DEFAULT_DECORATIVE_EMOJI_LIMIT) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        effective_limit = max(0, min(MAX_DECORATIVE_EMOJIS, int(limit or DEFAULT_DECORATIVE_EMOJI_LIMIT)))
        for emoji in emojis[:effective_limit]:
            try:
                raw = await self._fetch_custom_emoji_bytes(emoji)
                data, fmt = await self._recolor_emoji_bytes_local(raw, animated=bool(emoji.get("animated")), color_hex=color_hex, palette=palette)
                result.append({**emoji, "data_b64": base64.b64encode(data).decode("ascii"), "format": fmt})
            except Exception as exc:
                log.debug("não consegui recolorir emoji localmente: %s %r", emoji.get("raw"), exc)
        return result

    async def _application_id(self) -> int:
        app_id = int(getattr(self.bot, "application_id", 0) or 0)
        if app_id:
            return app_id
        info = await self.bot.application_info()
        return int(info.id)

    async def _create_application_emoji(self, *, name: str, data_b64: str, fmt: str) -> dict[str, Any] | None:
        try:
            app_id = await self._application_id()
            if not app_id:
                return None
            fmt = "gif" if str(fmt or "").lower() == "gif" else "png"
            image_data = f"data:image/{fmt};base64,{data_b64}"
            clean_name = re.sub(r"[^A-Za-z0-9_]+", "_", str(name or "cwemoji"))[:26].strip("_") or "cwemoji"
            clean_name = f"cw_{clean_name}_{uuid.uuid4().hex[:5]}"[:32]
            from discord.http import Route
            request = getattr(getattr(self.bot, "http", None), "request", None)
            if not callable(request):
                return None
            data = await request(Route("POST", "/applications/{application_id}/emojis", application_id=app_id), json={"name": clean_name, "image": image_data})
            if not isinstance(data, dict) or not data.get("id"):
                return None
            return {"id": str(data.get("id")), "name": str(data.get("name") or clean_name), "animated": bool(data.get("animated"))}
        except Exception as exc:
            log.warning("não consegui criar application emoji temporário de boas-vindas: %r", exc)
            return None

    async def _record_temp_emoji(self, *, guild_id: int, member_id: int, emoji: dict[str, Any], message_id: int = 0) -> None:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        now = datetime.now(timezone.utc)
        midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        doc = {
            "type": WELCOME_DOC_EMOJI,
            "guild_id": int(guild_id or 0),
            "member_id": int(member_id or 0),
            "message_id": int(message_id or 0),
            "emoji_id": str(emoji.get("id") or ""),
            "emoji_name": str(emoji.get("name") or ""),
            "animated": bool(emoji.get("animated")),
            "created_at": now,
            "delete_after": midnight,
            "status": "active",
        }
        if doc["emoji_id"]:
            with contextlib.suppress(Exception):
                await db.coll.insert_one(doc)

    async def _prepare_decorative_emojis(self, config: dict[str, Any], *, member: discord.Member | None, mode: str, dm: bool = False, invite_info: dict[str, Any] | None = None, preview: bool = False) -> dict[str, Any]:
        cfg = self._normalize_config(config)
        if dm or member is None or not bool(cfg.get("decorative_emoji_enabled", False)):
            return cfg
        effective_limit = await self._decorative_emoji_limit_for_member(member)
        emojis = self._emoji_tokens_from_config(cfg, mode=mode, dm=dm, limit=effective_limit)
        if not emojis:
            return cfg

        color_hex = _parse_hex((self._normalize_embed_config(cfg.get("embed")).get("color") if mode == "embed" else cfg.get("accent_color")) or cfg.get("accent_color") or DEFAULT_ACCENT)
        base_rgb = self._rgb_from_hex(color_hex)
        avatar_palette = await self._member_avatar_palette(member, color_hex, limit=6)
        emoji_palette = self._subtle_emoji_palette(base_rgb, avatar_palette)
        emoji_palette_hex = self._hex_palette_from_rgb(emoji_palette)

        # O worker pode devolver só parte dos emojis (ex.: o segundo asset animado falhou).
        # Nesse caso, tentamos completar localmente apenas os que faltaram. O fallback é por
        # emoji individual: o que não tiver replacement confirmado permanece original.
        processed_by_key: dict[str, dict[str, Any]] = {}
        worker_items = await self._worker_recolor_emojis(emojis, color_hex=color_hex, palette_hex=emoji_palette_hex, limit=effective_limit, monochrome=self._palette_is_mostly_monochrome(emoji_palette))
        for item in worker_items or []:
            key = str(item.get("key") or "")
            if key:
                processed_by_key[key] = item

        missing = [emoji for emoji in emojis if str(emoji.get("key") or "") not in processed_by_key]
        if missing:
            local_items = await self._local_recolor_emojis(missing, color_hex=color_hex, palette=emoji_palette, limit=effective_limit)
            for item in local_items or []:
                key = str(item.get("key") or "")
                if key and key not in processed_by_key:
                    processed_by_key[key] = item

        if not processed_by_key:
            return cfg

        replacements: dict[str, str] = {}
        created_for_tracking: list[dict[str, Any]] = []
        for original in emojis:
            key = str(original.get("key") or "")
            item = processed_by_key.get(key)
            if not item:
                continue
            emoji_id = str(original.get("id") or item.get("id") or "")
            if not re.fullmatch(r"\d{15,25}", emoji_id):
                continue
            normalized_item = await self._normalize_emoji_upload_item(item)
            if normalized_item is None:
                # Falhou? Mantém esse emoji base original em todas as ocorrências.
                continue
            created = await self._create_application_emoji(name=str(normalized_item.get("name") or item.get("name") or original.get("name") or "cwemoji"), data_b64=str(normalized_item.get("data_b64") or ""), fmt=str(normalized_item.get("format") or "png"))
            if not created:
                # Falhou? Mantém esse emoji base original em todas as ocorrências.
                continue
            # Use somente o tipo real do emoji criado. Se a animação caiu para PNG,
            # o token precisa ser estático; usar <a:...> para emoji estático pode renderizar estranho.
            animated = bool(created.get("animated")) or str(normalized_item.get("format") or "").lower() == "gif"
            replacement = f"<a:{created.get('name')}:{created.get('id')}>" if animated else f"<:{created.get('name')}:{created.get('id')}>"
            # Troca global por ID para cobrir nomes diferentes e todas as ocorrências.
            replacements[f"id:{emoji_id}"] = replacement
            # Compatibilidade: também troca os tokens raw conhecidos.
            raws = item.get("raw_variants") if isinstance(item.get("raw_variants"), list) else original.get("raw_variants")
            if not isinstance(raws, list) or not raws:
                raws = [item.get("raw") or original.get("raw")]
            for raw in [str(raw or "") for raw in raws if str(raw or "")]:
                replacements[raw] = replacement
            created_for_tracking.append(created)

        if not replacements:
            return cfg
        cfg = self._replace_emoji_tokens_in_config(cfg, replacements, mode=mode, dm=dm)
        for created in created_for_tracking:
            await self._record_temp_emoji(guild_id=int(getattr(member.guild, "id", 0) or 0), member_id=int(getattr(member, "id", 0) or 0), emoji=created)
        return cfg

    async def _delete_application_emoji(self, emoji_id: str) -> bool:
        try:
            app_id = await self._application_id()
            from discord.http import Route
            request = getattr(getattr(self.bot, "http", None), "request", None)
            if not callable(request):
                return False
            await request(Route("DELETE", "/applications/{application_id}/emojis/{emoji_id}", application_id=app_id, emoji_id=int(emoji_id)))
            return True
        except discord.NotFound:
            return True
        except Exception as exc:
            log.debug("não consegui apagar emoji temporário de boas-vindas %s: %r", emoji_id, exc)
            return False

    async def _purge_temp_emojis_once(self) -> None:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        now = datetime.now(timezone.utc)
        try:
            cursor = db.coll.find({"type": WELCOME_DOC_EMOJI, "status": "active", "delete_after": {"$lte": now}}, {"_id": 1, "emoji_id": 1})
            async for doc in cursor:
                emoji_id = str(doc.get("emoji_id") or "")
                if not emoji_id:
                    await db.coll.update_one({"_id": doc.get("_id")}, {"$set": {"status": "deleted", "deleted_at": now}})
                    continue
                ok = await self._delete_application_emoji(emoji_id)
                if ok:
                    await db.coll.update_one({"_id": doc.get("_id")}, {"$set": {"status": "deleted", "deleted_at": now}})
                else:
                    await asyncio.sleep(2.0)
                await asyncio.sleep(0.35)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("purge de emojis temporários de boas-vindas falhou: %r", exc)

    async def _emoji_midnight_purge_loop(self) -> None:
        try:
            await asyncio.sleep(20)
            await self._purge_temp_emojis_once()
            while True:
                now = datetime.now()
                tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                await asyncio.sleep(max(60.0, (tomorrow - now).total_seconds()))
                await self._purge_temp_emojis_once()
        except asyncio.CancelledError:
            return

    async def _send_rendered(self, destination: discord.abc.Messageable, cfg: dict[str, Any], *, member: discord.Member, dm: bool = False, invite_info: dict[str, Any] | None = None):
        cfg = await self._with_dynamic_colors(cfg, member=member)
        mode = str(cfg.get("dm_render_mode") if dm else cfg.get("render_mode") or "components_v2")
        try:
            cfg = await self._prepare_decorative_emojis(cfg, member=member, mode=mode, dm=dm, invite_info=invite_info)
        except Exception as exc:
            log.warning("falha ao preparar emojis de boas-vindas; mantendo originais: %r", exc)
        try:
            cfg, files = await self._prepare_dynamic_media(cfg, member=member, mode=mode, dm=dm)
        except Exception as exc:
            log.warning("falha ao montar mídia de boas-vindas; enviando sem imagem dinâmica: %r", exc)
            cfg, files = self._drop_dynamic_star_media(cfg, mode=mode), []
        allowed = discord.AllowedMentions.none() if dm else discord.AllowedMentions(users=True, roles=False, everyone=False)
        if mode == "embed":
            content, embed = self._make_embed_payload(cfg, member=member, guild_id=member.guild.id, dm=dm, invite_info=invite_info)
            kwargs: dict[str, Any] = {"embed": embed, "allowed_mentions": allowed}
            if content:
                kwargs["content"] = content
            if files:
                kwargs["files"] = files
            return await destination.send(**kwargs)
        if mode == "normal":
            return await destination.send(content=self._make_normal_content(cfg, member=member, guild_id=member.guild.id, dm=dm, invite_info=invite_info), allowed_mentions=allowed)
        kwargs: dict[str, Any] = {"view": self._make_components_view(cfg, member=member, dm=dm, invite_info=invite_info), "allowed_mentions": allowed}
        if files:
            kwargs["files"] = files
        return await destination.send(**kwargs)

    def _avatar_url_for(self, mode: str, *, member: discord.Member, guild: discord.Guild, invite_info: dict[str, Any] | None, custom_url: str = "") -> str:
        if mode == "custom" and custom_url:
            return custom_url
        if mode == "member":
            return str(member.display_avatar.url)
        if mode == "inviter":
            inviter_id = int((invite_info or {}).get("inviter_id") or 0)
            inviter = guild.get_member(inviter_id) if inviter_id else None
            if inviter is not None:
                return str(inviter.display_avatar.url)
        icon = getattr(guild, "icon", None)
        if icon:
            return str(icon.url)
        bot_user = getattr(self.bot, "user", None)
        return str(bot_user.display_avatar.url) if bot_user is not None else ""

    def _webhook_username_for(self, mode: str, *, member: discord.Member, guild: discord.Guild, invite_info: dict[str, Any] | None, fixed: str) -> str:
        if mode == "server":
            return _safe_webhook_name(guild.name)
        if mode == "member":
            return _safe_webhook_name(member.display_name)
        if mode == "inviter":
            inviter_id = int((invite_info or {}).get("inviter_id") or 0)
            inviter = guild.get_member(inviter_id) if inviter_id else None
            if inviter is not None:
                return _safe_webhook_name(inviter.display_name)
            name = str((invite_info or {}).get("inviter_name") or "")
            if name:
                return _safe_webhook_name(name)
        return _safe_webhook_name(fixed)

    async def _create_or_get_welcome_webhook(self, channel: discord.TextChannel | discord.Thread, webhook_cfg: dict[str, Any]) -> discord.Webhook | None:
        host = channel.parent if isinstance(channel, discord.Thread) else channel
        if host is None or not hasattr(host, "create_webhook"):
            return None
        me = host.guild.me if getattr(host, "guild", None) else None
        if me is None or not host.permissions_for(me).manage_webhooks:
            return None
        wanted_id = int(webhook_cfg.get("webhook_id") or 0)
        try:
            webhooks = await host.webhooks()
        except discord.HTTPException:
            return None
        if wanted_id:
            found = next((w for w in webhooks if int(getattr(w, "id", 0) or 0) == wanted_id), None)
            if found is not None:
                return found
        name = _safe_webhook_name(webhook_cfg.get("name"))
        found = next((w for w in webhooks if str(getattr(w, "name", "") or "") == name), None)
        if found is not None:
            return found
        try:
            return await host.create_webhook(name=name, reason="Boas-vindas")
        except discord.HTTPException:
            return None

    async def _list_channel_webhooks(self, guild: discord.Guild | None, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        channel = await self._configured_channel(guild, cfg)
        host = channel.parent if isinstance(channel, discord.Thread) else channel
        if host is None or not hasattr(host, "webhooks"):
            return []
        me = host.guild.me if getattr(host, "guild", None) else None
        if me is None or not host.permissions_for(me).manage_webhooks:
            return []
        try:
            webhooks = await host.webhooks()
        except discord.HTTPException:
            return []
        result: list[dict[str, Any]] = []
        for hook in webhooks:
            result.append({
                "id": int(getattr(hook, "id", 0) or 0),
                "name": str(getattr(hook, "name", "") or "Webhook"),
                "token": str(getattr(hook, "token", None) or ""),
                "channel_id": int(getattr(host, "id", 0) or 0),
            })
        return result

    async def _send_webhook_rendered(self, channel: discord.TextChannel | discord.Thread, cfg: dict[str, Any], *, member: discord.Member, invite_info: dict[str, Any] | None = None, wait: bool = False) -> tuple[bool, discord.Message | None]:
        webhook_cfg = self._normalize_webhook_config(cfg.get("webhook"))
        if not webhook_cfg.get("enabled"):
            return False, None
        cfg = await self._with_dynamic_colors(cfg, member=member)
        mode = str(cfg.get("render_mode") or "components_v2")
        try:
            cfg = await self._prepare_decorative_emojis(cfg, member=member, mode=mode, dm=False, invite_info=invite_info)
        except Exception as exc:
            log.warning("falha ao preparar emojis de webhook de boas-vindas; mantendo originais: %r", exc)
        try:
            cfg, files = await self._prepare_dynamic_media(cfg, member=member, mode=mode, dm=False)
        except Exception as exc:
            log.warning("falha ao montar mídia de webhook de boas-vindas; enviando sem imagem dinâmica: %r", exc)
            cfg, files = self._drop_dynamic_star_media(cfg, mode=mode), []
        webhook = await self._create_or_get_welcome_webhook(channel, webhook_cfg)
        if webhook is None:
            return False, None
        name = self._webhook_username_for(str(webhook_cfg.get("name_mode") or "fixed"), member=member, guild=member.guild, invite_info=invite_info, fixed=str(webhook_cfg.get("name") or DEFAULT_WEBHOOK_NAME))
        avatar_url = self._avatar_url_for(str(webhook_cfg.get("avatar_mode") or "server"), member=member, guild=member.guild, invite_info=invite_info, custom_url=str(webhook_cfg.get("avatar_url") or ""))
        allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)
        kwargs: dict[str, Any] = {"username": name, "allowed_mentions": allowed, "wait": bool(wait)}
        if avatar_url:
            kwargs["avatar_url"] = avatar_url
        if isinstance(channel, discord.Thread):
            kwargs["thread"] = channel
        if files:
            kwargs["files"] = files
        try:
            message = None
            if mode == "embed":
                content, embed = self._make_embed_payload(cfg, member=member, guild_id=member.guild.id, invite_info=invite_info)
                if content:
                    kwargs["content"] = content
                message = await webhook.send(embed=embed, **kwargs)
            elif mode == "normal":
                message = await webhook.send(content=self._make_normal_content(cfg, member=member, guild_id=member.guild.id, invite_info=invite_info), **kwargs)
            else:
                message = await webhook.send(view=self._make_components_view(cfg, member=member, invite_info=invite_info), **kwargs)
            return True, message if isinstance(message, discord.Message) else None
        except TypeError:
            # Algumas versões aceitam webhook sem view V2. Se acontecer, usa o bot no canal.
            return False, None
        except discord.HTTPException:
            return False, None

    def _welcome_utc_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _welcome_as_utc(self, value: Any) -> datetime | None:
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def _cleanup_expired_welcome_tracking(self, *, now: datetime | None = None) -> None:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        now = now or self._welcome_utc_now()
        try:
            result = await db.coll.delete_many({"type": WELCOME_DOC_SENT, "expires_at": {"$lt": now}})
            deleted = int(getattr(result, "deleted_count", 0) or 0)
            if deleted:
                log.info("[welcome] tracking expirado limpo: %s registro(s)", deleted)
        except Exception as exc:
            log.debug("[welcome] não consegui limpar tracking expirado: %r", exc)

    async def _migrate_welcome_tracking_user_ids(self) -> None:
        """Backfill legacy welcome tracking docs so they respect the shared unique DB index.

        The settings collection already has a unique index on (guild_id, user_id, type).
        Older welcome_sent_message docs used member_id but not user_id, which makes MongoDB
        see every tracking row in a guild as user_id=None and reject new rows.
        """
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        try:
            cursor = db.coll.find({
                "type": WELCOME_DOC_SENT,
                "$or": [{"user_id": {"$exists": False}}, {"user_id": None}, {"user_id": 0}],
            }).limit(200)
            fixed = 0
            removed = 0
            async for doc in cursor:
                doc_id = doc.get("_id")
                try:
                    member_id = int(doc.get("member_id") or 0)
                except Exception:
                    member_id = 0
                if member_id:
                    try:
                        await db.coll.update_one({"_id": doc_id}, {"$set": {"user_id": member_id}})
                        fixed += 1
                        continue
                    except Exception as exc:
                        log.debug("[welcome] não consegui migrar user_id do tracking _id=%s member=%s: %r", doc_id, member_id, exc)
                # Documento sem member_id não serve para apagar uma mensagem de um membro específico.
                # Remover evita manter o índice único preso em user_id=null.
                with contextlib.suppress(Exception):
                    await db.coll.delete_one({"_id": doc_id})
                    removed += 1
            if fixed or removed:
                log.info("[welcome] tracking antigo normalizado: %s corrigido(s), %s removido(s)", fixed, removed)
        except Exception as exc:
            log.debug("[welcome] não consegui normalizar tracking antigo: %r", exc)

    async def _track_sent_welcome_message(self, *, guild_id: int, member_id: int, message: discord.Message | None):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            log.debug("[welcome] tracking ignorado: settings_db indisponível guild=%s member=%s", guild_id, member_id)
            return
        if message is None:
            log.info("[welcome] tracking não salvo: mensagem enviada sem message_id guild=%s member=%s", guild_id, member_id)
            return
        now = self._welcome_utc_now()
        doc = {
            "type": WELCOME_DOC_SENT,
            "guild_id": int(guild_id),
            "user_id": int(member_id),
            "member_id": int(member_id),
            "channel_id": int(getattr(getattr(message, "channel", None), "id", 0) or 0),
            "message_id": int(getattr(message, "id", 0) or 0),
            "sent_at": now,
            "expires_at": now + timedelta(hours=24),
        }
        if not doc["channel_id"] or not doc["message_id"]:
            log.info("[welcome] tracking não salvo: channel/message vazio guild=%s member=%s channel=%s message=%s", guild_id, member_id, doc["channel_id"], doc["message_id"])
            return
        try:
            await db.coll.update_one(
                {"type": WELCOME_DOC_SENT, "guild_id": int(guild_id), "user_id": int(member_id)},
                {"$set": doc},
                upsert=True,
            )
            log.info(
                "[welcome] tracking salvo guild=%s member=%s channel=%s message=%s expires_at_utc=%s",
                guild_id,
                member_id,
                doc["channel_id"],
                doc["message_id"],
                doc["expires_at"].isoformat(),
            )
            await self._cleanup_expired_welcome_tracking(now=now)
        except Exception as exc:
            log.warning("[welcome] não consegui salvar tracking da boas-vindas guild=%s member=%s: %r", guild_id, member_id, exc)

    async def _delete_tracked_welcome_message(self, member: discord.Member):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            log.debug("[welcome] delete-on-leave ignorado: settings_db indisponível guild=%s member=%s", member.guild.id, member.id)
            return
        now = self._welcome_utc_now()
        query = {"type": WELCOME_DOC_SENT, "guild_id": int(member.guild.id), "user_id": int(member.id)}
        legacy_query = {"type": WELCOME_DOC_SENT, "guild_id": int(member.guild.id), "member_id": int(member.id)}
        try:
            doc = await db.coll.find_one(query, {"_id": 0})
            if not doc:
                doc = await db.coll.find_one(legacy_query, {"_id": 0})
                if doc:
                    query = legacy_query
        except Exception as exc:
            log.warning("[welcome] não consegui buscar tracking para apagar guild=%s member=%s: %r", member.guild.id, member.id, exc)
            return
        if not doc:
            log.info("[welcome] membro saiu sem tracking de boas-vindas guild=%s member=%s", member.guild.id, member.id)
            await self._cleanup_expired_welcome_tracking(now=now)
            return
        expires_at = self._welcome_as_utc(doc.get("expires_at"))
        sent_at = self._welcome_as_utc(doc.get("sent_at"))
        if expires_at is None and sent_at is not None:
            expires_at = sent_at + timedelta(hours=24)
        if expires_at is not None and expires_at < now:
            log.info(
                "[welcome] não apaguei boas-vindas: passou de 24h guild=%s member=%s message=%s expires_at_utc=%s now_utc=%s",
                member.guild.id,
                member.id,
                doc.get("message_id"),
                expires_at.isoformat(),
                now.isoformat(),
            )
            try:
                await db.coll.delete_one(query)
            except Exception:
                pass
            await self._cleanup_expired_welcome_tracking(now=now)
            return
        channel_id = int(doc.get("channel_id") or 0)
        message_id = int(doc.get("message_id") or 0)
        if not channel_id or not message_id:
            log.info("[welcome] tracking inválido ao sair guild=%s member=%s channel=%s message=%s", member.guild.id, member.id, channel_id, message_id)
            with contextlib.suppress(Exception):
                await db.coll.delete_one(query)
            return
        try:
            channel = member.guild.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                log.info("[welcome] canal de tracking não é apagável guild=%s member=%s channel=%s", member.guild.id, member.id, channel_id)
                return
            message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
            await message.delete()
            log.info("[welcome] boas-vindas apagada porque membro saiu em até 24h guild=%s member=%s channel=%s message=%s", member.guild.id, member.id, channel_id, message_id)
        except discord.NotFound:
            log.info("[welcome] boas-vindas já não existia ao tentar apagar guild=%s member=%s channel=%s message=%s", member.guild.id, member.id, channel_id, message_id)
        except discord.Forbidden:
            log.info("[welcome] sem permissão para apagar boas-vindas guild=%s member=%s channel=%s message=%s", member.guild.id, member.id, channel_id, message_id)
        except discord.HTTPException as exc:
            log.warning("[welcome] não consegui apagar boas-vindas guild=%s member=%s channel=%s message=%s: %r", member.guild.id, member.id, channel_id, message_id, exc)
        finally:
            try:
                await db.coll.delete_one(query)
            except Exception:
                pass

    async def _apply_auto_roles(self, member: discord.Member, cfg: dict[str, Any]):
        role_ids = [int(r) for r in cfg.get("auto_role_ids") or []]
        if not role_ids:
            return
        roles: list[discord.Role] = []
        bot_member = member.guild.me
        for role_id in role_ids[:MAX_AUTO_ROLES * 2]:
            role = member.guild.get_role(int(role_id))
            if role is None or role.is_default() or role.managed:
                continue
            if bot_member is not None and role >= bot_member.top_role:
                continue
            if role not in roles:
                roles.append(role)
        if not roles:
            return
        try:
            await member.add_roles(*roles, reason="Boas-vindas: cargos automáticos")
        except discord.HTTPException as exc:
            log.debug("não consegui entregar cargos de boas-vindas guild=%s member=%s: %r", member.guild.id, member.id, exc)

    async def _fetch_invite_snapshot(self, guild: discord.Guild | None) -> dict[str, dict[str, Any]] | None:
        if guild is None:
            return None
        me = guild.me
        perms = getattr(me, "guild_permissions", None)
        if not bool(getattr(perms, "manage_guild", False) or getattr(perms, "administrator", False)):
            return None
        try:
            invites = await guild.invites()
        except discord.HTTPException:
            return None
        snapshot: dict[str, dict[str, Any]] = {}
        for invite in invites:
            code = str(getattr(invite, "code", "") or "")
            if not code:
                continue
            inviter = getattr(invite, "inviter", None)
            channel = getattr(invite, "channel", None)
            snapshot[code] = {
                "uses": int(getattr(invite, "uses", 0) or 0),
                "inviter_id": int(getattr(inviter, "id", 0) or 0),
                "inviter_name": str(getattr(inviter, "display_name", None) or getattr(inviter, "name", None) or ""),
                "channel_id": int(getattr(channel, "id", 0) or 0),
                "channel_name": str(getattr(channel, "name", "") or ""),
            }
        return snapshot

    def _detect_used_invite(self, old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> dict[str, Any]:
        best_code = ""
        best_delta = 0
        for code, now in new.items():
            if code not in old:
                continue
            delta = int(now.get("uses") or 0) - int((old.get(code) or {}).get("uses") or 0)
            if delta > best_delta:
                best_delta = delta
                best_code = code
        if not best_code:
            return {"known": False}
        info = dict(new.get(best_code) or {})
        info["code"] = best_code
        info["known"] = True
        return info

    async def _refresh_invite_cache_for_guild(self, guild: discord.Guild | None, cfg: dict[str, Any] | None = None) -> bool:
        if guild is None:
            return False
        snapshot = await self._fetch_invite_snapshot(guild)
        if snapshot is None:
            return False
        config = self._normalize_config(cfg or await self._get_config(int(guild.id)))
        config["invite_cache"] = snapshot
        await self._save_config(int(guild.id), config)
        return True

    async def _warmup_invites(self):
        try:
            await self.bot.wait_until_ready()
            for guild in list(getattr(self.bot, "guilds", []) or []):
                try:
                    cfg = await self._get_config(int(guild.id))
                    if cfg.get("enabled") or cfg.get("special_rules"):
                        await self._refresh_invite_cache_for_guild(guild, cfg)
                        await asyncio.sleep(0.5)
                except Exception as exc:
                    log.debug("não consegui atualizar convites de boas-vindas guild=%s: %r", getattr(guild, "id", "?"), exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("warmup de convites de boas-vindas falhou: %r", exc)

    async def _invite_context_on_join(self, member: discord.Member, cfg: dict[str, Any]) -> dict[str, Any]:
        old_cache = self._normalize_invite_cache(cfg.get("invite_cache"))
        new_cache = await self._fetch_invite_snapshot(member.guild)
        if new_cache is None:
            return {"known": False}
        invite_info = self._detect_used_invite(old_cache, new_cache)
        saved = deepcopy(cfg)
        saved["invite_cache"] = new_cache
        await self._save_config(int(member.guild.id), saved)
        return invite_info

    def _find_variant(self, cfg: dict[str, Any], variant_id: str) -> dict[str, Any] | None:
        for variant in cfg.get("variants") or []:
            if str(variant.get("id")) == str(variant_id):
                return self._normalize_variant(variant)
        return None

    def _find_rule(self, cfg: dict[str, Any], rule_id: str) -> dict[str, Any] | None:
        for rule in cfg.get("special_rules") or []:
            if str(rule.get("id")) == str(rule_id):
                return self._normalize_rule(rule)
        return None

    def _rule_match_summary(self, rule: dict[str, Any]) -> str:
        rule = self._normalize_rule(rule)
        typ = str(rule.get("match_type") or "invite_code")
        val = str(rule.get("match_value") or "")
        if typ == "invite_code":
            return f"Convite `{val}`"
        if typ == "inviter":
            return f"Convidador {_user_mention(val)}"
        if typ == "invite_channel":
            return f"Canal {_channel_mention(int(val or 0))}"
        return "Regra especial"

    def _pick_special_rule(self, cfg: dict[str, Any], invite_info: dict[str, Any]) -> dict[str, Any] | None:
        if not invite_info.get("known"):
            return None
        rules = [self._normalize_rule(r) for r in cfg.get("special_rules") or [] if bool(r.get("enabled", True))]
        code = str(invite_info.get("code") or "").lower()
        inviter_id = str(int(invite_info.get("inviter_id") or 0)) if invite_info.get("inviter_id") else ""
        channel_id = str(int(invite_info.get("channel_id") or 0)) if invite_info.get("channel_id") else ""
        values = {"invite_code": code, "inviter": inviter_id, "invite_channel": channel_id}
        for typ in RULE_PRIORITY:
            wanted = values.get(typ) or ""
            if not wanted:
                continue
            for rule in rules:
                if str(rule.get("match_type")) != typ:
                    continue
                rv = str(rule.get("match_value") or "")
                if typ == "invite_code":
                    if rv.lower() == wanted:
                        return rule
                elif rv == wanted:
                    return rule
        return None

    def _effective_config_for_rule(self, base_cfg: dict[str, Any], rule: dict[str, Any] | None) -> dict[str, Any]:
        cfg = self._normalize_config(base_cfg)
        if not rule:
            return cfg
        rule = self._normalize_rule(rule)
        if str(rule.get("render_mode") or "inherit") != "inherit":
            cfg["render_mode"] = str(rule.get("render_mode"))
        if int(rule.get("channel_id") or 0):
            cfg["channel_id"] = int(rule.get("channel_id") or 0)
        if str(rule.get("style") or "inherit") != "inherit":
            cfg["style"] = str(rule.get("style"))
        if rule.get("accent_color"):
            cfg["accent_color"] = _parse_hex(rule.get("accent_color"))
        if rule.get("media_url"):
            cfg["media_url"] = _clean_url(rule.get("media_url"))
        if str(rule.get("media_mode") or "custom") != "custom":
            cfg["media_mode"] = _media_mode(rule.get("media_mode"))
        public = dict(cfg.get("public") or DEFAULT_PUBLIC)
        for key, value in dict(rule.get("public") or {}).items():
            if str(value or "").strip():
                public[key] = str(value)
        cfg["public"] = public
        rule_embed = self._normalize_embed_config(rule.get("embed"))
        if _has_custom_embed(rule_embed):
            embed = self._normalize_embed_config(cfg.get("embed"))
            for key, value in rule_embed.items():
                if str(value or "") != str(DEFAULT_EMBED.get(key) or ""):
                    embed[key] = value
            cfg["embed"] = embed
        base_roles = list(cfg.get("auto_role_ids") or [])
        for role_id in rule.get("auto_role_ids") or []:
            if int(role_id) not in base_roles:
                base_roles.append(int(role_id))
        cfg["auto_role_ids"] = base_roles[:MAX_AUTO_ROLES * 2]
        rweb = dict(rule.get("webhook") or {})
        mode = str(rweb.get("mode") or "inherit")
        webhook = dict(cfg.get("webhook") or {})
        if mode == "bot":
            webhook["enabled"] = False
        elif mode == "webhook":
            webhook["enabled"] = True
        if rweb.get("name"):
            webhook["name"] = _safe_webhook_name(rweb.get("name"))
            webhook["name_mode"] = "fixed"
        if str(rweb.get("avatar_mode") or "inherit") != "inherit":
            webhook["avatar_mode"] = str(rweb.get("avatar_mode"))
        if rweb.get("avatar_url"):
            webhook["avatar_url"] = _clean_url(rweb.get("avatar_url"))
            webhook["avatar_mode"] = "custom"
        cfg["webhook"] = self._normalize_webhook_config(webhook)
        return cfg

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = await self._get_config(int(member.guild.id))
        if not bool(cfg.get("enabled", False)):
            return
        invite_info = await self._invite_context_on_join(member, cfg)
        variant = self._pick_variant(cfg)
        base_effective = self._apply_variant(cfg, variant)
        rule = self._pick_special_rule(cfg, invite_info)
        effective = self._effective_config_for_rule(base_effective, rule)
        await self._apply_auto_roles(member, effective)
        channel_id = int(effective.get("channel_id") or 0)
        if channel_id:
            channel = member.guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    channel = None
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                sent = False
                sent_message: discord.Message | None = None
                track_message = bool(cfg.get("delete_on_leave_enabled", False))
                if (effective.get("webhook") or {}).get("enabled"):
                    sent, sent_message = await self._send_webhook_rendered(channel, effective, member=member, invite_info=invite_info, wait=track_message)
                if not sent:
                    try:
                        sent_message = await self._send_rendered(channel, effective, member=member, dm=False, invite_info=invite_info)
                        sent = True
                    except discord.HTTPException as exc:
                        log.debug("não consegui enviar boas-vindas guild=%s member=%s: %r", member.guild.id, member.id, exc)
                if sent and track_message:
                    await self._track_sent_welcome_message(guild_id=int(member.guild.id), member_id=int(member.id), message=sent_message)
        if bool(cfg.get("dm_enabled", False)):
            try:
                await self._send_rendered(member, cfg, member=member, dm=True, invite_info=invite_info)
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        cfg = await self._get_config(int(member.guild.id))
        if not bool(cfg.get("delete_on_leave_enabled", False)):
            log.info("[welcome] membro saiu; apagar em até 24h desligado guild=%s member=%s", member.guild.id, member.id)
            return
        log.info("[welcome] membro saiu; procurando boas-vindas para apagar guild=%s member=%s", member.guild.id, member.id)
        await self._delete_tracked_welcome_message(member)

    async def _reply_welcome_panel_safe(self, ctx: commands.Context, *, view: discord.ui.LayoutView | None = None, content: str = "") -> discord.Message | None:
        """Send the welcome panel without letting a Discord payload issue make the command look dead."""
        kwargs: dict[str, Any] = {
            "mention_author": False,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if view is not None:
            kwargs["view"] = view
        if content:
            kwargs["content"] = content
        try:
            return await ctx.reply(**kwargs)
        except discord.HTTPException as exc:
            log.warning("[welcome] não consegui responder com reply ao comando guild=%s channel=%s author=%s: %r", getattr(ctx.guild, "id", None), getattr(ctx.channel, "id", None), getattr(ctx.author, "id", None), exc)
            try:
                return await ctx.send(content=content or "Abrindo painel de boas-vindas...", view=view, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc2:
                log.exception("[welcome] não consegui enviar painel de boas-vindas nem por fallback guild=%s channel=%s author=%s", getattr(ctx.guild, "id", None), getattr(ctx.channel, "id", None), getattr(ctx.author, "id", None))
                with contextlib.suppress(discord.HTTPException):
                    return await ctx.send("Não consegui abrir o painel de boas-vindas agora. Verifique minhas permissões no canal e tente novamente.", allowed_mentions=discord.AllowedMentions.none())
                return None

    @commands.command(name="welcome", aliases=("boasvindas", "boas-vindas", "boas", "bv"))
    @commands.guild_only()
    async def welcome_panel(self, ctx: commands.Context):
        log.info("[welcome] comando recebido guild=%s channel=%s author=%s", getattr(ctx.guild, "id", None), getattr(ctx.channel, "id", None), getattr(ctx.author, "id", None))
        if not self._can_manage(ctx.author):
            notice = _make_notice_view("Sem permissão", "Você precisa gerenciar o servidor para usar esse painel.", ok=False)
            await self._reply_welcome_panel_safe(ctx, view=notice)
            return
        try:
            cfg = await self._get_config(int(ctx.guild.id))
            view = WelcomeAdminView(self, owner_id=int(ctx.author.id), guild_id=int(ctx.guild.id), config=cfg)
        except Exception:
            log.exception("[welcome] falha ao montar painel de boas-vindas guild=%s author=%s", getattr(ctx.guild, "id", None), getattr(ctx.author, "id", None))
            with contextlib.suppress(discord.HTTPException):
                await ctx.reply("Não consegui montar o painel de boas-vindas agora. Tente novamente em alguns segundos.", mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
        msg = await self._reply_welcome_panel_safe(ctx, view=view)
        if msg is not None:
            view.message = msg
            view.command_message = ctx.message

    @welcome_panel.error
    async def welcome_panel_error(self, ctx: commands.Context, error: commands.CommandError):
        log.error("[welcome] erro não tratado no comando welcome guild=%s channel=%s author=%s: %r", getattr(ctx.guild, "id", None), getattr(ctx.channel, "id", None), getattr(ctx.author, "id", None), error, exc_info=(type(error), error, getattr(error, "__traceback__", None)))
        with contextlib.suppress(discord.HTTPException):
            await ctx.reply("Não consegui abrir o painel de boas-vindas agora. Tente novamente em alguns segundos.", mention_author=False, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeCog(bot))
