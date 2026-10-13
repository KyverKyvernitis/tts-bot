"""Catálogo de canto do bot; a escolha vira uma partitura antes de entrar na fila."""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path



DEFAULT_MELODY_ID = "ttv-demo-v1"
_LOG = logging.getLogger(__name__)
_SCORE_FILE = Path(__file__).with_name("cd_song_scores.json")


@dataclass(frozen=True, slots=True)
class Melody:
    id: str
    name: str
    description: str
    duration_ms: int
    note_count: int
    thumbnail_url: str = ""
    score_json: str = (
        '{"version":1,"track_index":0,"duration_ms":4000,"lyrics_source":"message",'
        '"notes":[{"midi_note":60,"start_ms":0,"duration_ms":1000,"velocity":100},'
        '{"midi_note":64,"start_ms":1000,"duration_ms":1000,"velocity":100},'
        '{"midi_note":67,"start_ms":2000,"duration_ms":1000,"velocity":100},'
        '{"midi_note":64,"start_ms":3000,"duration_ms":1000,"velocity":100}]}'
    )
    revision: int = 1
    artist: str = ""
    source_url: str = ""


def _song_melodies() -> tuple[Melody, ...]:
    entries = json.loads(_SCORE_FILE.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or len(entries) > 500:
        raise ValueError("invalid_cd_catalog_size")
    seen = set()
    songs = []
    for entry in entries:
        identity = entry["id"]
        if (not isinstance(identity, str) or not identity or len(identity) > 90
                or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in identity)
                or identity in seen or identity == DEFAULT_MELODY_ID):
            raise ValueError("invalid_cd_catalog_id")
        seen.add(identity)
        notes = entry["notes"]
        if not isinstance(notes, list) or not 1 <= len(notes) <= 128:
            raise ValueError("invalid_cd_score_notes")
        if not 1 <= entry["duration_ms"] <= 60_000:
            raise ValueError("invalid_cd_score_duration")
        score = {"version": 1, "track_index": 0, "duration_ms": entry["duration_ms"],
                 "lyrics_source": "message", "notes": notes}
        songs.append(Melody(
            id=entry["id"], name=entry["name"],
            description=f"{entry['artist']} · {entry['excerpt']}",
            duration_ms=entry["duration_ms"], note_count=len(notes),
            score_json=json.dumps(score, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            revision=entry.get("revision", 1), artist=entry["artist"], source_url=entry["source_url"],
        ))
    return tuple(songs)


_MELODIES = (Melody(
    id=DEFAULT_MELODY_ID,
    name="Padrão",
    description="C4 · E4 · G4 · E4",
    duration_ms=4000,
    note_count=4,
), *_song_melodies())


try:
    _STAT = _SCORE_FILE.stat()
    _CATALOG_SIGNATURE = (_STAT.st_mtime_ns, _STAT.st_size)
except OSError:
    _CATALOG_SIGNATURE = (0, 0)

def list_melodies() -> tuple[Melody, ...]:
    global _MELODIES, _CATALOG_SIGNATURE
    try:
        stat = _SCORE_FILE.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature != _CATALOG_SIGNATURE:
            updated = (_MELODIES[0], *_song_melodies())
            _MELODIES = updated
            _CATALOG_SIGNATURE = signature
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        # Continue serving the previous complete catalog during an interrupted
        # publication. Never replace a valid snapshot with half-written data.
        _LOG.warning("[cd_catalog] recarga recusada: %s", type(exc).__name__)
    return _MELODIES


def catalog_signature() -> tuple[int, ...]:
    result = []
    for path in (_SCORE_FILE, _SCORE_FILE.with_name('cd_song_videos.json')):
        try:
            stat = path.stat()
            result.extend((stat.st_mtime_ns, stat.st_size))
        except OSError:
            result.extend((0, 0))
    return tuple(result)


def normalize_melody_id(value: object = None) -> str:
    if value is not None and not isinstance(value, str):
        raise ValueError("Escolha uma melodia disponível no painel de canto.")
    identity = str(value or "").strip() or DEFAULT_MELODY_ID
    if not any(melody.id == identity for melody in list_melodies()):
        raise ValueError("Essa melodia está indisponível. Escolha outra no painel de canto.")
    return identity


def get_melody(value: object = None) -> Melody:
    identity = normalize_melody_id(value)
    return next(melody for melody in list_melodies() if melody.id == identity)


def score_for_melody(value: object = None) -> dict:
    # JSON keeps the catalog immutable; each queue request owns its score.
    return json.loads(get_melody(value).score_json)


def fit_catalog_lyrics_to_score(text: str, segments: list[dict] | None, score: dict
                                ) -> tuple[str, list[dict] | None]:
    """Keep the lyric singable when shorthand '~' counts differ from a catalog score.

    A curated catalog score can receive freeform short lyrics: excess
    continuation marks are dropped from the right; when
    underspecified, automatic syllable alignment decides where to sustain.
    """
    if not segments or not any(part.get("text") == "~" and part.get("hold") for part in segments):
        return text, segments
    count = len(score["notes"])
    slots = sum(part.get("text") != "|" for part in segments)
    if slots == count:
        return text, segments
    result = [dict(part) for part in segments]
    if slots > count:
        excess = slots - count
        for index in range(len(result) - 1, -1, -1):
            if excess <= 0:
                break
            part = result[index]
            if part.get("text") == "~" and part.get("hold"):
                result.pop(index)
                excess -= 1
        if excess:
            # A lyric has more syllables than notes. Do not silently erase text.
            raise ValueError("A letra tem mais sílabas que notas nesta melodia.")
        slots = count
    if slots < count:
        # A shorter lyric can use the renderer's balanced automatic alignment.
        result = [part for part in result if not (part.get("text") == "~" and part.get("hold"))]
    clean = " ".join(part["text"] for part in result)
    if not any(part.get("hold") or part.get("fade_in") or part.get("fade_out")
               or part.get("pause_after_ms") or part["text"] == "|" for part in result):
        return clean, None
    return clean, result


_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def format_score_notes(melody: Melody) -> str:
    """Represent the *actual* MIDI score, in its stored order and octave."""
    score = json.loads(melody.score_json)
    sequence = []
    for note in score["notes"]:
        midi = note["midi_note"]
        if type(midi) is not int or not 0 <= midi <= 127:
            raise ValueError("invalid_score_midi_note")
        sequence.append(f"{_NOTE_NAMES[midi % 12]}{midi // 12 - 1}")
    return " · ".join(sequence)

def preview_text_for_melody(value: object = None, language: str = "pt-BR") -> str:
    """Keep one fresh vowel attack per note, including after musical rests."""
    melody = get_melody(value)
    if melody.id == DEFAULT_MELODY_ID:
        return "My dad has bread" if language == "en-US" else "Meu pai tem pão"
    syllable = "la" if language == "en-US" else "lá"
    return " ".join([syllable] * melody.note_count)
