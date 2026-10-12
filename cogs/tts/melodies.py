"""Catálogo de canto do bot; a escolha vira uma partitura antes de entrar na fila."""
from __future__ import annotations

from dataclasses import dataclass
import json

from .performance import DEFAULT_MELODY_PRESET


DEFAULT_MELODY_ID = DEFAULT_MELODY_PRESET


@dataclass(frozen=True, slots=True)
class Melody:
    id: str
    name: str
    description: str
    duration_ms: int
    note_count: int
    thumbnail_url: str = ""
    score_json: str = '{"version":1,"preset":"ttv-demo-v1"}'
    revision: int = 1


_MELODIES = (Melody(
    id=DEFAULT_MELODY_ID,
    name="Padrão",
    description="C4 · E4 · G4 · E4",
    duration_ms=4000,
    note_count=4,
),)


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
