"""Bounded offline TTV samples; validate synth profile and score before reuse."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .melodies import DEFAULT_MELODY_ID

_ROOT = Path(__file__).parent / 'cd' / 'previews'


def prepared_sample(melody_id: str, preferences: dict) -> bytes | None:
    if melody_id == DEFAULT_MELODY_ID:
        return None
    if not (preferences.get('ttv_voice_id') == 'kasane-teto'
            and preferences.get('ttv_language') == 'pt-BR'
            and float(preferences.get('ttv_pitch_semitones', 0) or 0) == 0
            and float(preferences.get('ttv_speech_rate', 1) or 1) == 1):
        return None
    try:
        index = json.loads((_ROOT / 'index.json').read_text(encoding='utf-8'))
        metadata = index.get(melody_id)
        score_hash = json.loads((Path(__file__).parent / 'cd_song_scores.json').read_text(encoding='utf-8'))
        entry = next(item for item in score_hash if item['id'] == melody_id)
        if (not isinstance(metadata, dict) or metadata.get('score_sha256') != entry.get('source_sha256')
                or metadata.get('voice_id') != 'kasane-teto' or metadata.get('language') != 'pt-BR'):
            return None
        target = _ROOT / f'{melody_id}.mp3'
        if target.is_symlink() or not target.is_file() or not 256 < target.stat().st_size <= 2_000_000:
            return None
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != metadata.get('mp3_sha256'):
            return None
        return data
    except (OSError, TypeError, ValueError, KeyError, StopIteration):
        return None
