from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import time

from motor.motor_asyncio import AsyncIOMotorClient

import config


class SettingsDB:
    def __init__(self, uri: str, db_name: str, coll_name: str):
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client[db_name]
        self.coll = self.db[coll_name]
        self.guild_cache: Dict[int, Dict[str, Any]] = {}
        self.user_cache: Dict[tuple[int, int], Dict[str, Any]] = {}
        self._resolved_tts_cache: Dict[tuple[int, int], Dict[str, str]] = {}

    async def init(self):
        await self._ensure_indexes()
        await self.load_cache()

    async def _ensure_indexes(self):
        try:
            indexes = await self.coll.index_information()

            old_index = indexes.get("guild_id_1")
            if old_index and old_index.get("unique"):
                key = old_index.get("key", [])
                if key == [("guild_id", 1)]:
                    try:
                        await self.coll.drop_index("guild_id_1")
                        print("[db] Índice antigo guild_id_1 removido.")
                    except Exception as e:
                        print(f"[db] Falha ao remover índice guild_id_1: {e}")

            idx = indexes.get("guild_id_1_type_1")
            if idx:
                try:
                    await self.coll.drop_index("guild_id_1_type_1")
                    print("[db] Índice antigo guild_id_1_type_1 removido.")
                except Exception as e:
                    print(f"[db] Falha ao remover índice guild_id_1_type_1: {e}")

            await self.coll.create_index("type")
            await self.coll.create_index(
                [("guild_id", 1), ("type", 1)],
                unique=True,
                name="guild_id_1_type_1",
                partialFilterExpression={"type": "guild"},
            )
            await self.coll.create_index([("guild_id", 1), ("user_id", 1), ("type", 1)], unique=True, name="guild_id_1_user_id_1_type_1")
        except Exception as e:
            print(f"[db] Erro ao garantir índices: {e}")

    async def load_cache(self):
        self.guild_cache.clear()
        self.user_cache.clear()
        if not hasattr(self, "_resolved_tts_cache"):
            self._resolved_tts_cache = {}
        self._resolved_tts_cache.clear()
        cursor = self.coll.find({}, {"_id": 0})
        async for doc in cursor:
            doc_type = doc.get("type")
            gid = int(doc.get("guild_id", 0) or 0)

            if doc_type == "guild" and gid:
                self.guild_cache[gid] = doc
            elif doc_type == "user" and gid and doc.get("user_id") is not None:
                uid = int(doc["user_id"])
                self.user_cache[(gid, uid)] = doc

    def _get_guild_doc(self, guild_id: int) -> Dict[str, Any]:
        return self.guild_cache.get(guild_id, {"type": "guild", "guild_id": guild_id})

    async def _save_guild_doc(self, guild_id: int, doc: Dict[str, Any]):
        doc["type"] = "guild"
        doc["guild_id"] = guild_id
        self.guild_cache[guild_id] = doc
        self._invalidate_resolved_tts_cache(guild_id=guild_id)
        await self.coll.update_one({"type": "guild", "guild_id": guild_id}, {"$set": doc}, upsert=True)

    def _invalidate_resolved_tts_cache(self, *, guild_id: int | None = None, user_id: int | None = None):
        if guild_id is None and user_id is None:
            self._resolved_tts_cache.clear()
            return

        if guild_id is not None and user_id is not None:
            self._resolved_tts_cache.pop((guild_id, user_id), None)
            return

        if guild_id is not None:
            stale = [key for key in self._resolved_tts_cache if key[0] == guild_id]
            for key in stale:
                self._resolved_tts_cache.pop(key, None)

    def anti_mzk_enabled(self, guild_id: int) -> bool:
        g = self.guild_cache.get(guild_id, {})
        return bool(g.get("anti_mzk_enabled", True))

    async def set_anti_mzk_enabled(self, guild_id: int, value: bool):
        doc = self._get_guild_doc(guild_id)
        doc["anti_mzk_enabled"] = bool(value)
        await self._save_guild_doc(guild_id, doc)

    def get_anti_mzk_role_ids(self, guild_id: int) -> list[int]:
        g = self.guild_cache.get(guild_id, {})
        raw = g.get("anti_mzk_role_ids", []) or []
        result: list[int] = []
        for value in raw:
            try:
                result.append(int(value))
            except (TypeError, ValueError):
                pass
        return result

    async def add_anti_mzk_role_id(self, guild_id: int, role_id: int) -> bool:
        doc = self._get_guild_doc(guild_id)
        role_ids = self.get_anti_mzk_role_ids(guild_id)

        role_id = int(role_id)
        if role_id in role_ids:
            return False

        role_ids.append(role_id)
        doc["anti_mzk_role_ids"] = role_ids
        await self._save_guild_doc(guild_id, doc)
        return True

    async def remove_anti_mzk_role_id(self, guild_id: int, role_id: int) -> bool:
        doc = self._get_guild_doc(guild_id)
        role_ids = self.get_anti_mzk_role_ids(guild_id)

        role_id = int(role_id)
        if role_id not in role_ids:
            return False

        doc["anti_mzk_role_ids"] = [rid for rid in role_ids if rid != role_id]
        await self._save_guild_doc(guild_id, doc)
        return True

    def get_anti_mzk_staff_role_id(self, guild_id: int) -> int:
        g = self.guild_cache.get(guild_id, {})
        try:
            return max(0, int(g.get("anti_mzk_staff_role_id", 0) or 0))
        except Exception:
            return 0

    async def set_anti_mzk_staff_role_id(self, guild_id: int, role_id: int | None):
        doc = self._get_guild_doc(guild_id)
        try:
            doc["anti_mzk_staff_role_id"] = max(0, int(role_id or 0))
        except Exception:
            doc["anti_mzk_staff_role_id"] = 0
        await self._save_guild_doc(guild_id, doc)


    def _get_modo_censura_focus_map(self, guild_id: int) -> Dict[int, int]:
        g = self.guild_cache.get(guild_id, {})
        raw = g.get("modo_censura_focus_users", {}) or {}
        cleaned: Dict[int, int] = {}
        changed = False

        for key, value in raw.items():
            try:
                uid = int(key)
            except (TypeError, ValueError):
                changed = True
                continue

            try:
                stored_value = int(value)
            except (TypeError, ValueError):
                stored_value = 1
                changed = True

            cleaned[uid] = max(1, stored_value)

        if changed:
            doc = self._get_guild_doc(guild_id)
            doc["modo_censura_focus_users"] = {str(uid): stored for uid, stored in cleaned.items()}
            self.guild_cache[guild_id] = doc

        return cleaned

    def get_modo_censura_focus_map(self, guild_id: int) -> Dict[int, int]:
        return dict(self._get_modo_censura_focus_map(guild_id))

    async def toggle_modo_censura_focus_users(self, guild_id: int, user_ids: list[int]) -> tuple[list[int], list[int], Dict[int, int]]:
        current = self._get_modo_censura_focus_map(guild_id)
        doc = self._get_guild_doc(guild_id)
        added: list[int] = []
        removed: list[int] = []

        for raw_uid in user_ids:
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue

            if uid in current:
                current.pop(uid, None)
                removed.append(uid)
            else:
                current[uid] = 1
                added.append(uid)

        doc["modo_censura_focus_users"] = {str(uid): stored for uid, stored in current.items()}
        await self._save_guild_doc(guild_id, doc)
        return added, removed, dict(current)

    async def clear_modo_censura_focus_users(self, guild_id: int) -> Dict[int, int]:
        doc = self._get_guild_doc(guild_id)
        doc["modo_censura_focus_users"] = {}
        await self._save_guild_doc(guild_id, doc)
        return {}

    def get_guild_tts_defaults(self, guild_id: int) -> Dict[str, Any]:
        g = self.guild_cache.get(guild_id, {})
        tts = g.get("tts_defaults", {}) or {}
        return {
            "engine": str(tts.get("engine", "") or ""),
            "voice": str(tts.get("voice", "") or ""),
            "language": str(tts.get("language", "") or ""),
            "rate": str(tts.get("rate", "") or ""),
            "pitch": str(tts.get("pitch", "") or ""),
            "gcloud_voice": str(tts.get("gcloud_voice", "") or ""),
            "gcloud_language": str(tts.get("gcloud_language", "") or ""),
            "gcloud_rate": str(tts.get("gcloud_rate", "") or ""),
            "gcloud_pitch": str(tts.get("gcloud_pitch", "") or ""),
            "bot_prefix": str(g.get("bot_prefix", "_") or "_"),
            "tts_prefix": str(g.get("tts_prefix", ",") or ","),
            "gtts_prefix": str(g.get("gtts_prefix", g.get("tts_prefix", ".")) or "."),
            "edge_prefix": str(g.get("edge_prefix", ",") or ","),
            "gcloud_prefix": str(g.get("gcloud_prefix", getattr(config, "GOOGLE_CLOUD_TTS_PREFIX", "'")) or getattr(config, "GOOGLE_CLOUD_TTS_PREFIX", "'")),
            "speech_limit_seconds": int(g.get("speech_limit_seconds", 30) or 30),
            "announce_author": bool(g.get("announce_author_enabled", False)),
            "auto_leave": bool(g.get("auto_leave_enabled", True)),
            "ignored_tts_role_id": int(g.get("ignored_tts_role_id", 0) or 0),
        }

    async def set_guild_tts_defaults(
        self,
        guild_id: int,
        *,
        engine: Optional[str] = None,
        voice: Optional[str] = None,
        language: Optional[str] = None,
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
        gcloud_voice: Optional[str] = None,
        gcloud_language: Optional[str] = None,
        gcloud_rate: Optional[str] = None,
        gcloud_pitch: Optional[str] = None,
        bot_prefix: Optional[str] = None,
        tts_prefix: Optional[str] = None,
        gtts_prefix: Optional[str] = None,
        edge_prefix: Optional[str] = None,
        gcloud_prefix: Optional[str] = None,
        speech_limit_seconds: Optional[int] = None,
        announce_author: Optional[bool] = None,
        auto_leave: Optional[bool] = None,
        ignored_tts_role_id: Optional[int] = None,
    ):
        doc = self._get_guild_doc(guild_id)
        tts = doc.get("tts_defaults", {}) or {}

        if engine is not None:
            tts["engine"] = engine
        if voice is not None:
            tts["voice"] = voice
        if language is not None:
            tts["language"] = language
        if rate is not None:
            tts["rate"] = rate
        if pitch is not None:
            tts["pitch"] = pitch
        if gcloud_voice is not None:
            tts["gcloud_voice"] = gcloud_voice
        if gcloud_language is not None:
            tts["gcloud_language"] = gcloud_language
        if gcloud_rate is not None:
            tts["gcloud_rate"] = gcloud_rate
        if gcloud_pitch is not None:
            tts["gcloud_pitch"] = gcloud_pitch
        if bot_prefix is not None:
            doc["bot_prefix"] = str(bot_prefix or "_")[:8]
        if tts_prefix is not None:
            doc["tts_prefix"] = str(tts_prefix or ",")[:8]
        if gtts_prefix is not None:
            doc["gtts_prefix"] = str(gtts_prefix or ".")[:8]
        if edge_prefix is not None:
            doc["edge_prefix"] = str(edge_prefix or ",")[:8]
        if gcloud_prefix is not None:
            doc["gcloud_prefix"] = str(gcloud_prefix or getattr(config, "GOOGLE_CLOUD_TTS_PREFIX", "'"))[:8]
        if speech_limit_seconds is not None:
            try:
                doc["speech_limit_seconds"] = max(1, min(600, int(speech_limit_seconds)))
            except Exception:
                doc["speech_limit_seconds"] = 30
        if announce_author is not None:
            doc["announce_author_enabled"] = bool(announce_author)
        if auto_leave is not None:
            doc["auto_leave_enabled"] = bool(auto_leave)
        if ignored_tts_role_id is not None:
            try:
                doc["ignored_tts_role_id"] = max(0, int(ignored_tts_role_id or 0))
            except Exception:
                doc["ignored_tts_role_id"] = 0

        doc["tts_defaults"] = tts
        self._invalidate_resolved_tts_cache(guild_id=guild_id)
        await self._save_guild_doc(guild_id, doc)

    def get_ignored_tts_role_id(self, guild_id: int) -> int:
        g = self.guild_cache.get(guild_id, {})
        try:
            return max(0, int(g.get("ignored_tts_role_id", 0) or 0))
        except Exception:
            return 0

    async def set_ignored_tts_role_id(self, guild_id: int, role_id: int | None):
        doc = self._get_guild_doc(guild_id)
        try:
            doc["ignored_tts_role_id"] = max(0, int(role_id or 0))
        except Exception:
            doc["ignored_tts_role_id"] = 0
        await self._save_guild_doc(guild_id, doc)

    def get_user_tts(self, guild_id: int, user_id: int) -> Dict[str, str]:
        u = self.user_cache.get((guild_id, user_id), {})
        tts = u.get("tts", {}) or {}
        return {
            "engine": str(tts.get("engine", "") or ""),
            "voice": str(tts.get("voice", "") or ""),
            "language": str(tts.get("language", "") or ""),
            "rate": str(tts.get("rate", "") or ""),
            "pitch": str(tts.get("pitch", "") or ""),
            "gcloud_voice": str(tts.get("gcloud_voice", "") or ""),
            "gcloud_language": str(tts.get("gcloud_language", "") or ""),
            "gcloud_rate": str(tts.get("gcloud_rate", "") or ""),
            "gcloud_pitch": str(tts.get("gcloud_pitch", "") or ""),
            "speaker_name": str(tts.get("speaker_name", "") or ""),
        }

    async def set_user_tts(
        self,
        guild_id: int,
        user_id: int,
        *,
        engine: Optional[str] = None,
        voice: Optional[str] = None,
        language: Optional[str] = None,
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
        gcloud_voice: Optional[str] = None,
        gcloud_language: Optional[str] = None,
        gcloud_rate: Optional[str] = None,
        gcloud_pitch: Optional[str] = None,
        speaker_name: Optional[str] = None,
    ):
        key = (guild_id, user_id)
        doc = self.user_cache.get(key, {"type": "user", "guild_id": guild_id, "user_id": user_id})
        tts = doc.get("tts", {}) or {}

        if engine is not None:
            tts["engine"] = engine
        if voice is not None:
            tts["voice"] = voice
        if language is not None:
            tts["language"] = language
        if rate is not None:
            tts["rate"] = rate
        if pitch is not None:
            tts["pitch"] = pitch
        if gcloud_voice is not None:
            tts["gcloud_voice"] = gcloud_voice
        if gcloud_language is not None:
            tts["gcloud_language"] = gcloud_language
        if gcloud_rate is not None:
            tts["gcloud_rate"] = gcloud_rate
        if gcloud_pitch is not None:
            tts["gcloud_pitch"] = gcloud_pitch
        if speaker_name is not None:
            cleaned_speaker_name = str(speaker_name or "").strip()
            if cleaned_speaker_name:
                tts["speaker_name"] = cleaned_speaker_name
            else:
                tts.pop("speaker_name", None)

        doc["type"] = "user"
        doc["guild_id"] = guild_id
        doc["user_id"] = user_id
        doc["tts"] = tts
        self.user_cache[key] = doc
        self._invalidate_resolved_tts_cache(guild_id=guild_id, user_id=user_id)
        await self.coll.update_one(
            {"type": "user", "guild_id": guild_id, "user_id": user_id},
            {"$set": doc},
            upsert=True,
        )

    async def reset_user_tts(self, guild_id: int, user_id: int) -> bool:
        key = (guild_id, user_id)
        doc = dict(self.user_cache.get(key, {"type": "user", "guild_id": guild_id, "user_id": user_id}))
        had_tts = bool((doc.get("tts", {}) or {}))
        doc.pop("tts", None)

        def _has_meaningful_value(value: Any) -> bool:
            if value in (None, "", [], {}):
                return False
            if isinstance(value, dict):
                return any(_has_meaningful_value(v) for v in value.values())
            if isinstance(value, (list, tuple, set)):
                return any(_has_meaningful_value(v) for v in value)
            return True

        keep_doc = False
        for field, value in doc.items():
            if field in {"type", "guild_id", "user_id"}:
                continue
            if _has_meaningful_value(value):
                keep_doc = True
                break

        if keep_doc:
            doc["type"] = "user"
            doc["guild_id"] = guild_id
            doc["user_id"] = user_id
            self.user_cache[key] = doc
            self._invalidate_resolved_tts_cache(guild_id=guild_id, user_id=user_id)
            await self.coll.update_one(
                {"type": "user", "guild_id": guild_id, "user_id": user_id},
                {"$unset": {"tts": ""}, "$set": doc},
                upsert=True,
            )
        else:
            self.user_cache.pop(key, None)
            self._invalidate_resolved_tts_cache(guild_id=guild_id, user_id=user_id)
            await self.coll.delete_one({"type": "user", "guild_id": guild_id, "user_id": user_id})

        return had_tts

    def resolve_tts(self, guild_id: int, user_id: int) -> Dict[str, str]:
        cache_key = (guild_id, user_id)
        cached = self._resolved_tts_cache.get(cache_key)
        if cached is not None:
            return dict(cached)

        user = self.get_user_tts(guild_id, user_id)
        guild = self.get_guild_tts_defaults(guild_id)

        def pick(key: str, fallback: str) -> str:
            return (user.get(key) or "").strip() or (guild.get(key) or "").strip() or fallback

        engine = pick("engine", "gtts").lower()
        if engine not in ("edge", "gtts", "gcloud"):
            engine = "gtts"

        resolved = {
            "engine": engine,
            "voice": pick("voice", "pt-BR-FranciscaNeural"),
            "language": pick("language", "pt-br"),
            "rate": pick("rate", "+0%"),
            "pitch": pick("pitch", "+0Hz"),
            "gcloud_voice": pick("gcloud_voice", "pt-BR-Standard-A"),
            "gcloud_language": pick("gcloud_language", "pt-BR"),
            "gcloud_rate": pick("gcloud_rate", "1.0"),
            "gcloud_pitch": pick("gcloud_pitch", "0.0"),
            "speaker_name": str(user.get("speaker_name", "") or ""),
            "bot_prefix": str(guild.get("bot_prefix", "_") or "_"),
            "tts_prefix": str(guild.get("tts_prefix", ",") or ","),
            "gtts_prefix": str(guild.get("gtts_prefix", guild.get("tts_prefix", ".")) or "."),
            "edge_prefix": str(guild.get("edge_prefix", ",") or ","),
            "gcloud_prefix": str(guild.get("gcloud_prefix", "'") or "'"),
            "speech_limit_seconds": int(guild.get("speech_limit_seconds", 30) or 30),
        }
        self._resolved_tts_cache[cache_key] = dict(resolved)
        return resolved

    def get_panel_history(self, guild_id: int, user_id: int) -> Dict[str, Any]:
        guild_doc = self.guild_cache.get(guild_id, {})
        guild_panel = guild_doc.get("panel_history", {}) or {}
        user_doc = self.user_cache.get((guild_id, user_id), {})
        user_panel = user_doc.get("panel_history", {}) or {}

        user_last_changes = [str(x) for x in (user_panel.get("last_changes", []) or []) if str(x or "")]
        server_last_changes = [str(x) for x in (guild_panel.get("server_last_changes", []) or []) if str(x or "")]
        toggle_last_changes = [str(x) for x in (guild_panel.get("toggle_last_changes", []) or []) if str(x or "")]

        if not user_last_changes and user_panel.get("last_change"):
            user_last_changes = [str(user_panel.get("last_change") or "")]
        if not server_last_changes and guild_panel.get("server_last_change"):
            server_last_changes = [str(guild_panel.get("server_last_change") or "")]
        if not toggle_last_changes and guild_panel.get("toggle_last_change"):
            toggle_last_changes = [str(guild_panel.get("toggle_last_change") or "")]

        return {
            "user_last_change": user_last_changes[-1] if user_last_changes else "",
            "server_last_change": server_last_changes[-1] if server_last_changes else "",
            "toggle_last_change": toggle_last_changes[-1] if toggle_last_changes else "",
            "user_last_changes": user_last_changes,
            "server_last_changes": server_last_changes,
            "toggle_last_changes": toggle_last_changes,
        }

    async def set_user_panel_last_change(self, guild_id: int, user_id: int, text: str):
        key = (guild_id, user_id)
        doc = self.user_cache.get(key, {"type": "user", "guild_id": guild_id, "user_id": user_id})
        panel = doc.get("panel_history", {}) or {}
        text = str(text or "")
        last_changes = [str(x) for x in (panel.get("last_changes", []) or []) if str(x or "")]
        if text:
            last_changes.append(text)
        panel["last_change"] = text
        panel["last_changes"] = last_changes[-3:]
        doc["type"] = "user"
        doc["guild_id"] = guild_id
        doc["user_id"] = user_id
        doc["panel_history"] = panel
        self.user_cache[key] = doc
        await self.coll.update_one(
            {"type": "user", "guild_id": guild_id, "user_id": user_id},
            {"$set": doc},
            upsert=True,
        )

    async def set_guild_panel_last_change(self, guild_id: int, *, server_last_change: str | None = None, toggle_last_change: str | None = None):
        doc = self._get_guild_doc(guild_id)
        panel = doc.get("panel_history", {}) or {}

        if server_last_change is not None:
            text = str(server_last_change or "")
            server_last_changes = [str(x) for x in (panel.get("server_last_changes", []) or []) if str(x or "")]
            if text:
                server_last_changes.append(text)
            panel["server_last_change"] = text
            panel["server_last_changes"] = server_last_changes[-3:]
        if toggle_last_change is not None:
            text = str(toggle_last_change or "")
            toggle_last_changes = [str(x) for x in (panel.get("toggle_last_changes", []) or []) if str(x or "")]
            if text:
                toggle_last_changes.append(text)
            panel["toggle_last_change"] = text
            panel["toggle_last_changes"] = toggle_last_changes[-3:]

        doc["panel_history"] = panel
        await self._save_guild_doc(guild_id, doc)

    def get_role_cooldown(self, guild_id: int) -> Dict[str, Any]:
        g = self.guild_cache.get(guild_id, {})
        data = g.get("role_cooldown", {}) or {}
        return {
            "active": bool(data.get("active", False)),
            "ends_at": str(data.get("ends_at", "") or ""),
            "role_id": int(data.get("role_id", 0) or 0),
            "role_was_mentionable": data.get("role_was_mentionable", None),
        }

    async def set_role_cooldown(
        self,
        guild_id: int,
        *,
        active: bool,
        ends_at: Optional[str] = None,
        role_id: Optional[int] = None,
        role_was_mentionable: Optional[bool] = None,
    ):
        doc = self._get_guild_doc(guild_id)
        cooldown = doc.get("role_cooldown", {}) or {}
        cooldown["active"] = bool(active)
        if ends_at is not None:
            cooldown["ends_at"] = ends_at
        if role_id is not None:
            cooldown["role_id"] = int(role_id)
        if role_was_mentionable is not None:
            cooldown["role_was_mentionable"] = bool(role_was_mentionable)
        doc["role_cooldown"] = cooldown
        await self._save_guild_doc(guild_id, doc)

    async def clear_role_cooldown(self, guild_id: int):
        doc = self._get_guild_doc(guild_id)
        doc["role_cooldown"] = {"active": False, "ends_at": "", "role_id": 0, "role_was_mentionable": None}
        await self._save_guild_doc(guild_id, doc)

    @staticmethod
    def utcnow_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
