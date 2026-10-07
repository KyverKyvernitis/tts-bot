"""Filtros de velocidade e ambiência; graves são reforçados no mixer."""
from __future__ import annotations

SAMPLE_RATE = 48000
MAX_EFFECT_LEVEL = 3
NIGHTCORE_STEP = 0.25
SLOWED_STEP = 0.20
SLOWED_REVERB_DECAYS = (0.27, 0.20, 0.14, 0.09)


def nivel_efeito(value: object, *, enabled: bool = False) -> int:
    """Normaliza nível 0..3 mantendo compatibilidade com flags booleanas antigas."""
    try:
        level = int(value) if value is not None else 0
    except (TypeError, ValueError):
        level = 0
    if level <= 0 and enabled:
        level = 1
    return max(0, min(MAX_EFFECT_LEVEL, level))


def velocidade(
    *,
    nightcore: bool,
    slowed_reverb: bool = False,
    is_live: bool = False,
    nightcore_level: int | None = None,
    slowed_reverb_level: int | None = None,
) -> float:
    # Uma transmissão ao vivo chega em tempo real e não suporta alterar o ritmo.
    if is_live:
        return 1.0
    slow_level = nivel_efeito(slowed_reverb_level, enabled=slowed_reverb)
    night_level = nivel_efeito(nightcore_level, enabled=nightcore)
    if slow_level:
        return max(0.4, 1.0 - SLOWED_STEP * slow_level)
    if night_level:
        return 1.0 + NIGHTCORE_STEP * night_level
    return 1.0


def _slowed_reverb_filter(level: int) -> str:
    multiplier = max(1, min(MAX_EFFECT_LEVEL, int(level or 1)))
    decays = "|".join(f"{min(0.99, decay * multiplier):.2f}" for decay in SLOWED_REVERB_DECAYS)
    return (
        f"aecho=0.75:0.75:65|145|290|520:{decays},"
        "volume=1.52,alimiter=limit=0.96:attack=5:release=100:level=0:latency=1"
    )


def filtros(
    *,
    bassboost: bool,
    nightcore: bool,
    slowed_reverb: bool = False,
    is_live: bool = False,
    resample: str = "",
    nightcore_resample: str = "aresample=48000",
    nightcore_level: int | None = None,
    slowed_reverb_level: int | None = None,
) -> str:
    parts: list[str] = [resample] if resample else []
    night_level = nivel_efeito(nightcore_level, enabled=nightcore)
    slow_level = nivel_efeito(slowed_reverb_level, enabled=slowed_reverb)
    if night_level and not is_live:
        # Normalize a entrada antes de elevar a taxa; a origem pode ser 44,1 kHz.
        if not resample:
            parts.append("aresample=48000")
        target_rate = round(SAMPLE_RATE * (1.0 + NIGHTCORE_STEP * night_level))
        parts.extend((f"asetrate={target_rate}", nightcore_resample))
    elif slow_level and not is_live:
        # Alterar a taxa reduz tom e andamento juntos, como em edits slowed.
        # A ambiência fica apenas na música: TTS é misturado depois do FFmpeg.
        if not resample:
            parts.append("aresample=48000")
        target_rate = round(SAMPLE_RATE * max(0.4, 1.0 - SLOWED_STEP * slow_level))
        parts.extend((f"asetrate={target_rate}", nightcore_resample, _slowed_reverb_filter(slow_level)))
    # Bassboost preserva o áudio original aqui: o mixer aplica o ganho somente
    # ao grave depois do volume, usando a folga disponível no PCM de saída.
    return ",".join(parts)


TTS_EFFECT_ENGINES = {"edge", "gtts"}
TTS_NIGHTCORE_MULTIPLIERS = (1.0, 1.10, 1.20, 1.30)
TTS_SLOWED_MULTIPLIERS = (1.0, 0.92, 0.84, 0.76)
TTS_REVERB_DECAYS = (
    (0.12, 0.07, 0.04),
    (0.20, 0.12, 0.07),
    (0.28, 0.17, 0.10),
)


def nivel_efeito_tts(value: object) -> int:
    """Normaliza o nível de DSP do TTS para 0..3."""
    try:
        level = int(value or 0)
    except (TypeError, ValueError):
        level = 0
    return max(0, min(MAX_EFFECT_LEVEL, level))


def normalizar_efeitos_tts(
    nightcore_level: object = 0,
    slowed_level: object = 0,
    reverb_level: object = 0,
) -> tuple[int, int, int]:
    """Normaliza níveis e impede Nightcore + Slowed simultâneos.

    A UI já garante exclusividade. Esta defesa mantém payloads antigos ou
    malformados determinísticos sem tocar no estado dos efeitos da música.
    """
    night = nivel_efeito_tts(nightcore_level)
    slow = nivel_efeito_tts(slowed_level)
    reverb = nivel_efeito_tts(reverb_level)
    if night and slow:
        slow = 0
    return night, slow, reverb


def filtros_tts(
    *,
    engine: object,
    nightcore_level: object = 0,
    slowed_level: object = 0,
    reverb_level: object = 0,
) -> str:
    """Monta o DSP pós-síntese do TTS sem afetar música/ducking.

    Só Edge e gTTS são elegíveis. A cauda do reverb é deliberadamente curta
    (até 250 ms) para que o overlay não mantenha a música duckada por tempo
    excessivo depois que a fala termina.
    """
    normalized_engine = str(engine or "").strip().lower().replace("-", "_")
    if normalized_engine not in TTS_EFFECT_ENGINES:
        return ""
    night, slow, reverb = normalizar_efeitos_tts(
        nightcore_level, slowed_level, reverb_level
    )
    if not (night or slow or reverb):
        return ""

    parts: list[str] = []
    if night:
        multiplier = TTS_NIGHTCORE_MULTIPLIERS[night]
        parts.extend((
            "aresample=48000",
            f"asetrate={round(SAMPLE_RATE * multiplier)}",
            "aresample=48000",
        ))
    elif slow:
        multiplier = TTS_SLOWED_MULTIPLIERS[slow]
        parts.extend((
            "aresample=48000",
            f"asetrate={round(SAMPLE_RATE * multiplier)}",
            "aresample=48000",
        ))

    if reverb:
        decays = "|".join(f"{value:.2f}" for value in TTS_REVERB_DECAYS[reverb - 1])
        parts.append(f"aecho=0.82:0.58:55|120|250:{decays}")

    # Não use o alimiter moderno da música aqui. O TTS também pode tocar na
    # VPS (Ubuntu 22.04/FFmpeg mais antigo), e opções como ``latency`` fazem
    # alguns builds encerrarem o decoder antes do primeiro PCM. O aecho já
    # trabalha com ganho de saída conservador e o mixer mantém a proteção da
    # soma quando há música. Assim o mesmo filtergraph funciona nos dois hosts.
    return ",".join(parts)
