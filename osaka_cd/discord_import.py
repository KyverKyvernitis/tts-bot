"""Bounded Discord CD imports through the existing importer and atomic store.

Discord authorization and presentation belong to the command. This module never
runs uploaded code, changes Git, or renders a sample during an import.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, replace
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
import unicodedata
import zipfile

from .store import StoreError, get_store, _contract

MAX_MIDI_BYTES = 8 * 1024 * 1024
MAX_ZIP_BYTES = 25 * 1024 * 1024
MAX_SIDECAR_BYTES = 64 * 1024
_ROOT = Path(__file__).resolve().parents[1]
_ACTIVATION_TIMEOUT = 8.0
_SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
_VIDEO = re.compile(r'[A-Za-z0-9_-]{11}\Z')
_ERRORS = {
    'invalid_input': 'Envie um arquivo MIDI ou um pacote de CD válido.',
    'file_too_large': 'O arquivo ultrapassa o limite: MIDI 8 MiB, ZIP 25 MiB ou JSON 64 KiB.',
    'invalid_sidecar': 'O JSON de configuração está inválido. Confira os dados e tente novamente.',
    'invalid_metadata': 'Confira o título, o artista e o trecho selecionado.',
    'invalid_midi': 'Não consegui ler notas nesse MIDI. Confira o arquivo e o trecho escolhido.',
    'invalid_package': 'Esse ZIP não é um pacote válido de CD.',
    'import_timeout': 'A importação demorou demais. Tente novamente em instantes.',
    'import_unavailable': 'O importador não conseguiu preparar esse CD. Confira os dados e tente novamente.',
    'conflict': 'Esse CD mudou durante a importação. Envie o comando novamente para usar a versão atual.',
    'storage_unavailable': 'Não consegui salvar o CD agora. O catálogo anterior foi preservado.',
    'storage_quota': 'Não há espaço disponível para salvar esse CD.',
    'superseded': 'Uma versão mais recente desse CD já foi publicada. Envie o comando novamente.',
}


class ImportCDError(ValueError):
    def __init__(self, code='invalid_input', public_message=None):
        self.code = code if code in _ERRORS or public_message else 'import_unavailable'
        self.public_message = public_message or _ERRORS[self.code]
        super().__init__(self.public_message)


@dataclass(frozen=True)
class PreparedInput:
    kind: str
    filename: str
    data: bytes
    sidecar: bytes | None = None


@dataclass(frozen=True)
class ImportResult:
    songs: tuple[dict, ...]
    pending: tuple[str, ...] = ()
    already_installed: bool = False
    applied: bool = False
    generation: str | None = None
    state: str = 'applied'
    job_id: str | None = None


def _json(data, code='invalid_sidecar'):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ImportCDError(code) from None


def prepare_input(filename, data, sidecar=None):
    if (not isinstance(filename, str) or not filename or len(filename) > 240
            or filename in {'.', '..'} or '/' in filename or '\\' in filename
            or any(unicodedata.category(c).startswith('C') for c in filename)
            or not isinstance(data, bytes) or not data):
        raise ImportCDError('invalid_input')
    extension = Path(filename).suffix.lower()
    kind = 'midi' if extension in {'.mid', '.midi'} else 'zip' if extension == '.zip' else None
    if kind is None:
        raise ImportCDError('invalid_input')
    if len(data) > (MAX_MIDI_BYTES if kind == 'midi' else MAX_ZIP_BYTES):
        raise ImportCDError('file_too_large')
    if kind == 'midi' and not data.startswith(b'MThd'):
        raise ImportCDError('invalid_midi')
    if kind == 'zip' and not data.startswith(b'PK\x03\x04'):
        raise ImportCDError('invalid_package')
    if sidecar is not None:
        if kind != 'midi' or not isinstance(sidecar, bytes):
            raise ImportCDError('invalid_sidecar')
        if not 0 < len(sidecar) <= MAX_SIDECAR_BYTES:
            raise ImportCDError('file_too_large')
        if not isinstance(_json(sidecar), dict):
            raise ImportCDError('invalid_sidecar')
    return PreparedInput(kind, filename, data, sidecar)


def inspect_input(prepared, overrides=None):
    prepared = prepare_input(prepared.filename, prepared.data, prepared.sidecar)
    if prepared.kind == 'zip':
        if overrides:
            raise ImportCDError('invalid_metadata')
        return {'kind': 'zip'}
    metadata = _json(prepared.sidecar) if prepared.sidecar else {}
    if overrides is not None and not isinstance(overrides, dict):
        raise ImportCDError('invalid_metadata')
    allowed = {'id', 'title', 'artist', 'aliases', 'artist_aliases', 'allow_versions',
               'search_query', 'seed_video_ids', 'expected_song_duration_seconds',
               'source_is_full_song', 'max_parts', 'include_bass', 'track_index',
               'channel', 'start_ms', 'max_duration_ms', 'max_notes'}
    metadata = {key: value for key, value in metadata.items() if key in allowed}
    overrides = overrides or {}
    if any(key not in allowed for key in overrides):
        raise ImportCDError('invalid_metadata')
    metadata.update(overrides)
    stem = Path(prepared.filename).stem.replace('_', ' ').strip()
    artist, title = stem.split(' - ', 1) if ' - ' in stem else ('', stem)
    for field, inferred, limit in [('title', title, 100), ('artist', artist, 160)]:
        value = metadata.get(field, inferred)
        if not isinstance(value, str) or len(value.strip()) > limit or any(
                unicodedata.category(c).startswith('C') for c in value):
            raise ImportCDError('invalid_metadata')
        metadata[field] = value.strip()
    if not metadata['title']:
        raise ImportCDError('invalid_metadata')
    digest = hashlib.sha256(prepared.data).hexdigest()
    if 'id' not in metadata:
        ascii_title = unicodedata.normalize('NFKD', metadata['title']).encode('ascii', 'ignore').decode().lower()
        slug = re.sub('[^a-z0-9]+', '-', ascii_title).strip('-')[:90].rstrip('-')
        loses_letters = any(c.isalpha() and not c.isascii() and not
            unicodedata.normalize('NFKD', c).encode('ascii', 'ignore') for c in metadata['title'])
        metadata['id'] = ('cd-' + digest[:16]) if loses_letters or not slug else slug
    if not isinstance(metadata['id'], str) or not _SLUG.fullmatch(metadata['id']) or len(metadata['id']) > 90:
        raise ImportCDError('invalid_metadata')
    # Match the shared importer, with a compact default excerpt for Discord.
    for field, lower, upper, default in [
        ('track_index', 0, 63, None), ('channel', 0, 15, None),
        ('start_ms', 0, 2147483647, None), ('max_duration_ms', 40, 60000, 30000),
        ('max_notes', 1, 128, 128), ('max_parts', 1, 3, 3),
    ]:
        value = metadata.get(field, default)
        if value is not None and (type(value) is not int or not lower <= value <= upper):
            raise ImportCDError('invalid_metadata')
        if value is not None:
            metadata[field] = value
    for field, default in [('include_bass', True), ('source_is_full_song', False)]:
        if type(metadata.get(field, default)) is not bool:
            raise ImportCDError('invalid_metadata')
        metadata[field] = metadata.get(field, default)
    for field in ('aliases', 'artist_aliases', 'allow_versions', 'seed_video_ids'):
        values = metadata.get(field, [])
        if (not isinstance(values, list) or len(values) > 12 or any(
                not isinstance(value, str) or not 0 < len(value.strip()) <= 160 for value in values)):
            raise ImportCDError('invalid_metadata')
        if field == 'seed_video_ids' and any(not _VIDEO.fullmatch(value) for value in values):
            raise ImportCDError('invalid_metadata')
        metadata[field] = [value.strip() for value in values]
    if 'search_query' in metadata and (not isinstance(metadata['search_query'], str)
            or not 0 < len(metadata['search_query'].strip()) <= 320):
        raise ImportCDError('invalid_metadata')
    if 'expected_song_duration_seconds' in metadata:
        value = metadata['expected_song_duration_seconds']
        if type(value) not in (int, float) or not 1 <= value <= 3600:
            raise ImportCDError('invalid_metadata')
    return dict(metadata, kind='midi')


def _dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')


def _read(path, default):
    return _json(path.read_bytes(), 'invalid_package') if path.is_file() and not path.is_symlink() else default


def _request_hash(prepared, metadata):
    return hashlib.sha256(json.dumps({'policy': 'discord-midi-v1',
        'source': hashlib.sha256(prepared.data).hexdigest(), 'metadata': metadata},
        sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _song_summary(score):
    return {key: value for key, value in {
        'id': score['id'], 'name': score['name'], 'artist': score['artist'],
        'notes': len(score['notes']), 'duration_ms': score['duration_ms'],
        'parts': score.get('arrangement_part_count', 1),
    }.items()}


def _resolve_identity(snapshot, prepared, metadata, overrides):
    explicit = ('id' in (_json(prepared.sidecar) if prepared.sidecar else {})
                or 'id' in (overrides or {}))
    if explicit:
        return metadata
    current = next((row for row in _read(Path(snapshot['root']) / _contract().SCORES, [])
                    if row.get('id') == metadata['id']), None)
    if not current or current.get('source_sha256') == hashlib.sha256(prepared.data).hexdigest():
        return metadata
    def key(value):
        return unicodedata.normalize('NFKC', str(value or '')).casefold().strip()
    unrelated = key(current.get('name')) != key(metadata['title']) or (
        bool(metadata['artist']) and key(current.get('artist')) != key(metadata['artist']))
    if unrelated:
        return dict(metadata, id=metadata['id'][:72].rstrip('-') + '-' + hashlib.sha256(prepared.data).hexdigest()[:16])
    return metadata


def _marker(score, request_hash):
    return {'version': 1, 'request_sha256': request_hash,
        'source_sha256': score.get('source_sha256'), 'score_sha256': _contract().score_sha256(score),
        'arrangement_sha256': score.get('arrangement_sha256'),
        'name': score.get('name'), 'artist': score.get('artist')}


def _existing_midi(snapshot, metadata, request_hash):
    contract = _contract()
    root = Path(snapshot['root'])
    score = next((row for row in _read(root / contract.SCORES, []) if row.get('id') == metadata['id']), None)
    if (not score or not isinstance(score.get('discord_import'), dict)
            or score['discord_import'] != _marker(score, request_hash)):
        return None
    try:
        report = contract.validate_cd_tree(root, [metadata['id']], audio_probe=False)
        video = _read(root / contract.VIDEOS, {}).get('songs', {}).get(metadata['id'], {})
        if (report['artifacts'][metadata['id']]['thumbnail'] != 'ready'
                or not video.get('selection_audit')):
            return None
    except (ValueError, OSError, KeyError, TypeError):
        return None
    return score


def _seed_catalog(snapshot, destination, identity):
    """Copy only this CD into the importer's private layout; never hard-link."""
    contract = _contract()
    source = Path(snapshot['root'])
    scores = [row for row in _read(source / contract.SCORES, []) if row.get('id') == identity]
    videos = _read(source / contract.VIDEOS, {}).get('songs', {})
    video = videos.get(identity)
    _dump(destination / 'cd_song_scores.json', scores)
    _dump(destination / 'cd_song_videos.json', {'version': 1, 'songs': {identity: video} if video else {}})
    _dump(destination / 'previews/index.json', {})
    if not scores:
        return
    try:
        contract.validate_cd_tree(source, [identity], audio_probe=False)
    except (ValueError, OSError, KeyError, TypeError):
        return
    preview = _read(source / contract.PREVIEWS, {}).get(identity)
    if preview:
        _dump(destination / 'previews/index.json', {identity: preview})
    relative = [f'cogs/tts/cd/{folder}/{identity}.{extension}' for folder, extension in
                [('arrangements', 'json'), ('packages', 'json'), ('previews', 'mp3')]]
    if video and _VIDEO.fullmatch(str(video.get('video_id', ''))):
        relative.append(f"cogs/tts/cd/thumbnails/{video['video_id']}.jpg")
    for rel in relative:
        path = source / rel
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_ZIP_BYTES:
            target = destination / Path(rel).relative_to('cogs/tts/cd')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


