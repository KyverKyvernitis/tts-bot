"""Selected CDs reach the live multipart queue without requiring '~'."""
from __future__ import annotations

import contextlib
import copy
import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

try:
    from test_cd_arrangement_audio_contract import (QueueItem, production_nodes, arrangement, Probe)
except ModuleNotFoundError:
    from tests.test_cd_arrangement_audio_contract import (QueueItem, production_nodes, arrangement, Probe)
from cogs.tts.ttv import resolve_preferences
from cogs.tts.performance import parse_performance_text, performance_hash
from cogs.tts.arrangement_audio import execution_arrangement
from cogs.tts.melodies import DEFAULT_MELODY_ID, fit_catalog_lyrics_to_score, fit_arrangement_lyrics
from osaka_cd.arrangement import arrangement_sha256
from osaka_cd.lyrics import prepare_message_arrangement


class MessageHarmonyModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop=asyncio.get_running_loop()
        def tick():
            if not loop.is_closed():
                loop.call_later(.01,tick)
        loop.call_later(.01,tick)

    async def prepare(self, text, *, selected=True, forced='teto', engine='teto'):
        value=arrangement()
        value['parts'][0]['lyrics_source']='vowel'
        self.template=copy.deepcopy(value)
        self.lookup=Mock(return_value=value)
        score={'version':1,'track_index':0,'duration_ms':value['duration_ms'],
               'lyrics_source':'message','notes':value['parts'][0]['notes']}
        namespace=dict(globals(),config=SimpleNamespace(),logger=logging.getLogger(__name__),
                       discord=SimpleNamespace(HTTPException=type('HTTPException',(Exception,),{})),
                       pin_snapshot=contextlib.nullcontext,
                       score_for_melody=lambda _:score,
                       arrangement_for_melody=self.lookup,
                       _has_speakable_tts_text=lambda text:bool(text))
        names={'_WithoutAttachments','_WithoutUserMidiAttachments','_reply_ttv_error',
               '_render_effect_segments','PayloadTTSMensagem','preparar_payload_tts_mensagem'}
        functions=production_nodes('cogs/tts/mensagens/preparacao.py',names,namespace)
        settings={'engine':engine,'voice':'kasane-teto','language':'pt-BR',
                  'ttv_voice_id':'kasane-teto','ttv_language':'pt-BR',
                  'ttv_melody_id':'test-song' if selected else DEFAULT_MELODY_ID}
        class Owner:
            def _get_db(self):return SimpleNamespace(resolve_tts=lambda *_:settings)
            async def _maybe_await(self,value):return value
            def _render_tts_text(self,message,text):return text
            def _guild_announce_author_enabled(self,defaults):return False
            def _apply_author_prefix_if_needed(self,*args,**kwargs):return args[2]
        message=SimpleNamespace(content="'"+text,guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=2,voice=SimpleNamespace(channel=SimpleNamespace(id=3))),attachments=[])
        result=await functions['preparar_payload_tts_mensagem'](Owner(),message,
            guild_defaults={},active_prefix="'",forced_engine=forced)
        self.assertEqual(value,self.template)
        return result.queue_item

    async def test_plain_selected_cd_message_keeps_both_harmonic_parts(self):
        item=await self.prepare('olá mundo')
        self.assertEqual(len(item.ttv_arrangement['parts']),2)
        self.assertEqual(item.ttv_arrangement['message_lyrics_mode'],'automatic-lyrics-v2')
        self.lookup.assert_called_once_with('test-song',preview=True)
        self.assertEqual(item.text,'olá mundo')

    async def test_saved_teto_voice_without_forced_prefix_still_uses_selected_cd(self):
        item=await self.prepare('amor',forced=None)
        self.assertEqual(len(item.ttv_arrangement['parts']),2)

    async def test_plain_default_voice_message_preserves_speech_mode(self):
        item=await self.prepare('olá mundo',selected=False)
        self.assertIsNone(item.ttv_arrangement)
        self.assertIsNone(item.ttv_score)
        self.lookup.assert_not_called()

    async def test_other_engine_does_not_turn_a_saved_cd_into_teto(self):
        item=await self.prepare('olá mundo',forced='edge')
        self.assertEqual(item.engine,'edge')
        self.assertIsNone(item.ttv_arrangement)
        self.lookup.assert_not_called()

    async def test_explicit_singing_on_default_cd_is_automatically_adapted(self):
        item=await self.prepare('/cantar porra ~ ~ ~ ~',selected=False)
        self.assertIsNotNone(item.ttv_arrangement)
        self.assertEqual(item.ttv_arrangement['message_lyrics_mode'],'automatic-lyrics-v2')

    async def test_plain_selected_message_generates_verified_full_live_mix(self):
        item=await self.prepare('olá mundo')
        with tempfile.TemporaryDirectory() as directory:
            from unittest.mock import patch
            import os
            with patch.dict(os.environ,{'OSAKA_CD_DATA_ROOT':directory}):
                owner=Probe(directory)
                path=await owner._generate_arrangement_worker_file(item)
            self.assertTrue(Path(path).is_file())
            self.assertEqual(len(owner.calls),2)
            self.assertEqual(item._tts_agent_render_metadata['parts'],2)
            self.assertTrue(all(child.ttv_arrangement is None for child in owner.calls))
            self.assertEqual({child.ttv_score['lyrics_source'] for child in owner.calls},{'message','midi'})


if __name__=='__main__':
    unittest.main()
