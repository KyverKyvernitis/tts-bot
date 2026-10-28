"""Import commands retain their attachment across TTS and updater entry paths."""
from __future__ import annotations

import ast
import asyncio
from collections import OrderedDict
import copy
import inspect
import logging
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord.http import handle_message_parameters
import pytest

import config
from cogs.tts.mensagens.triagem import analisar_mensagem_para_tts, match_import_cd_message


ROOT = Path(__file__).resolve().parents[1]


def _entry_class(path, source_class, methods, output_class):
    parsed = ast.parse((ROOT / path).read_text())
    parent = next(node for node in parsed.body if isinstance(node, ast.ClassDef) and node.name == source_class)
    selected = [copy.deepcopy(node) for node in parent.body
                if isinstance(node, ast.AsyncFunctionDef) and node.name in methods]
    assert len(selected) == len(methods)
    for method in selected:
        method.decorator_list = []
    module = ast.Module(body=[ast.ClassDef(name=output_class, bases=[], keywords=[],
        body=selected, decorator_list=[])], type_ignores=[])
    namespace = {"discord": discord, "asyncio": asyncio, "OrderedDict": OrderedDict,
                 "time": time, "logging": logging}
    exec(compile(ast.fix_missing_locations(module), path, "exec"), namespace)
    return namespace[output_class]


_BotEntry = _entry_class("bot.py", "BotLocal",
    {"on_message", "_dispatch_tts_message_bridge"}, "BotEntry")
_CogEntry = _entry_class("cogs/tts/cog.py", "TTSVoice", {"on_message"}, "CogEntry")


def _message(content="_importcd", *, guild=True, channel_id=123, bot=False):
    return SimpleNamespace(id=77, content=content,
        guild=SimpleNamespace(id=20) if guild else None,
        author=SimpleNamespace(id=10, bot=bot),
        channel=SimpleNamespace(id=channel_id), attachments=[], reference=None)


def _cog(prefix="_"):
    cog = SimpleNamespace()
    db = SimpleNamespace(get_guild_tts_defaults=Mock(return_value={"bot_prefix": prefix, "enabled": False}))
    cog._get_db = Mock(return_value=db)

    async def maybe_await(value):
        return await value if inspect.isawaitable(value) else value

    cog._maybe_await = maybe_await
    cog.bot = SimpleNamespace(get_context=AsyncMock(return_value=SimpleNamespace(valid=False)))
    return cog


@pytest.mark.asyncio
@pytest.mark.parametrize("content,prefix", [("_importcd", "_"), ("  !IMPORTCD  ", "!"), ("os!importcd", "os!")])
async def test_import_routes_without_synthesis_or_attachment_even_when_tts_disabled(monkeypatch, content, prefix):
    monkeypatch.setattr(config, "TTS_ENABLED", False)
    cog = _cog(prefix)
    decision = await analisar_mensagem_para_tts(cog, _message(content))
    assert decision.should_dispatch_prefix_command
    assert decision.prefix_command.kind == "importcd"
    assert not decision.should_process_tts
    cog.bot.get_context.assert_not_awaited()
    cog._get_db.return_value.get_guild_tts_defaults.assert_called_once_with(20)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["mensagem comum", "_cd", "_rcd", "_importcdmore outra", "'canto~"])
async def test_disabled_ordinary_paths_keep_their_zero_database_cost(monkeypatch, content):
    monkeypatch.setattr(config, "TTS_ENABLED", False)
    cog = _cog()
    cog._get_db.side_effect = AssertionError("ordinary disabled messages must not read guild configuration")
    decision = await analisar_mensagem_para_tts(cog, _message(content))
    assert not decision.should_dispatch_prefix_command and not decision.should_process_tts
    assert decision.reason == "tts_disabled"


@pytest.mark.asyncio
async def test_direct_messages_use_only_the_default_prefix_and_never_read_guild_data(monkeypatch):
    monkeypatch.setattr(config, "TTS_ENABLED", True)
    monkeypatch.setattr(config, "BOT_PREFIX", "_", raising=False)
    cog = _cog()
    cog._get_db.side_effect = AssertionError("DM has no guild defaults")
    accepted = await analisar_mensagem_para_tts(cog, _message(guild=False))
    assert accepted.should_dispatch_prefix_command and accepted.prefix_command.kind == "importcd"
    for content in ("!importcd", "_cd", "'olá~", "_importcdmore outra"):
        ignored = await analisar_mensagem_para_tts(cog, _message(content, guild=False))
        assert ignored.reason == "no_guild" and not ignored.should_dispatch_prefix_command


