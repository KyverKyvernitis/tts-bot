"""Origem confirmada, conversão fiel, reuso dos bytes e épocas de reset."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.chatbot.audio import recorded_reply_audio
from cogs.chatbot.memory import MemoryEpoch
from cogs.chatbot.reply_store import DOC_TYPE_REPLY, REPLY_RETENTION, ReplyStore
from test_chatbot_action_policy import world
from test_chatbot_action_store import _Collection, _matches


class _ReplyCollection(_Collection):
    async def update_one(self, query, update, *, upsert=False):
        doc = next((item for item in self.docs if _matches(item, query)), None)
        if doc is None and upsert:
            doc = deepcopy(query)
            doc.update(deepcopy(update.get("$setOnInsert", {})))
            doc.update(deepcopy(update.get("$set", {})))
            self.docs.append(doc)
        elif doc is not None:
            doc.update(deepcopy(update.get("$set", {})))


@pytest.fixture
def replies(world):
    coll = _ReplyCollection()
    store = ReplyStore(coll)
    cog = world.bot.get_cog("Chatbot")
    cog._reply_store = store
    cog._memory = SimpleNamespace(capture_epoch=AsyncMock(return_value=MemoryEpoch(1, 2, 3)))
    messages = {}
    async def fetch(message_id):
        if message_id not in messages:
            raise ValueError("missing message")
        return messages[message_id]
    world.chat.fetch_message = AsyncMock(side_effect=fetch)
    return SimpleNamespace(store=store, coll=coll, world=world, messages=messages)


async def sent(replies, *, message_id=88, format="text", text="Resposta exata", spoken_text="", attachment=None, user_id=1):
    record = await replies.store.record_sent(
        guild_id=10, channel_id=30, requester_id=user_id, origin_message_id=50,
        message_id=message_id, original_user_text="Pergunta original real", text=text,
        spoken_text=spoken_text, format=format, provider="groq", model="model-original",
        epoch=MemoryEpoch(1, 2, 3), attachment=attachment,
    )
    replies.messages[message_id] = SimpleNamespace(id=message_id, channel=replies.world.chat,
        guild=replies.world.guild, author=replies.world.bot.user, content=text, attachments=[])
    return record


@pytest.mark.asyncio
async def test_record_keeps_original_pair_provider_format_epoch_without_cards(replies):
    record = await sent(replies)
    assert record["type"] == DOC_TYPE_REPLY
    assert record["original_user_text"] == "Pergunta original real"
    assert record["text"] == "Resposta exata"
    assert record["provider"] == "groq" and record["model"] == "model-original"
    assert record["expires_at"] - record["sent_at"] == REPLY_RETENTION
    assert len(replies.coll.docs) == 1
    with pytest.raises(ValueError):
        await replies.store.record_sent(guild_id=10, channel_id=30, requester_id=1,
            origin_message_id=50, message_id=89, original_user_text="Pergunta", text="Permissão",
            format="card", epoch=MemoryEpoch())
    assert len(replies.coll.docs) == 1


@pytest.mark.asyncio
async def test_repeated_registration_cannot_rewrite_confirmed_reply(replies):
    original = await sent(replies)
    changed = await sent(replies, text="Não foi o que enviei")
    assert changed == original
    assert len(replies.coll.docs) == 1


@pytest.mark.asyncio
async def test_explicit_reply_resolves_only_own_bot_same_channel(replies):
    await sent(replies)
    found = await replies.store.resolve(replies.world.bot, guild_id=10, channel_id=30, user_id=1, message_id=88)
    assert found["text"] == "Resposta exata"
    assert await replies.store.resolve(replies.world.bot, guild_id=10, channel_id=99, user_id=1, message_id=88) is None
    replies.messages[88].author = replies.world.members[3]
    assert await replies.store.resolve(replies.world.bot, guild_id=10, channel_id=30, user_id=1, message_id=88) is None


@pytest.mark.asyncio
async def test_latest_skips_deleted_or_edited_response_and_other_user(replies):
    await sent(replies, message_id=87, text="Mais antiga válida")
    await sent(replies, message_id=88, text="Recente apagada")
    await sent(replies, message_id=89, text="Resposta de outro membro", user_id=3)
    replies.messages.pop(88)
    found = await replies.store.resolve(replies.world.bot, guild_id=10, channel_id=30, user_id=1)
    assert found["message_id"] == 87
    replies.messages[87].content = "Editada depois"
    assert await replies.store.resolve(replies.world.bot, guild_id=10, channel_id=30, user_id=1) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["user_reset", "guild_reset", "global_reset", "channel_off", "access_off", "expired"])
async def test_fresh_reset_access_and_expiration_invalidate_record(replies, change):
    await sent(replies)
    if change.endswith("reset"):
        epoch = {"user_reset": MemoryEpoch(1, 2, 4), "guild_reset": MemoryEpoch(1, 4, 3), "global_reset": MemoryEpoch(4, 2, 3)}[change]
        replies.world.bot.get_cog("Chatbot")._memory.capture_epoch.return_value = epoch
    elif change == "channel_off":
        replies.world.config.enabled = False
    elif change == "access_off":
        replies.world.chat.permissions_for.return_value.view_channel = False
    else:
        replies.coll.docs[0]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await replies.store.resolve(replies.world.bot, guild_id=10, channel_id=30, user_id=1, message_id=88) is None


@pytest.mark.asyncio
async def test_text_conversion_synthesizes_full_2000_once_with_voice_language_preferences(replies):
    text = "abcde" * 400
    record = await sent(replies, text=text)
    replies.world.bot.get_cog("Chatbot").get_conversation_preferences = AsyncMock(return_value=SimpleNamespace(voice="pt-BR-AntonioNeural", language="pt"))
    replies.world.bot.settings_db.resolve_tts.return_value = {"edge_voice": "old", "gtts_language": "en", "edge_rate": "+5%", "edge_pitch": "+2Hz"}
    data = await recorded_reply_audio(replies.world.bot, record, user_id=1)
    assert data == b"mp3 bytes"
    replies.world.tts.synthesize_chatbot_attachment.assert_awaited_once()
    arguments = replies.world.tts.synthesize_chatbot_attachment.await_args.kwargs
    assert arguments["text"] == text and arguments["max_text_chars"] == 2000
    assert arguments["voice"] == "pt-BR-AntonioNeural" and arguments["language"] == "pt"
    assert arguments["rate"] == "+5%" and arguments["pitch"] == "+2Hz"


@pytest.mark.asyncio
async def test_existing_audio_reuses_exact_attachment_without_synthesis_or_new_voice(replies):
    data = b"original accepted mp3"
    attachment = SimpleNamespace(id=77, filename="resposta.mp3", size=len(data), read=AsyncMock(return_value=data))
    record = await sent(replies, format="audio", text="", spoken_text="Fala original", attachment={
        "id": 77, "filename": "resposta.mp3", "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "url": "https://untrusted.invalid"})
    replies.messages[88].attachments = [attachment]
    result = await recorded_reply_audio(replies.world.bot, record, user_id=1)
    assert result is data
    assert "url" not in record["attachment"]
    attachment.read.assert_awaited_once()
    replies.world.tts.synthesize_chatbot_attachment.assert_not_awaited()
    replies.world.bot.settings_db.resolve_tts.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["wrong_id", "wrong_size", "wrong_digest", "reset_during_read",
                                    "removed_during_read", "replaced_during_read"])
async def test_unsafe_or_stale_attachment_never_regenerates_different_audio(replies, change):
    data = b"original accepted mp3"
    attachment = SimpleNamespace(id=77, filename="resposta.mp3", size=len(data), read=AsyncMock(return_value=data))
    record = await sent(replies, format="audio", text="", spoken_text="Fala original", attachment={
        "id": 77, "filename": "resposta.mp3", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    replies.messages[88].attachments = [attachment]
    if change == "wrong_id":
        attachment.id = 78
    elif change == "wrong_size":
        attachment.size += 1
    elif change == "wrong_digest":
        attachment.read.return_value = b"X" * len(data)
    elif change == "reset_during_read":
        async def read():
            replies.world.bot.get_cog("Chatbot")._memory.capture_epoch.return_value = MemoryEpoch(1, 2, 4)
            return data
        attachment.read.side_effect = read
    else:
        async def read():
            if change == "removed_during_read":
                replies.messages[88].attachments = []
            else:
                replies.messages[88].attachments = [SimpleNamespace(id=78, filename="resposta.mp3", size=len(data))]
            return data
        attachment.read.side_effect = read
    assert await recorded_reply_audio(replies.world.bot, record, user_id=1) is None
    replies.world.tts.synthesize_chatbot_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_during_synthesis_discards_bytes_without_second_generator(replies):
    record = await sent(replies)
    async def synth(**kwargs):
        replies.world.bot.get_cog("Chatbot")._memory.capture_epoch.return_value = MemoryEpoch(1, 2, 4)
        return b"old audio"
    replies.world.tts.synthesize_chatbot_attachment.side_effect = synth
    assert await recorded_reply_audio(replies.world.bot, record, user_id=1) is None
    replies.world.tts.synthesize_chatbot_attachment.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_reply_does_not_resolve_without_current_thread_membership(replies):
    import discord
    from unittest.mock import MagicMock
    record = await sent(replies)
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.guild = 30, replies.world.guild
    thread.is_private.return_value = True
    thread.permissions_for.return_value = SimpleNamespace(view_channel=True, manage_threads=False)
    thread.fetch_member = AsyncMock(side_effect=ValueError("not a member"))
    replies.world.guild.get_channel_or_thread = lambda ident: thread if ident == 30 else None
    assert await recorded_reply_audio(replies.world.bot, record, user_id=1) is None
    replies.world.tts.synthesize_chatbot_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_attachment_adapter_preserves_legacy_800_and_opt_in_full_2000():
    from cogs.tts.audio import TTSAudioMixin
    from test_chatbot_voice_actions import _Probe
    probe = _Probe()
    probe._schedule_tts_background = asyncio.create_task
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "provider.mp3"
        path.write_bytes(b"already-produced")
        probe._resolve_or_generate_singleflight_audio = AsyncMock(return_value=(str(path), False))
        text = "abcde" * 400
        adapter = TTSAudioMixin.synthesize_chatbot_attachment.__get__(probe)
        assert await adapter(guild_id=1, user_id=2, text=text, voice="pt-BR-FranciscaNeural") == b"already-produced"
        first = probe._resolve_or_generate_singleflight_audio.await_args.args[1]
        assert len(first.text) == 800
        assert await adapter(guild_id=1, user_id=2, text=text, voice="pt-BR-FranciscaNeural", max_text_chars=2000) == b"already-produced"
        second = probe._resolve_or_generate_singleflight_audio.await_args.args[1]
        assert second.text == text
        assert probe._resolve_or_generate_singleflight_audio.await_count == 2
