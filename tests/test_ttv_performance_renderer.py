"""Musical clock, vowel continuations and actual PCM effect regression tests."""
from __future__ import annotations

import array
import hashlib
import io
import json
import sys
import wave
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'deploy' / 'termux' / 'phone-worker'
sys.path.insert(0, str(WORKER)) if str(WORKER) not in sys.path else None
from teto_renderer.renderer import TetoRenderer
from teto_renderer.prosody import (
    RenderNote, align_singing_pitch, align_singing_syllables,
    singing_pitch_at, performance_hash, validate_performance,
)
from teto_renderer.phonemizer import Mora


def score(pitches=(60, 64, 67), lengths=None, starts=None, source='message'):
    lengths = lengths or [400] * len(pitches)
    starts = starts or [sum(lengths[:number]) for number in range(len(pitches))]
    return {'version': 1, 'track_index': 0, 'duration_ms': starts[-1] + lengths[-1],
            'lyrics_source': source, 'notes': [{'midi_note': pitch, 'start_ms': start,
                                               'duration_ms': length, 'velocity': 100}
                                              for pitch, start, length in zip(pitches, starts, lengths)]}


def segment(text, **effects):
    return {'text': text, 'fade_in': False, 'fade_out': False,
            'hold': False, 'pause_after_ms': 0, **effects}


def pcm(raw):
    with wave.open(io.BytesIO(raw), 'rb') as wav:
        return array.array('h', wav.readframes(wav.getnframes())), wav.getframerate()


