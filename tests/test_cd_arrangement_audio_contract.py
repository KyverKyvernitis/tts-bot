"""Live orchestration contract with real PCM/FFmpeg; no Discord/UTAU login."""
from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
from dataclasses import dataclass, field
import hashlib
import io
import json
import math
import logging
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
from typing import Any, Awaitable, Callable, Optional
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import wave

from cogs.tts.arrangement_audio import (
    ArrangementAudioMixin, arrangement_cache_identity, arrangement_profile_identity,
    execution_arrangement, frozen_arrangement,
)
from cogs.tts.ttv import resolve_preferences, worker_voice_details
from cogs.tts.performance import performance_hash, parse_performance_text, singing_request
from cogs.tts.melodies import DEFAULT_MELODY_ID, fit_arrangement_lyrics, fit_catalog_lyrics_to_score
from osaka_cd.lyrics import prepare_message_arrangement
from osaka_cd.arrangement import arrangement_sha256
from cogs.musica.runtime_telefone.agente.efeitos import (
    TTS_EFFECT_ENGINES, TTS_NIGHTCORE_MULTIPLIERS, TTS_SLOWED_MULTIPLIERS,
    normalizar_efeitos_tts, payload_efeitos_tts,
)

ROOT = Path(__file__).resolve().parents[1]
namespace = dict(globals(), TTS_TETO_DEFAULT_PITCH_SEMITONES=0, DEFAULT_VOICE_ID='kasane-teto', DEFAULT_LANGUAGE='pt-BR')
tree = ast.parse((ROOT / 'cogs/tts/audio.py').read_text())
queue_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'QueueItem')
exec(compile(ast.Module(body=[queue_node], type_ignores=[]), 'cogs/tts/audio.py', 'exec'), namespace)
QueueItem = namespace['QueueItem']


def production_nodes(path, names, namespace):
    """Execute real functions without pretending the Discord SDK is installed."""
    source = ast.parse((ROOT / path).read_text())
    nodes = [node for node in ast.walk(source)
             if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in names]
    module = ast.Module(body=[ast.ImportFrom(module='__future__',
        names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), path, 'exec'), namespace)
    return {name: namespace[name] for name in names}


contract_namespace = dict(globals(),
    TTS_TETO_DEFAULT_PITCH_SEMITONES=0, DEFAULT_VOICE_ID='kasane-teto', DEFAULT_LANGUAGE='pt-BR',
    DEFAULT_MELODY_ID='ttv-demo-v1', TTS_PLAYBACK_TIMEOUT_BASE_SECONDS=12.,
    TTS_PLAYBACK_TIMEOUT_MAX_SECONDS=120., TTS_PLAYBACK_TIMEOUT_PER_CHAR_SECONDS=.08,
    WORKER_VOICE_AGENT_DIRECT_TTS_PREBUILD_MAX_MB=8,
    WORKER_VOICE_AGENT_ENABLED=True, WORKER_VOICE_AGENT_DIRECT_TTS_ENABLED=True,
    WORKER_VOICE_AGENT_DIRECT_TTS_AUTO_ENABLED=True, WORKER_VOICE_AGENT_DIRECT_TTS_MAX_CHARS=1600,
    logger=logging.getLogger(__name__), __package__='cogs.tts')
contract_methods = production_nodes('cogs/tts/audio.py', {
    '_ttv_item_preferences', '_ttv_performance_payload', 'TTVSingingError',
    '_snapshot_tts_item', '_cache_key', '_get_item_normalized_cache_text', '_normalize_cache_text',
    '_estimate_playback_timeout', '_maybe_attach_prebuilt_direct_tts_audio',
    '_worker_voice_direct_tts_available_for', '_cd_preview_mp3', '_generate_tts_agent_worker_file',
}, contract_namespace)


def arrangement():
    return {'score_schema': 2, 'duration_ms': 500, 'parts': [
        {'id': name, 'role': role, 'voice_id': 'kasane-teto', 'lyrics_source': 'vowel',
         'notes': [{'midi_note': pitch, 'start_ms': 0, 'duration_ms': 500, 'velocity': 100}]}
        for name, role, pitch in [('lead', 'lead', 60), ('harmony-1', 'harmony', 64)]], 'warnings': []}


