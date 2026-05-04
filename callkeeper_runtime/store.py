from __future__ import annotations

import logging
from typing import Optional

from db import SettingsDB

log = logging.getLogger(__name__)


class CallKeeperStateStore:
    """Acesso mínimo ao estado compartilhado do CallKeeper no Mongo.

    O bot principal escreve `enabled` e `channel_id` por comando de prefixo.
    O serviço standalone lê o mesmo estado e aplica a regra de voz. Assim o
    CallKeeper continua vivo mesmo se o processo principal cair por outra cog.
    """

    def __init__(self, db: SettingsDB, *, default_channel_id: int = 0):
        self.db = db
        self.default_channel_id = int(default_channel_id or 0)

    def is_enabled(self, guild_id: int) -> bool:
        try:
            return bool(self.db.get_callkeeper_enabled(int(guild_id)))
        except Exception:
            log.exception("[callkeeper] falha lendo enabled no DB")
            return False

    async def set_enabled(self, guild_id: int, value: bool) -> None:
        await self.db.set_callkeeper_enabled(int(guild_id), bool(value))

    def get_channel_id(self, guild_id: int) -> int:
        try:
            saved = int(self.db.get_callkeeper_channel_id(int(guild_id)) or 0)
        except Exception:
            log.exception("[callkeeper] falha lendo channel_id no DB")
            saved = 0
        return saved if saved > 0 else max(0, int(self.default_channel_id or 0))

    async def set_channel_id(self, guild_id: int, channel_id: Optional[int]) -> None:
        await self.db.set_callkeeper_channel_id(int(guild_id), int(channel_id or 0))
