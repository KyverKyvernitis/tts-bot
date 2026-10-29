"""Known recordings survive search outages; artwork repairs never replace music."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/ttv-cd'))
import catalog_import
import cd_metadata
from osaka_cd.store import RuntimeStore, _contract


def _midi():
    events = bytes([0, 144, 72, 90, 131, 96, 128, 72, 0, 0, 255, 47, 0])
    return (b'MThd\0\0\0\6\0\0\0\1\1\xe0MTrk'
            + len(events).to_bytes(4, 'big') + events)


def _recording():
    return {'id': 'abcdefghijk', 'video_id': 'abcdefghijk',
            'title': 'Composer - 吸血鬼 feat. Hatsune Miku',
            'channel': 'Composer', 'view_count': 500, 'duration': 180}


def test_yt_dlp_resolves_original_seed_even_when_search_raises(monkeypatch):
    requests = []

    class Extractor:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, **kwargs):
            requests.append(url)
            if url.startswith('ytsearch'):
                raise RuntimeError('search unavailable')
            return _recording()

    monkeypatch.setitem(sys.modules, 'yt_dlp', SimpleNamespace(YoutubeDL=Extractor))
    rows = cd_metadata._yt_dlp_search('The Vampire Composer', limit=5,
                                      seed_video_ids=('abcdefghijk', 'invalid'))
    selected = cd_metadata.search_metadata('The Vampire', 'Composer', aliases=['吸血鬼'], candidates=rows)
    assert selected['selected']['video_id'] == 'abcdefghijk'
    assert requests == ['ytsearch5:The Vampire Composer', 'https://www.youtube.com/watch?v=abcdefghijk']
    # A supplied seed remains evidence to verify, never an identity bypass.
    with pytest.raises(cd_metadata.MetadataNoMatchError):
        cd_metadata.search_metadata('Different Song', 'Composer', candidates=rows)


@pytest.mark.asyncio
async def test_api_resolves_original_seed_even_when_search_raises():
    requests = []

    class Provider:
        async def youtube_search(self, query, **kwargs):
            requests.append('search')
            raise RuntimeError('search quota exhausted')

        async def youtube_video_metadata(self, video_id):
            requests.append(video_id)
            return SimpleNamespace(webpage_url='https://www.youtube.com/watch?v=' + video_id,
                                   title=_recording()['title'], artist='Composer',
                                   thumbnail='', duration=180)

    adapter = cd_metadata.YouTubePlayerAdapter('unused-test-key', limit=5)
    adapter.provider = Provider()
    adapter.statistics['abcdefghijk'] = 500
    rows = await adapter.search_async('The Vampire', seed_video_ids=['abcdefghijk'])
    assert requests == ['search', 'abcdefghijk']
    assert cd_metadata.search_metadata('The Vampire', 'Composer', aliases=['吸血鬼'], candidates=rows)['selected']['video_id'] == 'abcdefghijk'
    rows[0]['view_count'] = None
    with pytest.raises(cd_metadata.MetadataNoMatchError):
        cd_metadata.search_metadata('The Vampire', 'Composer', aliases=['吸血鬼'], candidates=rows)


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = tmp_path / 'catalog'
    root.mkdir()
    (root / 'cd_song_scores.json').write_text('[]')
    (root / 'cd_song_videos.json').write_text('{"version":1,"songs":{}}')
    source = tmp_path / 'The Vampire.mid'
    source.write_bytes(_midi())
    audio = tmp_path / 'sample.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=0.5', '-c:a', 'libmp3lame', '-y', str(audio)], check=True)

    def render(melody, target):
        target.write_bytes(audio.read_bytes())
        melody['_render_report'] = {'configuration_fingerprint': 'a' * 64,
                                   'mix_sha256': hashlib.sha256(audio.read_bytes()).hexdigest(),
                                   'worker_version': '1.11.41'}
        return 'ready'

    monkeypatch.setattr(catalog_import, '_sample', render)
    monkeypatch.setattr(catalog_import, '_search', lambda *args, **kwargs: (_ for _ in ()).throw(cd_metadata.MetadataSearchError('offline')))
    package = tmp_path / 'initial.zip'
    imported = asyncio.run(catalog_import.import_midi(source, root=root,
        cache_dir=tmp_path / 'covers', export_zip=package))
    assert {row['artifact'] for row in imported['pending_artifacts']} == {'metadata', 'thumbnail'}
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    store = RuntimeStore(tmp_path / 'runtime', legacy)
    receipt = store.receive(package)
    assert store.process(receipt['job_id'])['applied']
    # Unusual valid whitespace exercises the no-arrangement-reencoding promise.
    arrangement = root / 'arrangements/the-vampire.json'
    arrangement.write_text(json.dumps(json.loads(arrangement.read_text()), separators=(',', ':')))
    original_score = json.loads((root / 'cd_song_scores.json').read_text())[0]
    return SimpleNamespace(root=root, source=source, score=original_score, store=store,
        arrangement=arrangement, audio=root / 'previews/the-vampire.mp3')


def _external_cover(monkeypatch, png):
    requests = []

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
            yield png

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            requests.append(url)
            return Response()

    monkeypatch.setattr('aiohttp.ClientSession', Session)
    return requests


def test_existing_incomplete_cd_repairs_through_real_converter_without_source_midi(installed, tmp_path, monkeypatch):
    before_arrangement, before_audio = installed.arrangement.read_bytes(), installed.audio.read_bytes()
    installed.source.unlink()
    output = io.BytesIO()
    Image.new('RGB', (91, 70), 'turquoise').save(output, 'PNG')
    png = output.getvalue()
    requests = _external_cover(monkeypatch, png)
    monkeypatch.setattr(catalog_import, '_search', lambda *args, **kwargs: cd_metadata.search_metadata(
        'The Vampire', 'Composer', aliases=['吸血鬼'], candidates=[_recording()]))
    # Artwork repair must never call the audio renderer.
    monkeypatch.setattr(catalog_import, '_sample', lambda *_args: pytest.fail('repair synthesized audio'))
    package = tmp_path / 'repair.zip'
    repaired = asyncio.run(catalog_import.repair_metadata('the-vampire', root=installed.root,
        metadata={'title': 'Unwanted translation', 'artist': 'Composer', 'aliases': ['吸血鬼']},
        cache_dir=tmp_path / 'covers', export_zip=package))
    assert repaired['ok'] and not repaired['pending_artifacts']
    assert requests and all('/abcdefghijk/' in url for url in requests)
    score = json.loads((installed.root / 'cd_song_scores.json').read_text())[0]
    assert score['name'] == 'The Vampire'
    assert score['artist'] == 'Composer'
    assert {key: value for key, value in score.items() if key not in {'artist', 'source_url'}} == {
        key: value for key, value in installed.score.items() if key not in {'artist', 'source_url'}}
    assert installed.arrangement.read_bytes() == before_arrangement
    assert installed.audio.read_bytes() == before_audio
    with zipfile.ZipFile(package) as archive:
        assert set(archive.namelist()) == {'cogs/tts/cd_song_scores.json',
            'cogs/tts/cd_song_videos.json', 'cogs/tts/cd/thumbnails/abcdefghijk.jpg'}
        image = archive.read('cogs/tts/cd/thumbnails/abcdefghijk.jpg')
        assert image != png and image.startswith(b'\xff\xd8\xff')
        Image.open(io.BytesIO(image)).load()
    before = installed.store.snapshot()
    receipt = installed.store.receive(package, expected_versions={'the-vampire': before['versions']['the-vampire']})
    assert installed.store.process(receipt['job_id'])['applied']
    after = Path(installed.store.snapshot()['root'])
    previous = Path(before['root'])
    assert (after / 'cogs/tts/cd/arrangements/the-vampire.json').read_bytes() == (previous / 'cogs/tts/cd/arrangements/the-vampire.json').read_bytes()
    assert (after / 'cogs/tts/cd/previews/the-vampire.mp3').read_bytes() == before_audio
    assert _contract().validate_cd_tree(after, ['the-vampire'], audio_probe=True)['artifacts']['the-vampire']['thumbnail'] == 'ready'


@pytest.mark.parametrize('failure', ['metadata', 'thumbnail'])
def test_failed_artwork_repair_exports_nothing_and_preserves_installed_catalog(installed, tmp_path, monkeypatch, failure):
    before = {path.relative_to(installed.root): path.read_bytes() for path in installed.root.rglob('*') if path.is_file()}
    if failure == 'thumbnail':
        monkeypatch.setattr(catalog_import, '_search', lambda *args, **kwargs: cd_metadata.search_metadata(
            'The Vampire', 'Composer', aliases=['吸血鬼'], candidates=[_recording()]))

        async def failed(*args):
            raise RuntimeError('private provider URL must not escape')

        monkeypatch.setattr(catalog_import, '_cover_for', failed)
    package = tmp_path / 'not-exported.zip'
    result = asyncio.run(catalog_import.repair_metadata('the-vampire', root=installed.root,
        metadata={'artist': 'Composer', 'aliases': ['吸血鬼']},
        cache_dir=tmp_path / 'covers', export_zip=package))
    assert not result['ok']
    assert failure in {row['artifact'] for row in result['pending_artifacts']}
    assert not package.exists()
    assert 'private' not in json.dumps(result)
    assert before == {path.relative_to(installed.root): path.read_bytes() for path in installed.root.rglob('*') if path.is_file()}
