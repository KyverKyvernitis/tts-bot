"""Choice transport and incomplete-artwork imports cannot silently publish."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import zipfile

import pytest

from osaka_cd import discord_import as service
from osaka_cd.store import RuntimeStore
from test_cd_discord_import_service import midi, package


def choices(count=5):
    return [{'title': f'Song {index}', 'artist': f'Composer {index}',
             'aliases': [f'曲 {index}'], 'artist_aliases': [],
             'seed_video_ids': [f'{index:011d}'],
             'search_query': f'Song {index} Composer {index}'}
            for index in range(count)]


def test_choice_boundary_copies_only_metadata_and_limits_distinct_results():
    candidates = choices(8)
    candidates[0].update(root='/tmp/private', token='secret', thumbnail_url='http://127.0.0.1/')
    error = service.ImportCDError('metadata_ambiguous', candidates=candidates)
    assert len(error.candidates) == 5
    assert set(error.candidates[0]) == {
        'title', 'artist', 'aliases', 'artist_aliases', 'seed_video_ids', 'search_query'}
    candidates[0]['aliases'].append('late provider mutation')
    assert 'late provider mutation' not in error.candidates[0]['aliases']
    assert 'secret' not in json.dumps(error.candidates)
    assert 'Informe o artista' not in error.public_message


@pytest.mark.parametrize('mutation', [
    {'artist': ''}, {'title': 'bad\nname'}, {'artist': 'https://private.example/token'},
    {'seed_video_ids': ['http://127.0.0.1/']}, {'aliases': ['a'] * 13},
    {'search_query': 'a' * 321}, {'title': 'a' * 101}, {'artist_aliases': 'composer'},
])
def test_choice_boundary_rejects_malformed_identity(mutation):
    row = choices(1)[0] | mutation
    assert not service.ImportCDError('metadata_ambiguous', candidates=[row]).candidates


@pytest.mark.asyncio
async def test_child_failure_retains_candidates_and_does_not_leak_report_fields(tmp_path):
    service._dump(tmp_path / 'discovery.json', {'ok': False, 'code': 'metadata_ambiguous',
        'candidates': [choices(1)[0] | {'provider_url': 'https://secret.example'}]})
    process = SimpleNamespace(pid=12345, returncode=1, wait=AsyncMock(return_value=1))
    with patch.object(asyncio, 'create_subprocess_exec', new=AsyncMock(return_value=process)):
        with pytest.raises(service.ImportCDError) as caught:
            await service._run_discovery(tmp_path)
    assert caught.value.code == 'metadata_ambiguous'
    assert caught.value.candidates == tuple(choices(1))
    assert 'secret' not in str(caught.value) + json.dumps(caught.value.candidates)


def test_discovery_command_serializes_only_safe_candidates_on_ambiguous_error(tmp_path, monkeypatch):
    from osaka_cd import title_metadata
    error = title_metadata.TitleMetadataError('metadata_ambiguous')
    error.candidates = [choices(1)[0] | {'private_token': 'do not serialize'}]
    service._dump(tmp_path / 'source.json', {'title': 'Song'})
    monkeypatch.setattr(service.sys, 'argv', ['discord_import', '--discover', str(tmp_path)])
    with patch.object(title_metadata, 'resolve_title_metadata', side_effect=error):
        assert service._main() == 1
    report = json.loads((tmp_path / 'discovery.json').read_text())
    assert report['code'] == 'metadata_ambiguous'
    assert report['candidates'] == choices(1)
    assert 'private_token' not in json.dumps(report)


def test_choices_only_mode_can_return_alternatives_even_without_unique_identity(tmp_path):
    from osaka_cd import title_metadata
    service._dump(tmp_path / 'source.json', {'title': 'Song', 'choices_only': True})
    with patch.object(title_metadata, 'discover_title_candidates', return_value=choices()) as lookup, \
         patch.object(title_metadata, 'resolve_title_metadata', side_effect=AssertionError('must not auto-resolve')):
        service._discover_metadata(tmp_path)
    lookup.assert_called_once_with('Song', artist='')
    report = json.loads((tmp_path / 'discovery.json').read_text())
    assert report == {'ok': True, 'candidates': choices()}


@pytest.fixture
def store(tmp_path, monkeypatch):
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    monkeypatch.setattr(service, '_ACTIVATION_TIMEOUT', 0)
    return RuntimeStore(tmp_path / 'runtime', legacy)


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['metadata', 'thumbnail'])
async def test_incomplete_midi_raises_choices_before_catalog_changes(store, missing):
    initial = store.snapshot(strict=True, advertise_legacy=True)

    async def importer(directory):
        target = directory / 'package.zip'
        package(target, 'demo', cover=missing == 'thumbnail')
        if missing == 'thumbnail':
            with zipfile.ZipFile(target) as archive:
                content = {info.filename: archive.read(info) for info in archive.infolist()
                           if not info.filename.endswith('.jpg')}
            videos = json.loads(content['cogs/tts/cd_song_videos.json'])
            videos['songs']['demo'].pop('thumbnail_sha256')
            content['cogs/tts/cd_song_videos.json'] = json.dumps(videos).encode()
            with zipfile.ZipFile(target, 'w') as archive:
                for path, data in content.items():
                    archive.writestr(path, data)
        return {'ok': True, 'pending': [missing]}

    phases = []

    async def progress(phase):
        phases.append(phase)

    with patch.object(service, '_run_importer', side_effect=importer), \
         patch.object(service, '_run_discovery', new=AsyncMock(return_value={'ok': True, 'candidates': choices()})), \
         patch.object(service, '_publish', side_effect=AssertionError('must not publish incomplete MIDI')):
        with pytest.raises(service.ImportCDError) as caught:
            await service.import_cd(service.prepare_input('Composer - Demo.mid', midi()),
                                    store=store, progress=progress)
    assert caught.value.code == ('metadata_ambiguous' if missing == 'metadata' else 'artwork_unavailable')
    assert caught.value.candidates == tuple(choices())
    assert phases[-1] == 'artwork'
    assert store.snapshot()['generation'] == initial['generation']
    assert store.snapshot()['versions'] == initial['versions']


@pytest.mark.asyncio
async def test_cover_retry_search_failure_preserves_original_cover_error(store):
    async def importer(directory):
        package(directory / 'package.zip', 'demo', cover=False)
        return {'ok': True, 'pending': ['metadata', 'thumbnail']}
    with patch.object(service, '_run_importer', side_effect=importer), \
         patch.object(service, '_run_discovery', new=AsyncMock(side_effect=service.ImportCDError('metadata_unavailable'))):
        with pytest.raises(service.ImportCDError) as caught:
            await service.import_cd(service.prepare_input('Composer - Demo.mid', midi()), store=store)
    assert caught.value.code == 'metadata_ambiguous'
    assert caught.value.candidates == ()
    assert not store.snapshot()['versions']


@pytest.mark.asyncio
async def test_user_selected_identity_resumes_and_publishes_ready_package(store):
    async def importer(directory):
        metadata = json.loads((directory / 'source.json').read_text())
        assert metadata['artist'] == 'Composer 0'
        assert metadata['seed_video_ids'] == ['00000000000']
        assert metadata['id'] == 'demo'
        package(directory / 'package.zip', 'demo')
        return {'ok': True, 'pending': []}
    selected = choices(1)[0] | {'id': 'demo'}
    with patch.object(service, '_run_importer', side_effect=importer), \
         patch.object(service, '_run_discovery', side_effect=AssertionError('selected identity must not re-discover')):
        result = await service.import_cd(service.prepare_input('Demo.mid', midi()),
                                        overrides=selected, store=store)
    assert result.applied
    assert not result.pending
    assert set(store.snapshot()['versions']) == {'demo'}

