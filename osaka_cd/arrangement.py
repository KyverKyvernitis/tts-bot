"""Versioned musical arrangements, independent of the worker wire protocol.

The complete arrangement remains on its original MIDI timeline. Worker 1.11.41
still receives ordinary monophonic v1 scores. This module validates the stored
arrangement and produces bounded requests whose offsets all refer to that one
timeline; it does not transpose, accelerate or synthesize additional harmony.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
import hashlib
import json
import math
import re

SCORE_SCHEMA = 2
MAX_ARRANGEMENT_MS = 600_000
MAX_PARTS = 3
MAX_PART_NOTES = 24_000
MAX_SOURCE_NOTES = 60_000
MAX_JSON_BYTES = 16 * 1024 * 1024
WORKER_DURATION_MS = 60_000
WORKER_NOTES = 128
MIN_NOTE_MS = 40
MAX_VOWEL_MS = 18_000
_ID = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class ArrangementError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_arrangement"):
        super().__init__(message)
        self.code = code
        self.permanent = True


def _integer(value, lower: int, upper: int, label: str) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ArrangementError(f"{label} deve ser inteiro entre {lower} e {upper}.")
    return value


def _canonical(value: dict) -> bytes:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ArrangementError("O arranjo contém valores JSON inválidos.") from exc
    if len(raw) > MAX_JSON_BYTES:
        raise ArrangementError("O arranjo ultrapassa 16 MiB.", code="arrangement_too_large")
    return raw


def validate_arrangement(value: dict) -> dict:
    """Return an independent canonical snapshot; reject malformed active parts.

    Original source streams may contain percussion, out-of-range pitches and
    notes too short to sing. They are provenance, never worker requests. The
    selected parts are strictly monophonic and bounded separately.
    """
    if not isinstance(value, dict) or type(value.get("score_schema")) is not int or value["score_schema"] != SCORE_SCHEMA:
        raise ArrangementError("Versão de partitura do arranjo não suportada.")
    duration = _integer(value.get("duration_ms"), MIN_NOTE_MS, MAX_ARRANGEMENT_MS, "duration_ms")
    parts = value.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        raise ArrangementError("O arranjo deve possuir entre uma e três partes.")
    identities, leads, note_count = set(), 0, 0
    for part in parts:
        if not isinstance(part, dict):
            raise ArrangementError("Parte musical inválida.")
        identity = part.get("id")
        if not isinstance(identity, str) or len(identity) > 64 or not _ID.fullmatch(identity) or identity in identities:
            raise ArrangementError("Identificador de parte musical inválido ou repetido.")
        identities.add(identity)
        if part.get("role") not in {"lead", "harmony", "bass"}:
            raise ArrangementError("Função de parte musical inválida.")
        leads += part["role"] == "lead"
        voice = part.get("voice_id", "kasane-teto")
        if not isinstance(voice, str) or len(voice) > 64 or not _ID.fullmatch(voice):
            raise ArrangementError("Identificador de voicebank inválido.")
        _integer(part.get("track_index", 0), 0, 63, "track_index")
        if "channel" in part:
            _integer(part["channel"], 0, 15, "channel")
            if part["channel"] == 9:
                raise ArrangementError("A percussão não pode ser enviada para canto.")
        if part.get("lyrics_source", "vowel") not in {"midi", "message", "vowel"}:
            raise ArrangementError("Origem de letra inválida.")
        notes = part.get("notes")
        if not isinstance(notes, list) or not 1 <= len(notes) <= MAX_PART_NOTES:
            raise ArrangementError("Quantidade de notas da parte inválida.")
        note_count += len(notes)
        end = 0
        for note in notes:
            if not isinstance(note, dict) or set(note) - {"midi_note", "start_ms", "duration_ms", "velocity", "lyric"}:
                raise ArrangementError("Nota de parte musical inválida.")
            _integer(note.get("midi_note"), 36, 96, "midi_note")
            start = _integer(note.get("start_ms"), 0, duration, "start_ms")
            length = _integer(note.get("duration_ms"), MIN_NOTE_MS, duration, "duration_ms")
            _integer(note.get("velocity"), 1, 127, "velocity")
            if start < end or start + length > duration:
                raise ArrangementError("Notas sobrepostas ou fora da duração da parte.")
            end = start + length
            if "lyric" in note and (not isinstance(note["lyric"], str) or len(note["lyric"]) > 128):
                raise ArrangementError("Letra de nota inválida.")
        if "source_note_ids" in part:
            ids = part["source_note_ids"]
            if not isinstance(ids, list) or len(ids) != len(notes) or any(type(i) is not int or i < 0 for i in ids):
                raise ArrangementError("Proveniência de notas não corresponde à parte.")
        confidence = part.get("confidence", {})
        if not isinstance(confidence, dict) or confidence.get("vocal_transcription_confirmed", False) is not False:
            raise ArrangementError("Extração automática não confirma uma transcrição vocal.")
        certainty = confidence.get("score", 0.0)
        if isinstance(certainty, bool) or not isinstance(certainty, (int, float)) or not math.isfinite(certainty) or not 0 <= certainty <= 1:
            raise ArrangementError("Confiança musical inválida.")
        for field, lower, upper in (("gain", 0.0, 2.0), ("pan", -1.0, 1.0)):
            if field in part:
                number = part[field]
                if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not lower <= number <= upper:
                    raise ArrangementError(f"{field} musical inválido.")
    if leads != 1 or note_count > MAX_PART_NOTES:
        raise ArrangementError("O arranjo exige uma melodia principal e até 24.000 notas cantáveis.")
    source = value.get("source", {})
    if not isinstance(source, dict):
        raise ArrangementError("Proveniência MIDI inválida.")
    if "sha256" in source and (not isinstance(source["sha256"], str) or not _SHA.fullmatch(source["sha256"])):
        raise ArrangementError("Hash do MIDI original inválido.")
    streams = source.get("streams", [])
    if not isinstance(streams, list) or len(streams) > 1024:
        raise ArrangementError("Fluxos MIDI originais inválidos.")
    if any(not isinstance(s, dict) or not isinstance(s.get("notes", []), list) for s in streams):
        raise ArrangementError("Fluxo MIDI original inválido.")
    source_count = sum(len(s.get("notes", [])) for s in streams)
    if source_count > MAX_SOURCE_NOTES:
        raise ArrangementError("O MIDI ultrapassa 60.000 notas originais.", code="arrangement_too_large")
    if "duration_ms" in source:
        _integer(source["duration_ms"], duration, MAX_ARRANGEMENT_MS, "source.duration_ms")
    source_duration = source.get("duration_ms", MAX_ARRANGEMENT_MS)
    source_ids = set()
    for stream in streams:
        _integer(stream.get("track_index"), 0, 63, "source.track_index")
        _integer(stream.get("channel"), 0, 15, "source.channel")
        for note in stream.get("notes", []):
            if not isinstance(note, dict):
                raise ArrangementError("Nota original MIDI inválida.")
            serial = _integer(note.get("id"), 0, 500_000, "source.note.id")
            if serial in source_ids:
                raise ArrangementError("Identificador de nota original repetido.")
            source_ids.add(serial)
            _integer(note.get("midi_note"), 0, 127, "source.midi_note")
            _integer(note.get("velocity"), 1, 127, "source.velocity")
            start = _integer(note.get("start_ms"), 0, source_duration, "source.start_ms")
            finish = _integer(note.get("key_end_ms"), start, source_duration, "source.key_end_ms")
            _integer(note.get("sustain_end_ms", finish), finish, source_duration, "source.sustain_end_ms")
            tick = _integer(note.get("start_tick"), 0, 2**63 - 1, "source.start_tick")
            _integer(note.get("end_tick"), tick, 2**63 - 1, "source.end_tick")
    if streams and "note_count" in source and source["note_count"] != source_count:
        raise ArrangementError("Contagem de notas originais não corresponde à proveniência.")
    if source_ids and any(i not in source_ids for p in parts for i in p.get("source_note_ids", [])):
        raise ArrangementError("Parte musical aponta para uma nota original inexistente.")
    warnings = value.get("warnings", [])
    if not isinstance(warnings, list) or len(warnings) > 128 or any(not isinstance(w, str) or len(w) > 256 for w in warnings):
        raise ArrangementError("Avisos do arranjo inválidos.")
    return json.loads(_canonical(value))


def arrangement_sha256(value: dict) -> str:
    return hashlib.sha256(_canonical(validate_arrangement(value))).hexdigest()


def arrangement_performance_sha256(value: dict) -> str:
    """Musical identity for stem/mix reuse, separate from source provenance."""
    arrangement = validate_arrangement(value)
    payload = {"score_schema": SCORE_SCHEMA, "duration_ms": arrangement["duration_ms"],
               "parts": [{"id": p["id"], "role": p["role"],
                          "voice_id": p.get("voice_id", "kasane-teto"),
                          "lyrics_source": p.get("lyrics_source", "vowel"), "notes": p["notes"],
                          "gain": p.get("gain", .85 if p["role"] == "lead" else .50),
                          "pan": p.get("pan", 0.0)}
                         for p in arrangement["parts"]]}
    return hashlib.sha256(_canonical(payload)).hexdigest()


def minimum_syllables(score: dict) -> int:
    """Exact lower bound for the existing UTAU aligner's contiguous spans."""
    notes = score.get("notes", [])
    if not notes:
        return 0
    count, first, previous = 1, notes[0], notes[0]
    low = high = first["midi_note"]
    for note in notes[1:]:
        pitch = note["midi_note"]
        if (note["start_ms"] - previous["start_ms"] - previous["duration_ms"] > 20
                or note["start_ms"] + note["duration_ms"] - first["start_ms"] > MAX_VOWEL_MS
                or max(high, pitch) - min(low, pitch) > 40):
            count += 1
            first, low, high = note, pitch, pitch
        else:
            low, high = min(low, pitch), max(high, pitch)
        previous = note
    return count


