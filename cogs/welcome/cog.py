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

from .config.defaults import *
from .core.helpers import *

log = logging.getLogger(__name__)

from .ui import WelcomeAdminView
from .core.config_mixin import WelcomeConfigMixin
from .core.delivery_mixin import WelcomeDeliveryMixin
from .core.media_mixin import WelcomeMediaMixin
from .core.render_mixin import WelcomeRenderMixin
from .core.rules_mixin import WelcomeRulesMixin

class WelcomeCog(WelcomeRulesMixin, WelcomeDeliveryMixin, WelcomeMediaMixin, WelcomeRenderMixin, WelcomeConfigMixin, commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._warmup_task: asyncio.Task | None = None
        self._emoji_purge_task: asyncio.Task | None = None
        self._emoji_api_lock = asyncio.Lock()
        self._emoji_name_reservations: set[str] = set()
        self._application_emoji_items: list[dict[str, Any]] = []
        self._application_emoji_names: set[str] = set()
        self._application_emoji_count: int | None = None
        self._application_emoji_state_at = 0.0
        self._emoji_capacity_warning_at = 0.0
        self._emoji_worker_active: dict[str, int] = {}
        self._avatar_color_cache: dict[str, str] = {}
        self._avatar_palette_cache: dict[str, list[tuple[int, int, int]]] = {}
        self._star_image_cache: dict[str, bytes] = {}
        self._bot_owner_ids: set[int] = self._collect_known_bot_owner_ids()
        self._owner_presence_cache: dict[int, tuple[float, bool]] = {}

    @property
    def db(self):
        return getattr(self.bot, "settings_db", None)

    def _collect_known_bot_owner_ids(self) -> set[int]:
        ids: set[int] = set()

        def add(value: Any) -> None:
            try:
                parsed = int(value or 0)
            except Exception:
                return
            if parsed > 0:
                ids.add(parsed)

        add(getattr(self.bot, "owner_id", 0))
        for raw in (getattr(self.bot, "owner_ids", None) or []):
            add(raw)
        for env_name in ("BOT_OWNER_ID", "OWNER_ID", "TTS_VOICE_FAILURE_DM_USER_ID", "VOICE_FAILURE_DM_USER_ID"):
            add(os.getenv(env_name))
        with contextlib.suppress(Exception):
            import config as bot_config  # type: ignore
            for attr in ("BOT_OWNER_ID", "OWNER_ID", "TTS_VOICE_FAILURE_DM_USER_ID", "VOICE_FAILURE_DM_USER_ID"):
                add(getattr(bot_config, attr, 0))
        return ids

    async def _refresh_bot_owner_ids(self) -> None:
        ids = self._collect_known_bot_owner_ids()
        try:
            app = await self.bot.application_info()
        except Exception:
            self._bot_owner_ids = ids
            return
        owner = getattr(app, "owner", None)
        if owner is not None:
            with contextlib.suppress(Exception):
                ids.add(int(owner.id))
        team = getattr(app, "team", None)
        for member in getattr(team, "members", None) or []:
            user = getattr(member, "user", member)
            with contextlib.suppress(Exception):
                ids.add(int(user.id))
        self._bot_owner_ids = ids

    def _guild_has_bot_owner_cached(self, guild: discord.Guild | None) -> bool:
        if guild is None or not self._bot_owner_ids:
            return False
        cached = self._owner_presence_cache.get(int(guild.id))
        if cached and (time.monotonic() - float(cached[0])) < OWNER_PRESENCE_CACHE_SECONDS:
            return bool(cached[1])
        for owner_id in self._bot_owner_ids:
            if int(getattr(guild, "owner_id", 0) or 0) == int(owner_id):
                self._owner_presence_cache[int(guild.id)] = (time.monotonic(), True)
                return True
            if guild.get_member(int(owner_id)) is not None:
                self._owner_presence_cache[int(guild.id)] = (time.monotonic(), True)
                return True
        return False

    def _decorative_emoji_limit_for_guild_id(self, guild_id: int | None) -> int:
        guild = self.bot.get_guild(int(guild_id or 0)) if guild_id else None
        return OWNER_GUILD_DECORATIVE_EMOJI_LIMIT if self._guild_has_bot_owner_cached(guild) else DEFAULT_DECORATIVE_EMOJI_LIMIT

    async def _decorative_emoji_limit_for_member(self, member: discord.Member | None) -> int:
        guild = getattr(member, "guild", None)
        if guild is None or not self._bot_owner_ids:
            return DEFAULT_DECORATIVE_EMOJI_LIMIT
        if self._guild_has_bot_owner_cached(guild):
            return OWNER_GUILD_DECORATIVE_EMOJI_LIMIT
        cached = self._owner_presence_cache.get(int(guild.id))
        if cached and (time.monotonic() - float(cached[0])) < OWNER_PRESENCE_CACHE_SECONDS:
            return OWNER_GUILD_DECORATIVE_EMOJI_LIMIT if bool(cached[1]) else DEFAULT_DECORATIVE_EMOJI_LIMIT
        present = False
        for owner_id in self._bot_owner_ids:
            try:
                await asyncio.wait_for(guild.fetch_member(int(owner_id)), timeout=1.5)
                present = True
                break
            except Exception:
                continue
        self._owner_presence_cache[int(guild.id)] = (time.monotonic(), present)
        return OWNER_GUILD_DECORATIVE_EMOJI_LIMIT if present else DEFAULT_DECORATIVE_EMOJI_LIMIT

    async def cog_load(self):
        await self._refresh_bot_owner_ids()
        await self._ensure_indexes()
        self._warmup_task = asyncio.create_task(self._warmup_invites())
        self._emoji_purge_task = asyncio.create_task(self._emoji_midnight_purge_loop())

    async def cog_unload(self):
        if self._warmup_task is not None:
            self._warmup_task.cancel()
        if self._emoji_purge_task is not None:
            self._emoji_purge_task.cancel()

    async def _ensure_indexes(self):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        try:
            # Não criamos mais um índice simples (type, guild_id), porque outras cogs
            # já podem ter criado o mesmo padrão com outro nome. Isso evita aviso inútil
            # de IndexOptionsConflict a cada restart.
            await db.coll.create_index([("type", 1), ("guild_id", 1), ("member_id", 1)], name="welcome_sent_member")
            await db.coll.create_index([("type", 1), ("expires_at", 1)], name="welcome_sent_expires")
            await db.coll.create_index([("type", 1), ("delete_after", 1)], name="welcome_temp_emoji_purge")
        except Exception as exc:
            text = str(exc)
            if "IndexOptionsConflict" in text or "Index already exists" in text:
                log.debug("índice de boas-vindas já existe com outro nome: %s", exc)
            else:
                log.warning("falha ao criar índice de boas-vindas: %s", exc)
        await self._migrate_welcome_tracking_user_ids()

























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

    async def _configured_channel(self, guild: discord.Guild | None, cfg: dict[str, Any]) -> discord.TextChannel | discord.Thread | None:
        channel_id = int(cfg.get("channel_id") or 0)
        return await self._resolve_text_channel(guild, channel_id) if channel_id else None

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

    def _safe_role_ids(self, guild: discord.Guild | None, roles: list[Any]) -> tuple[list[int], list[str]]:
        safe_role_ids: list[int] = []
        skipped: list[str] = []
        bot_member = guild.me if guild is not None else None
        for role in roles[:MAX_AUTO_ROLES]:
            if not isinstance(role, discord.Role):
                continue
            if role.is_default() or role.managed:
                skipped.append(role.mention)
                continue
            if bot_member is not None and role >= bot_member.top_role:
                skipped.append(role.mention)
                continue
            safe_role_ids.append(int(role.id))
        return safe_role_ids, skipped










































































    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = await self._get_config(int(member.guild.id))
        if not bool(cfg.get("enabled", False)):
            return
        invite_info = await self._invite_context_on_join(member, cfg)
        variant = self._pick_variant(cfg)
        base_effective = self._apply_variant(cfg, variant)
        rule = self._pick_special_rule(cfg, invite_info)
        effective = self._effective_config_for_rule(base_effective, rule)
        await self._apply_auto_roles(member, effective)
        channel_id = int(effective.get("channel_id") or 0)
        if channel_id:
            channel = member.guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    channel = None
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                sent = False
                sent_message: discord.Message | None = None
                track_message = bool(cfg.get("delete_on_leave_enabled", False))
                if (effective.get("webhook") or {}).get("enabled"):
                    sent, sent_message = await self._send_webhook_rendered(channel, effective, member=member, invite_info=invite_info, wait=track_message)
                if not sent:
                    try:
                        sent_message = await self._send_rendered(channel, effective, member=member, dm=False, invite_info=invite_info)
                        sent = True
                    except discord.HTTPException as exc:
                        log.debug("não consegui enviar boas-vindas guild=%s member=%s: %r", member.guild.id, member.id, exc)
                if sent and track_message:
                    await self._track_sent_welcome_message(guild_id=int(member.guild.id), member_id=int(member.id), message=sent_message)
        if bool(cfg.get("dm_enabled", False)):
            try:
                await self._send_rendered(member, cfg, member=member, dm=True, invite_info=invite_info)
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        cfg = await self._get_config(int(member.guild.id))
        if not bool(cfg.get("delete_on_leave_enabled", False)):
            log.info("[welcome] membro saiu; apagar em até 24h desligado guild=%s member=%s", member.guild.id, member.id)
            return
        log.info("[welcome] membro saiu; procurando boas-vindas para apagar guild=%s member=%s", member.guild.id, member.id)
        await self._delete_tracked_welcome_message(member)

    async def _reply_welcome_panel_safe(self, ctx: commands.Context, *, view: discord.ui.LayoutView | None = None, content: str = "") -> discord.Message | None:
        """Envia o painel sem deixar uma falha de payload/permissão parecer comando morto."""
        kwargs: dict[str, Any] = {
            "mention_author": False,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if view is not None:
            kwargs["view"] = view
        if content:
            kwargs["content"] = content
        try:
            return await ctx.reply(**kwargs)
        except discord.HTTPException as exc:
            log.warning(
                "[welcome] não consegui responder com reply ao comando guild=%s channel=%s author=%s: %r",
                getattr(ctx.guild, "id", None),
                getattr(ctx.channel, "id", None),
                getattr(ctx.author, "id", None),
                exc,
            )
            try:
                return await ctx.send(
                    content=content or "Abrindo painel de boas-vindas...",
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                log.exception(
                    "[welcome] não consegui enviar painel de boas-vindas nem por fallback guild=%s channel=%s author=%s",
                    getattr(ctx.guild, "id", None),
                    getattr(ctx.channel, "id", None),
                    getattr(ctx.author, "id", None),
                )
                with contextlib.suppress(discord.HTTPException):
                    return await ctx.send(
                        "Não consegui abrir o painel de boas-vindas agora. Verifique minhas permissões no canal e tente novamente.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                return None

    @commands.command(name="welcome", aliases=("boasvindas", "boas-vindas", "boas", "bv"))
    @commands.guild_only()
    async def welcome_panel(self, ctx: commands.Context):
        log.info(
            "[welcome] comando recebido guild=%s channel=%s author=%s",
            getattr(ctx.guild, "id", None),
            getattr(ctx.channel, "id", None),
            getattr(ctx.author, "id", None),
        )
        if not self._can_manage(ctx.author):
            notice = _make_notice_view("Sem permissão", "Você precisa gerenciar o servidor para usar esse painel.", ok=False)
            await self._reply_welcome_panel_safe(ctx, view=notice)
            return
        try:
            cfg = await self._get_config(int(ctx.guild.id))
            view = WelcomeAdminView(self, owner_id=int(ctx.author.id), guild_id=int(ctx.guild.id), config=cfg)
        except Exception:
            log.exception(
                "[welcome] falha ao montar painel de boas-vindas guild=%s author=%s",
                getattr(ctx.guild, "id", None),
                getattr(ctx.author, "id", None),
            )
            with contextlib.suppress(discord.HTTPException):
                await ctx.reply(
                    "Não consegui montar o painel de boas-vindas agora. Tente novamente em alguns segundos.",
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return
        msg = await self._reply_welcome_panel_safe(ctx, view=view)
        if msg is not None:
            view.message = msg
            view.command_message = ctx.message

    @welcome_panel.error
    async def welcome_panel_error(self, ctx: commands.Context, error: commands.CommandError):
        log.error(
            "[welcome] erro não tratado no comando welcome guild=%s channel=%s author=%s: %r",
            getattr(ctx.guild, "id", None),
            getattr(ctx.channel, "id", None),
            getattr(ctx.author, "id", None),
            error,
            exc_info=(type(error), error, getattr(error, "__traceback__", None)),
        )
        with contextlib.suppress(discord.HTTPException):
            await ctx.reply(
                "Não consegui abrir o painel de boas-vindas agora. Tente novamente em alguns segundos.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )



async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeCog(bot))
