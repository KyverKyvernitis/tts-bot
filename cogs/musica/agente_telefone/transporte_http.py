from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

import aiohttp

_SESSAO: aiohttp.ClientSession | None = None
_SESSAO_LOOP: asyncio.AbstractEventLoop | None = None
_SESSAO_LOCK: asyncio.Lock | None = None
_SESSAO_LOCK_LOOP: asyncio.AbstractEventLoop | None = None


def _lock_da_sessao() -> asyncio.Lock:
    global _SESSAO_LOCK, _SESSAO_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _SESSAO_LOCK is None or _SESSAO_LOCK_LOOP is not loop:
        _SESSAO_LOCK = asyncio.Lock()
        _SESSAO_LOCK_LOOP = loop
    return _SESSAO_LOCK


async def obter_sessao_http() -> aiohttp.ClientSession:
    """Retorna uma sessão HTTP reutilizável para o Phone Worker.

    O bot roda em um único loop, mas os testes podem criar loops diferentes.
    Quando o loop muda, a sessão antiga é fechada antes de criar outra.
    """
    global _SESSAO, _SESSAO_LOOP
    loop = asyncio.get_running_loop()
    session = _SESSAO
    if session is not None and not session.closed and _SESSAO_LOOP is loop:
        return session

    async with _lock_da_sessao():
        session = _SESSAO
        if session is not None and not session.closed and _SESSAO_LOOP is loop:
            return session
        if session is not None and not session.closed:
            await session.close()
        connector = aiohttp.TCPConnector(
            limit=8,
            ttl_dns_cache=300,
            keepalive_timeout=30.0,
        )
        _SESSAO = aiohttp.ClientSession(connector=connector)
        _SESSAO_LOOP = loop
        return _SESSAO


async def fechar_sessao_http() -> None:
    global _SESSAO, _SESSAO_LOOP
    session = _SESSAO
    _SESSAO = None
    _SESSAO_LOOP = None
    if session is not None and not session.closed:
        await session.close()


async def post_json_worker(
    *,
    url: str,
    token: str,
    payload: Mapping[str, Any],
    timeout_seconds: float,
    max_erro: int = 400,
) -> dict[str, Any]:
    """POST JSON com keep-alive e validação estrita da resposta do Worker."""
    session = await obter_sessao_http()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=max(0.1, float(timeout_seconds)))
    async with session.post(
        url,
        headers=headers,
        json=dict(payload),
        timeout=timeout,
    ) as response:
        text = await response.text()
        try:
            parsed = json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            if 200 <= response.status < 300:
                raise RuntimeError("Phone Worker retornou JSON inválido") from exc
            parsed = {}

        if response.status < 200 or response.status >= 300:
            detail = text[:max_erro]
            if isinstance(parsed, Mapping):
                detail = str(parsed.get("error") or parsed.get("message") or detail)[:max_erro]
            raise RuntimeError(f"HTTP {response.status}: {detail}")

        if not isinstance(parsed, dict):
            raise RuntimeError(
                f"Phone Worker retornou resposta JSON inesperada: {type(parsed).__name__}"
            )
        return parsed
