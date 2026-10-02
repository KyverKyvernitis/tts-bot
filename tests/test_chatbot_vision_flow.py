"""Contratos do turno com anexos: escopo, visão, erros e memória textual."""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.cog import ChatbotCog, TriggerInfo
from cogs.chatbot.media import ImagePreparationError, PreparedImage
from cogs.chatbot.memory import MemoryEpoch
from cogs.chatbot.providers import AllProvidersExhausted


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Wl2mJAAAAAASUVORK5CYII="
)


def attachment(name: str, *, signature: str = "old", size: int = 100):
    return SimpleNamespace(
        url=f"https://cdn.discordapp.com/attachments/20/30/{name}?ex={signature}",
        filename=name, content_type="image/png", size=size,
    )


def source(*attachments, message_id=44, guild_id=10, channel_id=20, content=""):
    return SimpleNamespace(
        id=message_id, guild=SimpleNamespace(id=guild_id),
        channel=SimpleNamespace(id=channel_id), content=content,
        attachments=list(attachments),
        author=SimpleNamespace(id=41, name="bia", display_name="Bia"),
    )


@pytest.fixture
def turn():
    epoch = MemoryEpoch(global_generation=2, guild_generation=3, user_generation=4)
    cog = object.__new__(ChatbotCog)
    cog.bot = SimpleNamespace(user=SimpleNamespace(id=999), get_cog=lambda _name: None)
    cog._session = object()
    cog._master = None
    cog._memory = SimpleNamespace(
        load_context=AsyncMock(return_value=(epoch, [], [])),
        capture_epoch=AsyncMock(return_value=epoch), append_turn=AsyncMock(),
    )
    cog._router = SimpleNamespace(chat=AsyncMock(return_value="É um print de conversa."))
    cog._message_index = SimpleNamespace(remember=AsyncMock())
    cog._can_respond = AsyncMock(return_value=True)
    cog._add_processing_reaction = AsyncMock(return_value="⏳")
    cog._remove_processing_reaction = AsyncMock()
    cog._maybe_generate_tts = AsyncMock(return_value=None)
    cog._maybe_enqueue_voice_call_tts = AsyncMock()
    channel = Mock(spec=discord.TextChannel)
    channel.id = 20
    channel.nsfw = False
    channel.fetch_message = AsyncMock()
    channel.send = AsyncMock()
    message = SimpleNamespace(
        id=30, guild=SimpleNamespace(id=10), channel=channel,
        author=SimpleNamespace(id=40, name="ana", display_name="Ana"),
        content="olha esse print", reference=None, attachments=[],
        reply=AsyncMock(return_value=SimpleNamespace(id=50)),
    )
    prepared = PreparedImage("image/png", PNG_BYTES, filename="print.png")
    return SimpleNamespace(cog=cog, message=message, epoch=epoch, prepared=prepared)


def assert_no_turn_saved(turn, *, feedback_sent=True):
    turn.cog._memory.append_turn.assert_not_awaited()
    if feedback_sent:
        turn.cog._message_index.remember.assert_awaited_once_with(guild_id=10, channel_id=20, message_id=50)
    else:
        turn.cog._message_index.remember.assert_not_awaited()
    turn.cog._maybe_generate_tts.assert_not_awaited()


def assert_safe_reply(message):
    kwargs = message.reply.await_args.kwargs
    assert kwargs["mention_author"] is False
    assert kwargs["allowed_mentions"].to_dict() == {"parse": []}


def test_current_images_have_priority_and_signed_url_duplicates_share_cap(turn):
    current_a = attachment("a.png")
    turn.message.attachments = [current_a, attachment("b.png")]
    replied = source(
        attachment("a.png", signature="renewed"), attachment("c.png"), attachment("d.png"),
    )

    result = turn.cog._collect_turn_images(turn.message, replied)

    assert [image.filename for image in result] == ["a.png", "b.png", "c.png"]
    assert result[0].url == current_a.url
    assert len(result) == C.MAX_IMAGES_PER_MESSAGE
    turn.message.channel.history.assert_not_called()


