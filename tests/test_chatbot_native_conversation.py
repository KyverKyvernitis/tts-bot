"""Do catálogo às preferências e ao envio confirmado, sem gatilhos de texto."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.chatbot.action_protocol import ChatReply, NativeToolCall
from cogs.chatbot.cog import ChatbotCog
from cogs.chatbot.preferences import PreferenceStore
from cogs.chatbot.tool_registry import ToolRegistry, ToolSpec
from tests.test_chatbot_audio_format import turn
from tests.test_chatbot_preferences import Collection


@pytest.fixture
def native(turn, monkeypatch):
    turn.cog._preferences = PreferenceStore(Collection(), memory=turn.cog._memory)
    turn.cog.get_conversation_preferences = ChatbotCog.get_conversation_preferences.__get__(turn.cog)
    turn.cog._reply_store = SimpleNamespace(record_sent=AsyncMock())
    factories = []
    effects = []

    async def build(cog, message, config, *, epoch, **kwargs):
        registry = ToolRegistry()
        registry.runtime = SimpleNamespace(action_context=kwargs.get("action_context"),
                                          action_proposals=[], proposals_invalid=False, response_format=None)
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
        factories.append(registry)
        return registry
    monkeypatch.setattr("cogs.chatbot.tool_runtime.build_tool_registry", build)
    turn.factories, turn.effects = factories, effects
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
async def test_prompt_catalog_precedes_real_state_identity_and_style(native):
    native.cog._router.chat.return_value = ChatReply("Oi!")
    native.message.guild.me = SimpleNamespace(id=999, display_name="Osaka do servidor")
    voice = SimpleNamespace(id=777, permissions_for=lambda _member: SimpleNamespace(view_channel=False))
    native.message.guild.voice_client = SimpleNamespace(channel=voice)
    assert await native.cog._generate_and_send(native.message, "quais recursos você tem?")
    options = native.cog._router.chat.await_args.kwargs
    system = options["system"]
    assert system.startswith("Ferramentas reais deste turno.")
    assert system.index("set_conversation_preferences") < system.index("Estado confirmado") < system.index("Você é o próprio bot")
    assert '"bot_name": "Osaka do servidor"' in system
    assert '"voice_connected": true' in system and '"voice_channel_id": null' in system
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
