from __future__ import annotations

import asyncio
import copy
from typing import Any, Awaitable, Callable, Mapping

_Executor = Callable[..., Awaitable[dict[str, Any]]]
_EM_VOO: dict[tuple[str, str, str, int, bool, bool], asyncio.Task[dict[str, Any]]] = {}


def _chave(base: str, payload: Mapping[str, Any]) -> tuple[str, str, str, int, bool, bool]:
    return (
        str(base or "").rstrip("/").lower(),
        str(payload.get("task") or ""),
        " ".join(str(payload.get("query") or "").lower().split()),
        int(payload.get("limit") or 1),
        bool(payload.get("metadata_only")),
        bool(payload.get("allow_playlist")),
    )


def resolucoes_em_voo() -> int:
    return sum(1 for task in _EM_VOO.values() if not task.done())


def limpar_coalescencia_resolucao() -> None:
    for task in tuple(_EM_VOO.values()):
        if not task.done():
            task.cancel()
    _EM_VOO.clear()


def _limpar(chave: tuple[str, str, str, int, bool, bool], task: asyncio.Task[dict[str, Any]]) -> None:
    if _EM_VOO.get(chave) is task:
        _EM_VOO.pop(chave, None)
    if not task.cancelled():
        try:
            task.exception()
        except Exception:
            pass


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

    resultado = await asyncio.shield(task)
    return copy.deepcopy(resultado)
