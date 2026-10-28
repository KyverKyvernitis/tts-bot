"""A deterministic accompaniment for CDs which contain only a lead voice.

This is an execution overlay, not a transcription of the original MIDI. Source
streams and the complete lead are retained unchanged; authored multipart music
passes through without alterations.
"""
from __future__ import annotations

import math

from .arrangement import MAX_PART_NOTES, validate_arrangement


HARMONY_POLICY = "diatonic-third-v1"
HARMONY_PHRASE_MS = 1000
_SCALES = {"major": (0, 2, 4, 5, 7, 9, 11),
           "minor": (0, 2, 3, 5, 7, 8, 10)}
_PROFILES = {
    "major": (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88),
    "minor": (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17),
}


def _declared_key(source):
    """Use one uniform, initial signature; never flatten declared modulations."""
    tracks = source.get("tracks", [])
    if not isinstance(tracks, list):
        return None
    keys = set()
    for track in tracks:
        signatures = track.get("key_signatures", []) if isinstance(track, dict) else []
        if not isinstance(signatures, list):
            continue
        for signature in signatures:
            if not isinstance(signature, dict):
                return None
            accidental, minor = signature.get("accidentals"), signature.get("minor")
            tick = signature.get("tick")
            if (type(accidental) is not int or not -7 <= accidental <= 7
                    or type(minor) is not bool or type(tick) is not int or tick != 0):
                return None
            keys.add(((7 * accidental + (9 if minor else 0)) % 12,
                      "minor" if minor else "major"))
    return next(iter(keys)) if len(keys) == 1 else None


def _key(notes, source):
    declared = _declared_key(source)
    if declared is not None:
        return (*declared, "midi_key_signature")
    histogram = [0.0] * 12
    for note in notes:
        histogram[note["midi_note"] % 12] += note["duration_ms"]
    mean = sum(histogram) / 12
    centered = [number - mean for number in histogram]
    best = None
    for mode, profile in _PROFILES.items():
        profile_mean = sum(profile) / 12
        normalized = [number - profile_mean for number in profile]
        length = math.sqrt(sum(number * number for number in normalized))
        for tonic in range(12):
            score = sum(centered[pitch] * normalized[(pitch - tonic) % 12]
                        for pitch in range(12)) / length
            candidate = (score, mode == "major", -tonic, tonic, mode)
            if best is None or candidate > best:
                best = candidate
    return best[3], best[4], "duration_profile"


def _third(pitch, scale, previous):
    """Prefer a lower scale third; keep chromatic notes a consonant third apart."""
    direction = -1 if pitch >= 40 else 1
    candidates = [pitch + direction * interval for interval in (3, 4)
                  if 36 <= pitch + direction * interval <= 96]
    if pitch % 12 in scale:
        # The second neighbouring scale degree is the diatonic third. This
        # distinction matters when both candidate pitches belong to the scale.
        neighbours = [pitch + direction * distance for distance in range(1, 5)
                      if (pitch + direction * distance) % 12 in scale]
        target = neighbours[1] if len(neighbours) > 1 else None
        if target in candidates:
            return target
    return min(candidates, key=lambda value: (
        value % 12 not in scale,
        abs(value - previous) if previous is not None else 0,
        abs(value - pitch), value))


def _phrase_guides(notes):
    """Author short vowel phrases for this generated voice at whole-note edges.

    A new backing phrase can begin at an existing note onset. Unlike authored
    vocal ties, this new accompaniment has no continuity to override. Keeping
    each phrase near one second avoids a single native long-vowel transform
    holding up the first progressive mix window.
    """
    anchor = previous = None
    low = high = 0
    for note in notes:
        pitch = note["midi_note"]
        restart = (anchor is None
                   or note["start_ms"] - previous["start_ms"] - previous["duration_ms"] > 20
                   or note["start_ms"] + note["duration_ms"] - anchor > HARMONY_PHRASE_MS
                   or max(high, pitch) - min(low, pitch) > 40)
        note["lyric"] = "a" if restart else "~"
        if restart:
            anchor, low, high = note["start_ms"], pitch, pitch
        else:
            low, high = min(low, pitch), max(high, pitch)
        previous = note


def harmonize_monophonic(value: dict) -> dict:
    """Return a fresh validated arrangement, adding one voice only to mono CDs."""
    result = validate_arrangement(value)
    if len(result["parts"]) != 1:
        return result
    lead = result["parts"][0]
    notes = lead["notes"]
    tonic, mode, key_source = _key(notes, result.get("source", {}))
    scale = frozenset((tonic + degree) % 12 for degree in _SCALES[mode])
    capacity = MAX_PART_NOTES - len(notes)
    # Extremely dense inputs can fit the source limit but not a complete
    # second part. Spread the accompaniment through the whole song and expose
    # this reduction instead of rejecting an otherwise valid catalog entry.
    count = min(len(notes), capacity)
    if count < 1:
        return result
    indices = (range(len(notes)) if count == len(notes) else
               (index * len(notes) // count for index in range(count)))
    harmony, previous = [], None
    for index in indices:
        original = notes[index]
        pitch = _third(original["midi_note"], scale, previous)
        harmony.append({"midi_note": pitch, "start_ms": original["start_ms"],
                        "duration_ms": original["duration_ms"], "velocity": original["velocity"]})
        previous = pitch
    _phrase_guides(harmony)
    identity = "auto-harmony" if lead["id"] != "auto-harmony" else "auto-harmony-2"
    result["parts"].append({"id": identity, "role": "harmony",
                            "voice_id": lead.get("voice_id", "kasane-teto"),
                            "lyrics_source": "midi", "gain": .36, "pan": .18,
                            "notes": harmony})
    result["automatic_harmony"] = {
        "version": 1, "policy": HARMONY_POLICY, "generated": True,
        "tonic_midi_class": tonic, "mode": mode, "key_source": key_source,
        "source_part_id": lead["id"], "generated_part_id": identity,
        "phrase_target_ms": HARMONY_PHRASE_MS,
        "note_count": len(harmony), "omitted_notes": len(notes) - len(harmony),
    }
    selection = result.get("selection")
    if isinstance(selection, dict):
        selection["generated_harmonies"] = True
    return validate_arrangement(result)
