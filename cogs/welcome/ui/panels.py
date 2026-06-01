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
