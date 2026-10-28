"""Use the existing MIDI importer to resolve shipped CD artwork off the bot loop.

No artwork or provider result is embedded here. The same metadata selection and
ArchiveMixin cover conversion used by the Termux importer run in an isolated
child process; the caller owns the validated package until its context closes.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import zipfile


_ROOT = Path(__file__).resolve().parents[1]
_SOURCES = Path(__file__).with_name('bundled')
_INPUT_HASHES = {
    'ievan-polkka.mid': '0bfdb90f44fcbfba92eb81f07703d68a16ded9a7703c8a3a4df30e0ddd163d7c',
    'ievan-polkka.json': '24086782ff61201ef0e90f8f53882fb25afeb7f19c796e7284f4b14e881195c6',
}
_IDENTITY = 'ievan-polkka'
_MAX_INPUT_BYTES = 1024 * 1024
_MAX_PACKAGE_BYTES = 4 * 1024 * 1024


class ArtworkImportError(ValueError):
    """A compact retryable import outcome, without credentials or provider URLs."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _copy_verified_inputs(destination):
    destination = Path(destination)
    for name, expected in _INPUT_HASHES.items():
        source = _SOURCES / name
        if (source.is_symlink() or not source.is_file()
                or not 0 < source.stat().st_size <= _MAX_INPUT_BYTES):
            raise ArtworkImportError('bundled_artwork_source_invalid')
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ArtworkImportError('bundled_artwork_source_checksum')
        (destination / name).write_bytes(data)


async def _prepare_importer(directory):
    """Child entry point: reuse the complete existing importer, without synthesis."""
    directory = Path(directory)
    # This function runs in the separate interpreter. The bot's import paths
    # and process environment are never mutated by importing standalone tools.
    sys.path.insert(0, str(_ROOT))
    sys.path.insert(0, str(_ROOT / 'deploy/ttv-cd'))
    from catalog_import import import_midi

    report = await import_midi(directory / 'ievan-polkka.mid',
        root=directory / 'catalog', cache_dir=directory / 'covers',
        export_zip=directory / 'artwork.zip', render_sample=False)
    pending = report.get('pending_artifacts', [])
    if (not report.get('ok') or report.get('id') != _IDENTITY
            or any(row.get('artifact') in {'metadata', 'thumbnail'} for row in pending)):
        raise ArtworkImportError('bundled_artwork_import_pending')
    _validate_imported_package(directory / 'artwork.zip')


def _validate_imported_package(package):
    from osaka_cd.store import _contract

    package = Path(package)
    if (package.is_symlink() or not package.is_file()
            or not 0 < package.stat().st_size <= _MAX_PACKAGE_BYTES):
        raise ArtworkImportError('bundled_artwork_package_invalid')
    contract = _contract()
    report = contract.validate_package(package, require_cd_only=True, audio_probe=False)
    artifact = report.get('artifacts', {}).get(_IDENTITY, {})
    if (report.get('songs') != [_IDENTITY] or artifact.get('score') != 'ready'
            or artifact.get('arrangement') != 'ready' or artifact.get('thumbnail') != 'ready'):
        raise ArtworkImportError('bundled_artwork_package_incomplete')
    with zipfile.ZipFile(package) as archive:
        scores = json.loads(archive.read(contract.SCORES))
        video = json.loads(archive.read(contract.VIDEOS)).get('songs', {}).get(_IDENTITY, {})
    if (len(scores) != 1 or scores[0].get('id') != _IDENTITY
            or scores[0].get('source_sha256') != _INPUT_HASHES['ievan-polkka.mid']
            or type(video.get('view_count')) is not int or video['view_count'] < 0
            or not isinstance(video.get('selection_audit'), dict)):
        raise ArtworkImportError('bundled_artwork_identity_invalid')
    return report


async def _stop_process(process):
    if process.returncode is not None:
        await process.wait()
        return
    # Cover conversion can launch FFmpeg. Stop the complete private session so
    # cancellation cannot leave a converter or network importer behind.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def _drain_process(process):
    physical = asyncio.create_task(_stop_process(process))
    cancelled = False
    while not physical.done():
        try:
            await asyncio.shield(physical)
        except asyncio.CancelledError:
            cancelled = True
    physical.result()
    if cancelled:
        raise asyncio.CancelledError


async def _validated_package(package):
    physical = asyncio.create_task(asyncio.to_thread(_validate_imported_package, package))
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


@asynccontextmanager
async def imported_artwork_package(*, timeout_seconds=180):
    """Yield a validated importer package; clean up only after its consumer ends."""
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 600):
        raise ValueError('invalid_artwork_import_timeout')
    with tempfile.TemporaryDirectory(prefix='osaka-bundled-artwork-') as temporary:
        directory = Path(temporary)
        _copy_verified_inputs(directory)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).resolve()), '--prepare', str(directory),
                cwd=str(_ROOT), stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            try:
                code = await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                raise ArtworkImportError('bundled_artwork_import_timeout') from None
            if code != 0:
                raise ArtworkImportError('bundled_artwork_import_unavailable')
            package = directory / 'artwork.zip'
            await _validated_package(package)
            yield package
        finally:
            if process is not None:
                await _drain_process(process)


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', required=True, type=Path)
    arguments = parser.parse_args()
    try:
        asyncio.run(_prepare_importer(arguments.prepare))
        return 0
    except Exception:
        # The parent reports only a compact retryable outcome. Provider errors
        # can contain private API URLs and must never enter updater diagnostics.
        return 1


if __name__ == '__main__':
    raise SystemExit(_main())
