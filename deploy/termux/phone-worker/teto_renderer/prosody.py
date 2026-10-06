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
    pitchbend: str = "AA"
    gain: float = 1.0
    stressed: bool = False
    contour: str = "neutral"
    deaccented: bool = False
    role: str = "nucleus"
    source_phonemes: tuple[str, ...] = ()
    coverage: str = "standard-cv"
    word_index: int = 0
    mora_index: int = 0
    word_moras: int = 1
    word_end: bool = False
    phrase_end: str = ""


_PITCH_CLASSES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_UTAU_BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


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


def _tempo(value: int | float) -> int:
    try:
        tempo = int(value)
    except (TypeError, ValueError):
        return 140
    return max(60, min(240, tempo))


def _encode_pitch_value(cents: int) -> str:
    value = max(-2048, min(2047, int(round(cents))))
    if value < 0:
        value += 4096
    return _UTAU_BASE64[(value >> 6) & 0x3F] + _UTAU_BASE64[value & 0x3F]


def encode_pitchbend(values: list[int]) -> str:
    """Encode signed-cent UTAU pitch points as 12-bit Base64 + RLE."""
    if not values:
        return "AA"
    output: list[str] = []
    index = 0
    while index < len(values):
        code = _encode_pitch_value(values[index])
        run = 1
        while index + run < len(values) and values[index + run] == values[index]:
            run += 1
        if run >= 3:
            output.append(f"{code}#{run}#")
        else:
            output.append(code * run)
        index += run
    return "".join(output) or "AA"


def _smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def _pitch_curve(start: int, peak: int, end: int, *, duration_ms: int, tempo: int) -> str:
    # UTAU pitch points are spaced at 1/96 of a quarter note. Generate enough
    # points for the note so Straycat receives a continuous contour instead of
    # the old constant AA pitchbend.
    points = round(max(1, duration_ms) * tempo * 96 / 60000)
    points = max(8, min(64, points))
    bend: list[int] = []
    peak_at = max(2, min(points - 2, round(points * 0.48)))
    for index in range(points):
        if index <= peak_at:
            ratio = _smoothstep(index / max(1, peak_at))
            cents = start + (peak - start) * ratio
        else:
            ratio = _smoothstep((index - peak_at) / max(1, points - 1 - peak_at))
            cents = peak + (end - peak) * ratio
        # Two-cent quantization avoids needless fragment variants while staying
        # well below an audible semitone step.
        bend.append(int(round(cents / 2.0) * 2))
    return encode_pitchbend(bend)