@pytest.mark.parametrize("scope", [{"guild_id": 11}, {"channel_id": 21}])
def test_images_from_another_scope_are_never_collected(turn, scope):
    turn.message.attachments = [attachment("current.png")]
    replied = source(attachment("private.png"), **scope)

    result = turn.cog._collect_turn_images(turn.message, replied)

    assert [image.filename for image in result] == ["current.png"]
    turn.message.channel.history.assert_not_called()
    turn.message.channel.fetch_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_referenced_attachment_without_text_reaches_vision_and_text_only_memory(turn):
    replied = source(attachment("print.png"), content="")
    turn.message.reference = SimpleNamespace(message_id=replied.id)
    turn.cog._resolve_reply_target = AsyncMock(return_value=replied)
    with patch("cogs.chatbot.cog.prepare_image_attachments", new_callable=AsyncMock) as prepare:
        prepare.return_value = [turn.prepared]
        assert await turn.cog._generate_and_send(turn.message, "o que tem nessa imagem?")

    prepare.assert_awaited_once()
    supplied = prepare.await_args.args[1]
    assert [image.url for image in supplied] == [replied.attachments[0].url]
    request = turn.cog._router.chat.await_args.kwargs
    assert request["messages"][-1].images == [turn.prepared]
    assert request["messages"][-1].image_urls == []
    assert request["messages"][-1].images[0].data == PNG_BYTES
    assert request["temperature"] == C.DEFAULT_VISION_TEMPERATURE
    assert C.DEFAULT_VISION_TEMPERATURE <= C.DEFAULT_TEMPERATURE
    memory = turn.cog._memory.append_turn.await_args.kwargs
    assert "o que tem nessa imagem?" in memory["user_message"]
    assert "1 imagem" in memory["user_message"]
    assert "cdn.discordapp.com" not in memory["user_message"]
    assert "base64" not in memory["user_message"]
    assert PNG_BYTES not in memory["user_message"].encode()
    assert memory["epoch"] == turn.epoch
    assert_safe_reply(turn.message)
    turn.message.channel.history.assert_not_called()


@pytest.mark.asyncio
async def test_expired_signed_url_refreshes_only_the_explicit_message_once(turn):
    replied = source(attachment("print.png"))
    renewed = source(attachment("print.png", signature="new"))
    turn.message.channel.fetch_message.return_value = renewed
    with patch("cogs.chatbot.cog.prepare_image_attachments", new_callable=AsyncMock) as prepare:
        prepare.side_effect = [
            ImagePreparationError("URL expirou", kind="download", status=403), [turn.prepared],
        ]
        assert await turn.cog._prepare_turn_images(turn.message, replied) == [turn.prepared]

    turn.message.channel.fetch_message.assert_awaited_once_with(replied.id)
    assert prepare.await_count == 2
    assert prepare.await_args_list[0].args[1][0].url == replied.attachments[0].url
    assert prepare.await_args_list[1].args[1][0].url == renewed.attachments[0].url
    turn.message.channel.history.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_with_unchanged_url_does_not_retry_download(turn):
    replied = source(attachment("print.png"))
    turn.message.channel.fetch_message.return_value = replied
    error = ImagePreparationError("URL expirou", kind="download", status=404)
    with patch("cogs.chatbot.cog.prepare_image_attachments", new_callable=AsyncMock) as prepare:
        prepare.side_effect = error
        with pytest.raises(ImagePreparationError) as raised:
            await turn.cog._prepare_turn_images(turn.message, replied)

    assert raised.value is error
    prepare.assert_awaited_once()
    turn.message.channel.fetch_message.assert_awaited_once_with(replied.id)


@pytest.mark.asyncio
async def test_refresh_permission_denied_preserves_attachment_error_and_does_not_retry(turn):
    replied = source(attachment("print.png"))
    response = SimpleNamespace(status=403, reason="Forbidden")
    turn.message.channel.fetch_message.side_effect = discord.Forbidden(
        response, {"message": "Missing Access", "code": 50001},
    )
    error = ImagePreparationError("URL expirou", kind="download", status=403)
    with patch("cogs.chatbot.cog.prepare_image_attachments", new_callable=AsyncMock) as prepare:
        prepare.side_effect = error
        with pytest.raises(ImagePreparationError) as raised:
            await turn.cog._prepare_turn_images(turn.message, replied)

    assert raised.value is error
    assert raised.value.stage == "attachment"
    prepare.assert_awaited_once()
    turn.message.channel.fetch_message.assert_awaited_once_with(replied.id)


