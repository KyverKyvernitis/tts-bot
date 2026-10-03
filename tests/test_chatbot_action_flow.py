"""Da fala do usuário ao pedido persistido: nenhuma execução a partir de texto."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.chatbot import constants as C
from cogs.chatbot.action_protocol import ActionProposal, ChatReply
from cogs.chatbot.actions import ActionService
from cogs.chatbot.cog import ChatbotCog
from cogs.chatbot.config import GuildChatbotConfig
from cogs.chatbot.memory import MemoryEntry, MemoryEpoch


def _value(doc, key):
    value = doc
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _matches(doc, query):
    for key, expected in query.items():
        actual = _value(doc, key)
        if isinstance(expected, dict):
            for operator, operand in expected.items():
                if operator == "$in" and actual not in operand:
                    return False
                if operator == "$gt" and (actual is None or not actual > operand):
                    return False
                if operator == "$lte" and (actual is None or not actual <= operand):
                    return False
        elif actual != expected:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self.docs = deepcopy(docs)

    def sort(self, key, direction):
        self.docs.sort(key=lambda doc: _value(doc, key), reverse=direction < 0)
        return self

    def limit(self, count):
        self.docs = self.docs[:count]
        return self

    def __aiter__(self):
        self.iterator = iter(self.docs)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration


class _Collection:
    """Mongo mínimo: CAS real sobre os documentos usados pelo ActionStore."""
    def __init__(self, events):
        self.docs = []
        self.events = events

    async def insert_one(self, doc):
        self.docs.append(deepcopy(doc))
        self.events.append(("created", doc["request_id"]))

    def find(self, query):
        return _Cursor([doc for doc in self.docs if _matches(doc, query)])

    async def find_one(self, query):
        return next((deepcopy(doc) for doc in self.docs if _matches(doc, query)), None)

    async def find_one_and_update(self, query, update, **kwargs):
        for doc in self.docs:
            if not _matches(doc, query):
                continue
            for key, value in update.get("$set", {}).items():
                doc[key] = deepcopy(value)
            for key in update.get("$unset", {}):
                parts = key.split(".")
                target = doc
                for part in parts[:-1]:
                    target = target.get(part, {})
                target.pop(parts[-1], None)
            self.events.append((doc["state"], doc["request_id"]))
            return deepcopy(doc)
        return None


class _Supervisor:
    def __init__(self, collection, events):
        self.collection = collection
        self.events = events
        self.jobs = []

    def create(self, coroutine, *, name):
        # Os executores são agendados somente depois do vínculo persistido.
        assert self.collection.docs[-1]["state"] in {"pending", "executing"}
        assert self.collection.docs[-1]["message_id"] == 60
        assert any(event[0] == "reply_sent" for event in self.events)
        self.events.append(("scheduled", name))
        self.jobs.append(coroutine)

    async def drain(self):
        while self.jobs:
            await self.jobs.pop(0)


def _member(guild, identifier, *, rank=1, ban=False, bot=False):
    member = MagicMock(spec=discord.Member)
    member.id, member.guild, member.top_role = identifier, guild, rank
    member.name = member.display_name = f"membro {identifier}"
    member.roles, member.bot, member.voice = [], bot, None
    member.guild_permissions = SimpleNamespace(ban_members=ban, manage_guild=ban, administrator=False)
    member.ban = AsyncMock()
    return member


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(C, "SAFE_MODE", False)
    events = []
    guild = SimpleNamespace(id=10, owner_id=99, voice_client=None)
    members = {1: _member(guild, 1), 2: _member(guild, 2, rank=10, ban=True),
               3: _member(guild, 3, rank=2), 999: _member(guild, 999, rank=20, ban=True, bot=True)}
    guild.me = members[999]
    guild.fetch_member = AsyncMock(side_effect=lambda identifier: members.get(identifier))
    voice = MagicMock(spec=discord.VoiceChannel)
    voice.id, voice.guild = 20, guild
    voice.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True)
    members[1].voice = SimpleNamespace(channel=voice)
    members[999].voice = SimpleNamespace(channel=voice)
    guild.voice_client = SimpleNamespace(channel=voice)
    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.guild, channel.nsfw = 30, guild, False
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True, send_messages=True, attach_files=True)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=88))
    card = SimpleNamespace(id=60, guild=guild, author=members[999], edit=AsyncMock())
    channel.fetch_message = AsyncMock(return_value=card)
    channels = {20: voice, 30: channel}
    guild.get_channel = guild.get_channel_or_thread = channels.get
    config = GuildChatbotConfig(10, enabled=True, channel_ids=(30,))
    tts = SimpleNamespace(
        synthesize_chatbot_attachment=AsyncMock(return_value=b"mp3 bytes"),
        chatbot_join_voice=AsyncMock(return_value={"ok": True, "status": "executed"}),
        chatbot_speak_voice=AsyncMock(return_value={"ok": True, "status": "executed"}),
    )
    cog = object.__new__(ChatbotCog)
    cogs = {"TTSVoice": tts, "Chatbot": cog}
    cog.bot = SimpleNamespace(
        user=members[999], get_cog=cogs.get, get_channel=channels.get,
        get_guild=lambda identifier: guild if identifier == 10 else None,
        settings_db=SimpleNamespace(resolve_tts=AsyncMock(return_value={})),
    )
    cog._session, cog._master = object(), None
    original_epoch = MemoryEpoch(2, 3, 4)
    cog._memory = SimpleNamespace(
        load_context=AsyncMock(return_value=(original_epoch, [
            MemoryEntry("user", "assunto anterior", user_id=1),
            MemoryEntry("assistant", "resposta anterior", user_id=1),
        ], [])),
        capture_epoch=AsyncMock(return_value=MemoryEpoch(9, 9, 9)), append_turn=AsyncMock(),
    )
    cog._config = SimpleNamespace(get_config=AsyncMock(return_value=config))
    cog._router = SimpleNamespace(chat=AsyncMock(return_value=ChatReply("oi")))
    cog._message_index = SimpleNamespace(remember=AsyncMock())
    cog._can_respond = AsyncMock(return_value=True)
    cog._add_processing_reaction = AsyncMock(return_value="⏳")
    cog._remove_processing_reaction = AsyncMock()
    cog._maybe_generate_tts = AsyncMock(return_value=None)
    collection = _Collection(events)
    cog._supervisor = _Supervisor(collection, events)
    cog._actions = ActionService(cog, collection)
    cog._actions.ready = True

    async def reply(*args, **kwargs):
        events.append(("reply_sent", args[0]))
        return card

    message = SimpleNamespace(
        id=50, guild=guild, channel=channel, author=members[1], mentions=[members[3]],
        content="converse comigo", reference=None, attachments=[], reply=AsyncMock(side_effect=reply),
    )
    yield SimpleNamespace(
        cog=cog, guild=guild, members=members, voice=voice, channel=channel, config=config,
        tts=tts, card=card, message=message, collection=collection, events=events, epoch=original_epoch,
    )
    for coroutine in cog._supervisor.jobs:
        coroutine.close()
    cog._actions.shutdown()


def _interaction(world, actor):
    done = False

    async def mark_done(*args, **kwargs):
        nonlocal done
        done = True

    response = SimpleNamespace(
        is_done=lambda: done, defer=AsyncMock(side_effect=mark_done),
        send_message=AsyncMock(side_effect=mark_done),
    )
    return SimpleNamespace(
        guild=world.guild, channel_id=30, message=world.card, user=world.members[actor],
        response=response, followup=SimpleNamespace(send=AsyncMock()),
    )


def _public_output(world):
    output = [world.message.reply.await_args.args[0]]
    output.extend(call.kwargs.get("content", "") for call in world.card.edit.await_args_list)
    return "\n".join(output)


@pytest.mark.asyncio
async def test_native_ban_proposal_becomes_bound_button_and_only_staff_executes(world):
    world.cog._router.chat.return_value = ChatReply("Vou solicitar a ação.", (
        ActionProposal("ban_member", "m1", reason="spam repetido", ask_permission=False),
    ))
    assert await world.cog._generate_and_send(world.message, "peça para banir esse membro")
    options = world.cog._router.chat.await_args.kwargs
    assert set(options["actions"]) == {"send_audio", "speak_voice", "ban_member"}
    assert options["target_refs"] == ("autor", "m1")
    assert "Alvo autor:" in options["system"] and "Alvo m1:" in options["system"]
    assert options["messages"][-1].content == "peça para banir esse membro"
    request = world.collection.docs[0]
    assert (request["guild_id"], request["channel_id"], request["origin_message_id"],
            request["requester_id"], request["message_id"]) == (10, 30, 50, 1, 60)
    assert request["payload"]["target_id"] == 3
    assert request["ask_permission"] and request["state"] == "pending"
    view = world.message.reply.await_args.kwargs["view"]
    assert view.is_persistent()
    assert [button.label for button in view.children] == ["Aprovar banimento", "Rejeitar"]
    assert request["request_id"] in view.children[0].custom_id
    assert "spam repetido" in _public_output(world)
    world.members[3].ban.assert_not_awaited()
    assert not world.cog._supervisor.jobs
    world.cog._maybe_generate_tts.assert_not_awaited()
    ordinary = _interaction(world, 1)
    await view.children[0].callback(ordinary)
    assert request["state"] == "pending"
    assert "Banir membros" in ordinary.followup.send.await_args.args[0]
    staff = _interaction(world, 2)
    await view.children[0].callback(staff)
    assert request["state"] == "executing"
    await world.cog._supervisor.drain()
    world.members[3].ban.assert_awaited_once()
    assert world.members[3].ban.await_args.kwargs["delete_message_seconds"] == 0
    assert request["approved_by"] == 2 and request["state"] == "succeeded"
    assert "Banimento concluído" in _public_output(world)
    world.channel.history.assert_not_called()
    world.channel.webhooks.assert_not_called()


@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
@pytest.mark.asyncio
async def test_optional_audio_never_shows_speech_or_starts_legacy_tts_before_common_approval(world, action):
    secret = "FALA PRIVADA QUE NÃO PODE APARECER NA PRÉVIA"
    world.cog._router.chat.return_value = ChatReply(secret, (
        ActionProposal(action, "autor", text=secret, reason=secret, ask_permission=True),
        ActionProposal("ban_member", "m1", reason=secret),
    ))
    assert await world.cog._generate_and_send(world.message, "responda como preferir")
    assert len(world.collection.docs) == 1
    request = world.collection.docs[0]
    assert request["payload"]["text"] == secret and request["base_reply"] == ""
    assert request["state"] == "pending"
    assert secret not in _public_output(world)
    view = world.message.reply.await_args.kwargs["view"]
    assert view.is_persistent() and len(view.children) == 2
    world.tts.synthesize_chatbot_attachment.assert_not_awaited()
    world.tts.chatbot_speak_voice.assert_not_awaited()
    world.cog._maybe_generate_tts.assert_not_awaited()
    assert not world.cog._supervisor.jobs
    assert secret not in str(world.cog._memory.append_turn.await_args_list)
    await view.children[0].callback(_interaction(world, 2))
    assert request["state"] == "pending"  # Staff não substitui o consentimento do autor.
    await view.children[0].callback(_interaction(world, 1))
    assert request["state"] == "executing"
    await world.cog._supervisor.drain()
    adapter = world.tts.synthesize_chatbot_attachment if action == "send_audio" else world.tts.chatbot_speak_voice
    adapter.assert_awaited_once()
    assert adapter.await_args.kwargs["text"] == secret
    assert request["state"] == "succeeded" and "text" not in request["payload"]
    assert secret not in _public_output(world)
    stored = world.cog._memory.append_turn.await_args.kwargs
    assert stored["assistant_message"] == secret
    assert stored["epoch"] == world.epoch and stored["visibility_scope"] == "channel:30"


@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
@pytest.mark.asyncio
async def test_spontaneous_audio_schedules_after_binding_and_executes_once_without_buttons(world, action):
    secret = "Fala espontânea em português."
    world.cog._router.chat.return_value = ChatReply("", (ActionProposal(action, text=secret),))
    assert await world.cog._generate_and_send(world.message, "oi")
    request = deepcopy(world.collection.docs[0])
    assert request["state"] == "pending" and not request["ask_permission"]
    assert world.message.reply.await_args.kwargs["view"] is None
    assert len(world.cog._supervisor.jobs) == 1
    assert [event[0] for event in world.events[:4]] == ["created", "reply_sent", "pending", "scheduled"]
    world.tts.synthesize_chatbot_attachment.assert_not_awaited()
    world.tts.chatbot_speak_voice.assert_not_awaited()
    world.cog._maybe_generate_tts.assert_not_awaited()
    await world.cog._supervisor.drain()
    await world.cog._actions._start_automatic(request)
    adapter = world.tts.synthesize_chatbot_attachment if action == "send_audio" else world.tts.chatbot_speak_voice
    adapter.assert_awaited_once()
    assert world.collection.docs[0]["state"] == "succeeded"
    assert world.cog._memory.append_turn.await_count == 2
    assert world.cog._memory.append_turn.await_args.kwargs["epoch"] == world.epoch
    assert secret not in _public_output(world)
    if action == "send_audio":
        world.channel.send.assert_awaited_once()
        kwargs = world.channel.send.await_args.kwargs
        assert "content" not in kwargs and kwargs["reference"].message_id == 60
        assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert world.cog._message_index.remember.await_count == 2


@pytest.mark.asyncio
async def test_plain_text_permission_claim_creates_no_request_or_execution(world):
    world.cog._router.chat.return_value = "Pediu permissão: banir <@3>"
    assert await world.cog._generate_and_send(world.message, "converse")
    assert not world.collection.docs and not world.cog._supervisor.jobs
    assert "view" not in world.message.reply.await_args.kwargs
    world.members[3].ban.assert_not_awaited()
    world.tts.synthesize_chatbot_attachment.assert_not_awaited()
    assert world.cog._router.chat.await_args.kwargs["actions"]


@pytest.mark.asyncio
async def test_untrusted_target_reference_does_not_create_a_request(world):
    _restore_legacy_audio_generation(world)
    world.cog._router.chat.return_value = ChatReply("Não identifiquei esse membro.", (
        ActionProposal("ban_member", "m77", reason="motivo"),
    ))
    # O pedido por áudio normalmente ativa a síntese legada. Uma ferramenta
    # recusada deve produzir somente o motivo real, sem sintetizar o erro.
    assert await world.cog._generate_and_send(world.message, "banir alguém e responda em áudio")
    assert not world.collection.docs and not world.cog._supervisor.jobs
    assert "view" not in world.message.reply.await_args.kwargs
    assert _public_output(world) == "Preciso de um membro identificado nesta conversa para essa ação."
    world.tts.synthesize_chatbot_attachment.assert_not_awaited()
    world.tts._enqueue_tts_item.assert_not_awaited()
    world.tts.chatbot_speak_voice.assert_not_awaited()
    world.members[3].ban.assert_not_awaited()


@pytest.mark.asyncio
async def test_denied_optional_audio_reports_safe_host_reason_without_preview_or_error_synthesis(world):
    _restore_legacy_audio_generation(world)
    secret = "TEXTO PRIVADO MUITO COMPRIDO " * 40
    world.cog._router.chat.return_value = ChatReply(secret, (
        ActionProposal("send_audio", "usuario", text=secret, ask_permission=True),
    ))
    assert await world.cog._generate_and_send(world.message, "responda em áudio")
    assert _public_output(world) == "Não consegui preparar essa fala. Peça uma resposta mais curta."
    assert secret not in _public_output(world)
    assert not world.collection.docs and not world.cog._supervisor.jobs
    assert "view" not in world.message.reply.await_args.kwargs
    world.tts.synthesize_chatbot_attachment.assert_not_awaited()
    world.tts._enqueue_tts_item.assert_not_awaited()
    world.tts.chatbot_speak_voice.assert_not_awaited()
    assert secret not in str(world.cog._memory.append_turn.await_args_list)


@pytest.mark.asyncio
async def test_failed_public_reply_leaves_unbound_request_without_execution(world):
    world.cog._router.chat.return_value = ChatReply("", (ActionProposal("send_audio", text="fala privada"),))
    world.message.reply.side_effect = RuntimeError("Discord indisponível")
    with pytest.raises(RuntimeError):
        await world.cog._generate_and_send(world.message, "oi")
    request = world.collection.docs[0]
    assert request["state"] == "created" and "message_id" not in request
    assert not world.cog._supervisor.jobs
    world.tts.synthesize_chatbot_attachment.assert_not_awaited()
    world.cog._memory.append_turn.assert_not_awaited()
    world.cog._remove_processing_reaction.assert_awaited_once_with(world.message, "⏳")


@pytest.mark.asyncio
async def test_failed_audio_does_not_put_unheard_speech_into_conversation(world):
    secret = "Essa fala nunca foi entregue."
    world.tts.synthesize_chatbot_attachment.return_value = b""
    world.cog._router.chat.return_value = ChatReply("", (ActionProposal("send_audio", text=secret),))
    assert await world.cog._generate_and_send(world.message, "oi")
    await world.cog._supervisor.drain()
    assert world.collection.docs[0]["state"] == "failed"
    assert "text" not in world.collection.docs[0]["payload"]
    assert secret not in str(world.cog._memory.append_turn.await_args_list)
    assert secret not in _public_output(world)
    world.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unready_action_service_keeps_router_in_legacy_text_mode(world):
    world.cog._actions.ready = False
    world.cog._router.chat.return_value = "oi, tudo bem?"
    assert await world.cog._generate_and_send(world.message, "oi")
    assert "actions" not in world.cog._router.chat.await_args.kwargs
    assert "Capacidades reais neste turno" not in world.cog._router.chat.await_args.kwargs["system"]
    assert not world.collection.docs


def _restore_legacy_audio_generation(world):
    # Exercitar os gates reais; só os serviços externos de síntese/fila são mocks.
    world.cog._maybe_generate_tts = ChatbotCog._maybe_generate_tts.__get__(world.cog)
    world.tts._enqueue_tts_item = AsyncMock(return_value=(True, False, False))


@pytest.mark.parametrize("disabled", ["actions_enabled", "audio_actions_enabled"])
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.asyncio
async def test_plain_or_text_only_fallback_cannot_bypass_disabled_audio(world, disabled, structured):
    _restore_legacy_audio_generation(world)
    world.cog._config.get_config.return_value = replace(world.config, **{disabled: False})
    text = "Uma resposta normal em português."
    world.cog._router.chat.return_value = ChatReply(text) if structured else text
    assert await world.cog._generate_and_send(world.message, "me responde em áudio")
    world.tts.synthesize_chatbot_attachment.assert_not_awaited()
    world.tts._enqueue_tts_item.assert_not_awaited()
    world.tts.chatbot_speak_voice.assert_not_awaited()
    assert world.message.reply.await_args.kwargs["files"] is discord.utils.MISSING
    assert not world.collection.docs
    assert any(call.kwargs.get("fresh") for call in world.cog._config.get_config.await_args_list)


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.asyncio
async def test_disabled_voice_flag_prevents_legacy_call_mirror_while_chat_audio_remains_enabled(world, structured):
    _restore_legacy_audio_generation(world)
    world.cog._config.get_config.return_value = replace(world.config, voice_actions_enabled=False)
    text = "Essa resposta pode ser enviada em áudio no chat."
    world.cog._router.chat.return_value = ChatReply(text) if structured else text
    assert await world.cog._generate_and_send(world.message, "me responde em áudio")
    world.tts.synthesize_chatbot_attachment.assert_awaited_once()
    files = world.message.reply.await_args.kwargs["files"]
    assert len(files) == 1 and files[0].filename == "resposta.mp3"
    world.tts._enqueue_tts_item.assert_not_awaited()
    world.tts.chatbot_speak_voice.assert_not_awaited()
    files[0].close()


@pytest.mark.parametrize("revoked", ["actions_enabled", "audio_actions_enabled"])
@pytest.mark.asyncio
async def test_revocation_during_legacy_synthesis_closes_attachment_and_never_sends_it(world, monkeypatch, revoked):
    _restore_legacy_audio_generation(world)
    attachments = []
    original_file = discord.File

    class TrackedFile(original_file):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.closed_by_cog = False
            attachments.append(self)

        def close(self):
            self.closed_by_cog = True
            super().close()

    monkeypatch.setattr("cogs.chatbot.cog.discord.File", TrackedFile)

    async def synthesize_and_revoke(**kwargs):
        world.cog._config.get_config.return_value = replace(world.config, **{revoked: False})
        return b"mp3 generated before the staff disabled audio"

    world.tts.synthesize_chatbot_attachment.side_effect = synthesize_and_revoke
    world.cog._router.chat.return_value = "A resposta que seria sintetizada."
    assert await world.cog._generate_and_send(world.message, "me responde em áudio")
    world.tts.synthesize_chatbot_attachment.assert_awaited_once()
    assert len(attachments) == 1 and attachments[0].closed_by_cog
    assert world.message.reply.await_args.kwargs["files"] is discord.utils.MISSING
    assert world.message.reply.await_args.args[0] == "O áudio foi desativado antes do envio."
    world.tts._enqueue_tts_item.assert_not_awaited()
    world.channel.send.assert_not_awaited()
    assert not world.collection.docs


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.asyncio
async def test_text_only_reply_can_send_requested_mp3_but_never_speaks_without_native_proposal(world, structured):
    _restore_legacy_audio_generation(world)
    assert world.config.actions_enabled and world.config.audio_actions_enabled and world.config.voice_actions_enabled
    text = "Resposta para o anexo de áudio."
    world.cog._router.chat.return_value = ChatReply(text) if structured else text
    assert await world.cog._generate_and_send(world.message, "me responde em áudio")
    world.tts.synthesize_chatbot_attachment.assert_awaited_once()
    files = world.message.reply.await_args.kwargs["files"]
    assert len(files) == 1 and files[0].filename == "resposta.mp3"
    world.tts._enqueue_tts_item.assert_not_awaited()
    world.tts.chatbot_speak_voice.assert_not_awaited()
    world.tts.chatbot_join_voice.assert_not_awaited()
    assert not world.collection.docs and not world.cog._supervisor.jobs
    files[0].close()
