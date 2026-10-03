"""Respostas efetivamente enviadas, para conversão fiel entre texto e áudio.

Os IDs vêm do envio confirmado do Discord. Cartões, propostas e resultados
incertos nunca são respostas conversíveis. URLs livres não são armazenadas.
"""
from __future__ import annotations

from copy import deepcopy
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable

import discord

from .action_policy import ActionDenied, _fresh_member, _origin_channel, _requester_can_view
from .memory import MemoryEpoch


DOC_TYPE_REPLY = "chatbot_sent_reply"
REPLY_RETENTION = timedelta(days=7)
MAX_REPLY_CHARS = 2000
MAX_REPLY_AUDIO_BYTES = 8 * 1024 * 1024


def _epoch_doc(epoch) -> dict | None:
    if isinstance(epoch, MemoryEpoch):
        epoch = {key: getattr(epoch, key) for key in ("global_generation", "guild_generation", "user_generation")}
    if not isinstance(epoch, dict) or set(epoch) != {"global_generation", "guild_generation", "user_generation"}:
        return None
    if any(type(value) is not int or value < 0 for value in epoch.values()):
        return None
    return dict(epoch)


def _clean_attachment(value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        value = {key: getattr(value, key, None) for key in ("id", "filename", "size")}
    ident, size, filename = value.get("id"), value.get("size"), value.get("filename")
    if type(ident) is not int or ident <= 0 or type(size) is not int or not 0 < size <= MAX_REPLY_AUDIO_BYTES:
        return None
    if not isinstance(filename, str) or not filename or len(filename) > 256:
        return None
    result = {"id": ident, "size": size, "filename": filename}
    digest = value.get("sha256")
    if isinstance(digest, str) and len(digest) == 64 and all(char in "0123456789abcdef" for char in digest):
        result["sha256"] = digest
    return result


class ReplyStore:
    def __init__(self, coll, *, clock: Callable[[], datetime] | None = None):
        self._coll = coll
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        now = self._clock()
        return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)

    async def record_sent(
        self, *, guild_id: int, channel_id: int, requester_id: int,
        origin_message_id: int, message_id: int, original_user_text: str,
        text: str = "", spoken_text: str = "", format: str = "text",
        provider: str = "", model: str = "", epoch=None, attachment=None,
    ) -> dict | None:
        """Registra somente depois do envio confirmado; sem época não há reuso."""
        scope = {key: int(value) for key, value in {
            "guild_id": guild_id, "channel_id": channel_id, "requester_id": requester_id,
            "origin_message_id": origin_message_id, "message_id": message_id,
        }.items()}
        if any(value <= 0 for value in scope.values()):
            raise ValueError("A resposta precisa de IDs confirmados do Discord.")
        if format not in {"text", "audio"}:
            raise ValueError("Somente respostas em texto ou áudio podem ser registradas.")
        if not isinstance(text, str) or not isinstance(spoken_text, str) or max(len(text), len(spoken_text)) > MAX_REPLY_CHARS:
            raise ValueError("A resposta excede o limite de conversão fiel.")
        if (format == "text" and not text.strip()) or (format == "audio" and not spoken_text.strip()):
            return None
        clean_epoch = _epoch_doc(epoch)
        if clean_epoch is None:
            return None
        if not isinstance(original_user_text, str) or len(original_user_text) > 8000:
            raise ValueError("A mensagem original excede o limite do registro.")
        now = self._now()
        doc = {
            "_id": f"chatbot-reply:{scope['guild_id']}:{scope['message_id']}",
            "type": DOC_TYPE_REPLY, **scope, "original_user_text": original_user_text,
            "text": text, "spoken_text": spoken_text, "format": format,
            "provider": str(provider or "")[:128], "model": str(model or "")[:128],
            "epoch": clean_epoch, "attachment": _clean_attachment(attachment) if format == "audio" else None,
            "sent_at": now, "expires_at": now + REPLY_RETENTION,
        }
        # IDs globais do Discord + _id estável tornam repetições do registro
        # idempotentes; nenhum conteúdo fornecido ao modelo escolhe um _id.
        await self._coll.update_one({"_id": doc["_id"], "type": DOC_TYPE_REPLY}, {"$setOnInsert": doc}, upsert=True)
        stored = await self._coll.find_one({"_id": doc["_id"], "type": DOC_TYPE_REPLY})
        return deepcopy(stored) if stored is not None else None

    async def get_by_message(self, *, guild_id: int, channel_id: int, message_id: int) -> dict | None:
        doc = await self._coll.find_one({"type": DOC_TYPE_REPLY, "guild_id": int(guild_id),
                                         "channel_id": int(channel_id), "message_id": int(message_id)})
        return doc if self._unexpired(doc) else None

    def _unexpired(self, doc) -> bool:
        if not isinstance(doc, dict) or doc.get("type") != DOC_TYPE_REPLY or doc.get("format") not in {"text", "audio"}:
            return False
        expires = doc.get("expires_at")
        if not isinstance(expires, datetime):
            return False
        expires = expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires
        return expires > self._now() and _epoch_doc(doc.get("epoch")) is not None

    async def latest(self, *, guild_id: int, channel_id: int, requester_id: int) -> list[dict]:
        cursor = self._coll.find({"type": DOC_TYPE_REPLY, "guild_id": int(guild_id),
                                 "channel_id": int(channel_id), "requester_id": int(requester_id),
                                 "expires_at": {"$gt": self._now()}}).sort("sent_at", -1).limit(20)
        return [doc async for doc in cursor if self._unexpired(doc)]

    async def resolve(self, bot, *, guild_id: int, channel_id: int, user_id: int, message_id: int | None = None) -> dict | None:
        """Reply explícito deste canal, ou última resposta válida deste membro."""
        if message_id is not None:
            candidate = await self.get_by_message(guild_id=guild_id, channel_id=channel_id, message_id=message_id)
            candidates = [candidate] if candidate else []
        else:
            candidates = await self.latest(guild_id=guild_id, channel_id=channel_id, requester_id=user_id)
        for record in candidates:
            if await validate_recorded_reply(bot, record, user_id=int(user_id)) is not None:
                return deepcopy(record)
        return None


