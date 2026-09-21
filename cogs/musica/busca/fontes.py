from __future__ import annotations

from cogs.musica import configuracao as config

from ..metadados.modelos import ApiTrackCandidate
from ..metadados.provedores_api import MusicApiProviders
from ..metadados.quota_youtube import busca_youtube_disponivel
from ..metadados.resiliencia import buscar_metadata_compartilhada
from .chaves import chave_semantica_busca
from .intencao import analisar_consulta

_provedores_api: MusicApiProviders | None = None


def _provedores() -> MusicApiProviders:
    global _provedores_api
    if _provedores_api is None:
        _provedores_api = MusicApiProviders()
    return _provedores_api


def youtube_api_fast_disponivel() -> bool:
    return busca_youtube_disponivel(
        limite_diario=int(getattr(config, "MUSIC_SEARCH_YOUTUBE_API_DAILY_SOFT_CALLS", 80) or 0),
        habilitado=bool(getattr(config, "MUSIC_SEARCH_YOUTUBE_API_QUOTA_GUARD_ENABLED", True)),
    )


async def buscar_candidatos_youtube_fast(query: str, *, limit: int = 3) -> list[ApiTrackCandidate]:
    """Uma chamada YouTube API-first, sem cache de resultado.

    Apenas requests simultaneos equivalentes sao coalescidos. Depois que a
    chamada termina, o resultado e descartado; a memoria de escolha e a unica
    persistencia da pesquisa textual.
    """
    providers = _provedores()
    if not (providers.enabled and providers.youtube_api_key):
        return []
    consulta = analisar_consulta(query)
    texto = consulta.raw or str(query or "").strip()
    if not texto:
        return []
    timeout = max(
        0.15,
        float(getattr(config, "MUSIC_SEARCH_API_FIRST_TIMEOUT_SECONDS", 0.45) or 0.45),
    )

    async def _buscar() -> list[ApiTrackCandidate]:
        return await providers.search_youtube_fast(texto, limit=limit, timeout_seconds=timeout)

    return await buscar_metadata_compartilhada(
        chave_semantica_busca(texto),
        limit=limit,
        produtor=_buscar,
        ttl_seconds=0.0,
        max_itens=1,
        namespace="youtube-fast-no-cache",
        cancelar_quando_sem_consumidores=True,
    )


async def fechar_provedores_busca() -> None:
    global _provedores_api
    providers = _provedores_api
    _provedores_api = None
    if providers is not None:
        await providers.close()
