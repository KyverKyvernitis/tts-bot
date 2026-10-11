"""Testa os métodos reais do cog sem inicializar Discord, áudio ou rede."""
from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import unittest

ROOT = Path(__file__).resolve().parents[1]
# A suíte leve instala stubs para os módulos de áudio e Discord usados no gate.
from test_tts_message_flow import FakeCog, FakeDB, make_message
from cogs.tts.mensagens.triagem import analisar_mensagem_para_tts
from cogs.tts.prefix import build_prefix_routing_config, match_engine_prefix, has_speech_after_prefix
from cogs.tts.audio import QueueItem
from test_tts_notice_design import notices
import config


def extract_method(name, namespace):
    tree = ast.parse((ROOT / 'cogs/tts/cog.py').read_text())
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'TTSVoice')
    method = next(node for node in owner.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    ast.fix_missing_locations(method)
    compiled = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), method], type_ignores=[])
    exec(compile(ast.fix_missing_locations(compiled), str(ROOT/'cogs/tts/cog.py'), 'exec'), namespace)
    return namespace[name]


class NoticeGateIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _cog(self, *, content='!g oi', enabled=True, voice=True):
        db = FakeDB(guild_defaults={'gtts_prefix': '!g', 'edge_prefix': '!e', 'enabled': enabled})
        cog = FakeCog(db=db)
        cog.bot = SimpleNamespace(get_context=AsyncMock(return_value=SimpleNamespace(valid=False)))
        cog._record_tts_message_gate = Mock()
        cog._mark_tts_message_seen = Mock()
        cog._was_tts_message_seen = Mock(return_value=False)
        cog._notify_tts_failure = AsyncMock()
        cog._member_has_ignored_tts_role = Mock(return_value=False)
        cog._ensure_worker = Mock()
        cog._notify_tts_gate_failure = extract_method('_notify_tts_gate_failure', {
            'inspect': inspect, 'logger': logging.getLogger('test'), 'config': config,
            'build_prefix_routing_config': build_prefix_routing_config,
            'match_engine_prefix': match_engine_prefix, 'has_speech_after_prefix': has_speech_after_prefix,
            'QueueItem': QueueItem,
        }).__get__(cog)
        message = make_message(content, in_call=voice)
        message.id = 12345
        message.webhook_id = None
        message.guild.me = SimpleNamespace(voice=SimpleNamespace(channel=None, mute=False, suppress=False))
        return cog, message

    async def _process(self, cog, message):
        method = extract_method('_process_tts_message', {
            'time': time, 'logger': logging.getLogger('test'),
            'analisar_mensagem_para_tts': analisar_mensagem_para_tts,
            'discord': SimpleNamespace(Member=type('StubMember', (), {})),
            'diagnose_voice_access': notices.diagnose_voice_access,
            'dispatch_prefix_control_command': AsyncMock(),
            'despachar_mensagem_tts': AsyncMock(side_effect=AssertionError('não deve enfileirar')),
        })
        await method(cog, message)

    async def test_real_message_handler_ignores_empty_prefix_without_any_notice(self):
        for content in ('!g', '!g ', '!g?!'):
            cog, message = self._cog(content=content, voice=False)
            await self._process(cog, message)
            cog._notify_tts_failure.assert_not_awaited()
            cog._mark_tts_message_seen.assert_not_called()

    async def test_real_message_handler_notifies_outside_call(self):
        cog, message = self._cog(content='!g oi', voice=False)
        await self._process(cog, message)
        self.assertEqual(cog._notify_tts_failure.await_args.args[1], 'author_not_in_voice')

    async def test_real_message_handler_distinguishes_suppressed_and_muted(self):
        for flag, code in (('mute', 'bot_muted'), ('suppress', 'bot_suppressed')):
            cog, message = self._cog(content='!g oi', voice=True)
            message.guild.me.voice.channel = message.author.voice.channel
            setattr(message.guild.me.voice, flag, True)
            await self._process(cog, message)
            self.assertEqual(cog._notify_tts_failure.await_args.args[1], code)

    async def test_real_message_handler_detects_missing_voice_permission(self):
        cog, message = self._cog(content='!g oi', voice=True)
        voice_channel = message.author.voice.channel
        voice_channel.permissions_for = lambda member: SimpleNamespace(
            view_channel=True, connect=True, speak=False, move_members=False,
        )
        await self._process(cog, message)
        self.assertEqual(cog._notify_tts_failure.await_args.args[1], 'voice_permission_missing')

    async def test_prefix_only_does_not_respond_even_with_tts_disabled(self):
        for globally_disabled in (True, False):
            for content in ('!g', '!g    ', '!g!!!'):
                cog, message = self._cog(content=content, enabled=False, voice=False)
                with patch.object(config, 'TTS_ENABLED', not globally_disabled):
                    gate = await analisar_mensagem_para_tts(cog, message)
                    await cog._notify_tts_gate_failure(message, gate, gate.reason)
                cog._notify_tts_failure.assert_not_awaited()

    async def test_tts_disabled_still_notifies_when_real_text_exists(self):
        for global_disabled in (False, True):
            cog, message = self._cog(content='!g olá', enabled=False, voice=False)
            with patch.object(config, 'TTS_ENABLED', not global_disabled):
                gate = await analisar_mensagem_para_tts(cog, message)
                await cog._notify_tts_gate_failure(message, gate, gate.reason)
            cog._notify_tts_failure.assert_awaited_once()
            item, code = cog._notify_tts_failure.await_args.args
            self.assertEqual(item.message_id, message.id)
            self.assertEqual(code, 'tts_disabled' if global_disabled else 'tts_guild_disabled')

    async def test_missing_call_still_produces_notice(self):
        cog, message = self._cog(content='!g oi', voice=False)
        gate = await analisar_mensagem_para_tts(cog, message)
        await cog._notify_tts_gate_failure(message, gate, 'author_not_in_voice')
        cog._notify_tts_failure.assert_awaited_once()
        self.assertEqual(cog._notify_tts_failure.await_args.args[1], 'author_not_in_voice')

    async def test_legacy_prefix_outside_call_notifies(self):
        cog, message = self._cog(content='?oi', voice=False)
        cog._db.guild_defaults.update({'tts_prefix': '?'})
        gate = await analisar_mensagem_para_tts(cog, message)
        self.assertTrue(gate.should_process_tts)
        await cog._notify_tts_gate_failure(message, gate, 'author_not_in_voice')
        cog._notify_tts_failure.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
