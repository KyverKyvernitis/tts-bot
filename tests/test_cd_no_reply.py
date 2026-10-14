"""Exercise CD failures through both real Discord message entry paths.

The HTTP consumer is local: it serializes the SDK payload and reproduces a
Discord rejection without sending messages or contacting the Discord API.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord.http import handle_message_parameters
import pytest

import config
from cogs.tts.cog import TTSVoice
from cogs.tts.interface import painel_canto


_PRIVATE_ERROR = "private-token-do-not-print https://example.invalid/private?token=secret"


class _DB:
    def __init__(self, prefix="_"):
        self.prefix = prefix

    def get_guild_tts_defaults(self, _guild_id):
        # Configuration remains available while synthesis is disabled.
        return {"bot_prefix": self.prefix, "enabled": False}

    def resolve_tts(self, _guild_id, _user_id):
        return {}


def _actual_bot_entry():
    """Use the deployed entry methods without running bot.py startup services."""
    source = ast.parse((Path(__file__).resolve().parents[1] / "bot.py").read_text())
    bot_class = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "BotLocal")
    methods = [copy.deepcopy(node) for node in bot_class.body if isinstance(node, ast.AsyncFunctionDef)
               and node.name in {"on_message", "_dispatch_tts_message_bridge"}]
    assert len(methods) == 2
    module = ast.Module(body=[ast.ClassDef(name="ActualBotEntry", bases=[], keywords=[],
                                         body=methods, decorator_list=[])], type_ignores=[])
    namespace = {"discord": discord, "logging": logging}
    exec(compile(ast.fix_missing_locations(module), "bot.py", "exec"), namespace)
    return namespace["ActualBotEntry"]()


def _fixture(send, *, prefix="_"):
    bot = _actual_bot_entry()
    bot.ZIP_UPDATE_CHANNEL_ID = 987654
    bot.settings_db = _DB(prefix)
    bot.user = SimpleNamespace(id=900, display_avatar=SimpleNamespace(
        url="https://cdn.discordapp.com/embed/avatars/0.png"))
    bot.process_commands = AsyncMock()
    bot.get_context = AsyncMock(return_value=SimpleNamespace(valid=False))
    cog = TTSVoice(bot)
    bot.get_cog = lambda name: cog if name == "TTSVoice" else None
    cog._ttv_voice_status = lambda *_args, **_kwargs: {"ready": False, "online": False}
    cog._tts_phone_worker_online_for_ui = lambda: False
    cog._ensure_connected = AsyncMock(side_effect=AssertionError("CD must not join voice"))
    cog._get_phone_worker_json = AsyncMock(side_effect=AssertionError("CD must not request worker synthesis"))
    cog._enqueue_tts = AsyncMock(side_effect=AssertionError("CD must not enqueue speech"))
    channel = SimpleNamespace(id=30, send=send)

    def message(message_id=77):
        return SimpleNamespace(id=message_id, content=prefix + "cd", guild=SimpleNamespace(id=20),
                               author=SimpleNamespace(id=10, bot=False, voice=None), channel=channel)

    return bot, cog, message


async def _dispatch_both(bot, cog, message):
    # Discord runs the Cog listener alongside bot.on_message. Both must share
    # one panel attempt and, after a failure, one human-readable response.
    await asyncio.gather(bot.on_message(message), cog.on_message(message))


def _serialize(kwargs):
    with handle_message_parameters(**kwargs) as request:
        return request.payload


def _send_parameters(args, kwargs):
    """Messageable.send accepts its content argument positionally."""
    assert len(args) <= 1
    parameters = dict(kwargs)
    if args:
        assert "content" not in parameters
        parameters["content"] = args[0]
    return parameters


def _walk_components(components):
    for component in components:
        yield component
        yield from _walk_components(component.get("components", ()))
        accessory = component.get("accessory")
        if accessory:
            yield accessory


def _validate_api_payload(payload):
    """Reject the text checkmark exactly where Discord rejects the whole V2 message."""
    for component in _walk_components(payload.get("components", ())):
        if component.get("type") == 2 and component.get("emoji", {}).get("name") == "✓":
            raise _http_failure()


def _http_failure(*, forbidden=False):
    response = SimpleNamespace(status=403 if forbidden else 400, reason="Forbidden" if forbidden else "Bad Request")
    return (discord.Forbidden if forbidden else discord.HTTPException)(response, {
        "code": 50013 if forbidden else 50035,
        "message": _PRIVATE_ERROR,
        "errors": {"components": {"_errors": [{"code": "BUTTON_COMPONENT_INVALID_EMOJI", "message": _PRIVATE_ERROR}]}},
    })


def _assert_no_audio(cog):
    cog._ensure_connected.assert_not_awaited()
    cog._get_phone_worker_json.assert_not_awaited()
    cog._enqueue_tts.assert_not_awaited()


def _assert_safe_fallback(kwargs):
    assert "view" not in kwargs and "embed" not in kwargs and "embeds" not in kwargs
    content = str(kwargs.get("content", ""))
    assert content and "painel" in content.lower()
    assert _PRIVATE_ERROR not in content
    assert "private-token" not in content and "example.invalid" not in content
    assert kwargs["allowed_mentions"].to_dict()["parse"] == []


@pytest.mark.parametrize("prefix", ["_", "!"])
def test_real_bot_and_cog_listener_send_one_api_valid_v2_panel(monkeypatch, prefix):
    monkeypatch.setattr(config, "TTS_ENABLED", True)

    async def scenario():
        payloads = []

        async def consume(*args, **kwargs):
            kwargs = _send_parameters(args, kwargs)
            payload = _serialize(kwargs)
            _validate_api_payload(payload)
            payloads.append(payload)
            return SimpleNamespace(id=990, delete=AsyncMock())

        send = AsyncMock(side_effect=consume)
        bot, cog, message = _fixture(send, prefix=prefix)
        await _dispatch_both(bot, cog, message())
        assert send.await_count == 1 and bot.process_commands.await_count == 1
        payload = payloads[0]
        assert discord.MessageFlags._from_value(payload["flags"]).components_v2
        assert payload["components"][0]["type"] == 17
        assert not payload.get("content") and not payload.get("embeds")
        buttons = [row for row in _walk_components(payload["components"]) if row.get("type") == 2]
        assert {row["emoji"]["name"] for row in buttons} == {"▶️", "✅"}
        assert payload["allowed_mentions"]["parse"] == []
        view = send.await_args.kwargs["view"]
        assert cog._active_prefix_panels[(20, 10, "cd")] is view.message
        assert cog._public_panel_states[990] == {"panel_kind": "cd", "owner_id": 10}
        _assert_no_audio(cog)
        view.stop()

    asyncio.run(scenario())


def test_local_consumer_rejects_original_text_checkmark_as_http_400():
    payload = {"components": [{"type": 17, "components": [{"type": 1, "components": [
        {"type": 2, "emoji": {"name": "✓"}}
    ]}]}]}
    with pytest.raises(discord.HTTPException) as caught:
        _validate_api_payload(payload)
    assert caught.value.status == 400 and caught.value.code == 50035


@pytest.mark.parametrize("prefix", ["_", "!"])
def test_api_rejection_replies_once_retires_old_panel_and_allows_immediate_retry(monkeypatch, caplog, prefix):
    monkeypatch.setattr(config, "TTS_ENABLED", True)

    async def scenario():
        attempts = []
        reject = True
        old = SimpleNamespace(id=400, delete=AsyncMock())

        async def consume(*args, **kwargs):
            kwargs = _send_parameters(args, kwargs)
            attempts.append(kwargs)
            if "view" in kwargs:
                _validate_api_payload(_serialize(kwargs))
                old.delete.assert_awaited_once()
                if reject:
                    raise _http_failure()
                return SimpleNamespace(id=991, delete=AsyncMock())
            _assert_safe_fallback(kwargs)
            return SimpleNamespace(id=990)

        send = AsyncMock(side_effect=consume)
        bot, cog, message = _fixture(send, prefix=prefix)
        key = (20, 10, "cd")
        cog._active_prefix_panels[key] = old
        cog._public_panel_states[old.id] = {"panel_kind": "cd", "owner_id": 10}
        await _dispatch_both(bot, cog, message())
        assert len(attempts) == 2
        _assert_safe_fallback(attempts[1])
        assert f"`{prefix}cd`" in attempts[1]["content"]
        assert attempts[0]["view"].is_finished()
        assert key not in cog._prefix_panel_cooldowns
        assert key not in cog._active_prefix_panels and old.id not in cog._public_panel_states
        old.delete.assert_awaited_once()
        reject = False
        await _dispatch_both(bot, cog, message(78))
        assert len(attempts) == 3 and "view" in attempts[2]
        old.delete.assert_awaited_once()
        assert old.id not in cog._public_panel_states
        assert cog._active_prefix_panels[key].id == 991
        _assert_no_audio(cog)
        attempts[2]["view"].stop()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert _PRIVATE_ERROR not in caplog.text and "private-token" not in caplog.text


@pytest.mark.parametrize("failure_stage", ["import", "constructor", "refresh"])
def test_lazy_panel_errors_receive_safe_reply_from_real_entry(monkeypatch, failure_stage):
    monkeypatch.setattr(config, "TTS_ENABLED", True)

    async def scenario():
        created = []
        real_class = painel_canto.CDPanelView
        if failure_stage == "import":
            import builtins
            original_import = builtins.__import__

            def fail_import(name, *args, **kwargs):
                if name == "interface.painel_canto":
                    raise ImportError(_PRIVATE_ERROR)
                return original_import(name, *args, **kwargs)

            monkeypatch.setattr(builtins, "__import__", fail_import)
        else:
            def construct(*args, **kwargs):
                if failure_stage == "constructor":
                    raise ValueError(_PRIVATE_ERROR)
                view = real_class(*args, **kwargs)
                created.append(view)
                view.refresh = AsyncMock(side_effect=RuntimeError(_PRIVATE_ERROR))
                return view

            monkeypatch.setattr(painel_canto, "CDPanelView", construct)
        send = AsyncMock(return_value=SimpleNamespace(id=990))
        bot, cog, message = _fixture(send)
        await _dispatch_both(bot, cog, message())
        assert send.await_count == 1
        _assert_safe_fallback(_send_parameters(send.await_args.args, send.await_args.kwargs))
        assert not cog._active_prefix_panels and not cog._prefix_panel_cooldowns
        assert all(view.is_finished() for view in created)
        _assert_no_audio(cog)

    asyncio.run(scenario())


def test_forbidden_panel_and_forbidden_notice_are_classified_without_leaking_http_body(monkeypatch, caplog):
    monkeypatch.setattr(config, "TTS_ENABLED", True)

    async def scenario():
        attempts = []

        async def consume(*args, **kwargs):
            kwargs = _send_parameters(args, kwargs)
            attempts.append(kwargs)
            if "view" not in kwargs:
                _assert_safe_fallback(kwargs)
            raise _http_failure(forbidden=True)

        send = AsyncMock(side_effect=consume)
        bot, cog, message = _fixture(send)
        await _dispatch_both(bot, cog, message())
        assert len(attempts) == 2
        assert attempts[0]["view"].is_finished()
        assert not cog._active_prefix_panels and not cog._prefix_panel_cooldowns
        _assert_no_audio(cog)

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert caplog.records
    assert "Forbidden" in caplog.text
    assert _PRIVATE_ERROR not in caplog.text and "private-token" not in caplog.text


def test_cancellation_stops_unsent_view_without_sending_failure_message(monkeypatch):
    monkeypatch.setattr(config, "TTS_ENABLED", True)

    async def scenario():
        views = []

        async def consume(*args, **kwargs):
            kwargs = _send_parameters(args, kwargs)
            views.append(kwargs["view"])
            raise asyncio.CancelledError()

        send = AsyncMock(side_effect=consume)
        bot, cog, message = _fixture(send)
        with pytest.raises(asyncio.CancelledError):
            await _dispatch_both(bot, cog, message())
        await asyncio.sleep(0)
        assert send.await_count == 1 and len(views) == 1
        assert views[0].is_finished()
        assert not cog._active_prefix_panels and not cog._prefix_panel_cooldowns
        _assert_no_audio(cog)

    asyncio.run(scenario())
