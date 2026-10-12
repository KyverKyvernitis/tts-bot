"""Regressions for VPS TTS when the optional Android worker is offline.

Extract the original methods by AST to avoid requiring Discord or speech
providers in this test process; no network/audio device is used.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def extract_audio(*names):
    tree = ast.parse((ROOT / 'cogs/tts/audio.py').read_text(encoding='utf-8'))
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'TTSAudioMixin')
    body = [n for n in owner.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in body} == set(names)
    mod = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *body], type_ignores=[])
    namespace = {
        'asyncio': asyncio, 'contextlib': contextlib, 'time': time, 'os': os,
        'logger': logging.getLogger(__name__), '_tts_safe_exception_detail': lambda e: str(e),
    }
    exec(compile(ast.fix_missing_locations(mod), '<extracted-audio>', 'exec'), namespace)
    return type('AudioMethods', (), {name: namespace[name] for name in names})


Methods = extract_audio('_play_file_with_recovery', '_tts_local_complete_file_after_stream_failure')


class LocalRecoveryProbe(Methods):
    def __init__(self, root):
        self.root = root
        self.guild_states = {}
        self.calls = []
        self.failed_after_frame = False
        self.music_active = False
        self.generate_calls = []
        self.edge_error = False

    def _edge_stream_handle_for_path(self, path):
        if path == 'tts-stream.fifo':
            return SimpleNamespace(engine='edge')
        return None

    async def _play_file(self, vc, path, *, item=None, prepared=None):
        self.calls.append(path)
        if path == 'tts-stream.fifo':
            error = RuntimeError('decoder ended before frame')
            error._tts_first_frame_observed = self.failed_after_frame
            raise error
        return {'ok': True, 'source': path}

    async def _generate_edge_file(self, *args, **kwargs):
        self.generate_calls.append('edge')
        if self.edge_error:
            raise TimeoutError('edge offline')
        path = os.path.join(self.root, 'edge-recovery.mp3')
        Path(path).write_bytes(b'mp3')
        return path

    async def _generate_gtts_file_with_priority(self, *args, **kwargs):
        self.generate_calls.append('gtts')
        path = os.path.join(self.root, 'gtts-recovery.mp3')
        Path(path).write_bytes(b'mp3')
        return path

    def _edge_circuit_is_open(self):
        return False

    def _is_music_active_for_guild(self, guild_id):
        return self.music_active


class ProgressiveFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.probe = LocalRecoveryProbe(self.temp.name)
        self.item = SimpleNamespace(guild_id=12, request_id='test', engine='edge', text='fala', voice='pt-BR', rate='+0%', pitch='+0Hz', language='pt')
        self.guild = SimpleNamespace(id=12)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_fifo_fails_before_any_pcm_retries_as_local_full_file_and_cleans_up(self):
        result = await self.probe._play_file_with_recovery(self.guild, self.item, object(), 'tts-stream.fifo')
        self.assertTrue(result['ok'])
        self.assertEqual(self.probe.calls, ['tts-stream.fifo', os.path.join(self.temp.name, 'edge-recovery.mp3')])
        self.assertFalse(Path(result['source']).exists())

    async def test_never_replays_audible_fragment(self):
        self.probe.failed_after_frame = True
        with self.assertRaisesRegex(RuntimeError, 'decoder ended'):
            await self.probe._play_file_with_recovery(self.guild, self.item, object(), 'tts-stream.fifo')
        self.assertEqual(self.probe.generate_calls, [])

    async def test_preserves_active_music(self):
        self.probe.music_active = True
        with self.assertRaises(RuntimeError):
            await self.probe._play_file_with_recovery(self.guild, self.item, object(), 'tts-stream.fifo')
        self.assertEqual(self.probe.generate_calls, [])

    async def test_confirmed_offline_worker_ignores_stale_music_flag(self):
        self.probe.music_active = True
        self.item._tts_music_worker_unavailable = True
        result = await self.probe._play_file_with_recovery(self.guild, self.item, object(), 'tts-stream.fifo')
        self.assertTrue(result['ok'])

    async def test_edge_failure_uses_local_gtts_without_worker(self):
        self.probe.edge_error = True
        result = await self.probe._play_file_with_recovery(self.guild, self.item, object(), 'tts-stream.fifo')
        self.assertTrue(result['ok'])
        self.assertEqual(self.probe.generate_calls, ['edge', 'gtts'])
        self.assertEqual(self.item._tts_actual_engine, 'gtts')


class MusicOfflineTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_unavailable_is_fallback_even_if_music_state_is_stale(self):
        from cogs.musica.integracoes.tts import erro_agente_permite_fallback_local, preparar_cliente_voz_tts
        from cogs.musica.agente_telefone.modelos import MusicWorkerUnavailable

        router = SimpleNamespace(
            is_music_active=lambda _guild: True,
            should_block_tts_local_voice=lambda _guild: True,
        )
        bot = SimpleNamespace(audio_router=router)
        self.assertFalse(erro_agente_permite_fallback_local(bot, 12, RuntimeError('random failure')))
        self.assertTrue(erro_agente_permite_fallback_local(bot, 12, MusicWorkerUnavailable('Nenhum worker online')))

        owner = SimpleNamespace(bot=bot)
        guild = SimpleNamespace(id=12)
        item = SimpleNamespace(channel_id=30, _tts_music_worker_unavailable=True)
        vc = SimpleNamespace(channel=SimpleNamespace(id=30))
        handled, local_vc = await preparar_cliente_voz_tts(owner, guild, item, SimpleNamespace(), vc)
        self.assertFalse(handled)
        self.assertIs(local_vc, vc)


class OfflineRouterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_music_worker_failure_releases_item_to_local_tts(self):
        from unittest.mock import patch
        from cogs.musica.integracoes import tts as integration
        from cogs.musica.agente_telefone.modelos import MusicWorkerUnavailable

        async def unavailable(*args, **kwargs):
            raise MusicWorkerUnavailable('Sistema de música indisponível: Nenhum worker online')

        owner = SimpleNamespace(
            bot=SimpleNamespace(audio_router=None),
            _estimate_playback_timeout=lambda item: 20.0,
        )
        guild = SimpleNamespace(id=12)
        item = SimpleNamespace(
            channel_id=30, text='olá', engine='edge', _tts_remote_active=False,
            voice='pt-BR', language='pt-br', rate='+0%', pitch='+0Hz',
        )
        with patch.object(integration, 'deve_rotear_tts_para_agente', return_value=True), \
             patch.object(integration, 'musica_ativa', return_value=True), \
             patch.object(integration, 'suporta_cache_tts_agente', return_value=False), \
             patch.object(integration, 'tocar_tts_via_agente', unavailable):
            handled, next_task = await integration.rotear_item_tts_para_musica(owner, guild, item, None)
        self.assertFalse(handled)
        self.assertIsNone(next_task)
        self.assertTrue(item._skip_music_agent_tts_route)
        self.assertTrue(item._tts_music_worker_unavailable)


class LoggingSafetyTests(unittest.TestCase):
    def test_error_summary_censors_url_and_credentials(self):
        text = (ROOT / 'cogs/tts/audio.py').read_text(encoding='utf-8')
        tree = ast.parse(text)
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_tts_safe_exception_detail')
        exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), fn], type_ignores=[])), '<safe>', 'exec'), ns := {'re': re})
        detail = ns['_tts_safe_exception_detail'](RuntimeError('HTTP https://example.com/secret?key=abc Bearer=abcdefghi'))
        self.assertNotIn('example.com', detail)
        self.assertNotIn('abcdefghi', detail)
        self.assertIn('RuntimeError', detail)


if __name__ == '__main__':
    unittest.main()
