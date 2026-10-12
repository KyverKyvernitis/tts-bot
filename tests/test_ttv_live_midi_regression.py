"""Exercise Discord's actual text renderer before sending a singing request.

The existing performance tests replace ``_render_tts_text`` with an identity
function. This regression follows the real message gate, renderer and queue
dispatcher for the exact five-note demo that was sent in Discord.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest

from test_tts_helpers import tts_audio
from test_tts_message_flow import FakeCog, FakeDB, make_message
from test_ttv_audio import Probe

from cogs.tts.cog import TTSVoice
from cogs.tts.mensagens.despacho import despachar_mensagem_tts
from cogs.tts.mensagens.triagem import analisar_mensagem_para_tts
from cogs.tts.performance import performance_hash


# The immutable delivered ttv-09-melodia.mid: four connected notes followed
# by a half-second rest and another note. Keeping bytes here lets this test
# run on the VPS without requiring workspace-only sample files.
DEMO_MIDI = bytes.fromhex(
    "4d546864000000060000000101e04d54726b00000043"
    "00ff03064d656c6f647900ff510307a120"
    "00903c5a8740803c00"
    "0090405a8740804000"
    "0090435a8740804300"
    "0090485a8740804800"
    "836090435a8740804300"
    "00ff2f00"
)


class LiveRenderCog(FakeCog):
    """Keep DB/Discord offline, but use the exact production text renderer."""

    _render_tts_text = TTSVoice._render_tts_text
    _tts_user_reference = TTSVoice._tts_user_reference
    _tts_role_reference = TTSVoice._tts_role_reference
    _tts_channel_reference = TTSVoice._tts_channel_reference
    _tts_link_reference = TTSVoice._tts_link_reference


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", ["kasane-teto", "hatsune-miku"])
@pytest.mark.parametrize("language", ["pt-BR", "en-US"])
async def test_real_discord_render_keeps_demo_holds_and_exact_worker_identity(voice, language):
    assert len(DEMO_MIDI) == 89
    cog = LiveRenderCog(db=FakeDB(resolved={
        "ttv_voice_id": voice, "ttv_language": language,
        "ttv_pitch_semitones": "-2.0", "ttv_speech_rate": 1.25,
    }))
    msg = make_message("'a ~ ~ ~ a")
    msg.attachments = [SimpleNamespace(
        filename="ttv-09-melodia.mid", size=len(DEMO_MIDI),
        read=AsyncMock(return_value=DEMO_MIDI),
    )]
    msg.reply = AsyncMock()
    gate = await analisar_mensagem_para_tts(cog, msg)
    assert gate.should_process_tts and gate.forced_engine == "teto"
    result = await despachar_mensagem_tts(
        cog, msg, guild_defaults=gate.guild_defaults,
        active_prefix=gate.active_prefix, forced_engine=gate.forced_engine,
    )
    assert result.enqueued and len(cog.enqueue_calls) == 1
    queued = cog.enqueue_calls[0][1]
    assert queued.text == "a ~ ~ ~ a"
    assert [segment["hold"] for segment in queued.ttv_segments] == [False, True, True, True, False]
    assert [(note["midi_note"], note["start_ms"], note["duration_ms"])
            for note in queued.ttv_score["notes"]] == [
        (60, 0, 1000), (64, 1000, 1000), (67, 2000, 1000),
        (72, 3000, 1000), (67, 4500, 1000),
    ]
    assert queued.ttv_voice_id == voice and queued.ttv_language == language
    assert queued.ttv_pitch_semitones == "+0.0" and queued.ttv_speech_rate == 1.0
    expected = performance_hash(text=queued.text, score=queued.ttv_score, segments=queued.ttv_segments)
    probe = Probe()
    payload = probe._tts_agent_payload_for_item(queued)
    assert payload["text"] == "a ~ ~ ~ a"
    assert payload["ttv_performance_hash"] == expected
    assert payload["ttv_score"] == queued.ttv_score and payload["ttv_segments"] == queued.ttv_segments
    assert payload["ttv_voice_id"] == voice and payload["ttv_language"] == language
    msg.attachments[0].read.assert_awaited_once()
    msg.reply.assert_not_awaited()
