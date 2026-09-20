from __future__ import annotations

from ..metadados.modelos import ApiTrackCandidate
from ..metadados.provedores_api import MusicApiProviders
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
    return await providers.search_sources(texto, limit=limit, prefer_youtube=True)
