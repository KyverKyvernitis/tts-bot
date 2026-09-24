"""Efeitos leves aplicados somente ao PCM musical no Phone Worker."""
from __future__ import annotations

NIGHTCORE_SPEED = 1.25


def velocidade(*, nightcore: bool, is_live: bool = False) -> float:
    # Uma transmissão ao vivo chega em tempo real e não suporta acelerar a fonte.
    return NIGHTCORE_SPEED if nightcore and not is_live else 1.0


def filtros(*, bassboost: bool, nightcore: bool, is_live: bool = False, resample: str = "") -> str:
    parts: list[str] = [resample] if resample else []
    if nightcore and not is_live:
        # Normalize a entrada antes de elevar a taxa; a origem pode ser 44,1 kHz.
        if not resample:
            parts.append("aresample=48000")
        parts.extend(("asetrate=60000", "aresample=48000"))
    if bassboost:
        # Headroom antes da conversão s16 evita saturar o PCM com o ganho de graves.
        parts.extend(("bass=g=6:f=90", "volume=-6dB"))
    return ",".join(parts)
