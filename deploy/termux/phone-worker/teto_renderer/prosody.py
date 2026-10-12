from __future__ import annotations

import math
import re
import hashlib
import json
from bisect import bisect_right
from dataclasses import dataclass, replace

from .phonemizer import Mora


def validate_performance(text: str, *, score=None, segments=None) -> None:
    """Bound untrusted musical/effect requests before caches or native processes."""
    if not isinstance(text, str) or len(text) > (2048 if score is not None else 180):
        raise ValueError("texto TTV grande demais para esta apresentação")
    if score is None and segments is None:
        raise ValueError("apresentação TTV exige MIDI ou efeitos no texto")
    if score is not None:
        if not isinstance(score, dict) or set(score) - {"version", "track_index", "duration_ms", "lyrics_source", "notes"}:
            raise ValueError("partitura TTV inválida")
        if type(score.get("version")) is not int or score["version"] != 1:
            raise ValueError("versão da partitura TTV não suportada")
        if type(score.get("track_index")) is not int or not 0 <= score["track_index"] < 64:
            raise ValueError("faixa MIDI inválida")
        if score.get("lyrics_source") not in {"message", "midi", "vowel"}:
            raise ValueError("origem da letra MIDI inválida")
        duration = score.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 40 <= duration <= 60000:
            raise ValueError("MIDI deve durar até 60 segundos")
        notes = score.get("notes")
        if not isinstance(notes, list) or not 1 <= len(notes) <= 128:
            raise ValueError("MIDI deve conter entre 1 e 128 notas")
        previous_end = 0.0
        for item in notes:
            if not isinstance(item, dict) or set(item) - {"midi_note", "start_ms", "duration_ms", "velocity", "lyric"}:
                raise ValueError("nota MIDI inválida")
            if type(item.get("midi_note")) is not int or not 36 <= item["midi_note"] <= 96:
                raise ValueError("notas MIDI devem estar entre C2 e C7")
            if type(item.get("velocity")) is not int or not 1 <= item["velocity"] <= 127:
                raise ValueError("intensidade MIDI inválida")
            start, length = item.get("start_ms"), item.get("duration_ms")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (start, length)):
                raise ValueError("tempo da nota MIDI inválido")
            if start < previous_end - 0.001 or length < 40 or start + length > duration + 0.001:
                raise ValueError("MIDI exige uma melodia sem notas sobrepostas e notas de pelo menos 40 ms")
            previous_end = start + length
            if "lyric" in item and (not isinstance(item["lyric"], str) or len(item["lyric"]) > 128):
                raise ValueError("letra incorporada ao MIDI inválida")
    if segments is not None:
        if not isinstance(segments, list) or not 1 <= len(segments) <= 128:
            raise ValueError("efeitos TTV devem conter entre 1 e 128 trechos")
        for segment in segments:
            if not isinstance(segment, dict) or set(segment) != {"text", "fade_in", "fade_out", "hold", "pause_after_ms"}:
                raise ValueError("trecho de efeitos TTV inválido")
            if not isinstance(segment["text"], str) or not segment["text"].strip() or len(segment["text"]) > 2048:
                raise ValueError("texto do trecho TTV inválido")
            if any(type(segment[name]) is not bool for name in ("fade_in", "fade_out", "hold")):
                raise ValueError("efeito TTV inválido")
            if type(segment["pause_after_ms"]) is not int or segment["pause_after_ms"] not in {0, 180}:
                raise ValueError("pausa TTV inválida")
            if segment["text"].strip() == "~" and segment["hold"] and score is None:
                raise ValueError("~ separado exige uma nota MIDI; na fala use aaa~")
        joined = " ".join(" ".join(segment["text"].split()) for segment in segments)
        if joined != " ".join(text.split()):
            raise ValueError("trechos de efeitos TTV não correspondem ao texto")
    if not text.strip() and score is None:
        raise ValueError("texto TTV vazio")


