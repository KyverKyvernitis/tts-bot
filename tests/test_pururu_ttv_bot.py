"""Native Portuguese Pururu keeps independent identity and explicit language limits."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from cogs.tts import ttv
from test_ttv_audio import Probe, item
from test_ttv_preferences import settings
from test_ttv_ui import cog_for, interaction, modules
from test_tts_message_flow import (
    FakeCog, FakeDB, analyze_message_for_tts, dispatch_message_tts, make_message,
)


def pururu_row(**changes):
    return dict(dict(id='pururu-purpura', name='Pururu Púrpura', ready=True, available=True,
                supports_personal_rate=True, singing_available=True, ttv_performance_protocol=2,
                languages=['pt-BR'], fingerprint='pururu-bank-1',
                language_fingerprints={'pt-BR': 'pururu-pt-1'},
                singing_fingerprints={'pt-BR': 'pururu-song-1'}), **changes)


def pururu_probe(**changes):
    probe = Probe()
    probe.route['ttv_voices'].append(pururu_row(**changes))
    return probe


def test_native_portuguese_catalog_keeps_other_voices_and_default():
    preferences = dict(ttv_voice_id='pururu-purpura', ttv_language='pt-BR',
                       ttv_pitch_semitones='-1.5', ttv_speech_rate=.85)
    assert ttv.resolve_preferences(preferences) == preferences
    assert ttv.normalize_voice_id(' PURURU-PURPURA ') == 'pururu-purpura'
    assert ttv.voice_label('pururu-purpura') == 'Pururu Púrpura'
    assert ttv.get_voice('pururu-purpura').engine == 'teto'
    assert ttv.voice_language_choices('pururu-purpura') == (('pt-BR', 'Português brasileiro'),)
    assert ttv.language_label('pt-BR', voice_id='pururu-purpura') == 'Português brasileiro'
    assert ttv.language_label('pt-BR') == 'Português adaptado'
    assert ttv.resolve_preferences({})['ttv_voice_id'] == 'kasane-teto'
    assert {voice.id for voice in ttv.VOICE_CATALOG} == {
        'kasane-teto', 'hatsune-miku', 'anna-nyui', 'pururu-purpura'}


@pytest.mark.parametrize('language', ['en', 'en-US', 'EN_us'])
def test_explicit_english_is_not_repaired_to_portuguese(language):
    original = dict(ttv_voice_id='pururu-purpura', ttv_language=language)
    with pytest.raises(ValueError, match='Pururu Púrpura.*pt-BR'):
        ttv.resolve_preferences(original)
    assert original['ttv_language'] == language


def test_malformed_worker_advertisement_cannot_offer_english_for_pururu():
    probe = pururu_probe()
    row = probe.route['ttv_voices'][-1]
    row.update(languages=['pt-BR', 'en-US'],
               language_fingerprints={'pt-BR': 'native-pt', 'en-US': 'unsupported'},
               singing_fingerprints={'pt-BR': 'native-song', 'en-US': 'unsupported-song'})
    state = probe._ttv_voice_status('pururu-purpura')
    assert state['languages'] == ['pt-BR']
    assert state['language_fingerprints'] == {'pt-BR': 'native-pt'}
    assert state['singing_fingerprints'] == {'pt-BR': 'native-song'}


@pytest.mark.asyncio
async def test_pururu_preferences_persist_without_changing_edge_or_other_users(settings):
    settings.user_cache[(1, 2)] = {'tts': {'engine': 'edge', 'voice': 'EdgeVoice', 'rate': '+25%'}}
    settings.user_cache[(1, 3)] = {'tts': {'ttv_voice_id': 'hatsune-miku'}}
    await settings.set_user_tts(1, 2, ttv_voice_id='pururu-purpura', ttv_language='pt-BR',
                                ttv_pitch_semitones='-1.5', ttv_speech_rate=.85)
    resolved = settings.resolve_tts(1, 2)
    assert [resolved[key] for key in ('ttv_voice_id', 'ttv_language', 'ttv_pitch_semitones',
                                     'ttv_speech_rate')] == ['pururu-purpura', 'pt-BR', '-1.5', .85]
    assert [resolved[key] for key in ('engine', 'voice', 'rate')] == ['edge', 'EdgeVoice', '+25%']
    assert settings.user_cache[(1, 3)]['tts']['ttv_voice_id'] == 'hatsune-miku'


def test_legacy_prefix_dispatches_pururu_native_portuguese_and_personal_controls():
    async def run():
        cog = FakeCog(db=FakeDB(guild_defaults={'teto_prefix': "'"}, resolved={
            'engine': 'edge', 'voice': 'EdgeVoice', 'language': 'en',
            'ttv_voice_id': 'pururu-purpura', 'ttv_language': 'pt-BR',
            'ttv_pitch_semitones': '-1.5', 'ttv_speech_rate': .85,
        }))
        message = make_message("'Olá, Pururu")
        decision = await analyze_message_for_tts(cog, message)
        result = await dispatch_message_tts(cog, message, guild_defaults=decision.guild_defaults,
                                            active_prefix=decision.active_prefix,
                                            forced_engine=decision.forced_engine)
        assert result.enqueued
        queued = cog.enqueue_calls[0][1]
        assert queued.engine == 'teto'
        assert queued.voice == queued.ttv_voice_id == 'pururu-purpura'
        assert queued.language == queued.ttv_language == 'pt-BR'
        assert queued.ttv_pitch_semitones == '-1.5' and queued.ttv_speech_rate == .85
        assert queued.piper_fallback_voice == 'EdgeVoice'
    asyncio.run(run())


@pytest.mark.parametrize('installed', [True, False])
def test_modal_offers_pururu_only_when_ready(installed):
    async def run():
        module, _, _ = modules()
        cog = cog_for(ready=True)
        cog._ttv_voice_status = pururu_probe(ready=installed, available=installed)._ttv_voice_status
        modal = module.ModalConfiguracaoTeto(cog, None, target_user_id=10)
        options = modal.to_components()[0]['component']['options']
        assert [option['value'] for option in options] == (
            ['kasane-teto', 'pururu-purpura'] if installed else ['kasane-teto'])
    asyncio.run(run())


def test_current_pururu_modal_has_native_portuguese_only_and_saves_controls():
    async def run():
        module, _, _ = modules()
        cog = cog_for(dict(ttv_voice_id='pururu-purpura', ttv_language='pt-BR',
                           ttv_pitch_semitones='-1.5', ttv_speech_rate=.85), ready=True)
        cog._ttv_voice_status = pururu_probe()._ttv_voice_status
        modal = module.ModalConfiguracaoTeto(cog, None, target_user_id=10)
        language = modal.to_components()[1]['component']
        assert [(option['label'], option['value'], option['default'])
                for option in language['options']] == [('Português brasileiro', 'pt-BR', True)]
        modal.owner_id, modal.guild_id = 10, 20
        event = interaction()
        await modal.on_submit(event)
        cog._set_user_tts_and_refresh.assert_awaited_with(
            20, 10, ttv_voice_id='pururu-purpura', ttv_language='pt-BR',
            ttv_pitch_semitones='-1.5', ttv_speech_rate=.85)
        assert 'Português brasileiro' in event.followup.send.await_args.kwargs['content']
        fallback = module.ModalConfiguracaoTeto(cog, None, target_user_id=10, force_text_fallback=True)
        assert 'Pururu Púrpura: pururu-purpura' in fallback.ttv_voice_id.placeholder
        assert fallback.ttv_language.placeholder == 'Português brasileiro: pt-BR'
    asyncio.run(run())


def test_switching_from_english_to_pururu_saves_the_native_language_atomically():
    async def run():
        module, _, _ = modules()
        cog = cog_for(dict(ttv_voice_id='anna-nyui', ttv_language='en-US'), ready=True)
        cog._ttv_voice_status = pururu_probe()._ttv_voice_status
        modal = module.ModalConfiguracaoTeto(cog, None, target_user_id=10)
        modal.owner_id, modal.guild_id = 10, 20
        modal.ttv_voice_id._values = ['pururu-purpura']
        event = interaction()
        await modal.on_submit(event)
        cog._set_user_tts_and_refresh.assert_awaited_with(
            20, 10, ttv_voice_id='pururu-purpura', ttv_language='pt-BR',
            ttv_pitch_semitones='+0.0', ttv_speech_rate=1.0)
    asyncio.run(run())


def test_current_pururu_does_not_silently_repair_an_invalid_saved_english_choice():
    async def run():
        module, _, _ = modules()
        cog = cog_for(dict(ttv_voice_id='pururu-purpura', ttv_language='en-US'), ready=True)
        cog._ttv_voice_status = pururu_probe()._ttv_voice_status
        modal = module.ModalConfiguracaoTeto(cog, None, target_user_id=10)
        event = interaction()
        await modal.on_submit(event)
        cog._set_user_tts_and_refresh.assert_not_awaited()
        error = event.response.send_message.await_args.kwargs['embed']
        assert 'Pururu Púrpura' in error.description and 'pt-BR' in error.description
    asyncio.run(run())


def test_deliberate_english_change_cannot_be_saved_with_a_new_pururu_selection():
    async def run():
        module, _, _ = modules()
        cog = cog_for(dict(ttv_voice_id='kasane-teto', ttv_language='pt-BR'), ready=True)
        cog._ttv_voice_status = pururu_probe()._ttv_voice_status
        modal = module.ModalConfiguracaoTeto(cog, None, target_user_id=10)
        modal.ttv_voice_id._values = ['pururu-purpura']
        modal.ttv_language._value = 'en-US'
        event = interaction()
        await modal.on_submit(event)
        cog._set_user_tts_and_refresh.assert_not_awaited()
        assert 'pt-BR' in event.response.send_message.await_args.kwargs['embed'].description
    asyncio.run(run())


@pytest.mark.asyncio
async def test_explicit_database_edit_cannot_save_an_unsupported_pair_or_partial_edge_changes(settings):
    original = {'tts': {'ttv_voice_id': 'anna-nyui', 'ttv_language': 'en-US', 'voice': 'OldEdge'}}
    settings.user_cache[(1, 2)] = original
    with pytest.raises(ValueError, match='Pururu Púrpura.*pt-BR'):
        await settings.set_user_tts(1, 2, ttv_voice_id='pururu-purpura',
                                   ttv_language='en-US', voice='ChangedEdge')
    assert settings.user_cache[(1, 2)] == original
    settings.coll.update_one.assert_not_awaited()
    await settings.set_user_tts(1, 2, ttv_voice_id='pururu-purpura', ttv_language='pt-BR')
    saved = settings.resolve_tts(1, 2)
    assert saved['ttv_voice_id'] == 'pururu-purpura' and saved['ttv_language'] == 'pt-BR'
    assert saved['voice'] == 'OldEdge'


@pytest.mark.asyncio
async def test_voice_only_database_change_rejects_inherited_english_without_a_partial_write(settings):
    settings.user_cache[(1, 2)] = {'tts': {'ttv_voice_id': 'anna-nyui', 'ttv_language': 'en-US'}}
    with pytest.raises(ValueError, match='Pururu Púrpura'):
        await settings.set_user_tts(1, 2, ttv_voice_id='pururu-purpura')
    assert settings.user_cache[(1, 2)]['tts']['ttv_voice_id'] == 'anna-nyui'
    settings.coll.update_one.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('first_edit', ['voice', 'language'])
async def test_concurrent_voice_and_language_edits_cannot_persist_an_unsupported_merged_pair(settings, first_edit):
    original = {'ttv_voice_id': 'hatsune-miku', 'ttv_language': 'pt-BR', 'voice': 'OldEdge'}
    settings.user_cache[(1, 2)] = {'tts': deepcopy(original)}
    started = asyncio.Event()
    acknowledged = asyncio.Event()
    persisted = []

    async def update(_query, changes, **_kwargs):
        started.set()
        await acknowledged.wait()
        persisted.append(deepcopy(changes['$set']['tts']))

    settings.coll.update_one.side_effect = update
    edits = {'voice': {'ttv_voice_id': 'pururu-purpura'}, 'language': {'ttv_language': 'en-US'}}
    first = asyncio.create_task(settings.set_user_tts(1, 2, **edits[first_edit]))
    await started.wait()
    other_edit = 'language' if first_edit == 'voice' else 'voice'
    second = asyncio.create_task(settings.set_user_tts(1, 2, **edits[other_edit]))
    try:
        await asyncio.sleep(0)
        assert settings.coll.update_one.await_count == 1
        assert settings.user_cache[(1, 2)]['tts'] == original
    finally:
        acknowledged.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
    assert results[0] is None
    assert isinstance(results[1], ValueError)
    assert 'Pururu Púrpura' in str(results[1])
    expected = {**original, **edits[first_edit]}
    assert persisted == [expected]
    assert settings.user_cache[(1, 2)]['tts'] == expected
    assert settings.resolve_tts(1, 2)['ttv_voice_id'] == expected['ttv_voice_id']
    assert settings.resolve_tts(1, 2)['ttv_language'] == expected['ttv_language']
    assert settings.coll.update_one.await_count == 1
    assert not settings._user_tts_locks


@pytest.mark.asyncio
async def test_invalid_saved_pururu_language_does_not_block_unrelated_edge_edits(settings):
    settings.user_cache[(1, 2)] = {'tts': {'ttv_voice_id': 'pururu-purpura', 'ttv_language': 'en-US'}}
    await settings.set_user_tts(1, 2, voice='NewEdge')
    saved = settings.user_cache[(1, 2)]['tts']
    assert saved['voice'] == 'NewEdge'
    assert saved['ttv_voice_id'] == 'pururu-purpura' and saved['ttv_language'] == 'en-US'


def test_pururu_cache_does_not_reuse_other_voices_or_their_bank_revisions():
    probe = pururu_probe()
    key = probe._cache_key(item(ttv_voice_id='pururu-purpura'))
    assert key != probe._cache_key(item())
    probe.route['teto_fingerprint'] = 'new-teto-bank'
    assert probe._cache_key(item(ttv_voice_id='pururu-purpura')) == key
    probe.route['ttv_voices'][-1]['language_fingerprints']['pt-BR'] = 'new-pururu-bank'
    assert probe._cache_key(item(ttv_voice_id='pururu-purpura')) != key


def test_missing_pururu_does_not_borrow_teto_readiness():
    probe = Probe()
    state = probe._ttv_voice_status('pururu-purpura')
    assert not state['ready'] and not state['available']
    assert state['languages'] == [] and state['fingerprint'] == 'unavailable'


@pytest.mark.parametrize('returned', ['kasane-teto', 'hatsune-miku', 'anna-nyui', None])
def test_another_voice_cannot_be_played_for_pururu(tmp_path, returned):
    probe = pururu_probe()
    path = tmp_path / 'wrong-voice.wav'
    path.write_bytes(b'not Pururu')
    probe._get_metrics_store = lambda: {}
    probe._mark_tts_agent_synth_failure = Mock()
    probe._request_phone_worker_tts_audio = AsyncMock(return_value=dict(
        engine='teto', audio_format='wav', audio_path=str(path),
        ttv_voice_id=returned, ttv_language='pt-BR', ttv_pitch_semitones=0, ttv_speech_rate=1.0))
    with pytest.raises(RuntimeError, match='ajustes de TTV diferentes'):
        asyncio.run(probe._generate_tts_agent_worker_file(item(ttv_voice_id='pururu-purpura')))
    assert not path.exists()


def test_pururu_transport_keeps_native_language_and_voice_identity(tmp_path):
    probe = pururu_probe()
    path = tmp_path / 'pururu.wav'
    path.write_bytes(b'worker output contract')
    probe._get_metrics_store = lambda: {}
    probe._record_tts_agent_synth_success = Mock()
    probe._request_phone_worker_tts_audio = AsyncMock(return_value=dict(
        engine='teto', audio_format='wav', audio_path=str(path),
        ttv_voice_id='pururu-purpura', ttv_language='pt-BR', ttv_pitch_semitones=-1.5,
        ttv_speech_rate=.85))
    assert asyncio.run(probe._generate_tts_agent_worker_file(item(
        ttv_voice_id='pururu-purpura', ttv_language='pt-BR',
        ttv_pitch_semitones='-1.5', ttv_speech_rate=.85))) == str(path)
    payload = probe._request_phone_worker_tts_audio.await_args.kwargs['payload']
    assert payload['voice'] == payload['ttv_voice_id'] == 'pururu-purpura'
    assert payload['language'] == payload['ttv_language'] == 'pt-BR'
