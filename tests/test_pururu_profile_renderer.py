"""Pururu's opt-in voice profile reaches real speech, music and PCM paths."""
from __future__ import annotations

import array
from pathlib import Path
from unittest.mock import patch

import pytest

from test_pururu_renderer import renderer, music, wav_pcm  # noqa: F401
from teto_renderer.errors import TetoConfigurationError
from teto_renderer.renderer import TetoRenderer
import teto_renderer.renderer as implementation

PROFILE = 'pururu-ptbr-v1'


def configured(original, *, marker=PROFILE, voice='pururu-purpura', **updates):
    env = dict(original._environment)
    env.update(PHONE_WORKER_TETO_VOICE_PROFILE=marker,
               PHONE_WORKER_TETO_FLAGS='g5 B50', PHONE_WORKER_TETO_MODULATION='0',
               PHONE_WORKER_TETO_TEMPO='125', PHONE_WORKER_TETO_RENDER_THREADS='1')
    env.update(updates)
    return TetoRenderer(environment=env, voice_id=voice)


def test_active_profile_is_reported_by_status_speech_and_singing(renderer):
    active = configured(renderer)
    assert active.status(force=True)['voice_profile'] == PROFILE
    speech = active.synthesize('casa', reading_language='pt-BR',
                               pitch_offset_semitones=1.5, speech_rate=.85)
    raw, song = active.synthesize_performance('casa', score=music(count=2))
    assert speech['voice_profile'] == song['voice_profile'] == PROFILE
    assert speech['pitch_offset_semitones'] == 1.5 and speech['speech_rate'] == .85
    assert speech['voicebank_profile'] == song['voicebank_profile'] == 'portuguese-cvvc'
    assert not speech['missing_phonemes'] and not song['missing_phonemes']
    assert any(wav_pcm(speech['audio'])) and any(wav_pcm(raw))


def test_speech_profile_applies_to_plain_and_held_speech_but_not_score(renderer):
    active = configured(renderer)
    calls = []
    original = implementation.build_notes

    def capture(moras, **kwargs):
        notes = original(moras, **kwargs)
        calls.append((moras, kwargs, notes))
        return notes

    segments = [dict(text='a', fade_in=False, fade_out=False, hold=True, pause_after_ms=180),
                dict(text='a', fade_in=False, fade_out=False, hold=False, pause_after_ms=0)]
    with patch.object(implementation, 'build_notes', side_effect=capture):
        active.synthesize('casa')
        raw, held = active.synthesize_performance('a a', segments=segments)
        active.synthesize_performance('casa', score=music(count=2))
    assert len(calls) == 2, 'MIDI must retain its score-derived notes'
    assert all(call[1]['speech_profile'] == PROFILE for call in calls)
    moras, kwargs, notes = calls[1]
    assert [m.candidates[0] for m in moras] == ['-a', '-a']
    assert moras[0].phrase_end == '|' and moras[0].pause_after_ms == 180
    assert held['performance_mode'] == 'speech-effects' and any(wav_pcm(raw))


def test_plain_speech_uses_authored_cross_word_link_before_the_existing_casa_units(renderer):
    bank = Path(renderer._environment['PHONE_WORKER_TETO_VOICEBANK_DIR'])
    with (bank / 'oto.ini').open('a') as target:
        target.write('native.wav=ak,0,100,-900,50,10\n')
    active = configured(renderer)
    calls = []
    resample = active._resample_note

    def capture(**kwargs):
        calls.append(kwargs['entry'].alias)
        return resample(**kwargs)

    with patch.object(active, '_resample_note', side_effect=capture):
        output = active.synthesize('a casa')
    assert calls == ['-a', 'ak', 'ka', 'az', 'za']
    assert not output['missing_phonemes']
    assert output['voice_profile'] == PROFILE


