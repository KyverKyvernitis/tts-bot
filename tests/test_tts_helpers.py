from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_runtime_stubs() -> None:
    if "discord" not in sys.modules:
        discord = types.ModuleType("discord")

        class Object:
            def __init__(self, id: int):
                self.id = id

        class Embed:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        discord.Object = Object
        discord.Embed = Embed
        discord.Member = type("Member", (), {})
        discord.Role = type("Role", (), {})
        discord.Guild = type("Guild", (), {})
        discord.Message = type("Message", (), {})
        discord.Interaction = type("Interaction", (), {})
        discord.InteractionResponseType = type("InteractionResponseType", (), {})
        discord.NotFound = type("NotFound", (Exception,), {})
        discord.SelectOption = type("SelectOption", (), {})
        discord.ButtonStyle = type("ButtonStyle", (), {"secondary": 2, "success": 3, "danger": 4, "primary": 1})
        discord.Color = type("Color", (), {"green": staticmethod(lambda: 0), "red": staticmethod(lambda: 0)})
        discord.VoiceChannel = type("VoiceChannel", (), {})
        discord.StageChannel = type("StageChannel", (), {})
        discord.TextChannel = type("TextChannel", (), {})
        discord.VoiceClient = type("VoiceClient", (), {})
        discord.VoiceState = type("VoiceState", (), {})
        discord.FFmpegPCMAudio = type("FFmpegPCMAudio", (), {})
        discord.PCMVolumeTransformer = type("PCMVolumeTransformer", (), {})
        discord.utils = types.SimpleNamespace(get=lambda *args, **kwargs: None)
        discord.abc = types.SimpleNamespace(GuildChannel=type("GuildChannel", (), {}))
        discord.ui = types.SimpleNamespace(View=type("View", (), {}), Button=type("Button", (), {}), Select=type("Select", (), {}))

        app_commands = types.ModuleType("discord.app_commands")

        def guilds(*_objs):
            def decorator(func):
                return func
            return decorator

        app_commands.guilds = guilds
        discord.app_commands = app_commands

        ext = types.ModuleType("discord.ext")
        commands = types.ModuleType("discord.ext.commands")
        ext.commands = commands
        discord.ext = ext

        sys.modules["discord"] = discord
        sys.modules["discord.app_commands"] = app_commands
        sys.modules["discord.ext"] = ext
        sys.modules["discord.ext.commands"] = commands

    if "edge_tts" not in sys.modules:
        edge_tts = types.ModuleType("edge_tts")
        edge_tts.Communicate = object
        sys.modules["edge_tts"] = edge_tts

    if "gtts" not in sys.modules:
        gtts = types.ModuleType("gtts")

        class FakeGTTS:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

            def write_to_fp(self, fp):
                fp.write(b"")

        gtts.gTTS = FakeGTTS
        sys.modules["gtts"] = gtts
    else:
        gtts = sys.modules["gtts"]

    if "gtts.tts" not in sys.modules:
        gtts_tts = types.ModuleType("gtts.tts")

        class gTTSError(Exception):
            pass

        gtts_tts.gTTSError = gTTSError
        sys.modules["gtts.tts"] = gtts_tts

    if "gtts.lang" not in sys.modules:
        gtts_lang = types.ModuleType("gtts.lang")
        gtts_lang.tts_langs = lambda: {"pt": "Portuguese", "en": "English"}
        sys.modules["gtts.lang"] = gtts_lang

    if "google" not in sys.modules:
        google = types.ModuleType("google")
        sys.modules["google"] = google
    if "google.cloud" not in sys.modules:
        google_cloud = types.ModuleType("google.cloud")
        sys.modules["google.cloud"] = google_cloud
    if "google.cloud.texttospeech_v1" not in sys.modules:
        tts = types.ModuleType("google.cloud.texttospeech_v1")
        sys.modules["google.cloud.texttospeech_v1"] = tts


_install_runtime_stubs()

from cogs.tts.audio import QueueItem
from cogs.tts.utils.message_dispatch import dispatch_message_tts
from cogs.tts.utils.message_gate import analyze_message_for_tts
from cogs.tts.utils.message_payload import MessageTTSPayload, build_message_tts_payload
from cogs.tts.utils.message_render import render_message_tts_text
from cogs.tts.common import (
    _normalize_spaces,
    _speech_name,
    _looks_pronounceable_for_tts,
    _extract_primary_domain,
    DISCORD_CHANNEL_URL_PATTERN,
    _ATTACHMENT_IMAGE_EXTENSIONS,
    _ATTACHMENT_VIDEO_EXTENSIONS,
)
from cogs.tts.utils.text import (
    tts_attachment_descriptions,
    tts_channel_reference,
    tts_link_reference,
    tts_role_reference,
    tts_user_reference,
)


