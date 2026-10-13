#!/usr/bin/env python3
"""Import local MIDI files and verify per-song ZIPs for Osaka's updater."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from catalog_import import (DATA_ROOT, THUMB_ROOT, IMPORTER_VERSION, _atomic, _json_bytes,
                            import_midi, _sample, _resource_ready, export_song_package,
                            preview_manifest, score_sha256)


def _midi_files(folder: Path):
    for path in sorted(folder.iterdir()):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in ('.mid', '.midi'):
            yield path


def _state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _file_hash(path: Path | None) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None


def _identity(path: Path, args) -> tuple[str, str]:
    source = _file_hash(path)
    env_file = Path(os.getenv('PHONE_WORKER_ENV', '~/.phone-worker.env')).expanduser()
    try:
        from worker_config import configuration_fingerprint
        worker_fingerprint = configuration_fingerprint()
    except Exception as exc:
        worker_fingerprint = 'unavailable_' + type(exc).__name__
    settings = {'version': IMPORTER_VERSION, 'sidecar': _file_hash(path.with_suffix('.json')),
                'candidates': _file_hash(args.candidates), 'cover': _file_hash(args.cover_file),
                'no_sample': args.no_sample, 'worker_environment': _file_hash(env_file), 'worker_runtime': worker_fingerprint,
                'process_environment': hashlib.sha256(_json_bytes({key: os.getenv(key, '') for key in
                    ['PHONE_WORKER_RUNTIME_ROOT', 'PHONE_WORKER_TETO_BACKEND', 'TETO_BACKEND',
                     'TETO_VOICEBANK_DIR', 'TETO_RESAMPLER_PATH', 'YOUTUBE_API_KEY', 'GOOGLE_YOUTUBE_API_KEY']})).hexdigest()}
    return f'{source}:{hashlib.sha256(_json_bytes(settings)).hexdigest()}', source


def _patch_path(output: Path, source: str, slug: str | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    suffix = f'{slug}-' if slug else ''
    return output / f'osaka-cd-import-{stamp}-{suffix}{source[:8]}.zip'


def _failure(error: Exception) -> tuple[str, str]:
    # Input and explicit selector errors need changed input/settings. Resource,
    # network, filesystem and subprocess errors remain retryable.
    permanent = bool(getattr(error, 'permanent', False))
    return ('permanent' if permanent else 'temporary'), str(getattr(error, 'code', type(error).__name__))


async def _publish(result: dict, args) -> None:
    if not getattr(args, 'publish_url', None):
        return
    try:
        from cd_publish import publish_package
        response = await asyncio.to_thread(publish_package, Path(result['zip']), args.publish_url,
                                            token=os.getenv('OSAKA_CD_PUBLISH_TOKEN'))
        result['publication'] = response
        # HTTP acceptance is not proof of installation by the bot's updater.
        result['published_to_vps'] = bool(response.get('published_to_vps', response.get('state') == 'queued'))
        result['installed_on_vps'] = bool(response.get('installed', False))
    except Exception as exc:
        result['publication'] = {'ok': False, 'installed': False, 'error': type(exc).__name__}


async def batch(args) -> dict:
    inbox, output = args.inbox.expanduser(), args.output.expanduser()
    args.data_root = args.data_root.expanduser()
    inbox.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    args.data_root.mkdir(parents=True, exist_ok=True)
    state_path, retry_file = args.data_root / 'imported.json', args.data_root / 'failed.json'
    samples_file, pending_file = args.data_root / 'pending_samples.json', args.data_root / 'pending_artifacts.json'
    publications_file = args.data_root / 'pending_publications.json'
    history, retry, waiting_samples, waiting = map(_state, (state_path, retry_file, samples_file, pending_file))
    publications = _state(publications_file)
    successes, failures, pending = [], [], []
    processed_ids = set()
    attempted_publications = set()
    bypass = bool(args.force or getattr(args, 'retry', False))
    now = time.time()
    for path in _midi_files(inbox):
        identity = None
        try:
            if not bypass and now - path.stat().st_mtime < 5:
                pending.append({'file': path.name, 'reason': 'copy_in_progress'})
                continue
            identity, source = _identity(path, args)
            failed = retry.get(path.name)
            # A changed MIDI, sidecar, supplied cover/candidates or environment
            # invalidates failure state immediately instead of retaining cooldown.
            if isinstance(failed, dict) and failed.get('identity') != identity:
                retry.pop(path.name, None)
                failed = None
            if not bypass and isinstance(failed, dict):
                if failed.get('kind') == 'permanent':
                    pending.append({'file': path.name, 'reason': 'permanent_input_error', 'error': failed.get('error'),
                                    'note': 'Corrija MIDI/sidecar ou execute --retry para repetir o diagnóstico.'})
                    continue
                if now < failed.get('retry_after', 0):
                    pending.append({'file': path.name, 'reason': 'retry_cooldown', 'retry_after': failed['retry_after']})
                    continue
            queued = waiting.get(path.name)
            if history.get(path.name) == identity and not args.force:
                if not isinstance(queued, dict) or queued.get('identity') != identity:
                    continue
                if not bypass and now < queued.get('retry_after', 0):
                    pending.append({'file': path.name, 'reason': 'artifacts_pending', 'artifacts': queued.get('artifacts', [])})
                    continue
            result = await import_midi(path, root=args.data_root, cache_dir=args.cache_dir,
                                       candidates=args.candidates, cover_file=args.cover_file,
                                       render_sample=not args.no_sample, export_zip=_patch_path(output, source))
            if result['source_sha256'] != source:
                raise RuntimeError('midi_changed_during_import; tente novamente após concluir a cópia')
            history[path.name] = identity
            retry.pop(path.name, None)
            processed_ids.add(result['id'])
            artifacts = [item for item in result.get('pending_artifacts', [])
                         if item['artifact'] != 'sample' or not args.no_sample]
            if artifacts:
                waiting[path.name] = {'identity': identity, 'id': result['id'], 'artifacts': artifacts,
                                      'retry_after': now + 60}
            else:
                waiting.pop(path.name, None)
            if result.get('sample_status', '').startswith('pending_'):
                waiting_samples[result['id']] = {'source_sha256': result['source_sha256'],
                                                 'score_sha256': result['score_sha256']}
            else:
                waiting_samples.pop(result['id'], None)
            await _publish(result, args)
            if getattr(args, 'publish_url', None):
                attempted_publications.add(result['zip_sha256'])
                for digest, item in list(publications.items()):
                    if item.get('id') == result['id']:
                        publications.pop(digest, None)
                if not result.get('published_to_vps'):
                    publications[result['zip_sha256']] = {'id': result['id'], 'zip': result['zip'],
                                                         'retry_after': now + 60}
            successes.append(result)
        except Exception as error:
            kind, code = _failure(error)
            # Do not report environment values or provider exception strings:
            # input errors are authored locally and safe to show in full.
            reason = str(error)[:240] if kind == 'permanent' else code
            failures.append({'file': path.name, 'error': type(error).__name__, 'kind': kind, 'reason': reason})
            if identity is not None:
                previous = retry.get(path.name, {})
                attempt = int(previous.get('attempt', 0)) + 1 if previous.get('identity') == identity else 1
                retry[path.name] = {'identity': identity, 'kind': kind, 'error': code, 'attempt': attempt,
                                   'retry_after': 0 if kind == 'permanent' else now + min(3600, 60 * 2 ** min(attempt - 1, 6))}
    # Recover v7 pending samples even when the MIDI was removed from the inbox.
    # Each retry exports the full CD snapshot, never an MP3-only package.
    if waiting_samples and not args.no_sample and _resource_ready():
        try:
            scores = json.loads((args.data_root / 'cd_song_scores.json').read_text(encoding='utf-8'))
            manifest = _state(args.data_root / 'previews/index.json')
            for slug, wanted in list(waiting_samples.items()):
                if slug in processed_ids:
                    continue
                source_hash = wanted.get('source_sha256') if isinstance(wanted, dict) else wanted
                expected_score = wanted.get('score_sha256') if isinstance(wanted, dict) else None
                melody = next((song for song in scores if song.get('id') == slug and song.get('source_sha256') == source_hash), None)
                if melody is None or (expected_score and score_sha256(melody) != expected_score):
                    waiting_samples.pop(slug, None)
                    continue
                with tempfile.TemporaryDirectory(prefix='osaka-cd-retry-') as temporary:
                    audio_file = Path(temporary) / 'preview.mp3'
                    status = await asyncio.to_thread(_sample, melody, audio_file)
                    if status != 'ready' or not audio_file.is_file():
                        pending.append({'file': slug, 'reason': status})
                        continue
                    audio = audio_file.read_bytes()
                # Validate a complete candidate before changing the ready index.
                # export_song_package reads root, so stage a copied isolated song.
                with tempfile.TemporaryDirectory(prefix='osaka-cd-retry-stage-') as temporary:
                    import shutil
                    stage = Path(temporary)
                    for rel in ('cd_song_scores.json', 'cd_song_videos.json'):
                        shutil.copy2(args.data_root / rel, stage / rel)
                    if (args.data_root / 'thumbnails').is_dir():
                        shutil.copytree(args.data_root / 'thumbnails', stage / 'thumbnails')
                    item = preview_manifest(melody, audio)
                    _atomic(stage / 'previews' / f'{slug}.mp3', audio)
                    _atomic(stage / 'previews/index.json', _json_bytes({slug: item}))
                    verification = export_song_package(stage, slug, _patch_path(output, source_hash, slug), sample_status='ready')
                _atomic(args.data_root / 'previews' / f'{slug}.mp3', audio)
                manifest[slug] = item
                _atomic(args.data_root / 'previews/index.json', _json_bytes(manifest))
                _atomic(args.data_root / 'packages' / f'{slug}.json', _json_bytes(verification['package_manifest']))
                result = {'id': slug, 'sample_status': 'ready', 'zip': verification['zip_path'],
                          'zip_sha256': verification['zip_sha256'], 'validation': verification}
                await _publish(result, args)
                if getattr(args, 'publish_url', None):
                    attempted_publications.add(result['zip_sha256'])
                if getattr(args, 'publish_url', None) and not result.get('published_to_vps'):
                    publications[result['zip_sha256']] = {'id': slug, 'zip': result['zip'], 'retry_after': now + 60}
                successes.append(result)
                waiting_samples.pop(slug, None)
                for filename, job in list(waiting.items()):
                    if job.get('id') == slug:
                        job['artifacts'] = [item for item in job.get('artifacts', []) if item['artifact'] != 'sample']
                        if not job['artifacts']:
                            waiting.pop(filename, None)
        except Exception as error:
            pending.append({'file': 'samples', 'reason': type(error).__name__})
    if getattr(args, 'publish_url', None):
        for digest, item in list(publications.items()):
            if digest in attempted_publications:
                pending.append({'file': item.get('zip'), 'reason': 'publication_pending'})
                continue
            if not bypass and now < item.get('retry_after', 0):
                pending.append({'file': item.get('zip'), 'reason': 'publication_pending'})
                continue
            path = Path(item['zip'])
            if not path.is_file() or _file_hash(path) != digest:
                pending.append({'file': str(path), 'reason': 'publication_package_missing_or_changed'})
                continue
            result = {'id': item['id'], 'zip': str(path), 'zip_sha256': digest}
            await _publish(result, args)
            if result.get('published_to_vps'):
                publications.pop(digest, None)
                successes.append(result)
            else:
                item['retry_after'] = now + 300
                pending.append({'file': str(path), 'reason': 'publication_pending'})
    for path, state in [(state_path, history), (retry_file, retry), (samples_file, waiting_samples),
                        (pending_file, waiting), (publications_file, publications)]:
        _atomic(path, _json_bytes(state))
    return {'ok': not failures, 'processed': successes, 'pending': pending, 'failures': failures,
            'published_to_vps': any(item.get('published_to_vps') for item in successes),
            'installed_on_vps': any(item.get('installed_on_vps') for item in successes),
            'note': 'ZIPs validados em caminhos absolutos. Publicação HTTP aceita pode estar na fila; confirme aplicação no updater.'}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inbox', type=Path, default=Path.home() / 'storage/downloads/osaka-cd-midi')
    p.add_argument('--output', type=Path, default=Path.home() / 'storage/downloads')
    p.add_argument('--data-root', type=Path, default=DATA_ROOT)
    p.add_argument('--cache-dir', type=Path, default=THUMB_ROOT)
    p.add_argument('--report', type=Path, default=Path.home() / 'storage/downloads/osaka-cd-import-relatorio.json')
    p.add_argument('--candidates', type=Path, help='JSON de candidatos verificáveis para teste isolado')
    p.add_argument('--cover-file', type=Path, help='JPEG local correspondente ao vídeo selecionado')
    p.add_argument('--no-sample', action='store_true')
    p.add_argument('--watch', action='store_true', help='Verificar arquivos novos e recuperar artefatos pendentes continuamente')
    p.add_argument('--force', action='store_true', help='Reimportar também arquivos já processados e ignorar cooldown')
    p.add_argument('--retry', action='store_true', help='Tentar imediatamente; ignorar falha anterior e cooldown')
    p.add_argument('--publish-url', help='Rota autenticada /api/osaka/cd/publish; token somente em OSAKA_CD_PUBLISH_TOKEN')
    args = p.parse_args()
    args.output = args.output.expanduser()
    args.report = args.report.expanduser()
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        while True:
            result = asyncio.run(batch(args))
            _atomic(args.report, _json_bytes(result))
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            if not args.watch:
                return 0 if result['ok'] else 1
            time.sleep(45)
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