class Probe(ArrangementAudioMixin):
    def __init__(self, directory):
        self.directory, self.calls, self.metrics = Path(directory), [], {}
        self.block = False
        self.started, self.release = threading.Event(), threading.Event()

    def _tts_agent_route_state(self):
        return {'teto_fingerprint': 'actual-bank-resampler-fixture', 'ttv_voices': [
            {'id': 'kasane-teto', 'fingerprint': 'bank-v1', 'ready': True,
             'available': True, 'languages': ['pt-BR']} ]}

    def _get_metrics_store(self):
        return self.metrics

    def _make_runtime_temp_file(self, suffix):
        handle, name = tempfile.mkstemp(suffix=suffix, dir=self.directory)
        os.close(handle)
        return name

    async def _generate_tts_agent_worker_file(self, item):
        self.calls.append(item)
        if self.block:
            self.started.set()
            await asyncio.to_thread(self.release.wait, 5)
        score = item.ttv_score
        rate = 48000
        pitch = score['notes'][0]['midi_note']
        frequency = 440 * 2 ** ((pitch - 69) / 12)
        raw = io.BytesIO()
        with wave.open(raw, 'wb') as output:
            output.setparams((1, 2, rate, 0, 'NONE', ''))
            output.writeframes(b''.join(struct.pack('<h', round(6000 * math.sin(2 * math.pi * frequency * n / rate)))
                                      for n in range(round(score['duration_ms'] * rate / 1000))))
        content = raw.getvalue()
        item._tts_agent_render_metadata = {'midi_preroll_ms': 0, 'sha256': hashlib.sha256(content).hexdigest()}
        path = self._make_runtime_temp_file('.wav')
        Path(path).write_bytes(content)
        return path


def queued():
    return QueueItem(1, 2, 3, 'a', 'teto', 'kasane-teto', 'pt-BR', '1.0', '+0Hz',
                     ttv_arrangement=execution_arrangement(arrangement(), 'kasane-teto'))


class ArrangementFreezeTests(unittest.TestCase):
    def test_execution_is_independent_and_cantar_uses_message_for_lead(self):
        source = arrangement()
        result = execution_arrangement(source, 'kasane-teto')
        self.assertEqual(source['parts'][0]['lyrics_source'], 'vowel')
        self.assertEqual(result['parts'][0]['lyrics_source'], 'message')
        self.assertEqual(result['parts'][1]['lyrics_source'], 'vowel')

    def test_detects_mutation_and_cache_separates_harmony(self):
        first, second = queued(), queued()
        self.assertEqual(arrangement_cache_identity(first), arrangement_cache_identity(second))
        second.ttv_arrangement['parts'][1]['notes'][0]['midi_note'] = 67
        with self.assertRaisesRegex(ValueError, 'mudou'):
            frozen_arrangement(second)
        second.ttv_arrangement_hash = ''
        self.assertNotEqual(arrangement_cache_identity(first), arrangement_cache_identity(second))

    def test_profile_requires_real_identity_for_every_bank(self):
        item = queued()
        item.ttv_arrangement['parts'][1]['voice_id'] = 'hatsune-miku'
        route = {'ttv_voices': [
            {'id': 'kasane-teto', 'fingerprint': 'teto-bank'},
            {'id': 'hatsune-miku', 'fingerprint': 'miku-bank'},
        ]}
        first = arrangement_profile_identity(item, route)
        route['ttv_voices'][1]['fingerprint'] = 'new-miku-bank'
        self.assertNotEqual(first, arrangement_profile_identity(item, route))
        route['ttv_voices'].pop()
        self.assertEqual(arrangement_profile_identity(item, route), '')

    def test_all_cached_profiles_bind_harmony_and_unknown_profile_cannot_reuse(self):
        probe = ContractProbe('.')
        first, second = queued(), queued()
        self.assertEqual(probe._cache_key(first), probe._cache_key(second))
        first.ttv_arrangement['parts'][1]['notes'][0]['midi_note'] = 67
        with self.assertRaisesRegex(ValueError, 'mudou'):
            probe._cache_key(first)
        probe.route = {}
        self.assertNotEqual(probe._cache_key(queued()), probe._cache_key(queued()))

    def test_full_arrangement_timeout_and_old_gateway_limit(self):
        probe = ContractProbe('.')
        item = queued()
        item.ttv_arrangement['duration_ms'] = 188663
        self.assertGreater(probe._estimate_playback_timeout(item), 188.663)
        allowed, reason = probe._worker_voice_direct_tts_available_for(SimpleNamespace(id=1), item)
        self.assertFalse(allowed)
        self.assertEqual(reason, 'direct_tts_arrangement_exceeds_gateway_window')


class LiveCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # This restricted sandbox can suppress the loop's socket wakeup from
        # worker threads. Timers keep genuine executor completions observable,
        # including unittest's executor shutdown; no renderer is replaced.
        loop = asyncio.get_running_loop()
        def tick():
            if not loop.is_closed():
                loop.call_later(.01, tick)
        loop.call_later(.01, tick)

    async def test_all_parts_use_existing_score_v1_and_final_mix_is_decodable(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'OSAKA_CD_DATA_ROOT': directory}):
            probe, item = Probe(directory), queued()
            path = await probe._generate_arrangement_worker_file(item)
            self.assertEqual(len(probe.calls), 2)
            for child in probe.calls:
                self.assertEqual(child.ttv_score['version'], 1)
                self.assertIsNone(child.ttv_arrangement)
                self.assertEqual(child.ttv_speech_rate, 1.0)
                self.assertEqual(child.ttv_pitch_semitones, '+0.0')
                self.assertEqual(child.ttv_language, 'pt-BR')
            self.assertTrue(Path(path).stat().st_size > 256)
            import subprocess
            check = subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', path, '-f', 'null', '-'],
                                   capture_output=True, timeout=10)
            self.assertEqual(check.returncode, 0)
            self.assertEqual(list(Path(directory).glob('*.wav')), [])
            os.unlink(path)
            path2 = await probe._generate_arrangement_worker_file(queued())
            self.assertEqual(len(probe.calls), 2, 'same-profile stems must reuse prior physical renders')
            self.assertTrue(probe.metrics['cd_arrangement_last_mix_cache_hit'])
            os.unlink(path2)


class ContractProbe(Probe):
    """Real pipeline methods with only external transport/settings substituted."""
    def __init__(self, directory):
        super().__init__(directory)
        self.route = super()._tts_agent_route_state()
        self.direct_cache = None
        self.direct_error = None

    def _tts_agent_route_state(self):
        return self.route

    def _get_state(self, guild_id):
        return {}

    def _try_get_cached_path(self, state, item):
        return self.direct_cache

    def _path_audio_format(self, path):
        return Path(path).suffix.lstrip('.')

    async def _resolve_or_generate_singleflight_audio(self, state, item, **kwargs):
        if self.direct_error:
            raise self.direct_error
        return await self._generate_arrangement_worker_file(item), True

    async def _generate_tts_agent_worker_file(self, item):
        if item.ttv_arrangement is None:
            return await super()._generate_tts_agent_worker_file(item)
        return await contract_methods['_generate_tts_agent_worker_file'](self, item)

    def _ttv_voice_status(self, voice_id):
        return {'online': True, 'ready': True, 'singing_available': True, 'languages': ['pt-BR']}


for name, function in contract_methods.items():
    if name.startswith('_') and name not in {'_ttv_item_preferences', '_ttv_performance_payload',
                                             '_generate_tts_agent_worker_file'}:
        setattr(ContractProbe, name, function)


class ProductionFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = LiveCompositionTests.asyncSetUp

    async def test_prebuilt_mix_has_full_identity_and_cannot_send_partial_lead(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'OSAKA_CD_DATA_ROOT': directory}):
            probe, item = ContractProbe(directory), queued()
            item.ttv_score = {'version': 1, 'track_index': 0, 'duration_ms': 500,
                              'lyrics_source': 'message', 'notes': arrangement()['parts'][0]['notes']}
            payload = contract_methods['_ttv_performance_payload'](item)
            await probe._maybe_attach_prebuilt_direct_tts_audio(payload, item, generate_if_missing=True)
            self.assertNotIn('ttv_score', payload)
            self.assertNotIn('ttv_performance_hash', payload)
            audio = base64.b64decode(payload['audio_b64'], validate=True)
            self.assertEqual(payload['cd_arrangement_sha256'], item.ttv_arrangement_hash)
            self.assertEqual(payload['cd_arrangement_audio_sha256'], hashlib.sha256(audio).hexdigest())
            self.assertEqual(len(probe.calls), 2)
            self.assertEqual(list(Path(directory).glob('*.mp3')), [])

    async def test_failed_or_oversized_composition_cannot_fall_back_to_score_v1(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = ContractProbe(directory)
            probe.direct_error = RuntimeError('physical singing rejected')
            with self.assertRaisesRegex(RuntimeError, 'physical singing rejected'):
                await probe._maybe_attach_prebuilt_direct_tts_audio({}, queued(), generate_if_missing=True)
            path = Path(directory) / 'oversized.mp3'
            with path.open('wb') as handle:
                handle.truncate(8 * 1024 * 1024 + 1)
            probe.direct_cache = str(path)
            with self.assertRaisesRegex(RuntimeError, 'limite'):
                await probe._maybe_attach_prebuilt_direct_tts_audio({}, queued())

    async def test_private_preview_uses_joint_excerpt_and_keeps_stereo(self):
        def selected(melody_id, *, preview=False):
            self.assertTrue(preview)
            result = arrangement()
            result['parts'][0]['lyrics_source'] = 'vowel'
            return result
        score = {'version': 1, 'track_index': 0, 'duration_ms': 500,
                 'lyrics_source': 'message', 'notes': arrangement()['parts'][0]['notes']}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'OSAKA_CD_DATA_ROOT': directory}), \
                patch.dict(contract_namespace, {'score_for_melody': lambda _: score,
                    'preview_text_for_melody': lambda *_: 'a', 'arrangement_for_melody': selected}):
            probe = ContractProbe(directory)
            audio = await probe._cd_preview_mp3(1, 2, melody_id='poly-song',
                                               settings={'ttv_voice_id': 'kasane-teto', 'ttv_language': 'pt-BR'})
            self.assertEqual(len(probe.calls), 2)
            import subprocess
            report = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', 'pipe:0'],
                                    input=audio, capture_output=True, timeout=10)
            self.assertEqual(report.returncode, 0)
            self.assertEqual(json.loads(report.stdout)['streams'][0]['channels'], 2)
            self.assertEqual(list(Path(directory).glob('*.mp3')), [])

    async def test_real_message_preparation_freezes_complete_arrangement_and_preferences(self):
        full = arrangement()
        full['duration_ms'] = 3000
        for part in full['parts']:
            part['notes'] = [dict(part['notes'][0], duration_ms=1500),
                             dict(part['notes'][0], start_ms=1500, duration_ms=1500)]
        score = {'version': 1, 'track_index': 0, 'duration_ms': 500,
                 'lyrics_source': 'message', 'notes': arrangement()['parts'][0]['notes']}
        ns = dict(globals(), config=SimpleNamespace(), re=__import__('re'),
                  logger=logging.getLogger(__name__), _has_speakable_tts_text=lambda text: bool(text),
                  discord=SimpleNamespace(HTTPException=type('HTTPException', (Exception,), {})),
                  pin_snapshot=contextlib.nullcontext, score_for_melody=lambda _: score,
                  arrangement_for_melody=lambda _, **kwargs: full)
        names = {'_WithoutAttachments', '_WithoutUserMidiAttachments', '_reply_ttv_error',
                 '_render_effect_segments', 'PayloadTTSMensagem', 'preparar_payload_tts_mensagem'}
        preparation = production_nodes('cogs/tts/mensagens/preparacao.py', names, ns)
        settings = {'engine': 'teto', 'ttv_voice_id': 'kasane-teto', 'ttv_language': 'pt-BR',
                    'ttv_pitch_semitones': '+2.0', 'ttv_speech_rate': 1.2, 'ttv_melody_id': 'poly-song'}
        class MessageOwner:
            def _get_db(self): return SimpleNamespace(resolve_tts=lambda *_: settings)
            async def _maybe_await(self, value): return value
            def _render_tts_text(self, message, text): return text
        message = SimpleNamespace(content="'/cantar olá mundo ~", guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=2, voice=SimpleNamespace(channel=SimpleNamespace(id=3))), attachments=[])
        result = await preparation['preparar_payload_tts_mensagem'](
            MessageOwner(), message, guild_defaults={}, active_prefix="'", forced_engine='teto')
        self.assertEqual(result.queue_item.ttv_arrangement['duration_ms'], 7000)
        self.assertEqual(result.queue_item.ttv_arrangement['lyrics_adaptation']['requested_hold_ms'], 1000)
        self.assertEqual(len(result.queue_item.ttv_arrangement['parts']), 2)
        self.assertEqual(result.queue_item.ttv_score['duration_ms'], 500)
        self.assertEqual(result.queue_item.ttv_pitch_semitones, '+0.0')
        self.assertEqual(result.queue_item.ttv_speech_rate, 1.0)
        self.assertEqual(full['parts'][0]['lyrics_source'], 'vowel')
        self.assertEqual(result.queue_item.ttv_arrangement['parts'][0]['lyrics_source'], 'message')
        self.assertEqual(result.queue_item.ttv_arrangement_hash,
                         arrangement_sha256(result.queue_item.ttv_arrangement))

    async def test_cancel_holds_admission_until_physical_request_finishes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'OSAKA_CD_DATA_ROOT': directory}):
            probe = Probe(directory)
            probe.block = True
            task = asyncio.create_task(probe._generate_arrangement_worker_file(queued()))
            for _ in range(200):
                if probe.started.is_set():
                    break
                await asyncio.sleep(.005)
            self.assertTrue(probe.started.is_set())
            task.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            self.assertTrue(probe._cd_composition_gate.locked())
            task.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done(), 'a second cancellation cannot release physical admission')
            probe.release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(probe._cd_composition_gate.locked())
            self.assertEqual(list(Path(directory).glob('*.wav')), [])


class MusicalPlaybackTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = LiveCompositionTests.asyncSetUp

    def integration(self, played):
        async def play(bot, *, guild_id, channel_id, text, cache_key='', audio_b64='',
                       audio_format='', prebuilt_audio=False, prebuilt_audio_source='',
                       tld='com', tts_request_id='', engine='', voice='', language='',
                       rate='', pitch='', advanced_nightcore_level=0,
                       advanced_slowed_level=0, advanced_reverb_level=0, timeout=None):
            # Match the actual router's accepted parameters. Musical score or
            # local verification kwargs must not escape into this old API.
            played.append({'audio_b64': audio_b64, 'prebuilt_audio': prebuilt_audio, 'timeout': timeout})
            return {'ok': True, 'playback_ms': 500}
        ns = dict(globals(), deve_rotear_tts_para_agente=lambda *_: True,
                  suporta_cache_tts_agente=lambda *_: True, tocar_tts_via_agente=play,
                  erro_agente_permite_fallback_local=lambda *_: False)
        functions = production_nodes('cogs/musica/integracoes/tts.py',
            {'_TTVPerformancePlaybackError', 'rotear_item_tts_para_musica'}, ns)
        return functions['rotear_item_tts_para_musica']

    def owner(self, directory):
        owner = ContractProbe(directory)
        owner.bot = object()
        owner._record_queue_timing = lambda **kwargs: None
        owner._schedule_worker_voice_agent_register_session = lambda *args, **kwargs: None
        return owner

    async def test_music_route_accepts_verified_full_mix_without_score_kwargs(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'OSAKA_CD_DATA_ROOT': directory}):
            played, owner, item = [], self.owner(directory), queued()
            consumed, audio_task = await self.integration(played)(owner, SimpleNamespace(id=1), item, None)
            self.assertTrue(consumed)
            self.assertIsNone(audio_task)
            self.assertEqual(len(played), 1)
            self.assertTrue(played[0]['prebuilt_audio'])
            self.assertEqual(len(owner.calls), 2)

    async def test_mismatched_full_mix_is_rejected_before_music_play(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'OSAKA_CD_DATA_ROOT': directory}):
            played, owner, item = [], self.owner(directory), queued()
            original = owner._maybe_attach_prebuilt_direct_tts_audio
            async def corrupt(payload, item, **kwargs):
                result = await original(payload, item, **kwargs)
                payload['cd_arrangement_audio_sha256'] = '0' * 64
                return result
            owner._maybe_attach_prebuilt_direct_tts_audio = corrupt
            with self.assertRaisesRegex(RuntimeError, 'preparar o áudio'):
                await self.integration(played)(owner, SimpleNamespace(id=1), item, None)
            self.assertEqual(played, [])

    async def test_long_remote_song_leaves_music_and_pending_audio_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            played, owner, item = [], self.owner(directory), queued()
            item.ttv_arrangement['duration_ms'] = 188663
            pending = asyncio.get_running_loop().create_future()
            try:
                with self.assertRaisesRegex(RuntimeError, '90 segundos'):
                    await self.integration(played)(owner, SimpleNamespace(id=1), item, pending)
                self.assertFalse(pending.cancelled())
                self.assertEqual(owner.calls, [])
                self.assertEqual(played, [])
            finally:
                pending.cancel()


if __name__ == '__main__':
    unittest.main()
