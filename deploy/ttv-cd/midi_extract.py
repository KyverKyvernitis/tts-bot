"""Extract a bounded singing *excerpt* from an ordinary MIDI arrangement.

No third-party MIDI package is required. ``extract_midi(path, ...)`` returns
``{"score": <worker v1 score>, "extraction": <provenance and musical details>}``.
Track and channel selectors are zero based (MIDI percussion is channel 9).
The score contains only the fields accepted by Phone Worker 1.11.41. Note
names, octaves, rests, selection warnings and hashes live in ``extraction``.

This is a documented upper-voice heuristic, not vocal transcription. It never
changes tempo or transposes notes. Polyphonic input always carries an ambiguous
vocal status. A bounded excerpt is explicitly distinguished from the complete
source arrangement; existing worker limits are not relaxed.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import unicodedata


ALGORITHM_VERSION = "upper-voice-v1"
MAX_MIDI_BYTES = 8 * 1024 * 1024
MAX_TRACKS = 64
MAX_EVENTS = 500_000
MAX_DURATION_MS = 60_000
MAX_NOTES = 128
MIN_NOTE_MS = 40
MIN_PITCH, MAX_PITCH = 36, 96


class MidiExtractionError(ValueError):
    """A permanent source/selection error; changing the MIDI permits a retry."""

    def __init__(self, message: str, *, code: str = "invalid_midi"):
        super().__init__(message)
        self.code = code
        self.permanent = True


@dataclass(frozen=True)
class _Note:
    start: int
    end: int
    pitch: int
    velocity: int
    channel: int
    serial: int


@dataclass
class _Track:
    index: int
    name: str
    notes: list[_Note]
    lyrics: dict[int, str]
    tempos: list[tuple[int, int]]
    programs: dict[int, int]
    end: int
    issues: dict[str, int]


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos

    def take(self, count: int) -> bytes:
        if count < 0 or count > self.remaining:
            raise MidiExtractionError("O arquivo MIDI está truncado.")
        start, self.pos = self.pos, self.pos + count
        return self.data[start:self.pos]

    def byte(self) -> int:
        return self.take(1)[0]

    def vlq(self) -> int:
        value = 0
        for _ in range(4):
            byte = self.byte()
            value = (value << 7) | (byte & 127)
            if not byte & 128:
                return value
        raise MidiExtractionError("O MIDI contém um VLQ inválido.")


def _text(data: bytes, *, lyric: bool = False) -> str:
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError:
        value = data.decode("cp1252", errors="replace")
    value = unicodedata.normalize("NFC", value)
    value = "".join(" " if char.isspace() else char for char in value
                    if char.isspace() or not unicodedata.category(char).startswith("C"))
    value = " ".join(value.split())
    return (value.lstrip("/\\") if lyric else value)[:128]


def _read_track(data: bytes, index: int, counter: list[int]) -> _Track:
    reader = _Reader(data)
    tick = 0
    name = ""
    status = None
    active: dict[tuple[int, int], deque] = defaultdict(deque)
    notes, tempos, lyrics, programs = [], [], {}, {}
    issues: dict[str, int] = defaultdict(int)
    ended = False

    def finish(key: tuple[int, int], end: int) -> None:
        start, velocity, serial = active[key].popleft()
        if end > start:
            notes.append(_Note(start, end, key[1], velocity, key[0], serial))
        else:
            issues["zero_length_notes"] += 1
        if not active[key]:
            del active[key]

    while reader.remaining:
        counter[0] += 1
        if counter[0] > MAX_EVENTS:
            raise MidiExtractionError("O MIDI ultrapassa 500.000 eventos.", code="midi_too_large")
        tick += reader.vlq()
        first = reader.byte()
        first_data = None
        if first < 128:
            if status is None:
                raise MidiExtractionError("O MIDI usa running status sem status anterior.")
            first_data = first
        else:
            status = first
        if 0x80 <= status <= 0xEF:
            kind, channel = status & 0xF0, status & 15
            count = 1 if kind in (0xC0, 0xD0) else 2
            values = [] if first_data is None else [first_data]
            values.extend(reader.take(count - len(values)))
            if any(value >= 128 for value in values):
                raise MidiExtractionError("O MIDI contém dados inválidos em evento de canal.")
            if kind == 0xC0:
                programs[channel] = values[0]
            elif kind == 0xB0:
                if values[0] == 64 and values[1] >= 64:
                    issues["sustain_pedal_on"] += 1
                elif values[0] in (120, 123):
                    for key in [key for key in active if key[0] == channel]:
                        while key in active:
                            finish(key, tick)
                    issues["channel_all_notes_off"] += 1
            elif kind in (0x80, 0x90):
                pitch, velocity = values
                key = (channel, pitch)
                if kind == 0x90 and velocity:
                    if active.get(key):
                        issues["same_pitch_overlaps_fifo"] += 1
                    active[key].append((tick, velocity, counter[0]))
                elif active.get(key):
                    finish(key, tick)
                else:
                    # Some exporters redundantly issue note-off/panic events.
                    # They do not invent a note or invalidate real note timing.
                    issues["unmatched_note_off"] += 1
            continue

        # Meta and SysEx events cancel channel running status in SMF.
        status_value, status = status, None
        if status_value == 0xFF:
            kind = reader.byte()
            if kind >= 128:
                raise MidiExtractionError("Tipo de metadado MIDI inválido.")
            payload = reader.take(reader.vlq())
            if kind == 0x2F:
                if payload or reader.remaining:
                    raise MidiExtractionError("Evento de fim de faixa MIDI inválido.")
                ended = True
                break
            if kind == 0x51:
                tempo = int.from_bytes(payload, "big")
                if len(payload) != 3 or tempo == 0:
                    raise MidiExtractionError("Andamento MIDI inválido.")
                tempos.append((tick, tempo))
            elif kind == 0x03:
                name = _text(payload)
            elif kind == 0x05:
                text = _text(payload, lyric=True)
                if text:
                    if tick in lyrics and lyrics[tick] != text:
                        issues["conflicting_lyrics"] += 1
                    else:
                        lyrics[tick] = text
        elif status_value in (0xF0, 0xF7):
            reader.take(reader.vlq())
        else:
            raise MidiExtractionError("Evento de sistema MIDI não suportado.")
    if not ended:
        raise MidiExtractionError("Uma faixa MIDI não possui evento de fim.")
    # Broken note closures are recoverable and visible in provenance. No note
    # is extended beyond the actual track end.
    for key in list(active):
        while key in active:
            finish(key, tick)
            issues["unclosed_notes_at_track_end"] += 1
    notes.sort(key=lambda n: (n.start, n.pitch, n.end, n.serial))
    return _Track(index, name, notes, lyrics, tempos, programs, tick, dict(issues))


class _Timeline:
    def __init__(self, tracks: list[_Track], division: int):
        self.division = division
        self.conductor = next((track.index for track in tracks if track.tempos), None)
        self.ticks, self.elapsed, self.tempos = [0], [0], [500_000]
        self.conflicting_tracks = []
        if division & 0x8000:
            frames = 256 - (division >> 8)
            subframes = division & 255
            if frames not in (24, 25, 29, 30) or not subframes:
                raise MidiExtractionError("Divisão SMPTE MIDI inválida.")
            self.smpte = (30_000 if frames == 29 else frames, 1001 if frames == 29 else 1, subframes)
            return
        self.smpte = None
        if not division:
            raise MidiExtractionError("A resolução PPQ deve ser maior que zero.")
        changes = dict(tracks[self.conductor].tempos) if self.conductor is not None else {}
        conductor_ticks = sorted(changes)
        def effective_tempo(tick: int) -> int:
            index = bisect_right(conductor_ticks, tick) - 1
            return changes[conductor_ticks[index]] if index >= 0 else 500_000
        for track in tracks:
            if track.index != self.conductor and track.tempos:
                if any(effective_tempo(tick) != tempo for tick, tempo in track.tempos):
                    self.conflicting_tracks.append(track.index)
        for tick, tempo in sorted(changes.items()):
            if tick == self.ticks[-1]:
                self.tempos[-1] = tempo
            else:
                self.elapsed.append(self.elapsed[-1] + (tick - self.ticks[-1]) * self.tempos[-1])
                self.ticks.append(tick)
                self.tempos.append(tempo)

    def ms(self, tick: int) -> int:
        if self.smpte:
            frames, frame_denominator, subframes = self.smpte
            numerator, denominator = tick * 1000 * frame_denominator, frames * subframes
        else:
            index = bisect_right(self.ticks, tick) - 1
            numerator = self.elapsed[index] + (tick - self.ticks[index]) * self.tempos[index]
            denominator = self.division * 1000
        return (numerator + denominator // 2) // denominator


def _read_midi(data: bytes) -> tuple[list[_Track], _Timeline]:
    if len(data) > MAX_MIDI_BYTES:
        raise MidiExtractionError("O MIDI ultrapassa 8 MiB.", code="midi_too_large")
    reader = _Reader(data)
    if reader.take(4) != b"MThd":
        raise MidiExtractionError("O arquivo não é um MIDI padrão (SMF).")
    size = int.from_bytes(reader.take(4), "big")
    if size < 6:
        raise MidiExtractionError("Cabeçalho MIDI incompleto.")
    header = reader.take(size)
    format_number, count, division = (int.from_bytes(header[i:i + 2], "big") for i in (0, 2, 4))
    if format_number not in (0, 1):
        raise MidiExtractionError("Use MIDI SMF 0 ou 1; SMF 2 possui linhas de tempo independentes.")
    if not 1 <= count <= MAX_TRACKS or (format_number == 0 and count != 1):
        raise MidiExtractionError("O MIDI deve possuir entre 1 e 64 faixas.")
    tracks, counter = [], [0]
    for index in range(count):
        if reader.take(4) != b"MTrk":
            raise MidiExtractionError("Marcador de faixa MIDI ausente.")
        size = int.from_bytes(reader.take(4), "big")
        tracks.append(_read_track(reader.take(size), index, counter))
    if reader.remaining:
        raise MidiExtractionError("Há dados extras após as faixas MIDI declaradas.")
    return tracks, _Timeline(tracks, division)


_VOICE_NAME = re.compile(r"\b(?:vocals?|voices?|voz|canto|singing|vocaloid)\b")
_MELODY_NAME = re.compile(r"\b(?:melody|melodia|lead)\b")


def _name_hints(name: str) -> tuple[bool, bool]:
    folded = unicodedata.normalize("NFKC", name.casefold())
    ascii_name = "".join(c for c in unicodedata.normalize("NFKD", folded) if not unicodedata.combining(c))
    voice = bool(_VOICE_NAME.search(ascii_name) or "ボーカル" in folded)
    melody = bool(_MELODY_NAME.search(ascii_name) or "メロディ" in folded)
    return voice, melody


def _monophonic(notes: list[_Note]) -> bool:
    return bool(notes) and all(left.end <= right.start for left, right in zip(notes, notes[1:]))


def _upper_voice(notes: list[_Note], timeline: _Timeline) -> tuple[list[_Note], dict]:
    """Use original onsets, retain upper notes, and clip only voice crossings.

    Simultaneous onsets choose the highest singable pitch. A lower onset while
    the chosen upper note is held is accompaniment unless the held note ends
    within 80 ms (legato). A higher onset can interrupt it at that exact onset.
    Held notes are never resumed or repeated after an interruption.
    """
    usable, out_of_range, too_short = [], 0, 0
    for note in notes:
        if not MIN_PITCH <= note.pitch <= MAX_PITCH:
            out_of_range += 1
        elif timeline.ms(note.end) - timeline.ms(note.start) < MIN_NOTE_MS:
            too_short += 1
        else:
            usable.append(note)
    result, grouped = [], defaultdict(list)
    for note in usable:
        grouped[note.start].append(note)
    clips = 0
    for onset, group in sorted(grouped.items()):
        chosen = max(group, key=lambda n: (n.pitch, n.velocity, n.end, -n.serial))
        if result and result[-1].end > onset:
            previous = result[-1]
            remaining = timeline.ms(previous.end) - timeline.ms(onset)
            if chosen.pitch < previous.pitch and remaining > 80:
                continue
            if timeline.ms(onset) - timeline.ms(previous.start) < MIN_NOTE_MS:
                # A near-simultaneous onset does not create a spurious micro-note.
                # Select its upper note at its actual onset instead.
                if chosen.pitch <= previous.pitch:
                    continue
                result.pop()
            else:
                result[-1] = replace(previous, end=onset)
                clips += 1
        result.append(chosen)
    return result, {"out_of_range_notes_dropped": out_of_range, "short_notes_dropped": too_short,
                    "voice_crossings_clipped": clips, "selected_line_note_count": len(result),
                    "polyphonic_notes_dropped": len(usable) - len(result)}


def _candidate(track: _Track, channel: int, tracks: list[_Track], timeline: _Timeline) -> dict:
    notes = [note for note in track.notes if note.channel == channel]
    line, reduction = _upper_voice(notes, timeline)
    onsets = {note.start for note in notes}
    sources = [track] + [other for other in tracks if not other.notes and other.lyrics
                         and set(other.lyrics).issubset(onsets)]
    lyrics = {}
    conflicts = any(source.issues.get("conflicting_lyrics") for source in sources)
    for source in sources:
        for tick, value in source.lyrics.items():
            if tick in onsets:
                if tick in lyrics and lyrics[tick] != value:
                    conflicts = True
                lyrics[tick] = value
    if conflicts:
        lyrics = {}
    voice_hint, melody_hint = _name_hints(track.name)
    mono = _monophonic(notes)
    lyric_matches = len(set(lyrics).intersection(note.start for note in line))
    # Semantic hints win over heuristics, including a named polyphonic voice.
    # The statistical fallback is visible as ambiguity in the report.
    pitch_median = sorted(n.pitch for n in line)[len(line) // 2] if line else 0
    ranking = (bool(lyric_matches), voice_hint, melody_hint, mono,
               min(len(line), 32), pitch_median, -track.index, -channel)
    return {"track": track, "channel": channel, "notes": notes, "line": line,
            "lyrics": lyrics, "lyric_conflicts": conflicts, "voice_hint": voice_hint,
            "melody_hint": melody_hint, "monophonic": mono, "lyric_matches": lyric_matches,
            "ranking": ranking, "reduction": reduction}


def _excerpt(line: list[_Note], timeline: _Timeline, start_ms: int | None,
             max_duration_ms: int, max_notes: int) -> tuple[list[_Note], int]:
    if not line:
        raise MidiExtractionError("Nenhuma nota utilizável entre C2 e C7 com duração de 40 ms.", code="no_melody")
    if start_ms is not None:
        # Notes intersecting either boundary are not time-stretched or silently
        # extended. The explicit offset preserves any rest before the first note.
        selected = [n for n in line if timeline.ms(n.start) >= start_ms
                    and timeline.ms(n.end) <= start_ms + max_duration_ms][:max_notes]
        if not selected:
            raise MidiExtractionError("O trecho solicitado não contém notas completas utilizáveis.", code="empty_excerpt")
        return selected, start_ms
    # Sliding window maximizes actual sounding time, then note count; equal
    # excerpts select the earliest start. O(n), with no rhythm compression.
    starts, ends = [timeline.ms(n.start) for n in line], [timeline.ms(n.end) for n in line]
    right, sounding = 0, 0
    best, best_range = (-1, -1), (0, 0)
    for left in range(len(line)):
        while right < len(line) and right - left < max_notes and ends[right] - starts[left] <= max_duration_ms:
            sounding += ends[right] - starts[right]
            right += 1
        candidate = (sounding, right - left)
        if candidate > best:
            best, best_range = candidate, (left, right)
        if right > left:
            sounding -= ends[left] - starts[left]
        else:
            right = left + 1
    left, right = best_range
    if right <= left:
        raise MidiExtractionError("As notas são maiores que a duração máxima do trecho.", code="empty_excerpt")
    return line[left:right], starts[left]


def extract_midi(path: str | Path, *, track: int | None = None, channel: int | None = None,
                 start_ms: int | None = None, max_duration_ms: int = MAX_DURATION_MS,
                 max_notes: int = MAX_NOTES) -> dict:
    """Return a worker-compatible score and reproducible extraction provenance.

    Automatic selection compares individual track/channel streams, ignoring
    percussion. Explicit selectors constrain that comparison. ``start_ms`` is
    an absolute source offset; without it, the most musical bounded window is
    selected. Output timing is relative to the reported ``excerpt_start_ms``.
    ``extraction.vocal_status`` is ``midi_indicated`` only for monophonic streams
    with unique lyric or vocal-name evidence, otherwise ``ambiguous``. It never claims
    a confirmed transcription. Omitted polyphonic notes and pedal use are
    reported; sustain-controller duration is deliberately not added to singing.
    """
    for name, value, low, high in (("track", track, 0, MAX_TRACKS - 1), ("channel", channel, 0, 15),
                                  ("max_duration_ms", max_duration_ms, MIN_NOTE_MS, MAX_DURATION_MS),
                                  ("max_notes", max_notes, 1, MAX_NOTES)):
        if value is None and name in ("track", "channel"):
            continue
        if type(value) is not int or not low <= value <= high:
            raise MidiExtractionError(f"{name} deve ser inteiro entre {low} e {high}.", code="invalid_selection")
    if start_ms is not None and (type(start_ms) is not int or start_ms < 0):
        raise MidiExtractionError("start_ms deve ser inteiro não negativo.", code="invalid_selection")
    path = Path(path)
    if path.stat().st_size > MAX_MIDI_BYTES:
        raise MidiExtractionError("O MIDI ultrapassa 8 MiB.", code="midi_too_large")
    data = path.read_bytes()
    tracks, timeline = _read_midi(data)
    if track is not None and track >= len(tracks):
        raise MidiExtractionError("A faixa escolhida não existe.", code="invalid_selection")
    if channel == 9:
        raise MidiExtractionError("O canal 9 contém percussão e não será cantado.", code="no_melody")
    candidates = [_candidate(item, chan, tracks, timeline) for item in tracks
                  if track is None or item.index == track
                  for chan in sorted({n.channel for n in item.notes} - {9})
                  if channel is None or chan == channel]
    candidates = [candidate for candidate in candidates if candidate["line"]]
    if not candidates:
        raise MidiExtractionError("Nenhuma linha melódica utilizável para a seleção.", code="no_melody")
    selected = max(candidates, key=lambda candidate: candidate["ranking"])
    selected_track = selected["track"]
    line = selected["line"]
    excerpt, base = _excerpt(line, timeline, start_ms, max_duration_ms, max_notes)
    notes, annotations, rests = [], [], []
    embedded = selected["lyrics"]
    surviving_lyrics = {n.start: embedded[n.start] for n in excerpt if n.start in embedded}
    seen_lyric = False
    previous_end = 0
    original_notes = {note.serial: note for note in selected["notes"]}
    for index, note in enumerate(excerpt):
        start, end = timeline.ms(note.start) - base, timeline.ms(note.end) - base
        if start > previous_end:
            rests.append({"start_ms": previous_end, "duration_ms": start - previous_end})
        lyric = surviving_lyrics.get(note.start)
        notes.append({"midi_note": note.pitch, "start_ms": start, "duration_ms": end - start,
                      "velocity": note.velocity, "lyric": lyric if lyric is not None else "~" if seen_lyric else "a"})
        annotations.append({"index": index, "note": ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")[note.pitch % 12],
                            "octave": note.pitch // 12 - 1, "source_start_ms": timeline.ms(note.start),
                            "source_end_ms": timeline.ms(original_notes[note.serial].end),
                            "retained_end_ms": timeline.ms(note.end)})
        seen_lyric = seen_lyric or lyric is not None
        previous_end = end
    score = {"version": 1, "track_index": selected_track.index, "duration_ms": previous_end,
             "lyrics_source": "midi" if surviving_lyrics else "vowel", "notes": notes}
    warnings = []
    semantic_peers = [candidate for candidate in candidates
                      if candidate["ranking"][:3] == selected["ranking"][:3]]
    ambiguous_selection = len(semantic_peers) > 1
    indicated = (selected["monophonic"] and not ambiguous_selection
                 and (selected["voice_hint"] or bool(selected["lyric_matches"])))
    if not indicated:
        warnings.append("vocal_line_ambiguous")
    if ambiguous_selection:
        warnings.append("multiple_streams_share_selection_evidence")
    if not selected["monophonic"]:
        warnings.append("polyphonic_upper_voice_heuristic")
    if selected_track.issues.get("sustain_pedal_on"):
        warnings.append("sustain_pedal_ignored_key_durations_preserved")
    if selected["lyric_conflicts"] or selected_track.issues.get("conflicting_lyrics"):
        warnings.append("conflicting_embedded_lyrics_ignored")
    if selected_track.issues.get("unclosed_notes_at_track_end"):
        warnings.append("unclosed_notes_closed_at_track_end")
    if timeline.conflicting_tracks:
        warnings.append("conflicting_nonconductor_tempos_ignored")
    stream_duration_ms = timeline.ms(selected_track.end)
    partial = len(excerpt) != len(line) or base > 0 or base + previous_end < stream_duration_ms
    reasons = []
    if len(line) > max_notes:
        reasons.append("worker_note_limit")
    if timeline.ms(line[-1].end) - timeline.ms(line[0].start) > max_duration_ms:
        reasons.append("worker_duration_limit")
    if start_ms is not None:
        reasons.append("requested_offset")
    if len(excerpt) == len(line) and (base > 0 or base + previous_end < stream_duration_ms):
        reasons.append("source_silence_trimmed")
    score_bytes = json.dumps(score, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    extraction = {"version": 1, "algorithm": ALGORITHM_VERSION, "source_sha256": hashlib.sha256(data).hexdigest(),
                  "score_sha256": hashlib.sha256(score_bytes).hexdigest(), "track_index": selected_track.index,
                  "track_name": selected_track.name, "channel": selected["channel"],
                  "program": selected_track.programs.get(selected["channel"]),
                  "program_scope": "last_program_in_source_stream",
                  "selection": "explicit" if track is not None or channel is not None else "automatic",
                  "selection_ambiguous": ambiguous_selection,
                  "selection_criteria": ["aligned_lyrics", "vocal_name", "melody_name", "monophony", "usable_note_count", "median_pitch"],
                  "vocal_status": "midi_indicated" if indicated else "ambiguous", "vocal_transcription_confirmed": False,
                  "source_duration_ms": max(timeline.ms(item.end) for item in tracks),
                  "source_note_count": sum(len(item.notes) for item in tracks),
                  "selected_stream_note_count": len(selected["notes"]), "selected_stream_monophonic": selected["monophonic"],
                  "selected_stream_duration_ms": stream_duration_ms,
                  "excerpt_start_ms": base, "excerpt_end_ms": base + previous_end, "is_excerpt": partial,
                  "excerpt_reasons": reasons, "tempo_conductor_track": timeline.conductor,
                  "timing": "smpte" if timeline.smpte else "global_ppq_tempo_map", "tempo_changed": False,
                  "warnings": warnings, "parser_conditions": selected_track.issues,
                  "reduction": selected["reduction"], "note_names": annotations, "rests": rests,
                  "candidates": [{"track_index": c["track"].index, "track_name": c["track"].name,
                                  "channel": c["channel"], "note_count": len(c["notes"]),
                                  "line_note_count": len(c["line"]), "monophonic": c["monophonic"],
                                  "lyric_matches": c["lyric_matches"], "vocal_name": c["voice_hint"],
                                  "melody_name": c["melody_hint"]} for c in candidates]}
    return {"score": score, "extraction": extraction}
