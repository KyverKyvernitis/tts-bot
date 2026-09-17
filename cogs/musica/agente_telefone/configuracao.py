from __future__ import annotations

from dataclasses import dataclass

import config

from .utilitarios import _as_bool, _csv


@dataclass(frozen=True, slots=True)
class ConfiguracaoSelecaoWorker:
    worker_only: bool
    agente_ativo: bool
    exigir_turbo: bool
    papeis_obrigatorios: frozenset[str]
    capacidades_obrigatorias: frozenset[str]
    versao_minima_agente: str
    maximo_sessoes: int
    bootstrap_ao_tocar: bool
    cache_segundos: float


def carregar_configuracao_selecao() -> ConfiguracaoSelecaoWorker:
    try:
        maximo_sessoes = max(1, int(getattr(config, "MUSIC_AGENT_MAX_SESSIONS_PER_WORKER", 2) or 2))
    except Exception:
        maximo_sessoes = 2
    try:
        cache_segundos = max(
            0.0,
            min(3.0, float(getattr(config, "MUSIC_WORKER_SELECTION_CACHE_SECONDS", 0.8) or 0.0)),
        )
    except Exception:
        cache_segundos = 0.8

    return ConfiguracaoSelecaoWorker(
        worker_only=_as_bool(getattr(config, "MUSIC_WORKER_ONLY_ENABLED", True), True),
        agente_ativo=_as_bool(getattr(config, "MUSIC_AGENT_ENABLED", True), True),
        exigir_turbo=_as_bool(getattr(config, "MUSIC_WORKER_REQUIRE_TURBO", True), True),
        papeis_obrigatorios=frozenset(_csv(getattr(config, "MUSIC_WORKER_REQUIRED_ROLES", "phone-worker"))),
        capacidades_obrigatorias=frozenset(
            _csv(getattr(config, "MUSIC_WORKER_REQUIRED_CAPABILITIES", "ffmpeg,ffprobe"))
        ),
        versao_minima_agente=str(getattr(config, "MUSIC_AGENT_MIN_VERSION", "0.3.8") or "0.3.8"),
        maximo_sessoes=maximo_sessoes,
        bootstrap_ao_tocar=_as_bool(getattr(config, "MUSIC_AGENT_BOOTSTRAP_ON_PLAY", True), True),
        cache_segundos=cache_segundos,
    )


def music_worker_only_enabled() -> bool:
    return carregar_configuracao_selecao().worker_only
