from __future__ import annotations

from cogs.musica import configuracao as config

from .conversao_resolucao import parece_url


def limite_resolucao(query: str, limit: int, *, permitir_playlist: bool) -> tuple[int, bool]:
    """Normaliza limite e distingue busca textual de URL direta."""
    try:
        max_limit = max(1, min(10, int(limit or 3)))
    except Exception:
        max_limit = 3
    busca_textual = not parece_url(query)
    if not busca_textual and not permitir_playlist:
        # Link direto de faixa deve respeitar exatamente o URL enviado.
        max_limit = 1
    return max_limit, busca_textual


def timeout_resolucao(*, somente_metadados: bool, timeout_seconds: float | None) -> float:
    """Resolve o timeout da tarefa remota sem acoplar o transporte HTTP."""
    if somente_metadados:
        default_timeout = float(
            getattr(config, "MUSIC_WORKER_YTDLP_SEARCH_TIMEOUT_SECONDS", 12.0)
            or 12.0
        )
    else:
        default_timeout = float(
            getattr(config, "MUSIC_WORKER_YTDLP_TIMEOUT_SECONDS", 28.0)
            or 28.0
        )
    return max(
        5.0,
        float(timeout_seconds if timeout_seconds is not None else default_timeout),
    )


def montar_tarefa_resolucao(
    *,
    query: str,
    limit: int,
    timeout_seconds: float,
    somente_metadados: bool,
    permitir_playlist: bool,
    busca_textual: bool,
    fast_search: bool | None = None,
) -> dict[str, object]:
    """Monta apenas o contrato serializável enviado ao Phone Worker."""
    return {
        "task": "music_ytdlp_resolve",
        "query": query,
        "limit": limit,
        "timeout_seconds": timeout_seconds,
        "metadata_only": bool(somente_metadados),
        "allow_playlist": bool(permitir_playlist),
        "js_runtimes": str(
            getattr(config, "MUSIC_WORKER_YTDLP_JS_RUNTIMES", "node") or "node"
        ),
        "default_search": f"ytsearch{limit}" if busca_textual else "auto",
        "fast_search": bool(somente_metadados and busca_textual) if fast_search is None else bool(fast_search),
    }
