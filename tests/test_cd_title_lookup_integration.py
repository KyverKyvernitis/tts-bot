"""Title-only owner imports through discovery, real artwork conversion and store."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

from PIL import Image
import pytest

from cogs.tts.interface import importar_cd as ui
from osaka_cd import discord_import as service
from osaka_cd.store import RuntimeStore, _contract


ROOT = Path(__file__).resolve().parents[1]


def _midi():
    notes = bytes([0, 144, 60, 100, 131, 96, 128, 60, 0,
                   0, 144, 64, 100, 131, 96, 128, 64, 0, 0, 255, 47, 0])
    return (b'MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0MTrk'
            + len(notes).to_bytes(4, 'big') + notes)


def _candidates(artist='Circuit Composer', video_id='abcdefghijk'):
    return [{'video_id': video_id, 'title': f'{artist} - Circling Rooms',
             'channel': artist + ' - Topic', 'artist': artist, 'track': 'Circling Rooms',
             'view_count': 1200, 'duration': 180}]


def _context(filename='Circling Rooms.mid', *, owner=True):
    response = SimpleNamespace(id=800, edit=AsyncMock())
    attachment = SimpleNamespace(filename=filename, size=len(_midi()), read=AsyncMock(return_value=_midi()))
    message = SimpleNamespace(id=77, author=SimpleNamespace(id=42),
        channel=SimpleNamespace(send=AsyncMock(return_value=response)), attachments=[attachment])
    tasks = []

    def schedule(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    cog = SimpleNamespace(bot=SimpleNamespace(is_owner=AsyncMock(return_value=owner)),
                          _schedule_tts_background=schedule)
    return cog, message, response, attachment, tasks


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'deploy/ttv-cd'))
    metadata = importlib.import_module('cd_metadata')
    importer = importlib.import_module('catalog_import')
    from osaka_cd import title_metadata

    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    store = RuntimeStore(tmp_path / 'runtime', legacy)
    monkeypatch.setattr(service, 'get_store', lambda: store)
    monkeypatch.setattr(service, '_ACTIVATION_TIMEOUT', 0)
    rows = _candidates()
    discovery_titles, image_requests, provider_queries = [], [], []

    async def discover(directory, **kwargs):
        # Replace only isolated-process transport. The real resolver consumes
        # controlled external metadata, without a JSON attachment from the user.
        requested = json.loads((directory / 'source.json').read_text())
        discovery_titles.append(requested['title'])
        try:
            result = title_metadata.resolve_title_metadata(requested['title'],
                candidates=rows, vocadb_candidates=[])
        except title_metadata.TitleMetadataError as exc:
            raise service.ImportCDError(exc.code, candidates=exc.candidates) from None
        return {'ok': True, 'metadata': result}

    async def prepare(directory, **kwargs):
        await service._prepare_importer(directory)
        return json.loads((directory / 'report.json').read_text())

    def external_search(query, *args, **kwargs):
        provider_queries.append(query)
        return list(rows)

    output = io.BytesIO()
    Image.new('RGB', (64, 48), 'turquoise').save(output, 'PNG')
    png = output.getvalue()

    class Response:
        status = 200
        headers = {'Content-Type': 'image/png'}

        def __init__(self):
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def iter_chunked(self, size):
            for offset in range(0, len(png), 41):
                yield png[offset:offset + 41]

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            image_requests.append(url)
            return Response()

    monkeypatch.setattr(service, '_run_discovery', discover)
    monkeypatch.setattr(service, '_run_importer', prepare)
    monkeypatch.setattr(metadata, '_yt_dlp_search', external_search)
    monkeypatch.setattr(metadata.YouTubePlayerAdapter, 'search', external_search)
    monkeypatch.setattr(importer, '_sample', lambda *_args: pytest.fail('Import must not synthesize'))
    monkeypatch.setattr('aiohttp.ClientSession', Session)
    return SimpleNamespace(store=store, rows=rows, discovery_titles=discovery_titles,
        image_requests=image_requests, provider_queries=provider_queries, png=png)


@pytest.mark.asyncio
@pytest.mark.parametrize('filename,caption', [
    ('Circling Rooms.mid', ''),
    ('Unknown Uploader - recording.mid', 'Circling Rooms'),
])
async def test_name_alone_imports_real_cover_and_reuses_verified_lookup(pipeline, filename, caption):
    cog, message, response, attachment, tasks = _context(filename)
    assert await ui.handle_import_cd(cog, message, title=caption)
    await asyncio.wait_for(asyncio.gather(*tasks), 10)
    assert pipeline.discovery_titles == ['Circling Rooms']
    assert pipeline.image_requests and pipeline.provider_queries
    root = Path(pipeline.store.snapshot()['root'])
    scores = json.loads((root / _contract().SCORES).read_text())
    assert len(scores) == 1
    score = scores[0]
    assert score['name'] == 'Circling Rooms'
    assert score['artist'] == 'Circuit Composer'
    assert score['source_sha256'] == hashlib.sha256(_midi()).hexdigest()
    videos = json.loads((root / _contract().VIDEOS).read_text())['songs']
    video = videos[score['id']]
    cover = (root / f'cogs/tts/cd/thumbnails/{video["video_id"]}.jpg').read_bytes()
    assert cover != pipeline.png and cover.startswith(b'\xff\xd8\xff')
    assert hashlib.sha256(cover).hexdigest() == video['thumbnail_sha256']
    assert video['selection_audit']['accepted_candidates'] == 1
    assert _contract().validate_cd_tree(root, [score['id']])['artifacts'][score['id']]['thumbnail'] == 'ready'
    assert not hasattr(cog.bot, '_osaka_cd_import_session')
    attachment.read.assert_awaited_once()
    before = pipeline.store.snapshot()['generation']
    searches = len(pipeline.provider_queries)
    images = len(pipeline.image_requests)

    # A second real command follows the persisted lookup marker, with neither
    # a new provider search nor a new image conversion/publication.
    tasks.clear()
    message.id += 1
    assert await ui.handle_import_cd(cog, message, title=caption)
    await asyncio.wait_for(asyncio.gather(*tasks), 10)
    assert pipeline.discovery_titles == ['Circling Rooms']
    assert len(pipeline.provider_queries) == searches
    assert len(pipeline.image_requests) == images
    assert pipeline.store.snapshot()['generation'] == before
    assert message.channel.send.await_count == 2
    assert not hasattr(cog.bot, '_osaka_cd_import_session')


@pytest.mark.asyncio
async def test_ambiguous_title_waits_for_a_real_choice_before_importing_its_cover(pipeline):
    pipeline.rows.extend(_candidates('Other Composer', '12345678901'))
    pipeline.rows[1]['view_count'] = 999999
    cog, message, _response, _attachment, tasks = _context()
    before = pipeline.store.snapshot()['generation']
    assert await ui.handle_import_cd(cog, message)
    for _ in range(200):
        session = getattr(cog.bot, '_osaka_cd_import_session', None)
        if session is not None and session.waiting:
            break
        await asyncio.sleep(.005)
    assert session is not None and session.waiting
    assert pipeline.discovery_titles == ['Circling Rooms']
    assert not pipeline.image_requests and not pipeline.provider_queries
    assert pipeline.store.snapshot()['generation'] == before
    assert isinstance(session.view, ui._ChoicesView)
    assert len(session.view.select.options) == 2
    assert {row['artist'] for row in session.view.candidates} == {'Circuit Composer', 'Other Composer'}
    interaction = SimpleNamespace(user=message.author, response=SimpleNamespace(
        defer=AsyncMock(), send_message=AsyncMock()))
    # A view count does not choose a different composer on the owner's behalf.
    # The server-side selection is then fed through the real importer/converter.
    selected = next(index for index, row in enumerate(session.view.candidates)
                    if row['artist'] == 'Circuit Composer')
    session.view.select._values = [str(selected)]
    await session.view.choose(interaction)
    await asyncio.wait_for(asyncio.gather(*tasks), 10)
    current = Path(pipeline.store.snapshot()['root'])
    score = json.loads((current / _contract().SCORES).read_text())[0]
    video = json.loads((current / _contract().VIDEOS).read_text())['songs'][score['id']]
    assert score['artist'] == 'Circuit Composer'
    assert video['video_id'] == 'abcdefghijk'
    assert pipeline.image_requests and pipeline.provider_queries
    assert _contract().validate_cd_tree(current, [score['id']])['artifacts'][score['id']]['thumbnail'] == 'ready'
    interaction.response.defer.assert_awaited_once()
    assert not hasattr(cog.bot, '_osaka_cd_import_session')


@pytest.mark.asyncio
async def test_publication_during_title_lookup_keeps_the_newer_user_cd(pipeline, tmp_path, monkeypatch):
    discovered = service._run_discovery
    newer_source = hashlib.sha256(b'newer user MIDI').hexdigest()
    score = {'id': 'circling-rooms', 'name': 'Circling Rooms', 'artist': 'Other Composer',
        'excerpt': 'User version', 'source_url': '', 'source_sha256': newer_source,
        'revision': 1, 'duration_ms': 1000,
        'notes': [{'midi_note': 68, 'start_ms': 0, 'duration_ms': 1000, 'velocity': 100}]}
    package = tmp_path / 'newer-user-cd.zip'
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr(_contract().SCORES, json.dumps([score]))

    async def concurrent_lookup(directory, **kwargs):
        receipt = await asyncio.to_thread(pipeline.store.receive, package)
        applied = await asyncio.to_thread(pipeline.store.process, receipt['job_id'])
        assert applied['applied']
        return await discovered(directory, **kwargs)

    monkeypatch.setattr(service, '_run_discovery', concurrent_lookup)
    cog, message, response, _attachment, tasks = _context()
    assert await ui.handle_import_cd(cog, message)
    await asyncio.wait_for(asyncio.gather(*tasks), 10)
    current = Path(pipeline.store.snapshot()['root'])
    songs = json.loads((current / _contract().SCORES).read_text())
    assert len(songs) == 1
    assert songs[0]['source_sha256'] == newer_source
    assert songs[0]['artist'] == 'Other Composer'
    assert songs[0]['notes'][0]['midi_note'] == 68
    final = json.dumps(response.edit.await_args.kwargs['view'].to_components(), ensure_ascii=False)
    assert 'mudou durante a importação' in final
    assert not hasattr(cog.bot, '_osaka_cd_import_session')


def test_presentation_suffix_is_not_a_discovered_artist(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'deploy/ttv-cd'))
    from osaka_cd.title_metadata import resolve_title_metadata, TitleMetadataError
    row = {'video_id': 'abcdefghijk', 'title': 'Circling Rooms - Original Song',
           'channel': 'Uploader', 'view_count': 1000, 'duration': 180}
    with pytest.raises(TitleMetadataError) as caught:
        resolve_title_metadata('Circling Rooms', candidates=[row], vocadb_candidates=[])
    assert caught.value.code == 'metadata_unavailable'
