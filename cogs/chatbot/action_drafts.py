"""Um rascunho curto por conversa; não cria aprovação nem executa ações."""
from __future__ import annotations

import re
import math
import time
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .action_protocol import ALLOWED_ACTIONS, MAX_AUDIO_TEXT, MAX_REASON, action_options_schema
from .memory import MemoryEpoch
from .tool_registry import InvalidToolArguments, validate_tool_arguments

DOC_TYPE_ACTION_DRAFT = "chatbot_action_draft"
DRAFT_TTL_SECONDS = 600
MAX_ID = (1 << 63) - 1
UNSET = object()
_CANONICAL_ID = re.compile(r"[1-9][0-9]{0,18}\Z", re.ASCII)
_TARGET_ACTIONS = frozenset({"join_voice", "move_voice", "ban_member", "timeout_member", "untimeout_member",
    "kick_member", "unban_member", "assign_role", "remove_role", "change_nickname"})
_REASON_ACTIONS = frozenset({"ban_member", "timeout_member", "untimeout_member", "kick_member", "unban_member",
    "purge_messages", "assign_role", "remove_role", "change_nickname", "edit_channel"})
_OPTION_FIELDS = {
    "timeout_member": {"duration_seconds"}, "assign_role": {"role_ref"}, "remove_role": {"role_ref"},
    "change_nickname": {"nickname"}, "edit_channel": {"channel_ref", "channel_changes"},
    "purge_messages": {"message_refs"},
}


class InvalidDraft(ValueError):
    """Campos estruturados inválidos; não contém argumentos privados."""


class DraftStale(ValueError):
    """Um reset tornou a geração da conversa inválida."""


class DraftConflict(ValueError):
    """O rascunho mudou durante a atualização."""


