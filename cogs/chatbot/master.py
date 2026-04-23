"""System prompt mestre — global pra todos os profiles em todos os servers.

É um doc único na coll dedicada (`chatbot_data`), identificado por
`type=chatbot_master, _key="singleton"`. Contém:
- `content`: o texto do prompt (sobe antes do HARD_PREAMBLE em todos os calls)
- `config_guild_id`: qual server tem permissão pra editar isso

Política de edição:
- Staff do config_guild_id pode editar (`/chatbot master editar`)
- O próprio config_guild_id pode ser reatribuído, MAS só por staff do
  config_guild_id atual (safeguard anti-hijack)
- Primeira edição após deploy: se nunca foi configurado, DEFAULT_MASTER_CONFIG_GUILD_ID
  vale como config inicial

O doc é cacheado em RAM com TTL curto pra não martelar Mongo — é lido em
TODA mensagem que o bot responde.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from . import constants as C
from .lru_cache import LRUCacheTTL

log = logging.getLogger(__name__)


# Key interna pra achar o doc — é sempre único ("singleton").
_SINGLETON_KEY = "singleton"


@dataclass
class MasterConfig:
    """Estado atual do master prompt + config server."""
    content: str
    config_guild_id: int
    updated_at: float = 0.0
    updated_by: int = 0  # user_id

    @classmethod
    def default(cls) -> "MasterConfig":
        """Retorna config padrão — usada quando nunca foi configurado."""
        return cls(
            content=C.DEFAULT_MASTER_PROMPT,
            config_guild_id=C.DEFAULT_MASTER_CONFIG_GUILD_ID,
            updated_at=0.0,
            updated_by=0,
        )

    def to_doc(self) -> dict:
        return {
            "type": C.DOC_TYPE_MASTER,
            "_key": _SINGLETON_KEY,
            "content": self.content,
            "config_guild_id": int(self.config_guild_id),
            "updated_at": self.updated_at,
            "updated_by": int(self.updated_by),
        }

    @classmethod
    def from_doc(cls, d: dict) -> "MasterConfig":
        return cls(
            content=str(d.get("content") or ""),
            config_guild_id=int(d.get("config_guild_id") or C.DEFAULT_MASTER_CONFIG_GUILD_ID),
            updated_at=float(d.get("updated_at") or 0.0),
            updated_by=int(d.get("updated_by") or 0),
        )


class MasterStore:
    """Acesso ao doc singleton de master config. Com cache em RAM."""

    def __init__(self, chatbot_coll):
        self._coll = chatbot_coll
        # Cache de 1 entrada com TTL curto — evita ir ao Mongo a cada mensagem.
        # Invalidado explicitamente ao editar.
        self._cache: LRUCacheTTL[str, MasterConfig] = LRUCacheTTL(
            max_entries=2, ttl_seconds=30.0
        )

    async def get(self) -> MasterConfig:
        """Retorna a config atual. Se não existe no banco, retorna default.

        Não cria no banco automaticamente — só ao primeiro `update_content`
        ou `set_config_guild`.
        """
        cached = self._cache.get(_SINGLETON_KEY)
        if cached is not None:
            return cached

        doc = await self._coll.find_one({
            "type": C.DOC_TYPE_MASTER,
            "_key": _SINGLETON_KEY,
        })
        if doc is None:
            cfg = MasterConfig.default()
        else:
            cfg = MasterConfig.from_doc(doc)
        self._cache.set(_SINGLETON_KEY, cfg)
        return cfg

    async def update_content(self, *, content: str, editor_user_id: int) -> MasterConfig:
        """Atualiza só o texto do prompt. Preserva config_guild_id."""
        content = (content or "").strip()[:C.MAX_MASTER_PROMPT_LENGTH]
        now = time.time()
        # Precisa do config atual pra preservar config_guild_id se já tiver sido
        # setado. Se o doc não existe, pega do default.
        current = await self.get()

        await self._coll.update_one(
            {"type": C.DOC_TYPE_MASTER, "_key": _SINGLETON_KEY},
            {
                "$set": {
                    "content": content,
                    "updated_at": now,
                    "updated_by": int(editor_user_id),
                },
                "$setOnInsert": {
                    "type": C.DOC_TYPE_MASTER,
                    "_key": _SINGLETON_KEY,
                    "config_guild_id": int(current.config_guild_id),
                },
            },
            upsert=True,
        )
        self._cache.pop(_SINGLETON_KEY)  # invalida cache
        return MasterConfig(
            content=content,
            config_guild_id=current.config_guild_id,
            updated_at=now,
            updated_by=editor_user_id,
        )

    async def set_config_guild(
        self, *, new_guild_id: int, editor_user_id: int
    ) -> MasterConfig:
        """Muda qual server tem permissão pra editar o master prompt.

        O caller DEVE ter checado que o editor está no server atualmente
        configurado — esse método não faz check de permissão. Apenas persiste.
        """
        now = time.time()
        current = await self.get()

        await self._coll.update_one(
            {"type": C.DOC_TYPE_MASTER, "_key": _SINGLETON_KEY},
            {
                "$set": {
                    "config_guild_id": int(new_guild_id),
                    "updated_at": now,
                    "updated_by": int(editor_user_id),
                },
                "$setOnInsert": {
                    "type": C.DOC_TYPE_MASTER,
                    "_key": _SINGLETON_KEY,
                    "content": current.content,
                },
            },
            upsert=True,
        )
        self._cache.pop(_SINGLETON_KEY)
        return MasterConfig(
            content=current.content,
            config_guild_id=int(new_guild_id),
            updated_at=now,
            updated_by=editor_user_id,
        )