@pytest.mark.asyncio
async def test_bot_and_antibot_guards_run_before_import_configuration(monkeypatch):
    monkeypatch.setattr(config, "TTS_ENABLED", False)
    cog = _cog()
    cog._get_db.side_effect = AssertionError("guard must run before database")
    assert await match_import_cd_message(cog, _message(bot=True)) is None
    cog.bot.antibot_should_block_message = lambda _: True
    assert await match_import_cd_message(cog, _message()) is None
    decision = await analisar_mensagem_para_tts(cog, _message())
    assert not decision.should_dispatch_prefix_command


@pytest.mark.asyncio
@pytest.mark.parametrize("content,prefix", [("_importcd", "_"), ("!IMPORTCD", "!")])
async def test_update_channel_import_skips_code_updater_and_registered_commands(monkeypatch, content, prefix):
    monkeypatch.setattr(config, "TTS_ENABLED", False)
    bot = _BotEntry()
    bot.ZIP_UPDATE_CHANNEL_ID = 123
    cog = _cog(prefix)
    cog.handle_message_from_bot_on_message = AsyncMock()
    bot.get_cog = Mock(return_value=cog)
    bot._handle_zip_update_message = AsyncMock()
    bot.process_commands = AsyncMock()
    message = _message(content)
    attachment = SimpleNamespace(filename="cd.zip", read=AsyncMock())
    message.attachments = [attachment]
    await bot.on_message(message)
    cog.handle_message_from_bot_on_message.assert_awaited_once_with(message)
    bot._handle_zip_update_message.assert_not_awaited()
    bot.process_commands.assert_not_awaited()
    attachment.read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "Atualização", "_importcdmore outra", "!importcd"])
async def test_other_update_attachments_keep_the_existing_updater_path(content):
    bot = _BotEntry()
    bot.ZIP_UPDATE_CHANNEL_ID = 123
    cog = _cog()
    cog.handle_message_from_bot_on_message = AsyncMock()
    bot.get_cog = Mock(return_value=cog)
    bot._handle_zip_update_message = AsyncMock()
    bot.process_commands = AsyncMock()
    message = _message(content)
    message.attachments = [SimpleNamespace(filename="patch.zip")]
    await bot.on_message(message)
    bot._handle_zip_update_message.assert_awaited_once_with(message)
    cog.handle_message_from_bot_on_message.assert_not_awaited()
    bot.process_commands.assert_not_awaited()
    if not content.casefold().endswith("importcd"):
        cog._get_db.assert_not_called()


@pytest.mark.asyncio
async def test_update_channel_and_parallel_cog_listener_share_one_import(monkeypatch):
    monkeypatch.setattr(config, "TTS_ENABLED", False)
    bot = _BotEntry()
    bot.ZIP_UPDATE_CHANNEL_ID = 123
    fixture = _cog()
    cog = _CogEntry()
    cog._get_db, cog._maybe_await, cog.bot = fixture._get_db, fixture._maybe_await, fixture.bot
    completed = []

    async def process(message):
        decision = await analisar_mensagem_para_tts(cog, message)
        await asyncio.sleep(0)
        completed.append(decision.prefix_command.kind)

    cog._process_tts_message = process
    cog.handle_message_from_bot_on_message = cog.on_message
    bot.get_cog = lambda name: cog if name == "TTSVoice" else None
    bot._handle_zip_update_message = AsyncMock()
    bot.process_commands = AsyncMock()
    message = _message()
    await asyncio.gather(bot.on_message(message), cog.on_message(message))
    assert completed == ["importcd"]
    bot._handle_zip_update_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_guild_import_stays_a_control_command_with_no_speech(monkeypatch):
    monkeypatch.setattr(config, "TTS_ENABLED", True)
    cog = _cog()
    decision = await analisar_mensagem_para_tts(cog, _message())
    assert decision.should_dispatch_prefix_command and decision.prefix_command.kind == "importcd"
    assert not decision.should_process_tts
    cog.bot.get_context.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("content,prefix,title", [
    ("_importcd Tetoris", "_", "Tetoris"),
    (" !IMPORTCD\tLooping the Rooms  ", "!", "Looping the Rooms"),
    ("os!importcd\nテトリス", "os!", "テトリス"),
])
async def test_title_caption_is_routed_before_speech_even_when_disabled(monkeypatch, content, prefix, title):
    monkeypatch.setattr(config, "TTS_ENABLED", False)
    decision = await analisar_mensagem_para_tts(_cog(prefix), _message(content))
    assert decision.should_dispatch_prefix_command and not decision.should_process_tts
    assert decision.prefix_command.kind == "importcd"
    assert decision.prefix_command.argument == title


