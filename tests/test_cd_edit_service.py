"""Exact video URLs and metadata-only owner edits preserve installed music."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
import threading
from unittest.mock import AsyncMock, patch
import zipfile

from PIL import Image
import pytest

from osaka_cd import discord_edit as edit
from osaka_cd import discord_import as imports
from osaka_cd.store import _contract
from test_cd_import_cover_regression import installed
from test_cd_discord_import_service import package


VIDEO = 'AbcdefGh_12'


@pytest.mark.parametrize('url', [
    'https://www.youtube.com/watch?v=' + VIDEO,
    'http://youtube.com/watch?v=' + VIDEO + '&t=14&list=tracking',
    'youtube.com/watch?v=' + VIDEO,
    'https://youtu.be/' + VIDEO + '?si=tracking',
    ' https://youtu.be/' + VIDEO + ' ',
    'https://music.youtube.com/watch?v=' + VIDEO,
    'https://m.youtube.com/shorts/' + VIDEO,
    'https://www.youtube.com/live/' + VIDEO,
    'https://www.youtube.com/embed/' + VIDEO,
])
def test_only_video_identity_survives_public_youtube_links(url):
    assert edit.parse_video_url(url) == VIDEO


@pytest.mark.parametrize('url', [
    '', None, VIDEO, 'https://youtube.com/',
    'https://youtube.com/watch?v=' + VIDEO + '&v=abcdefghijk',
    'https://youtube.com/watch?v=short',
    'https://youtu.be/' + VIDEO + '/extra',
    'https://youtube.com.evil.example/watch?v=' + VIDEO,
    'https://youtube.com@127.0.0.1/watch?v=' + VIDEO,
    'https://127.0.0.1/watch?v=' + VIDEO,
    'https://youtube.com:443/watch?v=' + VIDEO,
    'ftp://youtube.com/watch?v=' + VIDEO,
    'https://youtu.be/' + VIDEO + '\n',
    'https://youtu.be/' + VIDEO + '?query=with space',
])
def test_non_video_and_arbitrary_urls_are_rejected_without_network(url):
    with pytest.raises(imports.ImportCDError) as caught:
        edit.parse_video_url(url)
    assert caught.value.code == 'invalid_video_url'
    assert len(caught.value.public_message) < 180


def test_list_editable_rows_have_snapshot_versions_and_skip_removed_and_default(installed, tmp_path):
    for identity in ('ghost-rule', 'rolling-girl', 'override', 'ttv-demo-v1', 'survivor'):
        path = package(tmp_path / (identity + '.zip'), identity)
        job = installed.store.receive(path)
        assert installed.store.process(job['job_id'])['applied']
    rows = edit.list_editable_cds(store=installed.store)
    assert {row['id'] for row in rows} == {'the-vampire', 'survivor'}
    for row in rows:
        assert row['version'] == installed.store.snapshot()['versions'][row['id']]
        assert row['notes'] > 0 and row['duration_ms'] > 0


def _overlay(directory, *, mutate=None):
    """Only the child transport/provider is simulated; publication is real."""
    request = json.loads((directory / 'request.json').read_text())
    metadata = request['metadata']
    score = json.loads((directory / 'catalog/cd_song_scores.json').read_text())[0]
    identity = score['id']
    score.update(name=metadata['title'], artist=metadata['artist'],
                 source_url='https://www.youtube.com/watch?v=' + VIDEO)
    image = io.BytesIO()
    Image.new('RGB', (48, 48), 'cyan').save(image, 'JPEG')
    jpeg = image.getvalue()
    video = {'video_id': VIDEO, 'title': 'Exact chosen video',
             'artist': metadata['artist'], 'view_count': 100,
             'thumbnail_sha256': hashlib.sha256(jpeg).hexdigest(),
             'source_sha256': score['source_sha256'],
             'score_sha256': _contract().score_sha256(score), 'revision': score['revision'],
             'selection_audit': {'scope': 'owner-selected-video'},
             'cover_source': {'selection': 'owner', 'video_id': VIDEO}}
    content = {_contract().SCORES: json.dumps([score]).encode(),
        _contract().VIDEOS: json.dumps({'version': 1, 'songs': {identity: video}}).encode(),
        f'cogs/tts/cd/thumbnails/{VIDEO}.jpg': jpeg}
    if mutate:
        mutate(content)
    with zipfile.ZipFile(directory / 'package.zip', 'w') as archive:
        for name, data in content.items():
            archive.writestr(name, data)


@pytest.mark.asyncio
async def test_exact_source_edit_changes_only_name_artist_and_cover(installed, monkeypatch):
    monkeypatch.setattr(imports, '_ACTIVATION_TIMEOUT', 0)
    before = installed.store.snapshot()
    old_root = Path(before['root'])
    old_score = json.loads((old_root / _contract().SCORES).read_text())[0]
    originals = {str(path.relative_to(old_root)): path.read_bytes() for path in
                 old_root.rglob('*') if path.is_file()}
    phases = []

    async def child(directory):
        assert not list(directory.rglob('*.mid'))
        request = json.loads((directory / 'request.json').read_text())
        assert request['metadata']['cover_video_id'] == VIDEO
        _overlay(directory)

    async def progress(stage):
        phases.append(stage)

    with patch.object(edit, '_run_editor', child):
        result = await edit.edit_cd('the-vampire', title='My Vampire', artist='DECO*27',
            video_url='https://youtu.be/' + VIDEO + '?si=ignored',
            expected_version=before['versions']['the-vampire'], progress=progress, store=installed.store)
    assert result.applied and not result.pending
    assert phases == ['preparing', 'artwork', 'installing', 'activating']
    current = Path(installed.store.snapshot()['root'])
    score = json.loads((current / _contract().SCORES).read_text())[0]
    assert score['name'] == 'My Vampire' and score['artist'] == 'DECO*27'
    for field in ('id', 'notes', 'revision', 'source_sha256', 'score_sha256',
                  'arrangement_sha256', 'preview_arrangement_sha256'):
        assert score[field] == old_score[field]
    video = json.loads((current / _contract().VIDEOS).read_text())['songs']['the-vampire']
    assert video['cover_source'] == {'selection': 'owner', 'video_id': VIDEO}
    assert video['owner_edit'] == {'display': True}
    for relative in (old_score['arrangement_path'], 'cogs/tts/cd/previews/the-vampire.mp3'):
        assert (current / relative).read_bytes() == originals[relative]
    assert all((old_root / relative).read_bytes() == data for relative, data in originals.items())
    row = edit.list_editable_cds(store=installed.store)[0]
    assert row['video_url'] == 'https://www.youtube.com/watch?v=' + VIDEO


@pytest.mark.asyncio
async def test_stale_modal_version_is_rejected_before_provider(installed):
    with patch.object(edit, '_run_editor', AsyncMock()) as child:
        with pytest.raises(imports.ImportCDError) as caught:
            await edit.edit_cd('the-vampire', expected_version='0' * 64, store=installed.store)
    assert caught.value.code == 'conflict'
    child.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_edit_reuses_valid_pinned_cover_while_provider_is_offline(installed, monkeypatch):
    monkeypatch.setattr(imports, '_ACTIVATION_TIMEOUT', 0)
    initial = installed.store.snapshot()
    async def first(directory):
        _overlay(directory)
    with patch.object(edit, '_run_editor', first):
        await edit.edit_cd('the-vampire', artist='DECO*27', video_url='https://youtu.be/' + VIDEO,
            expected_version=initial['versions']['the-vampire'], store=installed.store)
    before = installed.store.snapshot()
    previous = Path(before['root'])
    previous_cover = (previous / f'cogs/tts/cd/thumbnails/{VIDEO}.jpg').read_bytes()
    async def real_child(directory):
        await edit._prepare(directory)
    with patch.object(edit, '_run_editor', real_child), patch.object(
            __import__('catalog_import'), 'repair_metadata',
            AsyncMock(side_effect=AssertionError('Text edit must not fetch a recording'))) as network:
        result = await edit.edit_cd('the-vampire', title='Renamed CD', artist='Edited credit', video_url='',
            expected_version=before['versions']['the-vampire'], store=installed.store)
    assert result.applied
    network.assert_not_awaited()
    root = Path(installed.store.snapshot()['root'])
    score = json.loads((root / _contract().SCORES).read_text())[0]
    video = json.loads((root / _contract().VIDEOS).read_text())['songs']['the-vampire']
    assert score['name'] == 'Renamed CD' and score['artist'] == 'Edited credit'
    assert video['cover_source'] == {'selection': 'owner', 'video_id': VIDEO}
    assert (root / f'cogs/tts/cd/thumbnails/{VIDEO}.jpg').read_bytes() == previous_cover


@pytest.mark.asyncio
async def test_legacy_cover_without_provider_audit_can_be_renamed_offline(installed, tmp_path, monkeypatch):
    monkeypatch.setattr(imports, '_ACTIVATION_TIMEOUT', 0)
    path = package(tmp_path / 'legacy.zip', 'legacy-cd')
    with zipfile.ZipFile(path) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    videos = json.loads(contents[_contract().VIDEOS])
    videos['songs']['legacy-cd'].pop('selection_audit')
    contents[_contract().VIDEOS] = json.dumps(videos).encode()
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    receipt = installed.store.receive(path)
    assert installed.store.process(receipt['job_id'])['applied']
    before = installed.store.snapshot()
    previous = Path(before['root'])
    original = next(row for row in json.loads((previous / _contract().SCORES).read_text())
                    if row['id'] == 'legacy-cd')
    cover = (previous / 'cogs/tts/cd/thumbnails/abcdefghijk.jpg').read_bytes()
    async def real_child(directory):
        await edit._prepare(directory)
    with patch.object(edit, '_run_editor', real_child), patch.object(
            __import__('catalog_import'), 'repair_metadata',
            AsyncMock(side_effect=AssertionError('Legacy text edit must stay offline'))) as network:
        result = await edit.edit_cd('legacy-cd', title='Legacy renamed', video_url='',
            expected_version=before['versions']['legacy-cd'], store=installed.store)
    assert result.applied
    network.assert_not_awaited()
    root = Path(installed.store.snapshot()['root'])
    score = next(row for row in json.loads((root / _contract().SCORES).read_text())
                 if row['id'] == 'legacy-cd')
    video = json.loads((root / _contract().VIDEOS).read_text())['songs']['legacy-cd']
    assert score['name'] == 'Legacy renamed'
    assert score['notes'] == original['notes']
    assert video['selection_audit'] == {
        'scope': 'owner-kept-installed-cover', 'video_id': 'abcdefghijk', 'kept_existing_cover': True}
    assert (root / 'cogs/tts/cd/thumbnails/abcdefghijk.jpg').read_bytes() == cover


@pytest.mark.asyncio
async def test_concurrent_edit_during_capture_preserves_newer_owner_change(installed, tmp_path, monkeypatch):
    monkeypatch.setattr(imports, '_ACTIVATION_TIMEOUT', 0)
    before = installed.store.snapshot()
    old_score = json.loads((Path(before['root']) / _contract().SCORES).read_text())[0]
    path = tmp_path / 'other-owner.zip'

    async def child(directory):
        _overlay(directory)
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr(_contract().SCORES, json.dumps([dict(old_score, artist='Other owner')]))
        receipt = installed.store.receive(path)
        assert installed.store.process(receipt['job_id'])['applied']

    with patch.object(edit, '_run_editor', child):
        with pytest.raises(imports.ImportCDError) as caught:
            await edit.edit_cd('the-vampire', artist='DECO*27', video_url='https://youtu.be/' + VIDEO,
                expected_version=before['versions']['the-vampire'], store=installed.store)
    assert caught.value.code == 'conflict'
    root = Path(installed.store.snapshot()['root'])
    assert json.loads((root / _contract().SCORES).read_text())[0]['artist'] == 'Other owner'
    assert not (root / f'cogs/tts/cd/thumbnails/{VIDEO}.jpg').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['notes', 'arrangement', 'other-song'])
async def test_editor_rejects_musical_or_unrelated_payloads(installed, mutation):
    before = installed.store.snapshot()

    def mutate(content):
        if mutation == 'notes':
            data = json.loads(content[_contract().SCORES])
            data[0]['notes'][0]['midi_note'] += 1
            data[0]['score_sha256'] = _contract().score_sha256(data[0])
            content[_contract().SCORES] = json.dumps(data).encode()
        elif mutation == 'arrangement':
            source = Path(before['root']) / 'cogs/tts/cd/arrangements/the-vampire.json'
            content['cogs/tts/cd/arrangements/the-vampire.json'] = source.read_bytes()
        else:
            content['cogs/tts/cd/packages/other-song.json'] = b'{}'

    async def child(directory):
        _overlay(directory, mutate=mutate)

    with patch.object(edit, '_run_editor', child), pytest.raises(imports.ImportCDError):
        await edit.edit_cd('the-vampire', artist='DECO*27', video_url='https://youtu.be/' + VIDEO,
            expected_version=before['versions']['the-vampire'], store=installed.store)
    assert installed.store.snapshot()['generation'] == before['generation']


@pytest.mark.asyncio
async def test_cancellation_drains_physical_commit_before_temp_cleanup(installed, monkeypatch):
    monkeypatch.setattr(imports, '_ACTIVATION_TIMEOUT', 0)
    before = installed.store.snapshot()
    entered, finish = threading.Event(), threading.Event()
    verify = edit._verify_and_publish
    directories = []

    async def child(directory):
        _overlay(directory)

    def blocked(*args):
        directories.append(args[1].parent)
        entered.set()
        assert finish.wait(5)
        assert args[1].is_file()
        return verify(*args)

    with patch.object(edit, '_run_editor', child), patch.object(edit, '_verify_and_publish', blocked):
        task = asyncio.create_task(edit.edit_cd('the-vampire', artist='DECO*27',
            video_url='https://youtu.be/' + VIDEO, expected_version=before['versions']['the-vampire'],
            store=installed.store))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and directories[0].exists()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not directories[0].exists()
    assert installed.store.snapshot()['generation'] != before['generation']
