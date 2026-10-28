"""Install explicitly shipped CDs through the existing atomic catalog store.

This runs once during bot startup. Runtime readers remain read-only, and an
existing song always wins over a shipped default with the same identity.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
from pathlib import Path
import re
import tempfile
import zipfile

from .store import StoreError, get_store


_LOG = logging.getLogger(__name__)
_MAX_PACKAGE_BYTES = 2 * 1024 * 1024
_PACKAGE = Path(__file__).with_name('bundled') / 'ievan-polkka.zip'
_PACKAGE_SHA256 = 'b17ddcd5954fc19eb55249b933a8ba59016516a88d2384382c23b483b1f1f4bd'
_SONG_ID = 'ievan-polkka'
_ORIGINAL_MUSIC = (
    '0bfdb90f44fcbfba92eb81f07703d68a16ded9a7703c8a3a4df30e0ddd163d7c',
    '1a586270fa65d5b839aa6be21cb297bbc0f0df4d2e222b48b33d401a4daca678',
    '3f95bb0c26ddbfda87d9ebe1a0218a26e61c7311a106a2ac67e060372f3351bf',
)
_ORIGINAL_VIDEO = 'kbbA9BhCTko'
_ORIGINAL_METADATA = 'historical-reference-to-original-Miku-arrangement'
_VIDEO = re.compile(r'[A-Za-z0-9_-]{11}\Z')
_ARTWORK_POLICY = 'ievan-polkka-existing-importer-v1'


def _verified_bytes(package, checksum):
    package = Path(package)
    if (package.is_symlink() or not package.is_file()
            or not 0 < package.stat().st_size <= _MAX_PACKAGE_BYTES):
        raise StoreError('bundled CD package missing or invalid', 'configuration')
    raw = package.read_bytes()
    if hashlib.sha256(raw).hexdigest() != checksum:
        raise StoreError('bundled CD checksum mismatch', 'configuration')
    return raw


def install_missing_cd(store, *, identity, package, sha256):
    """Apply one trusted package only while its song is still absent.

    The compare-and-swap is enforced again by RuntimeStore inside its commit
    lock, so a simultaneous user publication cannot be overwritten.
    """
    current = store.snapshot(strict=True, advertise_legacy=True)
    if identity in current['versions']:
        return {'id': identity, 'status': 'existing'}
    raw = _verified_bytes(package, sha256)
    # Keep the startup migration restricted to its registered song even if an
    # accidentally rebuilt package contains a wider catalog snapshot.
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        scores = json.loads(archive.read('cogs/tts/cd_song_scores.json'))
        if (not isinstance(scores, list) or len(scores) != 1
                or not isinstance(scores[0], dict) or scores[0].get('id') != identity):
            raise StoreError('bundled CD identity mismatch', 'configuration')
    receipt = store.receive(package, sha256, expected_versions={identity: None})
    result = store.process(receipt['job_id'])
    if result.get('applied'):
        return {'id': identity, 'status': result['state'],
                'generation': result['generation'], 'job_id': receipt['job_id']}
    # A user may have installed the same identity after the initial snapshot.
    if result.get('error_code') == 'conflict':
        latest = store.snapshot(strict=True, advertise_legacy=True)
        if identity in latest['versions']:
            return {'id': identity, 'status': 'existing'}
    raise StoreError('bundled CD installation deferred',
                     result.get('error_code') or 'storage_unavailable')


def _catalog_json(root, relative, default):
    path = root / relative
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


def _has_valid_cover(root, metadata, identity):
    video, expected = metadata.get('video_id'), metadata.get('thumbnail_sha256')
    if (not isinstance(video, str) or not _VIDEO.fullmatch(video)
            or not isinstance(expected, str)):
        return False
    path = root / 'cogs/tts/cd/thumbnails' / f'{video}.jpg'
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= _MAX_PACKAGE_BYTES:
        return False
    image = path.read_bytes()
    if hashlib.sha256(image).hexdigest() != expected:
        return False
    from .store import _contract
    try:
        _contract()._jpeg(image, identity)
    except ValueError:
        return False
    return True


def _artwork_repair_state(store, identity):
    """Inspect only local catalog data; a current custom cover always wins."""
    from .store import _contract
    selected = store.snapshot(strict=True, advertise_legacy=True)
    version = selected['versions'].get(identity)
    if version is None:
        return selected, None, 'song_missing'
    root = Path(selected['root'])
    score = next((item for item in _catalog_json(root, 'cogs/tts/cd_song_scores.json', [])
                  if item.get('id') == identity), None)
    if (identity != _SONG_ID or not isinstance(score, dict)
            or (score.get('source_sha256'), _contract().score_sha256(score),
                score.get('arrangement_sha256')) != _ORIGINAL_MUSIC):
        return selected, None, 'custom_song'
    current = (_catalog_json(root, 'cogs/tts/cd_song_videos.json', {})
               .get('songs') or {}).get(identity) or {}
    if _has_valid_cover(root, current, identity):
        return selected, current, 'cover_ready'
    known_incomplete = (current.get('video_id') == _ORIGINAL_VIDEO
                        and current.get('selection_basis') == _ORIGINAL_METADATA
                        and not current.get('thumbnail_sha256'))
    ownership = current.get('bundled_artwork_origin') or {}
    owned_missing = (isinstance(ownership, dict)
                     and ownership.get('policy') == _ARTWORK_POLICY
                     and ownership.get('video_id') == current.get('video_id')
                     and ownership.get('thumbnail_sha256') == current.get('thumbnail_sha256'))
    return selected, current, 'repair' if known_incomplete or owned_missing else 'custom_metadata'


def bundled_cd_needs_artwork(store):
    """Cheap startup gate; no provider search or network happens here."""
    return _artwork_repair_state(store, _SONG_ID)[2] == 'repair'


def repair_cd_metadata(store, *, identity, package, sha256):
    """Fill the original shipped CD's cover without replacing its music/audio.

    A metadata-only overlay inherits the current score, arrangement and sample.
    The registered original music and metadata identify the old incomplete
    bundle; custom songs and covers remain authoritative.
    """
    from .store import _contract
    raw = _verified_bytes(package, sha256)
    # The provider package is produced by the existing importer, not a pinned
    # ready-made image. Validate its public artifacts before deriving an overlay.
    _contract().validate_package(package, require_cd_only=True, audio_probe=False)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        scores = json.loads(archive.read('cogs/tts/cd_song_scores.json'))
        videos = json.loads(archive.read('cogs/tts/cd_song_videos.json'))
        if (not isinstance(scores, list) or len(scores) != 1
                or not isinstance(scores[0], dict) or scores[0].get('id') != identity):
            raise StoreError('bundled CD identity mismatch', 'configuration')
        desired = (videos.get('songs') or {}).get(identity)
        if (identity != _SONG_ID or not isinstance(desired, dict)
                or not isinstance(desired.get('video_id'), str)
                or not _VIDEO.fullmatch(desired['video_id'])
                or not isinstance(desired.get('thumbnail_sha256'), str)):
            return {'id': identity, 'status': 'existing', 'reason': 'no_metadata_repair'}
        cover_path = f"cogs/tts/cd/thumbnails/{desired['video_id']}.jpg"
        image = archive.read(cover_path)
        if hashlib.sha256(image).hexdigest() != desired['thumbnail_sha256']:
            raise StoreError('bundled CD cover checksum mismatch', 'configuration')
        _contract()._jpeg(image, identity)
        desired = dict(desired, bundled_artwork_origin={
            'policy': _ARTWORK_POLICY, 'video_id': desired['video_id'],
            'thumbnail_sha256': desired['thumbnail_sha256']})
    for attempt in range(3):
        selected, current, state = _artwork_repair_state(store, identity)
        version = selected['versions'].get(identity)
        if state != 'repair':
            return {'id': identity, 'status': 'existing', 'reason': state}
        # The header is outside song metadata/versioning. It keeps a repair
        # tied to this snapshot, so a previous failed CAS (or lost image after
        # an applied repair) cannot strand the same semantic job forever.
        overlay = {'version': videos.get('version', 1), 'songs': {identity: desired},
                   'bundle_metadata_repair': {'id': identity,
                       'base_generation': selected.get('generation'), 'base_version': version}}
        with tempfile.TemporaryDirectory(prefix='osaka-cd-metadata-') as folder:
            path = Path(folder) / 'metadata.zip'
            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('cogs/tts/cd_song_videos.json',
                    json.dumps(overlay, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
                archive.writestr(cover_path, image)
            receipt = store.receive(path, expected_versions={identity: version}, retry_failed=True)
        result = store.process(receipt['job_id'])
        if result.get('applied'):
            return {'id': identity, 'status': result['state'], 'metadata_updated': True,
                    'generation': result['generation'], 'job_id': receipt['job_id']}
        if result.get('error_code') != 'conflict':
            raise StoreError('bundled CD metadata repair deferred',
                             result.get('error_code') or 'storage_unavailable')
    raise StoreError('bundled CD metadata changed during repair', 'conflict')


def install_bundled_cds():
    """A failed optional addition cannot prevent independent bot startup."""
    try:
        store = get_store()
        result = install_missing_cd(store, identity=_SONG_ID,
                                    package=_PACKAGE, sha256=_PACKAGE_SHA256)
        _LOG.info('[cd_bundle] id=%s status=%s', _SONG_ID, result['status'])
        return result
    except Exception as exc:
        code = getattr(exc, 'code', 'configuration')
        _LOG.warning('[cd_bundle] id=%s code=%s type=%s',
                     _SONG_ID, code, type(exc).__name__)
        return {'id': _SONG_ID, 'status': 'deferred', 'error_code': code}


async def _off_loop(callback, *args, **kwargs):
    """Keep SQLite/file work off the loop and drain it before cancellation.

    Startup owns this physical transaction until completion. No detached task
    can continue changing the catalog after its cog has finished unloading.
    """
    physical = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
    try:
        return await asyncio.shield(physical)
    except asyncio.CancelledError:
        while not physical.done():
            try:
                await asyncio.shield(physical)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Observe the outcome even if a test or a future installer raises an
        # unexpected exception instead of returning the compact failure result.
        if not physical.cancelled():
            physical.exception()
        raise


async def initialize_bundled_cds():
    """Install only the local music package before starting catalog readers."""
    return await _off_loop(install_bundled_cds)


def _store_needing_artwork():
    store = get_store()
    return store, bundled_cd_needs_artwork(store)


def _apply_imported_artwork(store, package):
    package = Path(package)
    checksum = hashlib.sha256(package.read_bytes()).hexdigest()
    return repair_cd_metadata(store, identity=_SONG_ID, package=package, sha256=checksum)


async def refresh_bundled_cd_artwork():
    """Use the normal importer in a managed background task after startup.

    Short local checks avoid all provider work when a cover is already usable.
    Network failures can retry without delaying Discord command registration.
    """
    from .bundled_artwork import imported_artwork_package
    for attempt, delay in enumerate((0, 60, 180), 1):
        if delay:
            await asyncio.sleep(delay)
        try:
            store, needed = await _off_loop(_store_needing_artwork)
            if not needed:
                return {'id': _SONG_ID, 'status': 'existing'}
            async with imported_artwork_package() as package:
                result = await _off_loop(_apply_imported_artwork, store, package)
            _LOG.info('[cd_artwork] id=%s status=%s', _SONG_ID, result['status'])
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = getattr(exc, 'code', 'artwork_unavailable')
            if not isinstance(code, str) or not re.fullmatch(r'[a-z_]{1,64}', code):
                code = 'artwork_unavailable'
            _LOG.warning('[cd_artwork] id=%s attempt=%s code=%s type=%s',
                         _SONG_ID, attempt, code, type(exc).__name__)
    return {'id': _SONG_ID, 'status': 'deferred', 'error_code': 'artwork_unavailable'}
