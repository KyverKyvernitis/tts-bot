"""Install explicitly shipped CDs through the existing atomic catalog store.

This runs once during bot startup. Runtime readers remain read-only, and an
existing song always wins over a shipped default with the same identity.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import zipfile

from .store import StoreError, get_store


_LOG = logging.getLogger(__name__)
_MAX_PACKAGE_BYTES = 2 * 1024 * 1024
_PACKAGE = Path(__file__).with_name('bundled') / 'ievan-polkka.zip'
_PACKAGE_SHA256 = 'b17ddcd5954fc19eb55249b933a8ba59016516a88d2384382c23b483b1f1f4bd'
_SONG_ID = 'ievan-polkka'


def install_missing_cd(store, *, identity, package, sha256):
    """Apply one trusted package only while its song is still absent.

    The compare-and-swap is enforced again by RuntimeStore inside its commit
    lock, so a simultaneous user publication cannot be overwritten.
    """
    current = store.snapshot(strict=True, advertise_legacy=True)
    if identity in current['versions']:
        return {'id': identity, 'status': 'existing'}
    package = Path(package)
    if (package.is_symlink() or not package.is_file()
            or not 0 < package.stat().st_size <= _MAX_PACKAGE_BYTES):
        raise StoreError('bundled CD package missing or invalid', 'configuration')
    raw = package.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise StoreError('bundled CD checksum mismatch', 'configuration')
    # Keep the startup migration restricted to its registered song even if an
    # accidentally rebuilt package contains a wider catalog snapshot.
    with zipfile.ZipFile(package) as archive:
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


def install_bundled_cds():
    """A failed optional addition cannot prevent independent bot startup."""
    try:
        result = install_missing_cd(get_store(), identity=_SONG_ID,
                                    package=_PACKAGE, sha256=_PACKAGE_SHA256)
        _LOG.info('[cd_bundle] id=%s status=%s', _SONG_ID, result['status'])
        return result
    except Exception as exc:
        code = getattr(exc, 'code', 'configuration')
        _LOG.warning('[cd_bundle] id=%s code=%s type=%s',
                     _SONG_ID, code, type(exc).__name__)
        return {'id': _SONG_ID, 'status': 'deferred', 'error_code': code}


async def initialize_bundled_cds():
    """Keep SQLite/file work off the loop and drain it before cancellation.

    Startup owns this physical transaction until completion. No detached task
    can continue changing the catalog after its cog has finished unloading.
    """
    physical = asyncio.create_task(asyncio.to_thread(install_bundled_cds))
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
