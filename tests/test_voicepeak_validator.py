"""Operator validation works from a checkout without pytest import paths."""
import json
import os
from pathlib import Path
import subprocess
import sys

from test_voicepeak_backend import voicepeak_assets  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/voicepeak-teto/validate.py'


def test_standalone_validator_saves_wav_and_reports_experimental_reading(voicepeak_assets, monkeypatch, tmp_path):
    monkeypatch.setenv('PHONE_WORKER_VOICEPEAK_TEXT_MODE', 'ptbr-kana')
    result = subprocess.run([sys.executable, '-I', str(SCRIPT), '--render-test', '--text', 'Olá, Teto.',
                             '--output', 'voice.wav'], cwd=tmp_path, capture_output=True, text=True,
                            timeout=5, env=dict(os.environ))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['ready'] and report['experimental_pronunciation']
    assert (tmp_path / 'voice.wav').read_bytes().startswith(b'RIFF')


def test_standalone_validator_reports_missing_narrator_without_audio(voicepeak_assets, tmp_path):
    _, configure, _ = voicepeak_assets
    configure(narrators=['別の声'])
    result = subprocess.run([sys.executable, '-I', str(SCRIPT), '--render-test'], cwd=tmp_path,
                            capture_output=True, text=True, timeout=5, env=dict(os.environ))
    assert result.returncode == 1
    assert not json.loads(result.stdout)['ready']
    assert not (tmp_path / 'teto-voicepeak-teste.wav').exists()
