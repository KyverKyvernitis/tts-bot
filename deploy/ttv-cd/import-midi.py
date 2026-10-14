#!/usr/bin/env python3
"""Import stabilized local MIDI files and publish verified CDs automatically."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


def _source_snapshot(directory: Path) -> dict[str, str]:
    """Hash executable kit sources, including imports loaded after startup."""
    snapshot = {}
    for path in sorted(directory.rglob('*.py')):
        if '__pycache__' in path.parts:
            continue
        relative = path.relative_to(directory).as_posix()
        try:
            snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            snapshot[relative] = 'unreadable'
    return snapshot


_CODE_ROOT = Path(__file__).resolve().parent
# Capture before importing the pipeline: an upgrade during module loading must
# not leave a watcher using a mixture of old and new code without a restart.
_LOADED_SOURCES = _source_snapshot(_CODE_ROOT)

from catalog_import import (DATA_ROOT, THUMB_ROOT, IMPORTER_VERSION, _atomic, _json_bytes,
                            import_midi, _sample, _resource_ready, export_song_package,
                            preview_manifest, score_sha256, sample_melody)


class ImporterGuardError(RuntimeError):
    def __init__(self, code: str, note: str, *, pid: int | None = None,
                 changed_files: list[str] | None = None):
        super().__init__(note)
        self.code, self.note = code, note
        self.pid, self.changed_files = pid, changed_files

    def report(self) -> dict:
        result = {'ok': False, 'error': self.code, 'note': self.note,
                  'importer_version': IMPORTER_VERSION}
        if self.pid is not None:
            result['pid'] = self.pid
        if self.changed_files:
            result['changed_files'] = self.changed_files
        return result


def _check_sources(directory: Path = _CODE_ROOT,
                   loaded: dict[str, str] = _LOADED_SOURCES) -> None:
    current = _source_snapshot(directory)
    changed = sorted(name for name in current.keys() | loaded.keys()
                     if current.get(name) != loaded.get(name))
    if changed:
        raise ImporterGuardError('importer_code_changed',
            'O kit foi atualizado durante esta execução. O importador foi encerrado; '
            'execute novamente o mesmo comando para carregar o código atualizado.',
            changed_files=changed)


@contextmanager
def data_root_lock(root: Path):
    """One writer for a catalog, across batches and the entire watch session."""
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / '.import-midi.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            try:
                owner = json.loads(os.pread(descriptor, 4096, 0))
                pid = owner.get('pid')
                pid = pid if type(pid) is int and pid > 0 else None
            except (OSError, ValueError, AttributeError):
                pid = None
            raise ImporterGuardError('importer_already_running',
                'Outro importador está usando este catálogo. Pare o --watch com Ctrl+C '
                'na sessão original antes de executar --force ou iniciar outro importador.',
                pid=pid) from None
        owner = json.dumps({'pid': os.getpid(), 'importer_version': IMPORTER_VERSION}).encode()
        os.ftruncate(descriptor, 0)
        os.write(descriptor, owner)
        yield root
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        # Keep the inode: unlinking lets another process lock a replacement
        # file while an existing waiter still holds the original descriptor.


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


@lru_cache(maxsize=1024)
def _hash_cached(path: str, signature: tuple) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_hash(path: Path | None) -> str | None:
    if not path:
        return None
    path = Path(path).expanduser()
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file():
        return None
    signature = (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino)
    return _hash_cached(str(path.resolve()), signature)



def _identity(path: Path, args) -> tuple[str, str]:
    source = _file_hash(path)
    env_file = Path(os.getenv('PHONE_WORKER_ENV', '~/.phone-worker.env')).expanduser()
    try:
        from worker_config import configuration_fingerprint
        worker_fingerprint = getattr(args, '_worker_fingerprint', None)
        if worker_fingerprint is None:
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


def _worker_fingerprint(args, *, force=False):
    """Probe the bank periodically while idle, fully before actual imports.

    The full fingerprint inspects nested recordings/oto files. Lightweight env
    and release signatures invalidate this observation sooner; directory mtime
    alone is deliberately never treated as proof that a bank is unchanged.
    """
    env_file = Path(os.getenv('PHONE_WORKER_ENV', '~/.phone-worker.env')).expanduser()
    runtime = Path(os.getenv('PHONE_WORKER_RUNTIME_ROOT', '~/.core-worker-runtime')).expanduser() / 'current'
    process_signature = hashlib.sha256(_json_bytes({key: value for key, value in os.environ.items()
        if key.startswith('PHONE_WORKER_TETO_') or key in {'PHONE_WORKER_RUNTIME_ROOT', 'PHONE_WORKER_DIR'}})).hexdigest()
    signature = (_file_hash(env_file), str(runtime.resolve()), process_signature)
    cached = getattr(args, '_worker_probe', None)
    now = time.monotonic()
    if not force and cached and cached['signature'] == signature and now - cached['observed'] < cached['ttl']:
        return cached['value']
    try:
        from worker_config import configuration_fingerprint
        value, ttl = configuration_fingerprint(), 30
    except Exception as exc:
        value, ttl = 'unavailable_' + type(exc).__name__, 5
    args._worker_probe = {'signature': signature, 'observed': now, 'value': value, 'ttl': ttl}
    return value


def _worker_retry_identity(args):
    """Retry admission when private auth changes, without exposing its values."""
    env_file = Path(os.getenv('PHONE_WORKER_ENV', '~/.phone-worker.env')).expanduser()
    auth_hash = hashlib.sha256(_json_bytes({key: os.getenv(key, '') for key in (
        'PHONE_WORKER_TOKEN', 'CORE_WORKER_DIRECT_HTTP_TOKEN', 'CORE_WORKER_APK_HTTP_TOKEN', 'CORE_WORKER_TOKEN')})).hexdigest()
    return hashlib.sha256(_json_bytes({'renderer': args._worker_fingerprint,
        'environment': _file_hash(env_file), 'auth_environment': auth_hash})).hexdigest()


def _patch_path(output: Path, source: str, slug: str | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    suffix = f'{slug}-' if slug else ''
    return output / f'osaka-cd-import-{stamp}-{suffix}{source[:8]}.zip'


def _failure(error: Exception) -> tuple[str, str]:
    # Input and explicit selector errors need changed input/settings. Resource,
    # network, filesystem and subprocess errors remain retryable.
    permanent = bool(getattr(error, 'permanent', False))
    return ('permanent' if permanent else 'temporary'), str(getattr(error, 'code', type(error).__name__))


def _publication_configuration(args):
    if hasattr(args, '_publication_configuration'):
        return args._publication_configuration
    from cd_publish import load_configuration
    return load_configuration(url=getattr(args, 'publish_url', None))


def _stable_for_watch(path, args):
    """Require unchanged MIDI and sidecar signatures across two seconds."""
    if not getattr(args, 'watch', False):
        return True
    observed = getattr(args, '_observed_files', None)
    if observed is None:
        observed = args._observed_files = {}
    signatures = []
    for source in (path, path.with_suffix('.json')):
        if source.is_symlink():
            raise ValueError('input_symlink_refused')
        try:
            info = source.stat()
            signatures.append((info.st_size, info.st_mtime_ns, info.st_ino))
        except FileNotFoundError:
            signatures.append(None)
    signature = tuple(signatures)
    previous, since = observed.get(str(path), (None, time.monotonic()))
    if previous != signature:
        observed[str(path)] = (signature, time.monotonic())
        return False
    return time.monotonic() - since >= 2


async def batch(args) -> dict:
    """Guard direct batch callers as well as command-line watch sessions."""
    with data_root_lock(args.data_root):
        _check_sources()
        return await _batch_unlocked(args)


async def _batch_unlocked(args) -> dict:
    inbox, output = args.inbox.expanduser(), args.output.expanduser()
    args.data_root = args.data_root.expanduser()
    inbox.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    args.data_root.mkdir(parents=True, exist_ok=True)
    # One environment/runtime probe per scan, shared by all file identities.
    args._worker_fingerprint = _worker_fingerprint(args)
    state_path, retry_file = args.data_root / 'imported.json', args.data_root / 'failed.json'
    samples_file, pending_file = args.data_root / 'pending_samples.json', args.data_root / 'pending_artifacts.json'
    history, retry, waiting_samples, waiting = map(_state, (state_path, retry_file, samples_file, pending_file))
    packages_path = args.data_root / 'publication_packages.json'
    saved_packages = _state(packages_path)
    from cd_publish import PublicationOutbox
    configuration = _publication_configuration(args)
    outbox = PublicationOutbox(args.data_root, configuration) if configuration else None
    successes, failures, pending = [], [], []
    processed_ids = set()
    bypass = bool(args.force or getattr(args, 'retry', False))
    now = time.time()
    for path in _midi_files(inbox):
        identity = None
        try:
            if not _stable_for_watch(path, args) or (not bypass and not getattr(args, 'watch', False) and now - path.stat().st_mtime < 2):
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
                if not bypass and queued.get('retryable') is False:
                    pending.append({'file': path.name, 'reason': 'permanent_artifact_error',
                                    'artifacts': queued.get('artifacts', []),
                                    'note': 'Corrija MIDI/sidecar/configuração ou execute --retry para repetir o diagnóstico.'})
                    continue
                if not bypass and now < queued.get('retry_after', 0):
                    pending.append({'file': path.name, 'reason': 'artifacts_pending', 'artifacts': queued.get('artifacts', [])})
                    continue
            args._worker_fingerprint = _worker_fingerprint(args, force=True)
            identity, source = _identity(path, args)
            result = await import_midi(path, root=args.data_root, cache_dir=args.cache_dir,
                                       candidates=args.candidates, cover_file=args.cover_file,
                                       render_sample=not args.no_sample, export_zip=_patch_path(output, source))
            if result['source_sha256'] != source or _identity(path, args)[0] != identity:
                raise RuntimeError('midi_changed_during_import; tente novamente após concluir a cópia')
            history[path.name] = identity
            retry.pop(path.name, None)
            processed_ids.add(result['id'])
            artifacts = [item for item in result.get('pending_artifacts', [])
                         if item['artifact'] != 'sample' or not args.no_sample]
            if artifacts:
                waiting[path.name] = {'identity': identity, 'id': result['id'], 'artifacts': artifacts,
                                      'retry_after': now + 60, 'retryable': any(item.get('retryable') is not False for item in artifacts)}
            else:
                waiting.pop(path.name, None)
            if result.get('sample_status', '').startswith('pending_'):
                sample_pending = next((item for item in artifacts if item['artifact'] == 'sample'), {})
                waiting_samples[result['id']] = {'source_sha256': result['source_sha256'],
                                                 'score_sha256': result['score_sha256'],
                                                 'retry_after': now + 60,
                                                 'retryable': sample_pending.get('retryable', True),
                                                 'configuration_identity': _worker_retry_identity(args)}
            else:
                waiting_samples.pop(result['id'], None)
            saved_packages[result['id']] = {key: result[key] for key in ('id', 'zip', 'zip_sha256')}
            _atomic(packages_path, _json_bytes(saved_packages))
            if outbox:
                outbox.enqueue(result)
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
                wanted = dict(wanted) if isinstance(wanted, dict) else {'source_sha256': wanted}
                current_configuration = _worker_retry_identity(args)
                changed_configuration = wanted.get('configuration_identity') != current_configuration
                if not bypass and not changed_configuration:
                    if wanted.get('retryable') is False:
                        pending.append({'file': slug, 'reason': 'permanent_sample_error'})
                        continue
                    if now < wanted.get('retry_after', 0):
                        continue
                source_hash = wanted.get('source_sha256') if isinstance(wanted, dict) else wanted
                expected_score = wanted.get('score_sha256') if isinstance(wanted, dict) else None
                melody = next((song for song in scores if song.get('id') == slug and song.get('source_sha256') == source_hash), None)
                if melody is None or (expected_score and score_sha256(melody) != expected_score):
                    waiting_samples.pop(slug, None)
                    continue
                with tempfile.TemporaryDirectory(prefix='osaka-cd-retry-') as temporary:
                    audio_file = Path(temporary) / 'preview.mp3'
                    render_input = sample_melody(args.data_root, melody)
                    status = await asyncio.to_thread(_sample, render_input, audio_file)
                    if status != 'ready' or not audio_file.is_file():
                        wanted.update(retry_after=now + 60, configuration_identity=current_configuration,
                                      retryable=render_input.get('_render_report', {}).get('retryable', True))
                        waiting_samples[slug] = wanted
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
                    if (args.data_root / 'arrangements').is_dir():
                        shutil.copytree(args.data_root / 'arrangements', stage / 'arrangements')
                    if (args.data_root / 'thumbnails').is_dir():
                        shutil.copytree(args.data_root / 'thumbnails', stage / 'thumbnails')
                    item = preview_manifest(melody, audio, render_input.get('_render_report'))
                    _atomic(stage / 'previews' / f'{slug}.mp3', audio)
                    _atomic(stage / 'previews/index.json', _json_bytes({slug: item}))
                    verification = export_song_package(stage, slug, _patch_path(output, source_hash, slug), sample_status='ready')
                _atomic(args.data_root / 'previews' / f'{slug}.mp3', audio)
                manifest[slug] = item
                _atomic(args.data_root / 'previews/index.json', _json_bytes(manifest))
                _atomic(args.data_root / 'packages' / f'{slug}.json', _json_bytes(verification['package_manifest']))
                result = {'id': slug, 'sample_status': 'ready', 'zip': verification['zip_path'],
                          'zip_sha256': verification['zip_sha256'], 'validation': verification}
                saved_packages[slug] = {key: result[key] for key in ('id', 'zip', 'zip_sha256')}
                _atomic(packages_path, _json_bytes(saved_packages))
                if outbox:
                    outbox.enqueue(result)
                successes.append(result)
                waiting_samples.pop(slug, None)
                for filename, job in list(waiting.items()):
                    if job.get('id') == slug:
                        job['artifacts'] = [item for item in job.get('artifacts', []) if item['artifact'] != 'sample']
                        if not job['artifacts']:
                            waiting.pop(filename, None)
        except Exception as error:
            pending.append({'file': 'samples', 'reason': type(error).__name__})
    if outbox:
        # Enabling transport after an offline import publishes its existing
        # validated artifact; it does not require reimporting or resynthesis.
        for package in saved_packages.values():
            if isinstance(package, dict) and all(isinstance(package.get(key), str) for key in ('id', 'zip', 'zip_sha256')):
                outbox.enqueue(package)
    publication_reports = await asyncio.to_thread(outbox.process, force=bypass,
        limit=1 if getattr(args, 'watch', False) else 8) if outbox else []
    for receipt in publication_reports:
        matching = next((item for item in successes if item.get('zip_sha256') == receipt.get('zip_sha256')), None)
        if matching is None:
            matching = {'id': receipt['id'], 'zip_sha256': receipt['zip_sha256']}
            successes.append(matching)
        matching.update(publication=receipt, published_to_vps=bool(receipt.get('published_to_vps')),
                        installed_on_vps=bool(receipt.get('installed')))
    if outbox:
        for item in outbox.jobs.values():
            pending.append({'file': item.get('zip'), 'reason': 'publication_pending',
                            'state': item.get('state', 'local'), 'id': item.get('id'),
                            **({'error': item['error']} if item.get('error') else {})})
    for path, state in [(state_path, history), (retry_file, retry), (samples_file, waiting_samples),
                        (pending_file, waiting)]:
        _atomic(path, _json_bytes(state))
    return {'ok': not failures, 'processed': successes, 'pending': pending, 'failures': failures,
            'published_to_vps': any(item.get('published_to_vps') for item in successes),
            'installed_on_vps': any(item.get('installed_on_vps') for item in successes),
            'automatic_publication': bool(configuration),
            'note': 'Artefatos validados; instalado somente após ativação confirmada pela VPS.'}


def console_report(result: dict, report_path: Path) -> dict:
    """Show the actionable outcome; keep every candidate/note in the file."""
    summary = {key: value for key, value in result.items() if key != 'processed'}
    rows = []
    for item in result.get('processed', []):
        row = {key: value for key, value in item.items()
               if key not in {'extraction', 'validation', 'selection_audit'}}
        extraction = item.get('extraction')
        if isinstance(extraction, dict):
            row['extraction'] = {key: extraction[key] for key in
                ('vocal_status', 'vocal_transcription_confirmed', 'track_index', 'channel',
                 'source_duration_ms', 'source_note_count', 'excerpt_start_ms', 'excerpt_end_ms', 'warnings')
                if key in extraction}
        validation = item.get('validation')
        if isinstance(validation, dict):
            row['validation'] = {key: validation[key] for key in ('ok', 'songs', 'artifacts') if key in validation}
        rows.append(row)
    summary['processed'] = rows
    summary['full_report'] = str(report_path.expanduser().resolve())
    return summary


def save_report(result: dict, report_path: Path, *, watch: bool = False) -> dict:
    """Keep the last operation's audit while exposing the current watch state."""
    report_path = report_path.expanduser().resolve()
    # Idle/pending-only polls have no new candidate or ZIP audit. They belong
    # in the watch status instead of erasing the last actionable operation.
    write_report = not watch or bool(result.get('processed') or result.get('failures')) or not report_path.is_file()
    if write_report:
        _atomic(report_path, _json_bytes(result))
    saved = {'last_report_preserved': not write_report}
    if watch:
        status_path = report_path.with_name(report_path.stem + '-estado-atual' + report_path.suffix)
        _atomic(status_path, _json_bytes({**result, 'observed_at': datetime.now(timezone.utc).isoformat(),
                                         'full_report': str(report_path), **saved}))
        saved['watch_status'] = str(status_path)
    return saved


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
    p.add_argument('--publish-url', help='Sobrescrever URL configurada; token no arquivo privado ou ambiente')
    p.add_argument('--verbose-report', action='store_true', help='Exibir também todas as notas e candidatos no terminal')
    args = p.parse_args()
    from cd_publish import PublishError, load_configuration
    try:
        args._publication_configuration = load_configuration(url=args.publish_url)
    except PublishError as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        return 2
    args.output = args.output.expanduser()
    args.report = args.report.expanduser()
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        with data_root_lock(args.data_root):
            last_console = 0.0
            while True:
                _check_sources()
                result = asyncio.run(_batch_unlocked(args))
                saved = save_report(result, args.report, watch=args.watch)
                shown = result if args.verbose_report else console_report(result, args.report)
                if args.watch:
                    shown = {**shown, **saved}
                if not args.watch or result.get('processed') or result.get('failures') or time.monotonic() - last_console >= 30:
                    print(json.dumps(shown, ensure_ascii=False, indent=2), flush=True)
                    last_console = time.monotonic()
                if not args.watch:
                    return 0 if result['ok'] else 1
                _check_sources()
                args.force = args.retry = False
                time.sleep(2)
    except ImporterGuardError as error:
        print(json.dumps(error.report(), ensure_ascii=False, indent=2), flush=True)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
