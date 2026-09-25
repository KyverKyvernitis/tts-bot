"""Filtros de velocidade e ambiência; graves são reforçados no mixer."""
from __future__ import annotations

NIGHTCORE_SPEED = 1.25
SLOWED_SPEED = 0.8
SLOWED_REVERB_FILTER = (
    "aecho=0.75:0.75:65|145|290|520:0.27|0.20|0.14|0.09,"
    "volume=1.52,alimiter=limit=0.96:attack=5:release=100:level=0:latency=1"
)


def velocidade(*, nightcore: bool, slowed_reverb: bool = False, is_live: bool = False) -> float:
    # Uma transmissão ao vivo chega em tempo real e não suporta alterar o ritmo.
    if is_live:
        return 1.0
    return SLOWED_SPEED if slowed_reverb else NIGHTCORE_SPEED if nightcore else 1.0


def filtros(
    *, bassboost: bool, nightcore: bool, slowed_reverb: bool = False, is_live: bool = False,
    resample: str = "", nightcore_resample: str = "aresample=48000",
) -> str:
    parts: list[str] = [resample] if resample else []
    if nightcore and not is_live:
        # Normalize a entrada antes de elevar a taxa; a origem pode ser 44,1 kHz.
        if not resample:
            parts.append("aresample=48000")
        parts.extend(("asetrate=60000", nightcore_resample))
    elif slowed_reverb and not is_live:
        # Alterar a taxa reduz tom e andamento juntos, como em edits slowed.
        # A ambiência fica apenas na música: TTS é misturado depois do FFmpeg.
        if not resample:
            parts.append("aresample=48000")
        parts.extend(("asetrate=38400", nightcore_resample, SLOWED_REVERB_FILTER))
    # Bassboost preserva o áudio original aqui: o mixer aplica o ganho somente
    # ao grave depois do volume, usando a folga disponível no PCM de saída.
    return ",".join(parts)
