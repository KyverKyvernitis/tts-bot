"""Updated Portuguese readings cross the VPS/worker and cache boundaries."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'deploy/termux/phone-worker'
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from osaka_cd.lyrics import automatic_render_plan, prepare_message_arrangement
from teto_renderer.renderer import TetoRenderer


def _renderer(voice='kasane-teto'):
    result = TetoRenderer(environment={'PHONE_WORKER_TETO_RESAMPLER_COMMAND': sys.executable},
                          voice_id=voice)
    result._index_profile = 'english-cvvc'
    return result


@pytest.mark.parametrize('voice', ['kasane-teto', 'hatsune-miku'])
def test_portuguese_reading_revision_invalidates_utterances_but_reuses_native_fragments(voice):
    renderer = _renderer(voice)
    index = SimpleNamespace(fingerprint='unchanged-bank')
    controls = {'pitch_offset_semitones': 0, 'speech_rate': 1}
    old_pt = renderer._request_fingerprint(index, reading_language='pt-BR', **controls)
    old_en = renderer._request_fingerprint(index, reading_language='en-US', **controls)
    fragments = renderer._fragment_fingerprint(index)
    renderer.PHONEMIZER_VERSION += '-next-pronunciation'
    assert renderer._request_fingerprint(index, reading_language='pt-BR', **controls) != old_pt
    assert renderer._request_fingerprint(index, reading_language='en-US', **controls) == old_en
    assert renderer._fragment_fingerprint(index) == fragments


def test_portuguese_source_changes_cannot_replay_an_old_reading_cache():
    index = SimpleNamespace(fingerprint='unchanged-bank')
    previous = _renderer()._reading_fingerprint(index, 'pt-BR')
    read_bytes = Path.read_bytes

    def revised_source(path):
        contents = read_bytes(path)
        return contents + b'\n# revised Portuguese lexicon\n' if path.name == 'ptbr_g2p.py' else contents

    with patch.object(Path, 'read_bytes', revised_source):
        assert _renderer()._reading_fingerprint(index, 'pt-BR') != previous


@pytest.mark.parametrize('voice', ['kasane-teto', 'hatsune-miku'])
@pytest.mark.parametrize('gap', [0, 120])
def test_corrected_words_and_second_holds_use_the_same_syllables_on_vps_and_worker(voice, gap):
    notes = [dict(midi_note=60 + number % 5, start_ms=number * (250 + gap),
                  duration_ms=250, velocity=100) for number in range(16)]
    template = dict(score_schema=2, duration_ms=notes[-1]['start_ms'] + 250,
                    parts=[dict(id='lead', role='lead', voice_id=voice,
                                lyrics_source='message', notes=notes)])
    original = copy.deepcopy(template)
    effects = [dict(text=word, hold=held, fade_in=False, fade_out=False, pause_after_ms=0)
               for word, held in [('água', True), ('~', True), ('quase', False)]]
    text = 'água ~ quase'
    prepared = prepare_message_arrangement(template, text, effects)
    groups = prepared['lyrics_adaptation']['note_groups']['lead']
    assert [group['core'] for group in groups] == [2, 2]
    assert prepared['lyrics_adaptation']['requested_hold_ms'] == 2000
    lead = prepared['parts'][0]['notes']
    held = lead[groups[0]['core']:groups[0]['core'] + groups[0]['hold']]
    assert abs(sum(note['duration_ms'] for note in held) - 2000) <= 39
    assert template == original

    plan = automatic_render_plan(prepared, text, effects, first_request_ms=1000, request_ms=2000)
    renderer = _renderer(voice)
    index = SimpleNamespace(resolve=lambda candidates: SimpleNamespace(alias=candidates[0])
                            if candidates else None)
    total_notes = 0
    for chunk in plan['chunks']:
        for request in chunk['parts']:
            moras, allocation = renderer._singing_lyrics(
                request['adaptive_text'], request['score'], index, 'pt-BR',
                request['adaptive_segments'])
            nuclei = [mora for mora in moras if mora.role == 'nucleus']
            assert len(nuclei) == len(allocation)
            assert sum(map(len, allocation)) == len(request['score']['notes'])
            total_notes += sum(map(len, allocation))
    assert total_notes == len(lead)
