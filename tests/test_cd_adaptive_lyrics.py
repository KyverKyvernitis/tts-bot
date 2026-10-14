"""Automatic CD lyrics checked against the unchanged 1.11.41 aligner."""
from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import importlib.util
import io
import math
import random
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from osaka_cd.lyrics import prepare_message_arrangement, automatic_render_plan, _phonemes
from osaka_cd.render import render_arrangement
from cogs.tts.performance import parse_performance_text
from cogs.tts.arrangement_audio import arrangement_cache_identity
from cogs.tts.melodies import score_for_melody, list_melodies


def template(rests=False, duration=1000, parts=3):
    notes=[{'midi_note':60+number,'start_ms':number*(duration+(500 if rests else 0)),
            'duration_ms':duration,'velocity':90} for number in range(4)]
    length=notes[-1]['start_ms']+duration
    return {'score_schema':2,'duration_ms':length,'parts':[
        {'id':role,'role':role,'voice_id':'kasane-teto',
         'lyrics_source':'message' if role=='lead' else 'vowel',
         'notes':[dict(note,midi_note=note['midi_note']+interval) for note in notes]}
        for role,interval in [('lead',0),('harmony',7),('bass',-12)][:parts]]}


def worker_aligner():
    path=ROOT/'deploy/termux/phone-worker/teto_renderer/prosody.py'
    if not path.is_file():
        raise unittest.SkipTest('Original worker aligner not bundled in this package')
    tree=ast.parse(path.read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='align_singing_syllables')
    ns={'Mora':object}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
    return ns['align_singing_syllables']


def worker_validate(text, score, segments):
    path=ROOT/'deploy/termux/phone-worker/teto_renderer/prosody.py'
    tree=ast.parse(path.read_text())
    names={'is_default_score','validate_performance'}
    nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    ns={'math':math,'DEFAULT_MELODY_PRESET':'ttv-demo-v1'}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),ns)
    ns['validate_performance'](text,score=score,segments=segments)


def assert_worker_accepts(test, request, align, language='pt-BR'):
    score=request['score']
    test.assertLessEqual(len(score['notes']),128)
    test.assertLessEqual(score['duration_ms'],60000)
    test.assertTrue(all(note['duration_ms']>=40 for note in score['notes']))
    if score['lyrics_source']!='message':
        worker_validate('a',score,None)
        return
    worker_validate(request['adaptive_text'],score,request.get('adaptive_segments',request.get('segments')))
    nuclei=[]
    slots=[]
    moras=0
    for token in request['adaptive_text'].split():
        if token=='~':
            slots.append(None)
        else:
            phonemes=_phonemes(token,language)
            moras+=len(phonemes)
            emitted=[mora for mora in phonemes if mora.role=='nucleus']
            slots.extend(range(len(nuclei),len(nuclei)+len(emitted)))
            nuclei.extend(emitted)
    test.assertEqual(len(slots),len(score['notes']))
    test.assertLessEqual(moras,1024)
    test.assertEqual(sum(len(group) for group in align(nuclei,score,slots=slots)),len(score['notes']))


