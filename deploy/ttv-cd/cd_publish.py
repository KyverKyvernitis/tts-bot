"""Authenticated CD transport and durable client outbox.

A receipt never means installation. Secrets live in a private configuration file
or the environment, and are never included in reports or command arguments.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from urllib.parse import urlsplit, urljoin
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

CONFIG_PATH = Path.home() / '.config/osaka-cd/publish.json'
MAX_RESPONSE = 1024 * 1024
STATES = {'received', 'processing', 'applied', 'activated', 'failed', 'queued', 'unchanged'}
ERROR_CODES = {'validation_failed', 'storage_unavailable', 'configuration', 'conflict',
               'not_found', 'superseded', 'invalid_package'}


class PublishError(ValueError):
    """Only locally-authored codes, never server/credential exception strings."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(url):
    if not isinstance(url, str):
        raise PublishError('publish_url_requires_https_without_credentials')
    endpoint = urlsplit(url)
    if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or endpoint.path.rstrip('/') != '/api/osaka/cd/publish'
            or any(c.isspace() for c in url)):
        raise PublishError('publish_url_requires_https_without_credentials')
    return url.rstrip('/')


def _secret(token):
    if not isinstance(token, str) or len(token) < 32 or len(token) > 4096 or any(c.isspace() for c in token):
        raise PublishError('publish_token_missing_or_invalid')
    return token


def _atomic_private(path, value):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise PublishError('publish_config_symlink_refused')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_configuration(url, token, path=None):
    value = {'version': 1, 'publish_url': validate_url(url), 'token': _secret(token)}
    _atomic_private(path or CONFIG_PATH, value)
    return {'configured': True, 'publish_url': value['publish_url'], 'config_path': str(path or CONFIG_PATH)}


def load_configuration(path=None, *, url=None):
    path = Path(path or os.getenv('OSAKA_CD_PUBLISH_CONFIG', CONFIG_PATH)).expanduser()
    value = {}
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise PublishError('publish_config_requires_private_owned_regular_file')
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise PublishError('publish_config_invalid') from None
        if not isinstance(value, dict) or value.get('version') != 1:
            raise PublishError('publish_config_invalid')
    endpoint = url or os.getenv('OSAKA_CD_PUBLISH_URL') or value.get('publish_url')
    if not endpoint:
        return None
    token = os.getenv('OSAKA_CD_PUBLISH_TOKEN') or value.get('token')
    return {'publish_url': validate_url(endpoint), 'token': _secret(token)}


def _status_url(publish_url, supplied, job_id=None):
    base = urlsplit(validate_url(publish_url))
    if supplied:
        result = urljoin(publish_url, supplied)
    elif job_id:
        if not isinstance(job_id, str) or not job_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in job_id):
            raise PublishError('publish_invalid_job_id')
        result = urljoin(publish_url, 'jobs/' + job_id)
    else:
        result = urljoin(publish_url, 'status')
    endpoint = urlsplit(result)
    if ((endpoint.scheme, endpoint.netloc) != (base.scheme, base.netloc) or endpoint.username
            or endpoint.password or endpoint.query or endpoint.fragment
            or not endpoint.path.startswith('/api/osaka/cd/')):
        raise PublishError('publish_status_url_refused')
    return result


def _request(url, token, *, data=None, headers=None, method='GET', timeout=15):
    request = Request(url, data=data, method=method, headers={'Authorization': 'Bearer ' + _secret(token), **(headers or {})})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if response.status not in {200, 202} or len(raw) > MAX_RESPONSE:
                raise PublishError('publish_unexpected_response')
        result = json.loads(raw)
        # A failed durable job is a valid status response, with ok:false by
        # design. Preserve it so retries do not remain stuck on an old receipt.
        if not isinstance(result, dict) or (not result.get('ok') and
                not (result.get('state') == 'failed' and isinstance(result.get('job_id'), str) and result['job_id'])):
            raise PublishError('publish_unexpected_response')
        return result
    except PublishError:
        raise
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise PublishError('publish_authentication_refused') from None
        if exc.code == 409:
            raise PublishError('publish_version_conflict') from None
        raise PublishError('publish_http_' + str(exc.code)) from None
    except Exception as exc:
        raise PublishError('publish_failed_' + type(exc).__name__) from None


def _receipt(result, url):
    state = result.get('state')
    if not isinstance(state, str) or state not in STATES:
        raise PublishError('publish_unknown_job_state')
    job_id = result.get('job_id') or result.get('candidate_id')
    if state in {'received', 'processing', 'applied', 'activated', 'queued'} and not job_id:
        raise PublishError('publish_missing_job_id')
    # Legacy unchanged is merely a receipt. Only the direct store explicitly
    # confirms activated data (or an unchanged already-installed revision).
    installed = bool(state == 'activated' and result.get('installed') is True
                     or state == 'unchanged' and result.get('installed') is True)
    status_url = _status_url(url, result.get('status_url'), job_id) if result.get('job_id') or result.get('status_url') else None
    return {'state': state, 'installed': installed, 'applied': bool(result.get('applied') or state in {'applied', 'activated'}),
            'published_to_vps': True, 'job_id': job_id, 'candidate_id': result.get('candidate_id'),
            'generation': result.get('generation'), 'status_url': status_url,
            'song_versions': result.get('song_versions', {}),
            **({'error': result.get('error_code') if isinstance(result.get('error_code'), str) and result['error_code'] in ERROR_CODES
                else 'publication_failed'} if state == 'failed' else {})}


