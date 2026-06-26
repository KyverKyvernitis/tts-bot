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

class WelcomeDeliveryMixin:
    async def _send_rendered(self, destination: discord.abc.Messageable, cfg: dict[str, Any], *, member: discord.Member, dm: bool = False, invite_info: dict[str, Any] | None = None):
        cfg = await self._with_dynamic_colors(cfg, member=member)
        mode = str(cfg.get("dm_render_mode") if dm else cfg.get("render_mode") or "components_v2")
        try:
            cfg = await self._prepare_decorative_emojis(cfg, member=member, mode=mode, dm=dm, invite_info=invite_info)
        except Exception as exc:
            log.warning("falha ao preparar emojis de boas-vindas; mantendo originais: %r", exc)
        prepared_emoji_ids = self._temp_emoji_ids_from_config(cfg)
        try:
            try:
                cfg, files = await self._prepare_dynamic_media(cfg, member=member, mode=mode, dm=dm)
            except Exception as exc:
                log.warning("falha ao montar mídia de boas-vindas; enviando sem imagem dinâmica: %r", exc)
                cfg, files = self._drop_dynamic_star_media(cfg, mode=mode), []
            allowed = discord.AllowedMentions.none() if dm else discord.AllowedMentions(users=True, roles=False, everyone=False)
            if mode == "embed":
                content, embed = self._make_embed_payload(cfg, member=member, guild_id=member.guild.id, dm=dm, invite_info=invite_info)
                kwargs: dict[str, Any] = {"embed": embed, "allowed_mentions": allowed}
                if content:
                    kwargs["content"] = content
                if files:
                    kwargs["files"] = files
                return await destination.send(**kwargs)
            if mode == "normal":
                return await destination.send(content=self._make_normal_content(cfg, member=member, guild_id=member.guild.id, dm=dm, invite_info=invite_info), allowed_mentions=allowed)
            kwargs: dict[str, Any] = {"view": self._make_components_view(cfg, member=member, dm=dm, invite_info=invite_info), "allowed_mentions": allowed}
            if files:
                kwargs["files"] = files
            return await destination.send(**kwargs)
        except asyncio.CancelledError:
            if prepared_emoji_ids:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._discard_temp_emojis(prepared_emoji_ids, reason="send_cancelled"))
            raise
        except Exception:
            if prepared_emoji_ids:
                await self._discard_temp_emojis(prepared_emoji_ids, reason="send_failed")
            raise

    def _avatar_url_for(self, mode: str, *, member: discord.Member, guild: discord.Guild, invite_info: dict[str, Any] | None, custom_url: str = "") -> str:
        if mode == "custom" and custom_url:
            return custom_url
        if mode == "member":
            return str(member.display_avatar.url)
        if mode == "inviter":
            inviter_id = int((invite_info or {}).get("inviter_id") or 0)
            inviter = guild.get_member(inviter_id) if inviter_id else None
            if inviter is not None:
                return str(inviter.display_avatar.url)
        icon = getattr(guild, "icon", None)
        if icon:
            return str(icon.url)
        bot_user = getattr(self.bot, "user", None)
        return str(bot_user.display_avatar.url) if bot_user is not None else ""

    def _webhook_username_for(self, mode: str, *, member: discord.Member, guild: discord.Guild, invite_info: dict[str, Any] | None, fixed: str) -> str:
        if mode == "server":
            return _safe_webhook_name(guild.name)
        if mode == "member":
            return _safe_webhook_name(member.display_name)
        if mode == "inviter":
            inviter_id = int((invite_info or {}).get("inviter_id") or 0)
            inviter = guild.get_member(inviter_id) if inviter_id else None
            if inviter is not None:
                return _safe_webhook_name(inviter.display_name)
            name = str((invite_info or {}).get("inviter_name") or "")
            if name:
                return _safe_webhook_name(name)
        return _safe_webhook_name(fixed)

    async def _create_or_get_welcome_webhook(self, channel: discord.TextChannel | discord.Thread, webhook_cfg: dict[str, Any]) -> discord.Webhook | None:
        host = channel.parent if isinstance(channel, discord.Thread) else channel
        if host is None or not hasattr(host, "create_webhook"):
            return None
        me = host.guild.me if getattr(host, "guild", None) else None
        if me is None or not host.permissions_for(me).manage_webhooks:
            return None
        wanted_id = int(webhook_cfg.get("webhook_id") or 0)
        try:
            webhooks = await host.webhooks()
        except discord.HTTPException:
            return None
        if wanted_id:
            found = next((w for w in webhooks if int(getattr(w, "id", 0) or 0) == wanted_id), None)
            if found is not None:
                return found
        name = _safe_webhook_name(webhook_cfg.get("name"))
        found = next((w for w in webhooks if str(getattr(w, "name", "") or "") == name), None)
        if found is not None:
            return found
        try:
            return await host.create_webhook(name=name, reason="Boas-vindas")
        except discord.HTTPException:
            return None

    async def _list_channel_webhooks(self, guild: discord.Guild | None, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        channel = await self._configured_channel(guild, cfg)
        host = channel.parent if isinstance(channel, discord.Thread) else channel
        if host is None or not hasattr(host, "webhooks"):
            return []
        me = host.guild.me if getattr(host, "guild", None) else None
        if me is None or not host.permissions_for(me).manage_webhooks:
            return []
        try:
            webhooks = await host.webhooks()
        except discord.HTTPException:
            return []
        result: list[dict[str, Any]] = []
        for hook in webhooks:
            result.append({
                "id": int(getattr(hook, "id", 0) or 0),
                "name": str(getattr(hook, "name", "") or "Webhook"),
                "token": str(getattr(hook, "token", None) or ""),
                "channel_id": int(getattr(host, "id", 0) or 0),
            })
        return result

    async def _send_webhook_rendered(self, channel: discord.TextChannel | discord.Thread, cfg: dict[str, Any], *, member: discord.Member, invite_info: dict[str, Any] | None = None, wait: bool = False) -> tuple[bool, discord.Message | None]:
        webhook_cfg = self._normalize_webhook_config(cfg.get("webhook"))
        if not webhook_cfg.get("enabled"):
            return False, None
        cfg = await self._with_dynamic_colors(cfg, member=member)
        mode = str(cfg.get("render_mode") or "components_v2")
        try:
            cfg = await self._prepare_decorative_emojis(cfg, member=member, mode=mode, dm=False, invite_info=invite_info)
        except Exception as exc:
            log.warning("falha ao preparar emojis de webhook de boas-vindas; mantendo originais: %r", exc)
        prepared_emoji_ids = self._temp_emoji_ids_from_config(cfg)
        published = False
        try:
            try:
                cfg, files = await self._prepare_dynamic_media(cfg, member=member, mode=mode, dm=False)
            except Exception as exc:
                log.warning("falha ao montar mídia de webhook de boas-vindas; enviando sem imagem dinâmica: %r", exc)
                cfg, files = self._drop_dynamic_star_media(cfg, mode=mode), []
            webhook = await self._create_or_get_welcome_webhook(channel, webhook_cfg)
            if webhook is None:
                return False, None
            name = self._webhook_username_for(str(webhook_cfg.get("name_mode") or "fixed"), member=member, guild=member.guild, invite_info=invite_info, fixed=str(webhook_cfg.get("name") or DEFAULT_WEBHOOK_NAME))
            avatar_url = self._avatar_url_for(str(webhook_cfg.get("avatar_mode") or "server"), member=member, guild=member.guild, invite_info=invite_info, custom_url=str(webhook_cfg.get("avatar_url") or ""))
            allowed = discord.AllowedMentions(users=True, roles=False, everyone=False)
            kwargs: dict[str, Any] = {"username": name, "allowed_mentions": allowed, "wait": bool(wait)}
            if avatar_url:
                kwargs["avatar_url"] = avatar_url
            if isinstance(channel, discord.Thread):
                kwargs["thread"] = channel
            if files:
                kwargs["files"] = files

            message = None
            if mode == "embed":
                content, embed = self._make_embed_payload(cfg, member=member, guild_id=member.guild.id, invite_info=invite_info)
                if content:
                    kwargs["content"] = content
                message = await webhook.send(embed=embed, **kwargs)
            elif mode == "normal":
                message = await webhook.send(content=self._make_normal_content(cfg, member=member, guild_id=member.guild.id, invite_info=invite_info), **kwargs)
            else:
                message = await webhook.send(view=self._make_components_view(cfg, member=member, invite_info=invite_info), **kwargs)
            published = True
            return True, message if isinstance(message, discord.Message) else None
        except asyncio.CancelledError:
            raise
        except TypeError:
            # Algumas versões aceitam webhook sem view V2. Se acontecer, usa o bot no canal.
            return False, None
        except discord.HTTPException:
            return False, None
        finally:
            if not published and prepared_emoji_ids:
                try:
                    await asyncio.shield(self._discard_temp_emojis(prepared_emoji_ids, reason="webhook_failed"))
                except Exception:
                    pass

    def _welcome_utc_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _welcome_as_utc(self, value: Any) -> datetime | None:
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def _cleanup_expired_welcome_tracking(self, *, now: datetime | None = None) -> None:
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        now = now or self._welcome_utc_now()
        try:
            result = await db.coll.delete_many({"type": WELCOME_DOC_SENT, "expires_at": {"$lt": now}})
            deleted = int(getattr(result, "deleted_count", 0) or 0)
            if deleted:
                log.info("[welcome] tracking expirado limpo: %s registro(s)", deleted)
        except Exception as exc:
            log.debug("[welcome] não consegui limpar tracking expirado: %r", exc)

    async def _migrate_welcome_tracking_user_ids(self) -> None:
        """Backfill legacy welcome tracking docs so they respect the shared unique DB index.

        The settings collection already has a unique index on (guild_id, user_id, type).
        Older welcome_sent_message docs used member_id but not user_id, which makes MongoDB
        see every tracking row in a guild as user_id=None and reject new rows.
        """
        db = self.db
        if db is None or not hasattr(db, "coll"):
            return
        try:
            cursor = db.coll.find({
                "type": WELCOME_DOC_SENT,
                "$or": [{"user_id": {"$exists": False}}, {"user_id": None}, {"user_id": 0}],
            }).limit(200)
            fixed = 0
            removed = 0
            async for doc in cursor:
                doc_id = doc.get("_id")
                try:
                    member_id = int(doc.get("member_id") or 0)
                except Exception:
                    member_id = 0
                if member_id:
                    try:
                        await db.coll.update_one({"_id": doc_id}, {"$set": {"user_id": member_id}})
                        fixed += 1
                        continue
                    except Exception as exc:
                        log.debug("[welcome] não consegui migrar user_id do tracking _id=%s member=%s: %r", doc_id, member_id, exc)
                # Documento sem member_id não serve para apagar uma mensagem de um membro específico.
                # Remover evita manter o índice único preso em user_id=null.
                with contextlib.suppress(Exception):
                    await db.coll.delete_one({"_id": doc_id})
                    removed += 1
            if fixed or removed:
                log.info("[welcome] tracking antigo normalizado: %s corrigido(s), %s removido(s)", fixed, removed)
        except Exception as exc:
            log.debug("[welcome] não consegui normalizar tracking antigo: %r", exc)

    async def _track_sent_welcome_message(self, *, guild_id: int, member_id: int, message: discord.Message | None):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            log.debug("[welcome] tracking ignorado: settings_db indisponível guild=%s member=%s", guild_id, member_id)
            return
        if message is None:
            log.info("[welcome] tracking não salvo: mensagem enviada sem message_id guild=%s member=%s", guild_id, member_id)
            return
        now = self._welcome_utc_now()
        doc = {
            "type": WELCOME_DOC_SENT,
            "guild_id": int(guild_id),
            "user_id": int(member_id),
            "member_id": int(member_id),
            "channel_id": int(getattr(getattr(message, "channel", None), "id", 0) or 0),
            "message_id": int(getattr(message, "id", 0) or 0),
            "sent_at": now,
            "expires_at": now + timedelta(hours=24),
        }
        if not doc["channel_id"] or not doc["message_id"]:
            log.info("[welcome] tracking não salvo: channel/message vazio guild=%s member=%s channel=%s message=%s", guild_id, member_id, doc["channel_id"], doc["message_id"])
            return
        try:
            await db.coll.update_one(
                {"type": WELCOME_DOC_SENT, "guild_id": int(guild_id), "user_id": int(member_id)},
                {"$set": doc},
                upsert=True,
            )
            log.info(
                "[welcome] tracking salvo guild=%s member=%s channel=%s message=%s expires_at_utc=%s",
                guild_id,
                member_id,
                doc["channel_id"],
                doc["message_id"],
                doc["expires_at"].isoformat(),
            )
            await self._cleanup_expired_welcome_tracking(now=now)
        except Exception as exc:
            log.warning("[welcome] não consegui salvar tracking da boas-vindas guild=%s member=%s: %r", guild_id, member_id, exc)

    async def _delete_tracked_welcome_message(self, member: discord.Member):
        db = self.db
        if db is None or not hasattr(db, "coll"):
            log.debug("[welcome] delete-on-leave ignorado: settings_db indisponível guild=%s member=%s", member.guild.id, member.id)
            return
        now = self._welcome_utc_now()
        query = {"type": WELCOME_DOC_SENT, "guild_id": int(member.guild.id), "user_id": int(member.id)}
        legacy_query = {"type": WELCOME_DOC_SENT, "guild_id": int(member.guild.id), "member_id": int(member.id)}
        try:
            doc = await db.coll.find_one(query, {"_id": 0})
            if not doc:
                doc = await db.coll.find_one(legacy_query, {"_id": 0})
                if doc:
                    query = legacy_query
        except Exception as exc:
            log.warning("[welcome] não consegui buscar tracking para apagar guild=%s member=%s: %r", member.guild.id, member.id, exc)
            return
        if not doc:
            log.info("[welcome] membro saiu sem tracking de boas-vindas guild=%s member=%s", member.guild.id, member.id)
            await self._cleanup_expired_welcome_tracking(now=now)
            return
        expires_at = self._welcome_as_utc(doc.get("expires_at"))
        sent_at = self._welcome_as_utc(doc.get("sent_at"))
        if expires_at is None and sent_at is not None:
            expires_at = sent_at + timedelta(hours=24)
        if expires_at is not None and expires_at < now:
            log.info(
                "[welcome] não apaguei boas-vindas: passou de 24h guild=%s member=%s message=%s expires_at_utc=%s now_utc=%s",
                member.guild.id,
                member.id,
                doc.get("message_id"),
                expires_at.isoformat(),
                now.isoformat(),
            )
            try:
                await db.coll.delete_one(query)
            except Exception:
                pass
            await self._cleanup_expired_welcome_tracking(now=now)
            return
        channel_id = int(doc.get("channel_id") or 0)
        message_id = int(doc.get("message_id") or 0)
        if not channel_id or not message_id:
            log.info("[welcome] tracking inválido ao sair guild=%s member=%s channel=%s message=%s", member.guild.id, member.id, channel_id, message_id)
            with contextlib.suppress(Exception):
                await db.coll.delete_one(query)
            return
        try:
            channel = member.guild.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                log.info("[welcome] canal de tracking não é apagável guild=%s member=%s channel=%s", member.guild.id, member.id, channel_id)
                return
            message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
            await message.delete()
            log.info("[welcome] boas-vindas apagada porque membro saiu em até 24h guild=%s member=%s channel=%s message=%s", member.guild.id, member.id, channel_id, message_id)
        except discord.NotFound:
            log.info("[welcome] boas-vindas já não existia ao tentar apagar guild=%s member=%s channel=%s message=%s", member.guild.id, member.id, channel_id, message_id)
        except discord.Forbidden:
            log.info("[welcome] sem permissão para apagar boas-vindas guild=%s member=%s channel=%s message=%s", member.guild.id, member.id, channel_id, message_id)
        except discord.HTTPException as exc:
            log.warning("[welcome] não consegui apagar boas-vindas guild=%s member=%s channel=%s message=%s: %r", member.guild.id, member.id, channel_id, message_id, exc)
        finally:
            try:
                await db.coll.delete_one(query)
            except Exception:
                pass
