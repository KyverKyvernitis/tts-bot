"""Pururu's PT-BR bank keeps its native recordings through speech and singing."""
from __future__ import annotations

import array
import io
from pathlib import Path
import sys
import threading
from unittest.mock import patch
import wave

import pytest

WORKER = Path(__file__).resolve().parents[1] / 'deploy/termux/phone-worker'
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer.errors import TetoConfigurationError, TetoSynthesisError
from teto_renderer.renderer import TetoRenderer


@pytest.fixture
def renderer(tmp_path):
    bank = tmp_path / 'pururu'
    bank.mkdir()
    with wave.open(str(bank / 'native.wav'), 'wb') as target:
        target.setparams((1, 2, 44100, 0, 'NONE', ''))
        target.writeframes(array.array('h', [1800, -1800] * 44100).tobytes())
    # Real Pururu has no bare vowel aliases. Its authored *V steady aliases
    # have 400 ms fixed regions, which cannot survive a 250 ms source cutoff.
    aliases = [('-a', 400, 90), ('*a', 400, 30), ('-6~', 400, 90),
               ('*6~', 400, 30), ('ka', 100, 50), ('az', 140, 90),
               ('za', 100, 50), ('a a', 100, 50), ('p6~', 100, 50)]
    (bank / 'oto.ini').write_text(''.join(
        f'native.wav={alias},0,{fixed},-900,{lead},10\n' for alias, fixed, lead in aliases))
    (bank / 'character.txt').write_text('name=Pururu - Purpura -\n')
    script = tmp_path / 'native_resampler.py'
    script.write_text(
        'import array,sys,wave\n'
        'with wave.open(sys.argv[1],"rb") as source:\n'
        ' value=array.array("h",source.readframes(1))[0]\n'
        'with wave.open(sys.argv[2],"wb") as target:\n'
        ' target.setparams((1,2,44100,0,"NONE",""))\n'
        ' target.writeframes(array.array("h",[value]*round(float(sys.argv[7])*44.1)).tobytes())\n')
    env = dict(PHONE_WORKER_TETO_ENABLED='true', PHONE_WORKER_TETO_VOICEBANK_DIR=str(bank),
               PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR=str(tmp_path / 'foreign-bank-not-used'),
               PHONE_WORKER_TETO_MIN_ALIASES='1',
               PHONE_WORKER_TETO_RESAMPLER_COMMAND=f'{sys.executable} {script}',
               PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR=str(tmp_path / 'pururu-fragments'))
    return TetoRenderer(environment=env, voice_id='pururu-purpura')


def music(count=3, duration=500, *, rest=0, source='message'):
    return dict(version=1, track_index=0, lyrics_source=source,
                duration_ms=count * duration + (count - 1) * rest,
                notes=[dict(midi_note=(60, 64, 67)[number % 3],
                            start_ms=number * (duration + rest),
                            duration_ms=duration, velocity=100)
                       for number in range(count)])


def wav_pcm(raw):
    with wave.open(io.BytesIO(raw), 'rb') as source:
        assert source.getnchannels() == 1 and source.getframerate() == 44100
        return source.readframes(source.getnframes())


def test_status_describes_a_native_portuguese_bank_without_an_english_inventory(renderer):
    status = renderer.status(force=True)
    assert status['ready'] and status['voice_id'] == 'pururu-purpura'
    assert status['voicebank_profile'] == 'portuguese-cvvc'
    assert status['ttv_languages'] == ['pt-BR']
    assert renderer._voice_label() == 'Pururu Púrpura'
    assert renderer.singing_fingerprint(reading_language='en-US') == 'unavailable'


def test_native_speech_keeps_the_recorded_cv_vc_cv_articulation(renderer):
    recorded = []
    native = renderer._resample_note

    def collect(**arguments):
        recorded.append(arguments['entry'].alias)
        return native(**arguments)

    with patch.object(renderer, '_resample_note', side_effect=collect):
        result = renderer.synthesize('casa', reading_language='pt-BR')
    assert sorted(recorded) == sorted(['ka', 'az', 'za'])
    assert result['voice_id'] == 'pururu-purpura'
    assert result['voicebank_profile'] == 'portuguese-cvvc'
    assert not result['missing_phonemes'] and any(wav_pcm(result['audio']))


def test_native_singing_preserves_both_syllables_and_the_midi_duration(renderer):
    raw, metadata = renderer.synthesize_performance('casa', score=music(count=2))
    assert metadata['voice_id'] == 'pururu-purpura'
    assert metadata['voicebank_profile'] == 'portuguese-cvvc'
    assert not metadata['missing_phonemes']
    assert metadata['singing_nuclei'] == 2
    assert len(wav_pcm(raw)) == round((1000 + metadata['midi_preroll_ms']) * 44.1) * 2


