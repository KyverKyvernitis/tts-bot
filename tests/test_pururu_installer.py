"""Pinned official Púrpura downloads, atomic installation and isolated settings."""
from __future__ import annotations

import array
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import stat
import struct
import sys
import wave
import zipfile
import zlib

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _rar_header(kind, flags, payload=b''):
    header = struct.pack('<BHH', kind, flags, 7 + len(payload)) + payload
    return struct.pack('<H', zlib.crc32(header) & 0xffff) + header


def _stored_rar(files):
    """Tiny real RAR4 fixture, with independent header and payload CRCs."""
    result = bytearray(b'Rar!\x1a\x07\x00' + _rar_header(0x73, 0, b'\0' * 6))
    for name, contents in files.items():
        encoded = name.encode('ascii')
        metadata = struct.pack('<IIBIIBBHI', len(contents), len(contents), 3,
                               zlib.crc32(contents) & 0xffffffff, 0, 20, 0x30,
                               len(encoded), stat.S_IFREG | 0o644)
        result.extend(_rar_header(0x74, 0x8000, metadata + encoded))
        result.extend(contents)
    result.extend(_rar_header(0x7b, 0))
    return bytes(result)


@pytest.fixture
def installer():
    path = ROOT / 'deploy/pururu-utau/termux/install-voicebank.py'
    spec = importlib.util.spec_from_file_location('test_pururu_installer_module', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sample(installer, monkeypatch, tmp_path):
    wav = io.BytesIO()
    with wave.open(wav, 'wb') as target:
        target.setparams((1, 2, 44100, 0, 'NONE', ''))
        target.writeframes(array.array('h', [1200, -1200] * 2205).tobytes())
    contents = {
        'sample.wav': wav.getvalue(),
        'oto.ini': '\n'.join('sample.wav=' + vowel + suffix + ',0,20,-90,10,5'
                             for suffix in ('',) for vowel in ('-a', '*a', '*e', '*E', '*i', '*o', '*O', '*u', '*6~')).encode(),
        'character.txt': b'name=Pururu Purpura\n',
        'readme.txt': 'Amostra de teste; não contém gravações oficiais.\n'.encode('utf-16'),
    }
    archive = tmp_path / 'source.rar'
    archive.write_bytes(_stored_rar({'Pururu Purpura/' + name: data for name, data in contents.items()}))
    monkeypatch.setattr(installer, 'SOURCE_BYTES', archive.stat().st_size)
    monkeypatch.setattr(installer, 'SOURCE_SHA256', hashlib.sha256(archive.read_bytes()).hexdigest())
    monkeypatch.setattr(installer, 'EXPECTED_FILES', len(contents))
    monkeypatch.setattr(installer, 'EXPECTED_UNPACKED_BYTES', sum(map(len, contents.values())))
    monkeypatch.setattr(installer, 'MINIMUM_ALIASES', 1)
    inventory = [[name, len(data), hashlib.sha256(data).hexdigest()]
                 for name, data in sorted(contents.items())]
    encoded = json.dumps(inventory, ensure_ascii=False, separators=(',', ':')).encode()
    monkeypatch.setattr(installer, 'INVENTORY_SHA256', hashlib.sha256(encoded).hexdigest())
    esper_root = tmp_path / 'ESPER com espaço'
    bridge = esper_root / 'bin/resampler.py'
    bridge.parent.mkdir(parents=True)
    bridge.write_text('''import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--root')
parser.add_argument('--cache-dir')
parser.add_argument('--container')
parser.add_argument('--timeout')
parser.add_argument('--implementation-id')
''')
    env = tmp_path / 'phone.env'
    original = (b'PHONE_WORKER_TETO_ENABLED=true\nPHONE_WORKER_TETO_BASE_PITCH=C4\n'
                b'PHONE_WORKER_MIKU_ENABLED=true\nPHONE_WORKER_MIKU_ALIAS_SUFFIX=_D4\n'
                b'PHONE_WORKER_ANNA_ENABLED=true\n'
                b'BOT_SELECTED_VOICE=kasane-teto\n# local settings\n')
    env.write_bytes(original)
    return dict(archive=archive, directory=tmp_path / 'pururu-installed', env=env,
                esper_root=esper_root, contents=contents, original=original)


def _install(installer, sample, **changes):
    options = {key: sample[key] for key in ('archive', 'directory', 'env', 'esper_root')}
    return installer.install_voicebank(**dict(options, **changes))


def test_installs_the_verified_bank_atomically_without_touching_other_voices(installer, sample):
    report = _install(installer, sample)
    assert report['ok'], report
    assert report['installed'] and report['voicebank_ready']
    assert report['voicebank']['wav_decode_checked']
    assert not report['production_voice_changed']
    assert report['voice_id'] == 'pururu-purpura'
    directory, env = sample['directory'], sample['env']
    assert (directory / 'readme.txt').read_bytes() == sample['contents']['readme.txt']
    assert not (directory / 'Pururu Purpura').exists()
    assert (directory / installer.MARKER).is_file()
    updated = env.read_text()
    for line in sample['original'].decode().splitlines():
        assert line in updated
    _, values = installer.read_environment(env)
    assert values['PHONE_WORKER_PURURU_ENABLED'] == 'true'
    assert values['PHONE_WORKER_PURURU_VOICEBANK_DIR'] == str(directory)
    assert 'PHONE_WORKER_PURURU_ENGLISH_VOICEBANK_DIR' not in values
    command = shlex.split(values['PHONE_WORKER_PURURU_RESAMPLER_COMMAND'])
    assert command[:2] == [sys.executable, str(sample['esper_root'] / 'bridges/pururu' / installer.BRIDGE_SHA256 / 'resampler.py')]
    assert Path(command[1]).read_bytes() == installer.BUNDLED_BRIDGE.read_bytes()
    assert Path(report['configuration_backup']).read_bytes() == sample['original']
    assert not list(directory.parent.glob('.*pururu-install.lock'))


def test_reinstallation_is_idempotent_and_modified_bank_is_preserved(installer, sample):
    first = _install(installer, sample)
    assert first['ok'], first
    settings = sample['env'].read_bytes()
    second = _install(installer, sample)
    assert second['ok'] and not second['installed']
    assert not second['worker_configuration_changed']
    (sample['directory'] / 'sample.wav').write_bytes(b'user-modified-recording')
    failed = _install(installer, sample)
    assert not failed['ok']
    assert (sample['directory'] / 'sample.wav').read_bytes() == b'user-modified-recording'
    assert sample['env'].read_bytes() == settings


def test_dry_run_checks_real_wavs_without_publishing_anything(installer, sample):
    report = _install(installer, sample, dry_run=True)
    assert report['ok'], report
    assert report['dry_run'] and report['voicebank']['wav_decode_checked']
    assert report['would_change_configuration']
    assert not report['installed'] and not report['worker_configuration_changed']
    assert not sample['directory'].exists()
    assert sample['env'].read_bytes() == sample['original']


def test_bad_archive_hash_leaves_existing_configuration_untouched(installer, sample):
    raw = bytearray(sample['archive'].read_bytes())
    raw[-1] ^= 1
    sample['archive'].write_bytes(raw)
    report = _install(installer, sample)
    assert not report['ok'] and not report['installed']
    assert not sample['directory'].exists()
    assert sample['env'].read_bytes() == sample['original']


def test_rar_payload_corruption_is_rejected_before_publication(installer, sample, monkeypatch):
    contents = bytearray(sample['archive'].read_bytes())
    contents[contents.index(b'RIFF') + 70] ^= 1
    sample['archive'].write_bytes(contents)
    # The extracted inventory protects every byte even on libarchive builds
    # that do not enforce stored RAR entries' CRCs themselves.
    monkeypatch.setattr(installer, 'SOURCE_SHA256', hashlib.sha256(contents).hexdigest())
    report = _install(installer, sample)
    assert not report['ok'] and not report['installed']
    assert 'CRC' in report['error'] or 'inventário' in report['error']
    assert not sample['directory'].exists()
    assert sample['env'].read_bytes() == sample['original']


def test_archive_library_probe_does_not_require_download_or_configuration(installer, monkeypatch, capsys):
    assert installer.main(['--check-archive-library']) == 0
    monkeypatch.setattr(installer, 'archive_library', lambda: (_ for _ in ()).throw(
        installer.InstallError('libarchive ausente')))
    assert installer.main(['--check-archive-library']) == 1
    assert 'libarchive ausente' in capsys.readouterr().err


def test_environment_race_rolls_back_only_new_bank_and_preserves_latest_settings(installer, sample, monkeypatch):
    publish = installer.publish_environment
    latest = sample['original'] + b'USER_SETTING=new\n'

    def raced(path, original, updated):
        path.write_bytes(latest)
        return publish(path, original, updated)

    monkeypatch.setattr(installer, 'publish_environment', raced)
    report = _install(installer, sample)
    assert not report['ok'] and not report['installed']
    assert report['checks']['rollback']['ok']
    assert not sample['directory'].exists()
    assert sample['env'].read_bytes() == latest


@pytest.mark.parametrize('name', ['../outside.wav', '/outside.wav', 'bank/../outside.wav', 'bank/C:/outside.wav'])
def test_unsafe_rar_paths_are_rejected(installer, tmp_path, name):
    archive = tmp_path / 'unsafe.rar'
    archive.write_bytes(_stored_rar({name: b'not audio'}))
    with installer.open_archive(archive) as source, pytest.raises(installer.InstallError, match='caminho inseguro'):
        installer.checked_members(source)


def test_zip_symlink_and_duplicate_paths_are_rejected(installer, tmp_path):
    archive = tmp_path / 'symlink.zip'
    member = zipfile.ZipInfo('bank/symlink.wav')
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr(member, b'../../outside')
    with installer.open_archive(archive) as source, pytest.raises(installer.InstallError, match='link'):
        installer.checked_members(source)
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('bank/item', b'file')
        output.writestr('bank/item/child', b'child')
    with installer.open_archive(archive) as source, pytest.raises(installer.InstallError, match='disputam'):
        installer.checked_members(source)


def test_unicode_archive_names_are_preserved(installer, tmp_path):
    archive = tmp_path / 'unicode.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('Pururu/voz_ação.wav', b'not a voice recording')
    with installer.open_archive(archive) as source:
        members, total = installer.checked_members(source)
    assert str(members[0][0]) == 'Pururu/voz_ação.wav'
    assert total == len(b'not a voice recording')


def test_google_confirmation_form_is_restricted_to_the_pinned_public_file(installer):
    form = installer.DownloadForm()
    form.feed('<form id="download-form" action="https://drive.usercontent.google.com/download">'
              '<input name="id" value="' + installer.SOURCE_ID + '">'
              '<input name="export" value="download"><input name="confirm" value="t"></form>')
    assert form.url().startswith('https://drive.usercontent.google.com/download?')
    form.parameters['id'] = 'different-file'
    with pytest.raises(installer.InstallError):
        form.url()
    form.parameters['id'] = installer.SOURCE_ID
    form.action = 'https://example.com/download'
    with pytest.raises(installer.InstallError):
        form.url()


class _Response(io.BytesIO):
    def __init__(self, data, content_type, url, length=None):
        super().__init__(data)
        self.headers = {'Content-Type': content_type}
        if length is not None:
            self.headers['Content-Length'] = str(length)
        self.url = url

    def geturl(self):
        return self.url


def test_download_follows_confirmation_and_verifies_exact_size_and_hash(installer, sample, monkeypatch, tmp_path):
    html = ('<form id="download-form" action="https://drive.usercontent.google.com/download">'
            '<input name="id" value="' + installer.SOURCE_ID + '">'
            '<input name="export" value="download"><input name="confirm" value="t"></form>').encode()
    responses = [_Response(html, 'text/html', 'https://drive.google.com/uc'),
                 _Response(sample['archive'].read_bytes(), 'application/octet-stream',
                           'https://drive.usercontent.google.com/download', installer.SOURCE_BYTES)]
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            return responses.pop(0)

    monkeypatch.setattr(installer.urllib.request, 'build_opener', lambda *_: Opener())
    destination = tmp_path / 'download.zip'
    report = installer.download_archive(destination, timeout=30)
    assert report['sha256'] == installer.SOURCE_SHA256
    assert destination.read_bytes() == sample['archive'].read_bytes()
    assert len(calls) == 2 and calls[1].startswith('https://drive.usercontent.google.com/download?')


def test_bad_download_is_removed_instead_of_being_reused(installer, sample, monkeypatch, tmp_path):
    class Opener:
        def open(self, request, timeout):
            return _Response(b'corrupt', 'application/octet-stream', 'https://drive.google.com/uc')

    monkeypatch.setattr(installer.urllib.request, 'build_opener', lambda *_: Opener())
    destination = tmp_path / 'download.zip'
    with pytest.raises(installer.InstallError, match='SHA-256'):
        installer.download_archive(destination, timeout=30)
    assert not destination.exists()


def test_cli_dry_run_returns_json_and_does_not_install(installer, sample, capsys):
    result = installer.main(['--source', str(sample['archive']), '--directory', str(sample['directory']),
                             '--env', str(sample['env']), '--esper-root', str(sample['esper_root']), '--dry-run'])
    report = json.loads(capsys.readouterr().out)
    assert result == 0 and report['ok']
    assert not report['installed'] and not sample['directory'].exists()


@pytest.mark.parametrize('arguments', [['--container', 'custom-esper-arm64'],
                                       ['--container=custom-esper-arm64']])
@pytest.mark.parametrize('replace_root', [False, True])
def test_omitted_container_keeps_existing_bridge_container(installer, sample, arguments, replace_root):
    bridge = sample['esper_root'] / 'bin/resampler.py'
    values = {'PHONE_WORKER_TETO_RESAMPLER_COMMAND': shlex.join([sys.executable, str(bridge), *arguments]),
              'PHONE_WORKER_ESPER_CONTAINER': 'different-env-container'}
    original = values.copy()
    configured = installer.configuration(sample['directory'], values,
        esper_root=sample['esper_root'] if replace_root else None)
    command = shlex.split(configured['PHONE_WORKER_PURURU_RESAMPLER_COMMAND'])
    assert command[command.index('--container') + 1] == 'custom-esper-arm64'
    assert values == original


def test_explicit_container_overrides_bridge_and_configured_environment(installer, sample):
    bridge = sample['esper_root'] / 'bin/resampler.py'
    values = {'PHONE_WORKER_MIKU_RESAMPLER_COMMAND': shlex.join([
        sys.executable, str(bridge), '--container=old-miku-container']),
        'PHONE_WORKER_ESPER_CONTAINER': 'old-env-container'}
    configured = installer.configuration(sample['directory'], values, container='explicit-new-container')
    command = shlex.split(configured['PHONE_WORKER_PURURU_RESAMPLER_COMMAND'])
    assert command[command.index('--container') + 1] == 'explicit-new-container'


@pytest.mark.parametrize('configured_name,expected', [('configured-esper', 'configured-esper'),
                                                      ('', 'voicepeak-arm64')])
def test_container_falls_back_to_environment_then_known_default(installer, sample, configured_name, expected):
    configured = installer.configuration(sample['directory'],
        {'PHONE_WORKER_ESPER_CONTAINER': configured_name}, esper_root=sample['esper_root'])
    command = shlex.split(configured['PHONE_WORKER_PURURU_RESAMPLER_COMMAND'])
    assert command[command.index('--container') + 1] == expected


@pytest.mark.parametrize('arguments', [['--container'], ['--container='],
                                       ['--container', '--timeout', '60']])
def test_missing_existing_container_value_is_rejected_even_with_override(installer, sample, arguments):
    bridge = sample['esper_root'] / 'bin/resampler.py'
    values = {'PHONE_WORKER_TETO_RESAMPLER_COMMAND': shlex.join([sys.executable, str(bridge), *arguments])}
    with pytest.raises(installer.InstallError, match='--container sem valor'):
        installer.configuration(sample['directory'], values, container='explicit-new-container')


def test_cli_without_container_preserves_custom_container_and_other_voices(installer, sample, capsys):
    bridge = sample['esper_root'] / 'bin/resampler.py'
    command = shlex.join([sys.executable, str(bridge), '--container=custom-existing-container'])
    original = sample['original'] + ('PHONE_WORKER_TETO_RESAMPLER_COMMAND=' +
                                    installer.shell_value(command) + '\n').encode()
    sample['env'].write_bytes(original)
    result = installer.main(['--source', str(sample['archive']), '--directory', str(sample['directory']),
                             '--env', str(sample['env'])])
    report = json.loads(capsys.readouterr().out)
    assert result == 0 and report['ok']
    updated = sample['env'].read_text()
    assert all(line in updated for line in original.decode().splitlines())
    _, settings = installer.read_environment(sample['env'])
    command = shlex.split(settings['PHONE_WORKER_PURURU_RESAMPLER_COMMAND'])
    assert command[command.index('--container') + 1] == 'custom-existing-container'


def test_runtime_repair_replaces_unrecognized_bridge_without_downloading_the_bank(installer, sample, monkeypatch):
    assert _install(installer, sample)['ok']
    before = installer.inventory(sample['directory'])
    old_bridge = sample['esper_root'] / 'bin/resampler.py'
    old_bytes = old_bridge.read_bytes()
    cache = sample['esper_root'] / 'private analysis cache'
    old_command = shlex.join([sys.executable, str(old_bridge), '--root', str(sample['esper_root']),
        '--cache-dir', str(cache), '--container', 'custom-native-engine', '--timeout', '75'])
    original = sample['original'] + ('PHONE_WORKER_PURURU_RESAMPLER_COMMAND=' +
                                     installer.shell_value(old_command) + '\n').encode()
    sample['env'].write_bytes(original)
    for name in ('download_archive', 'open_archive', 'archive_library'):
        monkeypatch.setattr(installer, name, lambda *a, **k: pytest.fail('repair must not download or extract'))
    report = installer.install_voicebank(archive=None, directory=sample['directory'],
        env=sample['env'], repair_runtime=True)
    assert report['ok'] and report['runtime_repair'] and not report['installed'], report
    assert installer.inventory(sample['directory']) == before
    assert old_bridge.read_bytes() == old_bytes
    _, values = installer.read_environment(sample['env'])
    command = shlex.split(values['PHONE_WORKER_PURURU_RESAMPLER_COMMAND'])
    assert hashlib.sha256(Path(command[1]).read_bytes()).hexdigest() == installer.BRIDGE_SHA256
    assert command[command.index('--root') + 1] == str(sample['esper_root'])
    assert command[command.index('--cache-dir') + 1] == str(cache)
    assert command[command.index('--container') + 1] == 'custom-native-engine'
    assert command[command.index('--timeout') + 1] == '75'
    assert command[command.index('--implementation-id') + 1] == installer.BRIDGE_SHA256
    assert all(line in sample['env'].read_text() for line in sample['original'].decode().splitlines())
    settings = sample['env'].read_bytes()
    again = installer.install_voicebank(archive=None, directory=sample['directory'], env=sample['env'], repair_runtime=True)
    assert again['ok'] and not again['worker_configuration_changed']
    assert sample['env'].read_bytes() == settings


def test_runtime_repair_cli_does_not_require_archive_or_download(installer, sample, monkeypatch, capsys):
    assert _install(installer, sample)['ok']
    monkeypatch.setattr(installer, 'download_archive', lambda *a, **k: pytest.fail('unexpected download'))
    assert installer.main(['--repair-runtime', '--directory', str(sample['directory']),
        '--env', str(sample['env'])]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['ok'] and report['runtime_repair'] and not report['installed']


def test_runtime_repair_requires_an_existing_verified_bank(installer, sample):
    report = _install(installer, sample, archive=None, repair_runtime=True)
    assert not report['ok'] and 'ainda não está instalada' in report['error']
    assert not sample['directory'].exists()
    assert sample['env'].read_bytes() == sample['original']


def test_runtime_repair_environment_race_rolls_back_only_new_bridge(installer, sample, monkeypatch):
    assert _install(installer, sample)['ok']
    _, values = installer.read_environment(sample['env'])
    command = shlex.split(values['PHONE_WORKER_PURURU_RESAMPLER_COMMAND'])
    runtime = Path(command[1])
    runtime.unlink()
    before = installer.inventory(sample['directory'])
    original = sample['env'].read_bytes()
    latest = original + b'LOCAL_SETTING=changed\n'
    publish = installer.publish_environment
    def raced(path, original, updated):
        path.write_bytes(latest)
        return publish(path, original, updated)
    monkeypatch.setattr(installer, 'publish_environment', raced)
    report = installer.install_voicebank(archive=None, directory=sample['directory'], env=sample['env'], repair_runtime=True)
    assert not report['ok'] and report['checks']['runtime_rollback']['ok'], report
    assert not runtime.exists()
    assert installer.inventory(sample['directory']) == before
    assert sample['env'].read_bytes() == latest


def test_changed_shipped_bridge_is_rejected_before_any_publication(installer, sample, monkeypatch, tmp_path):
    fake = tmp_path / 'changed-bridge.py'
    fake.write_bytes(installer.BUNDLED_BRIDGE.read_bytes() + b'\n# changed\n')
    monkeypatch.setattr(installer, 'BUNDLED_BRIDGE', fake)
    report = _install(installer, sample)
    assert not report['ok'] and 'versão fixada' in report['error']
    assert sample['env'].read_bytes() == sample['original']
    assert not sample['directory'].exists()


def test_changed_owned_bridge_is_preserved_and_not_executed(installer, sample):
    assert _install(installer, sample)['ok']
    _, values = installer.read_environment(sample['env'])
    path = Path(shlex.split(values['PHONE_WORKER_PURURU_RESAMPLER_COMMAND'])[1])
    path.write_bytes(b'raise RuntimeError("user changed bridge")\n')
    before = sample['env'].read_bytes()
    report = installer.install_voicebank(archive=None, directory=sample['directory'], env=sample['env'], repair_runtime=True)
    assert not report['ok'] and 'existente foi alterada' in report['error']
    assert path.read_bytes() == b'raise RuntimeError("user changed bridge")\n'
    assert sample['env'].read_bytes() == before


def test_pinned_bridge_is_recognized_by_the_shipped_renderer(installer):
    import ast
    tree = ast.parse((ROOT / 'deploy/termux/phone-worker/teto_renderer/renderer.py').read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'TetoRenderer')
    identity = next(ast.literal_eval(node.value) for node in cls.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'BACKGROUND_BRIDGE_SHA256' for target in node.targets))
    assert installer.BRIDGE_SHA256 == identity
    assert hashlib.sha256(installer.bundled_bridge_bytes()).hexdigest() == identity