def test_profile_keeps_native_midi_attacks_and_silent_rest(renderer):
    active = configured(renderer)
    score = music(rest=180, source='midi')
    for note, lyric in zip(score['notes'], ('ka', '~', '~')):
        note['lyric'] = lyric
    index = active._load_singing_index()
    active._index_profile = 'english-cvvc'
    moras, groups = active._singing_lyrics('', score, index, 'pt-BR')
    assert [m.candidates[0] for m in moras] == ['ka', '*a', '*a']
    assert groups == [[0], [1], [2]]
    raw, metadata = active.synthesize_performance('', score=score)
    samples = array.array('h', wav_pcm(raw))
    offset = metadata['midi_preroll_ms']
    middle = samples[round((530 + offset) * 44.1):round((620 + offset) * 44.1)]
    assert middle and not any(middle)
    assert len(samples) == round((score['duration_ms'] + offset) * 44.1)


def test_profile_streams_sealed_pcm_before_final_fragment_and_matches_final_wave(renderer):
    active = configured(renderer)
    chunks, calls = [], []
    original = active._resample_note

    def capture(**kwargs):
        calls.append(kwargs['entry'].alias)
        if len(calls) == 3:
            assert chunks, 'later fragments must not be required before the first PCM'
        return original(**kwargs)

    def consume(raw, metadata):
        if raw:
            chunks.append(raw)

    with patch.object(active, '_resample_note', side_effect=capture):
        raw, metadata = active.synthesize_performance_stream('a a a', score=music(), emit_pcm=consume)
    assert metadata['voice_profile'] == PROFILE
    assert calls == ['*a', '*a', '*a']
    assert b''.join(chunks) == wav_pcm(raw)


def test_legacy_profile_identity_and_notes_stay_separate_from_opt_in_profile(renderer):
    env = dict(renderer._environment)
    legacy = TetoRenderer(environment={**env, 'PHONE_WORKER_TETO_VOICE_PROFILE': 'legacy'},
                          voice_id='pururu-purpura')
    active = TetoRenderer(environment={**env, 'PHONE_WORKER_TETO_VOICE_PROFILE': PROFILE},
                          voice_id='pururu-purpura')
    assert renderer.fingerprint() == legacy.fingerprint()
    assert active.fingerprint() != legacy.fingerprint()
    assert renderer.singing_fingerprint() == legacy.singing_fingerprint()
    assert active.singing_fingerprint() != legacy.singing_fingerprint()
    assert renderer.status()['voice_profile'] == legacy.status()['voice_profile'] == 'legacy'


@pytest.mark.parametrize('voice', ['kasane-teto', 'hatsune-miku', 'anna-nyui'])
def test_pururu_profile_marker_cannot_change_other_characters_cache_identity(renderer, voice):
    env = dict(renderer._environment)
    first = TetoRenderer(environment=env, voice_id=voice)
    second = TetoRenderer(environment={**env, 'PHONE_WORKER_TETO_VOICE_PROFILE': PROFILE}, voice_id=voice)
    index = renderer._load_index()
    assert first._render_fingerprint(index) == second._render_fingerprint(index)
    assert first._reading_fingerprint(index, 'pt-BR') == second._reading_fingerprint(index, 'pt-BR')


@pytest.mark.parametrize('operation', ['speech', 'singing'])
def test_native_only_language_guard_precedes_synthesis_under_active_profile(renderer, operation):
    active = configured(renderer)
    with patch.object(active, '_resampler_command', side_effect=AssertionError('foreign synthesis attempted')):
        with pytest.raises(ValueError, match='português'):
            if operation == 'speech':
                active.synthesize('hello', reading_language='en-US')
            else:
                active.synthesize_performance('hello', reading_language='en-US', score=music(count=2))


def test_unknown_voice_profile_is_not_advertised_ready(renderer):
    active = configured(renderer, marker='unknown')
    status = active.status(force=True)
    assert not status['ready'] and 'perfil' in status['last_error']
    with pytest.raises(TetoConfigurationError, match='perfil'):
        active.synthesize('casa')
