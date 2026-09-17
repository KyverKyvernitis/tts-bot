from __future__ import annotations

import asyncio
import time
import weakref
from typing import Any

_CACHE: "weakref.WeakKeyDictionary[Any, tuple[float, bool]]" = weakref.WeakKeyDictionary()


async def musica_disponivel_para_ajuda(bot: Any) -> bool:
    """Informa ao Help Center se a categoria de música deve ser exibida."""
    if bot.get_cog("Music") is None:
        return False

    now = time.monotonic()
    cached = _CACHE.get(bot)
    if cached is not None and now - cached[0] <= 5.0:
        return bool(cached[1])

    def ler_registry() -> bool:
        try:
            from utility.commands.workers_registry import get_core_workers_registry

            snapshot = get_core_workers_registry().snapshot(lock_timeout_seconds=0.03)
            workers = snapshot.get("workers") if isinstance(snapshot, dict) else []
            for worker in workers or []:
                if not isinstance(worker, dict) or not bool(worker.get("online")):
                    continue
                runtime_kind = str(worker.get("runtime_kind") or "").strip().lower()
                source = str(worker.get("source") or "").strip().lower()
                if runtime_kind == "apk" or source.startswith("core-worker-apk"):
                    continue
                roles = {str(value or "").strip().lower() for value in (worker.get("roles") or [])}
                capabilities = {
                    str(value or "").strip().lower() for value in (worker.get("capabilities") or [])
                }
                roles_caps = roles | capabilities
                if "phone-worker" in roles_caps and "music" in roles_caps:
                    return True
            return False
        except Exception:
            return False

    available = bool(await asyncio.to_thread(ler_registry))
    _CACHE[bot] = (now, available)
    return available
