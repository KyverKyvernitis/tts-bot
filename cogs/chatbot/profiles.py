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