def performance_hash(text: str, *, score=None, segments=None) -> str:
    payload = {"version": 1, "text": " ".join(text.split()), "score": score, "segments": segments}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def align_singing_syllables(moras: list[Mora], score: dict, *, slots=None) -> list[list[int]]:
    """Choose vowel spans; a span may cover tied notes, never a musical rest."""
    nuclei = [mora for mora in moras if mora.role == "nucleus"]
    events = score["notes"]
    if any(event["duration_ms"] > 18000 for event in events):
        raise ValueError("uma vogal contínua pode durar até 18 segundos no ESPER; divida a nota com outra sílaba ou uma pausa MIDI")
    if not nuclei:
        raise ValueError("a letra não contém vogais que a voz possa cantar")
    if len(nuclei) > len(events):
        raise ValueError(f"letra contém {len(nuclei)} sílabas para {len(events)} notas; encurte a letra ou acrescente notas")
    if slots is not None:
        if len(slots) != len(events):
            raise ValueError(f"letra com ~ ocupa {len(slots)} notas; o MIDI contém {len(events)}")
        groups = [[] for _ in nuclei]
        active = None
        for index, slot in enumerate(slots):
            if slot is not None:
                active = slot
            if active is None or not 0 <= active < len(groups):
                raise ValueError("~ precisa vir depois de uma sílaba")
            groups[active].append(index)
        if any(not group for group in groups):
            raise ValueError("cada sílaba precisa de uma nota MIDI")
    else:
        # Balanced phrase alignment gives tonic syllables a little more room.
        # Dynamic programming respects rest boundaries rather than spreading
        # one vowel across silence or arbitrarily discarding surplus notes.
        count, total = len(nuclei), len(events)
        weights = [1.15 if mora.stressed else 1.0 for mora in nuclei]
        voiced_ms = sum(float(event["duration_ms"]) for event in events)
        desired = [voiced_ms * weight / sum(weights) for weight in weights]
        costs = {(0, 0): (0.0, [])}
        for syllable in range(count):
            for used in range(total + 1):
                previous = costs.get((syllable, used))
                if previous is None:
                    continue
                end_limit = total - (count - syllable - 1)
                duration = 0.0
                pitches = []
                for end in range(used + 1, end_limit + 1):
                    if end > used + 1:
                        left, right = events[end - 2], events[end - 1]
                        if right["start_ms"] - left["start_ms"] - left["duration_ms"] > 20:
                            break
                    duration += float(events[end - 1]["duration_ms"])
                    if events[end - 1]["start_ms"] + events[end - 1]["duration_ms"] - events[used]["start_ms"] > 18000:
                        break
                    pitches.append(events[end - 1]["midi_note"])
                    if max(pitches) - min(pitches) > 40:
                        break
                    cost = previous[0] + ((duration - desired[syllable]) / max(40, desired[syllable])) ** 2
                    key = (syllable + 1, end)
                    if key not in costs or cost < costs[key][0]:
                        costs[key] = (cost, previous[1] + [list(range(used, end))])
        result = costs.get((count, total))
        if result is None:
            raise ValueError("a letra não encaixa nas frases do MIDI; acrescente sílabas nas pausas ou divida vogais acima de 18 segundos; use ~ para indicar notas ligadas")
        groups = result[1]
    for group in groups:
        if events[group[-1]]["start_ms"] + events[group[-1]]["duration_ms"] - events[group[0]]["start_ms"] > 18000:
            raise ValueError("uma vogal contínua pode durar até 18 segundos no ESPER; divida a letra com outra sílaba ou uma pausa MIDI")
        for left_index, right_index in zip(group, group[1:]):
            left, right = events[left_index], events[right_index]
            if right["start_ms"] - left["start_ms"] - left["duration_ms"] > 20:
                raise ValueError("~ não pode prolongar uma vogal através de uma pausa MIDI")
        pitches = [events[i]["midi_note"] for i in group]
        if max(pitches) - min(pitches) > 40:
            raise ValueError("melisma abrange mais de 40 semitons; divida a vogal em outro ataque")
    return groups


def singing_pitch_at(score: dict, time_ms: float) -> float:
    """Absolute cents, with short legato transitions and delayed mild vibrato."""
    events = score["notes"]
    starts = [float(event["start_ms"]) for event in events]
    index = max(0, min(len(events) - 1, bisect_right(starts, time_ms) - 1))
    event = events[index]
    pitch = float(event["midi_note"] * 100)
    elapsed = time_ms - float(event["start_ms"])
    if index and 0 <= elapsed < 12:
        previous = events[index - 1]
        gap = event["start_ms"] - previous["start_ms"] - previous["duration_ms"]
        if gap <= 20:
            pitch = previous["midi_note"] * 100 + (pitch - previous["midi_note"] * 100) * _smoothstep(elapsed / 12.0)
    if event["duration_ms"] >= 700 and elapsed >= 550:
        ramp = _smoothstep((elapsed - 550) / 180.0)
        tail = _smoothstep((event["duration_ms"] - elapsed) / 100.0)
        pitch += 12.0 * ramp * tail * math.sin(2 * math.pi * 5.0 * (elapsed - 550) / 1000.0)
    return pitch