@pytest.mark.parametrize(('text', 'alias'), [('a ~ ~', '*a'), ('ã ~ ~', '*6~')])
def test_long_pure_vowel_uses_one_native_render_and_a_consistent_fixed_head(renderer, text, alias):
    recorded = []
    native = renderer._resample_note

    def collect(**arguments):
        recorded.append((arguments['entry'], arguments['note']))
        return native(**arguments)

    with patch.object(renderer, '_resample_note', side_effect=collect):
        raw, metadata = renderer.synthesize_performance(text, score=music(duration=800))
    assert len(recorded) == metadata['resampler_calls'] == metadata['singing_nuclei'] == 1
    assert metadata['melisma_notes'] == 2 and not metadata['missing_phonemes']
    entry, note = recorded[0]
    assert entry.alias == alias
    assert entry.cutoff_ms < 0 and 0 < entry.consonant_ms < -entry.cutoff_ms
    assert note.duration_ms == 2400 and any(wav_pcm(raw))


def test_midi_rest_restarts_the_authored_star_vowel_and_leaves_the_rest_quiet(renderer):
    score = music(rest=180, source='midi')
    for note, lyric in zip(score['notes'], ('ka', '~', '~')):
        note['lyric'] = lyric
    index = renderer._load_singing_index()
    renderer._index_profile = 'english-cvvc'
    moras, groups = renderer._singing_lyrics('', score, index, 'pt-BR')
    assert [mora.candidates[0] for mora in moras] == ['ka', '*a', '*a']
    assert [mora.source_phonemes for mora in moras] == [('k', 'a'), ('a',), ('a',)]
    assert groups == [[0], [1], [2]]
    raw, metadata = renderer.synthesize_performance('', score=score)
    samples = array.array('h', wav_pcm(raw))
    offset = metadata['midi_preroll_ms']
    # Stay clear of the incoming OTO preattack; the middle of the rest must
    # never be occupied by a serialized native vowel or a consonantal tail.
    middle = samples[round((530 + offset) * 44.1):round((620 + offset) * 44.1)]
    assert middle and not any(middle)


def test_first_native_pcm_does_not_wait_for_the_last_fragment_and_matches_the_final_wav(renderer):
    first = threading.Event()
    native = renderer._resample_note
    calls, chunks = [], []

    def resample(**arguments):
        calls.append(arguments['entry'].alias)
        if len(calls) == 3:
            assert first.wait(2), 'the last native fragment was required before first PCM'
        return native(**arguments)

    def consume(data, _metadata):
        if data:
            chunks.append(data)
            first.set()

    with patch.object(renderer, '_resample_note', side_effect=resample):
        raw, metadata = renderer.synthesize_performance_stream('a a a', score=music(), emit_pcm=consume)
    assert first.is_set() and calls == ['*a', '*a', '*a']
    assert b''.join(chunks) == wav_pcm(raw)
    assert metadata['voice_id'] == 'pururu-purpura' and metadata['normalization_gain'] == 1


@pytest.mark.parametrize('operation', ['speech', 'singing'])
def test_english_is_rejected_before_any_native_process_or_asset_probe(renderer, operation):
    with patch.object(renderer, '_resampler_command', side_effect=AssertionError('foreign process attempted')):
        with pytest.raises(ValueError, match='português'):
            if operation == 'speech':
                renderer.synthesize('hello', reading_language='en-US')
            else:
                renderer.synthesize_performance('hello', reading_language='en-US', score=music(count=2))


def test_missing_pururu_assets_cannot_borrow_the_configured_foreign_bank(renderer):
    settings = dict(renderer._environment)
    settings['PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR'] = settings['PHONE_WORKER_TETO_VOICEBANK_DIR']
    settings['PHONE_WORKER_TETO_VOICEBANK_DIR'] = str(Path(settings['PHONE_WORKER_TETO_VOICEBANK_DIR']).parent / 'missing-pururu')
    unavailable = TetoRenderer(environment=settings, voice_id='pururu-purpura')
    assert not unavailable.status(force=True)['ready']
    assert unavailable.singing_fingerprint() == 'unavailable'
    with pytest.raises(TetoConfigurationError):
        unavailable.synthesize('a')


@pytest.mark.parametrize('operation', ['speech', 'singing'])
def test_missing_native_consonant_or_glide_fails_before_render_instead_of_omitting_sound(renderer, operation):
    # The nucleus p6~ is present, but the necessary /w/ exit is deliberately
    # absent. Reporting successful "pão" without it would disguise bad coverage.
    with patch.object(renderer, '_resample_note', side_effect=AssertionError('partial synthesis attempted')):
        with pytest.raises(TetoSynthesisError, match='fonema'):
            if operation == 'speech':
                renderer.synthesize('pão')
            else:
                renderer.synthesize_performance('pão', score=music(count=1))
