from __future__ import annotations

import json
from typing import Any, Mapping

import aiohttp


async def executar_tarefa_resolucao(
    *,
    base: str,
    token: str,
    payload: Mapping[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Executa apenas o transporte HTTP da tarefa de resolução remota."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=max(1.0, float(timeout_seconds)) + 2.0)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{base}/task",
            headers=headers,
            json=dict(payload),
        ) as response:
            text = await response.text()
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"HTTP {response.status}: {text[:240]}")
            parsed = json.loads(text or "{}")
            return parsed if isinstance(parsed, dict) else {}
