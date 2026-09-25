"""Cenários reais de anexo de áudio e mensagem de voz do Discord."""
from __future__ import annotations

import asyncio
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cogs.musica.runtime_telefone.agente.estado import AgentTrack
from cogs.musica.runtime_telefone.agente.resolucao import ResolucaoMixin
from cogs.musica.runtime_telefone.agente import validade_stream as media
from cogs.tts.prefix import PrefixControlCommand, dispatch_prefix_control_command


ROOT = Path(__file__).resolve().parents[3]
REF = {"guild_id": 123, "channel_id": 456, "message_id": 789, "attachment_id": 10}
URL = "https://cdn.discordapp.com/attachments/456/10/voice-message.ogg?ex=ffffffff&hm=signature"


def seletor():
    name = "discord_audio_reply_selection_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "cogs/musica/nucleo/anexo_discord.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class AudioDiscordTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_p_reply_routes_voice_once_to_music(self):
        channel = types.SimpleNamespace(id=456)
        voice = types.SimpleNamespace(id=10, filename="voice-message.ogg", content_type="audio/ogg")
        original = types.SimpleNamespace(id=789, channel=channel, attachments=[voice])
        reply = types.SimpleNamespace(channel=channel,
                                      reference=types.SimpleNamespace(message_id=789, resolved=original))
        calls = []

        class Music:
            async def _run_play_discord_attachment(self, ctx, selected):
                calls.append(selected.status)

        class Bot:
            def get_cog(self, name):
                return Music() if name == "Music" else None

            async def get_context(self, message):
                return message

        class TTS:
            bot = Bot()

            async def _send_prefix_panel(self, *_args, **_kwargs):
                calls.append("tts_panel")
                return True

        routed = await dispatch_prefix_control_command(TTS(), reply, PrefixControlCommand("panel_user", "", "_p"))
        self.assertTrue(routed)
        self.assertEqual(calls, ["audio"])

    async def test_voice_reply_has_author_title_and_exact_reference(self):
        mod = seletor()
        voice = types.SimpleNamespace(id=10, filename="voice-message.ogg", content_type="audio/ogg",
                                      duration=5.0, title="voice-message", is_voice_message=lambda: True)
        channel = types.SimpleNamespace(id=456)
        original = types.SimpleNamespace(id=789, channel=channel, attachments=[voice],
                                         author=types.SimpleNamespace(display_name="Pessoa"))
        command = types.SimpleNamespace(guild=types.SimpleNamespace(id=123), channel=channel,
                                        reference=types.SimpleNamespace(message_id=789, resolved=original))
        selected = await mod.midia_respondida(command)
        self.assertEqual(selected.status, "audio")
        meta = mod.metadados_midia(selected, command)
        self.assertEqual(meta["title"], "Mensagem de voz de Pessoa")
        self.assertEqual(meta["attachment_ref"], REF)
        self.assertEqual(meta["attachment_duration_hint"], 5.0)
        voice.content_type = "application/octet-stream"
        self.assertEqual((await mod.midia_respondida(command)).status, "audio")
        original.attachments = [types.SimpleNamespace(id=11, filename="capa.png", content_type="image/png"), voice]
        self.assertEqual((await mod.midia_respondida(command)).status, "audio")

    async def test_api_checks_audio_identity_and_rejects_wrong_file(self):
        raw = {"guild_id": "123", "attachments": [{"id": "10", "filename": "voice-message.ogg",
               "content_type": "audio/ogg", "duration_secs": 5.0, "url": URL}]}
        client = types.SimpleNamespace(http=types.SimpleNamespace(get_message=AsyncMock(return_value=raw)))
        self.assertEqual((await media.fetch_discord_attachment(client, REF))["url"], URL)
        with self.assertRaises(media.DiscordAttachmentError):
            await media.fetch_discord_attachment(client, {**REF, "attachment_id": 11})
        raw["attachments"][0]["content_type"] = "image/png"
        with self.assertRaises(media.DiscordAttachmentError):
            await media.fetch_discord_attachment(client, REF)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg indisponível")
    async def test_ogg_opus_audio_probe_and_decode_using_confirmed_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample = str(Path(tmp) / "voice-message.ogg")
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=1.5", "-c:a", "libopus", "-y", sample], check=True)
            with patch.object(media, "valid_cdn_url", side_effect=lambda value: value):
                info = await media.probe_discord_audio(sample)
            self.assertEqual(info["audio_codec"], "opus")
            self.assertEqual(info["audio_stream_index"], 0)
            self.assertAlmostEqual(info["duration"], 1.5, delta=0.1)
            pcm = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", sample,
                                  "-map", f"0:{info['audio_stream_index']}", "-vn", "-f", "s16le",
                                  "-ac", "2", "-ar", "48000", "-"], capture_output=True, check=True)
            self.assertGreater(len(pcm.stdout), 100_000)

    async def test_first_play_reuses_probed_url_and_later_play_refreshes(self):
        class Resolver(ResolucaoMixin):
            _discord_verified_urls = {"discord-media:123:1000:10": (URL, time.monotonic() + 20)}
            client = None

            def _agent_track_from_metadata(self, metadata, *, body, fallback_query=""):
                return AgentTrack(title="Mensagem de voz", duration=5.0, audio_stream_index=0,
                                  queue_item_id=metadata["queue_item_id"], attachment_ref=REF)

        resolver = Resolver()
        meta = {"attachment_ref": REF, "queue_item_id": "discord-media:123:1000:10"}
        rest = AsyncMock(return_value={"url": URL})
        with patch("cogs.musica.runtime_telefone.agente.resolucao.fetch_discord_attachment", rest):
            first = await resolver._resolve_discord_attachment(track_meta=meta, body={"guild_id": 123})
            self.assertEqual(first.stream_url, URL)
            rest.assert_not_awaited()
            self.assertNotIn("stream_url", first.public())
            await resolver._resolve_discord_attachment(track_meta=meta, body={"guild_id": 123})
            rest.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
