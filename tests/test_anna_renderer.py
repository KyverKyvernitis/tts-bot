"""Anna's renderer uses actual pitch layers and isolated Cz vowel recordings."""
from __future__ import annotations

import array
import io
from pathlib import Path
import sys
from unittest.mock import patch
import wave

import pytest

WORKER = Path(__file__).resolve().parents[1] / 'deploy/termux/phone-worker'
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from teto_renderer.renderer import TetoRenderer
from teto_renderer.errors import TetoConfigurationError
from teto_renderer.prosody import RenderNote


@pytest.fixture
def renderer(tmp_path):
    bank = tmp_path / 'bank'
    bank.mkdir()
    lines = []
    for suffix, value in [('B3', 1100), ('', 2200), ('A4', 3300)]:
        filename = (suffix or 'E4') + '.wav'
        with wave.open(str(bank / filename), 'wb') as target:
            target.setparams((1, 2, 44100, 0, 'NONE', ''))
            target.writeframes(array.array('h', [value] * 44100).tobytes())
        for alias in ('-a', 'a', '-I', 'I', '-pa'):
            lines.append(f'{filename}={alias}{suffix},0,30,-900,20,10\n')
    (bank / 'oto.ini').write_text(''.join(lines))
    (bank / 'prefix.map').write_text('C4\t\tB3\nE4\t\t\nA4\t\tA4\n')
    script = tmp_path / 'resampler.py'
    script.write_text(
        'import array,sys,wave\n'
        'with wave.open(sys.argv[1],"rb") as source:\n'
        ' value=array.array("h",source.readframes(1))[0]\n'
        'with wave.open(sys.argv[2],"wb") as target:\n'
        ' target.setparams((1,2,44100,0,"NONE",""))\n'
        ' target.writeframes(array.array("h",[value]*round(float(sys.argv[7])*44.1)).tobytes())\n')
    env = dict(PHONE_WORKER_TETO_ENABLED='true', PHONE_WORKER_TETO_VOICEBANK_DIR=str(bank),
               PHONE_WORKER_TETO_ENGLISH_VOICEBANK_DIR=str(bank),
               PHONE_WORKER_TETO_MIN_ALIASES='1', PHONE_WORKER_TETO_ENGLISH_MIN_ALIASES='1',
               PHONE_WORKER_TETO_RESAMPLER_COMMAND=f'{sys.executable} {script}',
               PHONE_WORKER_TETO_FRAGMENT_CACHE_DIR=str(tmp_path / 'fragments'))
    return TetoRenderer(environment=env, voice_id='anna-nyui')


def music(*, rest=0, source='message'):
    return dict(version=1, track_index=0, lyrics_source=source,
                duration_ms=1200 + rest * 2,
                notes=[dict(midi_note=pitch, start_ms=number * (400 + rest),
                            duration_ms=400, velocity=100)
                       for number, pitch in enumerate((60, 64, 69))])


def test_three_midi_registers_choose_three_authored_recordings_and_reuse_fragments(renderer):
    rendered = []
    native = renderer._resample_note

    def record(**arguments):
        rendered.append((arguments['note'].pitch, arguments['entry'].alias))
        return native(**arguments)

    with patch.object(renderer, '_resample_note', side_effect=record):
        raw, metadata = renderer.synthesize_performance('a a a', score=music())
    assert sorted(rendered) == sorted([('C4', 'aB3'), ('E4', 'a'), ('A4', 'aA4')])
    assert metadata['voice_id'] == 'anna-nyui' and not metadata['missing_phonemes']
    assert metadata['resampler_calls'] == 3
    repeated, warm = renderer.synthesize_performance('a a a', score=music())
    assert repeated == raw and warm['resampler_calls'] == 0


def test_native_cz_joined_vowel_uses_lexical_source_for_its_steady_recording(renderer):
    note = RenderNote(('aI',), 'C4', 1000, 0, source_phonemes=('a', 'j'))
    assert renderer._steady_vowel_candidates(note) == ('I',)
    note = RenderNote(('-a',), 'C4', 1000, 0, source_phonemes=('a~',))
    assert renderer._steady_vowel_candidates(note) == ('a',)


def test_midi_rest_restarts_the_steady_vowel_without_its_old_consonant(renderer):
    score = music(rest=180, source='midi')
    for note, lyric in zip(score['notes'], ('pa', '~', '~')):
        note['lyric'] = lyric
    index = renderer._load_english_index()
    renderer._index_profile = 'english-cvvc'
    moras, groups = renderer._singing_lyrics('', score, index, 'pt-BR')
    assert [mora.source_phonemes for mora in moras] == [('p', 'a'), ('a',), ('a',)]
    assert moras[1].candidates == moras[2].candidates == ('a',)
    assert groups == [[0], [1], [2]]


def test_anna_streaming_keeps_prefixes_identical_to_the_completed_wav(renderer):
    chunks = []
    raw, meta = renderer.synthesize_performance_stream('a a a', score=music(),
        emit_pcm=lambda data, _: chunks.append(data))
    with wave.open(io.BytesIO(raw), 'rb') as audio:
        assert b''.join(chunks) == audio.readframes(audio.getnframes())
    assert len([chunk for chunk in chunks if chunk]) > 1
    assert meta['voice_id'] == 'anna-nyui'


def test_missing_anna_settings_cannot_fall_back_to_the_teto_bank(monkeypatch, tmp_path):
    monkeypatch.setenv('PHONE_WORKER_TETO_ENABLED', 'true')
    monkeypatch.setenv('PHONE_WORKER_TETO_VOICEBANK_DIR', str(tmp_path))
    with pytest.raises(TetoConfigurationError, match='independente'):
        TetoRenderer(voice_id='anna-nyui')
    unavailable = TetoRenderer(environment={}, voice_id='anna-nyui')
    assert not unavailable.status(force=True)['ready']
    assert unavailable.singing_fingerprint() == 'unavailable'