def vowel_guide_score(score: dict) -> dict:
    """Translate guide vowels into explicit MIDI attacks accepted by 1.11.41.

    Its legacy vowel mode only restarts at rests. Explicit attacks also respect
    the maximum continuous span and pitch range; no rest or tempo is added.
    """
    result = dict(score, lyrics_source="midi", notes=[dict(n) for n in score["notes"]])
    first = previous = result["notes"][0]
    low = high = first["midi_note"]
    first["lyric"] = "a"
    for note in result["notes"][1:]:
        pitch = note["midi_note"]
        attack = (note["start_ms"] - previous["start_ms"] - previous["duration_ms"] > 20
                  or note["start_ms"] + note["duration_ms"] - first["start_ms"] > MAX_VOWEL_MS
                  or max(high, pitch) - min(low, pitch) > 40)
        note["lyric"] = "a" if attack else "~"
        if attack:
            first, low, high = note, pitch, pitch
        else:
            low, high = min(low, pitch), max(high, pitch)
        previous = note
    return result


def _restart_midi_holds(score: dict) -> bool:
    """Keep an embedded syllable across limits the legacy worker cannot tie.

    This adds no notes or musical silence. Restarting the known syllable is
    disclosed to callers because the physical voice adds a consonant attack.
    """
    first = previous = score["notes"][0]
    low = high = first["midi_note"]
    anchor = str(first.get("lyric", "")).strip()
    restarted = False
    for note in score["notes"][1:]:
        pitch = note["midi_note"]
        lyric = str(note.get("lyric", "")).strip()
        is_hold = lyric in {"~", "-", "_", ""}
        after_rest = note["start_ms"] - previous["start_ms"] - previous["duration_ms"] > 20
        if is_hold and after_rest:
            # 1.11.41 already restarts the steady vowel after a real MIDI rest.
            # Preserve that behavior rather than adding the old consonant.
            first, low, high = note, pitch, pitch
        restart = (note["start_ms"] + note["duration_ms"] - first["start_ms"] > MAX_VOWEL_MS
                   or max(high, pitch) - min(low, pitch) > 40)
        if is_hold and restart:
            if anchor in {"~", "-", "_", ""}:
                raise ArrangementError("Sustentação MIDI sem uma sílaba anterior.",
                                       code="missing_midi_lyric_anchor")
            note["lyric"] = anchor
            restarted = True
            first, low, high = note, pitch, pitch
        elif not is_hold:
            anchor = lyric
            first, low, high = note, pitch, pitch
        else:
            low, high = min(low, pitch), max(high, pitch)
        previous = note
    return restarted