@pytest.mark.asyncio
async def test_download_network_error_does_not_refresh_signed_url(turn):
    turn.message.attachments = [attachment("print.png")]
    with patch("cogs.chatbot.cog.prepare_image_attachments", new_callable=AsyncMock) as prepare:
        prepare.side_effect = ImagePreparationError("sem rede", kind="download")
        with pytest.raises(ImagePreparationError):
            await turn.cog._prepare_turn_images(turn.message)

    prepare.assert_awaited_once()
    turn.message.channel.fetch_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected"),
    [("size", "versão menor"), ("mime", "PNG, JPG ou WebP"),
     ("unreadable", "PNG, JPG ou WebP"), ("download", "Reenvia"), ("timeout", "reenviar")],
)
async def test_preparation_failure_is_actionable_and_never_reaches_provider_or_memory(turn, kind, expected):
    turn.message.attachments = [attachment("print.png")]
    with patch("cogs.chatbot.cog.prepare_image_attachments", new_callable=AsyncMock) as prepare:
        prepare.side_effect = ImagePreparationError("anexo falhou", kind=kind)
        assert not await turn.cog._generate_and_send(turn.message, "leia esse print")

    turn.cog._router.chat.assert_not_awaited()
    assert_no_turn_saved(turn)
    assert expected in turn.message.reply.await_args.args[0]
    assert_safe_reply(turn.message)
    turn.cog._remove_processing_reaction.assert_awaited_once_with(turn.message, "⏳")


@pytest.mark.asyncio
async def test_exhausted_vision_models_give_clear_error_without_persisting_failure(turn):
    turn.message.attachments = [attachment("print.png")]
    turn.cog._router.chat.side_effect = AllProvidersExhausted(
        "nenhum modelo disponível", kind="model", stage="routing",
    )
    with patch("cogs.chatbot.cog.prepare_image_attachments", new_callable=AsyncMock) as prepare:
        prepare.return_value = [turn.prepared]
        assert not await turn.cog._generate_and_send(turn.message, "leia esse print")

    turn.cog._router.chat.assert_awaited_once()
    assert "leitura de imagens" in turn.message.reply.await_args.args[0]
    assert "configuração" in turn.message.reply.await_args.args[0]
    assert_no_turn_saved(turn)
    assert_safe_reply(turn.message)


@pytest.mark.asyncio
async def test_spontaneous_failure_remains_silent_and_is_not_saved(turn):
    turn.message.attachments = [attachment("print.png")]
    with patch("cogs.chatbot.cog.prepare_image_attachments", new_callable=AsyncMock) as prepare:
        prepare.side_effect = ImagePreparationError("anexo falhou", kind="unreadable")
        assert not await turn.cog._generate_and_send(
            turn.message, "olha esse print", behavior_hint="Responda brevemente.",
        )

    turn.message.reply.assert_not_awaited()
    turn.cog._router.chat.assert_not_awaited()
    assert_no_turn_saved(turn, feedback_sent=False)
    turn.cog._remove_processing_reaction.assert_awaited_once()


@pytest.mark.asyncio
async def test_animated_image_payload_discloses_only_first_frame(turn):
    turn.message.attachments = [attachment("gif.png")]
    prepared = PreparedImage("image/png", PNG_BYTES, first_frame_only=True)
    with patch("cogs.chatbot.cog.prepare_image_attachments", new_callable=AsyncMock) as prepare:
        prepare.return_value = [prepared]
        assert await turn.cog._generate_and_send(turn.message, "o que acontece no gif?")

    request = turn.cog._router.chat.await_args.kwargs
    assert request["messages"][-1].images == [prepared]
    assert "primeiro quadro" in request["system"]
    assert "restante da animação" in request["system"]


