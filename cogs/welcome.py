from __future__ import annotations

import logging
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

WELCOME_DOC_CONFIG = "welcome_config"
MAX_TEXT_DISPLAY = 3900
MAX_TEMPLATE_LENGTH = 1800
MAX_FOOTER_LENGTH = 300
MAX_AUTO_ROLES = 10
VAR_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")
HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

DEFAULT_ACCENT = "#5865F2"
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
}

VARIABLE_HELP: dict[str, str] = {
    "membro": "nome exibido do membro",
    "membro_mencao": "menção do membro",
    "usuario": "nome de usuário",
    "usuario_id": "ID do membro",
    "servidor": "nome do servidor",
    "servidor_id": "ID do servidor",
    "contador": "quantidade atual de membros",
    "criado_em": "data de criação da conta",
    "criado_relativo": "há quanto tempo a conta foi criada",
    "entrou_em": "horário da entrada no servidor",
}

STYLE_LABELS = {
    "complete": "Completo",
    "simple": "Simples",
    "compact": "Compacto",
}


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
    if not URL_RE.fullmatch(raw):
        return ""
    return raw[:1000]


def _make_notice_view(title: str, body: str | list[str], *, ok: bool = True) -> discord.ui.LayoutView:
    body_text = "\n".join(str(item) for item in body) if isinstance(body, list) else str(body or "")
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(_trim(f"# {title}\n{body_text}")),
        accent_color=discord.Color.green() if ok else discord.Color.red(),
    ))
    return view


def _status_label(value: bool) -> str:
    return "Ligado" if value else "Desligado"


def _template_changed(cfg: dict[str, Any]) -> bool:
    public = dict(cfg.get("public") or {})
    return any(str(public.get(k) or "") != str(DEFAULT_PUBLIC.get(k) or "") for k in DEFAULT_PUBLIC)


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
        closed = discord.ui.LayoutView(timeout=None)
        closed.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 🌟 Boas-vindas\nPainel fechado."),
            accent_color=_color_from_hex(self.panel.config.get("accent_color")),
        ))
        await interaction.response.edit_message(view=closed)


