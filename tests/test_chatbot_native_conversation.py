"""Do catálogo às preferências e ao envio confirmado, sem gatilhos de texto."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.chatbot.action_protocol import ChatReply, NativeToolCall
from cogs.chatbot.cog import ChatbotCog
from cogs.chatbot.preferences import PreferenceStore
from cogs.chatbot.tool_registry import ToolRegistry, ToolSpec
from cogs.chatbot.voice_context import build_voice_snapshot
from tests.test_chatbot_audio_format import turn
from tests.test_chatbot_preferences import Collection


@pytest.fixture
def native(turn, monkeypatch):
    turn.cog._preferences = PreferenceStore(Collection(), memory=turn.cog._memory)
    turn.cog.get_conversation_preferences = ChatbotCog.get_conversation_preferences.__get__(turn.cog)
    turn.cog._reply_store = SimpleNamespace(record_sent=AsyncMock())
    factories = []
    effects = []
    refreshes = []
    extra_specs = []

    async def build(cog, message, config, *, epoch, **kwargs):
        registry = ToolRegistry()
        registry.runtime = SimpleNamespace(action_context=kwargs.get("action_context"),
                                          action_proposals=[], proposals_invalid=False, response_format=None,
                                          action_draft=None, prepared_actions=[], proposals_error="")
        async def preferences(arguments):
            result = await cog._preferences.set_current(message.guild.id, message.channel.id,
                                                       message.author.id, epoch, **arguments)
            return {"ok": True, "status": "executed", "data": result.to_result()}
        async def temporary(arguments):
            registry.runtime.response_format = arguments["mode"]
            return {"ok": True, "status": "selected", "data": {"mode": arguments["mode"]}}
        async def effect(arguments):
            effects.append(arguments)
            return {"ok": True, "status": "executed", "data": {"paused": True}}
        for name, handler, permission in (
            ("set_conversation_preferences", preferences, "automatic_effect"),
            ("select_response_format", temporary, "automatic_effect"),
            ("automatic_test", effect, "automatic_effect"),
        ):
            schema = {"type": "object", "properties": {
                "mode": {"type": "string", "enum": ["audio", "text", "auto"]},
                "voice": {"type": "string"}, "language": {"type": "string"},
            }, "additionalProperties": False}
            registry.register(ToolSpec(name, "Ferramenta declarada para este turno.", schema,
                                       permission=permission, handler=handler))
        for spec in extra_specs:
            registry.register(spec)
        factories.append(registry)
        return registry
    monkeypatch.setattr("cogs.chatbot.tool_runtime.build_tool_registry", build)
    async def refresh(registry):
        state = build_voice_snapshot(turn.cog.bot, turn.message.guild, turn.message.author)
        registry.runtime.voice_state = state
        refreshes.append(state)
        return state
    monkeypatch.setattr("cogs.chatbot.tool_runtime.refresh_tool_context", refresh)
    turn.factories, turn.effects, turn.refreshes, turn.extra_specs = factories, effects, refreshes, extra_specs
    return turn


def call(identifier, name, **arguments):
    return NativeToolCall(identifier, name, arguments)


@pytest.mark.asyncio
async def test_saved_audio_mode_applies_to_following_question_without_repeat(native):
    native.cog._router.chat.side_effect = [
        ChatReply("", tool_calls=(call("pref-1", "set_conversation_preferences", mode="audio"),)),
        ChatReply("Pode falar."), ChatReply("Hoje foi tranquilo, e o seu?"),
    ]
    assert await native.cog._generate_and_send(native.message, "A partir de agora só converse em áudio")
    assert await native.cog._generate_and_send(native.message, "Como foi o seu dia?")
    assert native.tts.synthesize_chatbot_attachment.await_count == 2
    assert [args.args[0] for args in native.message.reply.await_args_list] == [None, None]
    native.draw.assert_not_called()
    assert native.tts.synthesize_chatbot_attachment.await_args.kwargs["text"] == "Hoje foi tranquilo, e o seu?"
    assert native.cog._memory.append_turn.await_args.kwargs["user_message"] == "Como foi o seu dia?"
    assert native.cog._reply_store.record_sent.await_args.kwargs["original_user_text"] == "Como foi o seu dia?"


@pytest.mark.asyncio
async def test_temporary_audio_overrides_saved_text_then_returns_to_text(native):
    await native.cog._preferences.set_current(10, 20, 40, native.epoch, mode="text")
    native.cog._router.chat.side_effect = [
        ChatReply("", tool_calls=(call("oneoff", "select_response_format", mode="audio"),)),
        ChatReply("Uma resposta em áudio."), ChatReply("A seguinte fica em texto."),
    ]
    assert await native.cog._generate_and_send(native.message, "essa em áudio")
    assert await native.cog._generate_and_send(native.message, "continue")
    assert native.tts.synthesize_chatbot_attachment.await_count == 1
    assert [args.args[0] for args in native.message.reply.await_args_list] == [None, "A seguinte fica em texto."]
    assert (await native.cog.get_conversation_preferences(10, 20, 40)).mode == "text"


@pytest.mark.asyncio
async def test_preference_voice_and_language_preserve_existing_rate_and_pitch(native):
    native.cog.bot.settings_db.resolve_tts.return_value = {"edge_rate": "+12%", "edge_pitch": "-4Hz"}
    native.cog._router.chat.side_effect = [
        ChatReply("", tool_calls=(call("voice", "set_conversation_preferences", mode="audio",
                                     voice="pt-BR-FranciscaNeural", language="pt-br"),)),
        ChatReply("Agora sim."),
    ]
    assert await native.cog._generate_and_send(native.message, "mude minha voz e continue em áudio")
    arguments = native.tts.synthesize_chatbot_attachment.await_args.kwargs
    assert (arguments["voice"], arguments["language"], arguments["rate"], arguments["pitch"]) == (
        "pt-BR-FranciscaNeural", "pt-br", "+12%", "-4Hz",
    )


@pytest.mark.asyncio
async def test_mutating_duplicate_call_ids_and_new_ids_do_not_repeat_effect(native):
    native.cog._router.chat.side_effect = [
        ChatReply("rascunho", tool_calls=(call("effect-a", "automatic_test"),)),
        ChatReply("rascunho", tool_calls=(call("effect-a", "automatic_test"),)),
        ChatReply("rascunho", tool_calls=(call("effect-b", "automatic_test"),)),
        ChatReply("Concluído."),
    ]
    await native.cog._preferences.set_current(10, 20, 40, native.epoch, mode="text")
    assert await native.cog._generate_and_send(native.message, "pause uma vez")
    assert native.effects == [{}]
    assert native.message.reply.await_args.args == ("Concluído.",)
    assert native.cog._router.chat.await_count == 4
    messages = native.cog._router.chat.await_args.kwargs["messages"]
    results = [item for item in messages if item.role == "tool"]
    assert [item.tool_call_id for item in results] == ["effect-a", "effect-a", "effect-b"]
    assert all(json.loads(item.content)["ok"] for item in results)


@pytest.mark.asyncio
async def test_repeated_identical_read_is_reused_and_forces_final_round(native):
    reads = []

    async def read(arguments):
        reads.append(dict(arguments))
        return {"ok": True, "status": "found", "data": {"value": "resultado grande" * 20}}

    native.extra_specs.append(ToolSpec(
        "read_test", "Consulte o mesmo dado sem efeitos.",
        {"type": "object", "properties": {"query": {"type": "string"}},
         "required": ["query"], "additionalProperties": False},
        permission="read", handler=read,
    ))
    native.cog._router.chat.side_effect = [
        ChatReply("", tool_calls=(call("read-a", "read_test", query="x"),)),
        ChatReply("", tool_calls=(call("read-b", "read_test", query="x"),)),
        ChatReply("Fechado."),
    ]
    assert await native.cog._generate_and_send(native.message, "consulte x")
    assert reads == [{"query": "x"}]
    assert native.cog._router.chat.await_count == 3
    messages = native.cog._router.chat.await_args.kwargs["messages"]
    second = next(item for item in messages if item.role == "tool" and item.tool_call_id == "read-b")
    assert json.loads(second.content) == {
        "ok": True, "status": "reused_read", "source_tool_call_id": "read-a",
        "detail": "Resultado idêntico já está no histórico desta rodada; não repetir a consulta.",
    }
    assert native.cog._last_turn_usage["tools"] == {"seen": 2, "executed": 1, "reused_reads": 1}


@pytest.mark.asyncio
async def test_prompt_stable_identity_precedes_catalog_and_real_state(native):
    native.cog._router.chat.return_value = ChatReply("Oi!")
    voice = SimpleNamespace(id=777, permissions_for=lambda _member: SimpleNamespace(view_channel=False))
    native.message.guild.me = SimpleNamespace(id=999, display_name="Osaka do servidor",
                                             voice=SimpleNamespace(channel=voice))
    native.message.guild.voice_client = SimpleNamespace(channel=voice)
    assert await native.cog._generate_and_send(native.message, "quais recursos você tem?")
    options = native.cog._router.chat.await_args.kwargs
    system = options["system"]
    assert system.startswith("Você é o próprio bot")
    assert system.index("Você é o próprio bot") < system.index("set_conversation_preferences") < system.index("Estado confirmado")
    assert confirmed_state(system)["bot_name"] == "Osaka do servidor"
    assert confirmed_state(system)["voice_state"]["bot"]["connected"] is True
    assert confirmed_state(system)["voice_state"]["bot"]["channel_id"] is None
    assert "777" not in system
    assert options["text_provider_order"] == ("groq", "gemini")
    assert "Geração de imagem adulta" not in system


@pytest.mark.asyncio
async def test_long_persistent_audio_uses_complete_reply_and_complete_receipt(native):
    await native.cog._preferences.set_current(10, 20, 40, native.epoch, mode="audio")
    answer = "a" * 799 + " O detalhe final precisa ser falado inteiro."
    native.cog._router.chat.return_value = ChatReply(answer)
    assert await native.cog._generate_and_send(native.message, "detalhe mais")
    assert native.tts.synthesize_chatbot_attachment.await_args.kwargs["text"] == answer
    receipt = native.cog._reply_store.record_sent.await_args.kwargs
    assert receipt["spoken_text"] == answer and receipt["format"] == "audio"
    assert native.cog._memory.append_turn.await_args.kwargs["assistant_message"] == answer


def voice_channel(identifier, name, *, stage=False):
    from unittest.mock import Mock
    channel = Mock(spec=discord.StageChannel if stage else discord.VoiceChannel)
    channel.id, channel.name = identifier, name
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True)
    return channel


def confirmed_state(system):
    encoded = system.split("Estado confirmado deste turno: ", 1)[1].split("\n", 1)[0]
    return json.loads(encoded)


@pytest.mark.asyncio
async def test_each_model_round_sees_fresh_calls_and_saved_draft_without_execution(native):
    old = voice_channel(777, "Call original")
    current = voice_channel(778, "Call atual", stage=True)
    native.message.author.voice = SimpleNamespace(channel=old)
    native.message.guild.me = SimpleNamespace(id=999, display_name="Osaka", voice=SimpleNamespace(channel=old))
    draft = {"draft_id": "test-draft", "revision": 1, "action": "timeout_member",
             "target_id": "41", "target_ref": "m1", "target_name": "alvo",
             "reason": "", "missing_fields": ["reason"], "options": {"duration_seconds": 600},
             "expires_at": "2026-10-04T12:00:00Z"}
    native.extra_specs.append(ToolSpec("save_action_draft", "Guarde o rascunho incompleto. Salve antes de perguntar o campo que falta; guardar não executa a ação.",
                                      {"type": "object", "properties": {}},
                                      handler=AsyncMock(), permission="automatic_effect"))
    systems = []

    async def model(**kwargs):
        systems.append(kwargs["system"])
        if len(systems) == 1:
            # Simula atualização do Gateway e rascunho salvo durante uma
            # ferramenta, antes da próxima requisição ao modelo.
            native.message.author.voice.channel = current
            native.factories[0].runtime.action_draft = draft
            return ChatReply("rascunho privado", tool_calls=(call("state-change", "automatic_test"),))
        return ChatReply("Qual foi o motivo?")

    native.cog._router.chat.side_effect = model
    await native.cog._preferences.set_current(10, 20, 40, native.epoch, mode="text")
    assert await native.cog._generate_and_send(native.message, "Dê timeout no membro")
    first, second = map(confirmed_state, systems)
    assert first["voice_state"]["same_channel"] is True
    assert first["voice_state"]["author"]["channel_id"] == "777"
    assert second["voice_state"]["same_channel"] is False
    assert second["voice_state"]["author"]["channel_id"] == "778"
    assert second["voice_state"]["author"]["channel_type"] == "stage"
    assert second["voice_state"]["author"]["supported"] is False
    assert second["voice_state"]["bot"]["channel_id"] == "777"
    assert second["voice_state"]["can_listen"] is False
    assert "action_draft" not in first
    assert second["action_draft"] == {key: value for key, value in draft.items()
                                      if key not in {"target_id", "draft_id", "revision", "expires_at"}}
    assert native.factories[0].runtime.action_draft == draft
    declared = native.cog._router.chat.await_args.kwargs["tool_specs"]
    assert "antes de perguntar" in next(spec.description for spec in declared if spec.name == "save_action_draft")
    assert native.message.reply.await_args.args == ("Qual foi o motivo?",)
    assert len(native.refreshes) == 2
    native.tts.synthesize_chatbot_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_action_failure_reports_safe_policy_reason_without_private_preview(native):
    from cogs.chatbot.action_policy import ActionDenied
    reason = "Preciso de um membro que esteja em uma call acessível."

    async def denied(_arguments):
        raise ActionDenied(reason)

    native.extra_specs.append(ToolSpec("propor_acao", "Prepare uma ação autorizada.",
        {"type": "object", "properties": {}}, handler=denied, permission="automatic_effect"))
    native.cog._router.chat.return_value = ChatReply("fala privada que não pode ser publicada",
                                                   tool_calls=(call("denied", "propor_acao"),))
    assert await native.cog._generate_and_send(native.message, "entre na call")
    assert native.message.reply.await_args.args == (reason,)
    native.tts.synthesize_chatbot_attachment.assert_not_awaited()
    assert not native.effects
    assert native.factories[0].runtime.proposals_invalid
    assert not native.cog._processing_indicator._channels


@pytest.mark.asyncio
async def test_context_refresh_respects_turn_deadline_before_model(native, monkeypatch):
    import asyncio
    from cogs.chatbot import constants as C
    monkeypatch.setattr(C, "TOOL_LOOP_BUDGET_SECONDS", .025)
    cancelled = asyncio.Event()

    async def blocked_refresh(_registry):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr("cogs.chatbot.tool_runtime.refresh_tool_context", blocked_refresh)
    assert await asyncio.wait_for(native.cog._generate_and_send(native.message, "qual call estou?"), timeout=.5)
    assert cancelled.is_set()
    native.cog._router.chat.assert_not_awaited()
    native.tts.synthesize_chatbot_attachment.assert_not_awaited()
    assert not native.effects
    assert native.message.reply.await_args.args == ("Não consegui terminar esse pedido dentro do limite deste turno.",)
    assert not native.cog._processing_indicator._channels


@pytest.mark.asyncio
async def test_confirmed_audio_is_not_replayed_and_later_failed_action_keeps_its_reason(native):
    import io
    from cogs.chatbot.action_policy import ActionDenied
    reason = "Meu cargo precisa estar acima do membro para essa ação."

    async def delivered(_arguments):
        await native.message.reply(None, files=[discord.File(io.BytesIO(b"confirmed audio"), filename="audio.mp3")])
        return {"ok": True, "status": "audio_sent", "data": {"message_id": "50"}}

    async def denied(_arguments):
        raise ActionDenied(reason)

    for name, handler in (("convert_reply_audio", delivered), ("propor_acao", denied)):
        native.extra_specs.append(ToolSpec(name, "Ferramenta real disponível.",
            {"type": "object", "properties": {}}, handler=handler, permission="automatic_effect"))
    native.cog._router.chat.return_value = ChatReply("nenhuma transcrição antes do áudio", tool_calls=(
        call("audio-converted", "convert_reply_audio"), call("action-denied", "propor_acao"),
    ))
    assert await native.cog._generate_and_send(native.message, "mande o áudio e dê timeout")
    assert [item.args[0] for item in native.message.reply.await_args_list] == [None, reason]
    native.cog._router.chat.assert_awaited_once()
    native.tts.synthesize_chatbot_attachment.assert_not_awaited()
    native.tts.chatbot_mirror_audio.assert_not_awaited()
    assert not native.cog._processing_indicator._channels


@pytest.mark.asyncio
@pytest.mark.parametrize("has_draft,consumption_failed", [(False, False), (True, False), (True, True)])
async def test_plan_gets_trusted_draft_snapshot_and_failed_consumption_stops_effects(native, has_draft, consumption_failed):
    from cogs.chatbot.action_protocol import ActionProposal
    proposal = ActionProposal("timeout_member", "m1", reason="spam", options={"duration_seconds": 600})
    snapshot = {"draft_id": "draft-identifier", "revision": 2, "action": "timeout_member",
                "target_id": "41", "target_ref": "m1", "reason": "spam", "missing_fields": [],
                "options": {"duration_seconds": 600}}
    context = SimpleNamespace(description="Timeout preparado via política.", actions=("timeout_member",),
                              targets={"m1": SimpleNamespace(id=41)})
    plan = SimpleNamespace(requests=[] if consumption_failed else [{"action": "timeout_member"}],
                           public_error="Esse pedido já foi continuado em outra mensagem." if consumption_failed else "")
    service = native.cog._actions = SimpleNamespace(ready=True, describe=AsyncMock(return_value=context),
        plan=AsyncMock(return_value=plan), content=lambda _plan: "", bind_and_start=AsyncMock())

    async def propose(_arguments):
        native.factories[0].runtime.action_proposals.append(proposal)
        return {"ok": True, "status": "pending", "data": {"requires_staff": True}}

    native.extra_specs.append(ToolSpec("propor_acao", "Prepare o pedido usando referências válidas.",
        {"type": "object", "properties": {}}, handler=propose, permission="automatic_effect"))

    async def model(**_kwargs):
        if has_draft:
            native.factories[0].runtime.action_draft = snapshot
        return ChatReply("", tool_calls=(call("proposal-from-draft", "propor_acao"),))

    native.cog._router.chat.side_effect = model
    assert await native.cog._generate_and_send(native.message, "spam")
    options = service.plan.await_args.kwargs
    assert options["original_user_text"] == "spam"
    if has_draft:
        assert options["action_draft"] == snapshot
    else:
        assert "action_draft" not in options
    if consumption_failed:
        service.bind_and_start.assert_not_awaited()
        assert native.message.reply.await_args.args == (plan.public_error,)
    else:
        service.bind_and_start.assert_awaited_once_with(plan, None)
        native.message.reply.assert_not_awaited()
    native.tts.synthesize_chatbot_attachment.assert_not_awaited()
    native.tts.chatbot_mirror_audio.assert_not_awaited()
