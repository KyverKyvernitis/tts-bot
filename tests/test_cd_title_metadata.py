"""Title discovery verifies artist evidence without changing recording filters."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys
from unittest.mock import Mock, patch

import pytest

TOOLS = Path(__file__).resolve().parents[1] / 'deploy/ttv-cd'
sys.path.insert(0, str(TOOLS))
metadata = importlib.import_module('cd_metadata')
from osaka_cd import title_metadata as discovery


def candidate(title='Composer - Example Song', artist=None, channel='Composer', **fields):
    result = {'id': 'abcdefghijk', 'title': title, 'channel': channel,
              'view_count': 1000, 'duration': 180, **fields}
    if artist is not None:
        result['artist'] = artist
    return result


def catalog(name='Example Song', artist='Composer', *, identity=1, producer=2, **fields):
    return {'id': identity, 'name': name, 'songType': 'Original',
        'names': [{'value': name}],
        'artists': [{'roles': 'Composer, Arranger',
                     'artist': {'id': producer, 'name': artist, 'artistType': 'Producer'}}],
        'pvs': [{'service': 'Youtube', 'pvType': 'Original', 'pvId': 'abcdefghijk'}], **fields}


def resolve(title='Example Song', **kwargs):
    with patch.object(discovery, '_vocadb_search', side_effect=AssertionError('offline lookup used network')):
        return discovery.resolve_title_metadata(title, **kwargs)


def test_title_credit_resolves_without_json_or_network():
    result = resolve(candidates=[candidate()])
    assert result['title'] == 'Example Song'
    assert result['artist'] == 'Composer'
    assert result['seed_video_ids'] == ['abcdefghijk']
    assert result['selection_audit']['evidence'] == 'recording_title_credit'


def test_reversed_title_credit_removes_official_presentation_from_artist():
    result = resolve(candidates=[candidate('Example Song - Composer (Official Audio)')])
    assert result['artist'] == 'Composer'


def test_generic_uploader_and_verified_but_non_official_channel_are_not_artist_evidence():
    for row in [candidate('Example Song', channel='Music Reuploads'),
                candidate('Example Song', channel='Music Reuploads', channel_is_verified=True)]:
        with pytest.raises(discovery.TitleMetadataError) as error:
            resolve(candidates=[row])
        assert error.value.code == 'metadata_unavailable'


def test_topic_channel_and_structured_artist_track_supply_explicit_evidence():
    topic = resolve(candidates=[candidate('Example Song', channel='Composer - Topic')])
    assert topic['artist'] == 'Composer'
    structured = resolve(candidates=[candidate('Example Song', artist='Composer',
                                              track='Example Song', channel='Release Label')])
    assert structured['artist'] == 'Composer'


@pytest.mark.parametrize('label', ['piano cover', 'remix', 'slowed', 'reaction', 'analysis', 'live concert'])
def test_more_popular_unrequested_versions_remain_rejected(label):
    result = resolve(candidates=[candidate(), candidate('Composer - Example Song ' + label,
        id='12345678901', view_count=999999999)])
    assert result['seed_video_ids'] == ['abcdefghijk']


def test_same_title_with_two_identified_artists_is_ambiguous_even_when_one_is_popular():
    with pytest.raises(discovery.TitleMetadataError) as error:
        resolve('MTC', candidates=[candidate('Producer A - MTC'),
            candidate('Producer B - MTC', id='12345678901', view_count=99999999)])
    assert error.value.code == 'metadata_ambiguous'


def test_invalid_view_count_and_video_identity_do_not_discover_an_artist():
    for fields in [{'view_count': None}, {'view_count': True}, {'id': '../not-video'}]:
        with pytest.raises(discovery.TitleMetadataError) as error:
            resolve(candidates=[candidate(**fields)])
        assert error.value.code == 'metadata_unavailable'


@pytest.mark.parametrize('suffix', ['Official Music Video', 'Original Song', 'FULL', 'Full Song',
    'Official Release', 'Audio Only', 'Original', 'Theme', 'Topic', 'feat. Singer',
    'Vocals Singer', 'Hatsune Miku', '重音テトSV'])
def test_presentation_and_performer_suffixes_are_not_composers(suffix):
    with pytest.raises(discovery.TitleMetadataError) as error:
        resolve(candidates=[candidate('Example Song - ' + suffix, channel='Uploader')])
    assert error.value.code == 'metadata_unavailable'


def test_japanese_slash_vocal_credit_does_not_become_the_producer():
    with pytest.raises(discovery.TitleMetadataError) as error:
        resolve('テトリス', candidates=[candidate('テトリス / 重音テトSV', channel='Uploader')])
    assert error.value.code == 'metadata_unavailable'


def test_vocadb_original_song_aliases_resolve_japanese_title_and_producer_only():
    row = catalog('ループザルーム', 'ルシノ')
    row['names'].append({'value': 'Looping the Rooms'})
    row['artists'][0]['artist']['names'] = [{'value': 'rusino'}]
    row['artists'].append({'roles': 'Vocalist', 'artist': {
        'id': 100, 'name': 'Hatsune Miku', 'artistType': 'Vocaloid'}})
    result = resolve('Looping the Rooms', vocadb_candidates=[row])
    assert result['title'] == 'Looping the Rooms'
    assert result['artist'] == 'ルシノ'
    assert 'rusino' in result['artist_aliases']
    assert result['aliases'] == ['ループザルーム']
    assert 'Hatsune Miku' not in str(result)
    assert result['selection_audit']['provider'] == 'vocadb-public-song-identities'


def test_unique_romanized_suffix_is_catalog_evidence_without_hardcoded_song_map():
    row = catalog('テトリス', '柊マグネタイト')
    row['names'] += [{'value': 'Tetorisu'}, {'value': 'Tetris'}]
    result = resolve('Tetoris', vocadb_candidates=[row])
    assert result['title'] == 'Tetoris'
    assert result['artist'] == '柊マグネタイト'
    assert result['selection_audit']['title_match'] == 'romanized_prefix'


def test_catalog_same_title_different_producers_is_ambiguous_and_does_not_call_youtube():
    backend = Mock(side_effect=AssertionError('popularity cannot resolve song identity'))
    with pytest.raises(discovery.TitleMetadataError) as error:
        resolve(vocadb_candidates=[catalog(), catalog(artist='Other', identity=3, producer=4)], search_backend=backend)
    assert error.value.code == 'metadata_ambiguous'
    backend.assert_not_called()


def test_catalog_cover_or_missing_original_recording_link_is_not_used():
    for fields in [{'songType': 'Cover'}, {'pvs': []}, {'artists': []},
                   {'names': None, 'artists': None},
                   {'pvs': [{'service': 'Youtube', 'pvType': 'Original', 'pvId': '../evil'}]}]:
        with pytest.raises(discovery.TitleMetadataError) as error:
            resolve(vocadb_candidates=[catalog(**fields)])
        assert error.value.code == 'metadata_unavailable'


def test_one_bounded_public_query_reuses_existing_provider_when_catalog_fails():
    with patch.object(discovery, '_vocadb_search', side_effect=RuntimeError('private-key-url')), \
            patch.object(metadata, '_yt_dlp_search', return_value=[candidate()]) as backend, \
            patch.dict('os.environ', {'YOUTUBE_API_KEY': '', 'GOOGLE_YOUTUBE_API_KEY': ''}):
        result = discovery.resolve_title_metadata('Example Song')
    assert result['artist'] == 'Composer'
    assert backend.call_count == 1
    assert backend.call_args.args == ('Example Song',)
    assert backend.call_args.kwargs['limit'] == 25
    assert 'private-key-url' not in str(result)


def test_provider_outage_is_compact_without_private_error_text():
    with pytest.raises(discovery.TitleMetadataError) as error:
        resolve(search_backend=Mock(side_effect=RuntimeError('https://private.invalid/?key=secret')))
    assert error.value.code == 'metadata_unavailable'
    assert str(error.value) == 'metadata_unavailable'


@pytest.mark.parametrize('title', ['', 'x' * 101, 'Song\nSecret', None])
def test_invalid_title_is_rejected_before_provider(title):
    with pytest.raises(discovery.TitleMetadataError) as error:
        resolve(title, candidates=[])
    assert error.value.code == 'metadata_invalid'


def test_public_api_response_size_is_bounded_and_redirects_are_refused():
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b' ' * (discovery._MAX_RESPONSE + 1)
    opener = Mock()
    opener.open.return_value = response
    with patch.object(discovery, 'build_opener', return_value=opener):
        with pytest.raises(discovery.TitleMetadataError):
            discovery._vocadb_search('Example Song')
    response.read.assert_called_once_with(discovery._MAX_RESPONSE + 1)
    assert opener.open.call_args.kwargs['timeout'] == 8
    assert discovery._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid') is None
