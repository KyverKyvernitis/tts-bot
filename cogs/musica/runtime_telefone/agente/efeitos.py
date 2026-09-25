"""Filtros de velocidade; o reforço de graves atua no mixer após o volume."""
from __future__ import annotations

NIGHTCORE_SPEED = 1.25


def velocidade(*, nightcore: bool, is_live: bool = False) -> float:
    # Uma transmissão ao vivo chega em tempo real e não suporta acelerar a fonte.
    return NIGHTCORE_SPEED if nightcore and not is_live else 1.0


def filtros(
    *, bassboost: bool, nightcore: bool, is_live: bool = False,
    resample: str = "", nightcore_resample: str = "aresample=48000",
) -> str:
    parts: list[str] = [resample] if resample else []
    if nightcore and not is_live:
        # Normalize a entrada antes de elevar a taxa; a origem pode ser 44,1 kHz.
        if not resample:
            parts.append("aresample=48000")
        parts.extend(("asetrate=60000", nightcore_resample))
    # Bassboost preserva o áudio original aqui: o mixer aplica o ganho somente
    # ao grave depois do volume, usando a folga disponível no PCM de saída.
    return ",".join(parts)
