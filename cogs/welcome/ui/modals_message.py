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

__all__ = [
    "WelcomeModeModal",
    "WelcomeSettingsModal",
    "WelcomeMessageModal",
    "WelcomeNormalMessageModal",
    "WelcomeEmbedContentModal",
    "WelcomeEmbedTextModal",
    "WelcomeEmbedAuthorModal",
    "WelcomeEmbedImagesModal",
    "WelcomeEmbedFooterModal",
]
