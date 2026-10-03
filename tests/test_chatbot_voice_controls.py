"""Sair/mover com sessão fixada e interrupção somente da fonte TTS própria."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from cogs.tts.audio import QueueItem
from test_chatbot_voice_actions import _Probe, _Source


class ChatbotVoiceControlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_move_pins_session_target_member_and_preserves_codec_settings(self):
        probe = _Probe()
        voice = probe.guild.voice_client
        probe.member.voice.channel = probe.other
        probe.other.permissions_for.return_value = probe.channel.permissions_for.return_value
        token = probe.chatbot_voice_session_ref(1)
        guard = AsyncMock()
        result = await probe.chatbot_move_voice(guild_id=1, user_id=2, channel_id=21,
            request_id="move-1", session_ref=token, before_effect=guard)
        self.assertTrue(result["ok"])
        self.assertEqual(voice.move_calls, [21])
        self.assertEqual(probe._chatbot_temporary_voice_channels, {1: 21})
        self.assertNotEqual(token, probe.chatbot_voice_session_ref(1))
        guard.assert_awaited_once()
        probe.synthesize_chatbot_attachment.assert_not_awaited()

    async def test_move_rechecks_session_member_music_and_target_permissions_after_guard(self):
        for change in ("new_session", "member_left", "music", "permission"):
            with self.subTest(change=change):
                probe = _Probe()
                voice = probe.guild.voice_client
                voice.session_id = "session-one"
                probe.member.voice.channel = probe.other
                probe.other.permissions_for.return_value = probe.channel.permissions_for.return_value
                token = probe.chatbot_voice_session_ref(1)

                async def changed():
                    if change == "new_session":
                        voice.session_id = "session-two"
                    elif change == "member_left":
                        probe.member.voice.channel = None
                    elif change == "music":
                        probe.music = True
                    else:
                        probe.other.permissions_for.return_value.connect = False

                result = await probe.chatbot_move_voice(guild_id=1, user_id=2, channel_id=21,
                    request_id="move-1", session_ref=token, before_effect=changed)
                self.assertFalse(result["ok"])
                self.assertEqual(voice.move_calls, [])
                probe.channel.connect.assert_not_awaited()

    async def test_move_and_leave_do_not_take_over_audio_or_music(self):
        for busy in ("playing", "music", "queued"):
            with self.subTest(busy=busy):
                probe = _Probe()
                voice = probe.guild.voice_client
                voice.disconnect = AsyncMock()
                if busy == "playing":
                    voice.playing = True
                elif busy == "music":
                    probe.music = True
                else:
                    probe._get_state(1).queue.put_nowait(QueueItem(1, 20, 2, "normal", "gtts", "", "pt", "+0%", "+0Hz"))
                self.assertIsNone(probe.chatbot_voice_session_ref(1))
                for name in ("chatbot_move_voice", "chatbot_leave_voice"):
                    result = await getattr(probe, name)(guild_id=1, user_id=2, channel_id=20, request_id="change")
                    self.assertFalse(result["ok"])
                voice.disconnect.assert_not_awaited()
                self.assertEqual(voice.move_calls, [])
                self.assertEqual(voice.stop_calls, 0)

    async def test_staff_leave_uses_exact_session_and_does_not_auto_restore(self):
        probe = _Probe()
        voice = probe.guild.voice_client
        token = probe.chatbot_voice_session_ref(1)
        async def disconnect(**kwargs):
            voice.connected = False
            probe.guild.me.voice.channel = None
        voice.disconnect = AsyncMock(side_effect=disconnect)
        probe._mark_manual_voice_disconnect = AsyncMock()  # helper é síncrono em produção
        # Use função síncrona observável, para não criar corrotina não aguardada.
        from unittest.mock import Mock
        probe._mark_manual_voice_disconnect = Mock()
        probe._clear_remembered_voice_channel = AsyncMock()
        result = await probe.chatbot_leave_voice(guild_id=1, user_id=2, channel_id=20,
            request_id="leave", session_ref=token)
        self.assertTrue(result["ok"])
        voice.disconnect.assert_awaited_once_with(force=False)
        probe._mark_manual_voice_disconnect.assert_called_once_with(1)
        probe._cancel_runtime_voice_restore.assert_called_once_with(1)
        probe._clear_remembered_voice_channel.assert_awaited_once_with(1)
        self.assertIsNone(probe.chatbot_voice_session_ref(1))
        probe.channel.connect.assert_not_awaited()

    async def test_unknown_move_is_not_retried(self):
        probe = _Probe()
        probe.member.voice.channel = probe.other
        probe.other.permissions_for.return_value = probe.channel.permissions_for.return_value
        probe.guild.voice_client.move_to = AsyncMock(side_effect=asyncio.TimeoutError())
        result = await probe.chatbot_move_voice(guild_id=1, user_id=2, channel_id=21, request_id="move")
        self.assertEqual(result["status"], "uncertain")
        probe.guild.voice_client.move_to.assert_awaited_once()

    async def test_interrupt_own_active_chatbot_source_without_waiting_for_playback_lock(self):
        probe = _Probe()
        voice = probe.guild.voice_client
        voice.auto_finish = False
        speech = asyncio.create_task(probe.chatbot_speak_voice(guild_id=1, user_id=2,
            channel_id=20, request_id="own-speech", text="Fala original"))
        await asyncio.wait_for(voice.played.wait(), timeout=1)
        cap = probe.chatbot_own_speech_ref(1, 2)
        self.assertIsNotNone(cap)
        self.assertTrue(probe._get_tts_playback_lock(1).locked())
        self.assertTrue(probe._get_voice_connect_lock(1).locked())
        result = await asyncio.wait_for(probe.chatbot_interrupt_speech(guild_id=1, user_id=2,
            channel_id=20, request_id="interrupt", session_ref=cap["session_ref"],
            speech_request_id=cap["request_id"]), timeout=1)
        self.assertTrue(result["ok"])
        finished = await speech
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["message"], "A fala foi interrompida.")
        self.assertEqual(voice.stop_calls, 1)
        self.assertEqual(voice.play_calls, 1)
        probe.synthesize_chatbot_attachment.assert_awaited_once()
        self.assertIsNone(probe.chatbot_own_speech_ref(1, 2))

    async def test_foreign_user_foreign_source_or_music_cannot_interrupt(self):
        for changed in ("other_user", "foreign_source", "music", "new_session"):
            with self.subTest(changed=changed):
                probe = _Probe()
                voice = probe.guild.voice_client
                voice.auto_finish = False
                voice.session_id = "first"
                speech = asyncio.create_task(probe.chatbot_speak_voice(guild_id=1, user_id=2,
                    channel_id=20, request_id="own-speech", text="Fala original"))
                await asyncio.wait_for(voice.played.wait(), timeout=1)
                cap = probe.chatbot_own_speech_ref(1, 2)
                if changed == "foreign_source":
                    voice.source = _Source()
                elif changed == "music":
                    probe.music = True
                elif changed == "new_session":
                    voice.session_id = "second"
                result = await probe.chatbot_interrupt_speech(guild_id=1, user_id=3 if changed == "other_user" else 2,
                    channel_id=20, request_id="interrupt", session_ref=cap["session_ref"], speech_request_id=cap["request_id"])
                self.assertFalse(result["ok"])
                self.assertEqual(voice.stop_calls, 0)
                voice.finish()
                await speech

    async def test_finished_speech_between_guard_and_stop_does_not_stop_next_source(self):
        probe = _Probe()
        voice = probe.guild.voice_client
        voice.auto_finish = False
        speech = asyncio.create_task(probe.chatbot_speak_voice(guild_id=1, user_id=2,
            channel_id=20, request_id="own-speech", text="Fala original"))
        await asyncio.wait_for(voice.played.wait(), timeout=1)
        async def finished():
            voice.finish()
            await speech
        result = await probe.chatbot_interrupt_speech(guild_id=1, user_id=2, channel_id=20,
            request_id="interrupt", before_effect=finished)
        self.assertFalse(result["ok"])
        self.assertEqual(voice.stop_calls, 0)

    async def test_legacy_tts_is_interruptible_only_by_corresponding_requester(self):
        probe = _Probe()
        voice = probe.guild.voice_client
        voice.auto_finish = False
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "normal.mp3"
            path.write_bytes(b"normal")
            item = QueueItem(1, 20, 2, "TTS do membro", "gtts", "", "pt", "+0%", "+0Hz", request_id="normal-1")
            speech = asyncio.create_task(probe._play_file(voice, str(path), item=item))
            await asyncio.wait_for(voice.played.wait(), timeout=1)
            self.assertIsNone(probe.chatbot_own_speech_ref(1, 3))
            result = await probe.chatbot_interrupt_speech(guild_id=1, user_id=2,
                channel_id=20, request_id="interrupt", speech_request_id="normal-1")
            self.assertTrue(result["ok"])
            self.assertTrue((await speech)["chatbot_interrupted"])
            self.assertEqual(voice.stop_calls, 1)

    async def test_preference_overrides_preserve_rate_pitch_and_confirm_first_frame(self):
        probe = _Probe()
        probe.db.resolve_tts = lambda *_: {"edge_voice": "base", "gtts_language": "en", "edge_rate": "+3%", "edge_pitch": "+5Hz"}
        result = await probe.chatbot_speak_voice(guild_id=1, user_id=2, channel_id=20,
            request_id="speak", text="Olá", voice_override="pt-BR-AntonioNeural", language_override="pt")
        self.assertTrue(result["first_frame_observed"])
        arguments = probe.synthesize_chatbot_attachment.await_args.kwargs
        self.assertEqual((arguments["voice"], arguments["language"], arguments["rate"], arguments["pitch"]),
                         ("pt-BR-AntonioNeural", "pt", "+3%", "+5Hz"))
