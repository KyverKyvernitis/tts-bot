#!/usr/bin/env python3
"""Recover the supplied Rolling Girl test adaptation and its existing UTAU MP3.

No synthesis, downloads, worker writes, or automatic remote publication. The
input MIDI and MP3 are the exact supplied artifacts, not newly rendered audio.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from catalog_import import (DATA_ROOT, _atomic, _json_bytes, _manifest, _matching_preview,
                            export_song_package, open_catalog, preview_manifest, score_sha256)
from midi_extract import extract_midi

HERE = Path(__file__).resolve().parent
KIT_ROOT = HERE.parents[1]
SONG_ID = 'rolling-girl'
MIDI_SHA256 = 'bceafe9c4bb75955e91113b76e971021cae6e1d5d08745283e48934a1f437f46'
MP3_SHA256 = '6c5b39a5db5160cc17867b94bd351535a08063e191563c58465a7ae32d7024e0'


def _read(path: Path, default=None):
    if not path.is_file():
        if default is not None:
            return default
        raise ValueError('recovery_file_missing: ' + path.name)
    return json.loads(path.read_text(encoding='utf-8'))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def recover(state_dir: Path, midi: Path, data_root: Path, output: Path) -> dict:
    state_dir, midi, data_root = state_dir.expanduser(), midi.expanduser(), data_root.expanduser()
    if not midi.is_file() or midi.is_symlink() or _digest(midi.read_bytes()) != MIDI_SHA256:
        raise ValueError('bundled_midi_hash_mismatch')
    rows = _read(state_dir / 'cd_song_scores.json')
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get('id') != SONG_ID:
        raise ValueError('invalid_recovery_score')
    bundled = dict(rows[0])
    extracted = extract_midi(midi)
    extracted_score = {'duration_ms': extracted['score']['duration_ms'],
                       'notes': [{key: note[key] for key in ('midi_note', 'start_ms', 'duration_ms', 'velocity')}
                                 for note in extracted['score']['notes']]}
    if (bundled.get('source_sha256') != MIDI_SHA256 or score_sha256(extracted_score) != score_sha256(bundled)
            or bundled.get('score_sha256') != score_sha256(bundled)):
        raise ValueError('bundled_score_does_not_match_exact_midi_notes_and_duration')
    bundled_preview = _read(state_dir / 'previews/index.json').get(SONG_ID)
    audio_path = state_dir / 'previews' / f'{SONG_ID}.mp3'
    if not audio_path.is_file() or audio_path.is_symlink():
        raise ValueError('bundled_mp3_missing')
    provided_audio = audio_path.read_bytes()
    if (not isinstance(bundled_preview, dict) or bundled_preview.get('manifest_version') != 2
            or bundled_preview.get('mp3_sha256') != MP3_SHA256 or _digest(provided_audio) != MP3_SHA256
            or bundled_preview.get('score_sha256') != score_sha256(bundled)
            or bundled_preview.get('source_sha256') != MIDI_SHA256):
        raise ValueError('bundled_mp3_or_manifest_hash_mismatch')
    videos = _read(state_dir / 'cd_song_videos.json')
    bundled_video = videos.get('songs', {}).get(SONG_ID)
    if not isinstance(bundled_video, dict) or bundled_video.get('video_id') != 'vnw8zURAxkU':
        raise ValueError('bundled_official_video_identity_mismatch')
    # Bootstrap only when necessary; never replace another local CD.
    songs, local_videos = open_catalog(data_root)
    local_song = next((song for song in songs if song.get('id') == SONG_ID), None)
    if local_song and local_song.get('revision', 1) > bundled.get('revision', 1):
        raise ValueError('local_newer_revision_preserved: use um data-root isolado para examinar a recuperação')
    if local_song and score_sha256(local_song) != score_sha256(bundled):
        raise ValueError('local_different_score_preserved: use um data-root isolado para examinar a recuperação')
    melody = dict(local_song or bundled)
    melody.setdefault('revision', bundled.get('revision', 1))
    melody['score_sha256'] = score_sha256(melody)
    local_video = local_videos['songs'].get(SONG_ID, {})
    video = dict(bundled_video)
    if isinstance(local_video, dict) and local_video.get('video_id') == video['video_id']:
        video.update(local_video)
    video.update(id=SONG_ID, revision=melody['revision'], source_sha256=melody['source_sha256'],
                 score_sha256=melody['score_sha256'])
    image = None
    if video.get('thumbnail_sha256'):
        candidates = [data_root / 'thumbnails' / f"{video['video_id']}.jpg",
                      state_dir / 'thumbnails' / f"{video['video_id']}.jpg"]
        image = next((path.read_bytes() for path in candidates if path.is_file() and not path.is_symlink()
                      and _digest(path.read_bytes()) == video['thumbnail_sha256']), None)
        if image is None:
            video.pop('thumbnail_sha256', None)
    previews = _manifest(data_root)
    preview = _matching_preview(data_root, melody, previews.get(SONG_ID))
    sample_origin = 'existing-compatible-local' if preview else 'provided-legacy-migration'
    if preview:
        audio = (data_root / 'previews' / f'{SONG_ID}.mp3').read_bytes()
    else:
        audio = provided_audio
        preview = {**bundled_preview, **preview_manifest(melody, audio), 'migration': {
            'source': 'osaka-cd-sample-rolling-girl-bceafe9c.zip',
            'legacy_score_hash_kind': 'source-midi-sha256', 'legacy_score_sha256': MIDI_SHA256,
            'provided_mp3_sha256': MP3_SHA256, 'resynthesized': False,
            'score_relation': 'exact-worker-notes-and-duration',
            'musical_content': 'simplified-instrumental-test-adaptation; vocal-transcription-unconfirmed'}}
    merged_songs = [song for song in songs if song.get('id') != SONG_ID] + [melody]
    merged_videos = {**local_videos, 'songs': {**local_videos['songs'], SONG_ID: video}}
    # The exported snapshot is validated, including MP3 decode, in isolation
    # before replacing any existing local score/index/audio.
    with tempfile.TemporaryDirectory(prefix='osaka-rolling-recovery-') as temporary:
        stage = Path(temporary)
        _atomic(stage / 'cd_song_scores.json', _json_bytes(merged_songs))
        _atomic(stage / 'cd_song_videos.json', _json_bytes(merged_videos))
        _atomic(stage / 'previews/index.json', _json_bytes({SONG_ID: preview}))
        _atomic(stage / 'previews' / f'{SONG_ID}.mp3', audio)
        if image is not None:
            _atomic(stage / 'thumbnails' / f"{video['video_id']}.jpg", image)
        verification = export_song_package(stage, SONG_ID, output, sample_status='ready')
    _atomic(data_root / 'previews' / f'{SONG_ID}.mp3', audio)
    previews[SONG_ID] = preview
    _atomic(data_root / 'previews/index.json', _json_bytes(previews))
    _atomic(data_root / 'cd_song_scores.json', _json_bytes(merged_songs))
    _atomic(data_root / 'cd_song_videos.json', _json_bytes(merged_videos))
    _atomic(data_root / 'packages' / f'{SONG_ID}.json', _json_bytes(verification['package_manifest']))
    if image is not None:
        _atomic(data_root / 'thumbnails' / f"{video['video_id']}.jpg", image)
    # Prime the ordinary importer with the current input/settings identity.
    # --retry immediately retries a missing cover and keeps this ready sample.
    spec = importlib.util.spec_from_file_location('_osaka_recovery_cli', HERE / 'import-midi.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity, _ = module._identity(midi, SimpleNamespace(candidates=None, cover_file=None, no_sample=True))
    history = _read(data_root / 'imported.json', {})
    history[midi.name] = identity
    _atomic(data_root / 'imported.json', _json_bytes(history))
    pending = _read(data_root / 'pending_artifacts.json', {})
    if image is None:
        pending[midi.name] = {'identity': identity, 'id': SONG_ID,
                             'artifacts': [{'artifact': 'thumbnail', 'reason': 'thumbnail_not_in_supplied_files'}],
                             'retry_after': 0}
    else:
        pending.pop(midi.name, None)
    _atomic(data_root / 'pending_artifacts.json', _json_bytes(pending))
    samples = _read(data_root / 'pending_samples.json', {})
    samples.pop(SONG_ID, None)
    _atomic(data_root / 'pending_samples.json', _json_bytes(samples))
    failures = _read(data_root / 'failed.json', {})
    failures.pop(midi.name, None)
    _atomic(data_root / 'failed.json', _json_bytes(failures))
    return {'ok': True, 'id': SONG_ID, 'complete': image is not None, 'sample_status': 'ready',
            'sample_origin': sample_origin, 'resynthesized': False,
            'thumbnail_status': 'ready' if image is not None else 'pending',
            'source_sha256': melody['source_sha256'], 'score_sha256': score_sha256(melody),
            'mp3_sha256': preview['mp3_sha256'], 'zip': verification['zip_path'],
            'zip_sha256': verification['zip_sha256'], 'validation': verification,
            'published_to_vps': False, 'installed_on_vps': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=KIT_ROOT / 'recovery/state')
    parser.add_argument('--midi', type=Path, default=KIT_ROOT / 'examples/rolling-girl/wowaka - Rolling Girl.mid')
    parser.add_argument('--data-root', type=Path, default=DATA_ROOT)
    parser.add_argument('--output', type=Path, default=Path.home() / 'storage/downloads' /
                        ('osaka-cd-recovered-rolling-girl-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '.zip'))
    args = parser.parse_args()
    try:
        result = recover(args.state_dir, args.midi, args.data_root, args.output)
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': type(exc).__name__,
                          'reason': str(exc) if isinstance(exc, ValueError) else 'recovery_failed'}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
