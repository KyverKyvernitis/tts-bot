"""Catálogo de canto do bot; a escolha vira uma partitura antes de entrar na fila."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path



DEFAULT_MELODY_ID = "ttv-demo-v1"


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
    entries = json.loads((Path(__file__).parent / "cd_song_scores.json").read_text(encoding="utf-8"))
    songs = []
    for entry in entries:
        notes = entry["notes"]
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


def list_melodies() -> tuple[Melody, ...]:
    return _MELODIES


def normalize_melody_id(value: object = None) -> str:
    if value is not None and not isinstance(value, str):
        raise ValueError("Escolha uma melodia disponível no painel de canto.")
    identity = str(value or "").strip() or DEFAULT_MELODY_ID
    if not any(melody.id == identity for melody in _MELODIES):
        raise ValueError("Essa melodia está indisponível. Escolha outra no painel de canto.")
    return identity


def get_melody(value: object = None) -> Melody:
    identity = normalize_melody_id(value)
    return next(melody for melody in _MELODIES if melody.id == identity)


def score_for_melody(value: object = None) -> dict:
    # JSON keeps the catalog immutable; each queue request owns its score.
    return json.loads(get_melody(value).score_json)


def preview_text_for_melody(value: object = None, language: str = "pt-BR") -> str:
    """Keep one fresh vowel attack per note, including after musical rests."""
    melody = get_melody(value)
    if melody.id == DEFAULT_MELODY_ID:
        return "My dad has bread" if language == "en-US" else "Meu pai tem pão"
    syllable = "la" if language == "en-US" else "lá"
    return " ".join([syllable] * melody.note_count)
