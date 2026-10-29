"""Requested removals apply to legacy and immutable runtime catalogs."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

import pytest

from cogs.tts import cd_previews, melodies, random_cd
from osaka_cd import runtime
from osaka_cd.catalog_policy import RETIRED_CD_IDS, is_retired_cd
from osaka_cd.store import RuntimeStore


def _entry(identity, pitch=72):
    return {'id': identity, 'name': identity, 'artist': 'Composer',
            'excerpt': 'Main phrase', 'source_url': '',
            'source_sha256': hashlib.sha256(identity.encode()).hexdigest(),
            'revision': 1, 'duration_ms': 1000,
            'notes': [{'midi_note': pitch, 'start_ms': 0, 'duration_ms': 1000, 'velocity': 90}]}


@pytest.fixture
def source_catalog(tmp_path, monkeypatch):
    path = tmp_path / 'legacy/cd_song_scores.json'
    path.parent.mkdir()
    entries = [_entry(identity) for identity in sorted(RETIRED_CD_IDS)]
    entries.extend([_entry('new-song', 65), _entry('ghost-rule-new', 67)])
    path.write_text(json.dumps(entries))
    monkeypatch.setattr(melodies, '_SCORE_FILE', path)
    monkeypatch.setattr(melodies, '_MELODIES', (melodies._DEFAULT_MELODY,))
    monkeypatch.setattr(melodies, '_CATALOG_SIGNATURE', (-1,))
    monkeypatch.setattr(melodies, '_FAILED_CATALOG_SIGNATURE', None)
    with runtime.unpin_snapshot(), runtime.pin_snapshot({'initialized': False, 'generation': None, 'root': ''}):
        yield path, entries


def test_removed_ids_are_exact_and_similar_names_remain_available(source_catalog):
    path, entries = source_catalog
    before = path.read_bytes()
    raw = melodies._read_song_melodies(path)
    assert {song.id for song in raw} == {row['id'] for row in entries}
    visible = melodies.list_melodies()
    assert {song.id for song in visible} == {melodies.DEFAULT_MELODY_ID, 'new-song', 'ghost-rule-new'}
    assert melodies.list_melodies() is visible
    assert path.read_bytes() == before
    assert not is_retired_cd('ghost-rule-new')
    assert not is_retired_cd(['ghost-rule'])


@pytest.mark.parametrize('identity', sorted(RETIRED_CD_IDS))
def test_old_saved_ids_select_default_score_and_arrangement_without_errors(source_catalog, identity):
    assert melodies.normalize_melody_id(identity) == melodies.DEFAULT_MELODY_ID
    assert melodies.get_melody(identity).id == melodies.DEFAULT_MELODY_ID
    assert melodies.score_for_melody(identity) == melodies.score_for_melody()
    assert melodies.arrangement_for_melody({'ttv_melody_id': identity}) == melodies.arrangement_for_melody()
    assert melodies.preview_text_for_melody(identity) == melodies.preview_text_for_melody()
    with pytest.raises(ValueError):
        melodies.get_melody('unknown-song')


@pytest.mark.parametrize('identity', sorted(RETIRED_CD_IDS))
def test_retired_previews_do_not_read_cached_artifacts(source_catalog, identity):
    preferences = {'ttv_voice_id': 'kasane-teto', 'ttv_language': 'pt-BR'}
    with patch.object(cd_previews, 'data_root', side_effect=AssertionError('retired preview must not read its old files')):
        assert cd_previews.prepared_sample(identity, preferences) is None


def test_shuffle_drops_removed_history_and_never_draws_removed_catalog_rows(source_catalog):
    all_ids = set(RETIRED_CD_IDS) | {'new-song', 'ghost-rule-new'}
    cycle = random_cd._RandomCDCycle(played=set(RETIRED_CD_IDS), last='ghost-rule')
    cog = SimpleNamespace(_random_cd_cycles=OrderedDict({(1, 2): cycle}))
    preferences = {'ttv_random_cd': True, 'ttv_melody_id': 'rolling-girl'}
    original = deepcopy(preferences)
    with patch.object(random_cd.random, 'choice', side_effect=lambda pool: pool[0]):
        with runtime.pin_snapshot({'initialized': False, 'generation': None, 'root': ''}):
            selected = [random_cd.choose_synthesis_melody(cog, 1, 2, preferences) for _ in range(4)]
    assert set(selected) == {'new-song', 'ghost-rule-new'}
    assert not cog._random_cd_cycles[(1, 2)].played & RETIRED_CD_IDS
    assert preferences == original
    assert all(identity in all_ids for identity in selected)


def test_retired_records_are_still_validated_before_filtering(source_catalog):
    path, entries = source_catalog
    entries[0]['notes'][0]['duration_ms'] = 1001
    path.write_text(json.dumps(entries))
    with pytest.raises(ValueError, match='invalid_cd_score'):
        melodies._read_song_melodies(path)


def test_runtime_publication_and_pinned_old_generation_cannot_restore_removed_cds(tmp_path, monkeypatch):
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    store = RuntimeStore(tmp_path / 'data', legacy)
    monkeypatch.setattr(melodies, '_MELODIES', (melodies._DEFAULT_MELODY,))
    monkeypatch.setattr(melodies, '_CATALOG_SIGNATURE', (-1,))
    monkeypatch.setattr(melodies, '_FAILED_CATALOG_SIGNATURE', None)

    def publish(filename, entries):
        package = tmp_path / filename
        with zipfile.ZipFile(package, 'w') as archive:
            archive.writestr('cogs/tts/cd_song_scores.json', json.dumps(entries))
            archive.writestr('cogs/tts/cd_song_videos.json', '{"version":1,"songs":{}}')
        job = store.receive(package)
        result = store.process(job['job_id'])
        assert result['applied'], result
        return store.snapshot()

    entries = [_entry(identity) for identity in sorted(RETIRED_CD_IDS)] + [_entry('survivor', 65)]
    old = publish('original.zip', entries)
    original_bytes = (Path(old['root']) / 'cogs/tts/cd_song_scores.json').read_bytes()
    updated_retired = {**_entry('ghost-rule', 74), 'revision': 2}
    new = publish('next.zip', [_entry('next-song', 67), updated_retired])
    assert set(store.snapshot()['versions']) == set(RETIRED_CD_IDS) | {'survivor', 'next-song'}
    with runtime.unpin_snapshot():
        for snapshot, expected in [(new, {'survivor', 'next-song'}), (old, {'survivor'})]:
            with runtime.pin_snapshot(snapshot):
                assert {song.id for song in melodies.list_melodies()} == expected | {melodies.DEFAULT_MELODY_ID}
                assert melodies.get_melody('ghost-rule').id == melodies.DEFAULT_MELODY_ID
    assert (Path(old['root']) / 'cogs/tts/cd_song_scores.json').read_bytes() == original_bytes
