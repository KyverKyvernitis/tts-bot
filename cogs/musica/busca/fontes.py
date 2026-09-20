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


async def buscar_candidatos_multifonte(query: str, *, limit: int = 3) -> list[ApiTrackCandidate]:
    """Busca metadata em providers opcionais sem resolver qualquer stream."""
    providers = _provedores()
    if not providers.has_any_provider:
        return []
    consulta = analisar_consulta(query)
    texto = consulta.raw or str(query or "").strip()
    if not texto:
        return []

    try:
        limite_fast = max(1, int(getattr(config, "MUSIC_SEARCH_RESULTS", 3) or 3))
    except Exception:
        limite_fast = 3
    budget_attr = (
        "MUSIC_SEARCH_PROVIDER_DEEP_BUDGET_SECONDS"
        if int(limit or limite_fast) > limite_fast
        else "MUSIC_SEARCH_PROVIDER_FAST_BUDGET_SECONDS"
    )
    budget_default = 1.5 if budget_attr.endswith("DEEP_BUDGET_SECONDS") else 0.65
    budget = max(0.05, float(getattr(config, budget_attr, budget_default) or budget_default))

    async def _buscar() -> list[ApiTrackCandidate]:
        return await providers.search_sources(
            texto,
            limit=limit,
            prefer_youtube=True,
            total_budget_seconds=budget,
        )

    return await buscar_metadata_compartilhada(
        texto,
        limit=limit,
        produtor=_buscar,
        ttl_seconds=float(getattr(config, "MUSIC_SEARCH_METADATA_CACHE_TTL_SECONDS", 90.0) or 0.0),
        max_itens=int(getattr(config, "MUSIC_SEARCH_METADATA_CACHE_MAX_ITEMS", 64) or 64),
    )
