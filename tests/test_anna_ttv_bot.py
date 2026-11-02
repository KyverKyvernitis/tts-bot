"""The third ESPER character keeps its own settings, readiness and audio."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from cogs.tts import ttv
from test_ttv_audio import Probe, item
from test_ttv_preferences import settings
from test_ttv_ui import cog_for, interaction, modules
from test_tts_message_flow import (
    FakeCog, FakeDB, analyze_message_for_tts, dispatch_message_tts, make_message,
)


def anna_row(**changes):
    return {
        'id': 'anna-nyui', 'name': 'Anna Nyui', 'ready': True, 'available': True,
        'supports_personal_rate': True, 'singing_available': True,
        'ttv_performance_protocol': 2, 'languages': ['pt-BR', 'en-US'],
        'fingerprint': 'anna-bank-1',
        'language_fingerprints': {'pt-BR': 'anna-pt-1', 'en-US': 'anna-en-1'},
        'singing_fingerprints': {'pt-BR': 'anna-song-pt-1', 'en-US': 'anna-song-en-1'},
        **changes,
    }


def anna_probe(**changes):
    probe = Probe()
    probe.route['ttv_voices'].append(anna_row(**changes))
    return probe


def test_anna_keeps_her_identity_and_controls_without_changing_the_default():
    preferences = dict(ttv_voice_id='anna-nyui', ttv_language='en-US',
                       ttv_pitch_semitones='-1.5', ttv_speech_rate=.85)
    assert ttv.resolve_preferences(preferences) == preferences
    assert ttv.normalize_voice_id(' ANNA-NYUI ') == 'anna-nyui'
    assert ttv.get_voice('anna-nyui').engine == 'teto'
    assert ttv.voice_label('anna-nyui') == 'Anna Nyui'
    assert ttv.resolve_preferences({})['ttv_voice_id'] == 'pururu-purpura'


@pytest.mark.asyncio
async def test_anna_preferences_persist_without_changing_other_users_or_other_engines(settings):
    settings.user_cache[(1, 2)] = {'tts': {'engine': 'edge', 'voice': 'EdgeVoice', 'rate': '+25%'}}
    settings.user_cache[(1, 3)] = {'tts': {'ttv_voice_id': 'hatsune-miku'}}
    await settings.set_user_tts(1, 2, ttv_voice_id='anna-nyui', ttv_language='en-US',
                                ttv_pitch_semitones='-1.5', ttv_speech_rate=.85)
    resolved = settings.resolve_tts(1, 2)
    assert [resolved[key] for key in ('ttv_voice_id', 'ttv_language', 'ttv_pitch_semitones',
                                     'ttv_speech_rate')] == ['anna-nyui', 'en-US', '-1.5', .85]
    assert [resolved[key] for key in ('engine', 'voice', 'rate')] == ['edge', 'EdgeVoice', '+25%']
    assert settings.user_cache[(1, 3)]['tts']['ttv_voice_id'] == 'hatsune-miku'
    assert settings.coll.update_one.await_args.args[1]['$set']['tts']['ttv_voice_id'] == 'anna-nyui'


def test_legacy_prefix_dispatches_the_selected_anna_voice_and_controls():
    async def run():
        cog = FakeCog(db=FakeDB(guild_defaults={'teto_prefix': "'"}, resolved={
            'engine': 'edge', 'voice': 'EdgeVoice', 'language': 'pt-br',
            'ttv_voice_id': 'anna-nyui', 'ttv_language': 'en-US',
            'ttv_pitch_semitones': '-1.5', 'ttv_speech_rate': .85,
        }))
        message = make_message("'Hello Anna")
        decision = await analyze_message_for_tts(cog, message)
        result = await dispatch_message_tts(cog, message, guild_defaults=decision.guild_defaults,
                                            active_prefix=decision.active_prefix,
                                            forced_engine=decision.forced_engine)
        assert result.enqueued
        queued = cog.enqueue_calls[0][1]
        assert queued.engine == 'teto'
        assert queued.voice == queued.ttv_voice_id == 'anna-nyui'
        assert queued.language == queued.ttv_language == 'en-US'
        assert queued.ttv_pitch_semitones == '-1.5' and queued.ttv_speech_rate == .85
        assert queued.piper_fallback_voice == 'EdgeVoice'
    asyncio.run(run())


def test_missing_anna_never_borrows_teto_or_miku_readiness_and_fingerprints():
    probe = Probe()
    probe.route.update(ttv_languages=['pt-BR', 'en-US'],
                       ttv_language_fingerprints={'pt-BR': 'teto-pt'},
                       ttv_singing_fingerprints={'pt-BR': 'teto-song'})
    probe.route['ttv_voices'].append(dict(anna_row(), id='hatsune-miku', name='Hatsune Miku'))
    state = probe._ttv_voice_status('anna-nyui')
    assert not state['advertised'] and not state['available'] and not state['ready']
    assert state['fingerprint'] == 'unavailable'
    assert state['languages'] == [] and state['language_fingerprints'] == {}
    assert state['singing_fingerprints'] == {}


def test_anna_audio_cache_tracks_only_her_bank_and_requested_language():
    probe = anna_probe()
    portuguese = probe._cache_key(item(ttv_voice_id='anna-nyui'))
    english = probe._cache_key(item(ttv_voice_id='anna-nyui', ttv_language='en-US'))
    assert english != portuguese
    assert portuguese != probe._cache_key(item(ttv_voice_id="kasane-teto"))
    probe.route['teto_fingerprint'] = 'new-teto-bank'
    assert probe._cache_key(item(ttv_voice_id='anna-nyui')) == portuguese
    row = probe.route['ttv_voices'][1]
    row['language_fingerprints']['en-US'] = 'anna-en-2'
    assert probe._cache_key(item(ttv_voice_id='anna-nyui', ttv_language='en-US')) != english
    assert probe._cache_key(item(ttv_voice_id='anna-nyui')) == portuguese


@pytest.mark.parametrize('installed', [True, False])
def test_modal_offers_anna_only_when_her_worker_row_is_ready(installed):
    async def run():
        module, _, _ = modules()
        cog = cog_for(ready=True)
        cog._ttv_voice_status = anna_probe(ready=installed, available=installed)._ttv_voice_status
        modal = module.ModalConfiguracaoTeto(cog, None, target_user_id=10)
        options = modal.to_components()[0]['component']['options']
        assert [option['value'] for option in options] == (
            ['kasane-teto', 'anna-nyui'] if installed else ['kasane-teto'])
        assert [option['value'] for option in options if option['default']] == ['kasane-teto']
        assert len(modal.to_components()) == 4
    asyncio.run(run())


def test_anna_modal_save_reset_and_text_fallback_preserve_the_character():
    async def run():
        module, actions, _ = modules()
        cog = cog_for(dict(ttv_voice_id='anna-nyui', ttv_language='en-US',
                           ttv_pitch_semitones='-1.5', ttv_speech_rate=.85), ready=True)
        cog._ttv_voice_status = anna_probe()._ttv_voice_status
        modal = module.ModalConfiguracaoTeto(cog, None, target_user_id=10)
        modal.owner_id, modal.guild_id = 10, 20
        await modal.on_submit(interaction())
        cog._set_user_tts_and_refresh.assert_awaited_with(
            20, 10, ttv_voice_id='anna-nyui', ttv_language='en-US',
            ttv_pitch_semitones='-1.5', ttv_speech_rate=.85)
        confirmation = actions.VisaoConfirmacaoTTV(cog, 10, 20, 10, 'Usuário', None)
        await confirmation.restaurar_padroes(interaction())
        cog._set_user_tts_and_refresh.assert_awaited_with(
            20, 10, ttv_voice_id='anna-nyui', ttv_language='en-US',
            ttv_pitch_semitones='+0.0', ttv_speech_rate=1.0)
        fallback = module.ModalConfiguracaoTeto(cog, None, target_user_id=10, force_text_fallback=True)
        assert fallback.ttv_voice_id.default == 'anna-nyui'
        assert 'Anna Nyui: anna-nyui' in fallback.ttv_voice_id.placeholder
    asyncio.run(run())


@pytest.mark.parametrize('changes', [dict(ready=False), dict(available=False), None])
def test_unavailable_anna_is_rejected_before_contacting_the_worker(changes):
    probe = Probe() if changes is None else anna_probe(**changes)
    probe._request_phone_worker_tts_audio = AsyncMock()
    with pytest.raises(RuntimeError, match='Anna Nyui está indisponível'):
        asyncio.run(probe._generate_tts_agent_worker_file(item(ttv_voice_id='anna-nyui')))
    probe._request_phone_worker_tts_audio.assert_not_awaited()


@pytest.mark.parametrize('returned', ['kasane-teto', 'hatsune-miku', None])
def test_another_voice_cannot_be_played_for_anna(tmp_path, returned):
    probe = anna_probe()
    path = tmp_path / 'wrong-voice.wav'
    path.write_bytes(b'another character must not play')
    probe._get_metrics_store = lambda: {}
    probe._mark_tts_agent_synth_failure = Mock()
    probe._request_phone_worker_tts_audio = AsyncMock(return_value=dict(
        engine='teto', audio_format='wav', audio_path=str(path),
        ttv_voice_id=returned, ttv_language='pt-BR', ttv_pitch_semitones=0,
        ttv_speech_rate=1.0))
    with pytest.raises(RuntimeError, match='ajustes de TTV diferentes'):
        asyncio.run(probe._generate_tts_agent_worker_file(item(ttv_voice_id='anna-nyui')))
    assert not path.exists()


@pytest.mark.parametrize('language', ['pt-BR', 'en-US'])
def test_anna_transport_sends_and_validates_the_selected_character(tmp_path, language):
    probe = anna_probe()
    path = tmp_path / 'anna.wav'
    path.write_bytes(b'worker output contract')
    probe._get_metrics_store = lambda: {}
    probe._record_tts_agent_synth_success = Mock()
    probe._request_phone_worker_tts_audio = AsyncMock(return_value=dict(
        engine='teto', audio_format='wav', audio_path=str(path),
        ttv_voice_id='anna-nyui', ttv_language=language, ttv_pitch_semitones=-1.5,
        ttv_speech_rate=.85))
    result = asyncio.run(probe._generate_tts_agent_worker_file(item(
        ttv_voice_id='anna-nyui', ttv_language=language,
        ttv_pitch_semitones='-1.5', ttv_speech_rate=.85)))
    assert result == str(path)
    payload = probe._request_phone_worker_tts_audio.await_args.kwargs['payload']
    assert payload['engine'] == payload['preferred_engine'] == 'teto'
    assert payload['voice'] == payload['ttv_voice_id'] == 'anna-nyui'
    assert payload['language'] == payload['ttv_language'] == language