class AdaptiveLyricsTests(unittest.TestCase):
    def setUp(self):
        self.align=worker_aligner()

    def plan(self,text,value=None,segments=None,language='pt-BR'):
        value=value or template()
        before=copy.deepcopy(value)
        prepared=prepare_message_arrangement(value,text,segments,language=language)
        result=automatic_render_plan(prepared,text,segments,language=language)
        self.assertEqual(value,before,'Published musical template must remain immutable')
        for chunk in result['chunks']:
            for request in chunk['parts']:
                assert_worker_accepts(self,request,self.align,language)
        return prepared,result

    def test_short_word_stops_after_its_syllables_without_filling_later_rests(self):
        for text in ('a','porra','amanheceu'):
            with self.subTest(text=text):
                prepared,plan=self.plan(text,template(rests=True))
                self.assertEqual(len(prepared['parts']),3)
                words=[word for chunk in plan['chunks'] for request in chunk['parts']
                       if request['role']=='lead' for word in request['adaptive_text'].split() if word!='~']
                self.assertEqual(words.count(text),1)

    def test_screenshot_long_sentence_keeps_all_words_in_order(self):
        text='eu vou dormir agora amor e te ligo mais tarde pra você fazer um sistema para eu te mandar'
        prepared,plan=self.plan(text,template(parts=1))
        words=[word for chunk in plan['chunks'] for request in chunk['parts']
               for word in request.get('adaptive_text','').split() if word!='~']
        self.assertEqual(words,text.split())
        self.assertGreater(prepared['lyrics_adaptation']['nuclei'],4)

    def test_long_lyrics_repeat_all_voices_and_do_not_truncate_words(self):
        text=' '.join(['amor']*60)
        prepared,plan=self.plan(text)
        self.assertGreater(prepared['lyrics_adaptation']['motif_repeats'],1)
        self.assertEqual({request['role'] for chunk in plan['chunks'] for request in chunk['parts']},
                         {'lead','harmony','bass'})
        words=[word for chunk in plan['chunks'] for request in chunk['parts']
               if request['role']=='lead' for word in request['adaptive_text'].split() if word!='~']
        self.assertEqual(words.count('amor'),60)

    def test_holds_and_fades_are_adapted_without_losing_words(self):
        text,segments=parse_performance_text('<porra ~ ~ ~ ~ amor>',singing=True)
        prepared,plan=self.plan(text,template(parts=1),segments)
        emitted=[segment for chunk in plan['chunks'] for request in chunk['parts']
                 for segment in request.get('adaptive_segments',[])]
        self.assertTrue(any(segment['fade_in'] for segment in emitted))
        self.assertTrue(any(segment['fade_out'] for segment in emitted))
        self.assertEqual([segment['text'] for segment in emitted if segment['text']!='~'],['porra','amor'])

    def test_tiny_motif_repeats_without_changing_note_duration_or_tempo(self):
        prepared,plan=self.plan('constitucionalmente',template(duration=40,parts=1))
        self.assertEqual(prepared['lyrics_adaptation']['tempo_stretch'],1)
        self.assertGreater(prepared['lyrics_adaptation']['motif_repeats'],1)
        self.assertTrue(all(note['duration_ms']==40 for note in prepared['parts'][0]['notes']))
        self.plan('constitucionalmente',template(rests=True,duration=40,parts=1))

    def test_dense_and_long_pitch_spans_respect_worker_boundaries(self):
        value=template(parts=1)
        value['duration_ms']=20000
        value['parts'][0]['notes']=[{'midi_note':60,'start_ms':i*50,'duration_ms':50,'velocity':90}
                                    for i in range(400)]
        self.plan('a',value)
        self.plan('mãe',{'score_schema':2,'duration_ms':40000,'parts':[
            {'id':'lead','role':'lead','voice_id':'kasane-teto','lyrics_source':'message',
             'notes':[{'midi_note':60,'start_ms':0,'duration_ms':40000,'velocity':90}]}]})

    def test_english_reading_and_physical_character_caps(self):
        self.plan('hello beautiful world',template(rests=True),language='en-US')
        self.plan('I will sleep now and call you later my love',language='en-US')
        self.plan('a'+'-'*2000,template(duration=4000,parts=1))
        with self.assertRaisesRegex(ValueError,'capacidade fonética'):
            prepare_message_arrangement(template(),'strstrstrstrstra'*128)

    def test_segments_must_match_text_and_literal_markers_are_not_holds(self):
        text,segments=parse_performance_text('amor ~',singing=True)
        with self.assertRaisesRegex(ValueError,'text_mismatch'):
            prepare_message_arrangement(template(),'outro ~',segments)
        for invalid in ({},[],[{'text':'amor'}]):
            with self.assertRaisesRegex(ValueError,'invalid_composition_segments'):
                prepare_message_arrangement(template(),'amor',invalid)
        text,segments=parse_performance_text('amor \\~',singing=True)
        prepared,_=self.plan(text,segments=segments)
        self.assertEqual(prepared['lyrics_adaptation']['words'],1)

    def test_different_rests_pitch_ranges_and_word_lengths_obey_physical_contract(self):
        rng=random.Random(904)
        for case in range(30):
            notes=[]
            start=0
            for _ in range(rng.randint(1,35)):
                length=rng.choice([40,80,120,300,1000])
                notes.append({'midi_note':rng.randint(36,96),'start_ms':start,
                              'duration_ms':length,'velocity':90})
                start+=length+rng.choice([0,1,21,180])
            value={'score_schema':2,'duration_ms':start,'parts':[
                {'id':'lead','role':'lead','voice_id':'kasane-teto','lyrics_source':'message','notes':notes}]}
            text=' '.join(rng.choices(['a','porra','constitucionalmente','amor'],k=rng.randint(1,40)))
            with self.subTest(case=case):
                self.plan(text,value)

    def test_all_builtin_cds_accept_one_short_word_and_a_long_sentence(self):
        for melody in list_melodies():
            score=score_for_melody(melody.id)
            value={'score_schema':2,'duration_ms':score['duration_ms'],'parts':[
                {'id':'lead','role':'lead','voice_id':'kasane-teto','lyrics_source':'message',
                 'notes':score['notes']}]}
            for text in ('porra','eu vou dormir agora amor e te ligo mais tarde pra você fazer um sistema para eu te mandar'):
                with self.subTest(cd=melody.id,text=text):
                    self.plan(text,value)

    def test_automatic_and_fixed_execution_cannot_share_message_audio_cache(self):
        value=template()
        fixed=SimpleNamespace(ttv_arrangement=value,ttv_arrangement_hash='',engine='teto',text='a',ttv_segments=None)
        auto=SimpleNamespace(ttv_arrangement=prepare_message_arrangement(value,'a'),
                             ttv_arrangement_hash='',engine='teto',text='a',ttv_segments=None)
        self.assertNotEqual(arrangement_cache_identity(fixed),arrangement_cache_identity(auto))

    def test_vendored_phonetics_are_byte_identical_to_installed_worker_sources(self):
        for name in ('phonemizer.py','ptbr_g2p.py'):
            original=ROOT/'deploy/termux/phone-worker/teto_renderer'/name
            if not original.exists():
                self.skipTest('Original worker source not present in this package')
            self.assertEqual((ROOT/'osaka_cd/phonetics'/name).read_bytes(),original.read_bytes())


@unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
class AdaptiveAudioTests(unittest.TestCase):
    def test_real_pcm_contains_all_three_harmonic_parts_and_decodes(self):
        try:
            from test_cd_multivoice_render import tone
        except ModuleNotFoundError:
            from tests.test_cd_multivoice_render import tone
        calls=[]
        align=worker_aligner()
        def renderer(**request):
            calls.append(request)
            adapted={**request,'adaptive_text':request['text']}
            assert_worker_accepts(self,adapted,align)
            frequency={'lead':440,'harmony':660,'bass':220}[request['role']]
            raw=tone(request['score'],frequency,100)
            return raw,{'midi_preroll_ms':100,'sha256':hashlib.sha256(raw).hexdigest()}
        value=prepare_message_arrangement(template(),'porra')
        with tempfile.TemporaryDirectory() as cache:
            result=render_arrangement(value,'porra',renderer,output_format='mp3',cache_dir=cache,
                                      render_profile_fingerprint='test-bank-identity')
            self.assertTrue(result['audio'])
            self.assertEqual(result['metadata']['parts'],3)
            self.assertEqual({request['role'] for request in calls},{'lead','harmony','bass'})
            decoded=subprocess.run(['ffmpeg','-v','error','-i','pipe:0','-ar','48000',
                '-ac','1','-f','s16le','pipe:1'],input=result['audio'],capture_output=True,check=True).stdout
            samples=struct.unpack('<'+'h'*(len(decoded)//2),decoded)
            window=samples[24000:28800]
            for frequency in (220,440,660):
                magnitude=math.hypot(sum(v*math.cos(2*math.pi*frequency*i/48000) for i,v in enumerate(window)),
                                     sum(v*math.sin(2*math.pi*frequency*i/48000) for i,v in enumerate(window)))/len(window)
                self.assertGreater(magnitude,900,'A mixed harmonic voice must be audible in decoded PCM')
            warm=render_arrangement(value,'porra',renderer,output_format='mp3',cache_dir=cache,
                                    render_profile_fingerprint='test-bank-identity')
            self.assertEqual(warm['audio'],result['audio'])
            self.assertTrue(warm['mix_cache_hit'])


if __name__=='__main__':
    unittest.main()
