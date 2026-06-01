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
from .helpers import *

log = logging.getLogger(__name__)

class WelcomeConfigMixin:
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
