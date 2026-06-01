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

from ..config.defaults import *
from ..core.helpers import *

log = logging.getLogger(__name__)

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

__all__ = [
    "WelcomeDmModal",
    "WelcomeDmOptionsModal",
    "WelcomeVisualModal",
    "WelcomeDecorativeEmojiModal",
    "WelcomeWebhookSetupModal",
    "WelcomeWebhookAppearanceModal",
    "WelcomeWebhookModal",
    "WelcomeQuickOptionsModal",
]
