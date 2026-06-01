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

class WelcomeRulesMixin:
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
