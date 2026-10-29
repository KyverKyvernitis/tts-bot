"""Repair incomplete attachment imports using the normal metadata/cover importer.

Only installed Discord-imported CDs are considered. Musical files are inherited
unchanged, and every publication compares against the snapshot used for lookup.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
import zipfile

from .catalog_policy import is_retired_cd
from .discord_import import (_dump, _off_loop, _publish, _read, _seed_catalog,
                             _stop_process)
from .store import StoreError, _contract, get_store


_LOG = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parents[1]
_ID = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
_VIDEO = re.compile(r'[A-Za-z0-9_-]{11}\Z')
_PASSES = (0, 45, 180)
_PER_PASS = 5
_CHILD_TIMEOUT = 180
# Recover the metadata supplied with this exact earlier attachment. This is a
# one-file migration hint, not a title-based shortcut around provider checks.
_PREVIOUS_VAMPIRE_SOURCE = '41f8081e6c4d0ef424a18df0dc2de4d1ca919a9beb741b48c60f79d845072709'


def _key(value):
    value = unicodedata.normalize('NFKD', str(value or '')).casefold()
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in value
                           if not unicodedata.combining(c)).split())


def _known_artist(value):
    return isinstance(value, str) and bool(value.strip()) and _key(value) not in {
        'unknown', 'unknown artist', 'artista nao identificado', 'nao identificado'}


def _pending(snapshot, score):
    from .bundled import _has_valid_cover
    root = Path(snapshot['root'])
    video = _read(root / _contract().VIDEOS, {}).get('songs', {}).get(score['id'], {})
    return (not _known_artist(score.get('artist')) or not isinstance(video, dict)
            or not isinstance(video.get('selection_audit'), dict)
            or not _has_valid_cover(root, video, score['id']))


def _targets(store, limit=_PER_PASS):
    snapshot = store.snapshot(strict=True, advertise_legacy=True)
    scores = _read(Path(snapshot['root']) / _contract().SCORES, [])
    result = []
    for score in scores:
        identity = score.get('id') if isinstance(score, dict) else None
        if (not isinstance(identity, str) or not _ID.fullmatch(identity)
                or is_retired_cd(identity) or identity not in snapshot['versions']
                or not isinstance(score.get('discord_import'), dict)):
            continue
        if _pending(snapshot, score):
            result.append(identity)
            if len(result) == limit:
                break
    return result


def _seed(store, directory, identity):
    snapshot = store.snapshot(strict=True, advertise_legacy=True)
    score = next((row for row in _read(Path(snapshot['root']) / _contract().SCORES, [])
                  if row.get('id') == identity), None)
    if (score is None or identity not in snapshot['versions']
            or not isinstance(score.get('discord_import'), dict)
            or is_retired_cd(identity) or not _pending(snapshot, score)):
        return None
    _seed_catalog(snapshot, directory / 'catalog', identity)
    _dump(directory / 'request.json', {'id': identity})
    return snapshot


def _merge_strings(*groups):
    result, seen = [], set()
    for group in groups:
        for value in group if isinstance(group, (list, tuple)) else ():
            if not isinstance(value, str) or not 0 < len(value.strip()) <= 160:
                continue
            key = _key(value)
            if key and key not in seen:
                result.append(value.strip())
                seen.add(key)
    return result[:12]


def _previous_attachment_metadata(score, video):
    marker = score.get('discord_import')
    if (score.get('id') != 'the-vampire' or _key(score.get('name')) != 'the vampire'
            or score.get('source_sha256') != _PREVIOUS_VAMPIRE_SOURCE
            or not isinstance(marker, dict)
            or marker.get('source_sha256') != _PREVIOUS_VAMPIRE_SOURCE):
        return None
    lookup = marker.get('title_lookup')
    lookup = lookup.get('metadata', {}) if isinstance(lookup, dict) else {}
    for artist in (score.get('artist'), video.get('artist') if isinstance(video, dict) else None,
                   lookup.get('artist') if isinstance(lookup, dict) else None):
        if _known_artist(artist) and _key(artist) != _key('DECO*27'):
            return None  # A later owner-supplied attribution remains authoritative.
    return {'title': 'The Vampire', 'artist': 'DECO*27', 'aliases': ['ヴァンパイア'],
            'artist_aliases': [], 'seed_video_ids': ['e1xCOsgWG0M'],
            'search_query': 'DECO*27 ヴァンパイア'}


async def _prepare(directory):
    """Child only: discover identity, then use the existing artwork converter."""
    sys.path.insert(0, str(_ROOT / 'deploy/ttv-cd'))
    from catalog_import import repair_metadata
    from .title_metadata import resolve_title_metadata, TitleMetadataError

    directory = Path(directory)
    identity = _read(directory / 'request.json', {}).get('id')
    if not isinstance(identity, str) or not _ID.fullmatch(identity):
        raise StoreError('invalid repair identity', 'invalid_package')
    scores = _read(directory / 'catalog/cd_song_scores.json', [])
    if len(scores) != 1 or scores[0].get('id') != identity:
        raise StoreError('invalid repair source', 'invalid_package')
    score = scores[0]
    title, artist = score.get('name'), score.get('artist')
    if not isinstance(title, str) or not 0 < len(title.strip()) <= 100:
        raise StoreError('invalid repair title', 'invalid_metadata')
    video = _read(directory / 'catalog/cd_song_videos.json', {}).get('songs', {}).get(identity, {})
    aliases = video.get('title_aliases', []) if isinstance(video, dict) else []
    artist_aliases = video.get('artist_aliases', []) if isinstance(video, dict) else []
    seeds = [video['video_id']] if isinstance(video, dict) and _VIDEO.fullmatch(str(video.get('video_id') or '')) else []
    found = _previous_attachment_metadata(score, video)
    if found is None:
        try:
            found = resolve_title_metadata(title)
        except TitleMetadataError:
            if not _known_artist(artist):
                raise  # Automatic repair never makes an ambiguous selection.
            found = {}
    if found:
        found_artists = [found.get('artist'), *found.get('artist_aliases', [])]
        if _known_artist(artist) and _key(artist) not in {_key(name) for name in found_artists}:
            found = {}  # Keep an installed author authoritative over a namesake.
        else:
            artist = artist if _known_artist(artist) else found['artist']
    if not _known_artist(artist):
        raise StoreError('artist still unknown', 'metadata_unavailable')
    aliases = _merge_strings(aliases, found.get('aliases', []), [found.get('title')])
    artist_aliases = _merge_strings(artist_aliases, found.get('artist_aliases', []))
    seeds = list(dict.fromkeys([*seeds, *found.get('seed_video_ids', [])]))[:12]
    metadata = {'id': identity, 'title': title, 'artist': artist,
                'aliases': aliases, 'artist_aliases': artist_aliases,
                'seed_video_ids': seeds, 'search_query': found.get('search_query') or f'{title} {artist}'}
    result = await repair_metadata(identity, root=directory / 'catalog', metadata=metadata,
        cache_dir=directory / 'covers', export_zip=directory / 'package.zip')
    if not result.get('ok') or any(row.get('artifact') in {'metadata', 'thumbnail'}
                                for row in result.get('pending_artifacts', [])):
        raise StoreError('artwork is still pending', 'metadata_unavailable')
    _dump(directory / 'report.json', {'ok': True})


async def _run_repair_child(directory, *, timeout_seconds=_CHILD_TIMEOUT):
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'osaka_cd.import_artwork_repair',
        '--prepare', str(directory), cwd=str(_ROOT), stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            raise StoreError('artwork repair timeout', 'import_timeout') from None
        report = directory / 'report.json'
        if (process.returncode != 0 or report.is_symlink() or not report.is_file()
                or not 0 < report.stat().st_size <= 4096 or not _read(report, {}).get('ok')):
            raise StoreError('artwork repair unavailable', 'metadata_unavailable')
    finally:
        cleanup = asyncio.create_task(_stop_process(process))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError


def _verify_and_publish(store, package, snapshot, identity):
    contract = _contract()
    report = contract.validate_package(package, base_dir=snapshot['root'], require_cd_only=True, audio_probe=False)
    if report.get('songs') != [identity] or report['artifacts'][identity]['thumbnail'] != 'ready':
        raise StoreError('incomplete artwork repair', 'invalid_package')
    root = Path(snapshot['root'])
    original = next(row for row in _read(root / contract.SCORES, []) if row.get('id') == identity)
    with zipfile.ZipFile(package) as archive:
        contents = {rel: archive.read(info) for info, rel in contract._zip_members(archive)}
    scores = json.loads(contents[contract.SCORES])
    if len(scores) != 1 or scores[0].get('id') != identity:
        raise StoreError('repair changes another CD', 'invalid_package')
    repaired = scores[0]
    if ({key: value for key, value in original.items() if key not in {'artist', 'source_url'}}
            != {key: value for key, value in repaired.items() if key not in {'artist', 'source_url'}}):
        raise StoreError('repair changes musical source', 'invalid_package')
    videos = json.loads(contents[contract.VIDEOS]).get('songs', {})
    if set(videos) != {identity} or not isinstance(videos[identity].get('selection_audit'), dict):
        raise StoreError('repair metadata invalid', 'invalid_package')
    video_id = videos[identity].get('video_id')
    allowed = {contract.SCORES, contract.VIDEOS, contract.PREVIEWS,
               f'cogs/tts/cd/packages/{identity}.json',
               f'cogs/tts/cd/thumbnails/{video_id}.jpg'}
    for relative in [original.get('arrangement_path'), f'cogs/tts/cd/previews/{identity}.mp3']:
        if not relative:
            continue
        allowed.add(relative)
        if relative in contents and contents[relative] != (root / relative).read_bytes():
            raise StoreError('repair changes existing audio or arrangement', 'invalid_package')
    if set(contents) - allowed:
        raise StoreError('repair contains unrelated files', 'invalid_package')
    if contract.PREVIEWS in contents:
        incoming = json.loads(contents[contract.PREVIEWS])
        existing = _read(root / contract.PREVIEWS, {})
        if set(incoming) - {identity} or any(existing.get(key) != value for key, value in incoming.items()):
            raise StoreError('repair changes existing sample metadata', 'invalid_package')
    return _publish(store, package, snapshot)


async def _repair_one(store, identity):
    with tempfile.TemporaryDirectory(prefix='osaka-cd-artwork-repair-') as temporary:
        directory = Path(temporary)
        snapshot = await _off_loop(_seed, store, directory, identity)
        if snapshot is None:
            return 'ready'
        await _run_repair_child(directory)
        await _off_loop(_verify_and_publish, store, directory / 'package.zip', snapshot, identity)
    return 'repaired'


async def refresh_imported_cd_artwork(*, store=None):
    """Try a bounded set at startup; provider failures back off without messages."""
    try:
        store = store or await _off_loop(get_store)
        for delay in _PASSES:
            if delay:
                await asyncio.sleep(delay)
            identities = await _off_loop(_targets, store)
            if not identities:
                return
            for identity in identities:
                try:
                    status = await _repair_one(store, identity)
                    _LOG.info('[cd_import_artwork] id=%s status=%s', identity, status)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _LOG.info('[cd_import_artwork] id=%s status=deferred code=%s type=%s',
                        identity, getattr(exc, 'code', 'metadata_unavailable'), type(exc).__name__)
            if not await _off_loop(_targets, store):
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOG.info('[cd_import_artwork] status=deferred type=%s', type(exc).__name__)


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', required=True, type=Path)
    arguments = parser.parse_args()
    try:
        asyncio.run(_prepare(arguments.prepare))
        return 0
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(_main())
