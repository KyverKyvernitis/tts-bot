"""Chronological complete mixes, typed RAM caches and preserved word anchors."""
from __future__ import annotations

import array
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import io
import math
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from osaka_cd.lyrics import automatic_render_plan, prepare_message_arrangement
from osaka_cd.progressive import DecodedPCMCache, DecodedStem, stream_arrangement
from osaka_cd.render import RenderError


def template(notes=24, note_ms=500):
    return dict(score_schema=2, duration_ms=notes*note_ms, parts=[
        dict(id=identity, role=role, voice_id='kasane-teto', lyrics_source=source,
             gain=1, pan=pan,
             notes=[dict(midi_note=pitch, start_ms=index*note_ms,
                         duration_ms=note_ms, velocity=100) for index in range(notes)])
        for identity, role, source, pitch, pan in (
            ('lead', 'lead', 'message', 69, -1), ('harmony', 'harmony', 'vowel', 76, 1))])


def renderer(calls, *, rate=48000, constant=False, fail_after=None):
    def render(**request):
        calls.append(copy.deepcopy(request))
        if fail_after is not None and len(calls) >= fail_after:
            raise RuntimeError('native_render_failed')
        lead = request['part_id'] == 'lead'
        preroll = 100 if lead else 200
        score = request['score']
        frames = round((score['duration_ms']+preroll)*rate/1000)
        samples = array.array('h', [0])*frames
        frequency = 440 if lead else 660
        for note in score['notes']:
            first = round((preroll+note['start_ms'])*rate/1000)
            last = min(frames, round((preroll+note['start_ms']+note['duration_ms'])*rate/1000))
            for number in range(first, last):
                samples[number] = 6000 if constant else round(6000*math.sin(2*math.pi*frequency*(number-first)/rate))
        if sys.byteorder != 'little':
            samples.byteswap()
        target = io.BytesIO()
        with wave_writer(target) as destination:
            destination.setparams((1, 2, rate, 0, 'NONE', ''))
            destination.writeframes(samples.tobytes())
        raw = target.getvalue()
        return raw, dict(midi_preroll_ms=preroll, sha256=hashlib.sha256(raw).hexdigest(),
                         resampler_calls=1, resample_ms=10, private_command='never-copy')
    return render


def wave_writer(target):
    import wave
    return wave.open(target, 'wb')


def normalized_renderer(calls, *, varying=False, gains=(1.2,1.0)):
    base = renderer(calls, constant=True)
    lead_number = 0
    def render(**request):
        nonlocal lead_number
        import wave
        raw, metadata = base(**request)
        gain = 1.0
        if varying and request['part_id'] == 'lead':
            gain = gains[lead_number % len(gains)]
            lead_number += 1
        with wave.open(io.BytesIO(raw), 'rb') as source:
            params = source.getparams()
            values = array.array('h', source.readframes(source.getnframes()))
        if sys.byteorder != 'little':
            values.byteswap()
        for number, value in enumerate(values):
            values[number] = int(value * gain)
        if sys.byteorder != 'little':
            values.byteswap()
        target = io.BytesIO()
        with wave.open(target, 'wb') as output:
            output.setparams(params)
            output.writeframes(values.tobytes())
        raw = target.getvalue()
        metadata.update(normalization_gain=gain, sha256=hashlib.sha256(raw).hexdigest())
        return raw, metadata
    return render