def _notes_for_window(part: dict, start: int, end: int, *, allow_clip: bool = True) -> tuple[list, list, list]:
    notes, indices, continuations = [], [], []
    original = part["notes"]
    ends = [n["start_ms"] + n["duration_ms"] for n in original]
    first = bisect_right(ends, start)
    for index in range(first, len(original)):
        note = original[index]
        if note["start_ms"] >= end:
            break
        note_end = note["start_ms"] + note["duration_ms"]
        left, right = max(start, note["start_ms"]), min(end, note_end)
        if right - left < MIN_NOTE_MS:
            continue
        clipped = left != note["start_ms"] or right != note_end
        if clipped and not allow_clip:
            continue
        item = dict(note)
        item["start_ms"], item["duration_ms"] = left - start, right - left
        notes.append(item)
        indices.append(index)
        continuations.append({"source_note_index": index,
                              "continues_from_previous": left > note["start_ms"],
                              "continues_in_next": right < note_end})
    return notes, indices, continuations


def _window_limit(parts: list[dict], start: int, end: int, max_notes: int) -> int:
    for part in parts:
        notes = part["notes"]
        endings = [n["start_ms"] + n["duration_ms"] for n in notes]
        first = bisect_right(endings, start)
        if first + max_notes < len(notes):
            end = min(end, notes[first + max_notes]["start_ms"])
    return end


