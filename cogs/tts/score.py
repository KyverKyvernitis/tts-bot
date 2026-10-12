"""Bounded Standard MIDI File reader for a single TTV singing melody.

This module deliberately has no MIDI or audio dependencies. It preserves the
selected track's absolute timing (including rests), and hands lyric alignment
from message text to the worker. It never merges accompaniment into the voice.
Track indices in this API and its error messages are zero based.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import re
import unicodedata


MAX_MIDI_BYTES = 512 * 1024
MAX_TRACKS = 64
MAX_EVENTS = 50_000
MAX_NOTES = 128
MAX_DURATION_MS = 60_000
MIN_NOTE_MS = 40
MIN_MIDI_NOTE = 36
MAX_MIDI_NOTE = 96
MAX_LYRIC_CHARACTERS = 128


class ScoreError(ValueError):
    """Invalid or ambiguous MIDI; ``tracks`` can populate a track selector."""

    def __init__(self, message: str, *, code: str = "invalid_midi", tracks: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.tracks = list(tracks or [])


@dataclass(frozen=True)
class _Note:
    start: int
    end: int
    pitch: int
    velocity: int


@dataclass
class _Track:
    index: int
    name: str
    notes: list[_Note]
    tempos: list[tuple[int, int]]
    lyrics: dict[int, str]
    end: int

    @property
    def monophonic(self) -> bool:
        return bool(self.notes) and all(
            left.end <= right.start for left, right in zip(self.notes, self.notes[1:])
        )


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.position = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.position

    def take(self, size: int) -> bytes:
        if size < 0 or size > self.remaining:
            raise ScoreError("O arquivo MIDI está truncado.")
        start = self.position
        self.position += size
        return self.data[start:self.position]

    def byte(self) -> int:
        return self.take(1)[0]

    def vlq(self) -> int:
        value = 0
        for _ in range(4):
            part = self.byte()
            value = (value << 7) | (part & 0x7F)
            if not part & 0x80:
                return value
        raise ScoreError("O MIDI contém um número VLQ inválido.")


def _text_metadata(payload: bytes, *, lyric: bool) -> str:
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError:
        try:
            value = payload.decode("cp1252")
        except UnicodeDecodeError as error:
            raise ScoreError("O MIDI contém texto com codificação inválida.") from error
    value = unicodedata.normalize("NFC", value)
    value = "".join(
        " " if char.isspace() else char
        for char in value
        if char.isspace() or not unicodedata.category(char).startswith("C")
    )
    value = " ".join(value.split())
    if lyric:
        # Karaoke line/page markers do not belong to the sung syllable.
        value = value.lstrip("/\\").strip()
    if len(value) > MAX_LYRIC_CHARACTERS:
        label = "Uma sílaba da letra" if lyric else "O nome de uma faixa"
        raise ScoreError(f"{label} MIDI ultrapassa 128 caracteres.")
    return value


def _read_track(data: bytes, index: int, counter: list[int]) -> _Track:
    reader = _Reader(data)
    tick = 0
    name = ""
    running_status: int | None = None
    active: dict[tuple[int, int], tuple[int, int]] = {}
    notes: list[_Note] = []
    tempos: list[tuple[int, int]] = []
    lyrics: dict[int, str] = {}
    ended = False

    while reader.remaining:
        counter[0] += 1
        if counter[0] > MAX_EVENTS:
            raise ScoreError("O MIDI ultrapassa o limite de 50.000 eventos.")
        tick += reader.vlq()
        first = reader.byte()
        initial_data: int | None = None
        if first < 0x80:
            if running_status is None:
                raise ScoreError("O MIDI usa running status sem um evento de canal anterior.")
            status = running_status
            initial_data = first
        else:
            status = first

        if 0x80 <= status <= 0xEF:
            running_status = status
            kind = status & 0xF0
            channel = status & 0x0F
            count = 1 if kind in (0xC0, 0xD0) else 2
            values = [initial_data] if initial_data is not None else []
            values.extend(reader.take(count - len(values)))
            if any(value >= 0x80 for value in values):
                raise ScoreError("O MIDI contém dados inválidos em um evento de canal.")
            if kind not in (0x80, 0x90):
                continue
            pitch, velocity = values
            key = (channel, pitch)
            if kind == 0x90 and velocity:
                if key in active:
                    raise ScoreError("O MIDI repete uma nota antes de encerrá-la.")
                active[key] = (tick, velocity)
            else:
                if key not in active:
                    raise ScoreError("O MIDI encerra uma nota que não foi iniciada.")
                start, onset_velocity = active.pop(key)
                if tick <= start:
                    raise ScoreError("O MIDI contém uma nota sem duração positiva.")
                # Validate percussion events too, but never sing channel 10.
                if channel != 9:
                    notes.append(_Note(start, tick, pitch, onset_velocity))
            continue

        running_status = None
        if status == 0xFF:
            meta = reader.byte()
            if meta >= 0x80:
                raise ScoreError("O MIDI contém um tipo de metadado inválido.")
            payload = reader.take(reader.vlq())
            if meta == 0x2F:
                if payload or reader.remaining:
                    raise ScoreError("O evento de fim da faixa MIDI é inválido.")
                ended = True
                break
            if meta == 0x51:
                if len(payload) != 3 or int.from_bytes(payload, "big") == 0:
                    raise ScoreError("O MIDI contém um andamento inválido.")
                tempos.append((tick, int.from_bytes(payload, "big")))
            elif meta == 0x03:
                name = _text_metadata(payload, lyric=False)
            elif meta == 0x05:
                value = _text_metadata(payload, lyric=True)
                if value:
                    if tick in lyrics:
                        raise ScoreError("O MIDI contém mais de uma letra na mesma posição.")
                    lyrics[tick] = value
        elif status in (0xF0, 0xF7):
            reader.take(reader.vlq())
        else:
            raise ScoreError("O MIDI contém um evento de sistema não suportado.")

    if not ended:
        raise ScoreError("Uma faixa MIDI não possui o evento de fim da faixa.")
    if active:
        raise ScoreError("O MIDI contém notas iniciadas sem encerramento.")
    notes.sort(key=lambda note: (note.start, note.end, note.pitch))
    return _Track(index, name, notes, tempos, lyrics, tick)


class _Timeline:
    def __init__(self, tracks: list[_Track], division: int):
        self.division = division
        # SMF 1 uses a global conductor. If an exporter places tempo elsewhere,
        # the first track containing tempo is its conductor for the whole file.
        # The last conductor event at a repeated tick wins deterministically.
        conductor = next((track for track in tracks if track.tempos), None)
        changes = sorted(dict(conductor.tempos).items()) if conductor else []
        self.ticks = [0]
        self.numerators = [0]
        self.tempos = [500_000]
        for tick, tempo in changes:
            if tick == self.ticks[-1]:
                self.tempos[-1] = tempo
            else:
                elapsed = self.numerators[-1] + (tick - self.ticks[-1]) * self.tempos[-1]
                self.ticks.append(tick)
                self.numerators.append(elapsed)
                self.tempos.append(tempo)

    def numerator(self, tick: int) -> int:
        index = bisect_right(self.ticks, tick) - 1
        return self.numerators[index] + (tick - self.ticks[index]) * self.tempos[index]

    def milliseconds(self, tick: int) -> int:
        denominator = self.division * 1000
        return (self.numerator(tick) + denominator // 2) // denominator


def _read_midi(data: bytes) -> tuple[list[_Track], _Timeline]:
    if not isinstance(data, bytes):
        raise ScoreError("O conteúdo MIDI deve ser enviado como bytes.")
    if len(data) > MAX_MIDI_BYTES:
        raise ScoreError("O arquivo MIDI ultrapassa o limite de 512 KiB.")
    reader = _Reader(data)
    if reader.take(4) != b"MThd":
        raise ScoreError("O anexo não é um arquivo MIDI padrão (SMF).")
    header_size = int.from_bytes(reader.take(4), "big")
    if header_size < 6:
        raise ScoreError("O cabeçalho MIDI está incompleto.")
    header = reader.take(header_size)
    format_number = int.from_bytes(header[:2], "big")
    track_count = int.from_bytes(header[2:4], "big")
    division = int.from_bytes(header[4:6], "big")
    if format_number not in (0, 1):
        raise ScoreError("Use um MIDI do formato 0 ou 1; o formato 2 não é suportado.")
    if not 1 <= track_count <= MAX_TRACKS or (format_number == 0 and track_count != 1):
        raise ScoreError("O MIDI deve conter entre 1 e 64 faixas (uma no formato 0).")
    if division & 0x8000:
        raise ScoreError("MIDI com divisão SMPTE não é suportado; exporte em PPQ.")
    if not division:
        raise ScoreError("A resolução PPQ do MIDI deve ser maior que zero.")
    tracks: list[_Track] = []
    counter = [0]
    for index in range(track_count):
        if reader.take(4) != b"MTrk":
            raise ScoreError("Uma faixa MIDI não possui o marcador MTrk.")
        size = int.from_bytes(reader.take(4), "big")
        tracks.append(_read_track(reader.take(size), index, counter))
    if reader.remaining:
        raise ScoreError("O MIDI contém dados extras depois das faixas declaradas.")
    return tracks, _Timeline(tracks, division)


_MELODY_NAME = re.compile(r"\b(?:vocals?|voices?|melody|melodia|lead|voz|canto|singing|vocaloid)\b")


def _named_melody(track: _Track) -> bool:
    folded = unicodedata.normalize("NFKC", track.name.casefold())
    normalized = "".join(
        char for char in unicodedata.normalize("NFKD", folded)
        if not unicodedata.combining(char)
    )
    return bool(_MELODY_NAME.search(normalized) or "ボーカル" in folded or "メロディ" in folded)


def _lyric_matches(track: _Track, source: _Track) -> int:
    onsets = {note.start for note in track.notes}
    return len(onsets.intersection(source.lyrics))


def _external_lyric_sources(track: _Track, tracks: list[_Track]) -> list[_Track]:
    # Lyric-only karaoke tracks can describe another track. A second melody's
    # lyrics must never be silently reassigned to an accompaniment track.
    onsets = {note.start for note in track.notes}
    return [
        source for source in tracks
        if not source.notes and source.lyrics and set(source.lyrics).issubset(onsets)
    ]


def _track_details(track: _Track, tracks: list[_Track], timeline: _Timeline) -> dict:
    external = _external_lyric_sources(track, tracks)
    return {
        "index": track.index,
        "name": track.name,
        "note_count": len(track.notes),
        "monophonic": track.monophonic,
        "duration_ms": timeline.milliseconds(track.end),
        "lyric_matches": max([_lyric_matches(track, track)] + [len(source.lyrics) for source in external]),
    }


def _choose_track(tracks: list[_Track], timeline: _Timeline, track_index: int | None) -> _Track:
    if track_index is not None:
        if isinstance(track_index, bool) or not isinstance(track_index, int) or not 0 <= track_index < len(tracks):
            raise ScoreError("O índice da faixa MIDI é inválido; os índices começam em 0.")
        selected = tracks[track_index]
        if not selected.notes:
            raise ScoreError(f"A faixa {track_index} não contém notas melódicas; a percussão é ignorada.")
        if not selected.monophonic:
            raise ScoreError(f"A faixa {track_index} tem notas sobrepostas. Exporte a melodia monofônica separada.")
        return selected

    candidates = [track for track in tracks if track.monophonic]
    direct = [track for track in candidates if _lyric_matches(track, track)]
    if len(direct) == 1:
        return direct[0]
    if not direct:
        external = [track for track in candidates if _external_lyric_sources(track, tracks)]
        if len(external) == 1:
            return external[0]
    named = [track for track in (direct or candidates) if _named_melody(track)]
    if len(named) == 1:
        return named[0]

    # A polyphonic track explicitly labelled as the voice is actionable, rather
    # than a reason to choose the only remaining bass/accompaniment track.
    explicit_polyphony = [
        track for track in tracks
        if track.notes and not track.monophonic and (_named_melody(track) or _lyric_matches(track, track))
    ]
    if explicit_polyphony and not direct and not named:
        indices = ", ".join(str(track.index) for track in explicit_polyphony)
        raise ScoreError(f"A melodia nas faixas {indices} tem notas sobrepostas. Exporte a melodia monofônica separada.")
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ScoreError("O MIDI não possui uma faixa melódica monofônica; exporte a melodia separada.")
    details = [_track_details(track, tracks, timeline) for track in candidates]
    labels = "; ".join(
        f"{track.index}: {track.name or 'sem nome'} ({len(track.notes)} notas)" for track in candidates
    )
    raise ScoreError(
        f"O MIDI tem mais de uma melodia possível: {labels}. Escolha uma faixa pelo índice ou exporte só a melodia.",
        code="ambiguous_track", tracks=details,
    )


def inspect_midi_tracks(data: bytes) -> list[dict]:
    """Inspect tracks without guessing when several possible melodies exist.

    Each item has ``index``, ``name``, ``note_count`` (percussion excluded),
    ``monophonic``, ``duration_ms``, ``lyric_matches`` and ``recommended``.
    A recommendation identifies an unambiguous melody, not a validated score:
    :func:`parse_midi` also validates pitch, note count and musical durations.
    """
    tracks, timeline = _read_midi(data)
    try:
        recommended = _choose_track(tracks, timeline, None).index
    except ScoreError:
        recommended = None
    return [
        {**_track_details(track, tracks, timeline), "recommended": track.index == recommended}
        for track in tracks
    ]


def parse_midi(data: bytes, *, lyrics: str = "", track_index: int | None = None) -> dict:
    """Convert SMF 0/1 PPQ into the version-1 monophonic TTV score contract.

    Message ``lyrics`` override embedded lyric events and are syllabified by the
    worker; those score notes omit ``lyric``. Otherwise an embedded lyric must
    coincide with a note onset. Missing syllables after the first embedded lyric
    become ``~`` (continue its vowel), while a score without lyrics uses ``a``.
    Select an ambiguous track explicitly with its zero-based ``track_index``.
    """
    if not isinstance(lyrics, str):
        raise ScoreError("A letra enviada com o MIDI deve ser texto.")
    tracks, timeline = _read_midi(data)
    selected = _choose_track(tracks, timeline, track_index)
    if len(selected.notes) > MAX_NOTES:
        raise ScoreError("A melodia ultrapassa o limite de 128 notas.")
    denominator = timeline.division * 1000
    if timeline.numerator(selected.end) > MAX_DURATION_MS * denominator:
        raise ScoreError("A melodia ultrapassa o limite de 60 segundos, incluindo as pausas.")

    message_lyrics = bool(lyrics.strip())
    lyric_source: _Track | None = None
    if not message_lyrics:
        if selected.lyrics:
            lyric_source = selected
        else:
            sources = _external_lyric_sources(selected, tracks)
            if sources:
                lyric_maps = {tuple(sorted(source.lyrics.items())) for source in sources}
                if len(lyric_maps) > 1:
                    raise ScoreError("O MIDI contém letras diferentes para a mesma melodia; envie a letra na mensagem.")
                lyric_source = sources[0]
            elif any(not track.notes and track.lyrics for track in tracks):
                raise ScoreError("A letra MIDI não coincide com o início das notas; envie a letra na mensagem ou ajuste o arquivo.")
        if lyric_source:
            onsets = {note.start for note in selected.notes}
            if not set(lyric_source.lyrics).issubset(onsets):
                raise ScoreError("A letra MIDI não coincide com o início das notas; envie a letra na mensagem ou ajuste o arquivo.")
    source = "message" if message_lyrics else "midi" if lyric_source else "vowel"

    result_notes: list[dict] = []
    seen_lyric = False
    for note in selected.notes:
        if not MIN_MIDI_NOTE <= note.pitch <= MAX_MIDI_NOTE:
            raise ScoreError("A melodia deve usar notas MIDI entre 36 e 96 (C2 a C7).")
        if timeline.numerator(note.end) - timeline.numerator(note.start) < MIN_NOTE_MS * denominator:
            raise ScoreError("Cada nota da melodia precisa durar pelo menos 40 ms.")
        start_ms = timeline.milliseconds(note.start)
        end_ms = timeline.milliseconds(note.end)
        item = {
            "midi_note": note.pitch,
            "start_ms": start_ms,
            "duration_ms": end_ms - start_ms,
            "velocity": note.velocity,
        }
        if not message_lyrics:
            value = lyric_source.lyrics.get(note.start) if lyric_source else None
            item["lyric"] = value if value is not None else "~" if seen_lyric else "a"
            seen_lyric = seen_lyric or value is not None
        result_notes.append(item)
    return {
        "version": 1,
        "track_index": selected.index,
        "duration_ms": timeline.milliseconds(selected.end),
        "lyrics_source": source,
        "notes": result_notes,
    }
