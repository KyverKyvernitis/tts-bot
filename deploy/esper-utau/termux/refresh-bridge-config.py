#!/usr/bin/env python3
"""Refresh pinned ESPER bridge identities without resetting voice settings.

Run after setup.py has upgraded the installed bridge, with the worker stopped.
The configuration is parsed as data; no configured command is executed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex

TOOLKIT_ROOT = Path(__file__).resolve().parent
COMMAND_KEYS = frozenset({"PHONE_WORKER_TETO_RESAMPLER_COMMAND",
                          "PHONE_WORKER_MIKU_RESAMPLER_COMMAND"})
KNOWN_PREVIOUS_SHA256 = frozenset({
    "b80dc4c34717611a6f7dd7056dcc6c31f1e59b3b0d660b9fe746773ce16e4f74",
    "49fac2cae6396f3ee8dda7bc6e2ccbe2ae3835d48b9e25b6eeedffb5b955b43b",
})
# These quoting forms include shell quoting nested in a JSON-quoted .env value.
IDENTITY = re.compile(r'''--implementation-id(?:\\?["'])*(?:=|[ \t]+)(?:\\?["'])*([a-fA-F0-9]{64})(?![a-fA-F0-9])''')


def _activation_helper():
    spec = importlib.util.spec_from_file_location(
        "esper_bridge_refresh_activation", TOOLKIT_ROOT / "activate-esper.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _command_value(raw):
    """Decode only the two command assignments, without shell expansion."""
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith('"'):
        value, end = json.JSONDecoder().raw_decode(raw.replace("\\$", "$").replace("\\`", "`"))
        tail = raw.replace("\\$", "$").replace("\\`", "`")[end:].strip()
        if tail and not tail.startswith('#'):
            raise ValueError('invalid_assignment')
    elif raw.startswith("'"):
        match = re.fullmatch(r"'([^']*)'(?:\s*#.*)?", raw)
        if match is None:
            raise ValueError('invalid_assignment')
        value = match.group(1)
    else:
        value = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
    if not isinstance(value, str) or any(c in value for c in '\x00\r\n'):
        raise ValueError('invalid_assignment')
    return value


def _recognized_command(command, root):
    """Accept only an installed Python bridge, never a shell or custom wrapper."""
    if not command:
        return None, []
    if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', Path(command[0]).name.lower()) and len(command) >= 2:
        wrapper, arguments = command[1], command[2:]
    else:
        wrapper, arguments = command[0], command[1:]
    path = Path(wrapper).expanduser().absolute()
    if path not in {root / 'bin/resampler.py', root / 'bin/esper-utau-resampler'}:
        return None, []
    return path, arguments


def _identity_argument(arguments):
    found = []
    position = 0
    options = {'--root', '--cache-dir', '--container', '--timeout', '--implementation-id'}
    while position < len(arguments):
        argument = arguments[position]
        name, equal, value = argument.partition('=')
        if name not in options:
            raise ValueError('unsupported_bridge_option')
        if not equal:
            if position + 1 >= len(arguments) or arguments[position + 1].startswith('--'):
                raise ValueError('missing_option_value')
            value = arguments[position + 1]
            position += 2
        else:
            if not value:
                raise ValueError('missing_option_value')
            position += 1
        if name == '--implementation-id':
            found.append(value)
    if len(found) != 1 or not re.fullmatch(r'[a-fA-F0-9]{64}', found[0]):
        raise ValueError('invalid_identity')
    return found[0]


def refresh(*, env: Path, esper_root: Path, check: bool = False):
    """Publish a digest-only .env update after verifying every affected bridge."""
    result = dict(ok=False, check_only=bool(check), bridge_verified=False,
                  commands_pending=0, commands_current=0, commands_skipped=0,
                  commands_updated=0, worker_configuration_changed=False,
                  backup_created=False, restart_required=False, validation_failed=False)
    try:
        helper = _activation_helper()
        env, root = env.expanduser().absolute(), esper_root.expanduser().absolute()
        original, snapshot = helper.regular_bytes(env, helper.MAX_ENV_BYTES, allow_empty=True)
        text = original.decode('utf-8')
        if '\x00' in text:
            raise ValueError('invalid_environment')
        source, source_snapshot = helper.regular_bytes(TOOLKIT_ROOT / 'resampler.py', helper.MAX_ENV_BYTES)
        digest = hashlib.sha256(source).hexdigest()
        installed, installed_snapshot = helper.regular_bytes(root / 'bin/resampler.py', helper.MAX_ENV_BYTES)
        if installed != source:
            raise ValueError('installed_bridge_not_upgraded')
        verified = {root / 'bin/resampler.py': installed_snapshot}
        result['bridge_verified'] = True
        output = []
        for line in text.splitlines(keepends=True):
            match = helper.KEY.match(line)
            if match is None or match.group(1) not in COMMAND_KEYS:
                output.append(line)
                continue
            value = _command_value(line[match.end():])
            command = shlex.split(value)
            wrapper, arguments = _recognized_command(command, root)
            if wrapper is None:
                result['commands_skipped'] += 1
                output.append(line)
                continue
            data, wrapper_snapshot = helper.regular_bytes(wrapper, helper.MAX_ENV_BYTES)
            if data != source:
                raise ValueError('configured_bridge_not_upgraded')
            verified[wrapper] = wrapper_snapshot
            identity = _identity_argument(arguments)
            if identity.lower() == digest:
                result['commands_current'] += 1
                output.append(line)
                continue
            if identity.lower() not in KNOWN_PREVIOUS_SHA256:
                raise ValueError('unknown_bridge_identity')
            # Change the exact digest token only. Comments, shell quoting,
            # cache directories, flags and every unrelated byte are preserved.
            matches = [item for item in IDENTITY.finditer(line)
                       if item.group(1) == identity]
            if len(matches) != 1:
                raise ValueError('ambiguous_identity_format')
            token = matches[0]
            output.append(line[:token.start(1)] + digest + line[token.end(1):])
            result['commands_pending'] += 1
        updated = ''.join(output).encode('utf-8')
        # Revalidate source/installed snapshots before the transactional write.
        current_source, current_snapshot = helper.regular_bytes(TOOLKIT_ROOT / 'resampler.py', helper.MAX_ENV_BYTES)
        if current_source != source or current_snapshot != source_snapshot:
            raise ValueError('kit_changed')
        for path, prior_snapshot in verified.items():
            data, current_snapshot = helper.regular_bytes(path, helper.MAX_ENV_BYTES)
            if data != source or current_snapshot != prior_snapshot:
                raise ValueError('installed_bridge_changed')
        if not check:
            publication = helper.publish_environment(env, original, snapshot, updated)
            changed = bool(publication['changed'])
            result.update(worker_configuration_changed=changed,
                          commands_updated=result['commands_pending'] if changed else 0,
                          backup_created=bool(publication.get('backup')), restart_required=changed)
        result['ok'] = True
    except (OSError, UnicodeError, ValueError):
        # Paths, commands and .env contents must never appear in the report.
        result['validation_failed'] = True
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path,
                        default=Path(os.getenv('PHONE_WORKER_ENV') or Path.home() / '.phone-worker.env'))
    parser.add_argument('--esper-root', type=Path, default=Path.home() / '.esper-utau')
    parser.add_argument('--check', action='store_true', help='Validate and report pending identities without writing.')
    options = parser.parse_args(argv)
    result = refresh(env=options.env, esper_root=options.esper_root, check=options.check)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