def align_singing_pitch(notes: list[RenderNote], placements: list[dict], score: dict, *, preroll_ms: float, tempo: int) -> list[RenderNote]:
    interval = 60000.0 / (_tempo(tempo) * 96.0)
    aligned = []
    for note, placement in zip(notes, placements):
        base = _pitch_midi(note.pitch) * 100
        start = placement["start_ms"] - preroll_ms
        duration = note.duration_ms + placement["lead_ms"]
        points = max(2, math.ceil(duration / interval) + 1)
        scope = placement.get("score_group")
        local_score = {"notes": [score["notes"][number] for number in scope]} if scope is not None else score
        values = [round(singing_pitch_at(local_score, start + point * interval) - base) for point in range(points)]
        # The UTAU wire encoding is signed 12-bit cents. Fail rather than
        # silently clamp a wide melisma to the wrong pitch.
        if min(values) < -2048 or max(values) > 2047:
            raise ValueError("salto de notas grande demais para um único trecho; divida a sílaba")
        aligned.append(replace(note, pitchbend=encode_pitchbend(values), pitch_start_cents=values[0], pitch_end_cents=values[-1]))
    return aligned


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
    profile: str = "standard"
    planner_cost: float = 0.0
    syllable_index: int = 0
    word_syllables: int = 1
    pitch_start_cents: int = 0
    pitch_end_cents: int = 0
    pitch_peak_cents: int | None = None


_PITCH_CLASSES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_PITCH_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_UTAU_BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def _pitch_midi(base_pitch: str) -> int:
    match = re.fullmatch(r"([A-Ga-g])([#b]?)(-?\d{1,2})", str(base_pitch).strip())
    midi = 60  # C4 is the defensive fallback for an invalid configuration.
    if match:
        name, accidental, octave = match.groups()
        midi = (int(octave) + 1) * 12 + _PITCH_CLASSES[name.upper()]
        midi += {"#": 1, "b": -1, "": 0}[accidental]
    return midi


def _relative_pitch(base_pitch: str, semitones: int) -> str:
    midi = max(0, min(127, _pitch_midi(base_pitch) + semitones))
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


def _pitch_line(start: int, end: int, *, duration_ms: int, tempo: int) -> str:
    """Encode one phrase-continuous segment without creating a new local peak."""
    points = round(max(1, duration_ms) * tempo * 96 / 60000)
    points = max(8, min(64, points))
    bend: list[int] = []
    for index in range(points):
        ratio = _smoothstep(index / max(1, points - 1))
        cents = start + (end - start) * ratio
        bend.append(int(round(cents / 2.0) * 2))
    return encode_pitchbend(bend)


