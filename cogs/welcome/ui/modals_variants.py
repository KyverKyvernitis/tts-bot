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

__all__ = [
    "WelcomeVariantCreateModal",
    "WelcomeVariantSettingsModal",
    "WelcomeVariantContentModal",
    "WelcomeVariantVisualModal",
]
