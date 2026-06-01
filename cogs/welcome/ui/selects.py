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

from .modals import *

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

__all__ = [
    "_MainSelect",
    "_MessageActionSelect",
    "_PresetSelect",
    "_EmbedActionSelect",
    "_RenderModeSelect",
    "_ModeActionSelect",
    "_ChannelSelect",
    "_ChannelActionSelect",
    "_WebhookNameModeSelect",
    "_WebhookActionSelect",
    "_WebhookExistingSelect",
    "_DmActionSelect",
    "_DmRenderModeSelect",
    "_RoleSelect",
    "_RoleActionSelect",
    "_VisualStyleSelect",
    "_VisualActionSelect",
    "_SpecialMainSelect",
    "_SpecialRuleListSelect",
    "_SpecialInviterSelect",
    "_SpecialInviteChannelSelect",
    "_SpecialRuleActionSelect",
    "_SpecialRuleModeSelect",
    "_SpecialRuleChannelSelect",
    "_SpecialRuleChannelActionSelect",
    "_SpecialRuleRoleSelect",
    "_StatusSelect",
    "_VariantListSelect",
    "_VariantActionSelect",
    "_VariantEditActionSelect",
]