def _phrase_spans(moras: list[Mora]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for index, mora in enumerate(moras):
        if mora.phrase_end:
            spans.append((start, index))
            start = index + 1
    if start < len(moras):
        spans.append((start, len(moras) - 1))
    return spans


def _phrase_terminal_nuclei(moras: list[Mora]) -> dict[int, str]:
    terminals: dict[int, str] = {}
    for start, end in _phrase_spans(moras):
        ending = moras[end].phrase_end
        terminal = next(
            (index for index in range(end, start - 1, -1) if moras[index].role == "nucleus"),
            end,
        )
        terminals[terminal] = ending
    return terminals


def _centers(moras: list[Mora]) -> list[int]:
    values = [0 for _ in moras]
    for start, end in _phrase_spans(moras):
        count = end - start + 1
        ending = moras[end].phrase_end
        terminal = next(
            (index for index in range(end, start - 1, -1) if moras[index].role == "nucleus"),
            end,
        )
        if ending == "?":
            phrase_start, phrase_end = 6, 20
        elif ending in {",", ";", ":"}:
            phrase_start, phrase_end = 8, -2
        elif ending == "!":
            phrase_start, phrase_end = 12, -10
        else:
            phrase_start, phrase_end = 9, -18

        for local_index, index in enumerate(range(start, end + 1)):
            ratio = local_index / max(1, count - 1)
            center = phrase_start + (phrase_end - phrase_start) * ratio
            mora = moras[index]
            if mora.role != "nucleus":
                # Auxiliary CV/coda units should carry articulation, not their
                # own melodic accent. Keep them near the surrounding baseline.
                center -= 8
            elif mora.stressed:
                center += 34
            elif mora.deaccented:
                center -= 10
            elif mora.word_moras > 1:
                center -= 3
            if index + 1 <= end and moras[index + 1].stressed and moras[index + 1].word_index == mora.word_index:
                center += 7
            if mora.word_end and not mora.phrase_end:
                center -= 3
            values[index] = int(round(center))

        previous_nucleus = next(
            (index for index in range(terminal - 1, start - 1, -1) if moras[index].role == "nucleus"),
            None,
        )
        if ending == "?":
            if previous_nucleus is not None:
                values[previous_nucleus] = max(values[previous_nucleus], 24)
            values[terminal] = max(values[terminal], 64)
        elif ending in {".", "\n"}:
            if previous_nucleus is not None:
                values[previous_nucleus] = min(values[previous_nucleus], -14)
            values[terminal] = min(values[terminal], -42)
        elif ending == "!":
            if previous_nucleus is not None:
                values[previous_nucleus] = max(values[previous_nucleus], 24)
            values[terminal] = min(values[terminal], -18)
        elif not ending:
            values[terminal] = min(values[terminal], -28)

        # A trailing coda/glide/nasal carries articulation, not the sentence's
        # melodic target. Let it follow the last lexical nucleus instead of
        # stealing the question rise or statement fall.
        for index in range(terminal + 1, end + 1):
            values[index] = int(round(values[terminal] * 0.82))

    return [max(-84, min(84, value)) for value in values]


def _duration_for(moras: list[Mora], index: int) -> int:
    mora = moras[index]
    duration = float(mora.duration_ms)
    if mora.role == "epenthetic":
        duration *= 0.90
    elif mora.role == "transition":
        duration *= 0.92
    elif mora.role == "cluster":
        duration *= 0.90
    elif mora.role == "coda":
        duration *= 0.96
    elif mora.role == "glide":
        duration *= 0.94
    elif mora.role == "nasal":
        duration *= 0.98
    elif mora.stressed:
        duration *= 1.14
    elif mora.deaccented:
        duration *= 0.88
    elif mora.word_moras > 1:
        duration *= 0.90
    if mora.role == "nucleus" and index + 1 < len(moras) and moras[index + 1].stressed and moras[index + 1].word_index == mora.word_index:
        duration *= 0.96
    if mora.phrase_end in {".", "!", "?", "\n"}:
        duration *= 1.08
    elif mora.phrase_end in {",", ";", ":"}:
        duration *= 1.02
    elif mora.word_end and mora.role == "nucleus":
        duration *= 1.015
    minimum = 34 if mora.role == "epenthetic" else 36 if mora.role in {"transition", "cluster"} else 38 if mora.role in {"coda", "glide"} else 48 if mora.role == "nasal" else 78
    maximum = 82 if mora.role == "epenthetic" else 92 if mora.role in {"transition", "cluster"} else 96 if mora.role in {"coda", "glide", "nasal"} else 240
    return max(minimum, min(maximum, round(duration)))


def _contour_name(mora: Mora, *, terminal_ending: str = "") -> str:
    if mora.role != "nucleus":
        return mora.role
    if terminal_ending == "?":
        return "question-rise"
    if terminal_ending in {".", "\n"}:
        return "statement-fall"
    if terminal_ending == "!":
        return "exclamation-fall"
    if terminal_ending in {",", ";", ":"}:
        return "continuation"
    if mora.stressed:
        return "stress"
    if mora.deaccented:
        return "deaccented"
    return "neutral"


def build_notes(
    moras: list[Mora], *, base_pitch: str = "C4", speech_rate: float = 1.0, tempo: int = 140
) -> list[RenderNote]:
    if not moras:
        return []
    rate = _speech_rate(speech_rate)
    tempo_value = _tempo(tempo)
    centers = _centers(moras)
    terminal_endings = _phrase_terminal_nuclei(moras)
    notes: list[RenderNote] = []

    for index, mora in enumerate(moras):
        raw_duration = round(_duration_for(moras, index) / rate)
        minimum = 32 if mora.role == "epenthetic" else 34 if mora.role in {"transition", "cluster"} else 36 if mora.role in {"coda", "glide"} else 44 if mora.role == "nasal" else 70
        duration = max(minimum, min(500, raw_duration))
        pause = max(0, min(1000, round(mora.pause_after_ms / rate)))
        center = centers[index]
        previous = centers[index - 1] if index > 0 and not moras[index - 1].phrase_end else center
        following = centers[index + 1] if index + 1 < len(moras) and not mora.phrase_end else center
        start = round((previous + center) / 2)
        end = round((center + following) / 2)
        terminal_ending = terminal_endings.get(index, "")
        peak = center + (0 if mora.role != "nucleus" else (12 if mora.stressed else (1 if mora.deaccented else 3)))
        if terminal_ending == "?":
            peak = max(peak, 68)
        elif terminal_ending in {".", "\n"}:
            peak = min(peak, center + 1)
        continuous_cvvc_aux = (
            mora.role != "nucleus"
            and mora.coverage in {"cvvc-transition", "cluster-hit", "approximation"}
        )
        if continuous_cvvc_aux:
            # Auxiliary CVVC pieces are articulation overlays, not independent
            # notes. Giving each one a fresh speech contour resets the spectral
            # trajectory at every boundary and is a major source of the robotic
            # "travado" sound. Keep one coarse pitch and let lexical nuclei own
            # phrase prosody.
            pitchbend = "AA"
            cap = {
                "transition": 48, "cluster": 46, "coda": 50,
                "glide": 50, "nasal": 56,
            }.get(mora.role, 52)
            duration = min(duration, cap)
        else:
            pitchbend = _pitch_curve(start, peak, end, duration_ms=duration, tempo=tempo_value)

        if mora.role == "epenthetic":
            gain = 0.72
        elif mora.role == "transition":
            gain = 0.84 if continuous_cvvc_aux else 0.90
        elif mora.role == "cluster":
            gain = 0.80 if continuous_cvvc_aux else 0.88
        elif mora.role == "coda":
            gain = 0.74 if continuous_cvvc_aux else 0.78
        elif mora.role == "glide":
            gain = 0.76 if continuous_cvvc_aux else 0.82
        elif mora.role == "nasal":
            gain = 0.82 if continuous_cvvc_aux else 0.88
        elif mora.stressed:
            gain = 1.055
        elif mora.deaccented:
            gain = 0.955
        else:
            gain = 0.985 if mora.word_moras > 1 else 1.0
        if mora.phrase_end in {".", "?", "!", "\n"}:
            gain *= 0.985

        notes.append(RenderNote(
            candidates=mora.candidates,
            # Keep the coarse note stable and let the UTAU pitchbend carry the
            # speech contour. This prevents audible semitone stair-steps.
            pitch=_relative_pitch(base_pitch, 0),
            duration_ms=duration,
            pause_after_ms=pause,
            pitchbend=pitchbend,
            gain=max(0.68, min(1.10, gain)),
            stressed=mora.stressed,
            contour=_contour_name(mora, terminal_ending=terminal_ending),
            deaccented=mora.deaccented,
            role=mora.role,
            source_phonemes=mora.source_phonemes,
            coverage=mora.coverage,
            word_index=mora.word_index,
            mora_index=mora.mora_index,
            word_moras=mora.word_moras,
            word_end=mora.word_end,
            phrase_end=mora.phrase_end,
        ))
    return notes
