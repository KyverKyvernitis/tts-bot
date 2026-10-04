"""Fronteiras dos módulos: efeitos confirmados, acesso atual e ausência de replay."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.module_tools import register_module_tools
from cogs.chatbot.tool_registry import ToolRegistry


def setup_modules(*, image=True, music=False, tickets=False):
    permissions = NS(view_channel=True, attach_files=True)
    text = NS(id=10, permissions_for=Mock(return_value=permissions))
    voice = NS(id=20, permissions_for=Mock(return_value=permissions))
    member = NS(id=3, display_name="Pessoa", voice=NS(channel=voice))
    guild = NS(id=1, me=NS(id=2), fetch_member=AsyncMock(return_value=member),
               voice_client=NS(channel=voice), get_channel=Mock(return_value=text))
    async def fresh_member(member_id):
        return member if member_id == member.id else guild.me
    guild.fetch_member.side_effect = fresh_member
    message = NS(id=40, guild=guild, channel=text, author=member, attachments=[],
                 reply=AsyncMock(return_value=NS(id=50)))
    state = NS(current=NS(short_title="Faixa", duration_label="2:00"), paused=False)
    router = NS(get_state=Mock(return_value=state), snapshot_queue=Mock(return_value=[]),
                pause=AsyncMock(return_value=True), resume=AsyncMock(return_value=True),
                request_skip=AsyncMock(return_value=(False, "Voto registrado.")))
    music_cog = NS(router=router, _music_agent_default_enabled=lambda: False)
    cfg = {"panel": {"channel_id": 10, "message_id": 77}}
    ticket_cog = NS(_get_config=Mock(return_value=cfg), _feature_active=lambda cfg: True,
                    _find_user_open_ticket=Mock(return_value={"channel_id": 10, "kind": "suporte"}))
    cogs = {"Music": music_cog if music else None, "TicketsCog": ticket_cog if tickets else None}
    bot = NS(user=NS(id=2), get_cog=lambda name: cogs.get(name))
    service = NS(generate=AsyncMock(return_value=NS(ok=True, provider="fake", reason=None,
                image=NS(mime_type="image/png", data=b"confirmed-image-bytes")))) if image else None
    cog = NS(bot=bot, _image_service=service, _session=None,
             _remember_sent_message=AsyncMock(), _maybe_transcribe=AsyncMock())
    guard = AsyncMock()
    registry = ToolRegistry()
    register_module_tools(registry, cog, message, NS(), epoch=None,
                          visibility_scope="channel:10", guard=guard)
    return NS(registry=registry, cog=cog, message=message, guild=guild, member=member,
              router=router, permissions=permissions, guard=guard, ticket_cog=ticket_cog)


@pytest.mark.asyncio
async def test_image_exact_prompt_single_confirmed_send_and_no_replay(monkeypatch):
    monkeypatch.setattr(C, "SAFE_MODE", False)
    env = setup_modules()
    tool = env.registry.get("generate_image")
    result = await tool.handler({"prompt": "um gato no espaço"})
    assert result["status"] == "image_sent"
    assert await tool.handler({"prompt": "um gato no espaço"}) == result
    env.cog._image_service.generate.assert_awaited_once_with(
        prompt="um gato no espaço", channel_is_nsfw=False,
    )
    env.message.reply.assert_awaited_once()
    assert env.message.reply.call_args.kwargs["file"].fp.getvalue() == b"confirmed-image-bytes"
    assert env.guard.await_count == 3


@pytest.mark.asyncio
async def test_revoked_access_after_generation_cannot_publish():
    env = setup_modules()
    env.guard.side_effect = [None, ValueError("revoked")]
    with pytest.raises(ValueError):
        await env.registry.get("generate_image").handler({"prompt": "gato"})
    env.message.reply.assert_not_awaited()
    second = await env.registry.get("generate_image").handler({"prompt": "gato"})
    assert second["status"] == "uncertain"
    assert env.cog._image_service.generate.await_count == 1


@pytest.mark.asyncio
async def test_image_uncertain_network_send_never_replayed():
    env = setup_modules()
    env.message.reply.side_effect = OSError("lost response")
    handler = env.registry.get("generate_image").handler
    assert (await handler({"prompt": "gato"}))["status"] == "uncertain"
    assert (await handler({"prompt": "gato"}))["status"] == "uncertain"
    env.message.reply.assert_awaited_once()
    env.cog._remember_sent_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError("índice fora do ar"), asyncio.CancelledError()])
async def test_confirmed_image_has_immediate_receipt_and_metadata_cannot_trigger_replay(failure):
    env = setup_modules()
    env.cog.note_public_delivery = Mock()

    async def metadata(**kwargs):
        env.cog.note_public_delivery.assert_called_once_with(50)
        raise failure

    env.cog._remember_sent_message.side_effect = metadata
    handler = env.registry.get("generate_image").handler
    result = await handler({"prompt": "gato"})

    assert result["status"] == "image_sent" and result["data"]["message_id"] == "50"
    assert await handler({"prompt": "gato"}) == result
    env.message.reply.assert_awaited_once()
    env.cog._image_service.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_during_final_bot_fetch_cannot_publish_image():
    env = setup_modules()
    calls = 0

    async def fresh_member(member_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            env.guard.side_effect = ValueError("epoch reset")
        return env.guild.me

    env.guild.fetch_member.side_effect = fresh_member
    with pytest.raises(ValueError):
        await env.registry.get("generate_image").handler({"prompt": "gato"})
    env.message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_nsfw_channel_conditions_cannot_publish_adult_image(monkeypatch):
    env = setup_modules()
    monkeypatch.setattr(C, "nsfw_enabled_for_guild", lambda gid: True)
    env.message.channel.nsfw = True
    result = env.cog._image_service.generate.return_value
    result.prompt_class = "adult_allowed"

    async def changed_channel(**arguments):
        env.guild.get_channel.return_value = NS(id=10, nsfw=False)
        return result

    env.cog._image_service.generate.side_effect = changed_channel
    reply = await env.registry.get("generate_image").handler({"prompt": "imagem adulta"})
    assert reply["status"] == "unavailable"
    env.message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_music_same_call_policy_and_vote_are_preserved():
    env = setup_modules(music=True)
    handler = env.registry.get("control_music").handler
    result = await handler({"action": "skip"})
    assert result["status"] == "music_vote_or_denied"
    assert result["data"]["notice"] == "Voto registrado."
    env.router.request_skip.assert_awaited_once_with(1, env.member)
    assert await handler({"action": "skip"}) == result
    env.member.voice.channel.id = 21
    env.guild.voice_client.channel = NS(id=20)
    refused = await handler({"action": "pause"})
    assert refused["status"] == "unavailable"
    env.router.pause.assert_not_awaited()


@pytest.mark.asyncio
async def test_music_session_move_during_worker_check_does_not_control_new_call():
    env = setup_modules(music=True)
    env.router.music_worker_only_enabled = lambda: True

    async def available():
        env.guild.voice_client.channel = NS(id=999)
        return NS(available=True)

    env.router.ensure_music_worker_available = available
    result = await env.registry.get("control_music").handler({"action": "pause"})
    assert result["status"] == "unavailable"
    env.router.pause.assert_not_awaited()


@pytest.mark.asyncio
async def test_tickets_do_not_disclose_inaccessible_ticket_or_panel():
    env = setup_modules(tickets=True)
    env.permissions.view_channel = False
    result = await env.registry.get("get_own_tickets").handler({})
    assert result["data"]["open_ticket"] is None
    assert result["data"]["public_panel"] is None
    env.ticket_cog._find_user_open_ticket.assert_called_once_with(
        env.ticket_cog._get_config.return_value, 3,
    )
    assert env.registry.get("manage_ticket").available is False


def test_unavailable_modules_have_real_reasons_and_no_callable_mutation():
    env = setup_modules(image=False)
    for name in ("generate_image", "get_music_state", "control_music", "get_own_tickets", "manage_ticket"):
        spec = env.registry.get(name)
        assert not spec.available and spec.why
    assert env.registry.get("manage_ticket").handler is None
