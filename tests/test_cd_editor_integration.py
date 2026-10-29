"""Standalone owner editor through real cover conversion and runtime publication."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord.http import handle_message_parameters
from PIL import Image
import pytest

from cogs.tts import melodies, cd_covers
from cogs.tts.interface import importar_cd as import_ui
from cogs.tts.interface import editar_cd as editor
from osaka_cd import discord_edit as service, runtime
from osaka_cd import discord_import
from osaka_cd.store import RuntimeStore, _contract


ROOT = Path(__file__).resolve().parents[1]
OLD_VIDEO = 'oldcover001'
NEW_VIDEO = 'newcover002'


def _midi():
    events = bytes([0, 144, 72, 90, 131, 96, 128, 72, 0,
                    0, 144, 74, 90, 131, 96, 128, 74, 0, 0, 255, 47, 0])
    return (b'MThd\0\0\0\6\0\0\0\1\1\xe0MTrk'
            + len(events).to_bytes(4, 'big') + events)


def _png(color):
    result = io.BytesIO()
    Image.new('RGB', (64, 48), color).save(result, 'PNG')
    return result.getvalue()


def _interaction(author, **data):
    return SimpleNamespace(user=author, data=data,
        response=SimpleNamespace(defer=AsyncMock(), send_modal=AsyncMock(), send_message=AsyncMock()))


def _serialized(kwargs):
    with handle_message_parameters(**kwargs) as request:
        value = request.payload
    assert discord.MessageFlags._from_value(value['flags']).components_v2
    assert value['allowed_mentions']['parse'] == []
    assert not value.get('content') and not value.get('embeds')
    return value


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'deploy/ttv-cd'))
    importer = importlib.import_module('catalog_import')
    metadata = importlib.import_module('cd_metadata')
    local = tmp_path / 'catalog'
    local.mkdir()
    (local / 'cd_song_scores.json').write_text('[]')
    (local / 'cd_song_videos.json').write_text('{"version":1,"songs":{}}')
    source = tmp_path / 'Example Song.mid'
    source.write_bytes(_midi())
    source.with_suffix('.json').write_text('{"artist":"Composer"}')
    audio = tmp_path / 'original.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
        'sine=frequency=440:duration=0.5', '-c:a', 'libmp3lame', '-y', str(audio)], check=True)

    def render(melody, target):
        target.write_bytes(audio.read_bytes())
        melody['_render_report'] = {'configuration_fingerprint': 'a' * 64,
            'mix_sha256': hashlib.sha256(audio.read_bytes()).hexdigest(), 'worker_version': '1.11.41'}
        return 'ready'

    initial = {'selected': {'video_id': OLD_VIDEO, 'title': 'Composer - Example Song',
               'channel': 'Composer', 'view_count': 500, 'duration': 180},
               'audit': {'inspected_candidates': 1, 'accepted_candidates': 1}}
    monkeypatch.setattr(importer, '_sample', render)
    monkeypatch.setattr(importer, '_search', lambda *_args, **_kwargs: initial)
    image_bytes = [_png('orange')]
    image_requests = []

    class Response:
        status = 200
        headers = {'Content-Type': 'image/png'}

        def __init__(self):
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def iter_chunked(self, _size):
            yield image_bytes[0]

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def get(self, url, **_kwargs):
            image_requests.append(url)
            return Response()

    monkeypatch.setattr('aiohttp.ClientSession', Session)
    package = tmp_path / 'initial.zip'
    imported = asyncio.run(importer.import_midi(source, root=local,
        cache_dir=tmp_path / 'covers', render_sample=True, export_zip=package))
    assert not imported['pending_artifacts']
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    store = RuntimeStore(tmp_path / 'runtime', legacy)
    job = store.receive(package)
    assert store.process(job['job_id'])['applied']
    source.unlink()
    source.with_suffix('.json').unlink()
    monkeypatch.setattr(service, 'get_store', lambda: store)
    monkeypatch.setattr(discord_import, '_ACTIVATION_TIMEOUT', 0)
    direct_requests = []

    def direct(video_id):
        direct_requests.append(video_id)
        return metadata._candidate({'video_id': video_id,
            'title': 'Original recording with a Japanese title', 'channel': 'Official source',
            'view_count': 42, 'duration': 180}, source='controlled-public-direct-response')

    monkeypatch.setattr(metadata, '_yt_dlp_video', direct)
    monkeypatch.setattr(metadata.YouTubePlayerAdapter, 'video', lambda self, video_id: direct(video_id))
    monkeypatch.setattr(metadata, 'search_metadata', lambda *_args, **_kwargs:
        pytest.fail('An exact owner video must never enter broad popularity search'))

    async def run_child(directory, **_kwargs):
        # Only replace the process transport. The actual editor child, importer,
        # exact ID resolver, image converter, overlay validation and CAS run.
        assert not list(directory.rglob('*.mid'))
        await service._prepare(directory)

    monkeypatch.setattr(service, '_run_editor', run_child)
    monkeypatch.setattr(importer, '_sample', lambda *_args: pytest.fail('Editing must not render audio'))
    return SimpleNamespace(store=store, image_bytes=image_bytes,
        image_requests=image_requests, direct_requests=direct_requests,
        source=source, png=_png('royalblue'))


@pytest.mark.asyncio
async def test_standalone_owner_selects_label_modal_exact_video_and_preserves_installed_music(installed):
    before = installed.store.snapshot()
    old_root = Path(before['root'])
    original = json.loads((old_root / _contract().SCORES).read_text())[0]
    arrangement = (old_root / original['arrangement_path']).read_bytes()
    audio = (old_root / 'cogs/tts/cd/previews/example-song.mp3').read_bytes()
    old_image = (old_root / f'cogs/tts/cd/thumbnails/{OLD_VIDEO}.jpg').read_bytes()
    installed.image_bytes[0] = installed.png
    author = SimpleNamespace(id=42)
    response = SimpleNamespace(id=800, edit=AsyncMock())
    message = SimpleNamespace(id=77, author=author, attachments=[],
        channel=SimpleNamespace(send=AsyncMock(return_value=response)),
        reference=SimpleNamespace(resolved=SimpleNamespace(attachments=[SimpleNamespace(read=AsyncMock())])))
    tasks = []

    def schedule(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    cog = SimpleNamespace(bot=SimpleNamespace(is_owner=AsyncMock(return_value=True)),
        _schedule_tts_background=schedule)
    assert await import_ui.handle_import_cd(cog, message)
    session = getattr(cog.bot, import_ui._LEASE_ATTRIBUTE)
    assert len(session.rows) == 1 and session.rows[0]['id'] == 'example-song'
    await session.view.choose(_interaction(author, values=['0']))
    opening = _interaction(author)
    await session.view.edit_cd(opening)
    modal = opening.response.send_modal.await_args.args[0]
    modal_json = modal.to_dict()
    assert [row['type'] for row in modal_json['components']] == [10, 18, 18, 18]
    assert all(row['component']['type'] == 4 for row in modal_json['components'][1:])
    modal.name._value = 'Example Song — Main Chorus'
    modal.artist._value = 'Correct Composer'
    modal.video._value = f'https://youtu.be/{NEW_VIDEO}?si=shared-link&t=30'
    await modal.on_submit(_interaction(author))
    assert len(tasks) == 1
    await asyncio.wait_for(asyncio.gather(*tasks), 10)
    after = installed.store.snapshot()
    current = Path(after['root'])
    edited = json.loads((current / _contract().SCORES).read_text())[0]
    video = json.loads((current / _contract().VIDEOS).read_text())['songs']['example-song']
    assert edited['id'] == original['id'] and edited['name'] == 'Example Song — Main Chorus'
    assert edited['artist'] == 'Correct Composer'
    for field in ('notes', 'revision', 'source_sha256', 'score_sha256', 'arrangement_sha256'):
        assert edited[field] == original[field]
    assert (current / original['arrangement_path']).read_bytes() == arrangement
    assert (current / 'cogs/tts/cd/previews/example-song.mp3').read_bytes() == audio
    assert (old_root / _contract().SCORES).read_text() != (current / _contract().SCORES).read_text()
    assert (old_root / f'cogs/tts/cd/thumbnails/{OLD_VIDEO}.jpg').read_bytes() == old_image
    assert video['video_id'] == NEW_VIDEO
    assert video['cover_source'] == {'selection': 'owner', 'video_id': NEW_VIDEO}
    image = (current / f'cogs/tts/cd/thumbnails/{NEW_VIDEO}.jpg').read_bytes()
    assert image.startswith(b'\xff\xd8\xff') and image != installed.png and image != old_image
    assert hashlib.sha256(image).hexdigest() == video['thumbnail_sha256']
    assert installed.direct_requests == [NEW_VIDEO]
    assert any(NEW_VIDEO in url for url in installed.image_requests)
    assert not installed.source.exists()
    message.reference.resolved.attachments[0].read.assert_not_awaited()
    assert message.channel.send.await_count == 1
    for kwargs in [message.channel.send.await_args.kwargs,
                   *[call.kwargs for call in response.edit.await_args_list]]:
        _serialized(kwargs)
    with runtime.unpin_snapshot(), runtime.pin_snapshot(after):
        assert melodies.get_melody('example-song').name == edited['name']
        attachment = cd_covers.cached_cover_file('example-song')
        assert attachment is not None
        try:
            assert attachment.fp.read() == image
        finally:
            attachment.close()
    assert session.waiting and not session.finished
    assert session.current()['version'] == after['versions']['example-song']
    await session.view.close(_interaction(author))
    assert not hasattr(cog.bot, import_ui._LEASE_ATTRIBUTE)


@pytest.mark.asyncio
async def test_standalone_non_owner_cannot_load_editor_catalog(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail('Unauthorized editor access must not read the catalog')
    monkeypatch.setattr(editor, 'handle_edit_cd', forbidden)
    response = SimpleNamespace(id=800, edit=AsyncMock())
    message = SimpleNamespace(author=SimpleNamespace(id=42), attachments=[],
        channel=SimpleNamespace(send=AsyncMock(return_value=response)))
    cog = SimpleNamespace(bot=SimpleNamespace(is_owner=AsyncMock(return_value=False)))
    assert await import_ui.handle_import_cd(cog, message)
    assert not hasattr(cog.bot, import_ui._LEASE_ATTRIBUTE)
    assert not response.edit.await_count
    assert 'Só o dono' in json.dumps(_serialized(message.channel.send.await_args.kwargs), ensure_ascii=False)