@pytest.mark.asyncio
async def test_title_caption_in_update_channel_cannot_become_a_code_update(monkeypatch):
    monkeypatch.setattr(config, "TTS_ENABLED", False)
    bot = _BotEntry()
    bot.ZIP_UPDATE_CHANNEL_ID = 123
    cog = _cog()
    cog.handle_message_from_bot_on_message = AsyncMock()
    bot.get_cog = Mock(return_value=cog)
    bot._handle_zip_update_message = AsyncMock()
    bot.process_commands = AsyncMock()
    message = _message("_importcd Looping the Rooms")
    message.attachments = [SimpleNamespace(filename="download.zip", read=AsyncMock())]
    await bot.on_message(message)
    cog.handle_message_from_bot_on_message.assert_awaited_once_with(message)
    bot._handle_zip_update_message.assert_not_awaited()
    bot.process_commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_title_caption_with_missing_cog_is_never_sent_to_code_updater():
    bot = _BotEntry()
    bot.ZIP_UPDATE_CHANNEL_ID = 123
    bot.get_cog = Mock(return_value=None)
    bot._handle_zip_update_message = AsyncMock()
    message = _message("_importcd Tetoris")
    message.channel.send = AsyncMock()
    await bot.on_message(message)
    message.channel.send.assert_awaited_once()
    bot._handle_zip_update_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["_", "!", "os!", "12345678"])
async def test_missing_tts_cog_never_delivers_explicit_import_zip_to_code_updater(prefix):
    bot = _BotEntry()
    bot.ZIP_UPDATE_CHANNEL_ID = 123
    bot.get_cog = Mock(return_value=None)
    bot._handle_zip_update_message = AsyncMock()
    bot.process_commands = AsyncMock()
    message = _message(prefix + "IMPORTCD")
    message.channel.send = AsyncMock()
    # This fallback cannot authorize an import and never touches the attachment.
    message.author.guild_permissions = SimpleNamespace(administrator=True)
    attachment = SimpleNamespace(filename="cd.zip", read=AsyncMock())
    message.attachments = [attachment]
    await bot.on_message(message)
    message.channel.send.assert_awaited_once()
    kwargs = message.channel.send.await_args.kwargs
    with handle_message_parameters(**kwargs) as serialized:
        payload = serialized.payload
    assert discord.MessageFlags._from_value(payload["flags"]).components_v2
    assert not payload.get("content") and not payload.get("embeds")
    assert payload["allowed_mentions"]["parse"] == []
    assert "Importação indisponível" in payload["components"][0]["components"][0]["content"]
    attachment.read.assert_not_awaited()
    bot._handle_zip_update_message.assert_not_awaited()
    bot.process_commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_cog_notice_timeout_still_cannot_reach_updater():
    bot = _BotEntry()
    bot.ZIP_UPDATE_CHANNEL_ID = 123
    bot.get_cog = Mock(return_value=None)
    bot._handle_zip_update_message = AsyncMock()
    message = _message()
    message.channel.send = AsyncMock(side_effect=asyncio.TimeoutError)
    await bot.on_message(message)
    bot._handle_zip_update_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["importcd", "123456789importcd", "texto com importcd", "_importcdmore outra"])
async def test_missing_cog_fallback_does_not_capture_update_prose(content):
    bot = _BotEntry()
    bot.ZIP_UPDATE_CHANNEL_ID = 123
    bot.get_cog = Mock(return_value=None)
    bot._handle_zip_update_message = AsyncMock()
    message = _message(content)
    message.channel.send = AsyncMock()
    await bot.on_message(message)
    bot._handle_zip_update_message.assert_awaited_once_with(message)
    message.channel.send.assert_not_awaited()
