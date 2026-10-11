"""Validação isolada dos avisos: funciona mesmo sem discord.py no ambiente de CI."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "cogs/tts/failure_notices.py"


class FakeThread:
    pass


class FakeReference:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeMentions:
    @classmethod
    def none(cls):
        return SimpleNamespace(everyone=False, users=False, roles=False, replied_user=False)


fake_discord = SimpleNamespace(Thread=FakeThread, MessageReference=FakeReference, AllowedMentions=FakeMentions)
spec = importlib.util.spec_from_file_location("isolated_tts_failure_notices", SOURCE)
notices = importlib.util.module_from_spec(spec)
with patch.dict("sys.modules", {"discord": fake_discord}):
    spec.loader.exec_module(notices)


class Fixture:
    def __init__(self, *, voice_perms=None, full=False):
        self.author = SimpleNamespace(id=2)
        self.me = SimpleNamespace(id=9, voice=SimpleNamespace(channel=None))
        self.voice = SimpleNamespace(
            id=20, members=[self.author] if full else [], user_limit=1 if full else 0,
            permissions_for=lambda member: voice_perms or SimpleNamespace(
                view_channel=True, connect=True, speak=True, move_members=False,
            ),
        )
        self.guild = SimpleNamespace(id=1, me=self.me)
        self.text_channel = SimpleNamespace(id=10, guild=self.guild, send=AsyncMock())
        self.text_channel.permissions_for = lambda member: SimpleNamespace(
            view_channel=True, send_messages=True, send_messages_in_threads=True,
        )
        self.guild.get_channel = lambda ident: self.voice if ident == 20 else None
        self.guild.get_channel_or_thread = lambda ident: self.text_channel if ident == 10 else None
        self.guild.get_member = lambda ident: self.author if ident == 2 else None
        self.guild.fetch_member = AsyncMock(return_value=self.author)
        self.cog = SimpleNamespace(bot=SimpleNamespace(get_guild=lambda ident: self.guild if ident == 1 else None))
        self.item = SimpleNamespace(guild_id=1, author_id=2, channel_id=20, text_channel_id=10, message_id=100)

    def sent(self):
        return self.text_channel.send.await_args.args[0]


class NoticeDesignTests(unittest.IsolatedAsyncioTestCase):
    async def test_six_approved_messages_are_exact_and_have_no_metadata(self):
        expected = {
            "author_not_in_voice": "-# C tem que estar em call pra poder usar isso <:vish:1533479762230775898>",
            "voice_channel_mismatch": "-# C tem que estar na mesma call q eu <:speed:1534287805424074772>",
            "tts_disabled": "-# Texto pra fala tá desativado nesse sv chefe <:vish:1533479762230775898>",
            "tts_guild_disabled": "-# Texto pra fala tá desativado nesse sv chefe <:vish:1533479762230775898>",
            "service_unavailable": "-# Tem algo de errado comigo agora, tenta dnv mais tarde <:osaka:1539137127852539944>",
            "playback_failed": "-# N consegui falar isso por algum motivo <:speed:1534287805424074772>",
        }
        for code, text in expected.items():
            with self.subTest(code=code):
                fixture = Fixture()
                await notices.notify_tts_failure(fixture.cog, fixture.item, code)
                self.assertEqual(fixture.sent(), text)
                self.assertNotIn('Código:', fixture.sent())
                kwargs = fixture.text_channel.send.await_args.kwargs
                self.assertEqual(kwargs['reference'].message_id, 100)
                self.assertFalse(kwargs['allowed_mentions'].replied_user)

    async def test_dynamic_permission_uses_only_trusted_labels(self):
        for attr, label in (("view_channel", "Ver canal"), ("connect", "Conectar"), ("speak", "Falar")):
            with self.subTest(permission=attr):
                perms = SimpleNamespace(view_channel=True, connect=True, speak=True, move_members=False)
                setattr(perms, attr, False)
                fixture = Fixture(voice_perms=perms)
                await notices.notify_tts_failure(fixture.cog, fixture.item, "connection_failed")
                self.assertEqual(fixture.sent(), f"-# Não consigo falar nessa call 💔  (ps: se vc me der permissão de *{label}* talvez eu consiga)")

    async def test_full_call_is_not_mistaken_for_permission(self):
        fixture = Fixture(full=True)
        await notices.notify_tts_failure(fixture.cog, fixture.item, "connection_failed")
        self.assertEqual(fixture.sent(), "-# Essa call tá cheia, n consigo entrar <:vish:1533479762230775898>")
        fixture.me.voice.channel = fixture.voice
        self.assertIsNone(notices.diagnose_voice_access(fixture.guild, fixture.voice))

    async def test_suppressed_muted_and_rate_limited_are_distinct(self):
        for code, message in {
            "bot_muted": "-# Tô mutada nessa call 💔 (ps: alguém precisa tirar meu mute)",
            "bot_suppressed": "-# Me botaram na plateia, n consigo falar 💔",
            "voice_rate_limited": "-# Tão limitando minhas falas agr, tenta dnv daqui a pouco <:speed:1534287805424074772>",
            "ignored_role": "-# C tem um cargo q não pode usar texto pra fala nesse sv <:vish:1533479762230775898>",
            "queue_unavailable": "-# Minha fila de fala tá com problema agr, tenta dnv <:speed:1534287805424074772>",
        }.items():
            fixture = Fixture()
            await notices.notify_tts_failure(fixture.cog, fixture.item, code)
            self.assertEqual(fixture.sent(), message)

    async def test_errors_of_different_categories_do_not_hide_each_other(self):
        fixture = Fixture()
        with patch.object(notices.time, "monotonic", return_value=100.0):
            await asyncio.gather(*(notices.notify_tts_failure(fixture.cog, fixture.item, 'playback_failed') for _ in range(5)))
            await notices.notify_tts_failure(fixture.cog, fixture.item, 'network_failed')
            await notices.notify_tts_failure(fixture.cog, fixture.item, 'author_not_in_voice')
            await notices.notify_tts_failure(fixture.cog, fixture.item, 'no_frames')
        self.assertEqual(fixture.text_channel.send.await_count, 3)
        self.assertEqual(len(fixture.cog._tts_failure_notice_times), 3)
        with patch.object(notices.time, "monotonic", return_value=130.0):
            await notices.notify_tts_failure(fixture.cog, fixture.item, 'playback_failed')
        self.assertEqual(fixture.text_channel.send.await_count, 4)

    async def test_cooldown_avoids_repeated_member_fetch(self):
        fixture = Fixture()
        fixture.guild.get_member = lambda _ident: None
        with patch.object(notices.time, "monotonic", side_effect=(100.0, 101.0)):
            await notices.notify_tts_failure(fixture.cog, fixture.item, "playback_failed")
            await notices.notify_tts_failure(fixture.cog, fixture.item, "playback_failed")
        fixture.guild.fetch_member.assert_awaited_once()
        self.assertEqual(fixture.text_channel.send.await_count, 1)

    async def test_bot_cannot_send_skips_member_fetch(self):
        fixture = Fixture()
        fixture.guild.get_member = lambda _ident: None
        fixture.text_channel.permissions_for = lambda member: SimpleNamespace(
            view_channel=True, send_messages=False, send_messages_in_threads=False,
        )
        await notices.notify_tts_failure(fixture.cog, fixture.item, "playback_failed")
        fixture.guild.fetch_member.assert_not_awaited()
        fixture.text_channel.send.assert_not_awaited()

    async def test_failed_delivery_does_not_hold_cooldown(self):
        fixture = Fixture()
        fixture.text_channel.send.side_effect = RuntimeError('private secret')
        await notices.notify_tts_failure(fixture.cog, fixture.item, 'playback_failed')
        self.assertEqual(fixture.cog._tts_failure_notice_times, {})

    async def test_no_speech_url_or_exception_in_user_visible_message(self):
        fixture = Fixture()
        await notices.notify_tts_failure(fixture.cog, fixture.item, "playback_failed", RuntimeError('private https://example.com <@123>'))
        self.assertNotIn('private', fixture.sent())
        self.assertNotIn('https', fixture.sent())
        self.assertNotIn('<@123>', fixture.sent())


if __name__ == '__main__':
    unittest.main()