@pytest.mark.asyncio
async def test_text_request_keeps_text_temperature_and_no_visual_payload(turn):
    with patch("cogs.chatbot.cog.prepare_image_attachments", new_callable=AsyncMock) as prepare:
        assert await turn.cog._generate_and_send(turn.message, "oi")

    prepare.assert_not_awaited()
    request = turn.cog._router.chat.await_args.kwargs
    assert request["temperature"] == C.DEFAULT_TEMPERATURE
    assert request["messages"][-1].images == []
    assert request["messages"][-1].image_urls == []
    assert turn.cog._memory.append_turn.await_args.kwargs["user_message"] == "oi"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["channel", "guild"])
async def test_foreign_cached_reply_cannot_supply_context_or_images(turn, field):
    cached = Mock(spec=discord.Message)
    cached.id = 44
    cached.guild = SimpleNamespace(id=11 if field == "guild" else 10)
    cached.channel = SimpleNamespace(id=21 if field == "channel" else 20)
    cached.attachments = [attachment("private.png")]
    turn.message.reference = SimpleNamespace(message_id=44, channel_id=20, resolved=cached)
    turn.message.channel.fetch_message.return_value = cached

    assert await turn.cog._resolve_reply_target(turn.message) is None

    turn.message.channel.fetch_message.assert_awaited_once_with(44)
    turn.message.channel.history.assert_not_called()


@pytest.mark.asyncio
async def test_cross_channel_reference_is_rejected_without_fetch(turn):
    turn.message.reference = SimpleNamespace(message_id=44, channel_id=21, resolved=None)

    assert await turn.cog._resolve_reply_target(turn.message) is None

    turn.message.channel.fetch_message.assert_not_awaited()


class _Lease:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def setup_process(turn, *, content="", via="mention"):
    cog = turn.cog
    cog._config = object()
    cog._resolve_trigger = AsyncMock(return_value=TriggerInfo(content=content, via=via))
    cog._is_user_on_cooldown = Mock(return_value=False)
    cog._admission = SimpleNamespace(try_admit=AsyncMock(return_value=_Lease()))
    cog._turn_lock_for = Mock(return_value=asyncio.Lock())
    cog._touch_turn_lock = Mock()
    cog._apply_user_cooldown = Mock()
    cog._detect_user_intent = Mock(return_value=SimpleNamespace(kind="chat", prompt=""))


@pytest.mark.asyncio
async def test_image_only_invalid_attachment_still_reaches_user_feedback(turn):
    setup_process(turn)
    turn.message.content = "<@999>"
    turn.message.attachments = [attachment("too-big.png", size=C.MAX_IMAGE_SIZE_BYTES + 1)]
    with patch("cogs.chatbot.cog.prepare_image_attachments", new_callable=AsyncMock) as prepare:
        await turn.cog._process_chat(turn.message)

    prepare.assert_not_awaited()
    turn.cog._router.chat.assert_not_awaited()
    assert "versão menor" in turn.message.reply.await_args.args[0]
    assert_no_turn_saved(turn)
    assert_safe_reply(turn.message)


@pytest.mark.asyncio
async def test_empty_reply_to_attachment_enters_vision_without_caption(turn):
    setup_process(turn, via="reply")
    replied = source(attachment("print.png"), content="")
    turn.message.content = ""
    turn.message.reference = SimpleNamespace(message_id=replied.id)
    turn.cog._resolve_reply_target = AsyncMock(return_value=replied)
    with patch("cogs.chatbot.cog.prepare_image_attachments", new_callable=AsyncMock) as prepare:
        prepare.return_value = [turn.prepared]
        await turn.cog._process_chat(turn.message)

    request = turn.cog._router.chat.await_args.kwargs
    assert request["messages"][-1].images == [turn.prepared]
    assert "Analise a imagem da mensagem respondida" in request["messages"][-1].content
    turn.message.reply.assert_awaited_once()
    turn.cog._memory.append_turn.assert_awaited_once()
    turn.message.channel.history.assert_not_called()
