"""Migração retomável para um chatbot único, sem reutilizar personagens."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import constants as C
from .db import retire_legacy_indexes

log = logging.getLogger(__name__)

MIGRATION_ID = "chatbot-v3-single-bot"
LEGACY_COLLECTION_NAME = "chatbot_legacy_v2"
# Somente esta migração conhece os formatos de personagens aposentados.
LEGACY_DOCUMENT_TYPES = (
    "chatbot_profile", "chatbot_memory", "chatbot_memory_v2",
    "chatbot_msg_map", "chatbot_webhook", "chatbot_extrovert",
)
LEGACY_CONFIG_FIELDS = (
    "active_profile_id", "profile_id", "profile_revision", "profile_ids",
    "system_prompt", "name", "avatar_url", "temperature", "history_size",
    "tts_chance", "profile_kind", "source_user_id", "source_channel_id",
    "dynamic_identity", "fallback_name", "fallback_avatar_url",
    "persona_sample_count", "persona_generated_at", "max_profiles_per_message",
)


@dataclass(frozen=True)
class MigrationReport:
    already_applied: bool = False
    archived_documents: int = 0
    legacy_documents_removed: int = 0
    configs_updated: int = 0
    configs_created: int = 0
    master_updated: bool = False


async def _archive_verified(archive, doc: dict) -> None:
    """A chave original permite retomar sem duplicar ou substituir o arquivo."""
    if "_id" not in doc:
        raise RuntimeError("Documento legado sem identificador")
    await archive.update_one(
        {"_id": doc["_id"]}, {"$setOnInsert": dict(doc)}, upsert=True,
    )
    archived = await archive.find_one({"_id": doc["_id"]})
    if archived != doc:
        raise RuntimeError("A cópia do documento legado não foi validada")


def _needs_config_migration(doc: dict) -> bool:
    return (
        doc.get("schema_version") != C.CHATBOT_SCHEMA_VERSION
        or any(field in doc for field in LEGACY_CONFIG_FIELDS)
    )


async def run_migrations(coll) -> MigrationReport:
    """Arquiva antes de mudar/remover; qualquer falha bloqueia o chatbot.

    O arquivo é uma coleção separada sem TTL. Configurações mantêm exatamente
    seu enabled. Memória V3 começa vazia e épocas de reset permanecem intactas.
    Nenhum webhook é removido no Discord.
    """
    if coll is None:
        return MigrationReport()
    archive = coll.database[LEGACY_COLLECTION_NAME]
    marker_query = {"type": C.DOC_TYPE_MIGRATION, "migration_id": MIGRATION_ID}
    marker = await coll.find_one(marker_query)
    previously_complete = bool(marker and marker.get("status") == "complete")
    now = time.time()
    await coll.update_one(
        marker_query,
        {"$set": {
            **marker_query, "schema_version": C.CHATBOT_SCHEMA_VERSION,
            "status": "running", "started_at": now,
        }}, upsert=True,
    )

    removed = 0
    cursor = coll.find({"type": {"$in": list(LEGACY_DOCUMENT_TYPES)}})
    async for doc in cursor:
        await _archive_verified(archive, doc)
        # Não apagar uma versão modificada por um processo concorrente.
        result = await coll.delete_one(doc)
        if not result.deleted_count and await coll.find_one({"_id": doc["_id"]}) is not None:
            raise RuntimeError("Documento legado mudou durante a migração")
        removed += int(result.deleted_count)

    configs_updated = 0
    cursor = coll.find({"type": C.DOC_TYPE_GUILD_CONFIG})
    async for doc in cursor:
        if not _needs_config_migration(doc):
            continue
        await _archive_verified(archive, doc)
        settings = {
            "schema_version": C.CHATBOT_SCHEMA_VERSION,
            "channel_ids": [],
            "spontaneous_enabled": False,
            "spontaneous_channel_ids": [],
            "spontaneous_chance_percent": C.SPONTANEOUS_DEFAULT_CHANCE_PERCENT,
            "updated_at": now,
            "updated_by": 0,
        }
        result = await coll.update_one(
            doc,
            {"$set": settings, "$unset": {field: "" for field in LEGACY_CONFIG_FIELDS}},
        )
        current = await coll.find_one({"_id": doc["_id"]})
        if current is None or _needs_config_migration(current) or current.get("enabled") != doc.get("enabled"):
            raise RuntimeError("Configuração mudou durante a migração")
        configs_updated += int(result.modified_count)

    # Instalações V1 podiam não ter configuração explícita. O arquivo é a
    # fonte também na retomada, quando a origem já foi removida.
    legacy_enabled: dict[int, bool] = {}
    async for doc in archive.find({"type": "chatbot_profile"}):
        guild_id = int(doc.get("guild_id") or 0)
        if guild_id > 0:
            legacy_enabled[guild_id] = legacy_enabled.get(guild_id, False) or bool(doc.get("active"))
    configs_created = 0
    for guild_id, enabled in legacy_enabled.items():
        result = await coll.update_one(
            {"type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": guild_id},
            {"$setOnInsert": {
                "type": C.DOC_TYPE_GUILD_CONFIG, "guild_id": guild_id,
                "schema_version": C.CHATBOT_SCHEMA_VERSION, "enabled": enabled,
                "channel_ids": [], "spontaneous_enabled": False,
                "spontaneous_channel_ids": [],
                "spontaneous_chance_percent": C.SPONTANEOUS_DEFAULT_CHANCE_PERCENT,
                "created_at": now, "updated_at": now, "updated_by": 0,
            }}, upsert=True,
        )
        configs_created += int(result.upserted_id is not None)

    master_updated = False
    master = await coll.find_one({"type": C.DOC_TYPE_MASTER})
    if master and master.get("schema_version") != C.CHATBOT_SCHEMA_VERSION:
        await _archive_verified(archive, master)
        result = await coll.update_one(
            master,
            {"$set": {
                "schema_version": C.CHATBOT_SCHEMA_VERSION,
                "prompt": C.DEFAULT_MASTER_PROMPT,
                "updated_at": 0.0,
                "updated_by": 0,
            }},
        )
        current = await coll.find_one({"_id": master["_id"]})
        if current is None or current.get("schema_version") != C.CHATBOT_SCHEMA_VERSION:
            raise RuntimeError("Instruções globais mudaram durante a migração")
        if current.get("config_guild_id") != master.get("config_guild_id"):
            raise RuntimeError("Servidor de configuração mudou durante a migração")
        master_updated = bool(result.modified_count)

    if await coll.count_documents({"type": {"$in": list(LEGACY_DOCUMENT_TYPES)}}):
        raise RuntimeError("Ainda existem documentos legados no armazenamento ativo")
    async for doc in coll.find({"type": C.DOC_TYPE_GUILD_CONFIG}):
        if _needs_config_migration(doc):
            raise RuntimeError("Ainda existem configurações legadas no armazenamento ativo")
    archived_count = await archive.count_documents({})
    # Índices antigos só são aposentados depois de todas as cópias verificadas.
    await retire_legacy_indexes(coll)
    # Contagens derivadas do arquivo também cobrem interrupções depois de apagar
    # a origem e antes de finalizar o marcador.
    total_removed = await archive.count_documents({"type": {"$in": list(LEGACY_DOCUMENT_TYPES)}})
    total_configs = await archive.count_documents({"type": C.DOC_TYPE_GUILD_CONFIG})
    await coll.update_one(
        marker_query,
        {"$set": {
            "status": "complete", "completed_at": time.time(),
            "archived_documents": archived_count,
            "legacy_documents_removed": total_removed,
            "configs_updated": total_configs,
            "master_updated": bool((marker or {}).get("master_updated") or master_updated),
        }},
    )
    log.info(
        "chatbot: migração V3 concluída (arquivo=%s removidos=%s configs=%s)",
        archived_count, removed, configs_updated,
    )
    return MigrationReport(
        already_applied=previously_complete and not (removed or configs_updated or configs_created or master_updated),
        archived_documents=archived_count,
        legacy_documents_removed=removed,
        configs_updated=configs_updated,
        configs_created=configs_created,
        master_updated=master_updated,
    )