async def _off_loop(callback, *args, **kwargs):
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
        if not physical.cancelled():
            physical.exception()
        raise


async def _stop_process(process):
    if process.returncode is not None:
        await process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 2)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def _run_importer(directory, *, timeout_seconds=180):
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'osaka_cd.discord_import',
        '--prepare', str(directory), cwd=str(_ROOT), stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout_seconds)
        except asyncio.TimeoutError:
            raise ImportCDError('import_timeout') from None
        result = _read(directory / 'report.json', {})
        if process.returncode != 0 or not result.get('ok'):
            raise ImportCDError(result.get('code', 'import_unavailable'))
        return result
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


async def _prepare_importer(directory):
    # Child process only: never alter the running bot's Python import paths.
    sys.path.insert(0, str(_ROOT / 'deploy/ttv-cd'))
    from catalog_import import import_midi
    report = await import_midi(directory / 'source.mid', root=directory / 'catalog',
        cache_dir=directory / 'covers', export_zip=directory / 'package.zip', render_sample=False)
    _dump(directory / 'report.json', {'ok': bool(report.get('ok')),
        'pending': sorted({row['artifact'] for row in report.get('pending_artifacts', [])
                           if row.get('artifact') in {'metadata', 'thumbnail'}})})


def _package_info(package, base):
    contract = _contract()
    report = contract.validate_package(package, base_dir=base, require_cd_only=True, audio_probe=True)
    with zipfile.ZipFile(package) as archive:
        paths = {rel: info for info, rel in contract._zip_members(archive)}
        if contract.SCORES not in paths:
            raise ImportCDError('invalid_package')
        scores = _json(archive.read(paths[contract.SCORES]), 'invalid_package')
        if not scores:
            raise ImportCDError('invalid_package')
        videos = (_json(archive.read(paths[contract.VIDEOS]), 'invalid_package').get('songs', {})
                  if contract.VIDEOS in paths else {})
    return report, scores, videos


