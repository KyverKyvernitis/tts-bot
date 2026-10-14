"""Bounded offline TTV samples; validate synth profile and score before reuse."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re

from .melodies import DEFAULT_MELODY_ID, get_melody, score_sha256
from .cd_assets import FILES, file_signature
from osaka_cd.runtime import data_root, pin_snapshot, register_validator

_ROOT = Path(__file__).parent / 'cd' / 'previews'
_ID = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')


@lru_cache(maxsize=8)
def _index(path: str, signature: tuple) -> dict:
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    return raw if isinstance(raw, dict) else {}


def _has_mp3_frames(data: bytes) -> bool:
    """Reject empty/renamed audio without running a decoder in Discord actions.

    The package validator decodes audio in isolation; this bounded header check
    confirms that installed, hash-checked bytes still contain MPEG Layer III.
    """
    offset = 0
    if data.startswith(b'ID3'):
        if len(data) < 10 or any(value & 0x80 for value in data[6:10]):
            return False
        size = 0
        for value in data[6:10]:
            size = (size << 7) | value
        offset = 10 + size + (10 if data[5] & 0x10 else 0)
    frames = 0
    while offset + 4 <= len(data):
        header = int.from_bytes(data[offset:offset + 4], 'big')
        if header >> 21 != 0x7ff:
            break
        version, layer = (header >> 19) & 3, (header >> 17) & 3
        bitrate_index, rate_index = (header >> 12) & 15, (header >> 10) & 3
        if version == 1 or layer != 1 or bitrate_index in (0, 15) or rate_index == 3:
            return False
        bitrates = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
                    if version == 3 else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))
        sample_rate = (44100, 48000, 32000)[rate_index] // (1 if version == 3 else 2 if version == 2 else 4)
        length = (144_000 if version == 3 else 72_000) * bitrates[bitrate_index] // sample_rate + ((header >> 9) & 1)
        if offset + length > len(data):
            return False
        frames += 1
        offset += length
        if frames >= 2:
            return True
    return False


def prepared_sample(melody_id: str, preferences: dict) -> bytes | None:
    with pin_snapshot():
        return _prepared_sample(melody_id, preferences)


def _prepared_sample(melody_id: str, preferences: dict) -> bytes | None:
    if (not isinstance(melody_id, str) or not _ID.fullmatch(melody_id)
            or melody_id == DEFAULT_MELODY_ID or not isinstance(preferences, dict)):
        return None
    try:
        # _cd_preview_mp3 sings absolute score pitches/timing and always forces
        # pitch=0/rate=1. Speech preferences do not transpose this CD sample.
        if not (preferences.get('ttv_voice_id') == 'kasane-teto'
                and preferences.get('ttv_language') == 'pt-BR'):
            return None
        selected_root = data_root(_ROOT.parent.parent) / 'cd/previews'
        # Preserve custom legacy preview roots in tests and existing deployments.
        from osaka_cd.runtime import snapshot
        if not snapshot().get('initialized'):
            selected_root = _ROOT
        index_path = selected_root / 'index.json'
        signature = file_signature(index_path)
        if signature is None or not 0 < signature[2] <= 8 * 1024 * 1024:
            return None
        index = _index(str(index_path), signature)
        metadata = index.get(melody_id)
        melody = get_melody(melody_id)
        if not isinstance(metadata, dict):
            return None
        # Legacy v1 called the MIDI provenance hash "score_sha256". It can only
        # be migrated when its actual score is known, adding this content key.
        digest = (metadata.get('score_sha256') if metadata.get('manifest_version') in (2, 3)
                  else metadata.get('score_content_sha256'))
        actual = melody.score_sha256 or score_sha256(json.loads(melody.score_json))
        if (not isinstance(digest, str) or not _SHA256.fullmatch(digest) or digest != actual
                or metadata.get('id', melody_id) != melody_id
                or ('revision' in metadata and metadata['revision'] != melody.revision)
                or ('source_sha256' in metadata and metadata['source_sha256'] != melody.source_sha256)
                or metadata.get('voice_id') != 'kasane-teto' or metadata.get('language') != 'pt-BR'
                or float(metadata.get('pitch_semitones', float('nan'))) != 0
                or float(metadata.get('speech_rate', float('nan'))) != 1):
            return None
        if melody.arrangement_json:
            profile = metadata.get('rendering_profile')
            if (metadata.get('manifest_version') != 3
                    or metadata.get('arrangement_sha256') != melody.arrangement_preview_sha256
                    or metadata.get('source_arrangement_sha256') != melody.arrangement_sha256
                    or metadata.get('arrangement_performance_sha256') != melody.arrangement_preview_performance_sha256
                    or not isinstance(profile, dict)
                    or profile.get('mode') not in {'multipart-utau', 'legacy-monophonic-migration'}):
                return None
            if profile['mode'] == 'legacy-monophonic-migration':
                if len(json.loads(melody.arrangement_preview_json)['parts']) != 1:
                    return None
            elif any(not isinstance(profile.get(key), str) or not _SHA256.fullmatch(profile[key])
                     for key in ('configuration_fingerprint', 'render_fingerprint')):
                return None
        mp3_hash = metadata.get('mp3_sha256')
        if not isinstance(mp3_hash, str) or not _SHA256.fullmatch(mp3_hash):
            return None
        target = selected_root / f'{melody_id}.mp3'
        return FILES.read(target, mp3_hash, 2_000_000, 'mp3',
                          lambda data: len(data) > 256 and _has_mp3_frames(data))
    except (OSError, TypeError, ValueError, KeyError):
        return None


def _validate_preview_index(root):
    path = root / 'cogs/tts/cd/previews/index.json'
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict) or len(raw) > 500:
        raise ValueError('invalid_cd_preview_index')
    for identity, metadata in raw.items():
        if not isinstance(identity, str) or not _ID.fullmatch(identity) or not isinstance(metadata, dict):
            raise ValueError('invalid_cd_preview_entry')


register_validator(_validate_preview_index)