class ProgressiveRenderTests(unittest.TestCase):
    def test_first_pcm_precedes_later_lyrics_and_contains_all_simultaneous_voices(self):
        text = ' '.join(['a']*24)
        adapted = prepare_message_arrangement(template(), text)
        calls, chunks, snapshots = [], [], []
        def emit(raw):
            chunks.append(raw)
            snapshots.append(len(calls))
        result = stream_arrangement(adapted, text, renderer(calls), emit)
        self.assertLess(snapshots[0], len(calls))
        self.assertEqual({call['part_id'] for call in calls[:snapshots[0]]}, {'lead', 'harmony'})
        self.assertLessEqual(calls[0]['score']['duration_ms'], 3000)
        self.assertEqual(' '.join(call['text'] for call in calls if call['part_id']=='lead'), text)
        raw = b''.join(chunks)
        self.assertEqual(len(raw), round(12350*48)*4)
        self.assertEqual(result['audio_bytes'], len(raw))
        self.assertEqual(result['pcm_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertTrue(all(len(chunk)%4==0 and len(chunk)<=96000 for chunk in chunks))
        samples = array.array('h', raw)
        if sys.byteorder != 'little':
            samples.byteswap()
        preroll_frames = round(350*48)
        self.assertFalse(any(samples[:preroll_frames*2]))
        def energy(channel, frequency):
            values = samples[preroll_frames*2+channel:preroll_frames*2+4800*2:2]
            return abs(sum(value*complex(math.cos(2*math.pi*frequency*n/48000),
                                          math.sin(2*math.pi*frequency*n/48000))
                           for n,value in enumerate(values)))
        self.assertGreater(energy(0,440), energy(0,660)*20)
        self.assertGreater(energy(1,660), energy(1,440)*20)
        self.assertAlmostEqual(result['first_nonzero_offset_ms'],350,delta=.05)

    def test_pcm_cache_replay_is_identical_and_voice_profile_isolated(self):
        text = 'a a a a a a a a'
        adapted = prepare_message_arrangement(template(),text)
        cache = DecodedPCMCache(4*1024*1024)
        calls, first, second = [], [], []
        initial = stream_arrangement(adapted,text,renderer(calls),first.append,
            render_profile_fingerprint='bank-v1',memory_cache=cache)
        original_calls=len(calls)
        repeat=stream_arrangement(adapted,text,renderer(calls),second.append,
            render_profile_fingerprint='bank-v1',memory_cache=cache)
        self.assertEqual(len(calls),original_calls)
        self.assertEqual(b''.join(first),b''.join(second))
        self.assertEqual(initial['pcm_sha256'],repeat['pcm_sha256'])
        self.assertTrue(all(stem['cache_kind']=='memory' for stem in repeat['stems']))
        self.assertTrue(all(not stem['render_metrics'] for stem in repeat['stems']))
        self.assertNotIn('private_command',str(initial))
        stream_arrangement(adapted,text,renderer(calls),lambda raw:None,
            render_profile_fingerprint='bank-v2',memory_cache=cache)
        self.assertGreater(len(calls),original_calls)

    def test_existing_disk_guide_cache_reused_before_decoded_cache_exists(self):
        from osaka_cd.render import warm_arrangement_stems
        value=template(notes=4)
        text='a a a a'
        adapted=prepare_message_arrangement(value,text)
        with tempfile.TemporaryDirectory() as temporary:
            calls=[]
            warm_arrangement_stems(value,renderer(calls),cache_dir=temporary,
                render_profile_fingerprint='bank')
            calls.clear()
            result=stream_arrangement(adapted,text,renderer(calls),lambda raw:None,
                render_profile_fingerprint='bank',cache_dir=temporary)
            self.assertEqual([call['role'] for call in calls],['lead'])
            harmony=next(stem for stem in result['stems'] if stem['part_id']=='harmony')
            self.assertEqual(harmony['cache_kind'],'disk')

    def test_fixed_sustain_is_not_cut_to_first_window_target(self):
        value=template(notes=1,note_ms=8000)
        adapted=prepare_message_arrangement(value,'a')
        calls=[]
        stream_arrangement(adapted,'a',renderer(calls),lambda raw:None)
        lead=[call for call in calls if call['part_id']=='lead']
        self.assertEqual(len(lead),1)
        self.assertEqual(lead[0]['score']['duration_ms'],8000)

    def test_holds_stay_on_original_word_and_default_plan_is_unchanged(self):
        text='a ~ ~ ~ a a a a'
        adapted=prepare_message_arrangement(template(),text)
        default=automatic_render_plan(adapted,text)
        explicit=automatic_render_plan(adapted,text,first_request_ms=None,request_ms=None)
        self.assertEqual(default,explicit)
        short=automatic_render_plan(adapted,text,first_request_ms=500,request_ms=1000)
        lead=[request for chunk in short['chunks'] for request in chunk['parts'] if request['role']=='lead']
        self.assertEqual(lead[0]['adaptive_text'],'a ~ ~ ~ ~ ~ ~')
        self.assertEqual(lead[0]['score']['duration_ms'],3500)
        self.assertTrue(all(not request['adaptive_text'].startswith('~') for request in lead))

    def test_cancellation_and_native_failure_do_not_emit_incomplete_initial_mix(self):
        text='a a a a'
        adapted=prepare_message_arrangement(template(),text)
        chunks=[]
        with self.assertRaisesRegex(RuntimeError,'native_render_failed'):
            stream_arrangement(adapted,text,renderer([],fail_after=2),chunks.append)
        self.assertFalse(chunks)
        calls=[]
        with self.assertRaisesRegex(RenderError,'cancelled'):
            stream_arrangement(adapted,text,renderer(calls),chunks.append,cancelled=lambda:True)
        self.assertFalse(calls)
        self.assertFalse(chunks)

    def test_consumer_cancellation_stops_before_future_physical_calls(self):
        text=' '.join(['a']*24)
        adapted=prepare_message_arrangement(template(),text)
        calls=[]
        class ConsumerStopped(Exception):
            pass
        def emit(raw):
            raise ConsumerStopped()
        with self.assertRaises(ConsumerStopped):
            stream_arrangement(adapted,text,renderer(calls),emit)
        self.assertEqual(len(calls),2)

    def test_offsets_final_duration_and_sample_conversion_are_preserved(self):
        value=template(notes=2,note_ms=500)
        for part in value['parts']:
            part['notes'][0]['start_ms']=100
            part['notes'][0]['duration_ms']=400
        adapted=prepare_message_arrangement(value,'a a')
        chunks=[]
        result=stream_arrangement(adapted,'a a',renderer([],rate=44100),chunks.append)
        raw=b''.join(chunks)
        self.assertEqual(len(raw),round(result['duration_ms']*48)*4)
        # Bandlimited 44.1->48 kHz conversion can ring immediately before the
        # attack. A 2 ms guard distinguishes that from a shifted musical note.
        self.assertFalse(any(raw[:round(448*48)*4]))

    def test_request_peak_gain_is_undone_without_volume_step_or_agc(self):
        text=' '.join(['a']*24)
        adapted=prepare_message_arrangement(template(),text)
        steady=[]
        reference=stream_arrangement(adapted,text,normalized_renderer([]),steady.append,
                                     require_normalization=True)
        for gains in ((1.2,1.0),(.6,1.2)):
            with self.subTest(gains=gains):
                varying=[]
                result=stream_arrangement(adapted,text,
                    normalized_renderer([],varying=True,gains=gains),varying.append,
                    require_normalization=True)
                self.assertEqual(b''.join(steady),b''.join(varying))
                self.assertEqual(reference['pcm_sha256'],result['pcm_sha256'])
                self.assertTrue(set(gains).issubset({stem['normalization_gain'] for stem in result['stems']}))

    def test_required_normalization_regenerates_old_disk_and_decoded_entries(self):
        from osaka_cd.render import warm_arrangement_stems
        value=template(notes=4)
        text='a a a a'
        adapted=prepare_message_arrangement(value,text)
        cache=DecodedPCMCache(4*1024*1024)
        with tempfile.TemporaryDirectory() as directory:
            warm_arrangement_stems(value,renderer([]),cache_dir=directory,
                                    render_profile_fingerprint='bank')
            stream_arrangement(adapted,text,renderer([]),lambda raw:None,
                render_profile_fingerprint='bank',memory_cache=cache,cache_dir=directory)
            calls=[]
            result=stream_arrangement(adapted,text,normalized_renderer(calls),lambda raw:None,
                render_profile_fingerprint='bank',memory_cache=cache,cache_dir=directory,
                require_normalization=True)
        self.assertEqual({call['role'] for call in calls},{'lead','harmony'})
        self.assertTrue(all(stem['normalization_gain']==1 for stem in result['stems']))

    def test_old_physical_worker_without_normalization_metadata_emits_nothing(self):
        text='a a a a'
        adapted=prepare_message_arrangement(template(),text)
        chunks=[]
        with self.assertRaisesRegex(RenderError,'normalization_metadata_missing'):
            stream_arrangement(adapted,text,renderer([]),chunks.append,
                               require_normalization=True)
        self.assertFalse(chunks)

    def test_invalid_normalization_gain_is_refused_before_first_pcm(self):
        text='a a'
        adapted=prepare_message_arrangement(template(),text)
        for invalid in (True,0,-1,.59,1.21,float('inf'),float('nan'),'1.0'):
            with self.subTest(kind=type(invalid).__name__):
                chunks=[]
                base=renderer([])
                def render(**request):
                    raw,metadata=base(**request)
                    metadata['normalization_gain']=invalid
                    return raw,metadata
                with self.assertRaisesRegex(RenderError,'normalization_metadata_missing'):
                    stream_arrangement(adapted,text,render,chunks.append,
                                       require_normalization=True)
                self.assertFalse(chunks)


class DecodedCacheTests(unittest.TestCase):
    def test_lru_and_concurrent_publication_keep_budget_and_immutable_samples(self):
        cache=DecodedPCMCache(100)
        def put(number):
            value=DecodedStem(bytes([number])*60,0,str(number))
            cache.put(str(number),value)
            return value
        with ThreadPoolExecutor(max_workers=4) as pool:
            values=list(pool.map(put,range(20)))
        self.assertLessEqual(cache.size_bytes,100)
        cache.put('a',values[0])
        self.assertIs(cache.get('a'),values[0])
        cache.put('b',values[1])
        self.assertIsNone(cache.get('a'))
        cache.clear()
        self.assertEqual(cache.size_bytes,0)


if __name__=='__main__':
    unittest.main()