class FakeDB:
    def __init__(self, *, guild_defaults=None, resolved=None):
        self.guild_defaults = guild_defaults or {}
        self.resolved = resolved or {}

    async def get_guild_tts_defaults(self, guild_id: int):
        return dict(self.guild_defaults)

    async def resolve_tts(self, guild_id: int, user_id: int):
        return dict(self.resolved)


class FakeGuild:
    def __init__(self, guild_id: int = 1, *, members=None, roles=None, channels=None):
        self.id = guild_id
        self._members = {m.id: m for m in (members or [])}
        self._roles = {r.id: r for r in (roles or [])}
        self._channels = {c.id: c for c in (channels or [])}

    def get_member(self, member_id: int):
        return self._members.get(member_id)

    def get_role(self, role_id: int):
        return self._roles.get(role_id)

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)


class FakeCog:
    def __init__(self, *, db=None):
        self._db = db
        self._state = SimpleNamespace(last_text_channel_id=None)
        self.enqueue_calls = []

    def _get_db(self):
        return self._db

    async def _maybe_await(self, value):
        if asyncio.iscoroutine(value):
            return await value
        return value

    def _render_tts_text(self, message, text: str) -> str:
        return text.strip().replace("dupe", "rendered")

    def _apply_author_prefix_if_needed(self, guild_id, author, text: str, *, enabled: bool):
        return f"{author.display_name} disse {text}" if enabled else text

    def _guild_announce_author_enabled(self, guild_defaults):
        return bool((guild_defaults or {}).get("announce_author"))

    def _get_state(self, guild_id: int):
        return self._state

    async def _enqueue_tts_item(self, guild_id: int, item: QueueItem):
        self.enqueue_calls.append((guild_id, item))
        return True, 0, False


class MessageGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_reason_when_tts_disabled(self):
        cog = FakeCog(db=FakeDB())
        message = SimpleNamespace(author=SimpleNamespace(bot=False), guild=SimpleNamespace(id=123), content=", oi")

        with patch("cogs.tts.utils.message_gate.config.TTS_ENABLED", False):
            decision = await analyze_message_for_tts(cog, message)

        self.assertFalse(decision.should_process_tts)
        self.assertEqual(decision.reason, "tts_disabled")

    async def test_detects_prefix_command(self):
        cog = FakeCog(db=FakeDB(guild_defaults={"bot_prefix": "_"}))
        message = SimpleNamespace(author=SimpleNamespace(bot=False), guild=SimpleNamespace(id=123), content="_join")

        decision = await analyze_message_for_tts(cog, message)

        self.assertTrue(decision.should_dispatch_prefix_command)
        self.assertEqual(decision.prefix_command.kind, "join")
        self.assertEqual(decision.reason, "prefix_command")

    async def test_detects_tts_prefix(self):
        cog = FakeCog(db=FakeDB(guild_defaults={"tts_prefix": "."}))
        message = SimpleNamespace(author=SimpleNamespace(bot=False), guild=SimpleNamespace(id=123), content=".olá")

        decision = await analyze_message_for_tts(cog, message)

        self.assertTrue(decision.should_process_tts)
        self.assertEqual(decision.forced_engine, "gtts")
        self.assertEqual(decision.active_prefix, ".")
        self.assertEqual(decision.reason, "tts_prefix_matched")

    async def test_returns_reason_when_no_matching_prefix(self):
        cog = FakeCog(db=FakeDB())
        message = SimpleNamespace(author=SimpleNamespace(bot=False), guild=SimpleNamespace(id=123), content="olá mundo")

        decision = await analyze_message_for_tts(cog, message)

        self.assertFalse(decision.should_process_tts)
        self.assertEqual(decision.reason, "no_engine_prefix")


