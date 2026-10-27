"""Draw a CD once per real message preparation, before freezing its music."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from dataclasses import dataclass
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_cd_arrangement_audio_contract import QueueItem, production_nodes
from cogs.tts.arrangement_audio import execution_arrangement
from cogs.tts.melodies import fit_arrangement_lyrics, fit_catalog_lyrics_to_score
from cogs.tts.performance import parse_performance_text, performance_hash, singing_request
from cogs.tts.ttv import resolve_preferences
from osaka_cd.arrangement import arrangement_sha256
from osaka_cd.lyrics import automatic_render_plan, prepare_message_arrangement
import cogs.tts.random_cd as random_cd


def music(pitch):
    return {"score_schema": 2, "duration_ms": 2000, "parts": [{
        "id": "lead", "role": "lead", "voice_id": "kasane-teto",
        "lyrics_source": "vowel", "notes": [
            {"midi_note": pitch + number, "start_ms": number * 500,
             "duration_ms": 500, "velocity": 90}
            for number in range(4)
        ],
    }]}


class Owner:
    def __init__(self, values):
        self.values = copy.deepcopy(values)

    def _get_db(self):
        return SimpleNamespace(resolve_tts=lambda *_: copy.deepcopy(self.values))

    async def _maybe_await(self, value):
        return value

    def _render_tts_text(self, message, text):
        return text.strip()

    def _guild_announce_author_enabled(self, defaults):
        return False

    def _apply_author_prefix_if_needed(self, guild_id, author, text, *, enabled):
        return text


class RandomCDPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()

        def tick():
            if not loop.is_closed():
                loop.call_later(.01, tick)

        loop.call_later(.01, tick)

    def setUp(self):
        self.templates = {"song-a": music(60), "song-b": music(67)}
        self.scores = {key: {
            "version": 1, "track_index": 0, "lyrics_source": "message",
            "duration_ms": value["duration_ms"],
            "notes": copy.deepcopy(value["parts"][0]["notes"]),
        } for key, value in self.templates.items()}
        self.score_lookup = Mock(side_effect=lambda key: copy.deepcopy(self.scores[key]))
        self.arrangement_lookup = Mock(side_effect=lambda key, **_: copy.deepcopy(self.templates[key]))
        namespace = dict(globals(),
            config=SimpleNamespace(), logger=logging.getLogger(__name__),
            discord=SimpleNamespace(HTTPException=type("HTTPException", (Exception,), {})),
            pin_snapshot=contextlib.nullcontext,
            score_for_melody=self.score_lookup,
            arrangement_for_melody=self.arrangement_lookup,
            _has_speakable_tts_text=lambda text: any(char.isalnum() for char in text),
        )
        names = {"_WithoutAttachments", "_WithoutUserMidiAttachments", "_reply_ttv_error",
                 "_render_effect_segments", "PayloadTTSMensagem", "preparar_payload_tts_mensagem"}
        self.prepare = production_nodes("cogs/tts/mensagens/preparacao.py", names, namespace)[
            "preparar_payload_tts_mensagem"]
        self.values = {"engine": "teto", "language": "pt-BR",
                       "ttv_voice_id": "hatsune-miku", "ttv_language": "pt-BR",
                       "ttv_melody_id": "song-a", "ttv_random_cd": True}

    async def payload(self, owner, text):
        message = SimpleNamespace(content="'" + text, guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=2, voice=SimpleNamespace(channel=SimpleNamespace(id=3))),
            attachments=[])
        return await self.prepare(owner, message, guild_defaults={}, active_prefix="'",
                                  forced_engine="teto")

    async def test_each_new_singing_message_draws_once_and_freezes_its_choice(self):
        owner = Owner(self.values)
        before = copy.deepcopy(owner.values)
        pool = tuple(SimpleNamespace(id=value) for value in self.templates)
        with patch.object(random_cd, "available_random_melodies", return_value=pool), \
             patch.object(random_cd.random, "choice", side_effect=lambda candidates: candidates[0]) as draw:
            first = await self.payload(owner, "a~")
            second = await self.payload(owner, "a~")
            third = await self.payload(owner, "a~")
            self.assertEqual(draw.call_count, 3)
            self.assertEqual([call.args[0] for call in self.score_lookup.call_args_list],
                             ["song-a", "song-b", "song-a"])
            self.assertEqual(first.resolved["ttv_melody_id"], "song-a")
            self.assertEqual(second.resolved["ttv_melody_id"], "song-b")
            self.assertEqual(third.resolved["ttv_melody_id"], "song-a")
            for result, identity in ((first, "song-a"), (second, "song-b"), (third, "song-a")):
                queued = result.queue_item
                self.assertEqual(queued.ttv_voice_id, "hatsune-miku")
                self.assertEqual(queued.ttv_score, self.scores[identity])
                for _ in range(2):
                    plan = automatic_render_plan(queued.ttv_arrangement, queued.text,
                                                 queued.ttv_segments)
                    self.assertTrue(plan["chunks"])
                self.assertEqual(queued.ttv_arrangement_hash,
                                 arrangement_sha256(queued.ttv_arrangement))
            self.assertEqual(draw.call_count, 3, "Physical chunks must reuse their frozen CD")
        self.assertEqual(owner.values, before, "Shuffle must preserve the saved fixed CD")

    async def test_random_mode_does_not_turn_ordinary_speech_into_singing(self):
        with patch.object(random_cd, "choose_synthesis_melody") as choose:
            payload = await self.payload(Owner(self.values), "a~mor")
        choose.assert_not_called()
        self.assertIsNone(payload.queue_item.ttv_score)
        self.assertIsNone(payload.queue_item.ttv_arrangement)
        self.assertTrue(payload.queue_item.ttv_segments[0]["hold"])

    async def test_fixed_mode_uses_the_saved_cd_without_drawing(self):
        values = {**self.values, "ttv_random_cd": False}
        with patch.object(random_cd, "choose_synthesis_melody") as choose:
            payload = await self.payload(Owner(values), "a~")
        choose.assert_not_called()
        self.assertEqual(payload.queue_item.ttv_score, self.scores["song-a"])


if __name__ == "__main__":
    unittest.main()
