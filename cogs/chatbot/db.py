"""Acesso ao banco de dados do chatbot.

Usamos uma coleção DEDICADA (`chatbot_data`) no mesmo database do bot, ao
invés de compartilhar a `settings` que já tem índices UNIQUE sobre
`(guild_id, user_id, type)` incompatíveis com profiles (user_id=null em
profile, e múltiplos profiles por guild violariam).

Índices criados aqui:
- `(type, guild_id)` — suporta list_profiles, get_active_profile, list de memória
- `(type, guild_id, profile_id)` UNIQUE quando type=chatbot_profile —
  garante que profile_id não duplica dentro do guild
- `(type, guild_id, scope, profile_id, user_id)` sparse — busca de memória
  pessoal por profile

Índices NÃO-UNIQUE são seguros (não disparam erro ao inserir). O UNIQUE em
profile_id é só pra integridade de dados, nunca conflita com outros cogs.

O `ensure_indexes()` é idempotente — chamado no `cog_load`. Se os índices
já existem, a call é no-op.
"""
from __future__ import annotations

import logging

from . import constants as C

log = logging.getLogger(__name__)


def get_chatbot_collection(settings_db):
    """Retorna o Motor AsyncIOMotorCollection dedicada ao chatbot.

    `settings_db` é o SettingsDB do bot. Usamos apenas `.db` (database obj)
    pra acessar nossa coleção isolada. Zero risco de colidir com outros cogs.
    """
    if settings_db is None or not hasattr(settings_db, "db"):
        return None
    return settings_db.db[C.CHATBOT_COLLECTION_NAME]


async def ensure_indexes(coll) -> None:
    """Cria índices necessários. Idempotente.

    Separamos em 2 funções (esta + get_chatbot_collection) pra poder testar
    cada uma isolada, e pra o setup do cog falhar graciosamente se o índice
    não puder ser criado por algum motivo (log warning, sistema continua).
    """
    if coll is None:
        return

    try:
        # Índice de suporte — acelera list_profiles por guild.
        await coll.create_index(
            [("type", 1), ("guild_id", 1)],
            name="type_1_guild_id_1",
        )

        # UNIQUE em profile_id dentro do guild (só pra docs de profile).
        # Se o profile_id colidir, preferimos receber DuplicateKeyError
        # (bug detectável) do que silenciosamente criar dois profiles
        # com mesmo ID.
        await coll.create_index(
            [("type", 1), ("guild_id", 1), ("profile_id", 1)],
            name="chatbot_profile_unique",
            unique=True,
            partialFilterExpression={"type": "chatbot_profile"},
        )

        # Suporte pra lookup de memória por (profile, user) — sparse pra
        # não gastar espaço com docs sem profile_id (legados).
        await coll.create_index(
            [("type", 1), ("guild_id", 1), ("scope", 1),
             ("profile_id", 1), ("user_id", 1)],
            name="chatbot_memory_lookup",
            sparse=True,
        )

        log.info("chatbot: índices do %s verificados", C.CHATBOT_COLLECTION_NAME)
    except Exception:
        # Não é fatal. Bot continua funcionando, só sem os índices
        # (queries ficam mais lentas mas corretas).
        log.exception("chatbot: falha ao criar índices — continuando")
