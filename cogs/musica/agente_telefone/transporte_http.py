from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping

import aiohttp

logger = logging.getLogger(__name__)

_SESSAO: aiohttp.ClientSession | None = None
_SESSAO_LOOP: asyncio.AbstractEventLoop | None = None
_SESSAO_LOCK: asyncio.Lock | None = None
_SESSAO_LOCK_LOOP: asyncio.AbstractEventLoop | None = None

# Pequena janela de recuperação para mudanças de rota/Tailscale. A primeira
# tentativa é normal; em falha de transporte, o pool/DNS é descartado e a
# chamada é refeita numa conexão nova. Os atrasos seguintes cobrem o curto
# período em que o túnel volta após Wi-Fi/dados móveis reconectarem.
_RECOVERY_DELAYS = (0.0, 0.20, 0.70, 1.80, 4.00)


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
            # O endpoint do Phone Worker pode atravessar Tailscale/MagicDNS.
            # Cinco minutos de DNS/keep-alive velho fazia uma troca de rede no
            # telefone sobreviver muito além da reconexão real do túnel.
            ttl_dns_cache=15,
            keepalive_timeout=15.0,
        )
        _SESSAO = aiohttp.ClientSession(connector=connector)
        _SESSAO_LOOP = loop
        return _SESSAO


async def invalidar_sessao_http(session: aiohttp.ClientSession | None = None) -> None:
    """Descarta pool/DNS atual após quebra de rota ou transporte.

    ``session`` evita que uma requisição atrasada feche uma sessão nova que já
    foi criada por outra coroutine enquanto ela tratava a mesma queda de rede.
    """
    global _SESSAO, _SESSAO_LOOP
    async with _lock_da_sessao():
        atual = _SESSAO
        if session is not None and atual is not session:
            return
        _SESSAO = None
        _SESSAO_LOOP = None
        if atual is not None and not atual.closed:
            await atual.close()


async def fechar_sessao_http() -> None:
    await invalidar_sessao_http()


def _erro_transporte_recuperavel(exc: BaseException) -> bool:
    """Retorna True só para falhas de socket/conexão, nunca HTTP/JSON."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (aiohttp.ClientConnectionError, ConnectionError, OSError)):
            return True
        current = current.__cause__ or current.__context__
    return False


async def _post_json_once(
    *,
    session: aiohttp.ClientSession,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout_seconds: float,
    max_erro: int,
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=max(0.1, float(timeout_seconds)))
    async with session.post(
        url,
        headers=dict(headers),
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


async def post_json_worker(
    *,
    url: str,
    token: str,
    payload: Mapping[str, Any],
    timeout_seconds: float,
    max_erro: int = 400,
) -> dict[str, Any]:
    """POST JSON com keep-alive, recuperação de rota e resposta estrita.

    Em queda/reconexão de Wi-Fi/dados/Tailscale, conexões keep-alive antigas
    podem permanecer no pool como transports em fechamento. Nessa situação o
    pool é descartado (incluindo cache DNS) e a mesma operação é tentada numa
    conexão nova. Operações mutáveis carregam ``command_id`` e são deduplicadas
    pelo Music Agent, portanto um retry de transporte não duplica play/enqueue.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.1, float(timeout_seconds))
    last_exc: BaseException | None = None

    for attempt, delay in enumerate(_RECOVERY_DELAYS, start=1):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        if delay > 0:
            await asyncio.sleep(min(delay, max(0.0, remaining)))
            remaining = deadline - loop.time()
            if remaining <= 0:
                break

        session = await obter_sessao_http()
        try:
            return await _post_json_once(
                session=session,
                url=url,
                headers=headers,
                payload=payload,
                timeout_seconds=remaining,
                max_erro=max_erro,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not _erro_transporte_recuperavel(exc):
                raise
            last_exc = exc
            await invalidar_sessao_http(session)
            logger.info(
                "[music/worker-http] transporte reiniciado após falha de rede | tentativa=%s/%s erro=%s",
                attempt,
                len(_RECOVERY_DELAYS),
                f"{type(exc).__name__}: {exc}",
            )

    if last_exc is not None:
        raise last_exc
    raise asyncio.TimeoutError("tempo esgotado ao reconectar ao Phone Worker")