class MessageRenderTests(unittest.TestCase):
    def _user_reference(self, member, *, guild_id=None):
        return tts_user_reference(member, resolver=lambda m, guild_id=None: (getattr(m, "display_name", "usuário"), None), guild_id=guild_id)

    def _role_reference(self, role):
        return tts_role_reference(role, normalize_spaces=_normalize_spaces, looks_pronounceable_for_tts=_looks_pronounceable_for_tts, speech_name=_speech_name)

    def _channel_reference(self, channel):
        return tts_channel_reference(channel, normalize_spaces=_normalize_spaces, looks_pronounceable_for_tts=_looks_pronounceable_for_tts, speech_name=_speech_name)

    def _link_reference(self, url, *, guild=None):
        return tts_link_reference(
            url,
            guild=guild,
            discord_channel_url_pattern=DISCORD_CHANNEL_URL_PATTERN,
            channel_reference=self._channel_reference,
            extract_primary_domain=_extract_primary_domain,
            looks_pronounceable_for_tts=_looks_pronounceable_for_tts,
            speech_name=_speech_name,
        )

    def test_simple_text_uses_fast_path_and_attachment_suffix(self):
        attachment = SimpleNamespace(filename="foto.png", content_type="image/png")
        message = SimpleNamespace(attachments=[attachment], guild=None, mentions=[], role_mentions=[], channel_mentions=[])

        result = render_message_tts_text(
            message,
            "vc mandou",
            guild_id=None,
            user_reference=self._user_reference,
            role_reference=self._role_reference,
            channel_reference=self._channel_reference,
            link_reference=self._link_reference,
            normalize_spaces=_normalize_spaces,
            image_extensions=_ATTACHMENT_IMAGE_EXTENSIONS,
            video_extensions=_ATTACHMENT_VIDEO_EXTENSIONS,
        )

        self.assertEqual(result, "você mandou. Anexo de imagem")

    def test_replaces_mentions_links_and_channels(self):
        member = SimpleNamespace(id=10, display_name="Lucas")
        role = SimpleNamespace(id=20, name="Staff")
        channel = SimpleNamespace(id=30, name="geral")
        guild = FakeGuild(members=[member], roles=[role], channels=[channel])
        message = SimpleNamespace(
            guild=guild,
            attachments=[],
            mentions=[member],
            role_mentions=[role],
            channel_mentions=[channel],
        )

        result = render_message_tts_text(
            message,
            "oi <@10> veja <@&20> no <#30> https://example.com/test",
            guild_id=1,
            user_reference=self._user_reference,
            role_reference=self._role_reference,
            channel_reference=self._channel_reference,
            link_reference=self._link_reference,
            normalize_spaces=_normalize_spaces,
            image_extensions=_ATTACHMENT_IMAGE_EXTENSIONS,
            video_extensions=_ATTACHMENT_VIDEO_EXTENSIONS,
        )

        self.assertIn("Lucas", result)
        self.assertIn("cargo Staff", result)
        self.assertIn("canal geral", result)
        self.assertIn("link do example", result)


class MessagePayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_payload_and_queue_item_with_author_prefix(self):
        db = FakeDB(resolved={"engine": "edge", "voice": "", "rate": "", "pitch": ""})
        cog = FakeCog(db=db)
        author = SimpleNamespace(id=22, display_name="Dilma", voice=SimpleNamespace(channel=SimpleNamespace(id=555)))
        message = SimpleNamespace(guild=SimpleNamespace(id=111), author=author, content=",olá")

        payload = await build_message_tts_payload(
            cog,
            message,
            guild_defaults={"announce_author": True},
            active_prefix=",",
            forced_engine="edge",
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload.queue_item.guild_id, 111)
        self.assertEqual(payload.queue_item.channel_id, 555)
        self.assertEqual(payload.queue_item.engine, "edge")
        self.assertEqual(payload.queue_item.voice, "pt-BR-FranciscaNeural")
        self.assertEqual(payload.queue_item.rate, "+0%")
        self.assertEqual(payload.text, "Dilma disse olá")

    async def test_returns_none_without_voice_channel(self):
        db = FakeDB(resolved={"engine": "gtts", "language": "pt-br"})
        cog = FakeCog(db=db)
        author = SimpleNamespace(id=22, display_name="Dilma", voice=None)
        message = SimpleNamespace(guild=SimpleNamespace(id=111), author=author, content=".olá")

        payload = await build_message_tts_payload(
            cog,
            message,
            guild_defaults={},
            active_prefix=".",
            forced_engine="gtts",
        )

        self.assertIsNone(payload)


class MessageDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_aborts_when_payload_is_none(self):
        cog = FakeCog(db=FakeDB())
        message = SimpleNamespace(guild=SimpleNamespace(id=1), channel=SimpleNamespace(id=2))

        async def fake_build(*args, **kwargs):
            return None

        with patch("cogs.tts.utils.message_dispatch.build_message_tts_payload", fake_build):
            result = await dispatch_message_tts(cog, message, guild_defaults={}, active_prefix=".", forced_engine="gtts")

        self.assertFalse(result.enqueued)
        self.assertIsNone(result.payload)
        self.assertEqual(cog.enqueue_calls, [])

    async def test_dispatch_enqueues_payload_and_updates_state(self):
        cog = FakeCog(db=FakeDB())
        message = SimpleNamespace(guild=SimpleNamespace(id=77), channel=SimpleNamespace(id=99))
        item = QueueItem(77, 55, 66, "teste", "gtts", "", "pt", "+0%", "+0Hz")
        payload = MessageTTSPayload(text="teste", resolved={"engine": "gtts"}, queue_item=item, forced_gtts=False)

        async def fake_build(*args, **kwargs):
            return payload

        with patch("cogs.tts.utils.message_dispatch.build_message_tts_payload", fake_build):
            result = await dispatch_message_tts(cog, message, guild_defaults={}, active_prefix=".", forced_engine="gtts")

        self.assertTrue(result.enqueued)
        self.assertEqual(cog._state.last_text_channel_id, 99)
        self.assertEqual(len(cog.enqueue_calls), 1)
        self.assertEqual(cog.enqueue_calls[0][1].text, "teste")


if __name__ == "__main__":
    unittest.main()
