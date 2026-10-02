from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import inspect
import logging
import math
import os
from pathlib import Path
import time
from typing import Any


logger = logging.getLogger(__name__)
STORAGE_CACHE_SECONDS = 10.0
_ENGINE_ALIASES = {
    "android": "android_native", "android_tts": "android_native",
    "native_android": "android_native", "atts": "android_native",
    "edge_tts": "edge", "microsoft": "edge", "microsoft_edge": "edge",
    "google": "gtts", "gcloud": "gtts", "google_cloud": "gtts",
    "googlecloud": "gtts", "google_tts": "gtts", "google_translate": "gtts",
    "google_translate_tts": "gtts", "kasane_teto": "teto",
    "teto_utau": "teto", "utau": "teto",
    "piper_tts": "piper", "unknown": "other", "desconhecida": "other",
}
ENGINE_LABELS = {
    "android_native": "ATTS", "teto": "Kasane Teto", "edge": "Edge",
    "gtts": "gTTS", "piper": "Piper legado", "other": "Outras", "auto": "Auto",
}
ENGINE_ORDER = {key: index for index, key in enumerate(ENGINE_LABELS)}


def number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0.0, parsed) if math.isfinite(parsed) else None


def integer(value: Any) -> int | None:
    # IDs Discord são inteiros de 64 bits: convertê-los para float perde bits.
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            pass
    parsed = number(value)
    return int(parsed) if parsed is not None else None


def engine_key(value: Any) -> str:
    key = str(value or "other").strip().lower().replace("-", "_").replace(" ", "_")
    if key.startswith("tts_agent:"):
        key = key.split(":", 1)[1] or "auto"
    return _ENGINE_ALIASES.get(key, key)


def engine_label(value: Any) -> str:
    key = engine_key(value)
    return ENGINE_LABELS.get(key, str(value or "Outras")[:64])


def first_present(primary: dict[str, Any], primary_key: str, fallback: dict[str, Any], fallback_key: str) -> Any:
    """Não troca False ou zero atuais por uma amostra anterior."""
    if primary_key in primary and primary[primary_key] is not None:
        return primary[primary_key]
    return fallback.get(fallback_key)


@dataclass(frozen=True, slots=True)
class ServerRow:
    guild_id: int
    name: str
    icon_url: str | None
    members: int | None
    approximate_members: bool
    engines: tuple[tuple[str, int], ...]
    stats_available: bool

    @property
    def synths(self) -> int | None:
        return sum(amount for _, amount in self.engines) if self.stats_available else None


@dataclass(frozen=True, slots=True)
class EngineMetric:
    key: str
    route: str
    count: int | None
    failures: int | None
    consecutive_failures: int | None
    average_ms: float | None
    last_ms: float | None
    cache_hits: int | None
    cache_misses: int | None
    last_error: str


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    collected_at: float
    runtime_files: int | None
    cache_files: int | None
    credential_files: int | None
    total_bytes: int | None


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    collected_at: float
    servers: tuple[ServerRow, ...]
    servers_available: bool
    stats_available: bool
    tts_available: bool
    tts: dict[str, Any]
    engines: tuple[EngineMetric, ...]
    storage: StorageSnapshot
    thumbnail_url: str | None


def _persistent_engines(raw: Any) -> tuple[tuple[str, int], ...]:
    if not isinstance(raw, dict):
        return ()
    values = raw.get("engines") if isinstance(raw.get("engines"), dict) else raw
    combined: dict[str, int] = {}
    for raw_key, raw_value in values.items():
        if raw_key in {"total", "updated_at"}:
            continue
        key = engine_key(raw_key)
        if key not in ENGINE_LABELS or key == "auto":
            key = "other"
        amount = integer(raw_value)
        if amount:
            combined[key] = combined.get(key, 0) + amount
    return tuple(sorted(combined.items(), key=lambda item: (ENGINE_ORDER.get(item[0], 99), item[0])))


def collect_servers(bot: Any, stats: dict[int, Any], *, stats_available: bool) -> tuple[ServerRow, ...]:
    rows: list[ServerRow] = []
    for guild in list(getattr(bot, "guilds", []) or []):
        gid = integer(getattr(guild, "id", None))
        if not gid:
            continue
        members = integer(getattr(guild, "member_count", None))
        approximate = members is None
        if members is None:
            cached_members = getattr(guild, "members", None)
            members = len(cached_members) if cached_members is not None else None
        icon = getattr(guild, "icon", None)
        icon_url = getattr(icon, "url", None)
        row_stats = stats.get(gid, stats.get(str(gid), {}))
        row_available = stats_available and isinstance(row_stats, dict)
        if row_available:
            values = row_stats.get("engines") if "engines" in row_stats else row_stats
            row_available = isinstance(values, dict) and all(integer(value) is not None for key, value in values.items() if key not in {"total", "updated_at"})
        rows.append(ServerRow(
            guild_id=gid, name=str(getattr(guild, "name", "") or "Servidor sem nome"),
            icon_url=str(icon_url) if icon_url else None,
            members=members, approximate_members=approximate,
            engines=_persistent_engines(row_stats), stats_available=row_available,
        ))
    return tuple(sorted(rows, key=lambda row: (-(row.members or 0), row.name.casefold(), row.guild_id)))


