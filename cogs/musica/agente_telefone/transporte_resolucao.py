from __future__ import annotations

from typing import Any, Mapping

from .transporte_http import post_json_worker


async def executar_tarefa_resolucao(
    *,
    base: str,
    token: str,
    payload: Mapping[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Executa apenas o transporte HTTP da tarefa de resolução remota."""
    return await post_json_worker(
        url=f"{base}/task",
        token=token,
        payload=payload,
        timeout_seconds=max(1.0, float(timeout_seconds)) + 2.0,
        max_erro=240,
    )
