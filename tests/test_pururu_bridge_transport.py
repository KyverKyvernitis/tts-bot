"""Pururu enters the pinned ESPER bridge without a Python launch per phoneme."""
from __future__ import annotations

import array
import hashlib
from pathlib import Path
import shlex
import subprocess
import sys
from unittest.mock import patch
import wave

from test_pururu_renderer import renderer, wav_pcm


ROOT = Path(__file__).resolve().parents[1]


def test_pinned_shipped_bridge_uses_one_reusable_client_for_native_portuguese(renderer, tmp_path):
    bridge = ROOT / 'deploy/esper-utau/termux/resampler.py'
    digest = hashlib.sha256(bridge.read_bytes()).hexdigest()
    environment = dict(renderer._environment)
    environment['PHONE_WORKER_TETO_RESAMPLER_COMMAND'] = shlex.join([
        sys.executable, str(bridge), '--root', str(tmp_path / 'esper'),
        '--cache-dir', str(tmp_path / 'esper-cache'), '--container', 'custom-esper',
        '--implementation-id', digest,
    ])
    renderer = type(renderer)(environment=environment, voice_id='pururu-purpura')
    command = renderer._resampler_command()
    client = renderer._esper_inprocess_client(command)
    assert client is not None
    assert client.container == 'custom-esper'
    calls = []

    def synthesize(values, **controls):
        assert controls['deadline'] > 0
        calls.append(values)
        with wave.open(values[1], 'wb') as output:
            output.setparams((1, 2, 44100, 0, 'NONE', ''))
            output.writeframes(array.array('h', [1700, -1700] * round(float(values[6]) * 22.05)).tobytes())
        return {'ok': True, 'bridge_in_process': True}

    try:
        with patch.object(client, 'synthesize', side_effect=synthesize), \
             patch('teto_renderer.renderer.subprocess.run', side_effect=AssertionError('per-phoneme launcher spawned')):
            result = renderer.synthesize('casa', reading_language='pt-BR')
        assert renderer._esper_inprocess_client(command) is client
        assert result['voice_id'] == 'pururu-purpura'
        assert result['resampler_calls'] == len(calls) == 3
        assert result['voicebank_profile'] == 'portuguese-cvvc'
        assert not result['missing_phonemes'] and any(wav_pcm(result['audio']))
    finally:
        client.close()


def test_unrecognized_bridge_retains_cli_compatibility_instead_of_loading_untrusted_source(renderer):
    command = renderer._resampler_command()
    assert renderer._esper_inprocess_client(command) is None
    native_run = subprocess.run
    with patch('teto_renderer.renderer.subprocess.run', wraps=native_run) as processes:
        result = renderer.synthesize('casa', reading_language='pt-BR')
    assert result['resampler_calls'] == processes.call_count == 3
    assert all(call.args[0][:len(command)] == command for call in processes.call_args_list)
    assert renderer._esper_client_cache is None
