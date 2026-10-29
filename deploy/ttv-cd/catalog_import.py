"""Import local MIDI into reproducible, incremental Osaka CD packages.

The Phone Worker runtime is read only. A package includes one CD and all its
completed artifacts; pending artifacts never discard a matching ready sample.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import zipfile

from midi_extract import extract_midi, MidiExtractionError, MAX_MIDI_BYTES
from package_validation import score_sha256, validate_package

ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path.home() / '.local/share/osaka-ttv-cd'
THUMB_ROOT = Path.home() / '.cache/core-worker/ttv/cd/thumbnails'
SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
VIDEO = re.compile(r'[A-Za-z0-9_-]{11}\Z')
MAX_MIDI = MAX_MIDI_BYTES
IMPORTER_VERSION = "9.0"


def normalize_text(value: str) -> str:
    from cd_metadata import player_modules
    return player_modules()[0].normalize_text(value)


def clean_metadata_title(value: str) -> str:
    from cd_metadata import player_modules
    return player_modules()[0].clean_metadata_title(value)


class ImportInputError(ValueError):
    permanent = True


def _read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def _atomic(path: Path, data: bytes):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name, delete=False) as file:
        temporary = Path(file.name)
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _slug(text: str) -> str:
    value = normalize_text(text).replace(' ', '-')[:90].strip('-')
    if not SLUG.fullmatch(value):
        raise ImportInputError('invalid_cd_id: informe id ASCII no sidecar')
    return value


def open_catalog(root: Path) -> tuple[list, dict]:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    scores, videos = root / 'cd_song_scores.json', root / 'cd_song_videos.json'
    if not scores.is_file():
        _atomic(scores, (ROOT / 'catalog/cd_song_scores.json').read_bytes())
    if not videos.is_file():
        _atomic(videos, (ROOT / 'catalog/cd_song_videos.json').read_bytes())
    songs, covers = _read_json(scores), _read_json(videos)
    if not isinstance(songs, list) or not isinstance(covers, dict) or not isinstance(covers.get('songs'), dict):
        raise ImportInputError('invalid_local_catalog')
    return songs, covers


def _strings(meta: dict, field: str, *, limit: int = 12) -> list[str]:
    values = meta.get(field, [])
    if not isinstance(values, list) or len(values) > limit or any(
            not isinstance(value, str) or not 1 <= len(value.strip()) <= 160 for value in values):
        raise ImportInputError('invalid_' + field)
    return [value.strip() for value in values]


def _meta_from_path(path: Path) -> dict:
    sidecar = path.with_suffix('.json')
    try:
        meta = _read_json(sidecar) if sidecar.is_file() else {}
    except (OSError, ValueError) as exc:
        raise ImportInputError('invalid_midi_sidecar') from exc
    if sidecar.is_symlink() or not isinstance(meta, dict):
        raise ImportInputError('invalid_midi_sidecar')
    stem = re.sub(r'[_]+', ' ', path.stem).strip()
    artist, title = stem.split(' - ', 1) if ' - ' in stem else ('', stem)
    title, artist = str(meta.get('title') or title).strip(), str(meta.get('artist') or artist).strip()
    if not title or len(title) > 160 or len(artist) > 160:
        raise ImportInputError('invalid_midi_title')
    slug = str(meta.get('id') or _slug(title))
    if not SLUG.fullmatch(slug) or len(slug) > 90:
        raise ImportInputError('invalid_midi_slug')
    selection = {}
    for field, low, high, default in [('track_index', 0, 63, None), ('channel', 0, 15, None),
                                      ('start_ms', 0, 2147483647, None),
                                      ('max_duration_ms', 40, 60000, 60000), ('max_notes', 1, 128, 128)]:
        value = meta.get(field, default)
        if value is not None and (type(value) is not int or not low <= value <= high):
            raise ImportInputError('invalid_' + field)
        selection[field] = value
    query = str(meta.get('search_query') or f'{title} {artist}').strip()
    if not query or len(query) > 320:
        raise ImportInputError('invalid_search_query')
    seeds = _strings(meta, 'seed_video_ids')
    if any(not VIDEO.fullmatch(seed) for seed in seeds):
        raise ImportInputError('invalid_seed_video_ids')
    expected_duration = meta.get('expected_song_duration_seconds')
    if expected_duration is not None and (type(expected_duration) not in (int, float) or not 1 <= expected_duration <= 3600):
        raise ImportInputError('invalid_expected_song_duration_seconds')
    max_parts = meta.get('max_parts', 3)
    include_bass = meta.get('include_bass', True)
    if type(max_parts) is not int or not 1 <= max_parts <= 3 or type(include_bass) is not bool:
        raise ImportInputError('invalid_arrangement_selection')
    full_song = meta.get('source_is_full_song', False)
    if type(full_song) is not bool:
        raise ImportInputError('invalid_source_is_full_song')
    return {'id': slug, 'title': title, 'artist': artist, 'search_query': query,
            'aliases': _strings(meta, 'aliases'), 'artist_aliases': _strings(meta, 'artist_aliases'),
            'allow_versions': _strings(meta, 'allow_versions'), 'seed_video_ids': seeds,
            'expected_song_duration_seconds': expected_duration, 'source_is_full_song': full_song,
            'max_parts': max_parts, 'include_bass': include_bass,
            '_explicit_fields': frozenset(meta), **selection}


def _inherit_aliases(meta: dict, original: dict | None, previous_video: dict | None) -> dict:
    """Keep established names when an unchanged song omits optional sidecar fields.

    An explicit empty array clears inherited aliases. Names never cross a changed
    song/artist identity, and arbitrary candidate titles are not added as aliases.
    """
    from cd_metadata import unicode_key
    if (not isinstance(original, dict) or not isinstance(previous_video, dict)
            or unicode_key(original.get('name')) != unicode_key(meta['title'])
            or not meta['artist']
            or unicode_key(original.get('artist')) != unicode_key(meta['artist'])):
        return meta
    result = dict(meta)
    explicit = meta.get('_explicit_fields', ())
    for field, stored_field in [('aliases', 'title_aliases'), ('artist_aliases', 'artist_aliases')]:
        if field in explicit:
            continue
        values = previous_video.get(stored_field)
        if not isinstance(values, list):
            continue
        unique = {}
        for value in values:
            if isinstance(value, str) and 1 <= len(value.strip()) <= 160:
                key = unicode_key(value)
                if key and (field != 'aliases' or key != unicode_key(meta['title'])):
                    unique.setdefault(key, value.strip())
        result[field] = list(unique.values())[:12]
    return result


def most_viewed(meta: dict, candidates: list[dict]) -> dict | None:
    """v7 helper retained for callers; matching lives in shared player metadata."""
    from cd_metadata import search_metadata, MetadataNoMatchError
    try:
        return search_metadata(meta['title'], meta.get('artist', ''), meta.get('aliases', []),
                               candidates=candidates, artist_aliases=meta.get('artist_aliases', []),
                               allow_versions=meta.get('allow_versions', []))['selected']
    except MetadataNoMatchError:
        return None


def _load_cover_tool():
    spec = importlib.util.spec_from_file_location('osaka_cd_cover_cache', ROOT / 'cache-thumbnails.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _search(meta: dict, *, candidate_file: Path | None = None, duration: float | None = None) -> dict:
    from cd_metadata import search_metadata, MetadataNoMatchError
    if not meta['artist'] and not meta['artist_aliases']:
        raise MetadataNoMatchError('artist_unspecified: informe artist ou artist_aliases no sidecar',
                                   {'provider': 'not-run', 'queries': [], 'inspected_candidates': 0,
                                    'criteria': {'artist_evidence_required': True}, 'reason': 'artist_unspecified'})
    candidates = None
    if candidate_file:
        try:
            candidates = _read_json(candidate_file)
        except (OSError, ValueError) as exc:
            raise ImportInputError('invalid_candidates_file') from exc
        if not isinstance(candidates, list):
            raise ImportInputError('candidates_file_must_be_list')
    # Read the player's configured API key without exporting or logging it.
    from worker_config import load_environment
    environment = load_environment(include_search_credentials=True) if candidates is None else {}
    return search_metadata(meta['title'], meta['artist'], meta['aliases'], 25,
                           artist_aliases=meta['artist_aliases'], candidates=candidates,
                           youtube_api_key=environment.get('YOUTUBE_API_KEY') or environment.get('GOOGLE_YOUTUBE_API_KEY'),
                           duration=duration, allow_versions=meta['allow_versions'],
                           seed_video_ids=meta['seed_video_ids'], queries=(meta['search_query'],))


def _verify_cover(image: bytes) -> str:
    if len(image) > 2 * 1024 * 1024 or not image.startswith(b'\xff\xd8\xff') or not image.endswith(b'\xff\xd9'):
        raise ImportInputError('invalid_override_cover')
    from PIL import Image
    try:
        with Image.open(io.BytesIO(image)) as cover:
            if cover.format != 'JPEG' or not (0 < cover.width <= 768 and 0 < cover.height <= 768):
                raise ImportInputError('invalid_override_cover_dimensions')
            cover.load()
    except (OSError, ValueError) as exc:
        raise ImportInputError('invalid_override_cover') from exc
    return hashlib.sha256(image).hexdigest()


async def _cover_for(meta: dict, chosen: dict, cache_dir: Path) -> tuple[bytes, str]:
    tool = _load_cover_tool()
    with tempfile.TemporaryDirectory(prefix='.cd-import-') as temporary:
        catalog = Path(temporary) / 'videos.json'
        catalog.write_bytes(_json_bytes({'version': 1, 'songs': {meta['id']: {
            'video_id': chosen['video_id'], 'title': chosen['title'], 'search_query': meta['search_query'],
            'title_aliases': [meta['title'], *meta['aliases']]}}}))
        result = await tool.prepare(type('CoverArgs', (), {
            'catalog': catalog, 'cache_dir': cache_dir, 'refresh_videos': False, 'export_zip': None})())
    if not result.get('ok') or not result.get('prepared'):
        raise RuntimeError('cover_preparation_failed')
    image = (cache_dir / f"{chosen['video_id']}.jpg").read_bytes()
    return image, _verify_cover(image)


def _selected_video(meta: dict, selected: dict, audit: dict) -> dict:
    """One recording/cover contract shared by MIDI imports and artwork repair."""
    return {'video_id': selected['video_id'], 'title': clean_metadata_title(selected['title']),
            'artist': meta['artist'], 'search_query': meta['search_query'],
            'title_aliases': [meta['title'], *meta['aliases']],
            'artist_aliases': list(meta['artist_aliases']), 'channel': selected.get('channel', ''),
            'duration': selected.get('duration'),
            'webpage_url': f"https://www.youtube.com/watch?v={selected['video_id']}",
            'thumbnail_url': selected.get('thumbnail') or f"https://i.ytimg.com/vi/{selected['video_id']}/hqdefault.jpg",
            'view_count': selected['view_count'],
            'view_count_source': selected.get('source', 'verified-candidates'),
            'checked_at': selected.get('checked_at') or datetime.now(timezone.utc).isoformat(),
            'inspected_candidates': audit.get('inspected_candidates', 0),
            'selection_basis': 'highest-view-count-among-matching-verified-candidates',
            'selection_audit': audit, 'source_url': f"https://www.youtube.com/watch?v={selected['video_id']}"}


def _metadata_overlay(root: Path, melody: dict, video: dict, image: bytes,
                      destination: Path) -> dict:
    """Export only display metadata and its verified cover, inheriting all music.

    A small private validation base models the existing CD. Its arrangement and
    audio are copied byte for byte for validation and are never included in the
    overlay, so a metadata repair cannot replace their original representations.
    """
    identity = melody['id']
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='osaka-metadata-overlay-') as temporary:
        base = Path(temporary) / 'base'
        _atomic(base / 'cogs/tts/cd_song_scores.json', _json_bytes([melody]))
        _atomic(base / 'cogs/tts/cd_song_videos.json', _json_bytes({'version': 1, 'songs': {}}))
        preview = _manifest(root).get(identity)
        _atomic(base / 'cogs/tts/cd/previews/index.json', _json_bytes({identity: preview} if preview else {}))
        relatives = [('arrangements', 'json'), ('previews', 'mp3')]
        for folder, extension in relatives:
            source = root / folder / f'{identity}.{extension}'
            if source.is_file() and not source.is_symlink():
                target = base / 'cogs/tts/cd' / folder / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        manifest_melody = dict(melody)
        arrangement_file = base / f'cogs/tts/cd/arrangements/{identity}.json'
        if melody.get('arrangement_path') and arrangement_file.is_file():
            manifest_melody['_arrangement_file_sha256'] = hashlib.sha256(arrangement_file.read_bytes()).hexdigest()
        _atomic(base / f'cogs/tts/cd/packages/{identity}.json', _json_bytes(
            _package_manifest(manifest_melody, video, preview, 'ready' if preview else 'pending_sample')))
        package = Path(temporary) / 'metadata.zip'
        with zipfile.ZipFile(package, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('cogs/tts/cd_song_scores.json', _json_bytes([melody]))
            archive.writestr('cogs/tts/cd_song_videos.json', _json_bytes({'version': 1, 'songs': {identity: video}}))
            archive.writestr(f"cogs/tts/cd/thumbnails/{video['video_id']}.jpg", image)
        report = validate_package(package, base_dir=base, require_cd_only=True, audio_probe=False)
        _atomic(destination, package.read_bytes())
    report.update(zip_path=str(destination), zip_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                  zip_bytes=destination.stat().st_size)
    return report


async def repair_metadata(identity: str, *, root: Path, metadata: dict,
                          cache_dir: Path = THUMB_ROOT, candidates: Path | None = None,
                          export_zip: Path | None = None) -> dict:
    """Repair an installed CD through the normal recording and cover importer.

    No source MIDI is required. The installed English/display name, notes,
    revision, hashes, arrangement bytes and audio remain unchanged. Only artist,
    source URL, verified recording metadata and cover can change. A provider or
    image failure writes nothing and exports no package.
    """
    if not isinstance(identity, str) or len(identity) > 90 or not SLUG.fullmatch(identity):
        raise ImportInputError('invalid_cd_id')
    if not isinstance(metadata, dict):
        raise ImportInputError('invalid_metadata')
    root, cache_dir = Path(root).expanduser(), Path(cache_dir).expanduser()
    songs, videos = open_catalog(root)
    original = next((song for song in songs if song.get('id') == identity), None)
    if original is None:
        raise ImportInputError('song_not_in_local_catalog')
    # Reuse the sidecar validator; no source MIDI or uploaded executable exists.
    with tempfile.TemporaryDirectory(prefix='osaka-repair-input-') as temporary:
        path = Path(temporary) / (identity + '.mid')
        _atomic(path.with_suffix('.json'), _json_bytes({**metadata, 'id': identity, 'title': original['name']}))
        meta = _meta_from_path(path)
    pending = []
    try:
        if not meta['artist'] or normalize_text(meta['artist']) in {'artista nao identificado', 'unknown artist', 'unknown'}:
            raise ImportInputError('artist_unspecified')
        search = await asyncio.to_thread(_search, meta, candidate_file=candidates,
                                        duration=meta['expected_song_duration_seconds'])
        video = _selected_video(meta, search['selected'], search['audit'])
    except Exception as exc:
        pending.extend([{'artifact': 'metadata', 'reason': type(exc).__name__},
                        {'artifact': 'thumbnail', 'reason': 'pending_metadata'}])
        return {'ok': False, 'id': identity, 'pending_artifacts': pending}
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        image, checksum = await _cover_for(meta, video, cache_dir)
    except Exception as exc:
        return {'ok': False, 'id': identity,
                'pending_artifacts': [{'artifact': 'thumbnail', 'reason': type(exc).__name__}]}
    melody = dict(original, artist=meta['artist'], source_url=video['source_url'])
    video.update(id=identity, revision=original.get('revision', 1),
                 source_sha256=original['source_sha256'], score_sha256=score_sha256(original),
                 thumbnail_sha256=checksum)
    destination = Path(export_zip).expanduser() if export_zip else root / 'exports' / f'{identity}-metadata.zip'
    verification = _metadata_overlay(root, melody, video, image, destination)
    # Only a completely validated metadata+cover overlay becomes local truth.
    songs = [melody if song.get('id') == identity else song for song in songs]
    videos['songs'][identity] = video
    _atomic(root / 'thumbnails' / f"{video['video_id']}.jpg", image)
    _atomic(root / 'cd_song_scores.json', _json_bytes(songs))
    _atomic(root / 'cd_song_videos.json', _json_bytes(videos))
    return {'ok': True, 'id': identity, 'pending_artifacts': [],
            'video_id': video['video_id'], 'source_sha256': original['source_sha256'],
            'score_sha256': score_sha256(original), 'revision': original.get('revision', 1),
            'zip': verification['zip_path'], 'zip_sha256': verification['zip_sha256'],
            'zip_bytes': verification['zip_bytes'], 'validation': verification}


def _resource_ready() -> bool:
    from worker_config import resource_ready
    try:
        return resource_ready()
    except Exception:
        return False


def _sample(melody: dict, target: Path) -> str:
    try:
        from worker_config import render_sample
        result = render_sample(melody, target)
        if isinstance(result, dict):
            melody['_render_report'] = result
        return str(result.get('status', 'pending_worker_unknown')) if isinstance(result, dict) else str(result)
    except Exception as exc:
        return 'pending_' + type(exc).__name__


def _validate_score(score: dict) -> None:
    notes, duration = score.get('notes'), score.get('duration_ms')
    if not isinstance(notes, list) or not 1 <= len(notes) <= 128:
        raise ImportInputError('invalid_score_notes')
    if type(duration) is not int or not 1 <= duration <= 60000:
        raise ImportInputError('invalid_score_duration')
    end = 0
    for note in notes:
        if (not isinstance(note, dict) or type(note.get('midi_note')) is not int
                or not 36 <= note['midi_note'] <= 96):
            raise ImportInputError('invalid_score_pitch')
        start, length = note.get('start_ms'), note.get('duration_ms')
        if (type(start) is not int or type(length) is not int or start < end
                or length < 40 or start + length > duration):
            raise ImportInputError('invalid_score_timing')
        end = start + length


def _manifest(root: Path) -> dict:
    try:
        value = _read_json(root / 'previews/index.json')
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def arrangement_performance_sha256(arrangement: dict) -> str:
    from osaka_cd.arrangement import arrangement_performance_sha256 as fingerprint
    return fingerprint(arrangement)


def sample_melody(root: Path, melody: dict) -> dict:
    """Load the same bounded common preview after a source MIDI was removed."""
    if not melody.get('arrangement_path'):
        return dict(melody)
    from osaka_cd.arrangement import arrangement_sha256, preview_arrangement
    full = _read_json(Path(root) / 'arrangements' / f"{melody['id']}.json")
    if arrangement_sha256(full) != melody.get('arrangement_sha256'):
        raise ImportInputError('local_arrangement_hash_mismatch')
    selected = preview_arrangement(full, start_ms=melody['arrangement_preview_start_ms'],
                                   end_ms=melody['arrangement_preview_end_ms'])['arrangement']
    if arrangement_sha256(selected) != melody.get('preview_arrangement_sha256'):
        raise ImportInputError('local_preview_arrangement_hash_mismatch')
    return dict(melody, arrangement=selected)


def preview_manifest(melody: dict, audio: bytes, render_report: dict | None = None) -> dict:
    item = {'manifest_version': 2, 'id': melody['id'], 'revision': melody.get('revision', 1),
            'source_sha256': melody['source_sha256'], 'score_sha256': score_sha256(melody),
            'mp3_sha256': hashlib.sha256(audio).hexdigest(), 'voice_id': 'kasane-teto',
            'language': 'pt-BR', 'pitch_semitones': 0, 'speech_rate': 1.0}
    if melody.get('arrangement_sha256'):
        report = render_report or melody.get('_render_report') or {}
        profile = report.get('rendering_profile') or {
            'mode': 'multipart-utau', 'configuration_fingerprint': report.get('configuration_fingerprint'),
            'render_fingerprint': report.get('mix_sha256'), 'renderer_version': report.get('worker_version')}
        item.update(manifest_version=3, arrangement_sha256=melody['preview_arrangement_sha256'],
                    source_arrangement_sha256=melody['arrangement_sha256'],
                    arrangement_performance_sha256=melody['preview_performance_sha256'],
                    rendering_profile=profile)
    return item


def _matching_preview(root: Path, melody: dict, item: dict | None) -> dict | None:
    if (not isinstance(item, dict) or item.get('manifest_version') not in {2, 3}
            or item.get('id', melody['id']) != melody['id']
            or item.get('score_sha256') != score_sha256(melody)
            or any(item.get(key) != value for key, value in [('voice_id', 'kasane-teto'), ('language', 'pt-BR'),
                                                          ('pitch_semitones', 0), ('speech_rate', 1.0)])):
        return None
    profile = item.get('rendering_profile')
    if melody.get('arrangement_sha256'):
        fingerprint = melody.get('_configuration_fingerprint')
        if item.get('manifest_version') == 3:
            if (item.get('arrangement_performance_sha256') != melody.get('preview_performance_sha256')
                    or not isinstance(profile, dict)
                    or profile.get('mode') not in {'multipart-utau', 'legacy-monophonic-migration'}
                    or profile.get('mode') == 'legacy-monophonic-migration' and melody.get('arrangement_part_count') != 1):
                return None
            if (profile.get('mode') != 'legacy-monophonic-migration' and fingerprint
                    and profile.get('configuration_fingerprint') != fingerprint):
                return None
        elif melody.get('arrangement_part_count') == 1:
            profile = {'mode': 'legacy-monophonic-migration', 'voice_id': 'kasane-teto',
                       'provenance': 'previous-validated-monophonic-preview'}
        else:
            return None
        if profile.get('mode') == 'legacy-monophonic-migration' and fingerprint:
            # Record acceptance, not a claim about the historical renderer.
            # Further configuration changes must invalidate migrated audio.
            accepted = profile.get('accepted_configuration_fingerprint')
            if accepted and accepted != fingerprint:
                return None
            profile = dict(profile, accepted_configuration_fingerprint=fingerprint)
    audio = root / 'previews' / f"{melody['id']}.mp3"
    if not audio.is_file() or audio.is_symlink() or hashlib.sha256(audio.read_bytes()).hexdigest() != item.get('mp3_sha256'):
        return None
    report = {'rendering_profile': profile} if profile else None
    return {**item, **preview_manifest(melody, audio.read_bytes(), report)}


def _package_manifest(melody: dict, video: dict | None, preview: dict | None, sample_status: str) -> dict:
    artifacts = {'score': {'status': 'ready', 'sha256': score_sha256(melody)},
                 'thumbnail': {'status': 'pending'}, 'sample': {'status': 'pending', 'reason': sample_status}}
    if video and video.get('thumbnail_sha256'):
        artifacts['thumbnail'] = {'status': 'ready', 'path': f"cogs/tts/cd/thumbnails/{video['video_id']}.jpg",
                                  'sha256': video['thumbnail_sha256']}
    if preview:
        artifacts['sample'] = {'status': 'ready', 'path': f"cogs/tts/cd/previews/{melody['id']}.mp3",
                               'sha256': preview['mp3_sha256']}
    package = {'manifest_version': 1, 'id': melody['id'], 'revision': melody.get('revision', 1),
            'source_sha256': melody['source_sha256'], 'score_sha256': score_sha256(melody),
            'producer': {'name': 'osaka-cd-termux-kit', 'version': IMPORTER_VERSION},
            'artifacts': artifacts}
    if melody.get('arrangement_sha256'):
        package.update(manifest_version=2, arrangement_sha256=melody['arrangement_sha256'])
        artifacts['arrangement'] = {'status': 'ready', 'path': melody['arrangement_path'],
                                   'sha256': melody['_arrangement_file_sha256']}
    return package


def _export(melody: dict, video: dict | None, preview: dict | None, image: bytes | None,
            audio: bytes | None, destination: Path, *, sample_status: str, audio_probe: bool = True, arrangement: dict | None = None) -> dict:
    destination = Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if arrangement is not None:
        melody = dict(melody, _arrangement_file_sha256=hashlib.sha256(_json_bytes(arrangement)).hexdigest())
    package = _package_manifest(melody, video, preview, sample_status)
    public_melody = {key: value for key, value in melody.items() if not key.startswith('_') and key != 'arrangement'}
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.zip', delete=False) as temporary:
        archive_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('cogs/tts/cd_song_scores.json', _json_bytes([public_melody]))
            archive.writestr('cogs/tts/cd_song_videos.json', _json_bytes({'version': 1, 'songs': {melody['id']: video} if video else {}}))
            archive.writestr('cogs/tts/cd/previews/index.json', _json_bytes({melody['id']: preview} if preview else {}))
            archive.writestr(f"cogs/tts/cd/packages/{melody['id']}.json", _json_bytes(package))
            if arrangement is not None:
                archive.writestr(melody['arrangement_path'], _json_bytes(arrangement))
            if image is not None and video:
                archive.writestr(f"cogs/tts/cd/thumbnails/{video['video_id']}.jpg", image)
            if audio is not None and preview:
                archive.writestr(f"cogs/tts/cd/previews/{melody['id']}.mp3", audio)
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None:
                raise ValueError('zip_crc_mismatch')
        report = validate_package(archive_path, require_cd_only=True, audio_probe=bool(preview) and audio_probe)
        os.replace(archive_path, destination)
    finally:
        archive_path.unlink(missing_ok=True)
    report.update(zip_path=str(destination), zip_sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                  zip_bytes=destination.stat().st_size, package_manifest=package)
    return report


def export_song_package(root: Path, slug: str, export_zip: Path, *, sample_status: str | None = None,
                        audio_probe: bool = True) -> dict:
    """Export a complete snapshot of one local CD, also used for sample retries."""
    root = Path(root)
    songs, videos = open_catalog(root)
    melody = next((song for song in songs if song.get('id') == slug), None)
    if melody is None:
        raise ImportInputError('song_not_in_local_catalog')
    arrangement = None
    if melody.get('arrangement_path'):
        path = root / 'arrangements' / f'{slug}.json'
        arrangement = _read_json(path)
        melody = dict(melody, _arrangement_file_sha256=hashlib.sha256(_json_bytes(arrangement)).hexdigest())
    video = videos['songs'].get(slug)
    image = None
    if video and video.get('thumbnail_sha256'):
        path = root / 'thumbnails' / f"{video['video_id']}.jpg"
        if not path.is_file():
            raise ImportInputError('local_thumbnail_missing')
        image = path.read_bytes()
        if _verify_cover(image) != video['thumbnail_sha256']:
            raise ImportInputError('local_thumbnail_hash_mismatch')
    preview = _matching_preview(root, melody, _manifest(root).get(slug))
    audio = (root / 'previews' / f'{slug}.mp3').read_bytes() if preview else None
    return _export(melody, video, preview, image, audio, export_zip,
                   sample_status=sample_status or ('ready' if preview else 'pending_sample'), audio_probe=audio_probe, arrangement=arrangement)


async def import_midi(path: Path, *, root: Path = DATA_ROOT, cache_dir: Path = THUMB_ROOT,
                      candidates: Path | None = None, cover_file: Path | None = None,
                      render_sample: bool = True, export_zip: Path | None = None) -> dict:
    path, root, cache_dir = Path(path).expanduser(), Path(root).expanduser(), Path(cache_dir).expanduser()
    if (not path.is_file() or path.is_symlink() or path.suffix.lower() not in ('.mid', '.midi')
            or path.stat().st_size > MAX_MIDI):
        raise ImportInputError('invalid_midi_input')
    meta = _meta_from_path(path)
    extracted = extract_midi(path, track=meta['track_index'], channel=meta['channel'], start_ms=meta['start_ms'],
                             max_duration_ms=meta['max_duration_ms'], max_notes=meta['max_notes'])
    score, extraction = extracted['score'], extracted['extraction']
    from midi_extract import extract_arrangement
    from osaka_cd.arrangement import arrangement_sha256, preview_arrangement
    composed = extract_arrangement(path, track=meta['track_index'], channel=meta['channel'],
                                   max_parts=meta['max_parts'], include_bass=meta['include_bass'])
    arrangement = composed['arrangement']
    arrangement_preview = preview_arrangement(arrangement,
        start_ms=extraction['excerpt_start_ms'], end_ms=extraction['excerpt_end_ms'],
        max_duration_ms=meta['max_duration_ms'], max_notes=meta['max_notes'])
    selected_arrangement = arrangement_preview['arrangement']
    lead = next(part for part in selected_arrangement['parts'] if part['role'] == 'lead')
    score = dict(score, duration_ms=selected_arrangement['duration_ms'], notes=lead['notes'])
    extraction.update(arrangement_algorithm=composed.get('extraction', {}).get('algorithm'),
                      full_duration_ms=arrangement['duration_ms'], parts=len(arrangement['parts']),
                      excerpt_start_ms=arrangement_preview['start_ms'], excerpt_end_ms=arrangement_preview['end_ms'],
                      is_excerpt=arrangement_preview['is_excerpt'],
                      arrangement_warnings=composed.get('extraction', {}).get('warnings', []))
    if score.get('lyrics_source') == 'midi':
        extraction['embedded_lyrics'] = [{'note_index': index, 'lyric': note.get('lyric')}
                                        for index, note in enumerate(score['notes']) if note.get('lyric')]
    # CDs use the worker's message lyrics contract. Embedded/default vowels must
    # not silently override what a person asks the panel to sing. v7 scores also
    # lacked a lyric key, which keeps the supplied sample's canonical hash exact.
    score = {**score, 'lyrics_source': 'message', 'track_index': 0,
             'notes': [{key: note[key] for key in ('midi_note', 'start_ms', 'duration_ms', 'velocity')}
                       for note in score['notes']]}
    extraction['parser_score_sha256'] = extraction.get('score_sha256')
    extraction['score_sha256'] = score_sha256(score)
    extraction['score_normalization'] = 'worker-message-notes-v1'
    _validate_score(score)
    songs, videos = open_catalog(root)
    original = next((entry for entry in songs if entry.get('id') == meta['id']), None)
    previous_video = videos['songs'].get(meta['id'])
    meta = _inherit_aliases(meta, original, previous_video)
    if original is None and len(songs) >= 500:
        raise ImportInputError('catalog_full')
    melody = {'id': meta['id'], 'name': meta['title'], 'artist': meta['artist'] or 'Artista não identificado',
              'excerpt': 'Trecho melódico extraído; linha vocal ambígua' if extraction['vocal_status'] == 'ambiguous'
                         else 'Trecho indicado como canto pelo MIDI; transcrição não confirmada',
              'source_url': '', 'source_sha256': extraction['source_sha256'],
              'revision': original.get('revision', 1) if original else 1,
              'duration_ms': score['duration_ms'], 'notes': score['notes'], 'extraction': extraction}
    melody.update(arrangement_path=f"cogs/tts/cd/arrangements/{meta['id']}.json",
                  arrangement_sha256=arrangement_sha256(arrangement),
                  arrangement_preview_start_ms=arrangement_preview['start_ms'],
                  arrangement_preview_end_ms=arrangement_preview['end_ms'],
                  preview_arrangement_sha256=arrangement_sha256(selected_arrangement),
                  preview_performance_sha256=arrangement_performance_sha256(selected_arrangement),
                  arrangement_part_count=len(selected_arrangement['parts']))
    melody['_arrangement_file_sha256'] = hashlib.sha256(_json_bytes(arrangement)).hexdigest()
    try:
        from worker_config import configuration_fingerprint
        melody['_configuration_fingerprint'] = configuration_fingerprint()
    except Exception:
        pass
    melody['score_sha256'] = score_sha256(melody)
    if original and (score_sha256(original) != melody['score_sha256']
                     or original.get('arrangement_sha256') != melody['arrangement_sha256']):
        melody['revision'] += 1
    pending, selection_audit = [], None
    video = dict(previous_video) if isinstance(previous_video, dict) else None
    if video:
        from cd_metadata import match_candidate
        compatible, _ = match_candidate(meta['title'], meta['artist'], video, aliases=meta['aliases'],
                                         artist_aliases=meta['artist_aliases'], allow_versions=meta['allow_versions'])
        if not compatible:
            video = None
        elif video['video_id'] not in meta['seed_video_ids']:
            # Reverify the prior recording directly so a limited query cannot
            # silently lose it merely because its search rank changed.
            meta['seed_video_ids'] = [*meta['seed_video_ids'], video['video_id']]
    try:
        expected_duration = meta['expected_song_duration_seconds']
        if expected_duration is None and meta['source_is_full_song']:
            expected_duration = extraction.get('source_duration_ms', 0) / 1000
        search = await asyncio.to_thread(_search, meta, candidate_file=candidates, duration=expected_duration)
        selected, selection_audit = search['selected'], search['audit']
        video = _selected_video(meta, selected, selection_audit)
        if previous_video and previous_video.get('video_id') == video['video_id']:
            if previous_video.get('thumbnail_sha256'):
                video['thumbnail_sha256'] = previous_video['thumbnail_sha256']
    except (ImportInputError, MidiExtractionError):
        raise
    except Exception as exc:
        selection_audit = getattr(exc, 'audit', None)
        from cd_metadata import MetadataSearchError
        pending.append({'artifact': 'metadata', 'reason': str(exc) if isinstance(exc, MetadataSearchError)
                        else type(exc).__name__})
    if video is None and previous_video:
        # Retain the installed identity while a renamed/reidentified song is
        # unverified. An empty overlay map cannot delete a prior VPS cover.
        raise RuntimeError('metadata_identity_changed_search_pending')
    image = None
    if video:
        video.update(id=meta['id'], revision=melody['revision'], source_sha256=melody['source_sha256'],
                     score_sha256=melody['score_sha256'])
        melody['source_url'] = video.get('source_url') or video.get('webpage_url', '')
        old_cover = root / 'thumbnails' / f"{video['video_id']}.jpg"
        if old_cover.is_file() and video.get('thumbnail_sha256') == hashlib.sha256(old_cover.read_bytes()).hexdigest():
            image = old_cover.read_bytes()
        if cover_file is not None:
            try:
                image = Path(cover_file).expanduser().read_bytes()
            except OSError as exc:
                raise ImportInputError('override_cover_unreadable') from exc
            video['thumbnail_sha256'] = _verify_cover(image)
        elif image is None:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                image, video['thumbnail_sha256'] = await _cover_for(meta, video, cache_dir)
            except Exception as exc:
                video.pop('thumbnail_sha256', None)
                pending.append({'artifact': 'thumbnail', 'reason': type(exc).__name__})
    else:
        pending.append({'artifact': 'thumbnail', 'reason': 'pending_metadata'})
    manifest = _manifest(root)
    preview = _matching_preview(root, melody, manifest.get(meta['id']))
    audio = (root / 'previews' / f"{meta['id']}.mp3").read_bytes() if preview else None
    sample_status = 'ready' if preview else 'not_requested'
    if preview is None and render_sample:
        with tempfile.TemporaryDirectory(prefix='osaka-cd-sample-') as temporary:
            temporary_audio = Path(temporary) / 'preview.mp3'
            render_input = dict(melody, arrangement=selected_arrangement)
            sample_status = await asyncio.to_thread(_sample, render_input, temporary_audio)
            if render_input.get('_render_report'):
                melody['_render_report'] = render_input['_render_report']
            if sample_status == 'ready':
                if not temporary_audio.is_file() or temporary_audio.stat().st_size < 256:
                    sample_status = 'pending_empty_audio'
                else:
                    audio = temporary_audio.read_bytes()
                    preview = preview_manifest(melody, audio)
    if preview is None:
        sample_pending = {'artifact': 'sample', 'reason': sample_status}
        if melody.get('_render_report', {}).get('retryable') is False:
            sample_pending['retryable'] = False
        pending.append(sample_pending)
    destination = export_zip or root / 'exports' / f"{meta['id']}-r{melody['revision']}.zip"
    verification = _export(melody, video, preview, image, audio, destination, sample_status=sample_status, arrangement=arrangement)
    # Only validated artifacts become local truth. Rendering never writes existing audio.
    songs = [entry for entry in songs if entry.get('id') != meta['id']]
    if len(songs) >= 500:
        raise ImportInputError('catalog_full')
    public_melody = {key: value for key, value in melody.items() if not key.startswith('_')}
    songs.append(public_melody)
    _atomic(root / 'arrangements' / f"{meta['id']}.json", _json_bytes(arrangement))
    if video:
        videos['songs'][meta['id']] = video
    else:
        videos['songs'].pop(meta['id'], None)
    if image is not None and video:
        _atomic(root / 'thumbnails' / f"{video['video_id']}.jpg", image)
    manifest.pop(meta['id'], None)
    if preview:
        _atomic(root / 'previews' / f"{meta['id']}.mp3", audio)
        manifest[meta['id']] = preview
    _atomic(root / 'cd_song_scores.json', _json_bytes(songs))
    _atomic(root / 'cd_song_videos.json', _json_bytes(videos))
    _atomic(root / 'previews/index.json', _json_bytes(manifest))
    _atomic(root / 'packages' / f"{meta['id']}.json", _json_bytes(_package_manifest(melody, video, preview, sample_status)))
    return {'ok': True, 'complete': not pending, 'importer_version': IMPORTER_VERSION,
            'id': meta['id'], 'name': meta['title'],
            'revision': melody['revision'], 'notes': len(score['notes']), 'duration_ms': score['duration_ms'],
            'full_duration_ms': arrangement['duration_ms'], 'parts': len(arrangement['parts']),
            'arrangement_sha256': melody['arrangement_sha256'],
            'source_sha256': melody['source_sha256'], 'score_sha256': melody['score_sha256'],
            'video_id': video.get('video_id') if video else None, 'view_count': video.get('view_count') if video else None,
            'view_count_source': video.get('view_count_source') if video else None,
            'verified_candidates': video.get('inspected_candidates', 0) if video else 0,
            'selection_audit': selection_audit,
            'sample_status': sample_status, 'pending_artifacts': pending, 'extraction': extraction,
            'zip': verification['zip_path'], 'zip_sha256': verification['zip_sha256'],
            'zip_bytes': verification['zip_bytes'], 'validation': verification,
            'selection_note': 'Maior contagem entre candidatos compatíveis verificados; busca limitada, sem garantia global.'}
