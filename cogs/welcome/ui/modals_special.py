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

__all__ = [
    "SpecialRuleCreateModal",
    "SpecialInviteRuleModal",
    "SpecialRuleTextModal",
    "SpecialRuleVisualModal",
    "SpecialRuleWebhookModal",
]
