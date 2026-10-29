"""Owner CD metadata edits with exact cover sources and immutable musical data.

Discord authorization belongs to the editor view. This service accepts no local
paths or arbitrary download URLs, and publishes against the version the owner
was shown before opening the modal.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from urllib.parse import parse_qs, urlsplit
import zipfile

from .catalog_policy import is_retired_cd
from .discord_import import (ImportCDError, MAX_SIDECAR_BYTES, _dump, _off_loop,
    _progress, _publish, _read, _seed_catalog, _stop_process, _wait_for_activation)
from .store import StoreError, _contract, get_store


_ROOT = Path(__file__).resolve().parents[1]
_ID = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
_VERSION = re.compile(r'[0-9a-f]{64}\Z')
_VIDEO = re.compile(r'[A-Za-z0-9_-]{11}\Z')
_VIDEO_HOSTS = frozenset({'youtube.com', 'www.youtube.com', 'm.youtube.com',
                        'music.youtube.com', 'youtu.be', 'www.youtu.be'})
_CHILD_TIMEOUT = 180
_MESSAGES = {
    'invalid_video_url': 'Use o link de um vídeo do YouTube, como youtube.com/watch?v=… ou youtu.be/… .',
    'invalid_edit': 'Confira o nome, o artista e o link do vídeo.',
    'cd_unavailable': 'Esse CD não está mais disponível. Abra o editor novamente.',
    'cover_unavailable': 'Não consegui preparar a capa desse vídeo. Confira o link e tente novamente.',
    'conflict': 'Esse CD mudou enquanto o editor estava aberto. Abra-o novamente para usar os dados atuais.',
    'import_timeout': 'A capa demorou para ficar pronta. Tente novamente em instantes.',
    'storage_unavailable': 'Não consegui salvar o CD agora. Os dados anteriores foram preservados.',
}


def _error(code='invalid_edit'):
    return ImportCDError(code, _MESSAGES.get(code, _MESSAGES['storage_unavailable']))


def parse_video_url(value):
    """Extract one public YouTube video ID without following any supplied URL."""
    if not isinstance(value, str):
        raise _error('invalid_video_url')
    value = value.strip(' ')
    if (not 0 < len(value) <= 2048 or any(character.isspace()
            or unicodedata.category(character).startswith('C') for character in value)):
        raise _error('invalid_video_url')
    if '://' not in value:
        value = 'https://' + value
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {'http', 'https'} or parts.hostname not in _VIDEO_HOSTS
                or parts.username is not None or parts.password is not None
                or parts.port is not None):
            raise ValueError('unsupported host')
        if parts.hostname in {'youtu.be', 'www.youtu.be'}:
            pieces = parts.path.split('/')
            video_id = pieces[1] if len(pieces) == 2 else ''
        elif parts.path == '/watch':
            query = parse_qs(parts.query, keep_blank_values=True)
            ids = query.get('v', [])
            video_id = ids[0] if len(ids) == 1 else ''
        else:
            pieces = parts.path.split('/')
            video_id = pieces[2] if (len(pieces) == 3 and pieces[1] in {'shorts', 'live', 'embed'}) else ''
        if not _VIDEO.fullmatch(video_id):
            raise ValueError('invalid video identity')
    except (ValueError, TypeError):
        raise _error('invalid_video_url') from None
    return video_id


def _text(value, *, limit, keep=None):
    if value is None or isinstance(value, str) and not value.strip():
        value = keep
    if (not isinstance(value, str) or not 0 < len(value.strip()) <= limit
            or any(unicodedata.category(character).startswith('C') for character in value)):
        raise _error('invalid_edit')
    return value.strip()


def _identity(identity):
    if (not isinstance(identity, str) or len(identity) > 90 or not _ID.fullmatch(identity)
            or is_retired_cd(identity) or identity == 'ttv-demo-v1'):
        raise _error('cd_unavailable')
    return identity


def list_editable_cds(*, store=None):
    """List public installed rows with server-owned optimistic edit versions."""
    store = store or get_store()
    snapshot = store.snapshot(strict=True, advertise_legacy=True)
    root = Path(snapshot['root'])
    contract = _contract()
    videos = _read(root / contract.VIDEOS, {}).get('songs', {})
    result = []
    for score in _read(root / contract.SCORES, []):
        identity = score.get('id') if isinstance(score, dict) else None
        if (not isinstance(identity, str) or is_retired_cd(identity)
                or identity == 'ttv-demo-v1' or identity not in snapshot['versions']):
            continue
        video = videos.get(identity)
        video = video if isinstance(video, dict) else {}
        source = video.get('cover_source')
        source = source if isinstance(source, dict) else {}
        video_id = source.get('video_id') if source.get('selection') == 'owner' else video.get('video_id')
        video_url = ('https://www.youtube.com/watch?v=' + video_id
                     if isinstance(video_id, str) and _VIDEO.fullmatch(video_id) else '')
        result.append({'id': identity, 'name': score.get('name') or identity,
            'artist': score.get('artist') or 'Artista não identificado',
            'video_url': video_url, 'source_url': score.get('source_url') or video_url,
            'version': snapshot['versions'][identity],
            'notes': len(score.get('notes') or []), 'duration_ms': score.get('duration_ms') or 0})
    return tuple(sorted(result, key=lambda row: (row['name'].casefold(), row['id'])))


def _seed_edit(store, directory, identity, expected_version, title, artist, video_url):
    identity = _identity(identity)
    if not isinstance(expected_version, str) or not _VERSION.fullmatch(expected_version):
        raise _error('conflict')
    snapshot = store.snapshot(strict=True, advertise_legacy=True)
    if snapshot['versions'].get(identity) != expected_version:
        raise _error('conflict')
    root = Path(snapshot['root'])
    contract = _contract()
    original = next((row for row in _read(root / contract.SCORES, []) if row.get('id') == identity), None)
    if original is None:
        raise _error('cd_unavailable')
    name = _text(title, limit=100, keep=original['name'])
    credit = _text(artist, limit=160, keep=original.get('artist') or 'Artista não identificado')
    if video_url is not None and not isinstance(video_url, str):
        raise _error('invalid_video_url')
    video_id = parse_video_url(video_url) if isinstance(video_url, str) and video_url.strip() else None
    video = _read(root / contract.VIDEOS, {}).get('songs', {}).get(identity)
    video = video if isinstance(video, dict) else {}
    aliases = list(video.get('title_aliases') or [])
    if name != original['name']:
        aliases.append(original['name'])
    metadata = {'id': identity, 'title': name, 'artist': credit,
                'aliases': list(dict.fromkeys(value for value in aliases
                    if isinstance(value, str) and 0 < len(value.strip()) <= 160))[:12],
                'artist_aliases': list(video.get('artist_aliases') or [])[:12]}
    if video_id is not None:
        metadata['cover_video_id'] = video_id
    _seed_catalog(snapshot, directory / 'catalog', identity)
    _dump(directory / 'request.json', {'id': identity, 'metadata': metadata,
        'display_changed': name != original['name'] or credit != original.get('artist')})
    return snapshot


async def _prepare(directory):
    """Isolated child: the existing importer owns video lookup and conversion."""
    sys.path.insert(0, str(_ROOT / 'deploy/ttv-cd'))
    from catalog_import import repair_metadata, _metadata_overlay, _verify_cover
    directory = Path(directory)
    request = _read(directory / 'request.json', {})
    identity = _identity(request.get('id'))
    metadata = request.get('metadata')
    if not isinstance(metadata, dict):
        raise _error('invalid_edit')
    # A text edit should still work while the video provider is unavailable.
    # Reuse only the validated installed JPEG; never fabricate new metadata or
    # silently substitute a different recording for a blank source field.
    if not metadata.get('cover_video_id'):
        root = directory / 'catalog'
        songs = _read(root / 'cd_song_scores.json', [])
        previous = _read(root / 'cd_song_videos.json', {}).get('songs', {}).get(identity)
        original = next((row for row in songs if row.get('id') == identity), None)
        video_id = previous.get('video_id') if isinstance(previous, dict) else None
        if original and isinstance(video_id, str) and _VIDEO.fullmatch(video_id):
            cover = root / 'thumbnails' / (video_id + '.jpg')
            if cover.is_file() and not cover.is_symlink():
                image = cover.read_bytes()
                checksum = hashlib.sha256(image).hexdigest()
                if previous.get('thumbnail_sha256') == checksum:
                    _verify_cover(image)
                    song = dict(original, name=metadata['title'], artist=metadata['artist'])
                    video = dict(previous, artist=metadata['artist'], owner_edit={'display': True})
                    if not isinstance(video.get('selection_audit'), dict):
                        # Older curated CDs predate provider audit records. An
                        # owner text edit reuses their verified installed image;
                        # it does not establish fresh search or view statistics.
                        video['selection_audit'] = {
                            'scope': 'owner-kept-installed-cover',
                            'video_id': video_id, 'kept_existing_cover': True}
                        video['selection_basis'] = 'owner-kept-installed-cover'
                    video['title_aliases'] = list(dict.fromkeys([
                        *video.get('title_aliases', []), original['name'], metadata['title']]))[:12]
                    _metadata_overlay(root, song, video, image, directory / 'package.zip')
                    _dump(directory / 'report.json', {'ok': True})
                    return
    result = await repair_metadata(identity, root=directory / 'catalog', metadata=metadata,
        cache_dir=directory / 'covers', export_zip=directory / 'package.zip', edit_display=True)
    if not result.get('ok') or result.get('pending_artifacts'):
        raise _error('cover_unavailable')
    _dump(directory / 'report.json', {'ok': True})


async def _run_editor(directory, *, timeout_seconds=_CHILD_TIMEOUT):
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'osaka_cd.discord_edit',
        '--prepare', str(directory), cwd=str(_ROOT), stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            raise _error('import_timeout') from None
        path = directory / 'report.json'
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_SIDECAR_BYTES:
            raise _error('cover_unavailable')
        report = _read(path, {})
        if not isinstance(report, dict) or process.returncode != 0 or report.get('ok') is not True:
            code = report.get('code') if isinstance(report, dict) else None
            raise _error(code if code in _MESSAGES else 'cover_unavailable')
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


def _verify_and_publish(store, package, snapshot, identity, metadata):
    """Only display strings and this CD's image/recording may be edited."""
    contract = _contract()
    report = contract.validate_package(package, base_dir=snapshot['root'], require_cd_only=True, audio_probe=False)
    if report.get('songs') != [identity] or report['artifacts'][identity]['thumbnail'] != 'ready':
        raise _error('cover_unavailable')
    original = next(row for row in _read(Path(snapshot['root']) / contract.SCORES, []) if row['id'] == identity)
    with zipfile.ZipFile(package) as archive:
        contents = {relative: archive.read(info) for info, relative in contract._zip_members(archive)}
    scores = json.loads(contents[contract.SCORES])
    if len(scores) != 1 or scores[0].get('id') != identity:
        raise _error('invalid_edit')
    incoming = scores[0]
    allowed_fields = {'name', 'artist', 'source_url'}
    if ({key: value for key, value in incoming.items() if key not in allowed_fields}
            != {key: value for key, value in original.items() if key not in allowed_fields}
            or incoming.get('name') != metadata['title'] or incoming.get('artist') != metadata['artist']):
        raise _error('invalid_edit')
    manifest = json.loads(contents[contract.VIDEOS])
    videos = manifest.get('songs', {})
    video = videos.get(identity)
    if set(videos) != {identity} or not isinstance(video, dict) or not isinstance(video.get('selection_audit'), dict):
        raise _error('invalid_edit')
    video_id = video.get('video_id')
    if not isinstance(video_id, str) or not _VIDEO.fullmatch(video_id):
        raise _error('cover_unavailable')
    selected_id = metadata.get('cover_video_id')
    if selected_id and (video_id != selected_id or video.get('cover_source') != {
            'selection': 'owner', 'video_id': selected_id}):
        raise _error('invalid_edit')
    # Reject even byte-identical musical payloads: an editor has no reason to
    # write audio, arrangements, previews, packages, or another CD's records.
    if set(contents) != {contract.SCORES, contract.VIDEOS,
            f'cogs/tts/cd/thumbnails/{video_id}.jpg'}:
        raise _error('invalid_edit')
    if incoming['name'] != original['name'] or incoming['artist'] != original.get('artist'):
        video['owner_edit'] = {'display': True}
        contents[contract.VIDEOS] = json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode()
        with zipfile.ZipFile(package, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for relative, data in contents.items():
                archive.writestr(relative, data)
    return _publish(store, package, snapshot)


async def edit_cd(identity, *, title=None, artist=None, video_url=None,
                  expected_version, progress=None, store=None):
    """Edit an installed CD while retaining the exact music and history."""
    try:
        store = store or await _off_loop(get_store)
        await _progress(progress, 'preparing')
        with tempfile.TemporaryDirectory(prefix='osaka-cd-editor-') as temporary:
            directory = Path(temporary)
            snapshot = await _off_loop(_seed_edit, store, directory, identity,
                                      expected_version, title, artist, video_url)
            metadata = await _off_loop(_read, directory / 'request.json', {})
            await _progress(progress, 'artwork')
            await _run_editor(directory)
            await _progress(progress, 'installing')
            result = await _off_loop(_verify_and_publish, store, directory / 'package.zip',
                                    snapshot, identity, metadata['metadata'])
        await _progress(progress, 'activating')
        return await _wait_for_activation(store, result)
    except asyncio.CancelledError:
        raise
    except ImportCDError:
        raise
    except StoreError as exc:
        raise _error(exc.code if exc.code in _MESSAGES else 'storage_unavailable') from None
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile):
        raise _error('storage_unavailable') from None


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', required=True, type=Path)
    directory = parser.parse_args().prepare
    try:
        asyncio.run(_prepare(directory))
        return 0
    except Exception as exc:
        code = getattr(exc, 'code', 'cover_unavailable')
        _dump(directory / 'report.json', {'ok': False,
            'code': code if code in _MESSAGES else 'cover_unavailable'})
        return 1


if __name__ == '__main__':
    raise SystemExit(_main())
