from typing import Dict, Any
from motor.motor_asyncio import AsyncIOMotorClient

class SettingsDB:
    def __init__(self, uri: str, db_name: str, coll_name: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client[db_name]
        self.coll = self.db[coll_name]
        self.cache: Dict[int, Dict[str, Any]] = {}

    async def init(self):
        try:
            await self.coll.create_index("guild_id", unique=True)
        except Exception:
            pass
        await self.load_cache()

    async def load_cache(self):
        self.cache.clear()
        cursor = self.coll.find({}, {"_id": 0})
        async for doc in cursor:
            gid = int(doc["guild_id"])
            self.cache[gid] = {
                "anti_mzk_enabled": bool(doc.get("anti_mzk_enabled", True))
            }

    def anti_mzk_enabled(self, guild_id: int) -> bool:
        return bool(self.cache.get(guild_id, {}).get("anti_mzk_enabled", True))

    async def set_anti_mzk_enabled(self, guild_id: int, value: bool):
        self.cache[guild_id] = {"anti_mzk_enabled": bool(value)}
        await self.coll.update_one(
            {"guild_id": guild_id},
            {"$set": {"guild_id": guild_id, "anti_mzk_enabled": bool(value)}},
            upsert=True,
        )