def collect_engines(metrics: dict[str, Any]) -> tuple[EngineMetric, ...]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_route = metrics.get("engines_by_route")
    entries: list[tuple[str, Any, Any]] = []
    if isinstance(by_route, dict):
        for route in ("worker", "vps"):
            routed = by_route.get(route)
            if isinstance(routed, dict):
                entries.extend((route, key, value) for key, value in routed.items())
    else:
        raw_engines = metrics.get("engines")
        if isinstance(raw_engines, dict):
            for key, value in raw_engines.items():
                raw_name = str(key).lower().replace("-", "_")
                route = "worker" if raw_name.startswith("tts_agent:") or raw_name in {"tts_agent", "worker_tts_agent"} else "unknown"
                entries.append((route, key, value))
    for route, raw_key, raw_data in entries:
        if not isinstance(raw_data, dict):
            continue
        raw_name = str(raw_key).lower().replace("-", "_")
        key = "auto" if raw_name in {"tts_agent", "worker_tts_agent"} else engine_key(raw_key)
        groups.setdefault((route, key), []).append(raw_data)
    result: list[EngineMetric] = []
    for (route, key), values in groups.items():
        def summed(field: str) -> int | None:
            numbers = [integer(item.get(field)) for item in values]
            return sum(numbers) if all(item is not None for item in numbers) else None

        weighted = []
        for item in values:
            count = integer(item.get("synth_count"))
            total_ms = number(item.get("synth_total_ms"))
            average_ms = total_ms / count if total_ms is not None and count else number(item.get("avg_synth_ms"))
            weighted.append((average_ms, count))
        samples = [(average, count) for average, count in weighted if average is not None and count is not None and count > 0]
        complete_samples = all(count is not None and (count == 0 or avg is not None) for avg, count in weighted)
        average = sum(avg * count for avg, count in samples) / sum(count for _, count in samples) if samples and complete_samples else None
        consecutive = [integer(item.get("consecutive_failures")) for item in values]
        last = [number(item.get("last_synth_ms")) for item in values]
        result.append(EngineMetric(
            key=key, route=route, count=summed("synth_count"), failures=summed("synth_failures"),
            consecutive_failures=max((item for item in consecutive if item is not None), default=None),
            average_ms=average, last_ms=next((item for item in reversed(last) if item is not None), None),
            cache_hits=summed("cache_hits"), cache_misses=summed("cache_misses"),
            last_error=next((str(item.get("last_error")) for item in reversed(values) if item.get("last_error")), ""),
        ))
    return tuple(sorted(result, key=lambda item: (item.route != "worker", ENGINE_ORDER.get(item.key, 99), item.key)))


def scan_storage(root: Path) -> StorageSnapshot:
    """Somente esta função toca o disco; chamada com asyncio.to_thread."""
    counts: list[int | None] = []
    total = 0
    complete = True
    for name in ("runtime", "cache", "credentials"):
        count = 0
        try:
            with os.scandir(root / name) as entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False):
                        count += 1
                        total += entry.stat(follow_symlinks=False).st_size
        except FileNotFoundError:
            # Um diretório ainda não criado contém zero arquivos.
            pass
        except OSError:
            counts.append(None)
            complete = False
            continue
        counts.append(count)
    return StorageSnapshot(time.time(), *counts, total if complete else None)


class StatusCollector:
    def __init__(self, bot: Any, *, storage_root: Path | None = None):
        self.bot = bot
        self.storage_root = storage_root or Path.cwd() / "tmp_audio"
        self._storage_cache: StorageSnapshot | None = None
        self._storage_lock = asyncio.Lock()

    async def _storage(self) -> StorageSnapshot:
        async with self._storage_lock:
            cached = self._storage_cache
            if cached is not None and time.time() - cached.collected_at < STORAGE_CACHE_SECONDS:
                return cached
            self._storage_cache = await asyncio.to_thread(scan_storage, self.storage_root)
            return self._storage_cache

    async def collect(self) -> StatusSnapshot:
        stats: dict[int, Any] = {}
        stats_available = False
        getter = getattr(getattr(self.bot, "settings_db", None), "get_all_tts_synt_stats", None)
        if callable(getter):
            try:
                raw = getter()
                if inspect.isawaitable(raw):
                    raw = await raw
                if isinstance(raw, dict):
                    stats = raw
                    stats_available = True
            except Exception:
                logger.exception("[utility/status] estatísticas históricas indisponíveis")

        # Discord/cache são lidos e copiados no event loop, antes do await do disco.
        servers_available = hasattr(self.bot, "guilds")
        servers = collect_servers(self.bot, stats, stats_available=stats_available)
        metrics: dict[str, Any] = {}
        tts_available = False
        getter = getattr(self.bot, "get_health_snapshot", None)
        if callable(getter):
            try:
                health = getter()
                if inspect.isawaitable(health):
                    health = await health
                if isinstance(health, dict) and isinstance(health.get("tts_metrics"), dict) and not health.get("tts_metrics_error"):
                    metrics = copy.deepcopy(health["tts_metrics"])
                    tts_available = True
            except Exception:
                logger.exception("[utility/status] métricas TTS indisponíveis")
        engines = collect_engines(metrics)
        avatar = getattr(getattr(self.bot, "user", None), "display_avatar", None)
        avatar_url = getattr(avatar, "url", None)
        collected_at = time.time()
        storage = await self._storage()
        return StatusSnapshot(
            collected_at=collected_at, servers=servers, servers_available=servers_available,
            stats_available=stats_available, tts_available=tts_available,
            tts=metrics, engines=engines, storage=storage,
            thumbnail_url=str(avatar_url) if avatar_url else None,
        )