async def validate_recorded_reply(bot, record: dict, *, user_id: int):
    """Retorna a mensagem nativa atual após acesso, configuração e época novos."""
    try:
        if not isinstance(record, dict) or record.get("type") != DOC_TYPE_REPLY or record.get("format") not in {"text", "audio"}:
            return None
        expires = record.get("expires_at")
        if not isinstance(expires, datetime):
            return None
        expires = expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires
        if expires <= datetime.now(timezone.utc):
            return None
        epoch = _epoch_doc(record.get("epoch"))
        if epoch is None:
            return None
        guild = bot.get_guild(int(record["guild_id"]))
        cog = bot.get_cog("Chatbot")
        store, memory = getattr(cog, "_config", None), getattr(cog, "_memory", None)
        if guild is None or store is None or memory is None:
            return None
        requester = await _fresh_member(guild, int(user_id))
        if requester.bot:
            return None
        config = await store.get_config(guild.id, fresh=True)
        channel = _origin_channel(guild, record, config)
        await _requester_can_view(channel, requester)
        message = await channel.fetch_message(int(record["message_id"]))
        if (int(getattr(getattr(message, "author", None), "id", 0)) != int(bot.user.id)
                or int(getattr(getattr(message, "channel", None), "id", 0)) != int(channel.id)
                or int(getattr(getattr(message, "guild", None), "id", 0)) != int(guild.id)
                or getattr(message, "webhook_id", None) is not None):
            return None
        if record["format"] == "text" and getattr(message, "content", None) != record.get("text"):
            return None  # mensagem editada não prova mais o texto registrado
        if record["format"] == "audio":
            stored = _clean_attachment(record.get("attachment"))
            if stored is None:
                return None
            attachment = next((item for item in getattr(message, "attachments", ())
                               if int(item.id) == stored["id"]), None)
            if (attachment is None or attachment.filename != stored["filename"]
                    or int(attachment.size) != stored["size"]):
                return None  # arquivo removido/substituído não autoriza reuso
        config = await store.get_config(guild.id, fresh=True)
        _origin_channel(guild, record, config)
        if await memory.capture_epoch(guild.id, int(record["requester_id"])) != MemoryEpoch(**epoch):
            return None
        return message
    except (ActionDenied, discord.HTTPException, asyncio.TimeoutError, KeyError, TypeError, ValueError, AttributeError):
        return None
