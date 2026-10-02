"""Coleção e índices exclusivos da conversação do bot."""
from __future__ import annotations

import logging

from . import constants as C

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
    ]
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
