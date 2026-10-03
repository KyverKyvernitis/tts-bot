"""Botões persistentes encaminham decisões; a fala privada nunca aparece."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.chatbot.action_views import ActionRequestView, render_action_requests


PRIVATE_SPEECH = "FALA_PRIVADA_QUE_NAO_PODE_APARECER_NA_CONFIRMACAO"


def request(action="join_voice", *, request_id="request-1", state="pending", ask_permission=True):
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
    assert [button.label for button in view.children] == ["Pode entrar", "Agora não"]
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
    [("join_voice", ["Pode entrar", "Agora não"]),
     ("ban_member", ["Pode banir", "Não"])],
)
async def test_each_staff_action_has_a_clear_first_person_approval_pair(action, labels):
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [request(action)])

    assert [button.label for button in view.children] == labels
    assert all(not button.disabled for button in view.children)
    if action == "ban_member":
        assert view.children[0].style == discord.ButtonStyle.danger


@pytest.mark.asyncio
async def test_interface_caps_message_to_one_request_and_two_buttons():
    requests = [request("ban_member", request_id=f"ban-{index}") for index in range(4)]
    for index, item in enumerate(requests):
        item["payload"]["target_id"] = 100 + index
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), requests)
    summary = render_action_requests(requests)

    assert len(view.children) == 2
    assert {button.row for button in view.children} == {0}
    assert "<@100>" in summary
    assert all(f"<@{identifier}>" not in summary for identifier in (101, 102, 103))
    assert all("ban-0" in button.custom_id for button in view.children)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["created", "publishing", "pending"])
@pytest.mark.parametrize("action", ["join_voice", "ban_member"])
async def test_ready_publication_and_pending_staff_requests_are_actionable(state, action):
    item = request(action, state=state)
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [item])

    assert len(view.children) == 2
    assert all(not button.disabled for button in view.children)
    assert render_action_requests([item]).startswith("Posso ")


@pytest.mark.parametrize("state", ["created", "publishing", "pending"])
def test_active_staff_requests_use_first_person_phrasing_and_pinned_targets(state):
    voice = render_action_requests([request("join_voice", state=state)])
    ban = render_action_requests([request("ban_member", state=state)])

    assert "Posso entrar na call de <@41>?" in voice
    assert "Canal: <#50>." in voice
    assert "Posso banir <@41>?" in ban
    assert "Motivo: motivo de teste" in ban
    assert "Vou preservar o histórico de mensagens." in ban
    assert "Estou conversando com <@40>." in voice and "Estou conversando com <@40>." in ban
    assert "Pediu permissão" not in voice + ban


@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
def test_legacy_optional_audio_permission_has_no_public_card_or_private_speech(action):
    item = request(action)
    item["payload"]["reason"] = PRIVATE_SPEECH
    rendered = render_action_requests([item])

    assert rendered == ""
    assert PRIVATE_SPEECH not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["blocked", "approved", "executing", "processing", "completed",
                                   "rejected", "succeeded", "expired", "uncertain", "failed",
                                   "cancelled", "invalid"])
@pytest.mark.parametrize("action", ["join_voice", "ban_member"])
async def test_blocked_consumed_terminal_and_invalid_requests_have_no_card_or_buttons(state, action):
    item = request(action, state=state)
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [item])

    assert view.children == []
    assert render_action_requests([item]) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["join_voice", "ban_member"])
async def test_private_speech_is_absent_from_staff_labels_ids_components_and_card(action):
    service = SimpleNamespace(handle_interaction=AsyncMock())
    item = request(action)
    if action == "join_voice":
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
    item["state"] = "succeeded"
    assert render_action_requests([item]) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
@pytest.mark.parametrize("ask_permission", [True, False])
@pytest.mark.parametrize("state", ["created", "publishing", "pending", "executing", "succeeded",
                                   "blocked", "rejected", "expired", "failed", "uncertain", "cancelled"])
async def test_audio_always_has_no_permission_buttons_footer_or_status(action, ask_permission, state):
    item = request(action, ask_permission=ask_permission, state=state)
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [item])

    assert view.children == []
    assert render_action_requests([item]) == ""
    assert PRIVATE_SPEECH not in render_action_requests([item])


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["join_voice", "ban_member"])
async def test_staff_actions_require_buttons_even_if_payload_asks_to_skip_approval(action):
    item = request(action, ask_permission=False)
    item["ask_permission"] = False
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [item])

    assert len(view.children) == 2
    assert all(not button.disabled for button in view.children)


def test_footer_uses_pinned_target_and_voice_ids_and_neutralizes_reason_mentions():
    ban = request("ban_member")
    ban["payload"]["reason"] = "**spam** @everyone @here <@999> <#998>"
    rendered = render_action_requests([request("join_voice")]) + render_action_requests([ban])

    assert "<@40>" in rendered
    assert "<#50>" in rendered
    assert "<@41>" in rendered
    assert "@everyone" not in rendered and "@here" not in rendered
    assert "<@999>" not in rendered
    assert "<#998>" not in rendered
    assert "**spam**" not in rendered
    assert PRIVATE_SPEECH not in rendered


def test_renderer_never_represents_structured_reason_containing_private_text():
    item = request("ban_member")
    item["payload"]["reason"] = {"text": PRIVATE_SPEECH}

    assert PRIVATE_SPEECH not in render_action_requests([item])


def test_renderer_uses_authoritative_document_requester_over_payload_field():
    item = request("ban_member")
    item["payload"]["requester_id"] = 999

    rendered = render_action_requests([item])

    assert "<@40>" in rendered
    assert "<@999>" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("ask_permission", [True, False])
@pytest.mark.parametrize("action", ["send_audio", "speak_voice"])
async def test_conflicting_document_and_payload_flags_never_enable_audio_ui(ask_permission, action):
    item = request(action, ask_permission=not ask_permission)
    item["ask_permission"] = ask_permission
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [item])

    assert view.children == []
    assert render_action_requests([item]) == ""


@pytest.mark.asyncio
async def test_request_id_that_cannot_fit_discord_component_is_rejected():
    with pytest.raises(ValueError, match="Identificador"):
        ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [request(request_id="x" * 100)])


@pytest.mark.asyncio
async def test_hidden_audio_and_blocked_steps_do_not_take_the_current_card_slot():
    items = [
        request("ban_member", request_id="future-ban", state="blocked"),
        request("send_audio", request_id="hidden-audio"),
        request("join_voice", request_id="current-entry"),
        request("ban_member", request_id="next-ban"),
    ]
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), items)
    assert [button.custom_id for button in view.children] == [
        "chatbot:action:current-entry:approve", "chatbot:action:current-entry:reject",
    ]
    assert render_action_requests(items).startswith("Posso entrar na call")
    assert "Posso banir" not in render_action_requests(items)


@pytest.mark.parametrize("invalid_id", [None, True, -1, 0, "invalid", {"text": PRIVATE_SPEECH}])
def test_invalid_control_ids_use_generic_labels_and_never_represent_private_objects(invalid_id):
    item = request("join_voice")
    item["requester_id"] = invalid_id
    item["payload"]["target_id"] = invalid_id
    item["payload"]["voice_channel_id"] = invalid_id
    rendered = render_action_requests([item])
    assert "o usuário informado" in rendered
    assert "o canal de voz informado" in rendered
    assert "o solicitante" in rendered
    assert PRIVATE_SPEECH not in rendered
    assert "<@" not in rendered and "<#" not in rendered


PRIVILEGED_CARD_CASES = [
    ("timeout_member", "Posso silenciar <@41> por 1 minuto e 1 segundo?", "Pode silenciar"),
    ("untimeout_member", "Posso retirar o timeout de <@41>?", "Pode liberar"),
    ("kick_member", "Posso expulsar <@41> do servidor?", "Pode expulsar"),
    ("unban_member", "Posso desbanir <@41>?", "Pode desbanir"),
    ("purge_messages", "Posso apagar 2 mensagens de <#60>?", "Pode apagar"),
    ("assign_role", "Posso adicionar o cargo <@&70> a <@41>?", "Pode adicionar"),
    ("remove_role", "Posso remover o cargo <@&70> de <@41>?", "Pode remover"),
    ("change_nickname", "Posso alterar o apelido de <@41>?", "Pode alterar"),
    ("edit_channel", "Posso alterar <#60>?", "Pode alterar"),
    ("move_voice", "Posso mudar minha sessão de voz para a call de <@41>?", "Pode mover"),
    ("leave_voice", "Posso sair da call <#50>?", "Pode sair"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,description,label", PRIVILEGED_CARD_CASES)
async def test_expanded_staff_catalog_has_exact_host_targets_first_person_and_no_private_speech(action, description, label):
    item = request(action, ask_permission=False)
    item["ask_permission"] = False
    item["payload"].update({
        "duration_seconds": 61, "role_id": 70, "nickname": "novo-apelido",
        "channel_id": 60, "channel_changes": {"name": "nova-sala", "topic": "novo tópico", "slowmode_delay": 10},
        "message_ids": [111, 112, 111],
    })
    item["public_result"] = PRIVATE_SPEECH
    rendered = render_action_requests([item])
    view = ActionRequestView(SimpleNamespace(handle_interaction=AsyncMock()), [item])
    assert rendered.startswith(description)
    assert "Estou conversando com <@40>." in rendered
    assert "Motivo: motivo de teste" in rendered
    assert [button.label for button in view.children] == [label, "Agora não" if action == "leave_voice" else "Não"]
    assert len(view.children) == 2 and all(not button.disabled for button in view.children)
    assert PRIVATE_SPEECH not in rendered and PRIVATE_SPEECH not in json.dumps(view.to_components())
    if action in {"kick_member", "purge_messages"}:
        assert view.children[0].style == discord.ButtonStyle.danger


def test_purge_card_displays_only_the_exact_fixed_message_ids_up_to_twenty_five():
    item = request("purge_messages")
    item["payload"].update({"channel_id": 60, "message_ids": list(range(100, 130))})
    rendered = render_action_requests([item])
    assert "Posso apagar 25 mensagens de <#60>?" in rendered
    assert "`100`" in rendered and "`124`" in rendered
    assert "`125`" not in rendered and "`129`" not in rendered
    item["payload"]["message_ids"] = {"text": PRIVATE_SPEECH}
    assert PRIVATE_SPEECH not in render_action_requests([item])


def test_channel_card_exposes_proposed_values_and_neutralizes_mentions_without_unknown_fields():
    item = request("edit_channel")
    item["payload"].update({"channel_id": 60, "channel_changes": {
        "name": "**sala** <@999>", "topic": "@everyone <#998>", "slowmode_delay": 0,
        "text": PRIVATE_SPEECH, "permissions": {"text": PRIVATE_SPEECH},
    }})
    rendered = render_action_requests([item])
    assert "Nome novo:" in rendered and "Tópico novo:" in rendered and "Modo lento novo: 0 segundos" in rendered
    assert "<@999>" not in rendered and "<#998>" not in rendered and "@everyone" not in rendered
    assert "**sala**" not in rendered and PRIVATE_SPEECH not in rendered and "permissions" not in rendered
    item["payload"]["channel_changes"] = {"name": {"text": PRIVATE_SPEECH}, "topic": None}
    rendered = render_action_requests([item])
    assert "(valor inválido)" in rendered and "(remover tópico)" in rendered
    assert PRIVATE_SPEECH not in rendered


def test_nickname_card_shows_removal_or_escaped_exact_new_nickname():
    item = request("change_nickname")
    item["payload"]["nickname"] = "**novo** <@999>"
    rendered = render_action_requests([item])
    assert "Apelido novo:" in rendered
    assert "**novo**" not in rendered and "<@999>" not in rendered
    item["payload"]["nickname"] = None
    assert "(remover apelido)" in render_action_requests([item])


def test_nickname_and_channel_cards_show_safe_before_and_after_snapshots():
    nick = request("change_nickname")
    nick["payload"].update({"nickname_before": "antigo", "nickname": "novo"})
    rendered = render_action_requests([nick])
    assert "Apelido atual: antigo" in rendered and "Apelido novo: novo" in rendered
    channel = request("edit_channel")
    channel["payload"].update({
        "channel_id": 60,
        "channel_before": {"name": "antiga", "topic": "antigo tópico", "slowmode_delay": 0},
        "channel_changes": {"name": "nova", "topic": "novo tópico", "slowmode_delay": 10},
    })
    rendered = render_action_requests([channel])
    assert "Nome atual: antiga" in rendered and "Nome novo: nova" in rendered
    assert "Tópico atual: antigo tópico" in rendered and "Tópico novo: novo tópico" in rendered
    assert "Modo lento atual: 0 segundos" in rendered and "Modo lento novo: 10 segundos" in rendered
    channel["payload"]["channel_before"] = {"name": {"text": PRIVATE_SPEECH}, "topic": {"text": PRIVATE_SPEECH}}
    assert PRIVATE_SPEECH not in render_action_requests([channel])


def test_channel_card_keeps_full_proposed_values_with_explicit_old_summary_and_fits_discord():
    item = request("edit_channel")
    item["guild_id"] = item["requester_id"] = 12345678901234567890
    item["payload"].update({
        "channel_id": 12345678901234567890, "reason": "*" * 150,
        "channel_before": {"name": "*" * 100, "topic": "*" * 1024, "slowmode_delay": 21600},
        "channel_changes": {"name": "*" * 100, "topic": "*" * 500, "slowmode_delay": 21600},
    })
    rendered = render_action_requests([item])
    assert "Tópico novo: " + "\\*" * 500 in rendered
    assert "Nome novo: " + "\\*" * 100 in rendered
    assert "… (resumo)" in rendered
    assert len(rendered) <= 2000
    item["payload"]["channel_changes"]["topic"] = "*" * 501
    assert "(valor inválido: excede 500 caracteres)" in render_action_requests([item])


@pytest.mark.parametrize("seconds", [True, -1, 0, 29 * 86400, {"text": PRIVATE_SPEECH}])
def test_timeout_card_never_represents_invalid_or_private_duration(seconds):
    item = request("timeout_member")
    item["payload"]["duration_seconds"] = seconds
    rendered = render_action_requests([item])
    assert "por o período informado" in rendered
    assert PRIVATE_SPEECH not in rendered
