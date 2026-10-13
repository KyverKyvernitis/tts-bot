"""Actual 1.11.41 aligner and bot production methods; no bank/Discord API mock claim."""
from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest

from cogs.tts.melodies import (
    fit_catalog_lyrics_to_score, list_melodies, minimum_singing_syllables, score_for_melody,
)
from cogs.tts.singing_errors import (
    PhoneWorkerResponseError, request_scoped_worker_rejection, singing_failure_code,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/termux/phone-worker'))
from teto_renderer.phonemizer import Mora, phonemize
from teto_renderer.prosody import align_singing_syllables, validate_performance


def production_method(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text())
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    node.returns = None
    for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        argument.annotation = None
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


class LyricsContractTests(unittest.TestCase):
    def test_only_preflighted_vowels_have_exact_one_nucleus_in_both_real_phonemizers(self):
        for language in ('pt-BR','en-US'):
            for vowel in 'a á à â ã e é ê i í o ó ô õ u ú'.split():
                moras=phonemize(vowel,max_moras=1025,resolve_alias=lambda aliases:None,
                    voicebank_profile='english-cvvc',reading_language=language)
                self.assertEqual(sum(mora.role=='nucleus' for mora in moras),1)

    def test_real_catalog_short_two_syllables_reproduces_seven_song_failures(self):
        expected = {'world-is-mine':4, 'popipo':18, 'mesmerizer':5, 'ochame-kinou':4,
                    'override':7, 'teto-territory':4, 'triple-baka':4}
        failures = {}
        for melody in list_melodies():
            score = score_for_melody(melody.id)
            validate_performance('a a', score=score)
            before = json.dumps(score, sort_keys=True)
            nuclei = [Mora(('a',), role='nucleus', word_index=n) for n in range(2)]
            try:
                align_singing_syllables(nuclei, score)
            except ValueError:
                failures[melody.id] = minimum_singing_syllables(score)
                with self.assertRaisesRegex(ValueError, f'pelo menos {failures[melody.id]} sílabas'):
                    fit_catalog_lyrics_to_score('a a', None, score)
            else:
                self.assertEqual(fit_catalog_lyrics_to_score('a a', None, score), ('a a', None))
            self.assertEqual(json.dumps(score, sort_keys=True), before)
        self.assertEqual(failures, expected)

    def test_actual_aligner_accepts_minimum_syllables_for_every_real_catalog_score(self):
        for melody in list_melodies():
            score = score_for_melody(melody.id)
            count = minimum_singing_syllables(score)
            nuclei = [Mora(('a',), role='nucleus', word_index=n) for n in range(count)]
            groups = align_singing_syllables(nuclei, score)
            self.assertEqual([n for group in groups for n in group], list(range(melody.note_count)))

    def test_minimum_also_respects_maximum_vowel_span_and_pitch_range(self):
        music = {'notes':[{'midi_note':60, 'start_ms':n*10000, 'duration_ms':10000}
                          for n in range(3)]}
        self.assertEqual(minimum_singing_syllables(music),3)
        music['notes'] = [{'midi_note':p, 'start_ms':n*500, 'duration_ms':500}
                          for n,p in enumerate((36,60,96))]
        self.assertEqual(minimum_singing_syllables(music),2)

    def test_real_phonemizer_retains_ownership_of_arbitrary_words(self):
        score=score_for_melody('popipo')
        self.assertEqual(fit_catalog_lyrics_to_score('Olá meu amigo',None,score),('Olá meu amigo',None))
        self.assertEqual(fit_catalog_lyrics_to_score('mãe',None,score),('mãe',None))

    def test_public_lyrics_notice_uses_safe_score_count_without_worker_text(self):
        notice=production_method('cogs/tts/failure_notices.py','_public_notice',{
            '_MESSAGES':{'ttv_singing_lyrics_mismatch':'fallback'},
            'minimum_singing_syllables':minimum_singing_syllables})
        output=notice('ttv_singing_lyrics_mismatch',score=score_for_melody('popipo'))
        self.assertIn('18 sílabas',output)
        self.assertNotIn('voz está desativada',output)


class FailureClassificationTests(unittest.TestCase):
    def test_confirmed_worker_errors_have_precise_fixed_categories(self):
        cases = {
            'ValueError: a letra não encaixa nas frases do MIDI; acrescente sílabas nas pausas': 'ttv_singing_lyrics_mismatch',
            'ValueError: letra contém 20 sílabas para 16 notas; encurte a letra': 'ttv_singing_lyrics_too_long',
            'ValueError: MIDI exige uma melodia sem notas sobrepostas e notas de pelo menos 40 ms': 'ttv_singing_score_invalid',
            'TetoResourceError: recursos insuficientes para Teto': 'ttv_singing_busy',
            'ValueError: canto MIDI exige ESPER; o motor WORLDLINE ainda não aceita partituras': 'ttv_singing_backend_unsupported',
            'TetoConfigurationError: Inglês exige a voicebank English-CVVC da Teto instalada':'ttv_singing_bank_missing',
        }
        for message, code in cases.items():
            failure=PhoneWorkerResponseError(message,status=502)
            self.assertEqual(singing_failure_code(failure),code)
            self.assertTrue(request_scoped_worker_rejection(failure))
            self.assertEqual(failure.status,502)
        self.assertEqual(singing_failure_code(TimeoutError()),'ttv_singing_timeout')

    def test_transport_auth_and_unknown_server_errors_still_count(self):
        message='ValueError: a letra não encaixa nas frases do MIDI'
        self.assertFalse(request_scoped_worker_rejection(ConnectionError(message)))
        self.assertFalse(request_scoped_worker_rejection(PhoneWorkerResponseError(message,status=401)))
        self.assertFalse(request_scoped_worker_rejection(PhoneWorkerResponseError('unrecognized failure',status=502)))
        self.assertFalse(request_scoped_worker_rejection(TimeoutError('timeout')))

    def test_many_request_rejections_do_not_disable_whole_worker(self):
        mark=production_method('cogs/tts/audio.py','_mark_tts_agent_synth_failure',{
            'request_scoped_worker_rejection':request_scoped_worker_rejection,
            'TTS_WORKER_AGENT_FAILURE_THRESHOLD':3,'TTS_WORKER_AGENT_FAILURE_COOLDOWN_SECONDS':45,
            'time':time,'logger':logging.getLogger('contract')})
        state={'route':'worker','ok':True,'failure_count':0,'disabled_until_monotonic':0}
        metrics={}
        probe=SimpleNamespace(_tts_agent_route_state=lambda:state,_get_metrics_store=lambda:metrics)
        for _ in range(20):
            mark(probe,PhoneWorkerResponseError('ValueError: a letra não encaixa nas frases do MIDI',status=502))
        self.assertEqual(state,{'route':'worker','ok':True,'failure_count':0,'disabled_until_monotonic':0})
        self.assertEqual(metrics['tts_agent_request_rejected'],20)
        for _ in range(3):mark(probe,TimeoutError('worker transport timeout'))
        self.assertFalse(state['ok'])
        self.assertEqual(state['failure_count'],3)
        self.assertGreater(state['disabled_until_monotonic'],time.monotonic())


class Response:
    def __init__(self,body,status=502):
        self.body=body
        self.status=status
        self.headers={'Content-Type':'application/json'}
    async def text(self):return self.body
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass


class WorkerTransportTests(unittest.IsolatedAsyncioTestCase):
    def probe(self,response):
        async def get_session():return SimpleNamespace(post=lambda *args,**kwargs:response)
        return SimpleNamespace(_phone_worker_tts_base_url=lambda:'http://isolated-worker',
            _get_phone_worker_http_session=get_session,_normalize_worker_audio_format=lambda value:value or 'mp3')

    async def test_both_real_transport_parsers_keep_json_rejection_status_and_reason(self):
        import aiohttp
        namespace={'time':time,'aiohttp':aiohttp,'json':json,'asyncio':asyncio,
                   'PHONE_WORKER_TOKEN':'unused-fixture-token','PhoneWorkerResponseError':PhoneWorkerResponseError}
        for name in ('_request_phone_worker_json','_request_phone_worker_raw_audio'):
            request=production_method('cogs/tts/audio.py',name,namespace)
            message='ValueError: a letra não encaixa nas frases do MIDI'
            response=Response(json.dumps({'ok':False,'error':message}))
            kwargs={'payload':{},'timeout_seconds':1,'max_audio_mb':8}
            if name=='_request_phone_worker_json':kwargs['task']='tts_agent_synthesize'
            with self.assertRaises(PhoneWorkerResponseError) as captured:
                await request(self.probe(response),**kwargs)
            self.assertEqual(captured.exception.status,502)
            self.assertEqual(str(captured.exception),message)
            self.assertTrue(request_scoped_worker_rejection(captured.exception))

    async def test_malformed_raw_json_is_not_confirmed_lyric_rejection(self):
        import aiohttp
        request=production_method('cogs/tts/audio.py','_request_phone_worker_raw_audio',{
            'time':time,'aiohttp':aiohttp,'json':json,'asyncio':asyncio,
            'PHONE_WORKER_TOKEN':'unused-fixture-token','PhoneWorkerResponseError':PhoneWorkerResponseError})
        with self.assertRaises(RuntimeError) as captured:
            await request(self.probe(Response('ValueError: a letra não encaixa nas frases do MIDI')),
                          payload={},timeout_seconds=1,max_audio_mb=8)
        self.assertFalse(request_scoped_worker_rejection(captured.exception))


if __name__=='__main__':unittest.main()
