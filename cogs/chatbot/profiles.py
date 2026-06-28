"""CRUD de profiles de chatbot sobre a coleção `settings` existente.

Decisão de esquema:
- Reusar a mesma coleção Mongo que `db.py:SettingsDB` usa.
- Docs têm `type="chatbot_profile"` (separados dos `type="guild"` etc).
- Chave natural: (guild_id, profile_id). profile_id é slug gerado do nome +
  aleatoriedade (pra evitar colisão ao renomear).
- UM campo `active` = True no doc do profile atualmente selecionado pelo server.
  Como só um pode estar ativo por guild, a lógica de ativação desativa os outros.

Segurança:
- Nenhum import de `db.py` aqui. Recebemos a instância pronta (injeção).
- Todas as validações de permissão (staff? limite atingido?) ficam na camada
  de cog — este módulo é só persistência.

Footprint:
- Não guardamos profiles em RAM aqui; o cache fica em `webhooks.py`/`cog.py`
  que consomem o profile ativo.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import constants as C

log = logging.getLogger(__name__)


@dataclass
class ChatbotProfile:
    """Estrutura in-memory de um profile. Serializa como o doc Mongo."""

    guild_id: int
    profile_id: str
    name: str
    avatar_url: str = ""
    system_prompt: str = ""
    temperature: float = C.DEFAULT_TEMPERATURE
    history_size: int = C.DEFAULT_HISTORY_SIZE
    active: bool = False
    # tts_chance: probabilidade 0-1 de o bot responder com áudio ANEXO
    # (além do texto) em cada mensagem, mesmo sem o user pedir. 0 = só
    # quando pedirem explicitamente; 1 = sempre. Default 0.
    # Permite profiles tipo "locutor" falarem muito, ou mudos tipo "sussurrador".
    tts_chance: float = 0.0
    profile_kind: str = C.PROFILE_KIND_NORMAL
    source_user_id: int = 0
    source_channel_id: int = 0
    dynamic_identity: bool = False
    fallback_name: str = ""
    fallback_avatar_url: str = ""
    persona_sample_count: int = 0
    persona_generated_at: float = 0.0
    created_by: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_doc(cls, doc: dict) -> "ChatbotProfile":
        return cls(
            guild_id=int(doc.get("guild_id") or 0),
            profile_id=str(doc.get("profile_id") or ""),
            name=str(doc.get("name") or ""),
            avatar_url=str(doc.get("avatar_url") or ""),
            system_prompt=str(doc.get("system_prompt") or ""),
            temperature=float(doc.get("temperature") or C.DEFAULT_TEMPERATURE),
            history_size=int(doc.get("history_size") or C.DEFAULT_HISTORY_SIZE),
            active=bool(doc.get("active") or False),
            tts_chance=float(doc.get("tts_chance") or 0.0),
            profile_kind=str(doc.get("profile_kind") or C.PROFILE_KIND_NORMAL),
            source_user_id=int(doc.get("source_user_id") or 0),
            source_channel_id=int(doc.get("source_channel_id") or 0),
            dynamic_identity=bool(doc.get("dynamic_identity") or False),
            fallback_name=str(doc.get("fallback_name") or ""),
            fallback_avatar_url=str(doc.get("fallback_avatar_url") or ""),
            persona_sample_count=int(doc.get("persona_sample_count") or 0),
            persona_generated_at=float(doc.get("persona_generated_at") or 0.0),
            created_by=int(doc.get("created_by") or 0),
            created_at=float(doc.get("created_at") or time.time()),
            updated_at=float(doc.get("updated_at") or time.time()),
        )

    def to_doc(self) -> dict:
        return {
            "type": C.DOC_TYPE_PROFILE,
            "guild_id": self.guild_id,
            "profile_id": self.profile_id,
            "name": self.name,
            "avatar_url": self.avatar_url,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "history_size": self.history_size,
            "active": self.active,
            "tts_chance": self.tts_chance,
            "profile_kind": self.profile_kind,
            "source_user_id": self.source_user_id,
            "source_channel_id": self.source_channel_id,
            "dynamic_identity": self.dynamic_identity,
            "fallback_name": self.fallback_name,
            "fallback_avatar_url": self.fallback_avatar_url,
            "persona_sample_count": self.persona_sample_count,
            "persona_generated_at": self.persona_generated_at,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _slugify(name: str) -> str:
    """Slug simples: minúsculas, ascii-only, separador '-'. Se vazio, 'profile'."""
    stripped = re.sub(r"[^a-z0-9]+", "-", name.lower().strip())
    stripped = stripped.strip("-")
    return stripped[:40] if stripped else "profile"


def _new_profile_id(name: str) -> str:
    """profile_id único = slug + sufixo aleatório curto. Evita colisão em renomes."""
    return f"{_slugify(name)}-{secrets.token_hex(3)}"


class ProfileStore:
    """Camada de persistência de profiles. Recebe a collection do chatbot
    (dedicada, não a `settings` do bot — evita colisão de índices).

    Todos os métodos são async porque vão ao Mongo via motor.
    """

    def __init__(self, chatbot_coll):
        # `chatbot_coll` é o Motor AsyncIOMotorCollection da coleção
        # `chatbot_data` (ou o que `C.CHATBOT_COLLECTION_NAME` definir).
        # Ver cogs/chatbot/db.py:get_chatbot_collection.
        self._coll = chatbot_coll

    # --- Leitura ---------------------------------------------------------------

    async def list_profiles(self, guild_id: int) -> list[ChatbotProfile]:
        cursor = self._coll.find({"type": C.DOC_TYPE_PROFILE, "guild_id": int(guild_id)})
        out: list[ChatbotProfile] = []
        async for doc in cursor:
            out.append(ChatbotProfile.from_doc(doc))
        # ordena por created_at (mais antigo primeiro) pra lista estável
        out.sort(key=lambda p: p.created_at)
        return out

    async def get_profile(self, guild_id: int, profile_id: str) -> Optional[ChatbotProfile]:
        doc = await self._coll.find_one({
            "type": C.DOC_TYPE_PROFILE,
            "guild_id": int(guild_id),
            "profile_id": str(profile_id),
        })
        return ChatbotProfile.from_doc(doc) if doc else None

    async def get_active_profile(self, guild_id: int) -> Optional[ChatbotProfile]:
        doc = await self._coll.find_one({
            "type": C.DOC_TYPE_PROFILE,
            "guild_id": int(guild_id),
            "active": True,
        })
        return ChatbotProfile.from_doc(doc) if doc else None

    async def count_profiles(self, guild_id: int) -> int:
        return await self._coll.count_documents({
            "type": C.DOC_TYPE_PROFILE,
            "guild_id": int(guild_id),
        })

    async def get_user_style_profile(
        self, guild_id: int, source_user_id: int
    ) -> Optional[ChatbotProfile]:
        doc = await self._coll.find_one({
            "type": C.DOC_TYPE_PROFILE,
            "guild_id": int(guild_id),
            "profile_kind": C.PROFILE_KIND_USER_STYLE,
            "source_user_id": int(source_user_id),
        })
        return ChatbotProfile.from_doc(doc) if doc else None

    # --- Escrita ---------------------------------------------------------------

    async def create_profile(
        self,
        *,
        guild_id: int,
        name: str,
        created_by: int,
        system_prompt: str = "",
        avatar_url: str = "",
        temperature: float = C.DEFAULT_TEMPERATURE,
        history_size: int = C.DEFAULT_HISTORY_SIZE,
    ) -> ChatbotProfile:
        """Cria profile. NÃO valida limite de MAX_PROFILES_PER_GUILD — é
        responsabilidade do cog (lá é onde fica a msg de erro user-facing)."""
        now = time.time()
        profile = ChatbotProfile(
            guild_id=int(guild_id),
            profile_id=_new_profile_id(name),
            name=name.strip()[:C.MAX_NAME_LENGTH],
            avatar_url=avatar_url.strip()[:C.MAX_AVATAR_URL_LENGTH],
            system_prompt=system_prompt.strip()[:C.MAX_SYSTEM_EXTRA_LENGTH],
            temperature=max(C.MIN_TEMPERATURE, min(C.MAX_TEMPERATURE, float(temperature))),
            history_size=max(1, min(C.MAX_HISTORY_SIZE, int(history_size))),
            active=False,
            created_by=int(created_by),
            created_at=now,
            updated_at=now,
        )
        await self._coll.insert_one(profile.to_doc())
        return profile

    async def update_profile(
        self,
        guild_id: int,
        profile_id: str,
        *,
        name: Optional[str] = None,
        avatar_url: Optional[str] = None,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        history_size: Optional[int] = None,
    ) -> Optional[ChatbotProfile]:
        """Edita campos do profile. Retorna o profile atualizado ou None se não existe."""
        updates: dict[str, Any] = {"updated_at": time.time()}
        if name is not None:
            updates["name"] = name.strip()[:C.MAX_NAME_LENGTH]
        if avatar_url is not None:
            updates["avatar_url"] = avatar_url.strip()[:C.MAX_AVATAR_URL_LENGTH]
        if system_prompt is not None:
            updates["system_prompt"] = system_prompt.strip()[:C.MAX_SYSTEM_EXTRA_LENGTH]
        if temperature is not None:
            updates["temperature"] = max(
                C.MIN_TEMPERATURE, min(C.MAX_TEMPERATURE, float(temperature))
            )
        if history_size is not None:
            updates["history_size"] = max(1, min(C.MAX_HISTORY_SIZE, int(history_size)))

        result = await self._coll.find_one_and_update(
            {
                "type": C.DOC_TYPE_PROFILE,
                "guild_id": int(guild_id),
                "profile_id": str(profile_id),
            },
            {"$set": updates},
            return_document=True,  # motor: retorna o doc depois do update
        )
        return ChatbotProfile.from_doc(result) if result else None

    async def delete_profile(self, guild_id: int, profile_id: str) -> bool:
        result = await self._coll.delete_one({
            "type": C.DOC_TYPE_PROFILE,
            "guild_id": int(guild_id),
            "profile_id": str(profile_id),
        })
        return result.deleted_count > 0

    async def upsert_user_style_profile(
        self,
        *,
        guild_id: int,
        source_user_id: int,
        source_channel_id: int,
        created_by: int,
        fallback_name: str,
        fallback_avatar_url: str,
        system_prompt: str,
        sample_count: int,
        activate: bool = False,
    ) -> tuple[ChatbotProfile, bool]:
        """Cria ou atualiza o profile especial vinculado a um usuário.

        Retorna (profile, created). O profile_id é determinístico para que
        `/chatbot persona` atualize a persona do usuário sem criar duplicatas.
        """
        now = time.time()
        guild_id_i = int(guild_id)
        source_user_id_i = int(source_user_id)
        profile_id = f"persona-{source_user_id_i}"
        existing = await self.get_user_style_profile(guild_id_i, source_user_id_i)
        created = existing is None

        updates: dict[str, Any] = {
            "type": C.DOC_TYPE_PROFILE,
            "guild_id": guild_id_i,
            "profile_id": profile_id,
            "name": fallback_name.strip()[:C.MAX_NAME_LENGTH] or "Persona",
            "avatar_url": fallback_avatar_url.strip()[:C.MAX_AVATAR_URL_LENGTH],
            "system_prompt": system_prompt.strip()[:C.MAX_SYSTEM_EXTRA_LENGTH],
            "temperature": C.DEFAULT_TEMPERATURE,
            "history_size": C.DEFAULT_HISTORY_SIZE,
            "profile_kind": C.PROFILE_KIND_USER_STYLE,
            "source_user_id": source_user_id_i,
            "source_channel_id": int(source_channel_id),
            "dynamic_identity": True,
            "fallback_name": fallback_name.strip()[:C.MAX_NAME_LENGTH] or "Persona",
            "fallback_avatar_url": fallback_avatar_url.strip()[:C.MAX_AVATAR_URL_LENGTH],
            "persona_sample_count": int(sample_count),
            "persona_generated_at": now,
            "created_by": int(created_by if created else (existing.created_by if existing else created_by)),
            "updated_at": now,
        }
        if created:
            updates["created_at"] = now
            updates["active"] = False
        else:
            # Preserva flags editadas manualmente em versões futuras.
            updates["active"] = bool(existing.active)
            updates["tts_chance"] = float(existing.tts_chance)
            updates["created_at"] = float(existing.created_at)

        await self._coll.update_one(
            {
                "type": C.DOC_TYPE_PROFILE,
                "guild_id": guild_id_i,
                "profile_id": profile_id,
            },
            {"$set": updates},
            upsert=True,
        )
        if activate:
            activated = await self.set_active_profile(guild_id_i, profile_id)
            if activated is not None:
                return activated, created
        profile = await self.get_profile(guild_id_i, profile_id)
        if profile is None:
            # Defensivo: não deveria acontecer depois do upsert.
            profile = ChatbotProfile.from_doc(updates)
        return profile, created

    async def delete_user_style_profile(self, guild_id: int, source_user_id: int) -> bool:
        result = await self._coll.delete_one({
            "type": C.DOC_TYPE_PROFILE,
            "guild_id": int(guild_id),
            "profile_kind": C.PROFILE_KIND_USER_STYLE,
            "source_user_id": int(source_user_id),
        })
        return result.deleted_count > 0

    async def set_active_profile(self, guild_id: int, profile_id: str) -> Optional[ChatbotProfile]:
        """Ativa `profile_id` e desativa qualquer outro no mesmo guild.

        Retorna o profile ativado, ou None se `profile_id` não existe.
        Dois updates — não é atômico, mas o pior caso é dois ativos
        temporariamente (resolvido no próximo call de `get_active_profile`
        que só retorna um).
        """
        # desativa todos outros do mesmo guild
        await self._coll.update_many(
            {
                "type": C.DOC_TYPE_PROFILE,
                "guild_id": int(guild_id),
                "profile_id": {"$ne": str(profile_id)},
                "active": True,
            },
            {"$set": {"active": False, "updated_at": time.time()}},
        )
        # ativa o escolhido
        result = await self._coll.find_one_and_update(
            {
                "type": C.DOC_TYPE_PROFILE,
                "guild_id": int(guild_id),
                "profile_id": str(profile_id),
            },
            {"$set": {"active": True, "updated_at": time.time()}},
            return_document=True,
        )
        return ChatbotProfile.from_doc(result) if result else None

    async def deactivate_all(self, guild_id: int) -> int:
        """Desativa todos os profiles do guild. Retorna quantos foram afetados."""
        result = await self._coll.update_many(
            {
                "type": C.DOC_TYPE_PROFILE,
                "guild_id": int(guild_id),
                "active": True,
            },
            {"$set": {"active": False, "updated_at": time.time()}},
        )
        return result.modified_count