def _phrase_spans(moras: list[Mora] | list[RenderNote]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for index, mora in enumerate(moras):
        if mora.phrase_end:
            spans.append((start, index))
            start = index + 1
    if start < len(moras):
        spans.append((start, len(moras) - 1))
    return spans


def align_pitch_to_timeline(
    notes: list[RenderNote], placements: list[dict[str, float]], *, tempo: int = 140,
) -> list[RenderNote]:
    """Sample one F0 envelope per phrase at each fragment's actual PCM time.

    CVVC fragments include preutterance before their lexical anchors and can
    overlap other notes for much of their duration. Matching successive note
    endpoints therefore does not make their simultaneous pitch targets match.
    Nucleus peaks define a shared absolute-time contour; auxiliary aliases sample
    it instead of creating local contours. Phrase edges are held through the
    leading/trailing articulation, with independent contours across punctuation.
    """
    if len(notes) != len(placements):
        raise ValueError("notas e posições Teto têm tamanhos diferentes")
    if not notes or not any(note.profile == "english-cvvc" for note in notes):
        return list(notes)

    interval_ms = 60000.0 / (_tempo(tempo) * 96.0)
    aligned = list(notes)
    for first, last in _phrase_spans(notes):
        nuclei = [index for index in range(first, last + 1) if notes[index].role == "nucleus"]
        if not nuclei:
            nuclei = list(range(first, last + 1))
        initial = notes[nuclei[0]]
        terminal = notes[nuclei[-1]]
        knots: list[tuple[float, float]] = [(
            float(placements[nuclei[0]]["anchor_ms"]),
            _pitch_midi(initial.pitch) * 100.0 + initial.pitch_start_cents,
        )]
        for index in nuclei:
            note = notes[index]
            peak = note.pitch_peak_cents
            if peak is None:
                peak = (note.pitch_start_cents + note.pitch_end_cents) / 2.0
            knots.append((
                float(placements[index]["anchor_ms"]) + note.duration_ms * 0.48,
                _pitch_midi(note.pitch) * 100.0 + peak,
            ))
        knots.append((
            float(placements[nuclei[-1]]["anchor_ms"]) + terminal.duration_ms,
            _pitch_midi(terminal.pitch) * 100.0 + terminal.pitch_end_cents,
        ))
        # Defensive sorting also handles a phrase consisting only of malformed
        # auxiliary notes with identical anchors. The last target owns a tie.
        ordered = dict(sorted(knots, key=lambda knot: knot[0]))
        times = sorted(ordered)

        def pitch_at(time_ms: float) -> float:
            right = bisect_right(times, time_ms)
            if right == 0:
                return ordered[times[0]]
            if right == len(times):
                return ordered[times[-1]]
            left_time, right_time = times[right - 1], times[right]
            ratio = _smoothstep((time_ms - left_time) / (right_time - left_time))
            return ordered[left_time] + (ordered[right_time] - ordered[left_time]) * ratio

        for index in range(first, last + 1):
            note = notes[index]
            if note.profile != "english-cvvc":
                continue
            placement = placements[index]
            start_ms = float(placement["start_ms"])
            duration_ms = note.duration_ms + max(0.0, float(placement["lead_ms"]))
            base_cents = _pitch_midi(note.pitch) * 100.0
            # UTAU points have a fixed tempo-derived interval. Include the point
            # beyond the PCM tail so its interpolation remains defined; a fixed
            # 64-point cap would flatten longer fragments before their endings.
            points = max(2, math.ceil(duration_ms / interval_ms) + 1)
            values = [
                round(pitch_at(start_ms + point * interval_ms) - base_cents)
                for point in range(points)
            ]
            aligned[index] = replace(
                note,
                pitchbend=encode_pitchbend(values),
                pitch_start_cents=round(pitch_at(start_ms) - base_cents),
                pitch_end_cents=round(pitch_at(start_ms + duration_ms) - base_cents),
            )
    return aligned


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
        # A VC exit belongs to the existing vowel/consonant boundary. Adding
        # that recording must not create another beat in the phrase contour.
        active = [index for index in range(start, end + 1) if moras[index].role != "vowel-exit"]
        count = len(active)
        ending = moras[end].phrase_end
        english_speech = any(moras[i].profile == "english-cvvc" for i in range(start, end + 1))
        terminal = next(
            (index for index in range(end, start - 1, -1) if moras[index].role == "nucleus"),
            end,
        )
        if english_speech:
            if ending == "?":
                phrase_start, phrase_end = 4, 14
            elif ending in {",", ";", ":"}:
                phrase_start, phrase_end = 5, -1
            elif ending == "!":
                phrase_start, phrase_end = 7, -8
            else:
                phrase_start, phrase_end = 5, -12
        else:
            if ending == "?":
                phrase_start, phrase_end = 6, 20
            elif ending in {",", ";", ":"}:
                phrase_start, phrase_end = 8, -2
            elif ending == "!":
                phrase_start, phrase_end = 12, -10
            else:
                phrase_start, phrase_end = 9, -18

        for local_index, index in enumerate(active):
            ratio = local_index / max(1, count - 1)
            center = phrase_start + (phrase_end - phrase_start) * ratio
            mora = moras[index]
            if mora.role != "nucleus":
                center -= 3 if english_speech else 8
            elif mora.stressed:
                center += 19 if english_speech else 34
            elif mora.deaccented:
                center -= 6 if english_speech else 10
            elif mora.word_moras > 1:
                center -= 2 if english_speech else 3
            following = active[local_index + 1] if local_index + 1 < len(active) else None
            if following is not None and moras[following].stressed and moras[following].word_index == mora.word_index:
                center += 5 if english_speech else 7
            if mora.word_end and not mora.phrase_end:
                center -= 2 if english_speech else 3
            values[index] = int(round(center))

        for index in range(start, end + 1):
            if moras[index].role == "vowel-exit":
                previous = next((value for value in reversed(active) if value < index), None)
                following = next((value for value in active if value > index), None)
                adjacent = [values[value] for value in (previous, following) if value is not None]
                values[index] = round(sum(adjacent) / len(adjacent)) if adjacent else 0

        previous_nucleus = next(
            (index for index in range(terminal - 1, start - 1, -1) if moras[index].role == "nucleus"),
            None,
        )
        if ending == "?":
            if previous_nucleus is not None:
                values[previous_nucleus] = max(values[previous_nucleus], 14 if english_speech else 24)
            values[terminal] = max(values[terminal], 38 if english_speech else 64)
        elif ending in {".", "\n"}:
            if previous_nucleus is not None:
                values[previous_nucleus] = min(values[previous_nucleus], -8 if english_speech else -14)
            values[terminal] = min(values[terminal], -24 if english_speech else -42)
        elif ending == "!":
            if previous_nucleus is not None:
                values[previous_nucleus] = max(values[previous_nucleus], 15 if english_speech else 24)
            values[terminal] = min(values[terminal], -12 if english_speech else -18)
        elif not ending:
            values[terminal] = min(values[terminal], -16 if english_speech else -28)

        for index in range(terminal + 1, end + 1):
            values[index] = int(round(values[terminal] * 0.82))

    limit = 52 if any(mora.profile == "english-cvvc" for mora in moras) else 84
    return [max(-limit, min(limit, value)) for value in values]


def _duration_for(moras: list[Mora], index: int) -> int:
    mora = moras[index]
    duration = float(mora.duration_ms)
    english_speech = mora.profile == "english-cvvc"
    if mora.role == "epenthetic":
        duration *= 0.90
    elif mora.role in {"transition", "vowel-exit"}:
        duration *= 0.84 if english_speech else 0.92
    elif mora.role == "cluster":
        duration *= 0.82 if english_speech else 0.90
    elif mora.role == "coda":
        duration *= 0.84 if english_speech else 0.96
    elif mora.role == "glide":
        duration *= 0.82 if english_speech else 0.94
    elif mora.role == "nasal":
        duration *= 0.88 if english_speech else 0.98
    elif english_speech:
        # The phonemizer already seeds lexical timing from the PT-BR syllable.
        # Only subtle phrase-level adjustment belongs here; applying the older
        # UTAU stress multipliers again creates the mechanical ta-ta-ta cadence.
        if mora.stressed:
            duration *= 1.05
        elif mora.deaccented:
            duration *= 0.94
        elif mora.word_syllables > 1:
            duration *= 0.98
    elif mora.stressed:
        duration *= 1.14
    elif mora.deaccented:
        duration *= 0.88
    elif mora.word_moras > 1:
        duration *= 0.90
    following = next((value for value in range(index + 1, len(moras)) if moras[value].role != "vowel-exit"), None)
    if mora.role == "nucleus" and following is not None and moras[following].stressed and moras[following].word_index == mora.word_index:
        duration *= 0.97 if english_speech else 0.96
    if mora.phrase_end in {".", "!", "?", "\n"}:
        duration *= 1.06 if english_speech else 1.08
    elif mora.phrase_end in {",", ";", ":"}:
        duration *= 1.015 if english_speech else 1.02
    elif mora.word_end and mora.role == "nucleus":
        duration *= 1.01 if english_speech else 1.015
    minimum = 30 if mora.role == "epenthetic" else 30 if mora.role in {"transition", "vowel-exit", "cluster"} else 32 if mora.role in {"coda", "glide"} else 42 if mora.role == "nasal" else 68
    maximum = 76 if mora.role == "epenthetic" else 78 if mora.role in {"transition", "vowel-exit", "cluster"} else 84 if mora.role in {"coda", "glide", "nasal"} else 220
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
    moras: list[Mora], *, base_pitch: str = "C4", speech_rate: float = 1.0, tempo: int = 140,
    pitch_offset_semitones: float = 0.0,
) -> list[RenderNote]:
    if not moras:
        return []
    rate = _speech_rate(speech_rate)
    tempo_value = _tempo(tempo)
    try:
        pitch_offset = float(pitch_offset_semitones)
    except (TypeError, ValueError):
        pitch_offset = 0.0
    if not math.isfinite(pitch_offset):
        pitch_offset = 0.0
    pitch_offset = max(-4.0, min(4.0, round(pitch_offset * 2.0) / 2.0))
    coarse_offset = math.trunc(pitch_offset)
    residual_cents = int(round((pitch_offset - coarse_offset) * 100.0))
    coarse_pitch = _relative_pitch(base_pitch, coarse_offset)
    centers = _centers(moras)
    terminal_endings = _phrase_terminal_nuclei(moras)
    notes: list[RenderNote] = []

    for index, mora in enumerate(moras):
        raw_duration = round(_duration_for(moras, index) / rate)
        minimum = 32 if mora.role == "epenthetic" else 34 if mora.role in {"transition", "vowel-exit", "cluster"} else 36 if mora.role in {"coda", "glide"} else 44 if mora.role == "nasal" else 70
        duration = max(minimum, min(500, raw_duration))
        pause = max(0, min(1000, round(mora.pause_after_ms / rate)))
        center = centers[index]
        previous_index = next((value for value in range(index - 1, -1, -1) if moras[value].role != "vowel-exit"), None)
        following_index = next((value for value in range(index + 1, len(moras)) if moras[value].role != "vowel-exit"), None)
        previous = centers[previous_index] if previous_index is not None and not moras[previous_index].phrase_end else center
        following = centers[following_index] if following_index is not None and not mora.phrase_end else center
        start = round((previous + center) / 2)
        end = round((center + following) / 2)
        terminal_ending = terminal_endings.get(index, "")
        if mora.profile == "english-cvvc":
            peak = center + (0 if mora.role != "nucleus" else (7 if mora.stressed else (1 if mora.deaccented else 2)))
            if terminal_ending == "?":
                peak = max(peak, 42)
        else:
            peak = center + (0 if mora.role != "nucleus" else (12 if mora.stressed else (1 if mora.deaccented else 3)))
            if terminal_ending == "?":
                peak = max(peak, 68)
        if terminal_ending in {".", "\n"}:
            peak = min(peak, center + 1)
        continuous_cvvc_aux = (
            mora.profile == "english-cvvc"
            and mora.role != "nucleus"
            and mora.coverage in {"cvvc-transition", "cluster-hit", "approximation"}
        )
        if continuous_cvvc_aux:
            # Every fragment samples the same phrase envelope. 4D kept CVVC
            # auxiliaries at neutral AA, which introduced a small pitch reset at
            # every consonantal boundary even though the PCM timeline overlapped.
            pitchbend = _pitch_line(
                start + residual_cents,
                end + residual_cents,
                duration_ms=duration,
                tempo=tempo_value,
            )
            cap = {
                "transition": 44, "vowel-exit": 44, "cluster": 42, "coda": 46,
                "glide": 44, "nasal": 52,
            }.get(mora.role, 46)
            duration = min(duration, cap)
        else:
            pitchbend = _pitch_curve(
                start + residual_cents,
                peak + residual_cents,
                end + residual_cents,
                duration_ms=duration,
                tempo=tempo_value,
            )

        if mora.role == "epenthetic":
            gain = 0.76
        elif mora.role in {"transition", "vowel-exit"}:
            gain = 0.89 if continuous_cvvc_aux else 0.92
        elif mora.role == "cluster":
            gain = 0.87 if continuous_cvvc_aux else 0.91
        elif mora.role == "coda":
            gain = 0.86 if continuous_cvvc_aux else 0.84
        elif mora.role == "glide":
            gain = 0.87 if continuous_cvvc_aux else 0.86
        elif mora.role == "nasal":
            gain = 0.89 if continuous_cvvc_aux else 0.90
        elif mora.profile == "english-cvvc" and mora.stressed:
            gain = 1.035
        elif mora.profile == "english-cvvc" and mora.deaccented:
            gain = 0.97
        elif mora.stressed:
            gain = 1.055
        elif mora.deaccented:
            gain = 0.955
        else:
            gain = 0.99 if mora.word_moras > 1 else 1.0
        if mora.phrase_end in {".", "?", "!", "\n"}:
            gain *= 0.985

        notes.append(RenderNote(
            candidates=mora.candidates,
            # Keep the coarse note stable and let the UTAU pitchbend carry the
            # speech contour. This prevents audible semitone stair-steps.
            pitch=coarse_pitch,
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
            profile=mora.profile,
            planner_cost=mora.planner_cost,
            syllable_index=mora.syllable_index,
            word_syllables=mora.word_syllables,
            pitch_start_cents=int(start + residual_cents),
            pitch_end_cents=int(end + residual_cents),
            pitch_peak_cents=int(peak + residual_cents),
        ))
    return notes
