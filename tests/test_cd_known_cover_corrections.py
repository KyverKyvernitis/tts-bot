"""Known delivered sources recover official images without changing music."""
from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock
import zipfile

from PIL import Image
import pytest

from osaka_cd import discord_import as service
from osaka_cd import import_artwork_repair as repair
from osaka_cd.store import RuntimeStore, _contract
from test_cd_import_cover_regression import _external_cover


ROOT = Path(__file__).resolve().parents[1]
_SOURCES = {'monitoring': 'TVRoZAAAAAYAAQACAeBNVHJrAAAA7AD/AQ9PU0FLQV9DRF9NT05PPTEA/wMUTW9uaXRvcmluZyAtIERFQ08qMjcA/1EDBu+RAP9YBAQCGAgA/wFUQ29tcG9zaXRpb246IERFQ08qMjcuIFRyYW5zY3JpcHRpb246IFJ1aXNnaS1rZW4uIGh0dHBzOi8vb25saW5lc2VxdWVuY2VyLm5ldC80MzcwNTkyAP8BUEZpcnN0IHJlZnJhaW4sIHNvdXJjZSBzaXh0ZWVudGggdW5pdHMzNzgtNjM2LiBPcmlnaW5hbCBIYXRzdW5lIE1pa3UgbWVsb2R5IG9ubHkugfFw/y8ATVRyawAABDsA/wMbVm9jYWwgTWVsb2R5IC0gSGF0c3VuZSBNaWt1AMAAAJBIX4JogEgAAJBJX4JogEkAAJBKX4FwgEoAAJA+X3iAPgAAkD5feIA+AACQPl94gD4AeJA+X3iAPgAAkD5feIA+AACQQF+CaIBAAACQQV+CaIBBAACQQ1+DYIBDAACQRV+BcIBFAIFwkEFfeIBBAACQQV94gEEAAJBDX4FwgEMAAJBFX3iARQAAkENfgXCAQwAAkEFfeIBBAACQQV+BcIBBAACQRF+BcIBEAIFwkENfgXCAQwCBcJBBX4FwgEEAAJBBX3iAQQAAkD5fgmiAPgAAkDxfg2CAPAAAkD5fgXCAPgCBcJBFX4FwgEUAAJBFX4FwgEUAAJBFX3iARQAAkEhfgXCASAAAkEhfeIBIAACQSl+CaIBKAIFwkEpfeIBKAACQSl94gEoAAJBKX3iASgAAkEpfeIBKAACQTF+CaIBMAHiQTF94gEwAAJBMX3iATAAAkExfeIBMAACQTF94gEwAAJBNX4JogE0AAJBFX4FwgEUAAJBFX4FwgEUAAJBDX3iAQwAAkEFfgmiAQQCBcJBBX4FwgEEAAJBDX4FwgEMAAJBEX4FwgEQAeJBEX4FwgEQAeJBEX3iARAAAkERfeIBEAACQQ1+CaIBDAACQQV+CaIBBAACQPV+BcIA9AACQPl94gD4AeJA+X3iAPgB4kD5feIA+AHiQPF94gDwAAJA+X4JogD4AAJBIX4JogEgAAJBJX4JogEkAAJBKX4FwgEoAAJA+X3iAPgAAkD5feIA+AACQPl94gD4AeJA+X3iAPgAAkD5feIA+AACQQF+CaIBAAACQQV+CaIBBAACQQ1+DYIBDAACQRV+BcIBFAIFwkEFfeIBBAACQQV94gEEAAJBDX4FwgEMAAJBFX3iARQAAkENfgXCAQwAAkEFfeIBBAACQQV+BcIBBAACQRF+BcIBEAIFwkENfgXCAQwCBcJBBX4FwgEEAAJBBX3iAQQAAkD5fgmiAPgAAkDxfg2CAPAAAkD5fgXCAPgCBcJBFX3iARQAAkEVfeIBFAACQRV+BcIBFAACQRV94gEUAAJBIX4FwgEgAAJBIX3iASAAAkEpfgmiASgCBcJBKX3iASgAAkEpfeIBKAACQSl94gEoAAJBKX3iASgAAkExfgmiATAB4kExfeIBMAACQTF94gEwAAJBMX3iATAAAkExfeIBMAACQTV+CaIBNAACQRV+BcIBFAACQRV+BcIBFAACQQ194gEMAAJBBX4JogEEAgXCQQV+BcIBBAACQQ1+BcIBDAACQRF+BcIBEAHiQRF+BcIBEAHiQRF94gEQAAJBEX3iARAAAkENfgmiAQwAAkEFfgmiAQQAAkD1fgXCAPQAAkD5feIA+AHiQPl94gD4AeJA+X3iAPgB4kDxfeIA8AACQPl+EWIA+AAD/LwA=', 'tetoris': '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'}


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'deploy/ttv-cd'))
    return importlib.import_module('catalog_import'), importlib.import_module('cd_metadata')


