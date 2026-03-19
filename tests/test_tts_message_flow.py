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
        discord.Object = type("Object", (), {"__init__": lambda self, id: setattr(self, "id", id)})
        discord.Embed = type("Embed", (), {"__init__": lambda self, *a, **k: None})
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
        app_commands.guilds = lambda *_objs: (lambda func: func)
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

    if "gtts.tts" not in sys.modules:
        gtts_tts = types.ModuleType("gtts.tts")
        gtts_tts.gTTSError = type("gTTSError", (Exception,), {})
        sys.modules["gtts.tts"] = gtts_tts

    if "gtts.lang" not in sys.modules:
        gtts_lang = types.ModuleType("gtts.lang")
        gtts_lang.tts_langs = lambda: {"pt": "Portuguese", "en": "English"}
        sys.modules["gtts.lang"] = gtts_lang

    if "google" not in sys.modules:
        sys.modules["google"] = types.ModuleType("google")
    if "google.cloud" not in sys.modules:
        sys.modules["google.cloud"] = types.ModuleType("google.cloud")
    if "google.cloud.texttospeech_v1" not in sys.modules:
        sys.modules["google.cloud.texttospeech_v1"] = types.ModuleType("google.cloud.texttospeech_v1")


_install_runtime_stubs()

from cogs.tts.audio import QueueItem
from cogs.tts.utils.message_dispatch import dispatch_message_tts
from cogs.tts.utils.message_gate import analyze_message_for_tts


class FakeDB:
    def __init__(self, *, guild_defaults=None, resolved=None):
        self.guild_defaults = guild_defaults or {}
        self.resolved = resolved or {}

    async def get_guild_tts_defaults(self, guild_id: int):
        return dict(self.guild_defaults)

    async def resolve_tts(self, guild_id: int, user_id: int):
        return dict(self.resolved)


class FakeCog:
    def __init__(self, *, db=None):
        self._db = db
        self._state = SimpleNamespace(last_text_channel_id=None)
        self.enqueue_calls = []
        self.prefix_calls = []

    def _get_db(self):
        return self._db

    async def _maybe_await(self, value):
        if asyncio.iscoroutine(value):
            return await value
        return value

    def _render_tts_text(self, message, text: str) -> str:
        return text.strip()

    def _apply_author_prefix_if_needed(self, guild_id, author, text: str, *, enabled: bool):
        return f"{author.display_name} disse {text}" if enabled else text

    def _guild_announce_author_enabled(self, guild_defaults):
        return bool((guild_defaults or {}).get("announce_author"))

    def _get_state(self, guild_id: int):
        return self._state

    async def _enqueue_tts_item(self, guild_id: int, item: QueueItem):
        self.enqueue_calls.append((guild_id, item))
        return True, 0, False

    async def _handle_message_prefix_command(self, message, command):
        self.prefix_calls.append((message, command))


def make_message(content: str, *, in_call: bool = True, channel_id: int = 777, guild_id: int = 123, user_id: int = 456):
    voice = SimpleNamespace(channel=SimpleNamespace(id=999)) if in_call else None
    author = SimpleNamespace(id=user_id, bot=False, display_name="Dilma", voice=voice)
    guild = SimpleNamespace(id=guild_id)
    channel = SimpleNamespace(id=channel_id)
    return SimpleNamespace(content=content, author=author, guild=guild, channel=channel)


class MessageFlowSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefix_command_path_detected(self):
        cog = FakeCog(db=FakeDB(guild_defaults={"bot_prefix": "_"}))
        message = make_message("_join")

        decision = await analyze_message_for_tts(cog, message)

        self.assertFalse(decision.should_process_tts)
        self.assertTrue(decision.should_dispatch_prefix_command)
        self.assertEqual(decision.reason, "prefix_command")
        self.assertEqual(decision.prefix_command.kind, "join")

    async def test_tts_message_enqueues_through_dispatch(self):
        cog = FakeCog(db=FakeDB(guild_defaults={"tts_prefix": "."}, resolved={"engine": "edge", "voice": "pt-BR-FranciscaNeural", "rate": "+0%", "pitch": "+0Hz"}))
        message = make_message(".olá mundo")

        decision = await analyze_message_for_tts(cog, message)
        self.assertTrue(decision.should_process_tts)
        self.assertEqual(decision.forced_engine, "gtts")

        result = await dispatch_message_tts(
            cog,
            message,
            guild_defaults=decision.guild_defaults,
            active_prefix=decision.active_prefix,
            forced_engine=decision.forced_engine,
        )

        self.assertTrue(result.enqueued)
        self.assertIsNotNone(result.payload)
        self.assertEqual(len(cog.enqueue_calls), 1)
        self.assertEqual(cog._state.last_text_channel_id, message.channel.id)
        _, queue_item = cog.enqueue_calls[0]
        self.assertEqual(queue_item.text, "olá mundo")
        self.assertEqual(queue_item.engine, "gtts")

    async def test_tts_message_without_voice_channel_aborts_before_enqueue(self):
        cog = FakeCog(db=FakeDB(guild_defaults={"tts_prefix": "."}, resolved={"engine": "gtts", "language": "pt-br"}))
        message = make_message(".sem call", in_call=False)

        decision = await analyze_message_for_tts(cog, message)
        result = await dispatch_message_tts(
            cog,
            message,
            guild_defaults=decision.guild_defaults,
            active_prefix=decision.active_prefix,
            forced_engine=decision.forced_engine,
        )

        self.assertFalse(result.enqueued)
        self.assertIsNone(result.payload)
        self.assertEqual(cog.enqueue_calls, [])
        self.assertIsNone(cog._state.last_text_channel_id)

    async def test_message_without_tts_prefix_stops_in_gate(self):
        cog = FakeCog(db=FakeDB(guild_defaults={"tts_prefix": "."}))
        message = make_message("olá sem prefixo")

        decision = await analyze_message_for_tts(cog, message)

        self.assertFalse(decision.should_process_tts)
        self.assertFalse(decision.should_dispatch_prefix_command)
        self.assertEqual(decision.reason, "no_engine_prefix")


if __name__ == "__main__":
    unittest.main()