def missing_fields(action: str, target_id=None, *, text="", reason="", options=None) -> tuple[str, ...]:
    """Faltantes estruturados, sem inventar motivo ou interpretar mensagens."""
    options = options if isinstance(options, dict) else {}
    fields = []
    if action in _TARGET_ACTIONS and not (type(target_id) is int and 0 < target_id <= MAX_ID):
        fields.append("target_id")
    if action in {"send_audio", "speak_voice"} and (not isinstance(text, str) or not text.strip()):
        fields.append("text")
    if action in _REASON_ACTIONS and (not isinstance(reason, str) or not reason.strip()):
        fields.append("reason")
    if action == "timeout_member" and (type(options.get("duration_seconds")) is not int or not 1 <= options["duration_seconds"] <= 2419200):
        fields.append("options.duration_seconds")
    if action in {"assign_role", "remove_role"} and not options.get("role_ref"):
        fields.append("options.role_ref")
    if action == "change_nickname" and "nickname" not in options:
        fields.append("options.nickname")
    if action == "edit_channel":
        if not options.get("channel_ref"):
            fields.append("options.channel_ref")
        if not options.get("channel_changes"):
            fields.append("options.channel_changes")
    if action == "purge_messages" and not options.get("message_refs"):
        fields.append("options.message_refs")
    return tuple(fields)


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise InvalidDraft("Os campos do rascunho não são válidos.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise InvalidDraft("Os campos do rascunho não são válidos.") from None
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise InvalidDraft("Os campos do rascunho não são válidos.")
    return value.strip()


def _fields(action, target_id, text, reason, options):
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        raise InvalidDraft("Escolha uma ação disponível para o rascunho.")
    if target_id is not None and (type(target_id) is not int or not 0 < target_id <= MAX_ID):
        raise InvalidDraft("O alvo precisa ser um membro confirmado pelo sistema.")
    text = _text(text, MAX_AUDIO_TEXT)
    reason = _text(reason, 150 if action == "edit_channel" else MAX_REASON)
    if text and action not in {"send_audio", "speak_voice"}:
        raise InvalidDraft("Esta ação não recebe texto de fala.")
    try:
        options = validate_tool_arguments(options, action_options_schema())
    except InvalidToolArguments:
        raise InvalidDraft("Os parâmetros do rascunho não são válidos.") from None
    if set(options) - _OPTION_FIELDS.get(action, set()):
        raise InvalidDraft("Os parâmetros não pertencem a esta ação.")
    refs = [options[key] for key in ("role_ref", "channel_ref") if key in options]
    refs.extend(options.get("message_refs", ()))
    if any(not _CANONICAL_ID.fullmatch(ref) or int(ref) > MAX_ID for ref in refs):
        raise InvalidDraft("Os recursos precisam ter IDs confirmados pelo sistema.")
    if "nickname" in options and any(ord(character) < 32 for character in options["nickname"]):
        raise InvalidDraft("O apelido não é válido.")
    return {"action": action, "target_id": target_id, "text": text, "reason": reason, "options": options}


class ActionDraftStore:
    def __init__(self, collection, *, memory, clock=time.time):
        self._coll, self._memory, self._clock = collection, memory, clock

    async def ensure_indexes(self):
        await self._coll.create_index(
            [("type", 1), ("guild_id", 1), ("channel_id", 1), ("user_id", 1)],
            unique=True, partialFilterExpression={"type": DOC_TYPE_ACTION_DRAFT},
            name="chatbot_action_draft_scope_v1",
        )
        await self._coll.create_index(
            [("expires_on", 1)], expireAfterSeconds=0,
            partialFilterExpression={"type": DOC_TYPE_ACTION_DRAFT}, name="chatbot_action_draft_expiry_v1",
        )

    async def initialize(self):
        await self.ensure_indexes()

    @staticmethod
    def _scope(guild_id, channel_id, user_id):
        identifiers = guild_id, channel_id, user_id
        if any(type(value) is not int or not 0 < value <= MAX_ID for value in identifiers):
            raise InvalidDraft("O escopo da conversa não é válido.")
        return {"_id": f"chatbot-action-draft:{guild_id}:{channel_id}:{user_id}",
                "type": DOC_TYPE_ACTION_DRAFT, "guild_id": guild_id, "channel_id": channel_id, "user_id": user_id}

    @staticmethod
    def _generation(epoch):
        if not isinstance(epoch, MemoryEpoch) or any(type(value) is not int or value < 0 for value in (
            epoch.global_generation, epoch.guild_generation, epoch.user_generation)):
            raise InvalidDraft("A geração da conversa não é válida.")
        return {"global_generation": epoch.global_generation, "guild_generation": epoch.guild_generation,
                "user_generation": epoch.user_generation}

    async def _check_epoch(self, scope, epoch):
        if self._memory is None or await self._memory.capture_epoch(scope["guild_id"], scope["user_id"]) != epoch:
            raise DraftStale("A conversa foi reiniciada. Comece um novo pedido.")

    def _current(self, doc, scope, generation):
        if not isinstance(doc, dict) or any(doc.get(key) != value for key, value in {**scope, **generation}.items()):
            return None
        if "consumed_plan_id" in doc or "consumed_at" in doc:
            return None
        try:
            if not isinstance(doc.get("draft_id"), str) or type(doc.get("expires_at")) not in {float, int}:
                return None
            UUID(doc["draft_id"])
            if type(doc["revision"]) is not int or not 0 < doc["revision"] < MAX_ID or not math.isfinite(float(doc["expires_at"])) or float(doc["expires_at"]) <= self._clock():
                return None
            values = _fields(doc["action"], doc.get("target_id"), doc.get("text", ""), doc.get("reason", ""), doc.get("options", {}))
            values.update(draft_id=doc["draft_id"], revision=doc["revision"], expires_at=float(doc["expires_at"]))
            values["missing_fields"] = list(missing_fields(**{key: values[key] for key in ("action", "target_id", "text", "reason", "options")}))
            return values
        except (InvalidDraft, KeyError, ValueError, TypeError, OverflowError):
            return None

    async def get_current(self, guild_id, channel_id, user_id, epoch):
        scope, generation = self._scope(guild_id, channel_id, user_id), self._generation(epoch)
        await self._check_epoch(scope, epoch)
        doc = await self._coll.find_one(scope)
        await self._check_epoch(scope, epoch)
        return self._current(doc, scope, generation)

    @staticmethod
    def _expected(current, expected_draft_id, expected_revision):
        if expected_draft_id is not None and (not current or expected_draft_id != current["draft_id"]):
            raise DraftConflict("O rascunho mudou. Consulte o pedido atual antes de alterá-lo.")
        if expected_revision is not None and (type(expected_revision) is not int or not current or expected_revision != current["revision"]):
            raise DraftConflict("O rascunho mudou. Consulte o pedido atual antes de alterá-lo.")

    async def save(self, guild_id, channel_id, user_id, epoch, *, action=None, target_id=UNSET,
                   text=UNSET, reason=UNSET, options=UNSET, expected_draft_id=None, expected_revision=None):
        scope, generation = self._scope(guild_id, channel_id, user_id), self._generation(epoch)
        await self._check_epoch(scope, epoch)
        previous = await self._coll.find_one(scope)
        current = self._current(previous, scope, generation)
        self._expected(current, expected_draft_id, expected_revision)
        selected_action = action if action is not None else current["action"] if current else None
        same_target = current and (target_id is UNSET or target_id == current["target_id"])
        base = current if current and current["action"] == selected_action and same_target else {"target_id": None, "text": "", "reason": "", "options": {}}
        merged_options = deepcopy(base["options"])
        if options is not UNSET:
            if not isinstance(options, dict):
                raise InvalidDraft("Os parâmetros do rascunho não são válidos.")
            for key, value in options.items():
                if key == "channel_changes" and isinstance(value, dict):
                    merged_options[key] = {**merged_options.get(key, {}), **value}
                else:
                    merged_options[key] = deepcopy(value)
        values = _fields(selected_action, base["target_id"] if target_id is UNSET else target_id,
                         base["text"] if text is UNSET else text, base["reason"] if reason is UNSET else reason, merged_options)
        now = self._clock()
        doc = {**scope, **generation, **values,
               "draft_id": current["draft_id"] if current else str(uuid4()),
               "revision": current["revision"] + 1 if current else 1,
               "created_at": previous.get("created_at", now) if current else now, "updated_at": now,
               "expires_at": now + DRAFT_TTL_SECONDS,
               "expires_on": datetime.fromtimestamp(now + DRAFT_TTL_SECONDS, timezone.utc)}
        await self._check_epoch(scope, epoch)
        try:
            if previous is None:
                await self._coll.insert_one(doc)
            else:
                query = {**scope, "draft_id": previous.get("draft_id"), "revision": previous.get("revision")}
                changed = await self._coll.find_one_and_replace(query, doc, return_document=ReturnDocument.AFTER)
                if changed is None:
                    await self._check_epoch(scope, epoch)
                    raise DraftConflict("O rascunho mudou. Consulte o pedido atual antes de alterá-lo.")
        except DuplicateKeyError:
            await self._check_epoch(scope, epoch)
            raise DraftConflict("O rascunho mudou. Consulte o pedido atual antes de alterá-lo.") from None
        try:
            await self._check_epoch(scope, epoch)
        except DraftStale:
            await self._coll.delete_one({**scope, **generation, "draft_id": doc["draft_id"], "revision": doc["revision"]})
            raise
        saved = self._current(doc, scope, generation)
        if saved is None:
            raise DraftConflict("O rascunho expirou. Comece um novo pedido.")
        return saved

    async def update(self, *args, **kwargs):
        return await self.save(*args, **kwargs)

    async def consume(self, guild_id, channel_id, user_id, epoch, *, expected_draft_id, expected_revision, plan_id):
        """Reserva o rascunho uma vez antes de persistir um plano; sem rollback."""
        scope, generation = self._scope(guild_id, channel_id, user_id), self._generation(epoch)
        for identifier in (expected_draft_id, plan_id):
            if not isinstance(identifier, str):
                raise InvalidDraft("O vínculo do rascunho não é válido.")
            try:
                UUID(identifier)
            except (ValueError, AttributeError):
                raise InvalidDraft("O vínculo do rascunho não é válido.") from None
        if type(expected_revision) is not int or not 0 < expected_revision < MAX_ID:
            raise InvalidDraft("A revisão do rascunho não é válida.")
        await self._check_epoch(scope, epoch)
        current = await self._coll.find_one({**scope, **generation, "draft_id": expected_draft_id, "revision": expected_revision})
        if self._current(current, scope, generation) is None:
            await self._check_epoch(scope, epoch)
            return False
        now = self._clock()
        changed = await self._coll.find_one_and_update(
            {**scope, **generation, "draft_id": expected_draft_id, "revision": expected_revision,
             "consumed_plan_id": {"$exists": False}, "consumed_at": {"$exists": False},
             "expires_at": {"$gt": now}},
            {"$set": {"consumed_plan_id": plan_id, "consumed_at": now}, "$inc": {"revision": 1}},
            return_document=ReturnDocument.AFTER,
        )
        await self._check_epoch(scope, epoch)
        return changed is not None

    async def cancel(self, guild_id, channel_id, user_id, epoch, *, expected_draft_id=None, expected_revision=None):
        scope, generation = self._scope(guild_id, channel_id, user_id), self._generation(epoch)
        current = await self.get_current(guild_id, channel_id, user_id, epoch)
        if current is None:
            return False
        self._expected(current, expected_draft_id, expected_revision)
        result = await self._coll.delete_one({**scope, **generation, "draft_id": current["draft_id"], "revision": current["revision"]})
        await self._check_epoch(scope, epoch)
        return bool(result.deleted_count)
