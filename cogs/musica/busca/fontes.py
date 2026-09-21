from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from cogs.musica import configuracao as config

from ..metadados.modelos import ApiTrackCandidate
from ..metadados.provedores_api import MusicApiProviders
from ..metadados.quota_youtube import busca_youtube_disponivel
from .chaves import chave_semantica_busca
from .intencao import analisar_consulta

logger = logging.getLogger(__name__)

_provedores_api: MusicApiProviders | None = None
_EM_VOO_YOUTUBE: dict[tuple[str, int], asyncio.Task[list[ApiTrackCandidate]]] = {}
_CONSUMIDORES_YOUTUBE: dict[tuple[str, int], int] = {}
_API_BREAKER_ABERTO_ATE = 0.0
_API_BREAKER_MOTIVO = ""
_API_BREAKER_TRANSITORIO_SECONDS = 45.0
_API_BREAKER_RATE_LIMIT_SECONDS = 600.0


@dataclass(slots=True, frozen=True)
class SnapshotCircuitoYouTube:
    aberto: bool
    restante_seconds: float
    motivo: str
    em_voo: int


def _provedores() -> MusicApiProviders:
    global _provedores_api
    if _provedores_api is None:
        _provedores_api = MusicApiProviders()
    return _provedores_api


def _agora() -> float:
    return time.monotonic()


def _breaker_aberto() -> bool:
    return _API_BREAKER_ABERTO_ATE > _agora()


def snapshot_circuito_youtube_fast() -> SnapshotCircuitoYouTube:
    restante = max(0.0, _API_BREAKER_ABERTO_ATE - _agora())
    return SnapshotCircuitoYouTube(
        aberto=restante > 0.0,
        restante_seconds=restante,
        motivo=_API_BREAKER_MOTIVO if restante > 0.0 else "",
        em_voo=sum(1 for task in _EM_VOO_YOUTUBE.values() if not task.done()),
    )


def resetar_circuito_youtube_fast() -> None:
    global _API_BREAKER_ABERTO_ATE, _API_BREAKER_MOTIVO
    _API_BREAKER_ABERTO_ATE = 0.0
    _API_BREAKER_MOTIVO = ""


def _status_http(exc: BaseException) -> int | None:
    for nome in ("code", "status", "status_code"):
        valor = getattr(exc, nome, None)
        try:
            if valor is not None:
                return int(valor)
        except (TypeError, ValueError):
            pass
    return None


def _abrir_breaker(exc: BaseException) -> None:
    global _API_BREAKER_ABERTO_ATE, _API_BREAKER_MOTIVO
    status = _status_http(exc)
    cooldown = (
        _API_BREAKER_RATE_LIMIT_SECONDS
        if status in {403, 429}
        else _API_BREAKER_TRANSITORIO_SECONDS
    )
    motivo = f"{type(exc).__name__}: {exc}"[:220]
    _API_BREAKER_ABERTO_ATE = _agora() + cooldown
    _API_BREAKER_MOTIVO = motivo
    log = logger.warning if status in {403, 429} else logger.debug
    log(
        "[music/search] youtube api-first em cooldown | status=%s cooldown=%.0fs erro=%s",
        status,
        cooldown,
        motivo,
    )


def youtube_api_fast_disponivel() -> bool:
    if _breaker_aberto():
        return False
    return busca_youtube_disponivel(
        limite_diario=int(getattr(config, "MUSIC_SEARCH_YOUTUBE_API_DAILY_SOFT_CALLS", 80) or 0),
        habilitado=bool(getattr(config, "MUSIC_SEARCH_YOUTUBE_API_QUOTA_GUARD_ENABLED", True)),
    )


def _limpar_task_youtube(
    chave: tuple[str, int],
    task: asyncio.Task[list[ApiTrackCandidate]],
) -> None:
    if _EM_VOO_YOUTUBE.get(chave) is task:
        _EM_VOO_YOUTUBE.pop(chave, None)
    if task.done() and _CONSUMIDORES_YOUTUBE.get(chave, 0) <= 0:
        _CONSUMIDORES_YOUTUBE.pop(chave, None)
    if not task.cancelled():
        try:
            task.exception()
        except Exception:
            pass


async def _executar_youtube_singleflight(
    chave: tuple[str, int],
    produtor,
) -> list[ApiTrackCandidate]:
    task = _EM_VOO_YOUTUBE.get(chave)
    if task is None or task.done():
        task = asyncio.create_task(produtor())
        _EM_VOO_YOUTUBE[chave] = task
        task.add_done_callback(lambda done, key=chave: _limpar_task_youtube(key, done))

    _CONSUMIDORES_YOUTUBE[chave] = _CONSUMIDORES_YOUTUBE.get(chave, 0) + 1
    try:
        # Copia rasa da lista basta aqui: os candidatos nao sao ranqueados nem
        # mutados no fluxo simplificado. Nao existe cache apos o task terminar.
        return list(await asyncio.shield(task))
    finally:
        restantes = max(0, _CONSUMIDORES_YOUTUBE.get(chave, 1) - 1)
        if restantes:
            _CONSUMIDORES_YOUTUBE[chave] = restantes
        else:
            _CONSUMIDORES_YOUTUBE.pop(chave, None)
            if not task.done():
                task.cancel()


async def buscar_candidatos_youtube_fast(query: str, *, limit: int = 3) -> list[ApiTrackCandidate]:
    """Executa uma tentativa YouTube API-first sem cache de resultados.

    Requests simultaneos semanticamente equivalentes compartilham apenas a
    operacao em voo. Uma falha abre um cooldown curto para evitar insistir em
    API lenta/quebrada; 403/429 recebem cooldown maior. Resultado concluido nao
    e persistido nem mantido em RAM.
    """
    if _breaker_aberto():
        snap = snapshot_circuito_youtube_fast()
        logger.debug(
            "[music/search] youtube api-first pulada pelo cooldown | restante_ms=%.0f motivo=%s",
            snap.restante_seconds * 1000.0,
            snap.motivo,
        )
        return []
    if not youtube_api_fast_disponivel():
        return []

    providers = _provedores()
    if not (providers.enabled and providers.youtube_api_key):
        return []
    consulta = analisar_consulta(query)
    texto = consulta.raw or str(query or "").strip()
    if not texto:
        return []
    timeout = max(
        0.10,
        float(getattr(config, "MUSIC_SEARCH_API_FIRST_TIMEOUT_SECONDS", 0.30) or 0.30),
    )
    limite = max(1, min(3, int(limit or 3)))
    chave = (chave_semantica_busca(texto), limite)

    async def _buscar() -> list[ApiTrackCandidate]:
        try:
            resultado = await providers.search_youtube_fast(
                texto,
                limit=limite,
                timeout_seconds=timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _abrir_breaker(exc)
            raise
        else:
            # Resposta vazia e uma resposta valida, nao uma falha de saude.
            resetar_circuito_youtube_fast()
            return list(resultado)

    return await _executar_youtube_singleflight(chave, _buscar)


async def fechar_provedores_busca() -> None:
    global _provedores_api
    for task in tuple(_EM_VOO_YOUTUBE.values()):
        if not task.done():
            task.cancel()
    _EM_VOO_YOUTUBE.clear()
    _CONSUMIDORES_YOUTUBE.clear()
    providers = _provedores_api
    _provedores_api = None
    if providers is not None:
        await providers.close()
