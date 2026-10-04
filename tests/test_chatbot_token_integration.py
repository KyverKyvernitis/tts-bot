"""Economia de rodadas conserva confirmação, áudio e isolamento de consumo."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cogs.chatbot.action_protocol import ChatReply
from cogs.chatbot import constants as C
from cogs.chatbot.cog import ChatbotCog, _TURN_USAGE
from cogs.chatbot.context_efficiency import TurnUsage
from cogs.chatbot.tool_registry import ToolSpec
from cogs.chatbot.providers import ProviderRouter
from tests.test_chatbot_action_providers import _Response, _Session, _groq
from tests.test_chatbot_audio_format import turn
from tests.test_chatbot_native_conversation import call, native


@pytest.fixture
def prepared_native(native):
    async def prepare(arguments):
        native.factories[-1].runtime.prepared_response = arguments["text"]
        return {"ok": True, "status": "response_prepared", "data": {"prepared": True}}

    native.extra_specs.append(ToolSpec(
        "preparar_resposta", "Prepare uma resposta independente dos resultados de consultas.",
        {"type": "object", "properties": {"text": {"type": "string"}},
         "required": ["text"], "additionalProperties": False}, handler=prepare,
    ))
    return native


@pytest.mark.asyncio
async def test_preferences_and_prepared_response_save_generation_and_keep_audio_private(prepared_native):
    native = prepared_native
    answer = "Fechou, vou continuar por áudio."
    native.cog._router.chat.return_value = ChatReply(
        "Não publique esta prévia privada.", tool_calls=(
            call("preference", "set_conversation_preferences", mode="audio"),
            call("prepared", "preparar_resposta", text=answer),
        ),
    )

    assert await native.cog._generate_and_send(native.message, "Converse por áudio daqui pra frente")

    native.cog._router.chat.assert_awaited_once()
    native.tts.synthesize_chatbot_attachment.assert_awaited_once()
    assert native.tts.synthesize_chatbot_attachment.await_args.kwargs["text"] == answer
    assert native.message.reply.await_args.args == (None,)
    assert native.message.reply.await_args.kwargs["files"][0].fp.read() == b"the exact mp3 bytes"
    native.tts.chatbot_mirror_audio.assert_awaited_once()
    assert native.cog._memory.append_turn.await_args.kwargs["assistant_message"] == answer


@pytest.mark.asyncio
async def test_prepared_answer_waits_for_unknown_read_results(prepared_native):
    native = prepared_native
    await native.cog._preferences.set_current(10, 20, 40, native.epoch, mode="text")

    async def read(_arguments):
        return {"ok": True, "status": "found", "data": {"queue_count": 3}}

    native.extra_specs.append(ToolSpec("read_queue", "Consulte a fila real.",
                                     {"type": "object", "properties": {}}, handler=read))
    native.cog._router.chat.side_effect = [
        ChatReply("", tool_calls=(
            call("prepared", "preparar_resposta", text="A fila está vazia."),
            call("queue", "read_queue"),
        )),
        ChatReply("Há três músicas na fila."),
    ]

    assert await native.cog._generate_and_send(native.message, "Como está a fila?")

    assert native.cog._router.chat.await_count == 2
    assert native.message.reply.await_args.args == ("Há três músicas na fila.",)
    native.tts.synthesize_chatbot_attachment.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_batch_cannot_reuse_prepared_answer_in_later_successful_batch(prepared_native):
    native = prepared_native
    await native.cog._preferences.set_current(10, 20, 40, native.epoch, mode="text")
    reads = []

    async def read(_arguments):
        reads.append(True)
        if len(reads) == 1:
            return {"ok": False, "status": "failed", "reason": "Não consegui consultar a fila."}
        return {"ok": True, "status": "found", "data": {"queue_count": 3}}

    native.extra_specs.append(ToolSpec("read_queue", "Consulte a fila real.",
                                     {"type": "object", "properties": {}}, handler=read))
    native.cog._router.chat.side_effect = [
        ChatReply("", tool_calls=(
            call("prepared", "preparar_resposta", text="A fila está vazia."),
            call("failed-queue", "read_queue"),
        )),
        ChatReply("", tool_calls=(call("confirmed-queue", "read_queue"),)),
        ChatReply("Consegui consultar: há três músicas na fila."),
    ]

    assert await native.cog._generate_and_send(native.message, "Como está a fila?")

    assert native.cog._router.chat.await_count == 3
    assert native.message.reply.await_args.args == ("Consegui consultar: há três músicas na fila.",)
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_prepared_response_cannot_bypass_privileged_proposal(prepared_native):
    native = prepared_native
    await native.cog._preferences.set_current(10, 20, 40, native.epoch, mode="text")

    async def propose(_arguments):
        return {"ok": True, "status": "awaiting_approval", "data": {"request_count": 1}}

    native.extra_specs.append(ToolSpec("propor_acao", "Proponha um efeito para aprovação da staff.",
        {"type": "object", "properties": {}}, permission="staff", handler=propose))
    native.cog._router.chat.return_value = ChatReply("", tool_calls=(
        call("prepared", "preparar_resposta", text="Já bani o membro."),
        call("moderation", "propor_acao"),
    ))
    reply, _, state = await native.cog._run_native_tools(
        message=native.message, system="Converse naturalmente.", messages=[], config=native.config,
        preferences=await native.cog.get_conversation_preferences(10, 20, 40, native.epoch),
        epoch=native.epoch, visibility="channel:20", reply_target=None, action_context=None,
        temperature=.8, router_options={},
    )

    assert not reply.text
    assert not state["delivered"]
    native.message.reply.assert_not_awaited()
    native.tts.synthesize_chatbot_attachment.assert_not_awaited()
    native.cog._router.chat.assert_awaited_once()


def _report(input_tokens, output_tokens, cached_tokens):
    return {"outcome": "success", "request_count": 1, "attempt_count": 1,
            "generation_attempt_count": 1, "usage_attempt_count": 1,
            "usage_complete": True, "usage": {
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cached_tokens": cached_tokens, "reasoning_tokens": output_tokens // 2,
            }, "usage_field_attempts": {name: 1 for name in
                ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")}}


@pytest.mark.asyncio
async def test_concurrent_turn_reports_do_not_read_shared_last_request():
    ready = {name: asyncio.Event() for name in ("ana", "bia")}
    release = {name: asyncio.Event() for name in ready}
    measured = {"ana": _report(200, 30, 100), "bia": _report(800, 80, 400)}
    shared_router = SimpleNamespace(last_request={})

    async def chat(*, user, request_report):
        ready[user].set()
        await release[user].wait()
        request_report.update(measured[user])
        shared_router.last_request = measured[user]
        await asyncio.sleep(0)
        return ChatReply("oi")

    shared_router.chat = chat
    cog = object.__new__(ChatbotCog)
    cog._router = shared_router

    async def collect(user):
        usage = TurnUsage()
        token = _TURN_USAGE.set(usage)
        try:
            await cog._call_chat(user=user)
            return usage.result()
        finally:
            _TURN_USAGE.reset(token)

    tasks = {name: asyncio.create_task(collect(name)) for name in ready}
    await asyncio.gather(*(event.wait() for event in ready.values()))
    release["bia"].set()
    release["ana"].set()
    results = dict(zip(tasks, await asyncio.gather(*tasks.values())))

    for user, result in results.items():
        assert result["turn_calls"] == result["request_count"] == 1
        assert result["usage"] == measured[user]["usage"]
        assert result["usage_complete"]
        assert result["usage"]["total_tokens"] == result["usage"]["input_tokens"] + result["usage"]["output_tokens"]
    assert _TURN_USAGE.get() is None


@pytest.mark.asyncio
async def test_whole_turn_counts_native_repair_and_read_round_with_real_router(prepared_native, monkeypatch):
    native = prepared_native
    monkeypatch.setattr(C, "GROQ_MODELS", ("openai/gpt-oss-20b",))
    await native.cog._preferences.set_current(10, 20, 40, native.epoch, mode="text")

    async def read(_arguments):
        return {"ok": True, "status": "found", "data": {"queue_count": 3}}

    native.extra_specs.append(ToolSpec("read_queue", "Consulte a fila real.",
        {"type": "object", "properties": {}, "additionalProperties": False}, handler=read))
    responses = []
    for text, calls, usage in (
        ("", (("read_queue", {"unexpected": "invalid"}),), (70, 9, 30, 3)),
        ("", (("read_queue", {}),), (100, 20, 50, 4)),
        ("Há três músicas na fila.", (), (200, 30, 120, 7)),
    ):
        data = _groq(text, calls, finish="tool_calls" if calls else "stop")
        if calls:
            data["choices"][0]["message"]["tool_calls"][0]["id"] = "native-queue"
        data["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1],
                         "total_tokens": usage[0] + usage[1],
                         "prompt_tokens_details": {"cached_tokens": usage[2]},
                         "completion_tokens_details": {"reasoning_tokens": usage[3]}}
        responses.append(_Response(data))
    session = _Session(*responses)
    native.cog._router = ProviderRouter(session, groq_key="offline-placeholder-key")

    assert await native.cog._generate_and_send(native.message, "Como está a fila?")

    assert len(session.requests) == 3
    assert native.message.reply.await_args.args == ("Há três músicas na fila.",)
    report = native.cog._last_turn_usage
    assert report["turn_calls"] == report["reported_calls"] == 2
    assert report["request_count"] == report["generation_attempt_count"] == report["measured_attempts"] == 3
    assert report["usage"] == {"input_tokens": 370, "output_tokens": 59, "total_tokens": 429,
                               "cached_tokens": 200, "reasoning_tokens": 14}
    assert report["usage_complete"] and report["request_count_complete"]
    assert all(count == 3 for count in report["usage_field_attempts"].values())
    native.tts.synthesize_chatbot_attachment.assert_not_awaited()
