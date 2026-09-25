"""Regressões de mídia e fila para `_play` em resposta a vídeo do Discord."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from cogs.musica.runtime_telefone.agente import validade_stream as media
from cogs.tts.prefix import PrefixControlCommand, dispatch_prefix_control_command


ROOT = Path(__file__).resolve().parents[3]
REF = {"guild_id": 123, "channel_id": 456, "message_id": 789, "attachment_id": 10}


def input_module():
    name = "video_reply_detection_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "cogs/musica/nucleo/anexo_discord.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class VideoMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_uses_exactly_one_video_and_preserves_real_file_name(self):
        mod = input_module()
        anexo = types.SimpleNamespace(id=10, filename="show_ao_vivo.mp4", content_type="video/mp4",
                                      title=None, duration=82, url="https://cdn.discordapp.com/attachments/456/10/show.mp4")
        author = types.SimpleNamespace(display_name="Pessoa real")
        original = types.SimpleNamespace(id=789, attachments=[anexo], author=author,
                                         channel=types.SimpleNamespace(id=456))
        command = types.SimpleNamespace(guild=types.SimpleNamespace(id=123),
                                        channel=original.channel,
                                        reference=types.SimpleNamespace(message_id=789, resolved=original))
        selection = await mod.video_respondido(command)
        self.assertEqual(selection.status, "video")
        metadata = mod.metadados_video(selection, command)
        self.assertEqual(metadata["title"], "show_ao_vivo")
        self.assertEqual(metadata["attachment_ref"], REF)
        anexo.content_type = "application/octet-stream"
        self.assertEqual((await mod.video_respondido(command)).status, "video")
        original.attachments.append(anexo)
        self.assertEqual((await mod.video_respondido(command)).status, "multiplos")

    async def test_api_refresh_requires_same_attachment_and_video(self):
        async def get_message(_channel, _message):
            return {"guild_id": "123", "attachments": [{
                "id": "10", "filename": "clip.mp4", "content_type": "video/mp4",
                "url": "https://cdn.discordapp.com/attachments/456/10/clip.mp4?ex=ff&hm=signed",
            }]}
        client = types.SimpleNamespace(http=types.SimpleNamespace(get_message=get_message))
        self.assertIn("hm=signed", (await media.fetch_discord_attachment(client, REF))["url"])
        previous = get_message
        async def generic(_channel, _message):
            result = await previous(_channel, _message)
            result["attachments"][0]["content_type"] = "application/octet-stream"
            return result
        client.http.get_message = generic
        self.assertEqual((await media.fetch_discord_attachment(client, REF))["filename"], "clip.mp4")
        with self.assertRaises(media.DiscordAttachmentError):
            await media.fetch_discord_attachment(client, {**REF, "attachment_id": 11})
        with self.assertRaises(media.DiscordAttachmentError):
            media.valid_cdn_url("https://localhost/admin")

    async def test_probe_uses_audio_duration_and_rejects_silent_video(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg ausente")
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = str(Path(tmp) / "com_audio.mp4")
            silent_path = str(Path(tmp) / "sem_audio.mp4")
            video_input = ["-f", "lavfi", "-i", "color=c=black:s=16x16:r=10:d=1"]
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *video_input,
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest",
                            "-c:v", "mpeg4", "-c:a", "aac", "-y", audio_path], check=True)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *video_input,
                            "-c:v", "mpeg4", "-y", silent_path], check=True)
            with patch.object(media, "valid_cdn_url", side_effect=lambda value: value):
                probe = await media.probe_discord_audio(audio_path)
                self.assertEqual(probe["audio_stream_index"], 1)
                self.assertEqual(probe["audio_codec"], "aac")
                self.assertAlmostEqual(probe["duration"], 1.0, delta=0.1)
                with self.assertRaisesRegex(media.DiscordAttachmentError, "não contém uma faixa"):
                    await media.probe_discord_audio(silent_path)

    def test_signed_discord_url_expires_before_play(self):
        now = media.time.time()
        resolved = media.time.monotonic()
        url = f"https://cdn.discordapp.com/attachments/456/10/a.mp4?ex={int(now + 180):x}&hm=key"
        deadline = media.prazo_stream(url, resolved, 300, margin_seconds=60)
        self.assertAlmostEqual(deadline - resolved, 120.0, delta=2.0)
        self.assertLess(media.prazo_stream(url.replace(f"{int(now + 180):x}", f"{int(now - 1):x}"),
                                           resolved, 300), resolved)


class VideoReplyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_p_reply_routes_once_to_music_and_plain_p_stays_tts(self):
        mod = input_module()
        attachment = types.SimpleNamespace(id=10, filename="clip.mp4", content_type="video/mp4")
        original = types.SimpleNamespace(id=789, attachments=[attachment])
        reply = types.SimpleNamespace(reference=types.SimpleNamespace(message_id=789, resolved=original))
        calls = []

        class Music:
            async def _run_play_discord_attachment(self, ctx, selection):
                calls.append((ctx, selection.status))

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

        with patch.dict(sys.modules, {"cogs.musica.nucleo.anexo_discord": mod}):
            consumed = await dispatch_prefix_control_command(TTS(), reply, PrefixControlCommand("panel_user", "", "_p"))
            self.assertTrue(consumed)
            self.assertEqual(calls, [(reply, "video")])
            reply.reference = None
            await dispatch_prefix_control_command(TTS(), reply, PrefixControlCommand("panel_user", "", "_p"))
            self.assertEqual(calls[-1], "tts_panel")


if __name__ == "__main__":
    unittest.main()
