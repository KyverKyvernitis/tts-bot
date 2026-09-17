from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from typing import Any, Hashable

_TAREFAS: dict[Hashable, asyncio.Task[Any]] = {}


def agendar_tarefa_unica(chave: Hashable, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Mantém no máximo uma tarefa ativa por chave.

    Útil para watchers/prefetch por guild: o pedido mais recente substitui o
    anterior e evita polling duplicado e edições concorrentes do mesmo painel.
    """
    anterior = _TAREFAS.get(chave)
    if anterior is not None and not anterior.done():
        anterior.cancel()

    tarefa = asyncio.create_task(coro)
    _TAREFAS[chave] = tarefa

    def _finalizar(done: asyncio.Task[Any]) -> None:
        if _TAREFAS.get(chave) is done:
            _TAREFAS.pop(chave, None)
        if not done.cancelled():
            with contextlib.suppress(Exception):
                done.exception()

    tarefa.add_done_callback(_finalizar)
    return tarefa


async def cancelar_tarefas_interface() -> None:
    tarefas = list(_TAREFAS.values())
    _TAREFAS.clear()
    for tarefa in tarefas:
        if not tarefa.done():
            tarefa.cancel()
    if tarefas:
        await asyncio.gather(*tarefas, return_exceptions=True)


def quantidade_tarefas_interface() -> int:
    return sum(1 for tarefa in _TAREFAS.values() if not tarefa.done())
