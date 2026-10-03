"""Botões persistentes encaminham decisões; a fala privada nunca aparece."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.chatbot.action_views import ActionRequestView, render_action_requests


PRIVATE_SPEECH = "FALA_PRIVADA_QUE_NAO_PODE_APARECER_NA_CONFIRMACAO"


def request(action="send_audio", *, request_id="request-1", state="pending", ask_permission=True):
    return {
        "action": action, "guild_id": 10, "channel_id": 20, "message_id": 30,
        "request_id": request_id, "requester_id": 40, "state": state,
        "payload": {
            "target_id": 41, "voice_channel_id": 50, "text": PRIVATE_SPEECH,
            "reason": "motivo de teste", "ask_permission": ask_permission,
        },
    }


@pytest.mark.asyncio
async def test_buttons_are_persistent_and_bind_only_the_persisted_request_identity():
    service = SimpleNamespace(handle_interaction=AsyncMock())
    original = request()
    view = ActionRequestView(service, [original])
    restarted = ActionRequestView(service, [dict(original)])

    assert view.timeout is None
    assert view.is_persistent()
    assert restarted.is_persistent()
    assert [button.custom_id for button in view.children] == [
        "chatbot:action:request-1:approve", "chatbot:action:request-1:reject",
    ]
    assert [button.custom_id for button in restarted.children] == [button.custom_id for button in view.children]
    assert [button.label for button in view.children] == ["Pode mandar", "Agora não"]
    assert all(len(button.custom_id) <= 100 for button in view.children)
    service.handle_interaction.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_callback_routes_interaction_and_decision_without_executing_it_locally(approve):
    service = SimpleNamespace(handle_interaction=AsyncMock())
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()))
    view = ActionRequestView(service, [request("ban_member", request_id="ban-44")])
    button = view.children[0 if approve else 1]

    await button.callback(interaction)

    service.handle_interaction.assert_awaited_once_with(interaction, "ban-44", approve=approve)
    interaction.response.send_message.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "labels"),
    [("join_voice", ["Aprovar entrada", "Rejeitar"]),
     ("ban_member", ["Aprovar banimento", "Rejeitar"]),
     ("send_audio", ["Pode mandar", "Agora não"]),
     ("speak_voice", ["Pode falar", "Agora não"])],
)
async def test_each_action_has_a_clear_approval_pair(action, labels):
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [request(action)])

    assert [button.label for button in view.children] == labels
    assert all(not button.disabled for button in view.children)
    if action == "ban_member":
        assert view.children[0].style == discord.ButtonStyle.danger


@pytest.mark.asyncio
async def test_interface_caps_message_to_two_requests_and_four_buttons():
    requests = [request("ban_member", request_id=f"ban-{index}") for index in range(4)]
    for index, item in enumerate(requests):
        item["payload"]["target_id"] = 100 + index
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), requests)
    summary = render_action_requests(requests)

    assert len(view.children) == 4
    assert {button.row for button in view.children} == {0, 1}
    assert "<@100>" in summary and "<@101>" in summary
    assert "<@102>" not in summary and "<@103>" not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["created", "pending"])
async def test_created_and_pending_requests_are_actionable(state):
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [request(state=state)])

    assert all(not button.disabled for button in view.children)
    assert "Aguardando aprovação" in render_action_requests([request(state=state)])


@pytest.mark.parametrize("state", ["created", "pending"])
def test_pending_staff_requests_use_requested_permission_phrasing_and_pinned_targets(state):
    rendered = render_action_requests([request("join_voice", state=state), request("ban_member", state=state)])

    assert "Pediu permissão: entrar na call de <@41> (<#50>)" in rendered
    assert "Pediu permissão: banir <@41>" in rendered
    assert "Motivo: motivo de teste" in rendered
    assert "Sem apagar o histórico de mensagens" in rendered


@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
def test_optional_audio_permission_text_is_generic_and_never_contains_private_speech(action):
    item = request(action)
    item["payload"]["reason"] = PRIVATE_SPEECH
    rendered = render_action_requests([item])

    assert "Pediu permissão: " in rendered
    assert PRIVATE_SPEECH not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "notice"),
    [("approved", "preparando"), ("executing", "preparando"),
     ("completed", "Áudio enviado"), ("rejected", "rejeitado"),
     ("succeeded", "Áudio enviado"),
     ("expired", "Faça um novo pedido"), ("uncertain", "Confira antes de repetir"),
     ("failed", "Não consegui concluir")],
)
async def test_non_pending_requests_disable_both_buttons_and_explain_status(state, notice):
    item = request(state=state)
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [item])

    assert len(view.children) == 2
    assert all(button.disabled for button in view.children)
    assert notice in render_action_requests([item])
    assert "Pediu permissão" not in render_action_requests([item])


@pytest.mark.asyncio
async def test_private_speech_is_absent_from_labels_custom_ids_components_and_public_status():
    service = SimpleNamespace(handle_interaction=AsyncMock())
    item = request("speak_voice")
    item["payload"]["reason"] = PRIVATE_SPEECH
    item["public_result"] = PRIVATE_SPEECH
    view = ActionRequestView(service, [item])

    assert PRIVATE_SPEECH not in json.dumps(view.to_components())
    assert PRIVATE_SPEECH not in render_action_requests([item])
    assert not hasattr(view, "requests")
    assert not hasattr(view, "payload")
    assert not hasattr(view, "text")
    for button in view.children:
        assert not hasattr(button, "payload")
        assert not hasattr(button, "text")
    item["state"] = "completed"
    assert PRIVATE_SPEECH not in render_action_requests([item])


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
async def test_automatic_audio_has_preparing_footer_and_no_approval_buttons(action):
    item = request(action, ask_permission=False)
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [item])

    assert view.children == []
    assert "Áudio: preparando..." in render_action_requests([item])
    assert "Pediu permissão" not in render_action_requests([item])
    assert PRIVATE_SPEECH not in render_action_requests([item])


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["join_voice", "ban_member"])
async def test_staff_actions_require_buttons_even_if_payload_asks_to_skip_approval(action):
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [request(action, ask_permission=False)])

    assert len(view.children) == 2
    assert all(not button.disabled for button in view.children)


def test_footer_uses_pinned_target_and_voice_ids_and_neutralizes_reason_mentions():
    ban = request("ban_member")
    ban["payload"]["reason"] = "**spam** @everyone @here <@999>"
    rendered = render_action_requests([request("join_voice"), ban])

    assert "<@40>" in rendered
    assert "<#50>" in rendered
    assert "<@41>" in rendered
    assert "@everyone" not in rendered and "@here" not in rendered
    assert "<@999>" not in rendered
    assert "**spam**" not in rendered
    assert PRIVATE_SPEECH not in rendered


def test_renderer_never_represents_structured_reason_containing_private_text():
    item = request("ban_member")
    item["payload"]["reason"] = {"text": PRIVATE_SPEECH}

    assert PRIVATE_SPEECH not in render_action_requests([item])


def test_renderer_uses_authoritative_document_requester_over_payload_field():
    item = request("send_audio")
    item["payload"]["requester_id"] = 999

    rendered = render_action_requests([item])

    assert "<@40>" in rendered
    assert "<@999>" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("ask_permission", [True, False])
async def test_document_permission_flag_wins_over_conflicting_payload_flag(ask_permission):
    item = request(ask_permission=not ask_permission)
    item["ask_permission"] = ask_permission
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [item])

    assert len(view.children) == (2 if ask_permission else 0)
    assert ("Aguardando aprovação" in render_action_requests([item])) is ask_permission


@pytest.mark.asyncio
async def test_request_id_that_cannot_fit_discord_component_is_rejected():
    with pytest.raises(ValueError, match="Identificador"):
        ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [request(request_id="x" * 100)])
