from __future__ import annotations

from cogs.musica import configuracao as config

from ..metadados.modelos import ApiTrackCandidate
from ..metadados.provedores_api import MusicApiProviders
from ..metadados.resiliencia import buscar_metadata_compartilhada
from ..metadados.quota_youtube import busca_youtube_disponivel
from .chaves import chave_semantica_busca
from .intencao import analisar_consulta
from .roteamento_fontes import planejar_fontes

_provedores_api: MusicApiProviders | None = None


def _provedores() -> MusicApiProviders:
    global _provedores_api
    if _provedores_api is None:
        _provedores_api = MusicApiProviders()
    return _provedores_api


def youtube_api_fast_disponivel() -> bool:
    # Credencial/enable continuam responsabilidade do provider. Aqui o roteador
    # consulta apenas o guard local, mantendo o fast path testável e desacoplado.
    return busca_youtube_disponivel(
        limite_diario=int(getattr(config, "MUSIC_SEARCH_YOUTUBE_API_DAILY_SOFT_CALLS", 80) or 0),
        habilitado=bool(getattr(config, "MUSIC_SEARCH_YOUTUBE_API_QUOTA_GUARD_ENABLED", True)),
    )


async def buscar_candidatos_youtube_fast(query: str, *, limit: int = 3) -> list[ApiTrackCandidate]:
    """Fast path opcional: somente YouTube Data API, sem tocar no Phone Worker."""
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

    # No caminho normal nao guardamos resultado de pesquisa: apenas coalescemos
    # requests equivalentes que estejam simultaneamente em voo. A memoria
    # persistente de escolhas e o unico estado reutilizado entre pesquisas.
    cache_texto = chave_semantica_busca(texto)
    return await buscar_metadata_compartilhada(
        cache_texto,
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


async def buscar_candidatos_multifonte(
    query: str,
    *,
    limit: int = 3,
    incluir_youtube: bool = True,
) -> list[ApiTrackCandidate]:
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

    profundo = int(limit or limite_fast) > limite_fast
    plano = None
    if bool(getattr(config, "MUSIC_SEARCH_PROVIDER_ROUTING_ENABLED", True)):
        plano = planejar_fontes(
            texto,
            profundo=profundo,
            incluir_youtube=bool(incluir_youtube),
        )
        if plano.max_fontes <= 0:
            return []

    async def _buscar() -> list[ApiTrackCandidate]:
        kwargs = {}
        if plano is not None:
            kwargs = {
                "provider_order": plano.prioridades,
                "max_providers": plano.max_fontes,
            }
        return await providers.search_sources(
            texto,
            limit=limit,
            prefer_youtube=bool(incluir_youtube),
            total_budget_seconds=budget,
            **kwargs,
        )

    cache_texto = (
        chave_semantica_busca(texto)
        if bool(getattr(config, "MUSIC_SEARCH_SEMANTIC_CACHE_ENABLED", True))
        else texto
    )
    return await buscar_metadata_compartilhada(
        cache_texto,
        limit=limit,
        produtor=_buscar,
        ttl_seconds=float(getattr(config, "MUSIC_SEARCH_METADATA_CACHE_TTL_SECONDS", 300.0) or 0.0),
        max_itens=int(getattr(config, "MUSIC_SEARCH_METADATA_CACHE_MAX_ITEMS", 128) or 128),
        namespace=(
            ("route:" + plano.assinatura)
            if plano is not None
            else ("all" if incluir_youtube else "sem-youtube")
        ),
        cancelar_quando_sem_consumidores=True,
    )
