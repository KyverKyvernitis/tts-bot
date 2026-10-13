"""Offline contracts for automatic CD catalog, colors and editor safety."""
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
from PIL import Image

from cogs.tts.cd_colors import DEFAULT_COLOR, cover_accent
from cogs.tts import melodies


def test_grayscale_cover_is_still_grayscale():
    buffer = io.BytesIO()
    Image.new('RGB', (64, 64), (179, 179, 179)).save(buffer, format='JPEG')
    result = cover_accent(buffer.getvalue())
    assert (result >> 16) == ((result >> 8) & 0xff) == (result & 0xff)
    assert cover_accent(None) == DEFAULT_COLOR


def test_reload_catalog_without_bot_restart(tmp_path, monkeypatch):
    original = melodies.list_melodies()
    catalog_file = tmp_path / 'scores.json'
    catalog_file.write_text('[]')
    monkeypatch.setattr(melodies, '_SCORE_FILE', catalog_file)
    monkeypatch.setattr(melodies, '_CATALOG_SIGNATURE', (-1, -1))
    try:
        assert len(melodies.list_melodies()) == 1
        entry = {'id': 'new-cd', 'name': 'New CD', 'artist': 'Demo', 'excerpt': '',
                 'duration_ms': 500, 'source_url': 'https://example.org', 'notes': [
                     {'midi_note': 60, 'start_ms': 0, 'duration_ms': 500, 'velocity': 90}]}
        catalog_file.write_text(json.dumps([entry])+'\n')
        assert melodies.get_melody('new-cd').name == 'New CD'
        catalog_file.write_text('not json')
        assert len(melodies.list_melodies()) == 2  # retained during publication error
    finally:
        monkeypatch.setattr(melodies, '_MELODIES', original)