def _stamp_package(package, snapshot, scores, request_hash=None, identities=None):
    contract = _contract()
    with zipfile.ZipFile(package) as archive:
        contents = {rel: archive.read(info) for info, rel in contract._zip_members(archive)}
    if request_hash:
        scores = [dict(score, discord_import=_marker(score, request_hash)) for score in scores]
        contents[contract.SCORES] = json.dumps(scores, ensure_ascii=False, allow_nan=False).encode()
    videos = _json(contents.get(contract.VIDEOS, b'{"version":1,"songs":{}}'), 'invalid_package')
    videos['discord_import_request'] = {'base_generation': snapshot.get('generation'),
        'versions': {identity: snapshot['versions'].get(identity) for identity in sorted(identities or [row['id'] for row in scores])}}
    contents[contract.VIDEOS] = json.dumps(videos, ensure_ascii=False, allow_nan=False).encode()
    with zipfile.ZipFile(package, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    return scores


def _contains(current, incoming):
    if isinstance(incoming, dict):
        return isinstance(current, dict) and all(key in current and _contains(current[key], value)
            for key, value in incoming.items())
    return current == incoming


def _package_matches(package, snapshot):
    contract = _contract()
    root = Path(snapshot['root'])
    with zipfile.ZipFile(package) as archive:
        for info, rel in contract._zip_members(archive):
            incoming = archive.read(info)
            current = root / rel
            if rel.startswith('cogs/tts/cd/packages/'):
                continue  # Store regenerates these from verified actual artifacts.
            if rel in (contract.SCORES, contract.VIDEOS, contract.PREVIEWS):
                value = _json(incoming, 'invalid_package')
                existing = _read(current, {})
                if rel == contract.SCORES:
                    existing = {row['id']: row for row in existing} if isinstance(existing, list) else {}
                    if any(not _contains(existing.get(row['id']), row) for row in value):
                        return False
                elif rel == contract.VIDEOS:
                    if not _contains(existing.get('songs', {}), value.get('songs', {})):
                        return False
                elif not _contains(existing, value):
                    return False
            elif (not current.is_file() or current.is_symlink()
                    or current.stat().st_size != len(incoming)
                    or hashlib.sha256(current.read_bytes()).digest() != hashlib.sha256(incoming).digest()):
                return False
    return True


def _pending_artifacts(report, scores, videos, base):
    effective_videos = dict(_read(Path(base) / _contract().VIDEOS, {}).get('songs', {}))
    effective_videos.update(videos)
    pending = set()
    for score in scores:
        identity = score['id']
        if report['artifacts'].get(identity, {}).get('thumbnail') != 'ready':
            pending.add('thumbnail')
        if identity not in effective_videos:
            pending.add('metadata')
    return tuple(sorted(pending))


def _publish(store, package, snapshot, request_hash=None):
    report, scores, videos = _package_info(package, snapshot['root'])
    identities = report['songs']
    pending = _pending_artifacts(report, scores, videos, snapshot['root'])
    if not request_hash and _package_matches(package, snapshot):
        return ImportResult(tuple(_song_summary(score) for score in scores), pending, True, True,
            snapshot.get('generation'), 'activated' if snapshot.get('generation') and snapshot.get('activated_generation') == snapshot.get('generation') else 'applied')
    scores = _stamp_package(package, snapshot, scores, request_hash, identities)
    expected = {identity: snapshot['versions'].get(identity) for identity in identities}
    receipt = store.receive(package, expected_versions=expected, retry_failed=True)
    result = store.process(receipt['job_id'])
    if not result.get('applied'):
        raise ImportCDError(result.get('error_code', 'storage_unavailable'))
    return ImportResult(tuple(_song_summary(score) for score in scores), pending,
        False, True, result.get('generation'), result.get('state', 'applied'), receipt['job_id'])


async def _wait_for_activation(store, result):
    if not result.job_id or result.state == 'activated':
        return result
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _ACTIVATION_TIMEOUT
    while loop.time() < deadline:
        try:
            job = await _off_loop(store.job, result.job_id)
        except (OSError, sqlite3.Error, StoreError):
            return result  # The data is already durable; acknowledgement can follow later.
        if job.get('state') == 'activated':
            return replace(result, state='activated', generation=job.get('generation'))
        if job.get('state') == 'failed':
            raise ImportCDError(job.get('error_code', 'superseded'))
        await asyncio.sleep(min(.2, max(0, deadline - loop.time())))
    return result


async def _progress(callback, phase):
    if callback is not None:
        try:
            await callback(phase)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # A deleted progress message must not cancel a valid import.


async def import_cd(prepared, *, overrides=None, progress=None, store=None):
    """Prepare, validate, then atomically publish against the original snapshot."""
    prepared = await _off_loop(prepare_input, prepared.filename, prepared.data, prepared.sidecar)
    metadata = await _off_loop(inspect_input, prepared, overrides)
    await _progress(progress, 'preparing')
    try:
        store = store or await _off_loop(get_store)
        snapshot = await _off_loop(store.snapshot, strict=True, advertise_legacy=True)
        if prepared.kind == 'midi':
            metadata = await _off_loop(_resolve_identity, snapshot, prepared, metadata, overrides)
        request_hash = await _off_loop(_request_hash, prepared, metadata) if prepared.kind == 'midi' else None
        if request_hash:
            existing = await _off_loop(_existing_midi, snapshot, metadata, request_hash)
            if existing:
                return ImportResult((_song_summary(existing),), (), True, True,
                    snapshot.get('generation'), 'activated' if snapshot.get('generation') and snapshot.get('activated_generation') == snapshot.get('generation') else 'applied')
        with tempfile.TemporaryDirectory(prefix='osaka-discord-cd-') as temporary:
            directory = Path(temporary)
            package = directory / 'package.zip'
            importer_pending = ()
            if prepared.kind == 'midi':
                await _off_loop(_seed_catalog, snapshot, directory / 'catalog', metadata['id'])
                await _off_loop((directory / 'source.mid').write_bytes, prepared.data)
                await _off_loop(_dump, directory / 'source.json', {key: value for key, value in metadata.items() if key != 'kind'})
                await _progress(progress, 'importing')
                imported = await _run_importer(directory)
                importer_pending = tuple((imported or {}).get('pending', ()))
            else:
                await _off_loop(package.write_bytes, prepared.data)
            await _progress(progress, 'installing')
            result = await _off_loop(_publish, store, package, snapshot, request_hash)
        if importer_pending:
            result = replace(result, pending=tuple(sorted(set(result.pending) | set(importer_pending))))
        await _progress(progress, 'activating')
        return await _wait_for_activation(store, result)
    except ImportCDError:
        raise
    except asyncio.CancelledError:
        raise
    except StoreError as exc:
        raise ImportCDError(exc.code) from None
    except (ValueError, zipfile.BadZipFile):
        raise ImportCDError('invalid_package' if prepared.kind == 'zip' else 'invalid_midi') from None
    except Exception as exc:
        raise ImportCDError(getattr(exc, 'code', 'import_unavailable')) from None


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', required=True, type=Path)
    directory = parser.parse_args().prepare
    try:
        asyncio.run(_prepare_importer(directory))
        return 0
    except Exception as exc:
        code = 'invalid_midi' if type(exc).__name__ in {'MidiExtractionError', 'ImportInputError'} else 'import_unavailable'
        _dump(directory / 'report.json', {'ok': False, 'code': code})
        return 1


if __name__ == '__main__':
    raise SystemExit(_main())