@pytest.fixture
def renderer(tmp_path):
    bank = tmp_path / 'bank'
    bank.mkdir()
    with wave.open(str(bank / 'vowel.wav'), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(array.array('h', [1000] * 44100).tobytes())
    aliases = ['- a', 'a', 'A', 'a a', '- mo', 'mo', 'o 4', 'o r', '- A', 'a mo']
    (bank / 'oto.ini').write_text('\n'.join('vowel.wav=' + alias + ',0,30,-600,20,10' for alias in aliases))
    executable = tmp_path / 'resampler.py'
    executable.write_text('''import array, math, sys, wave
pitch, length, tempo, bend = sys.argv[3], float(sys.argv[7]), float(sys.argv[12][1:]), sys.argv[13]
classes={'C':0,'D':2,'E':4,'F':5,'G':7,'A':9,'B':11}
name=pitch[0]; acc=1 if '#' in pitch else -1 if 'b' in pitch else 0
octave=int(pitch[2:] if acc else pitch[1:]); midi=(octave+1)*12+classes[name]+acc
chars='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
values=[]; i=0
while i<len(bend):
 v=chars.index(bend[i])*64+chars.index(bend[i+1]); v=v-4096 if v>=2048 else v; i+=2; count=1
 if i<len(bend) and bend[i]=='#':
  j=bend.index('#',i+1); count=int(bend[i+1:j]); i=j+1
 values.extend([v]*count)
phase=0; data=array.array('h'); interval=60000/(tempo*96)
for frame in range(round(length*44.1)):
 point=frame/44.1/interval; left=min(len(values)-1,int(point)); right=min(len(values)-1,left+1)
 cents=values[left]+(values[right]-values[left])*(point-int(point))
 hz=440*2**((midi-69+cents/100)/12); phase+=2*math.pi*hz/44100
 data.append(round(8000*math.sin(phase)))
with wave.open(sys.argv[2],'wb') as out:
 out.setnchannels(1);out.setsampwidth(2);out.setframerate(44100);out.writeframes(data.tobytes())
''')
    env = {'PHONE_WORKER_TETO_ENABLED': 'true', 'PHONE_WORKER_TETO_VOICEBANK_DIR': str(bank),
           'PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR': str(bank), 'PHONE_WORKER_TETO_VOICEBANK_MODE': 'english',
           'PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES': '1', 'PHONE_WORKER_TETO_MIN_ALIASES': '1',
           'PHONE_WORKER_TETO_RESAMPLER_COMMAND': str(Path(sys.executable)) + ' ' + str(executable),
           'PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR': str(tmp_path / 'cache'),
           'PHONE_WORKER_TETO_LENGTH_MODE': 'total', 'PHONE_WORKER_TETO_MAX_AUDIO_SECONDS': '20'}
    return TetoRenderer(environment=env)


def test_canonical_performance_hash_matches_wire_protocol():
    music = score()
    expected = hashlib.sha256(json.dumps({'version': 1, 'text': 'Olá Miku', 'score': music,
                                          'segments': None}, sort_keys=True, separators=(',', ':'),
                                         ensure_ascii=False).encode()).hexdigest()
    assert performance_hash('  Olá\n Miku ', score=music) == expected
    assert performance_hash('Olá Miku', score=music) != performance_hash('Olá Miku', score=score((60, 65, 67)))


@pytest.mark.parametrize('mutation', [
    lambda s: s['notes'][0].update(duration_ms=39),
    lambda s: s['notes'][1].update(start_ms=20),
    lambda s: s.update(duration_ms=60001),
    lambda s: s['notes'][0].update(midi_note=True),
    lambda s: s['notes'][0].update(velocity=0),
    lambda s: s['notes'][0].update(start_ms=float('nan')),
])
def test_bad_scores_rejected_before_native_process(mutation):
    music = score()
    mutation(music)
    with pytest.raises(ValueError):
        validate_performance('a', score=music)


def test_long_midi_lyric_and_literal_marker_are_accepted():
    music = score((60,))
    music['notes'][0]['lyric'] = 'a' * 128
    validate_performance('', score=music)
    validate_performance('a ~', segments=[segment('a'), segment('~')])
    with pytest.raises(ValueError, match='correspondem'):
        validate_performance('a', segments=[segment('b')])


def test_melisma_is_one_native_vowel_and_cache_identical(renderer):
    music = score(lengths=[400, 500, 900])
    raw, meta = renderer.synthesize_performance('a ~ ~', score=music)
    assert meta['singing_nuclei'] == 1
    assert meta['melisma_notes'] == 2
    assert meta['resampler_calls'] == 1
    assert meta['pitchbend_fallbacks'] == 0
    samples, rate = pcm(raw)
    assert len(samples) == round(rate * (1800 + meta['midi_preroll_ms']) / 1000)
    repeated, warm = renderer.synthesize_performance('a ~ ~', score=music)
    assert repeated == raw
    assert warm['fragment_cache_hits'] == 1
    assert warm['resampler_calls'] == 0


def test_auto_melisma_uses_musical_rests(renderer):
    music = score((60, 62, 64, 65), [400] * 4, [0, 400, 1200, 1600])
    raw, meta = renderer.synthesize_performance('a a', score=music)
    assert meta['singing_nuclei'] == 2
    assert meta['melisma_notes'] == 2
    samples, rate = pcm(raw)
    shift = meta['midi_preroll_ms']
    gap = samples[round(rate * (850 + shift) / 1000):round(rate * (1100 + shift) / 1000)]
    assert gap and not any(gap)


def test_explicit_continuation_cannot_erase_midi_rest(renderer):
    music = score((60, 64), [400, 400], [0, 800])
    with pytest.raises(ValueError, match='pausa MIDI'):
        renderer.synthesize_performance('a ~', score=music)


def test_embedded_missing_lyric_after_rest_restarts_vowel(renderer):
    music = score((60, 64), [400, 400], [0, 800], source='midi')
    music['notes'][0]['lyric'] = 'a'
    music['notes'][1]['lyric'] = '~'
    raw, meta = renderer.synthesize_performance('', score=music)
    assert meta['singing_nuclei'] == 2
    assert meta['melisma_notes'] == 0
    assert raw[:4] == b'RIFF'


def test_midi_does_not_inherit_speech_controls(renderer):
    with pytest.raises(ValueError, match='preserva tom'):
        renderer.synthesize_performance('a', score=score((60,)), pitch_offset_semitones=-2)
    with pytest.raises(ValueError, match='preserva tom'):
        renderer.synthesize_performance('a', score=score((60,)), speech_rate=1.25)


def test_wide_jump_between_attacks_does_not_clamp_or_reject(renderer):
    music = score((36, 96), [100, 100])
    raw, meta = renderer.synthesize_performance('a a', score=music)
    assert meta['midi_notes'] == 2
    assert meta['pitchbend_fallbacks'] == 0
    assert raw[:4] == b'RIFF'


def test_inline_envelopes_change_only_word_edges(renderer):
    baseline, before = renderer.synthesize_performance('a a', segments=[segment('a'), segment('a')])
    effected, after = renderer.synthesize_performance('a a', segments=[segment('a', fade_in=True), segment('a', fade_out=True)])
    clean, rate = pcm(baseline)
    changed, _ = pcm(effected)
    assert len(clean) == len(changed)
    assert clean != changed
    middle = len(clean) // 2
    assert clean[middle - 20:middle + 20] == changed[middle - 20:middle + 20]
    assert after['ttv_performance_hash'] != before['ttv_performance_hash']


def test_hold_extends_voiced_vowel_and_pause_is_silence(renderer):
    plain, meta = renderer.synthesize_performance('a', segments=[segment('a')])
    held, hold_meta = renderer.synthesize_performance('a', segments=[segment('a', hold=True)])
    clean, rate = pcm(plain)
    extended, _ = pcm(held)
    assert len(extended) - len(clean) == pytest.approx(rate, abs=1)
    assert any(extended[len(clean):])
    paused, _ = renderer.synthesize_performance('a | a', segments=[segment('a', pause_after_ms=180), segment('|'), segment('a')])
    values, _ = pcm(paused)
    longest = current = 0
    for value in values:
        current = current + 1 if value == 0 else 0
        longest = max(longest, current)
    assert longest >= round(rate * .179)


def test_score_lyric_literal_tilde_is_not_a_melisma(renderer):
    segments = [segment('a'), segment('~'), segment('a')]
    _, meta = renderer.synthesize_performance('a ~ a', score=score((60, 64)), segments=segments)
    assert meta['singing_nuclei'] == 2
    assert meta['melisma_notes'] == 0


def test_shared_music_curve_retains_long_sustain_and_late_vibrato():
    music = score((60,), [1800])
    note = RenderNote(('a',), 'C4', 1800, 0)
    placement = {'start_ms': 0, 'lead_ms': 0, 'score_group': [0]}
    aligned = align_singing_pitch([note], [placement], music, preroll_ms=0, tempo=140)[0]
    assert aligned.pitchbend != 'AA'
    assert singing_pitch_at(music, 200) == 6000
    assert singing_pitch_at(music, 550) == 6000
    assert abs(singing_pitch_at(music, 1000) - 6000) <= 12
    assert abs(singing_pitch_at(music, 1000) - 6000) > 1


def test_extra_syllables_have_actionable_count_error():
    moras = [Mora(('a',), role='nucleus', word_index=index) for index in range(3)]
    with pytest.raises(ValueError, match='3 sílabas para 2 notas'):
        align_singing_syllables(moras, score((60, 64)))


def test_native_long_vowel_limit_has_actionable_error_before_resampling(renderer):
    with pytest.raises(ValueError, match='18 segundos'):
        renderer.synthesize_performance('a', score=score((60,), [19000]))
    with pytest.raises(ValueError, match='18 segundos'):
        renderer.synthesize_performance('a ~', score=score((60, 64), [10000, 10000]))


def test_performance_context_does_not_change_standard_speech_or_its_cache(renderer):
    before = renderer.synthesize('aaa', timeout_seconds=10, speech_rate=1.0, reading_language='pt-BR')
    renderer.synthesize_performance('a ~ ~', score=score(), timeout_seconds=10)
    after = renderer.synthesize('aaa', timeout_seconds=10, speech_rate=1.0, reading_language='pt-BR')
    assert before['audio'] == after['audio']
    assert before['renderer_fingerprint'] == after['renderer_fingerprint']
    assert after['resampler_calls'] == 0
    assert not hasattr(renderer._reading_context, 'performance_singing')
    assert not hasattr(renderer._reading_context, 'performance_sources')