def _jpeg(color):
    out = io.BytesIO()
    Image.new('RGB', (64, 64), color).save(out, 'JPEG')
    return out.getvalue()


async def _installed(tmp_path, monkeypatch, modules, identity):
    importer, metadata = modules
    correction = repair._PREVIOUS_COVER_CORRECTIONS[identity]
    midi = tmp_path / (correction['title'] + '.mid')
    midi.write_bytes(base64.b64decode(_SOURCES[identity]))
    assert hashlib.sha256(midi.read_bytes()).hexdigest() == correction['source_sha256']
    midi.with_suffix('.json').write_text(json.dumps({'artist': 'Wrong automatic composer'}))
    local = tmp_path / 'catalog'; local.mkdir()
    (local / 'cd_song_scores.json').write_text('[]')
    (local / 'cd_song_videos.json').write_text('{"version":1,"songs":{}}')
    audio = tmp_path / 'sample.mp3'
    from midi_extract import extract_midi
    seconds = extract_midi(midi)['score']['duration_ms'] / 1000
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    f'sine=frequency=440:duration={seconds}', '-c:a', 'libmp3lame',
                    '-y', str(audio)], check=True)
    def render(melody, target):
        target.write_bytes(audio.read_bytes())
        melody['_render_report'] = {'configuration_fingerprint': 'a' * 64,
            'mix_sha256': hashlib.sha256(audio.read_bytes()).hexdigest(),
            'worker_version': '1.11.41'}
        return 'ready'
    monkeypatch.setattr(importer, '_sample', render)
    monkeypatch.setattr(importer, '_search', lambda *a, **k: {
        'selected': {'video_id': 'wrongclip00',
                     'title': 'Wrong automatic composer - ' + correction['title'],
                     'channel': 'Wrong automatic composer', 'view_count': 99999999,
                     'duration': 180}, 'audit': {'inspected_candidates': 1}})
    wrong = _jpeg('gold')
    # The initial ready cover models the already published wrong image.
    with monkeypatch.context() as initial:
        initial.setattr(importer, '_cover_for', AsyncMock(return_value=(
            wrong, hashlib.sha256(wrong).hexdigest())))
        package = tmp_path / 'initial.zip'
        await importer.import_midi(midi, root=local, cache_dir=tmp_path / 'covers',
                                   render_sample=True, export_zip=package)
    legacy = tmp_path / 'legacy'; legacy.mkdir()
    store = RuntimeStore(tmp_path / 'runtime', legacy)
    snapshot = store.snapshot(strict=True, advertise_legacy=True)
    service._publish(store, package, snapshot, request_hash='a' * 64)
    midi.unlink()
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize('identity', ['monitoring', 'tetoris'])
async def test_ready_wrong_cover_is_corrected_by_source_without_music_writes(tmp_path, monkeypatch, modules, identity):
    importer, metadata = modules
    store = await _installed(tmp_path, monkeypatch, modules, identity)
    correction = repair._PREVIOUS_COVER_CORRECTIONS[identity]
    before = store.snapshot()
    old = Path(before['root'])
    old_score = json.loads((old / _contract().SCORES).read_text())[0]
    old_audio = (old / f'cogs/tts/cd/previews/{identity}.mp3').read_bytes()
    old_arrangement = (old / old_score['arrangement_path']).read_bytes()
    assert _contract().validate_cd_tree(old, [identity])['artifacts'][identity]['thumbnail'] == 'ready'
    assert repair._targets(store) == [identity]
    output = io.BytesIO()
    Image.new('RGB', (80, 61), 'turquoise').save(output, 'PNG')
    requests = _external_cover(monkeypatch, output.getvalue())
    monkeypatch.setattr(importer, '_sample', lambda *_: pytest.fail('repair rendered audio'))
    monkeypatch.setattr(importer, '_search', lambda *_a, **_k: pytest.fail('correction ran popularity search'))
    original_video_metadata = metadata.video_metadata
    exact_requests = []
    def exact(video_id, **kwargs):
        exact_requests.append(video_id)
        return original_video_metadata(video_id, candidates=[
            {'video_id': correction['video_id'], 'title': correction['aliases'][0],
             'channel': correction['artist'], 'duration': 180, 'view_count': 1000},
            {'video_id': 'wrongclip00', 'title': correction['title'],
             'channel': 'Wrong automatic composer', 'duration': 180, 'view_count': 99999999}])
    monkeypatch.setattr(metadata, 'video_metadata', exact)
    from osaka_cd import title_metadata
    monkeypatch.setattr(title_metadata, 'resolve_title_metadata',
                        lambda *_: pytest.fail('known source needs no ambiguous title lookup'))
    async def child(directory):
        assert not list(directory.rglob('*.mid'))
        await repair._prepare(directory)
    monkeypatch.setattr(repair, '_run_repair_child', child)
    assert await repair._repair_one(store, identity) == 'repaired'
    after = store.snapshot()
    root = Path(after['root'])
    score = json.loads((root / _contract().SCORES).read_text())[0]
    assert score['name'] == correction['title']
    assert score['artist'] == correction['artist']
    assert {key: value for key, value in score.items() if key not in {'artist', 'source_url'}} == {
        key: value for key, value in old_score.items() if key not in {'artist', 'source_url'}}
    assert (root / score['arrangement_path']).read_bytes() == old_arrangement
    assert (root / f'cogs/tts/cd/previews/{identity}.mp3').read_bytes() == old_audio
    assert (old / old_score['arrangement_path']).read_bytes() == old_arrangement
    assert (old / f'cogs/tts/cd/previews/{identity}.mp3').read_bytes() == old_audio
    video = json.loads((root / _contract().VIDEOS).read_text())['songs'][identity]
    assert exact_requests == [correction['video_id']]
    assert video['video_id'] == correction['video_id']
    assert video['cover_source'] == {'selection': 'official-correction', 'video_id': correction['video_id']}
    assert video['selection_audit']['owner_selected'] is False
    assert requests and all(f'/{correction["video_id"]}/' in url for url in requests)
    image = (root / f'cogs/tts/cd/thumbnails/{correction["video_id"]}.jpg').read_bytes()
    assert image != output.getvalue() and image.startswith(b'\xff\xd8\xff')
    Image.open(io.BytesIO(image)).load()
    assert repair._targets(store) == []