class _PreviewButton(discord.ui.Button):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(label="Preview", emoji="👁️", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        self.panel.go_to("preview")
        self.panel.notice = "Ficaria assim quando alguém entrar."
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _MainSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        options = [
            discord.SelectOption(label="Mensagem de boas-vindas", value="message", emoji="📢", description="Texto que aparece no canal"),
            discord.SelectOption(label="Canal de envio", value="channel", emoji="📍", description="Onde a mensagem vai aparecer"),
            discord.SelectOption(label="Mensagem privada", value="dm", emoji="💬", description="Mensagem opcional no privado"),
            discord.SelectOption(label="Cargos automáticos", value="roles", emoji="🎭", description="Cargos entregues ao entrar"),
            discord.SelectOption(label="Visual da mensagem", value="visual", emoji="🖼️", description="Estilo, cor e imagem"),
            discord.SelectOption(label="Variáveis", value="variables", emoji="🧬", description="Palavras que o bot troca sozinho"),
            discord.SelectOption(label="Ativar ou desativar", value="status", emoji="⚙️", description="Ligar ou pausar as boas-vindas"),
        ]
        super().__init__(placeholder="O que você quer configurar?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        self.panel.go_to(str(self.values[0]))
        self.panel.notice = ""
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _MessageActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        options = [
            discord.SelectOption(label="Editar texto", value="edit", emoji="✏️", description="Título, mensagem e rodapé"),
            discord.SelectOption(label="Escolher preset", value="presets", emoji="✨", description="Usar uma base pronta"),
            discord.SelectOption(label="Restaurar mensagem padrão", value="restore", emoji="↩️", description="Voltar para o texto inicial"),
            discord.SelectOption(label="Ver preview", value="preview", emoji="👁️", description="Prévia dentro deste painel"),
        ]
        super().__init__(placeholder="O que deseja ajustar na mensagem?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "edit":
            await interaction.response.send_modal(WelcomeMessageModal(self.panel))
            return
        if action == "presets":
            self.panel.go_to("presets")
            self.panel.notice = ""
        elif action == "restore":
            cfg = deepcopy(self.panel.config)
            cfg["public"] = dict(DEFAULT_PUBLIC)
            await self.panel.cog._save_config(self.panel.guild_id, cfg)
            self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
            self.panel.notice = "Mensagem padrão restaurada."
        elif action == "preview":
            self.panel.go_to("preview")
            self.panel.notice = "Ficaria assim quando alguém entrar."
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
        await self.panel.cog._save_config(self.panel.guild_id, cfg)
        self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
        self.panel.screen = "message"
        self.panel.notice = f"Preset **{preset['label']}** aplicado."
        self.panel._rebuild()
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
        await self.panel.cog._save_config(self.panel.guild_id, cfg)
        self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
        self.panel.notice = f"Pronto, as boas-vindas vão aparecer em {channel.mention}."
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
        await self.panel.cog._save_config(self.panel.guild_id, cfg)
        self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
        self.panel.notice = "Canal removido. As boas-vindas ficaram desligadas por enquanto."
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _DmActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        enabled = bool(panel.config.get("dm_enabled", False))
        options = [
            discord.SelectOption(label="Desligar mensagem privada" if enabled else "Ligar mensagem privada", value="toggle", emoji="💬"),
            discord.SelectOption(label="Editar mensagem privada", value="edit", emoji="✏️"),
            discord.SelectOption(label="Restaurar mensagem padrão", value="restore", emoji="↩️"),
            discord.SelectOption(label="Ver preview", value="preview", emoji="👁️"),
        ]
        super().__init__(placeholder="O que deseja ajustar no privado?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "edit":
            await interaction.response.send_modal(WelcomeDmModal(self.panel))
            return
        cfg = deepcopy(self.panel.config)
        if action == "toggle":
            cfg["dm_enabled"] = not bool(cfg.get("dm_enabled", False))
            await self.panel.cog._save_config(self.panel.guild_id, cfg)
            self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
            self.panel.notice = "Mensagem privada ligada." if self.panel.config.get("dm_enabled") else "Mensagem privada desligada."
            self.panel.screen = "dm"
        elif action == "restore":
            cfg["dm"] = dict(DEFAULT_DM)
            await self.panel.cog._save_config(self.panel.guild_id, cfg)
            self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
            self.panel.notice = "Mensagem privada restaurada."
            self.panel.screen = "dm"
        elif action == "preview":
            self.panel.go_to("dm_preview")
            self.panel.notice = "Ficaria assim no privado."
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
        roles: list[discord.Role] = [role for role in self.values if isinstance(role, discord.Role)]
        safe_role_ids: list[int] = []
        skipped: list[str] = []
        bot_member = interaction.guild.me if interaction.guild is not None else None
        for role in roles[:MAX_AUTO_ROLES]:
            if role.is_default() or role.managed:
                skipped.append(role.mention)
                continue
            if bot_member is not None and role >= bot_member.top_role:
                skipped.append(role.mention)
                continue
            safe_role_ids.append(int(role.id))
        cfg = deepcopy(self.panel.config)
        cfg["auto_role_ids"] = safe_role_ids
        await self.panel.cog._save_config(self.panel.guild_id, cfg)
        self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
        if skipped:
            self.panel.notice = "Salvei os cargos possíveis. Alguns precisam ficar abaixo do meu cargo."
        elif safe_role_ids:
            self.panel.notice = "Pronto, esses cargos serão entregues quando alguém entrar."
        else:
            self.panel.notice = "Nenhum cargo automático ficou salvo."
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
        await self.panel.cog._save_config(self.panel.guild_id, cfg)
        self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
        self.panel.notice = "Cargos automáticos removidos."
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
        await self.panel.cog._save_config(self.panel.guild_id, cfg)
        self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
        self.panel.notice = f"Visual ajustado para **{STYLE_LABELS.get(cfg['style'], 'Completo')}**."
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _VisualActionSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        options = [
            discord.SelectOption(label="Editar cor e imagem", value="edit", emoji="🎨"),
            discord.SelectOption(label="Remover imagem", value="clear_image", emoji="🧹"),
            discord.SelectOption(label="Ver preview", value="preview", emoji="👁️"),
        ]
        super().__init__(placeholder="Mais opções do visual", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        action = str(self.values[0])
        if action == "edit":
            await interaction.response.send_modal(WelcomeVisualModal(self.panel))
            return
        if action == "clear_image":
            cfg = deepcopy(self.panel.config)
            cfg["media_url"] = ""
            await self.panel.cog._save_config(self.panel.guild_id, cfg)
            self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
            self.panel.notice = "Imagem removida."
            self.panel.screen = "visual"
        elif action == "preview":
            self.panel.go_to("preview")
            self.panel.notice = "Ficaria assim quando alguém entrar."
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class _StatusSelect(discord.ui.Select):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        enabled = bool(panel.config.get("enabled", False))
        options = [
            discord.SelectOption(label="Ligar boas-vindas", value="enable", emoji="✅", default=enabled),
            discord.SelectOption(label="Desligar boas-vindas", value="disable", emoji="⏸️", default=not enabled),
        ]
        super().__init__(placeholder="Escolha o status", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        want_enable = str(self.values[0]) == "enable"
        cfg = deepcopy(self.panel.config)
        if want_enable and not int(cfg.get("channel_id") or 0):
            self.panel.notice = "Escolha um canal antes de ligar."
        else:
            cfg["enabled"] = want_enable
            await self.panel.cog._save_config(self.panel.guild_id, cfg)
            self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
            self.panel.notice = "Boas-vindas ligadas." if want_enable else "Boas-vindas pausadas."
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class WelcomeMessageModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Mensagem de boas-vindas")
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
            label="Rodapé opcional",
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
        await self.panel.cog._save_config(self.panel.guild_id, cfg)
        self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
        self.panel.screen = "message"
        self.panel.notice = "Mensagem atualizada."
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
            label="Rodapé opcional",
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
        await self.panel.cog._save_config(self.panel.guild_id, cfg)
        self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
        self.panel.screen = "dm"
        self.panel.notice = "Mensagem privada atualizada."
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
        await interaction.response.edit_message(view=self.panel)


class WelcomeVisualModal(discord.ui.Modal):
    def __init__(self, panel: "WelcomeAdminView"):
        super().__init__(title="Visual da mensagem")
        self.panel = panel
        self.accent_input = discord.ui.TextInput(
            label="Cor lateral em HEX",
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
        cfg = deepcopy(self.panel.config)
        cfg["accent_color"] = _parse_hex(raw_hex)
        cfg["media_url"] = _clean_url(raw_url)
        await self.panel.cog._save_config(self.panel.guild_id, cfg)
        self.panel.config = await self.panel.cog._get_config(self.panel.guild_id)
        self.panel.screen = "visual"
        self.panel.notice = "Visual atualizado."
        self.panel._rebuild(member=interaction.user if isinstance(interaction.user, discord.Member) else None)
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
        self._preview_member: discord.Member | None = None
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

    def _clear(self):
        for item in list(self.children):
            self.remove_item(item)

    def _home_lines(self) -> list[str]:
        cfg = self.config
        role_ids = [int(r) for r in cfg.get("auto_role_ids") or []]
        lines = [
            "# 🌟 Boas-vindas",
            "Receba novos membros com uma mensagem personalizada.",
            "",
            f"**Status**\n{_status_label(bool(cfg.get('enabled', False)))}",
            "",
            f"**Canal**\n{_channel_mention(cfg.get('channel_id'))}",
            "",
            f"**Mensagem**\n{'personalizada' if _template_changed(cfg) else 'padrão'}",
            "",
            f"**Mensagem privada**\n{_status_label(bool(cfg.get('dm_enabled', False)))}",
            "",
            f"**Cargos automáticos**\n{len(role_ids)} cargo(s)",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        lines.extend(["", "Escolha abaixo o que quer ajustar."])
        return lines

    def _rebuild(self, *, member: discord.Member | None = None):
        if member is not None:
            self._preview_member = member
        self._clear()
        if self.screen == "home":
            self.add_item(discord.ui.Container(
                discord.ui.TextDisplay("\n".join(self._home_lines())),
                discord.ui.Separator(),
                discord.ui.ActionRow(_MainSelect(self)),
                discord.ui.ActionRow(_PreviewButton(self), _CloseButton(self)),
                accent_color=_color_from_hex(self.config.get("accent_color")),
            ))
            return
        if self.screen == "message":
            self._build_message()
            return
        if self.screen == "presets":
            self._build_presets()
            return
        if self.screen == "channel":
            self._build_channel()
            return
        if self.screen == "dm":
            self._build_dm()
            return
        if self.screen == "dm_preview":
            self._build_dm_preview()
            return
        if self.screen == "roles":
            self._build_roles()
            return
        if self.screen == "visual":
            self._build_visual()
            return
        if self.screen == "variables":
            self._build_variables()
            return
        if self.screen == "status":
            self._build_status()
            return
        if self.screen == "preview":
            self._build_preview()
            return
        self.screen = "home"
        self._rebuild()

    def _build_message(self):
        public = dict(self.config.get("public") or {})
        lines = [
            "# 📢 Mensagem de boas-vindas",
            "Edite o texto que aparece quando alguém entra.",
            "",
            "**Título**",
            _trim(public.get("title") or DEFAULT_PUBLIC["title"], 500),
            "",
            "**Mensagem**",
            _trim(public.get("body") or DEFAULT_PUBLIC["body"], 900),
        ]
        footer = str(public.get("footer") or "").strip()
        if footer:
            lines.extend(["", "**Rodapé**", _trim(footer, 300)])
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_MessageActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_presets(self):
        lines = [
            "# ✨ Presets",
            "Escolha uma base e edite depois como quiser.",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_PresetSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_channel(self):
        channel_id = int(self.config.get("channel_id") or 0)
        lines = [
            "# 📍 Canal de envio",
            "Onde a mensagem deve aparecer quando alguém entrar?",
            "",
            f"**Canal atual**\n{_channel_mention(channel_id)}",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        rows: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_ChannelSelect(self)),
        ]
        if channel_id:
            rows.append(discord.ui.ActionRow(_ChannelActionSelect(self)))
        rows.append(discord.ui.ActionRow(_BackButton(self)))
        self.add_item(discord.ui.Container(
            *rows,
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_dm(self):
        dm = dict(self.config.get("dm") or {})
        lines = [
            "# 💬 Mensagem privada",
            "Você pode mandar uma mensagem no privado quando alguém entrar.",
            "",
            f"**Status**\n{_status_label(bool(self.config.get('dm_enabled', False)))}",
            "",
            "**Título**",
            _trim(dm.get("title") or DEFAULT_DM["title"], 400),
            "",
            "**Mensagem**",
            _trim(dm.get("body") or DEFAULT_DM["body"], 700),
        ]
        footer = str(dm.get("footer") or "").strip()
        if footer:
            lines.extend(["", "**Rodapé**", _trim(footer, 300)])
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_DmActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_dm_preview(self):
        member = self._preview_member
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("# 💬 Preview da mensagem privada\nFicaria assim no privado."),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))
        self.add_item(self.cog._make_welcome_container(self.config, member=member, guild_id=self.guild_id, dm=True))
        self.add_item(discord.ui.ActionRow(_BackButton(self)))

    def _build_roles(self):
        role_ids = [int(r) for r in self.config.get("auto_role_ids") or []]
        lines = [
            "# 🎭 Cargos automáticos",
            "Escolha os cargos entregues quando alguém entrar.",
            "",
            f"**Atuais**\n{_role_list(self.cog.bot.get_guild(self.guild_id), role_ids)}",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        rows: list[discord.ui.Item[Any]] = [
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_RoleSelect(self)),
        ]
        if role_ids:
            rows.append(discord.ui.ActionRow(_RoleActionSelect(self)))
        rows.append(discord.ui.ActionRow(_BackButton(self)))
        self.add_item(discord.ui.Container(
            *rows,
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_visual(self):
        lines = [
            "# 🖼️ Visual da mensagem",
            "Escolha como a mensagem vai aparecer.",
            "",
            f"**Estilo**\n{STYLE_LABELS.get(str(self.config.get('style') or 'complete'), 'Completo')}",
            "",
            f"**Cor**\n`{_parse_hex(self.config.get('accent_color'))}`",
            "",
            f"**Imagem**\n{'configurada' if _clean_url(self.config.get('media_url')) else 'sem imagem'}",
        ]
        if self.notice:
            lines.extend(["", self.notice])
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.ActionRow(_VisualStyleSelect(self)),
            discord.ui.ActionRow(_VisualActionSelect(self)),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_variables(self):
        lines = [
            "# 🧬 Variáveis",
            "Use essas palavras no texto. O bot troca sozinho quando alguém entra.",
            "",
        ]
        for name, description in VARIABLE_HELP.items():
            lines.append(f"`{{{name}}}` — {description}")
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(_trim("\n".join(lines))),
            discord.ui.Separator(),
            discord.ui.ActionRow(_BackButton(self)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))

    def _build_status(self):
        channel_id = int(self.config.get("channel_id") or 0)
        lines = [
            "# ⚙️ Ativar ou desativar",
            "Ligue quando a mensagem estiver pronta.",
            "",
            f"**Status atual**\n{_status_label(bool(self.config.get('enabled', False)))}",
            "",
            f"**Canal**\n{_channel_mention(channel_id)}",
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
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay("\n".join(lines)),
            accent_color=_color_from_hex(self.config.get("accent_color")),
        ))
        self.add_item(self.cog._make_welcome_container(self.config, member=member, guild_id=self.guild_id, dm=False))
        self.add_item(discord.ui.ActionRow(_BackButton(self)))


class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return getattr(self.bot, "settings_db", None)

    async def cog_load(self):
        await self._ensure_indexes()

    async def _ensure_indexes(self):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        try:
            await db.coll.create_index([("type", 1), ("guild_id", 1)], name="welcome_type_guild")
        except Exception as exc:
            log.warning("falha ao criar índice de boas-vindas: %s", exc)

    def _default_config(self, guild_id: int | None = None) -> dict[str, Any]:
        cfg = {
            "type": WELCOME_DOC_CONFIG,
            "enabled": False,
            "channel_id": 0,
            "dm_enabled": False,
            "auto_role_ids": [],
            "style": "complete",
            "accent_color": DEFAULT_ACCENT,
            "media_url": "",
            "public": dict(DEFAULT_PUBLIC),
            "dm": dict(DEFAULT_DM),
        }
        if guild_id is not None:
            cfg["guild_id"] = int(guild_id)
        return cfg

    def _normalize_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        base = self._default_config()
        cfg = dict(config or {})
        merged = {**base, **cfg}
        public = dict(DEFAULT_PUBLIC)
        public.update({k: str(v) for k, v in dict(merged.get("public") or {}).items() if k in DEFAULT_PUBLIC})
        merged["public"] = public
        dm = dict(DEFAULT_DM)
        dm.update({k: str(v) for k, v in dict(merged.get("dm") or {}).items() if k in DEFAULT_DM})
        merged["dm"] = dm
        role_ids: list[int] = []
        for raw in merged.get("auto_role_ids") or []:
            try:
                role_id = int(raw)
            except Exception:
                continue
            if role_id > 0 and role_id not in role_ids:
                role_ids.append(role_id)
            if len(role_ids) >= MAX_AUTO_ROLES:
                break
        merged["auto_role_ids"] = role_ids
        merged["enabled"] = bool(merged.get("enabled", False))
        merged["dm_enabled"] = bool(merged.get("dm_enabled", False))
        merged["channel_id"] = int(merged.get("channel_id") or 0)
        merged["style"] = str(merged.get("style") or "complete") if str(merged.get("style") or "complete") in STYLE_LABELS else "complete"
        merged["accent_color"] = _parse_hex(merged.get("accent_color"))
        merged["media_url"] = _clean_url(merged.get("media_url"))
        merged["type"] = WELCOME_DOC_CONFIG
        return merged

    async def _get_config(self, guild_id: int) -> dict[str, Any]:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return self._normalize_config({"guild_id": int(guild_id)})
        doc = await db.coll.find_one({"type": WELCOME_DOC_CONFIG, "guild_id": int(guild_id)}, {"_id": 0})
        cfg = self._normalize_config(doc or {"guild_id": int(guild_id)})
        cfg["guild_id"] = int(guild_id)
        return cfg

    async def _save_config(self, guild_id: int, config: dict[str, Any]):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        cfg = self._normalize_config(config)
        cfg["guild_id"] = int(guild_id)
        cfg["type"] = WELCOME_DOC_CONFIG
        await db.coll.update_one(
            {"type": WELCOME_DOC_CONFIG, "guild_id": int(guild_id)},
            {"$set": cfg},
            upsert=True,
        )

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

    def _member_values(self, member: discord.Member | None, *, guild_id: int | None = None) -> dict[str, str]:
        guild = getattr(member, "guild", None) if member is not None else None
        if guild is None and guild_id:
            guild = self.bot.get_guild(int(guild_id))
        now_ts = int(datetime.now(timezone.utc).timestamp())
        created_at = getattr(member, "created_at", None) if member is not None else None
        created_ts = int(created_at.timestamp()) if created_at else now_ts
        return {
            "membro": str(getattr(member, "display_name", None) or getattr(member, "name", None) or "novo membro"),
            "membro_mencao": str(getattr(member, "mention", None) or "@membro"),
            "usuario": str(getattr(member, "name", None) or getattr(member, "display_name", None) or "membro"),
            "usuario_id": str(getattr(member, "id", "" ) or ""),
            "servidor": str(getattr(guild, "name", None) or "servidor"),
            "servidor_id": str(getattr(guild, "id", "" ) or guild_id or ""),
            "contador": str(getattr(guild, "member_count", None) or ""),
            "criado_em": f"<t:{created_ts}:D>",
            "criado_relativo": f"<t:{created_ts}:R>",
            "entrou_em": f"<t:{now_ts}:F>",
        }

    def _replace_vars(self, text: str, values: dict[str, str]) -> str:
        def repl(match: re.Match[str]) -> str:
            key = match.group(1)
            return values.get(key, match.group(0))
        return VAR_RE.sub(repl, str(text or ""))

    def _build_welcome_text(self, cfg: dict[str, Any], *, member: discord.Member | None, guild_id: int | None, dm: bool = False) -> tuple[str, str, str]:
        values = self._member_values(member, guild_id=guild_id)
        source = dict(cfg.get("dm") or DEFAULT_DM) if dm else dict(cfg.get("public") or DEFAULT_PUBLIC)
        title = self._replace_vars(str(source.get("title") or ""), values).strip()
        body = self._replace_vars(str(source.get("body") or ""), values).strip()
        footer = self._replace_vars(str(source.get("footer") or ""), values).strip()
        return title, body, footer

    def _make_welcome_container(
        self,
        config: dict[str, Any],
        *,
        member: discord.Member | None,
        guild_id: int | None = None,
        dm: bool = False,
    ) -> discord.ui.Container:
        cfg = self._normalize_config(config)
        title, body, footer = self._build_welcome_text(cfg, member=member, guild_id=guild_id, dm=dm)
        style = str(cfg.get("style") or "complete")
        children: list[discord.ui.Item[Any]] = []
        if title:
            children.append(discord.ui.TextDisplay(_trim(f"# {title}", 900)))
        if body:
            children.append(discord.ui.TextDisplay(_trim(body, 1800 if style != "compact" else 900)))
        media_url = _clean_url(cfg.get("media_url")) if not dm else ""
        if media_url and style == "complete":
            children.extend([
                discord.ui.Separator(),
                discord.ui.MediaGallery(discord.MediaGalleryItem(media_url)),
            ])
        if footer and style != "compact":
            children.extend([discord.ui.Separator(), discord.ui.TextDisplay(_trim(footer, 500))])
        if not children:
            children.append(discord.ui.TextDisplay("# Bem-vindo(a)!"))
        return discord.ui.Container(
            *children,
            accent_color=_color_from_hex(cfg.get("accent_color")),
        )

    def _make_welcome_view(self, config: dict[str, Any], *, member: discord.Member, dm: bool = False) -> discord.ui.LayoutView:
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(self._make_welcome_container(config, member=member, guild_id=int(member.guild.id), dm=dm))
        return view

    async def _apply_auto_roles(self, member: discord.Member, cfg: dict[str, Any]):
        role_ids = [int(r) for r in cfg.get("auto_role_ids") or []]
        if not role_ids:
            return
        roles: list[discord.Role] = []
        bot_member = member.guild.me
        for role_id in role_ids[:MAX_AUTO_ROLES]:
            role = member.guild.get_role(int(role_id))
            if role is None or role.is_default() or role.managed:
                continue
            if bot_member is not None and role >= bot_member.top_role:
                continue
            roles.append(role)
        if not roles:
            return
        try:
            await member.add_roles(*roles, reason="Boas-vindas: cargos automáticos")
        except discord.HTTPException as exc:
            log.debug("não consegui entregar cargos de boas-vindas guild=%s member=%s: %r", member.guild.id, member.id, exc)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = await self._get_config(int(member.guild.id))
        if not bool(cfg.get("enabled", False)):
            return
        await self._apply_auto_roles(member, cfg)
        channel_id = int(cfg.get("channel_id") or 0)
        if channel_id:
            channel = member.guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    channel = None
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                try:
                    await channel.send(
                        view=self._make_welcome_view(cfg, member=member, dm=False),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                    )
                except discord.HTTPException as exc:
                    log.debug("não consegui enviar boas-vindas guild=%s member=%s: %r", member.guild.id, member.id, exc)
        if bool(cfg.get("dm_enabled", False)):
            try:
                await member.send(
                    view=self._make_welcome_view(cfg, member=member, dm=True),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass

    @commands.command(name="welcome", aliases=("boasvindas", "boas-vindas", "bv"))
    @commands.guild_only()
    async def welcome_panel(self, ctx: commands.Context):
        if not self._can_manage(ctx.author):
            await ctx.reply(
                view=_make_notice_view("Sem permissão", "Você precisa gerenciar o servidor para usar esse painel.", ok=False),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        cfg = await self._get_config(int(ctx.guild.id))
        view = WelcomeAdminView(self, owner_id=int(ctx.author.id), guild_id=int(ctx.guild.id), config=cfg)
        msg = await ctx.reply(view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        view.message = msg


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeCog(bot))
