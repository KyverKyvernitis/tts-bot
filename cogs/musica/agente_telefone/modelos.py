from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from cogs.musica import configuracao as config

MUSIC_WORKER_UNAVAILABLE_MESSAGE = str(
    getattr(config, "MUSIC_WORKER_UNAVAILABLE_MESSAGE", "Sistema de música indisponível no momento: Nenhum worker online")
    or "Sistema de música indisponível no momento: Nenhum worker online"
).strip()
MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE = str(
    getattr(config, "MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE", "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta")
    or "Sistema de música indisponível no momento: O worker está online, mas a música ainda não está pronta"
).strip()
MUSIC_WORKER_NO_CAPACITY_MESSAGE = str(
    getattr(
        config,
        "MUSIC_WORKER_NO_CAPACITY_MESSAGE",
        "Sistema de música indisponível no momento: Há worker online, mas nenhum está apto para música",
    )
    or "Sistema de música indisponível no momento: Há worker online, mas nenhum está apto para música"
).strip()


class MusicWorkerUnavailable(RuntimeError):
    pass


class MusicWorkerEngineUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class MusicWorkerSelection:
    available: bool
    worker_id: str = ""
    name: str = ""
    reason: str = ""
    worker: Mapping[str, Any] | None = None
    # Idade da validação original, também quando a seleção vem do cache.
    # Estes campos são locais: não fazem parte do protocolo com o worker.
    validated_at: float = field(default=0.0, repr=False, compare=False)
    validated_until: float = field(default=0.0, repr=False, compare=False)
    validation_key: tuple[Any, ...] = field(default=(), repr=False, compare=False)

    @property
    def message(self) -> str:
        if self.available:
            return ""
        reason = str(self.reason or "").lower()
        if (
            "music_agent_full" in reason
            or "sem_capacidade" in reason
            or "capacity" in reason
            or "não_turbo" in reason
            or "nao_turbo" in reason
        ):
            return MUSIC_WORKER_NO_CAPACITY_MESSAGE
        if "music_agent" in reason or "agent_" in reason or "dependency" in reason or "engine" in reason:
            return MUSIC_WORKER_ENGINE_UNAVAILABLE_MESSAGE
        return MUSIC_WORKER_UNAVAILABLE_MESSAGE