@pytest.mark.parametrize('identity', ['monitoring', 'tetoris'])
def test_source_correction_cannot_override_owner_or_unrelated_attachment(identity):
    correction = repair._PREVIOUS_COVER_CORRECTIONS[identity]
    score = {'id': identity, 'name': correction['title'],
             'source_sha256': correction['source_sha256'],
             'discord_import': {'source_sha256': correction['source_sha256']}}
    assert repair._previous_cover_correction(score, {})
    assert repair._previous_cover_correction(score,
        {'cover_source': {'selection': 'owner', 'video_id': 'wrongclip00'}}) is None
    assert repair._previous_cover_correction(score, {'owner_edit': {'display': True}}) is None
    for change in ({'source_sha256': '0' * 64}, {'id': 'other-song'},
                   {'name': 'Owner title'}, {'discord_import': {'source_sha256': '0' * 64}}):
        assert repair._previous_cover_correction(dict(score, **change), {}) is None


@pytest.mark.asyncio
async def test_owner_change_while_correcting_is_not_overwritten(tmp_path, monkeypatch, modules):
    importer, metadata = modules
    identity = 'monitoring'
    store = await _installed(tmp_path, monkeypatch, modules, identity)
    before = store.snapshot()
    old = Path(before['root'])
    original = json.loads((old / _contract().SCORES).read_text())[0]
    correction = repair._PREVIOUS_COVER_CORRECTIONS[identity]
    image = _jpeg('turquoise')
    def exact(*args, **kwargs):
        return {'selected': {'video_id': correction['video_id'], 'title': 'モニタリング',
                             'view_count': 1000, 'channel': 'DECO*27', 'duration': 180},
                'audit': {'inspected_candidates': 1}}
    monkeypatch.setattr(metadata, 'video_metadata', exact)
    monkeypatch.setattr(importer, '_cover_for', AsyncMock(return_value=(image, hashlib.sha256(image).hexdigest())))
    async def raced(directory):
        await repair._prepare(directory)
        owner = tmp_path / 'owner.zip'
        with zipfile.ZipFile(owner, 'w') as archive:
            archive.writestr(_contract().SCORES, json.dumps([dict(original, artist='Owner choice')]))
        job = store.receive(owner, expected_versions={identity: before['versions'][identity]})
        assert store.process(job['job_id'])['applied']
    monkeypatch.setattr(repair, '_run_repair_child', raced)
    with pytest.raises(service.ImportCDError) as caught:
        await repair._repair_one(store, identity)
    assert caught.value.code == 'conflict'
    current = Path(store.snapshot()['root'])
    assert json.loads((current / _contract().SCORES).read_text())[0]['artist'] == 'Owner choice'
    assert not (current / f'cogs/tts/cd/thumbnails/{correction["video_id"]}.jpg').exists()