def publish_package(zip_path, url, token=None, *, expected_versions=None, retry_failed=False, timeout=120):
    from package_validation import validate_package
    validate_package(zip_path, require_cd_only=True, audio_probe=True)
    url = validate_url(url)
    secret = _secret(token if token is not None else os.getenv('OSAKA_CD_PUBLISH_TOKEN'))
    data = Path(zip_path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    headers = {'Content-Type': 'application/zip', 'X-CD-Package-SHA256': digest, 'Idempotency-Key': digest}
    if retry_failed:
        headers['X-CD-Retry-Failed'] = 'true'
    if expected_versions is not None:
        headers['X-CD-Expected-Versions'] = json.dumps(expected_versions, separators=(',', ':'), allow_nan=False)
    result = _request(url, secret, data=data, headers=headers, method='POST', timeout=timeout)
    return {**_receipt(result, url), 'zip_sha256': digest}


def publication_status(url, token, *, job_id=None, status_url=None):
    result = _request(_status_url(url, status_url, job_id), token)
    return _receipt(result, url) if job_id or status_url else result


class PublicationOutbox:
    """Persist before network I/O and resume jobs without repeated uploads."""
    def __init__(self, root, configuration):
        self.path = Path(root) / 'pending_publications.json'
        self.completed_path = Path(root) / 'published_publications.json'
        self.configuration = configuration
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            self.jobs = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            self.jobs = {}
        try:
            value = json.loads(self.completed_path.read_text(encoding='utf-8'))
            self.completed = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            self.completed = {}

    def save(self):
        _atomic_private(self.path, self.jobs)

    def enqueue(self, result):
        digest = result['zip_sha256']
        if digest in self.completed:
            return digest
        if digest not in self.jobs:
            # Unsent superseded packages cannot overwrite newer local truth.
            for previous, item in list(self.jobs.items()):
                if item.get('id') == result['id'] and (item.get('state') == 'failed'
                        or not item.get('job_id') and not item.get('published_to_vps')):
                    self.jobs.pop(previous)
            self.jobs[digest] = {'id': result['id'], 'zip': result['zip'], 'zip_sha256': digest,
                                 'state': 'local', 'attempt': 0, 'retry_after': 0}
            self.save()
        return digest

    def process(self, *, force=False, limit=8):
        reports = []
        now = time.time()
        processed = 0
        blocked_songs = set()
        # Keep each song's accepted versions in order, while giving unrelated
        # due jobs a turn before polling the same receipt again.
        groups = {}
        for digest, item in self.jobs.items():
            groups.setdefault(item.get('id'), []).append((digest, item))
        ordered = [entry for group in sorted(groups.values(), key=lambda group: group[0][1].get('retry_after', 0))
                   for entry in group]
        for digest, item in ordered:
            identity = item.get('id')
            if identity in blocked_songs:
                continue
            if not force and now < item.get('retry_after', 0):
                if item.get('published_to_vps') and item.get('state') != 'failed':
                    blocked_songs.add(identity)
                continue
            if processed >= limit:
                break
            processed += 1
            try:
                config = self.configuration
                retry_failed = bool(item.get('retry_failed') or force and item.get('state') == 'failed')
                if force and item.get('state') == 'failed' and not item.get('retry_failed'):
                    item['retry_failed'] = True
                    for key in ('job_id', 'status_url', 'expected_versions', 'published_to_vps'):
                        item.pop(key, None)
                    self.save()
                if item.get('job_id') and item.get('status_url'):
                    response = publication_status(config['publish_url'], config['token'], job_id=item['job_id'], status_url=item['status_url'])
                elif item.get('published_to_vps'):
                    # The old updater has no install-status contract. Keep its
                    # receipt visible instead of falsely concluding success.
                    item['retry_after'] = now + 60
                    reports.append({**item, 'reason': 'legacy_updater_receipt_requires_verification'})
                    continue
                else:
                    path = Path(item['zip'])
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                        raise PublishError('publication_package_missing_or_changed')
                    if 'expected_versions' not in item:
                        snapshot = publication_status(config['publish_url'], config['token'])
                        versions = snapshot.get('song_versions', snapshot.get('versions', {}))
                        if not isinstance(versions, dict):
                            raise PublishError('publish_invalid_version_snapshot')
                        item['expected_versions'] = {item['id']: versions.get(item['id'])}
                        self.save()
                    response = publish_package(path, config['publish_url'], config['token'], expected_versions=item['expected_versions'], retry_failed=retry_failed)
                item.update(response, attempt=0, retry_after=now + 2, retry_failed=False)
                reports.append({'id': item['id'], 'zip_sha256': digest, **response})
                if response['installed']:
                    # Persist completion before deleting the pending receipt.
                    # A restart between these writes is idempotent.
                    self.completed[digest] = {'id': identity, 'generation': response.get('generation'),
                                              'state': response['state'], 'installed': True}
                    _atomic_private(self.completed_path, self.completed)
                    self.jobs.pop(digest)
                elif response['state'] == 'failed':
                    item['retry_after'] = now + 3600
            except Exception as exc:
                code = str(exc) if isinstance(exc, PublishError) else 'publication_' + type(exc).__name__
                item['attempt'] = int(item.get('attempt', 0)) + 1
                item.update(error=code, retry_after=now + min(300, 2 ** min(item['attempt'], 8)))
                reports.append({'id': item.get('id'), 'zip_sha256': digest, 'state': item.get('state', 'local'),
                                'installed': False, 'published_to_vps': bool(item.get('published_to_vps')), 'error': code})
            finally:
                if digest in self.jobs and item.get('published_to_vps') and item.get('state') != 'failed':
                    blocked_songs.add(identity)
                self.save()
        return reports
