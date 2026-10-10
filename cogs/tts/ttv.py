"""Catálogo e preferências de TTV, sem depender dos componentes do Discord.

O catálogo contém somente vozes com uma rota de síntese implementada. Perfis
Standard e English são bancos da mesma personagem, e não vozes distintas.
IDs desconhecidos são rejeitados para nunca trocar a personagem em silêncio.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


DEFAULT_VOICE_ID = "kasane-teto"
DEFAULT_PITCH_SEMITONES = "+0.0"
DEFAULT_SPEECH_RATE = 1.0


@dataclass(frozen=True, slots=True)
class TTVVoice:
    id: str
    name: str
    engine: str
    description: str


VOICE_CATALOG = (
    TTVVoice(
        id=DEFAULT_VOICE_ID,
        name="Kasane Teto",
        engine="teto",
        description="A voz da Kasane Teto, com síntese no phone worker.",
    ),
)
VOICES = VOICE_CATALOG
_VOICES_BY_ID = {voice.id: voice for voice in VOICE_CATALOG}
_LEGACY_VOICE_IDS = {
    "teto": DEFAULT_VOICE_ID,
    "kasane_teto": DEFAULT_VOICE_ID,
    "kasane-teto-standard": DEFAULT_VOICE_ID,
    "kasane-teto-english-cvvc": DEFAULT_VOICE_ID,
}


def normalize_voice_id(value: object) -> str:
    """Return the canonical voice ID; an unknown explicit ID raises ValueError."""
    voice_id = str(value if value is not None else "").strip().lower()
    voice_id = _LEGACY_VOICE_IDS.get(voice_id, voice_id) or DEFAULT_VOICE_ID
    if voice_id not in _VOICES_BY_ID:
        raise ValueError("Vocaloid desconhecida ou ainda indisponível.")
    return voice_id


def get_voice(value: object = DEFAULT_VOICE_ID) -> TTVVoice:
    return _VOICES_BY_ID[normalize_voice_id(value)]


def voice_label(value: object = DEFAULT_VOICE_ID) -> str:
    return get_voice(value).name


def normalize_pitch(value: object) -> str:
    """Validate finite half-semitone steps in [-4, +4], returning signed text."""
    text = str(value if value is not None else "0").strip().lower()
    text = text.replace(",", ".").replace("−", "-").replace("–", "-").replace("—", "-")
    for suffix in ("semitones", "semitone", "semitons", "semitom", "st"):
        if text.endswith(suffix):
            text = text[:-len(suffix)].strip()
            break
    try:
        pitch = float(text or "0")
    except (TypeError, ValueError):
        raise ValueError("O tom deve ser um número de −4 a +4 semitons.") from None
    rounded = round(pitch * 2.0) / 2.0 if math.isfinite(pitch) else 0.0
    if not math.isfinite(pitch) or not -4.0 <= pitch <= 4.0 or abs(pitch - rounded) > 1e-9:
        raise ValueError("O tom deve ficar entre −4 e +4 semitons, em passos de 0,5.")
    # Canonical zero prevents two cache identities for -0.0 and +0.0.
    return f"{rounded if rounded else 0.0:+.1f}"


def normalize_speech_rate(value: object, default: float = DEFAULT_SPEECH_RATE) -> float:
    """Validate a finite speed multiplier in [0.75, 1.5], preserving custom values."""
    if value is None or (isinstance(value, str) and not value.strip()):
        value = default
    try:
        rate = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError("A velocidade deve ficar entre 75% e 150%.") from None
    if not math.isfinite(rate) or not 0.75 <= rate <= 1.5:
        raise ValueError("A velocidade deve ficar entre 75% e 150%.")
    return rate


def resolve_preferences(mapping: Mapping[str, Any] | None, default_pitch: object = 0.0) -> dict[str, Any]:
    """Resolve personal fields without writing a migration or touching Edge values.

    A missing or malformed new pitch uses the legacy Teto value, then the
    configured default. Malformed saved speeds use 1.0. Explicit unknown voice
    IDs remain an error: only a missing voice selects the default character.
    """
    values = mapping or {}
    pitch = None
    for candidate in (
        values.get("ttv_pitch_semitones"),
        values.get("teto_pitch_semitones"),
        default_pitch,
    ):
        if candidate is None or (isinstance(candidate, str) and not candidate.strip()):
            continue
        try:
            pitch = normalize_pitch(candidate)
            break
        except ValueError:
            continue
    try:
        rate = normalize_speech_rate(values.get("ttv_speech_rate"))
    except ValueError:
        rate = DEFAULT_SPEECH_RATE
    return {
        "ttv_voice_id": normalize_voice_id(values.get("ttv_voice_id")),
        "ttv_pitch_semitones": pitch or DEFAULT_PITCH_SEMITONES,
        "ttv_speech_rate": rate,
    }
