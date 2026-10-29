"""Real recording choices stay separate from automatic identity confidence."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/ttv-cd'))
metadata = importlib.import_module('cd_metadata')
worker_config = importlib.import_module('worker_config')
from osaka_cd import title_metadata as discovery


def recording(number=0, title='Composer - Example Song', **fields):
    return {'id': f'choice{number:05d}', 'title': title, 'channel': 'Composer',
            'view_count': 1000 + number, 'duration': 180, **fields}


def song(number=0, *, name='Example Song', artist='Composer'):
    return {'id': number, 'name': name, 'songType': 'Original',
            'names': [{'value': name}],
            'artists': [{'roles': 'Composer', 'artist': {
                'id': number, 'artistType': 'Producer', 'name': artist}}],
            'pvs': [{'service': 'Youtube', 'pvType': 'Original', 'pvId': f'choice{number:05d}'}]}


def choices(title='Example Song', **kwargs):
    with patch.object(discovery, '_vocadb_search', side_effect=AssertionError('offline network')), \
            patch.object(discovery, '_configured_api_key', side_effect=AssertionError('offline credentials')):
        return discovery.discover_title_candidates(title, **kwargs)


def test_choices_show_five_distinct_real_recordings_and_keep_exact_matches_first():
    rows = [recording(index, f'Producer {index} - Other Track {index}') for index in range(8)]
    rows.insert(0, recording(99, 'Exact Composer - Example Song', view_count=0))
    result = choices(candidates=rows)
    assert len(result) == 5
    assert result[0]['title'] == 'Example Song'
    assert result[0]['artist'] == 'Exact Composer'
    assert len({choice['seed_video_ids'][0] for choice in result}) == 5
    assert all(set(choice) == {'title', 'artist', 'aliases', 'artist_aliases',
                               'seed_video_ids', 'search_query'} for choice in result)


def test_translated_recording_is_an_owner_choice_and_never_an_automatic_match():
    row = recording(0, 'DECO*27 - ヴァンパイア feat. 初音ミク',
                    id='e1xCOsgWG0M', channel='DECO*27')
    result = choices('The Vampire', candidates=[row])
    assert result == [{'title': 'ヴァンパイア', 'artist': 'DECO*27',
                       'aliases': ['The Vampire'], 'artist_aliases': [],
                       'seed_video_ids': ['e1xCOsgWG0M'],
                       'search_query': 'ヴァンパイア DECO*27'}]
    with pytest.raises(discovery.TitleMetadataError) as error:
        discovery.resolve_title_metadata('The Vampire', candidates=[row])
    assert error.value.code == 'metadata_unavailable'
    assert error.value.candidates == result


def test_ambiguous_catalog_reuses_collected_choices_without_second_provider_query():
    backend = Mock(side_effect=AssertionError('must reuse initial catalog'))
    rows = [song(index, artist=f'Composer {index}') for index in range(7)]
    with pytest.raises(discovery.TitleMetadataError) as error:
        discovery.resolve_title_metadata('Example Song', vocadb_candidates=rows, search_backend=backend)
    assert error.value.code == 'metadata_ambiguous'
    assert len(error.value.candidates) == 5
    assert {row['artist'] for row in error.value.candidates} == {f'Composer {index}' for index in range(5)}
    backend.assert_not_called()


def test_same_video_from_both_providers_and_repeated_rows_do_not_fill_options():
    rows = [recording(), recording(), recording(1, 'Other Composer - Example Song')]
    result = choices(candidates=rows, vocadb_candidates=[song()])
    assert len(result) == 2
    assert {row['seed_video_ids'][0] for row in result} == {'choice00000', 'choice00001'}


def test_same_song_artist_merges_recordings_into_one_distinguishable_choice():
    rows = [recording(0), recording(1), recording(2, 'Other Composer - Example Song')]
    result = choices(candidates=rows)
    assert len(result) == 2
    composer = next(row for row in result if row['artist'] == 'Composer')
    assert set(composer['seed_video_ids']) == {'choice00000', 'choice00001'}


@pytest.mark.parametrize('suffix', ['piano cover', 'reaction', 'remix', 'live concert', 'slowed'])
def test_translation_suggestions_do_not_authorize_unrequested_versions(suffix):
    rows = [recording(title='DECO*27 - ヴァンパイア ' + suffix, channel='DECO*27')]
    assert choices('The Vampire', candidates=rows) == []


def test_uncredited_uploader_and_voice_credits_are_not_composer_suggestions():
    rows = [recording(0, 'Example Song', channel='Music Reuploads'),
            recording(1, 'Hatsune Miku - Example Song'),
            recording(2, 'The Vampire / 重音テトSV')]
    assert choices(candidates=rows) == []


@pytest.mark.parametrize('suffix', ['Original Song', 'Official Music Video', 'Hatsune Miku',
                                    'feat. Singer', 'Full Song', '重音テトSV'])
def test_presentation_and_vocal_suffix_do_not_become_reversed_song_choices(suffix):
    row = recording(title='Example Song - ' + suffix, channel='Uploader')
    assert choices(candidates=[row]) == []
    with pytest.raises(discovery.TitleMetadataError) as error:
        discovery.resolve_title_metadata('Example Song', candidates=[row])
    assert error.value.candidates == []


def test_flat_search_without_views_can_be_offered_but_not_auto_selected():
    row = recording(view_count=None)
    result = choices(candidates=[row])
    assert result[0]['seed_video_ids'] == ['choice00000']
    with pytest.raises(discovery.TitleMetadataError) as error:
        discovery.resolve_title_metadata('Example Song', candidates=[row])
    assert error.value.candidates == result


def test_explicit_artist_improves_query_without_reading_credentials_in_offline_seam():
    backend = Mock(return_value=[recording()])
    result = choices(artist='Composer', search_backend=backend)
    assert result[0]['artist'] == 'Composer'
    backend.assert_called_once_with('Example Song Composer', limit=25, seed_video_ids=())


def test_live_search_uses_existing_private_search_configuration_without_serializing_key():
    provider = Mock()
    provider.search.return_value = [recording()]
    with patch.object(discovery, '_vocadb_search', return_value=[]), \
            patch.dict('os.environ', {'YOUTUBE_API_KEY': '', 'GOOGLE_YOUTUBE_API_KEY': ''}), \
            patch.object(worker_config, 'load_environment', return_value={'YOUTUBE_API_KEY': 'private-test-value'}) as loader, \
            patch.object(metadata, 'YouTubePlayerAdapter', return_value=provider) as factory:
        result = discovery.discover_title_candidates('Example Song')
    loader.assert_called_once_with(include_search_credentials=True)
    factory.assert_called_once_with('private-test-value', limit=25)
    assert 'private-test-value' not in str(result)


def test_search_hydrates_at_most_five_distinct_credited_recordings():
    def search(query, *, limit, candidate_filter):
        assert limit == 25
        rows = [recording(index, f'Producer {index} - Example Song', view_count=None) for index in range(20)]
        assert sum(candidate_filter(row) for row in rows) == 5
        return rows
    with patch.object(discovery, '_vocadb_search', return_value=[]), \
            patch.object(discovery, '_configured_api_key', return_value=None), \
            patch.object(metadata, '_yt_dlp_search', side_effect=search):
        assert len(discovery.discover_title_candidates('Example Song')) == 5


def test_choice_payload_drops_urls_controls_extra_fields_and_invalid_ids():
    data = [{'title': 'Example Song', 'artist': 'Composer',
             'seed_video_ids': ['choice00000', '../bad'],
             'aliases': ['Alias', 'https://private.invalid/?key=secret'],
             'artist_aliases': ['Composer Alias'], 'stream_url': 'https://private.invalid/token',
             'search_query': 'https://private.invalid/?key=secret'},
            {'title': 'https://private.invalid/token', 'artist': 'Composer',
             'seed_video_ids': ['choice00001']},
            {'title': 'Song', 'artist': 'Bad\nSecret', 'seed_video_ids': ['choice00002']}]
    error = discovery.TitleMetadataError('metadata_ambiguous', candidates=data)
    assert error.candidates == [{'title': 'Example Song', 'artist': 'Composer',
                                 'seed_video_ids': ['choice00000'], 'aliases': ['Alias'],
                                 'artist_aliases': ['Composer Alias'],
                                 'search_query': 'Example Song Composer'}]
    assert 'private.invalid' not in str(error.candidates)


@pytest.mark.parametrize('title', ['', None, 'x' * 101, 'Song\nSecret'])
def test_invalid_query_is_rejected_before_provider(title):
    with pytest.raises(discovery.TitleMetadataError) as error:
        choices(title, candidates=[])
    assert error.value.code == 'metadata_invalid'
