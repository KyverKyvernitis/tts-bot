"""Coleção e índices exclusivos da conversação do bot."""
from __future__ import annotations

import logging

from . import constants as C
from .action_store import DOC_TYPE_ACTION_REQUEST
from .preferences import DOC_TYPE_PREFERENCES
from .reply_store import DOC_TYPE_REPLY
from .tool_memory import FACT_TYPE
from .knowledge import KNOWLEDGE_TYPE

log = logging.getLogger(__name__)

# A aposentadoria só acontece ao fim da migração, após validar o arquivo.
LEGACY_INDEX_NAMES = (
    "chatbot_profile_unique",
    "chatbot_memory_lookup",
    "chatbot_memory_v2_unique",
    "chatbot_msg_map_lookup",
    "chatbot_msg_map_ttl",
    "chatbot_extrovert_unique",
    "chatbot_webhook_unique",
)


def get_chatbot_collection(settings_db):
    """Mantém dados do chatbot separados das configurações de outros cogs."""
    if settings_db is None or not hasattr(settings_db, "db"):
        return None
    return settings_db.db[C.CHATBOT_COLLECTION_NAME]


async def ensure_indexes(coll) -> None:
    """Cria índices V3; falhas impedem iniciar armazenamento sem integridade."""
    if coll is None:
        return
    specs = [
        ([("type", 1), ("guild_id", 1)], {"name": "type_1_guild_id_1"}),
        (
            [
                ("type", 1), ("scope", 1), ("guild_id", 1),
                ("channel_id", 1), ("visibility_scope", 1),
                ("global_generation", 1), ("guild_generation", 1),
                ("user_id", 1), ("user_generation", 1),
            ],
            {
                "name": "chatbot_memory_v3_unique",
                "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_MEMORY_V3},
            },
        ),
        (
            [("type", 1), ("epoch_key", 1)],
            {
                "name": "chatbot_memory_epoch_unique", "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_MEMORY_EPOCH},
            },
        ),
        (
            [("type", 1), ("guild_id", 1)],
            {
                "name": "chatbot_guild_config_unique", "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_GUILD_CONFIG},
            },
        ),
        (
            [("type", 1)],
            {
                "name": "chatbot_master_singleton", "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_MASTER},
            },
        ),
        (
            [("type", 1), ("message_id", 1)],
            {
                "name": "chatbot_bot_message_unique", "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_MESSAGE_MAP},
            },
        ),
        (
            [("expires_at", 1)],
            {
                "name": "chatbot_bot_message_ttl", "expireAfterSeconds": 0,
                "partialFilterExpression": {"type": C.DOC_TYPE_MESSAGE_MAP},
            },
        ),
        (
            [("type", 1), ("migration_id", 1)],
            {
                "name": "chatbot_migration_unique", "unique": True,
                "partialFilterExpression": {"type": C.DOC_TYPE_MIGRATION},
            },
        ),
        (
            [("type", 1), ("request_id", 1)],
            {
                "name": "chatbot_action_request_unique", "unique": True,
                "partialFilterExpression": {"type": DOC_TYPE_ACTION_REQUEST},
            },
        ),
        (
            [("delete_at", 1)],
            {
                "name": "chatbot_action_request_cleanup", "expireAfterSeconds": 0,
                "partialFilterExpression": {"type": DOC_TYPE_ACTION_REQUEST},
            },
        ),
        (
            [("type", 1), ("guild_id", 1), ("channel_id", 1), ("message_id", 1)],
            {
                "name": "chatbot_action_message_lookup",
                "partialFilterExpression": {"type": DOC_TYPE_ACTION_REQUEST},
            },
        ),
        (
            [("type", 1), ("plan_id", 1), ("step_index", 1)],
            {
                "name": "chatbot_action_plan_lookup",
                "partialFilterExpression": {"type": DOC_TYPE_ACTION_REQUEST},
            },
        ),
        (
            [("type", 1), ("guild_id", 1), ("channel_id", 1),
             ("requester_id", 1), ("finished_at", -1)],
            {
                "name": "chatbot_action_result_lookup",
                "partialFilterExpression": {"type": DOC_TYPE_ACTION_REQUEST},
            },
        ),
        (
            [("type", 1), ("state", 1), ("expires_at", 1)],
            {
                "name": "chatbot_action_pending_lookup",
                "partialFilterExpression": {"type": DOC_TYPE_ACTION_REQUEST},
            },
        ),
        (
            [("type", 1), ("state", 1), ("executing_at", 1)],
            {
                "name": "chatbot_action_recovery_lookup",
                "partialFilterExpression": {"type": DOC_TYPE_ACTION_REQUEST},
            },
        ),
    ]
    for kind, name, keys in (
        (DOC_TYPE_PREFERENCES, "chatbot_preferences_lookup",
         [("type", 1), ("guild_id", 1), ("channel_id", 1), ("user_id", 1),
          ("global_generation", 1), ("guild_generation", 1), ("user_generation", 1)]),
        (DOC_TYPE_REPLY, "chatbot_reply_lookup",
         [("type", 1), ("guild_id", 1), ("channel_id", 1), ("requester_id", 1), ("sent_at", -1)]),
        (FACT_TYPE, "chatbot_fact_lookup",
         [("type", 1), ("guild_id", 1), ("channel_id", 1), ("user_id", 1),
          ("visibility_scope", 1), ("global_generation", 1), ("guild_generation", 1),
          ("user_generation", 1), ("updated_at", -1)]),
        (KNOWLEDGE_TYPE, "chatbot_knowledge_scope_lookup",
         [("type", 1), ("guild_id", 1), ("global_generation", 1), ("guild_generation", 1),
          ("channel_id", 1), ("visibility_scope", 1), ("updated_at", -1)]),
    ):
        specs.append((keys, {"name": name, "partialFilterExpression": {"type": kind}}))
    specs.extend([
        ([("type", 1), ("knowledge_ref", 1)],
         {"name": "chatbot_knowledge_ref_unique", "unique": True,
          "partialFilterExpression": {"type": KNOWLEDGE_TYPE}}),
        ([("type", 1), ("guild_id", 1), ("global_generation", 1),
          ("guild_generation", 1), ("entry_slot", 1)],
         {"name": "chatbot_knowledge_slot_unique", "unique": True,
          "partialFilterExpression": {"type": KNOWLEDGE_TYPE}}),
        ([("type", 1), ("guild_id", 1), ("channel_id", 1), ("user_id", 1)],
         {"name": "chatbot_action_draft_scope_v1", "unique": True,
          "partialFilterExpression": {"type": "chatbot_action_draft"}}),
        ([("expires_on", 1)],
         {"name": "chatbot_action_draft_expiry_v1", "expireAfterSeconds": 0,
          "partialFilterExpression": {"type": "chatbot_action_draft"}}),
        ([("type", 1), ("message_id", 1)],
         {"name": "chatbot_reply_unique", "unique": True,
          "partialFilterExpression": {"type": DOC_TYPE_REPLY}}),
        ([("expires_at", 1)],
         {"name": "chatbot_reply_ttl", "expireAfterSeconds": 0,
          "partialFilterExpression": {"type": DOC_TYPE_REPLY}}),
    ])
    for keys, kwargs in specs:
        try:
            await coll.create_index(keys, **kwargs)
        except Exception as exc:
            # Erros Mongo podem conter documentos; registrar só o nome e classe.
            log.error("chatbot: índice %s falhou (%s)", kwargs["name"], type(exc).__name__)
            raise RuntimeError("Não foi possível garantir os índices do chatbot") from None
    log.info("chatbot: índices V3 verificados")


async def retire_legacy_indexes(coll) -> None:
    """Remove somente índices conhecidos do armazenamento aposentado."""
    existing = await coll.index_information()
    for name in LEGACY_INDEX_NAMES:
        if name in existing:
            await coll.drop_index(name)
