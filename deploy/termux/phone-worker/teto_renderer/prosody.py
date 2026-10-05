from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .phonemizer import Mora


@dataclass(frozen=True, slots=True)
class RenderNote:
    candidates: tuple[str, ...]
    pitch: str
    duration_ms: int
    pause_after_ms: int


_PITCH_CLASSES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def _relative_pitch(base_pitch: str, semitones: int) -> str:
    match = re.fullmatch(r"([A-Ga-g])([#b]?)(-?\d{1,2})", str(base_pitch).strip())
    midi = 60  # C4 is the defensive fallback for an invalid configuration.
    if match:
        name, accidental, octave = match.groups()
        midi = (int(octave) + 1) * 12 + _PITCH_CLASSES[name.upper()]
        midi += {"#": 1, "b": -1, "": 0}[accidental]
    midi = max(0, min(127, midi + semitones))
    return f"{_PITCH_NAMES[midi % 12]}{midi // 12 - 1}"


def _speech_rate(value: float) -> float:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(rate) or rate <= 0:
        return 1.0
    return max(0.5, min(2.0, rate))


def build_notes(
    moras: list[Mora], *, base_pitch: str = "C4", speech_rate: float = 1.0
) -> list[RenderNote]:
    if not moras:
        return []
    rate = _speech_rate(speech_rate)
    notes: list[RenderNote] = []
    for index, mora in enumerate(moras):
        # Only signal a phrase ending; do not guess Portuguese word stress.
        # Three relative pitches keep fragment-cache variants limited.
        semitones = 0
        if mora.phrase_end == "?":
            semitones = 1
        elif mora.phrase_end in {".", "!", "\n"}:
            semitones = -1
        elif index == len(moras) - 1 and not mora.phrase_end:
            semitones = -1
        pitch = _relative_pitch(base_pitch, semitones)
        notes.append(RenderNote(
            candidates=mora.candidates,
            pitch=pitch,
            duration_ms=max(70, min(500, round(mora.duration_ms / rate))),
            pause_after_ms=max(0, min(1000, round(mora.pause_after_ms / rate))),
        ))
    return notes
