from __future__ import annotations

from cogs.musica import configuracao as config

from ..metadados.modelos import ApiTrackCandidate
from ..metadados.provedores_api import MusicApiProviders
from ..metadados.resiliencia import buscar_metadata_compartilhada
from .intencao import analisar_consulta

_provedores_api: MusicApiProviders | None = None


def _provedores() -> MusicApiProviders:
    global _provedores_api
    if _provedores_api is None:
        _provedores_api = MusicApiProviders()
    return _provedores_api


async def buscar_candidatos_multifonte(query: str, *, limit: int = 5) -> list[ApiTrackCandidate]:
    """Busca metadata em providers opcionais sem resolver qualquer stream."""
    providers = _provedores()
    if not providers.has_any_provider:
        return []
    consulta = analisar_consulta(query)
    texto = consulta.raw or str(query or "").strip()
    if not texto:
        return []

    async def _buscar() -> list[ApiTrackCandidate]:
        return await providers.search_sources(texto, limit=limit, prefer_youtube=True)

    return await buscar_metadata_compartilhada(
        texto,
        limit=limit,
        produtor=_buscar,
        ttl_seconds=float(getattr(config, "MUSIC_SEARCH_METADATA_CACHE_TTL_SECONDS", 30.0) or 0.0),
        max_itens=int(getattr(config, "MUSIC_SEARCH_METADATA_CACHE_MAX_ITEMS", 64) or 64),
    )
