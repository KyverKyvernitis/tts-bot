"""An owner's cover link is an exact source, never a popularity hint."""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/ttv-cd'))
import catalog_import
import cd_metadata
from test_cd_import_cover_regression import installed, _external_cover


def _recording(video='abcdefghijk', **fields):
    return {'id': video, 'title': 'Unrelated Japanese video title',
            'channel': 'Chosen public channel', 'duration': 180,
            'view_count': 10, **fields}


def test_direct_candidates_never_replace_owner_source_with_more_popular_match():
    rows = [_recording('12345678901', title='Composer - The Vampire', view_count=99999999),
            _recording(view_count=None)]
    result = cd_metadata.video_metadata('abcdefghijk', candidates=rows)
    assert result['selected']['video_id'] == 'abcdefghijk'
    assert result['selected']['view_count'] is None
    assert result['audit']['queries'] == []
    assert result['audit']['scope'] == 'owner-selected-video'
    assert not result['audit']['view_count_observed']


@pytest.mark.parametrize('value', ['https://youtu.be/abcdefghijk', 'short', None, [], '../invalid'])
def test_direct_provider_rejects_non_id_input_before_network(value, monkeypatch):
    monkeypatch.setattr(cd_metadata, '_yt_dlp_video', lambda *_: pytest.fail('network on invalid ID'))
    with pytest.raises(ValueError, match='invalid_selected_video_id'):
        cd_metadata.video_metadata(value)


def test_yt_dlp_direct_fetch_never_searches_or_downloads_audio(monkeypatch):
    calls = []
    class Extractor:
        def __init__(self, options):
            assert options['skip_download'] and options['noplaylist']
            assert not options['extract_flat']
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def extract_info(self, url, **kwargs):
            calls.append(url)
            assert kwargs == {'download': False}
            return _recording()
    monkeypatch.setitem(sys.modules, 'yt_dlp', SimpleNamespace(YoutubeDL=Extractor))
    result = cd_metadata._yt_dlp_video('abcdefghijk')
    assert result['video_id'] == 'abcdefghijk'
    assert calls == ['https://www.youtube.com/watch?v=abcdefghijk']


@pytest.mark.asyncio
async def test_api_direct_fetch_cannot_use_search_or_different_video():
    class Provider:
        async def youtube_search(self, *args, **kwargs):
            pytest.fail('owner link must never search')
        async def youtube_video_metadata(self, video_id):
            assert video_id == 'abcdefghijk'
            return SimpleNamespace(webpage_url='https://www.youtube.com/watch?v=abcdefghijk',
                                   title='Public title', artist='Chosen channel', thumbnail='', duration=180)
    adapter = cd_metadata.YouTubePlayerAdapter('test-value', limit=1)
    adapter.provider = Provider()
    assert (await adapter.video_async('abcdefghijk'))['video_id'] == 'abcdefghijk'


def test_exact_cover_and_edited_display_use_real_converter_keep_all_music(installed, tmp_path, monkeypatch):
    audio, arrangement = installed.audio.read_bytes(), installed.arrangement.read_bytes()
    output = io.BytesIO()
    Image.new('RGB', (91, 70), 'orange').save(output, 'PNG')
    requests = _external_cover(monkeypatch, output.getvalue())
    candidates = tmp_path / 'videos.json'
    candidates.write_text(json.dumps([_recording(), _recording('12345678901', view_count=99999999)]))
    monkeypatch.setattr(catalog_import, '_search', lambda *_args, **_kwargs: pytest.fail('exact cover used broad search'))
    package = tmp_path / 'edited.zip'
    result = asyncio.run(catalog_import.repair_metadata('the-vampire', root=installed.root,
        metadata={'title': 'Custom Display', 'artist': 'Edited Composer', 'cover_video_id': 'abcdefghijk'},
        candidates=candidates, cache_dir=tmp_path / 'covers', export_zip=package, edit_display=True))
    assert result['ok']
    assert requests and all('/abcdefghijk/' in url for url in requests)
    score = json.loads((installed.root / 'cd_song_scores.json').read_text())[0]
    assert score['name'] == 'Custom Display' and score['artist'] == 'Edited Composer'
    for key in ('notes', 'revision', 'source_sha256', 'score_sha256', 'arrangement_sha256'):
        assert score[key] == installed.score[key]
    video = json.loads((installed.root / 'cd_song_videos.json').read_text())['songs']['the-vampire']
    assert video['cover_source'] == {'selection': 'owner', 'video_id': 'abcdefghijk'}
    assert video['owner_edit'] == {'display': True}
    assert video['selection_audit']['scope'] == 'owner-selected-video'
    assert installed.audio.read_bytes() == audio
    assert installed.arrangement.read_bytes() == arrangement

    # A later MIDI import uses the owner-selected recording and display even
    # when its filename and a much more popular candidate disagree.
    result = asyncio.run(catalog_import.import_midi(installed.source, root=installed.root,
        candidates=candidates, cache_dir=tmp_path / 'covers', render_sample=False,
        export_zip=tmp_path / 'reimport.zip'))
    assert result['video_id'] == 'abcdefghijk'
    assert result['name'] == 'Custom Display'
    video = json.loads((installed.root / 'cd_song_videos.json').read_text())['songs']['the-vampire']
    assert video['cover_source']['selection'] == 'owner'
    assert video['owner_edit']['display'] is True
    assert installed.audio.read_bytes() == audio


def test_automatic_metadata_repair_retains_owner_pin_and_display(installed, tmp_path, monkeypatch):
    candidate_file = tmp_path / 'videos.json'
    candidate_file.write_text(json.dumps([_recording()]))
    async def cover(*_args):
        out = io.BytesIO()
        Image.new('RGB', (32, 32), 'blue').save(out, 'JPEG')
        data = out.getvalue()
        return data, catalog_import._verify_cover(data)
    monkeypatch.setattr(catalog_import, '_cover_for', cover)
    first = asyncio.run(catalog_import.repair_metadata('the-vampire', root=installed.root,
        metadata={'title': 'Custom Name', 'artist': 'My Composer', 'cover_video_id': 'abcdefghijk'},
        candidates=candidate_file, cache_dir=tmp_path / 'covers', edit_display=True))
    assert first['ok']
    second = asyncio.run(catalog_import.repair_metadata('the-vampire', root=installed.root,
        metadata={'title': 'Provider Name', 'artist': 'Provider Artist'},
        candidates=candidate_file, cache_dir=tmp_path / 'covers'))
    assert second['ok']
    score = json.loads((installed.root / 'cd_song_scores.json').read_text())[0]
    assert (score['name'], score['artist']) == ('Custom Name', 'My Composer')
    video = json.loads((installed.root / 'cd_song_videos.json').read_text())['songs']['the-vampire']
    assert video['owner_edit'] == {'display': True}
    assert video['cover_source']['video_id'] == 'abcdefghijk'


def test_failed_exact_reference_never_substitutes_another_video(installed, tmp_path):
    candidates = tmp_path / 'videos.json'
    candidates.write_text(json.dumps([_recording('12345678901', view_count=99999999)]))
    before = {path.relative_to(installed.root): path.read_bytes() for path in installed.root.rglob('*') if path.is_file()}
    package = tmp_path / 'failed.zip'
    result = asyncio.run(catalog_import.repair_metadata('the-vampire', root=installed.root,
        metadata={'artist': 'Composer', 'cover_video_id': 'abcdefghijk'},
        candidates=candidates, export_zip=package))
    assert not result['ok'] and not package.exists()
    assert before == {path.relative_to(installed.root): path.read_bytes() for path in installed.root.rglob('*') if path.is_file()}