def preview_arrangement(value: dict, *, start_ms: int | None = None,
                        end_ms: int | None = None, max_duration_ms: int = WORKER_DURATION_MS,
                        max_notes: int = WORKER_NOTES) -> dict:
    """One common bounded window, never independent offsets for each voice.

    Explicit start/end should use the legacy extractor's preview offsets. If
    additional parts exceed its budget, the common end is shortened for all of
    them. Crossing notes are clipped only at the declared excerpt boundaries;
    original source and full arrangement remain untouched.
    """
    arrangement = validate_arrangement(value)
    _integer(max_duration_ms, MIN_NOTE_MS, WORKER_DURATION_MS, "max_duration_ms")
    _integer(max_notes, 1, WORKER_NOTES, "max_notes")
    if start_ms is not None:
        _integer(start_ms, 0, arrangement["duration_ms"] - MIN_NOTE_MS, "start_ms")
    if end_ms is not None:
        _integer(end_ms, MIN_NOTE_MS, arrangement["duration_ms"], "end_ms")
    if start_ms is None:
        lead = next(p for p in arrangement["parts"] if p["role"] == "lead")
        offsets = [n["start_ms"] for n in lead["notes"]]
        # Bound automatic window search on exceptionally dense large sources.
        stride = max(1, (len(offsets) + 511) // 512)
        offsets = offsets[::stride]
        best, chosen = (-1, -1), (offsets[0], offsets[0] + MIN_NOTE_MS)
        for start in offsets:
            end = _window_limit(arrangement["parts"], start,
                                min(arrangement["duration_ms"], start + max_duration_ms), max_notes)
            if end - start < MIN_NOTE_MS:
                continue
            sounding, count = 0, 0
            for part in arrangement["parts"]:
                notes, _, _ = _notes_for_window(part, start, end)
                sounding += sum(n["duration_ms"] for n in notes) * (2 if part["role"] == "lead" else 1)
                count += len(notes)
            if (sounding, count) > best:
                best, chosen = (sounding, count), (start, end)
        start_ms, auto_end = chosen
        end_ms = min(auto_end, end_ms) if end_ms is not None else auto_end
    else:
        end_ms = min(end_ms if end_ms is not None else arrangement["duration_ms"], start_ms + max_duration_ms)
        end_ms = _window_limit(arrangement["parts"], start_ms, end_ms, max_notes)
    if end_ms - start_ms < MIN_NOTE_MS:
        raise ArrangementError("O trecho comum não contém notas utilizáveis.", code="empty_arrangement_preview")
    result = dict(arrangement)
    result["duration_ms"] = end_ms - start_ms
    result["parts"] = []
    clipped, omitted_boundary_notes = False, 0
    for part in arrangement["parts"]:
        # A sub-40ms edge fragment is omitted from a usable preview, but its
        # source note remains intact and the excerpt must disclose the cut.
        for original in part["notes"]:
            left, right = max(start_ms, original["start_ms"]), min(
                end_ms, original["start_ms"] + original["duration_ms"])
            if left < right and (left != original["start_ms"]
                                 or right != original["start_ms"] + original["duration_ms"]):
                clipped = True
                omitted_boundary_notes += right - left < MIN_NOTE_MS
        notes, indices, continuations = _notes_for_window(part, start_ms, end_ms)
        if not notes:
            continue
        selected = dict(part, notes=notes)
        if "source_note_ids" in part:
            selected["source_note_ids"] = [part["source_note_ids"][i] for i in indices]
        selected["source_note_indices"] = indices
        selected["continuations"] = continuations
        clipped |= any(c["continues_from_previous"] or c["continues_in_next"] for c in continuations)
        result["parts"].append(selected)
    if not any(p["role"] == "lead" for p in result["parts"]):
        raise ArrangementError("O trecho comum não contém a melodia principal.", code="empty_arrangement_preview")
    result["preview"] = {"start_ms": start_ms, "end_ms": end_ms,
                         "full_duration_ms": arrangement["duration_ms"],
                         "boundary_notes_clipped": clipped,
                         "boundary_notes_omitted": omitted_boundary_notes}
    if omitted_boundary_notes:
        result["warnings"] = list(result.get("warnings", [])) + ["preview_boundary_notes_omitted"]
    result = validate_arrangement(result)
    return {"arrangement": result, "start_ms": start_ms, "end_ms": end_ms,
            "is_excerpt": start_ms != 0 or end_ms != arrangement["duration_ms"],
            "boundary_notes_clipped": clipped}


def _safe_common_end(parts: list[dict], start: int, limit: int) -> int:
    """Prefer a phrase boundary that does not split any sounding source note."""
    boundaries = {limit}
    for part in parts:
        for note in part["notes"]:
            note_end = note["start_ms"] + note["duration_ms"]
            if start + MIN_NOTE_MS <= note_end <= limit:
                boundaries.add(note_end)
            if start + MIN_NOTE_MS <= note["start_ms"] <= limit:
                boundaries.add(note["start_ms"])
    # Avoid tiny requests merely because one voice has a sustained note.
    lower = max(start + MIN_NOTE_MS, start + (limit - start) // 2)
    for boundary in sorted(boundaries, reverse=True):
        if boundary < lower:
            break
        if all(not any(n["start_ms"] < boundary < n["start_ms"] + n["duration_ms"]
                       for n in part["notes"]) for part in parts):
            return boundary
    # A dense arrangement can have no global silence. Choose a deterministic
    # original onset/end avoiding sub-40ms fragments. Continuations are explicit
    # in the render plan so the mixer can preserve its continuous timeline.
    for boundary in sorted(boundaries, reverse=True):
        if boundary < lower:
            break
        usable = True
        for part in parts:
            for note in part["notes"]:
                note_end = note["start_ms"] + note["duration_ms"]
                if note["start_ms"] < boundary < note_end:
                    if boundary - max(start, note["start_ms"]) < MIN_NOTE_MS or note_end - boundary < MIN_NOTE_MS:
                        usable = False
                        break
            if not usable:
                break
        if usable:
            return boundary
    raise ArrangementError("Não foi possível dividir o arranjo sem perder notas curtas.", code="unsafe_chunk_boundary")


def arrangement_render_plan(value: dict, *, max_duration_ms: int = MAX_VOWEL_MS,
                            max_notes: int = WORKER_NOTES) -> dict:
    """Render every selected note through bounded v1 requests.

    A joint source window and explicit source-note indices let the caller place
    stems and partition text without changing musical time. Mid-note boundaries
    are exceptional and reported; physical rendering may add a new vowel attack
    there unless the caller supports the continuation marker. Notes longer than
    18 seconds fail explicitly rather than claiming faithful synthesis.
    """
    arrangement = validate_arrangement(value)
    _integer(max_duration_ms, MIN_NOTE_MS, WORKER_DURATION_MS, "max_duration_ms")
    _integer(max_notes, 1, WORKER_NOTES, "max_notes")
    parts = arrangement["parts"]
    if any(n["duration_ms"] > MAX_VOWEL_MS for p in parts for n in p["notes"]):
        raise ArrangementError("Uma nota original supera o limite de 18 segundos da voz; o MIDI foi preservado.",
                               code="note_exceeds_vowel_limit")
    chunks, cursor, warnings = [], 0, []
    while cursor < arrangement["duration_ms"]:
        if arrangement["duration_ms"] - cursor < MIN_NOTE_MS:
            chunks.append({"index": len(chunks), "start_ms": cursor,
                           "end_ms": arrangement["duration_ms"],
                           "duration_ms": arrangement["duration_ms"] - cursor, "parts": []})
            break
        limit = _window_limit(parts, cursor, min(cursor + max_duration_ms, arrangement["duration_ms"]), max_notes)
        if limit - cursor < MIN_NOTE_MS:
            raise ArrangementError("Não há espaço para um trecho musical válido.", code="unsafe_chunk_boundary")
        end = _safe_common_end(parts, cursor, limit)
        requests = []
        for part in parts:
            notes, indices, continuations = _notes_for_window(part, cursor, end)
            if not notes:
                continue
            score = {"version": 1, "track_index": part.get("track_index", 0),
                     "duration_ms": end - cursor,
                     "lyrics_source": part.get("lyrics_source", "vowel"), "notes": notes}
            carried_lyric = None
            if score["lyrics_source"] == "midi" and str(notes[0].get("lyric", "~")).strip() in {"~", "-", "_", ""}:
                carried_lyric = next((str(n.get("lyric", "")).strip()
                                      for n in reversed(part["notes"][:indices[0]])
                                      if str(n.get("lyric", "")).strip() not in {"~", "-", "_", ""}), None)
                if carried_lyric is None:
                    raise ArrangementError("A parte MIDI começa com sustentação sem uma sílaba anterior.",
                                           code="missing_midi_lyric_anchor")
                # Independent legacy UTAU requests cannot inherit a phoneme
                # state. Preserve its syllable and disclose the extra attack.
                score["notes"][0]["lyric"] = carried_lyric
                if "chunk_boundary_midi_syllable_reattacked" not in warnings:
                    warnings.append("chunk_boundary_midi_syllable_reattacked")
            if score["lyrics_source"] == "vowel":
                score = vowel_guide_score(score)
            elif score["lyrics_source"] == "midi" and _restart_midi_holds(score):
                if "midi_continuation_reattacked_for_worker_limit" not in warnings:
                    warnings.append("midi_continuation_reattacked_for_worker_limit")
            requests.append({"id": part["id"], "part_id": part["id"], "role": part["role"],
                             "voice_id": part.get("voice_id", "kasane-teto"), "score": score,
                             "minimum_syllables": minimum_syllables(score),
                             "source_note_indices": indices, "text_note_indices": indices,
                             "text_start_index": indices[0], "continuations": continuations,
                             "carried_lyric": carried_lyric, "lyrics_reattacked": carried_lyric is not None,
                             "gain": part.get("gain", 1.0), "pan": part.get("pan", 0.0)})
            if any(c["continues_from_previous"] or c["continues_in_next"] for c in continuations):
                if "chunk_boundary_continuation_requires_mixer_alignment" not in warnings:
                    warnings.append("chunk_boundary_continuation_requires_mixer_alignment")
        chunks.append({"index": len(chunks), "start_ms": cursor, "end_ms": end,
                       "duration_ms": end - cursor, "parts": requests})
        cursor = end
    return {"score_schema": SCORE_SCHEMA, "arrangement_sha256": arrangement_sha256(arrangement),
            "duration_ms": arrangement["duration_ms"], "chunks": chunks, "warnings": warnings,
            "timing": "one_original_timeline", "tempo_changed": False}


# Plain aliases for callers that already use a shorter vocabulary.
render_plan = arrangement_render_plan
arrangement_chunks = arrangement_render_plan
