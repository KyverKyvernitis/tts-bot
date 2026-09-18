from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from cogs.musica import configuracao as config

from .modelos import MusicWorkerSelection
from .utilitarios import _phone_worker_base_url


@dataclass(frozen=True, slots=True)
class DestinoWorker:
    worker_id: str
    name: str
    base: str
    token: str


_DESTINOS_GUILD: dict[int, DestinoWorker] = {}


def _normalizar_base(value: object) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return text


def destino_da_selecao(selection: MusicWorkerSelection) -> DestinoWorker | None:
    worker = selection.worker if isinstance(selection.worker, Mapping) else {}
    base = _normalizar_base(worker.get("endpoint")) or _normalizar_base(_phone_worker_base_url())
    token = str(getattr(config, "PHONE_WORKER_TOKEN", "") or "").strip()
    if not base or not token:
        return None
    return DestinoWorker(
        worker_id=str(selection.worker_id or worker.get("worker_id") or "").strip(),
        name=str(selection.name or worker.get("name") or selection.worker_id or "Phone Worker").strip(),
        base=base,
        token=token,
    )


def destino_vinculado(guild_id: int) -> DestinoWorker | None:
    try:
        guild = int(guild_id or 0)
    except Exception:
        return None
    return _DESTINOS_GUILD.get(guild) if guild > 0 else None


def resolver_destino_worker(
    selection: MusicWorkerSelection,
    *,
    guild_id: int = 0,
    preferir_vinculo: bool = True,
) -> DestinoWorker | None:
    if preferir_vinculo:
        bound = destino_vinculado(guild_id)
        if bound is not None:
            return bound
    return destino_da_selecao(selection)


def vincular_guild_worker(guild_id: int, destino: DestinoWorker) -> None:
    try:
        guild = int(guild_id or 0)
    except Exception:
        return
    if guild > 0:
        _DESTINOS_GUILD[guild] = destino


def desvincular_guild_worker(guild_id: int) -> None:
    try:
        guild = int(guild_id or 0)
    except Exception:
        return
    if guild > 0:
        _DESTINOS_GUILD.pop(guild, None)


def limpar_vinculos_worker() -> None:
    _DESTINOS_GUILD.clear()


def escopo_cache_worker(selection: MusicWorkerSelection) -> str:
    destino = destino_da_selecao(selection)
    if destino is None:
        return str(selection.worker_id or selection.name or "").strip().lower()
    return (destino.worker_id or destino.base).strip().lower()


__all__ = [
    "DestinoWorker",
    "destino_da_selecao",
    "destino_vinculado",
    "resolver_destino_worker",
    "vincular_guild_worker",
    "desvincular_guild_worker",
    "limpar_vinculos_worker",
    "escopo_cache_worker",
]
