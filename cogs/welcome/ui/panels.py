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

from .controls import *

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
