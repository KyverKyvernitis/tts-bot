#!/usr/bin/env python3
"""Build and verify the optional resident host without replacing the CLI."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import select
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time


SOURCE_COMMIT = "2ff1a4692fa88fb1b60933408125632d0c5d8356"
ENGINE_SHA256 = "e2cb6dc113593cb3b788debb51996f591a5f9d47bbd2a54df57bc1ecd843602f"
CONFIG_SHA256 = "21951154b9bafdebde0b3a1d6533bb1fde549113b63b99a573b49ea68fadbe83"
TOOLKIT = Path(__file__).resolve().parent
MAXIMUM_FILE_BYTES = 256 * 1024 * 1024


def digest(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAXIMUM_FILE_BYTES:
        raise ValueError("invalid_resident_file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect(directory):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if (manifest.get('protocol') != 1 or manifest.get('source_commit') != SOURCE_COMMIT
            or manifest.get('engine_sha256') != ENGINE_SHA256
            or manifest.get('config_sha256') != CONFIG_SHA256):
        raise ValueError('invalid_resident_manifest')
    files = manifest.get('files')
    if not isinstance(files, dict) or not 2 <= len(files) <= 128:
        raise ValueError('invalid_resident_manifest_files')
    for name, expected in files.items():
        relative = Path(name)
        if relative.is_absolute() or relative.name != name or name in {'.', '..', 'manifest.json'}:
            raise ValueError('invalid_resident_manifest_path')
        if digest(directory / name) != expected:
            raise ValueError('resident_file_digest_mismatch')
    if files.get('esper-config.ini') != CONFIG_SHA256 or 'Esper.Resident' not in files:
        raise ValueError('resident_runtime_missing')
    return manifest


def _native_probe(directory, timeout):
    """Check handshake and two errors in one physical native host process."""
    executable = directory / 'Esper.Resident'
    process = subprocess.Popen([str(executable), '--server'], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, 'DOTNET_GCHeapHardLimit': '40000000', 'DOTNET_gcServer': '0',
             'DOTNET_SYSTEM_GLOBALIZATION_INVARIANT': '1'})
    # Keep the ownership pipe open until both replies arrive: EOF deliberately
    # stops an in-flight native render if the phone worker terminates.
    deadline = time.monotonic() + timeout
    def response():
        if not select.select([process.stdout], [], [], max(0, deadline-time.monotonic()))[0]:
            raise TimeoutError('resident_probe_timeout')
        raw = process.stdout.readline(65537)
        if not raw.endswith('\n') or len(raw) > 65536:
            raise ValueError('resident_probe_protocol_mismatch')
        return json.loads(raw)
    try:
        values = [response()]
        for number in (1, 2):
            process.stdin.write(json.dumps({'id': number, 'args': []}) + '\n')
            process.stdin.flush()
            values.append(response())
        process.stdin.close()
        process.wait(timeout=max(.01, deadline-time.monotonic()))
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    if process.returncode:
        raise ValueError('resident_probe_failed')
    if (len(values) != 3 or values[0].get('ready') is not True
            or values[0].get('protocol') != 1 or values[0].get('native_resident') is not True
            or values[0].get('native_streaming') is not False
            or values[0].get('source_commit') != SOURCE_COMMIT
            or any(value.get('id') != number or value.get('ok') is not False
                   or value.get('process_id') != values[0].get('process_id')
                   for number, value in zip((1, 2), values[1:]))):
        raise ValueError('resident_probe_protocol_mismatch')


def build(*, root, dotnet, runtime='linux-arm64', config=None, output=None, timeout=600,
          probe=True):
    if runtime not in {'linux-arm64', 'linux-x64'}:
        raise ValueError('unsupported_resident_runtime')
    if type(timeout) not in (int, float) or not 10 <= timeout <= 1800:
        raise ValueError('invalid_build_timeout')
    root = Path(root).expanduser().absolute()
    destination = (Path(output).expanduser().absolute() if output is not None
                   else root / 'resident' / ('v1-' + runtime))
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError('invalid_resident_destination')
        inspect(destination)
        return {'ok': True, 'action': 'already_installed', 'runtime': runtime,
                'native_resident': True, 'native_streaming': False}
    source = TOOLKIT / 'resident'
    config = Path(config) if config else root / 'releases/v2.5.0/esper-config.ini'
    if digest(config) != CONFIG_SHA256:
        raise ValueError('config_digest_mismatch')
    dotnet = shutil.which(str(dotnet)) or str(Path(dotnet).expanduser().absolute())
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.resident-build-', dir=destination.parent) as temporary:
        base = Path(temporary)
        project = base / 'source'
        project.mkdir(mode=0o700)
        # Only the pinned project inputs enter the build; never copy bin/obj.
        for path in source.iterdir():
            if path.suffix in {'.cs', '.csproj'} or path.name == 'packages.lock.json':
                digest(path)
                shutil.copyfile(path, project / path.name)
        published = base / 'publish'
        environment = {**os.environ, 'DOTNET_CLI_HOME': str(base / 'cli'),
            'DOTNET_CLI_TELEMETRY_OPTOUT': '1', 'DOTNET_SKIP_FIRST_TIME_EXPERIENCE': '1'}
        command = [dotnet, 'publish', str(project / 'Esper.Resident.csproj'), '-c', 'Release',
                   '-r', runtime, '--self-contained', 'true', '-o', str(published),
                   '-p:RestoreLockedMode=true']
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                env=environment, check=False)
        if result.returncode:
            raise ValueError('resident_build_failed: ' + (result.stderr or result.stdout)[-2000:])
        shutil.copyfile(config, published / 'esper-config.ini')
        for path in published.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ValueError('invalid_published_resident_file')
            path.chmod(0o700 if path.name == 'Esper.Resident' else 0o600)
        files = {path.name: digest(path) for path in sorted(published.iterdir())}
        manifest = {'protocol': 1, 'source_commit': SOURCE_COMMIT, 'runtime': runtime,
                    'engine_sha256': ENGINE_SHA256, 'config_sha256': CONFIG_SHA256,
                    'native_resident': True, 'native_streaming': False,
                    'files': files}
        (published / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True) + '\n')
        (published / 'manifest.json').chmod(0o600)
        inspect(published)
        if probe:
            _native_probe(published, min(timeout, 30))
        os.rename(published, destination)
    return {'ok': True, 'action': 'installed', 'runtime': runtime,
            'native_resident': True, 'native_streaming': False}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path.home() / '.esper-utau')
    parser.add_argument('--dotnet', default='dotnet')
    parser.add_argument('--runtime', choices=('linux-arm64', 'linux-x64'), default='linux-arm64')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--no-probe', action='store_true')
    options = parser.parse_args(argv)
    try:
        result = build(root=options.root, dotnet=options.dotnet, runtime=options.runtime,
                       config=options.config, output=options.output, timeout=options.timeout,
                       probe=not options.no_probe)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result = {'ok': False, 'error': str(error)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
