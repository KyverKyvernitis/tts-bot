from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class SinucaActivityContext:
    mode: str
    guild_id: int | None
    channel_id: int | None
    instance_id: str | None


class GincanaSinucaMixin:
    """Base da integração da Activity de sinuca.

    Nesta primeira etapa, a gincana só ganha utilitários pequenos para o fluxo
    futuro da Activity. O lobby e a economia entram em patches separados.
    """

    _SINUCA_ACTIVITY_STAKE = 25

    def _build_sinuca_activity_context(self, payload: dict[str, Any] | None) -> SinucaActivityContext:
        payload = payload or {}
        guild_id = payload.get("guild_id")
        channel_id = payload.get("channel_id")
        instance_id = payload.get("instance_id") or payload.get("instanceId")
        mode = "server" if guild_id else "casual"
        return SinucaActivityContext(
            mode=mode,
            guild_id=int(guild_id) if guild_id else None,
            channel_id=int(channel_id) if channel_id else None,
            instance_id=str(instance_id) if instance_id else None,
        )
