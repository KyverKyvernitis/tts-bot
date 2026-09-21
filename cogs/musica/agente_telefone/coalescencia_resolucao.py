from __future__ import annotations

import asyncio
import copy
from typing import Any, Awaitable, Callable, Mapping

_Executor = Callable[..., Awaitable[dict[str, Any]]]
_EM_VOO: dict[tuple[str, str, str, int, bool, bool, bool], asyncio.Task[dict[str, Any]]] = {}
_CONSUMIDORES: dict[tuple[str, str, str, int, bool, bool, bool], int] = {}


def _chave(base: str, payload: Mapping[str, Any]) -> tuple[str, str, str, int, bool, bool, bool]:
    return (
        str(base or "").rstrip("/").lower(),
        str(payload.get("task") or ""),
        " ".join(str(payload.get("query") or "").lower().split()),
        int(payload.get("limit") or 1),
        bool(payload.get("metadata_only")),
        bool(payload.get("allow_playlist")),
        bool(payload.get("fast_search")),
    )


def resolucoes_em_voo() -> int:
    return sum(1 for task in _EM_VOO.values() if not task.done())


def limpar_coalescencia_resolucao() -> None:
    for task in tuple(_EM_VOO.values()):
        if not task.done():
            task.cancel()
    _EM_VOO.clear()
    _CONSUMIDORES.clear()


def _limpar(chave: tuple[str, str, str, int, bool, bool, bool], task: asyncio.Task[dict[str, Any]]) -> None:
    if _EM_VOO.get(chave) is task:
        _EM_VOO.pop(chave, None)
        _CONSUMIDORES.pop(chave, None)
    if not task.cancelled():
        try:
            task.exception()
        except Exception:
            pass


def _adicionar_consumidor(chave: tuple[str, str, str, int, bool, bool, bool]) -> None:
    _CONSUMIDORES[chave] = _CONSUMIDORES.get(chave, 0) + 1


def _remover_consumidor(
    chave: tuple[str, str, str, int, bool, bool, bool],
    task: asyncio.Task[dict[str, Any]],
) -> None:
    atual = max(0, _CONSUMIDORES.get(chave, 0) - 1)
    if atual > 0:
        _CONSUMIDORES[chave] = atual
        return
    _CONSUMIDORES.pop(chave, None)
    # Se nenhum request ainda precisa desta resolucao e ela nao terminou, nao
    # deixe yt-dlp/rede consumindo recursos no Phone Worker sem consumidor.
    if _EM_VOO.get(chave) is task and not task.done():
        task.cancel()


async def executar_resolucao_compartilhada(
    executor: _Executor,
    *,
    base: str,
    token: str,
    payload: Mapping[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Coalesce resolucoes textuais identicas antes do cache ficar pronto.

    O resultado compartilhado e apenas o JSON cru do worker; requester, memoria
    e ranking continuam sendo aplicados separadamente em cada request.
    """
    busca_textual = bool(payload.get("metadata_only")) and str(
        payload.get("default_search") or ""
    ).startswith("ytsearch")
    if not busca_textual:
        return await executor(
            base=base,
            token=token,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )

    chave = _chave(base, payload)
    task = _EM_VOO.get(chave)
    if task is None or task.done():
        async def _executar() -> dict[str, Any]:
            return await executor(
                base=base,
                token=token,
                payload=payload,
                timeout_seconds=timeout_seconds,
            )

        task = asyncio.create_task(_executar())
        _EM_VOO[chave] = task
        task.add_done_callback(lambda done, key=chave: _limpar(key, done))

    _adicionar_consumidor(chave)
    try:
        resultado = await asyncio.shield(task)
        return copy.deepcopy(resultado)
    finally:
        _remover_consumidor(chave, task)
