"""Falhas dos prefixos de voz: aviso seguro e preso à mensagem original."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.tts.audio import QueueItem, TTSPlaybackError
from cogs.tts.cog import TTSVoice
from cogs.tts.failure_notices import failure_code, notify_tts_failure
from cogs.tts.mensagens.despacho import despachar_mensagem_tts
from cogs.tts.mensagens.preparacao import PayloadTTSMensagem


class _Fixture:
    def __init__(self, *, thread: bool = False):
        self.member = Mock(spec=discord.Member)
        self.member.id = 2
        self.me = Mock(spec=discord.Member)
        self.me.id = 9
        self.guild = Mock(spec=discord.Guild)
        self.guild.id, self.guild.me = 1, self.me
        self.member_permissions = SimpleNamespace(view_channel=True, manage_threads=False)
        self.bot_permissions = SimpleNamespace(
            view_channel=True, send_messages=True, send_messages_in_threads=True,
        )
        self.channel = Mock(spec=discord.Thread if thread else discord.TextChannel)
        self.channel.id, self.channel.guild = 10, self.guild
        self.channel.permissions_for.side_effect = lambda member: (
            self.bot_permissions if member.id == self.me.id else self.member_permissions
        )
        self.channel.send = AsyncMock()
        if thread:
            self.channel.is_private.return_value = True
            self.channel.fetch_member = AsyncMock(return_value=SimpleNamespace(id=self.member.id))
        self.guild.get_channel_or_thread.return_value = self.channel
        self.guild.get_member.return_value = self.member
        self.guild.fetch_member = AsyncMock(return_value=self.member)
        self.bot = SimpleNamespace(get_guild=Mock(return_value=self.guild))
        self.state = SimpleNamespace(last_text_channel_id=999)
        self.cog = SimpleNamespace(bot=self.bot, guild_states={1: self.state})
        self.item = QueueItem(
            guild_id=1, channel_id=20, author_id=2, text="fala privada <@999> https://private.example/token",
            engine="edge", voice="pt-BR-FranciscaNeural", language="pt-br", rate="+0%", pitch="+0Hz",
            message_id=100, text_channel_id=10,
        )


class FailureNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_and_cog_wrapper_keep_original_reference_when_shared_state_moves(self):
        fixture = _Fixture()
        fixture.item.message_id, fixture.item.text_channel_id = 800, 900
        payload = PayloadTTSMensagem("fala privada", {}, fixture.item, False)
        fixture.cog._get_state = lambda guild_id: fixture.state
        fixture.cog._enqueue_tts_items = AsyncMock(return_value=(True, 0, False))
        message = SimpleNamespace(id=100, guild=fixture.guild, channel=fixture.channel)

        result = await despachar_mensagem_tts(
            fixture.cog, message, guild_defaults={}, active_prefix=",", forced_engine="edge",
            construir_payload=AsyncMock(return_value=payload),
        )
        self.assertTrue(result.enqueued)
        fixture.state.last_text_channel_id = 777  # Another user's message moved shared state.
        await TTSVoice._notify_tts_failure(fixture.cog, result.payload.queue_item, "playback_failed")

        fixture.guild.get_channel_or_thread.assert_called_once_with(10)
        fixture.channel.send.assert_awaited_once()
        reference = fixture.channel.send.await_args.kwargs["reference"]
        self.assertEqual((reference.guild_id, reference.channel_id, reference.message_id), (1, 10, 100))
        self.assertFalse(reference.fail_if_not_exists)

    async def test_public_notice_never_contains_speech_exception_url_or_mentions(self):
        for engine in ("gtts", "edge"):
            with self.subTest(engine=engine):
                fixture = _Fixture()
                fixture.item.engine = engine
                response = SimpleNamespace(status=502, reason="bad provider")
                error = discord.HTTPException(response, {
                    "message": "secret-api-key https://private.example/token <@999> fala privada",
                    "code": 0,
                })
                await notify_tts_failure(fixture.cog, fixture.item, "synthesis_failed", error)
                content = fixture.channel.send.await_args.args[0]
                self.assertIn("synthesis_failed", content)
                self.assertIn("gTTS" if engine == "gtts" else "Edge", content)
                for private in ("fala privada", "secret-api-key", "https://", "<@999>", "bad provider"):
                    self.assertNotIn(private, content)
                mentions = fixture.channel.send.await_args.kwargs["allowed_mentions"]
                self.assertFalse(mentions.everyone)
                self.assertFalse(mentions.users)
                self.assertFalse(mentions.roles)
                self.assertFalse(mentions.replied_user)

    async def test_missing_message_or_text_channel_binding_skips_notice(self):
        for field in ("message_id", "text_channel_id"):
            with self.subTest(field=field):
                fixture = _Fixture()
                setattr(fixture.item, field, 0)
                await notify_tts_failure(fixture.cog, fixture.item, "playback_failed")
                fixture.bot.get_guild.assert_not_called()
                fixture.channel.send.assert_not_awaited()

    async def test_missing_or_cross_guild_destination_skips_notice(self):
        for scenario in ("missing_guild", "missing_channel", "other_guild"):
            with self.subTest(scenario=scenario):
                fixture = _Fixture()
                if scenario == "missing_guild":
                    fixture.bot.get_guild.return_value = None
                elif scenario == "missing_channel":
                    fixture.guild.get_channel_or_thread.return_value = None
                else:
                    fixture.channel.guild = SimpleNamespace(id=88)
                await notify_tts_failure(fixture.cog, fixture.item, "playback_failed")
                fixture.channel.send.assert_not_awaited()

    async def test_revoked_requester_or_bot_channel_permissions_skip_notice(self):
        for scenario in ("requester_hidden", "bot_hidden", "bot_cannot_send"):
            with self.subTest(scenario=scenario):
                fixture = _Fixture()
                if scenario == "requester_hidden":
                    fixture.member_permissions.view_channel = False
                elif scenario == "bot_hidden":
                    fixture.bot_permissions.view_channel = False
                else:
                    fixture.bot_permissions.send_messages = False
                await notify_tts_failure(fixture.cog, fixture.item, "playback_failed")
                fixture.channel.send.assert_not_awaited()

    async def test_private_thread_requires_membership_and_thread_send_permission(self):
        for scenario in ("outside", "cannot_send", "allowed"):
            with self.subTest(scenario=scenario):
                fixture = _Fixture(thread=True)
                if scenario == "outside":
                    fixture.channel.fetch_member.side_effect = discord.NotFound(
                        SimpleNamespace(status=404, reason="Not Found"), "Unknown Member",
                    )
                elif scenario == "cannot_send":
                    fixture.bot_permissions.send_messages_in_threads = False
                await notify_tts_failure(fixture.cog, fixture.item, "playback_failed")
                fixture.channel.fetch_member.assert_awaited_once_with(2)
                if scenario == "allowed":
                    fixture.channel.send.assert_awaited_once()
                else:
                    fixture.channel.send.assert_not_awaited()

    async def test_member_cache_miss_fetches_current_member_before_notice(self):
        fixture = _Fixture()
        fixture.guild.get_member.return_value = None
        await notify_tts_failure(fixture.cog, fixture.item, "connection_failed")
        fixture.guild.fetch_member.assert_awaited_once_with(2)
        fixture.channel.send.assert_awaited_once()

    async def test_throttle_has_30_second_boundary_and_concurrent_errors_send_once(self):
        fixture = _Fixture()
        with patch("cogs.tts.failure_notices.time.monotonic", return_value=100.0):
            await asyncio.gather(*(
                notify_tts_failure(fixture.cog, fixture.item, "playback_failed") for _ in range(8)
            ))
        fixture.channel.send.assert_awaited_once()
        with patch("cogs.tts.failure_notices.time.monotonic", return_value=129.99):
            await notify_tts_failure(fixture.cog, fixture.item, "playback_failed")
        self.assertEqual(fixture.channel.send.await_count, 1)
        with patch("cogs.tts.failure_notices.time.monotonic", return_value=130.0):
            await notify_tts_failure(fixture.cog, fixture.item, "playback_failed")
        self.assertEqual(fixture.channel.send.await_count, 2)

    async def test_throttle_retains_at_most_512_members_and_expires_old_entries(self):
        fixture = _Fixture()
        fixture.cog._tts_failure_notice_times = {(1, ident): 990.0 for ident in range(1000, 1512)}
        with patch("cogs.tts.failure_notices.time.monotonic", return_value=1000.0):
            await notify_tts_failure(fixture.cog, fixture.item, "synthesis_failed")
        self.assertEqual(len(fixture.cog._tts_failure_notice_times), 512)
        self.assertIn((1, 2), fixture.cog._tts_failure_notice_times)
        with patch("cogs.tts.failure_notices.time.monotonic", return_value=1061.0):
            await notify_tts_failure(fixture.cog, fixture.item, "synthesis_failed")
        self.assertEqual(fixture.cog._tts_failure_notice_times, {(1, 2): 1061.0})

    async def test_failed_notice_logs_only_error_type_without_payload(self):
        fixture = _Fixture()
        fixture.channel.send.side_effect = RuntimeError("secret-key https://private.example fala privada")
        with self.assertLogs("cogs.tts.failure_notices", level="WARNING") as captured:
            await notify_tts_failure(fixture.cog, fixture.item, "playback_failed")
        self.assertIn("RuntimeError", captured.output[0])
        self.assertNotIn("secret-key", captured.output[0])
        self.assertNotIn("https://", captured.output[0])


class FailureClassificationTests(unittest.TestCase):
    def test_known_audio_failures_and_unknown_provider_errors_have_safe_codes(self):
        cases = (
            ("playback_failed", discord.opus.OpusNotLoaded(), "opus_missing"),
            ("playback_failed", FileNotFoundError(2, "secret", "/usr/bin/ffmpeg"), "ffmpeg_missing"),
            ("playback_failed", FileNotFoundError(2, "secret", "C:\\bin\\ffmpeg.exe"), "ffmpeg_missing"),
            ("playback_failed", FileNotFoundError(2, "secret", "/private/audio.mp3"), "audio_missing"),
            ("playback_failed", TTSPlaybackError("no_frames"), "no_frames"),
            ("playback_failed", TTSPlaybackError("route_failed"), "route_failed"),
            ("playback_failed", TTSPlaybackError("secret-unknown-code"), "playback_failed"),
            ("secret-reason", RuntimeError("token https://private.example"), "tts_failed"),
        )
        for reason, error, expected in cases:
            with self.subTest(error=type(error).__name__, expected=expected):
                self.assertEqual(failure_code(reason, error), expected)


class GateFailureNoticeTests(unittest.IsolatedAsyncioTestCase):
    def _fixture(self, content, *, guild_enabled=True):
        fixture = _Fixture()
        fixture.member.bot = False
        call = SimpleNamespace(id=20)
        fixture.member.voice = SimpleNamespace(channel=call)
        fixture.me.voice = SimpleNamespace(channel=call, mute=False, suppress=False)
        fixture.message = SimpleNamespace(
            id=100, guild=fixture.guild, channel=fixture.channel, author=fixture.member, content=content,
        )
        fixture.defaults = {"enabled": guild_enabled, "gtts_prefix": "!g", "edge_prefix": "!e"}
        fixture.db = SimpleNamespace(get_guild_tts_defaults=AsyncMock(return_value=fixture.defaults))
        fixture.cog._get_db = lambda: fixture.db

        async def maybe_await(value):
            return await value if asyncio.iscoroutine(value) else value

        fixture.cog._maybe_await = maybe_await
        fixture.cog._notify_tts_failure = AsyncMock()
        fixture.cog._notify_tts_gate_failure = TTSVoice._notify_tts_gate_failure.__get__(fixture.cog)
        fixture.cog._record_tts_message_gate = Mock()
        fixture.cog._was_tts_message_seen = Mock(return_value=False)
        fixture.cog._mark_tts_message_seen = Mock()
        fixture.cog._member_has_ignored_tts_role = Mock(return_value=False)
        return fixture

    async def test_global_disabled_gate_does_not_reply_to_bots_or_antibot_captured_messages(self):
        for blocked_by in ("bot", "antibot", "webhook"):
            with self.subTest(blocked_by=blocked_by):
                fixture = self._fixture("!e fala privada")
                fixture.member.bot = blocked_by == "bot"
                fixture.message.webhook_id = 123 if blocked_by == "webhook" else None
                fixture.bot.antibot_should_block_message = lambda message: blocked_by == "antibot"
                with patch("cogs.tts.mensagens.triagem.config.TTS_ENABLED", False):
                    await TTSVoice._process_tts_message(fixture.cog, fixture.message)
                fixture.cog._notify_tts_failure.assert_not_awaited()
                fixture.db.get_guild_tts_defaults.assert_not_awaited()

    async def test_disabled_gate_keeps_ordinary_messages_silent(self):
        for globally_enabled in (False, True):
            with self.subTest(globally_enabled=globally_enabled):
                fixture = self._fixture("texto comum e privado", guild_enabled=False)
                with patch("cogs.tts.mensagens.triagem.config.TTS_ENABLED", globally_enabled), patch(
                    "cogs.tts.cog.despachar_mensagem_tts", new_callable=AsyncMock,
                ) as dispatch:
                    await TTSVoice._process_tts_message(fixture.cog, fixture.message)
                fixture.cog._notify_tts_failure.assert_not_awaited()
                dispatch.assert_not_awaited()

    async def test_disabled_gate_recognizes_configured_prefix_and_pins_origin_without_speech(self):
        for globally_enabled in (False, True):
            for prefix, engine in (("!g", "gtts"), ("!e", "edge")):
                with self.subTest(globally_enabled=globally_enabled, engine=engine):
                    fixture = self._fixture(prefix + " fala privada <@999>", guild_enabled=False)
                    with patch("cogs.tts.mensagens.triagem.config.TTS_ENABLED", globally_enabled), patch(
                        "cogs.tts.cog.despachar_mensagem_tts", new_callable=AsyncMock,
                    ) as dispatch:
                        await TTSVoice._process_tts_message(fixture.cog, fixture.message)
                    fixture.cog._notify_tts_failure.assert_awaited_once()
                    item, reason = fixture.cog._notify_tts_failure.await_args.args
                    self.assertEqual((item.guild_id, item.author_id, item.text_channel_id, item.message_id), (1, 2, 10, 100))
                    self.assertEqual(item.engine, engine)
                    self.assertEqual(item.text, "")
                    self.assertEqual(reason, "tts_guild_disabled" if globally_enabled else "tts_disabled")
                    dispatch.assert_not_awaited()

    async def test_bot_muted_or_suppressed_in_author_call_does_not_enqueue(self):
        for voice_flag in ("mute", "suppress"):
            with self.subTest(voice_flag=voice_flag):
                fixture = self._fixture("!e fala privada")
                setattr(fixture.me.voice, voice_flag, True)
                with patch("cogs.tts.mensagens.triagem.config.TTS_ENABLED", True), patch(
                    "cogs.tts.cog.despachar_mensagem_tts", new_callable=AsyncMock,
                ) as dispatch:
                    await TTSVoice._process_tts_message(fixture.cog, fixture.message)
                dispatch.assert_not_awaited()
                fixture.cog._notify_tts_failure.assert_awaited_once()
                item, reason = fixture.cog._notify_tts_failure.await_args.args
                self.assertEqual(reason, "bot_muted")
                self.assertEqual(item.text, "")
                self.assertEqual(item.message_id, 100)
                self.assertEqual(item.text_channel_id, 10)

    async def test_bot_muted_in_other_call_does_not_block_author_prefix_at_gate(self):
        fixture = self._fixture("!g fala privada")
        fixture.me.voice.channel = SimpleNamespace(id=21)
        fixture.me.voice.mute = True
        with patch("cogs.tts.mensagens.triagem.config.TTS_ENABLED", True), patch(
            "cogs.tts.cog.despachar_mensagem_tts", new_callable=AsyncMock,
            return_value=SimpleNamespace(payload=None),
        ) as dispatch:
            await TTSVoice._process_tts_message(fixture.cog, fixture.message)
        dispatch.assert_awaited_once()
        fixture.cog._notify_tts_failure.assert_not_awaited()
