"""Cartões curtos e persistentes exibem somente a decisão e seus parâmetros."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.chatbot.action_views import ActionRequestView, render_action_requests


PRIVATE_SPEECH = "FALA_PRIVADA_QUE_NAO_PODE_APARECER_NA_CONFIRMACAO"


def request(action="ban_member", *, request_id="request-1", state="pending", ask_permission=True):
    return {
        "action": action, "guild_id": 10, "channel_id": 20, "message_id": 30,
        "request_id": request_id, "requester_id": 40, "state": state,
        "payload": {
            "target_id": 41, "voice_channel_id": 50, "text": PRIVATE_SPEECH,
            "reason": "motivo de teste", "ask_permission": ask_permission,
        },
    }


def service():
    return SimpleNamespace(handle_interaction=AsyncMock())


@pytest.mark.asyncio
async def test_buttons_persist_after_restart_and_bind_only_request_identity():
    handler = service()
    original = request()
    view = ActionRequestView(handler, [original])
    restarted = ActionRequestView(handler, [deepcopy(original)])
    assert view.timeout is None and view.is_persistent() and restarted.is_persistent()
    assert [button.custom_id for button in view.children] == [
        "chatbot:action:request-1:approve", "chatbot:action:request-1:reject",
    ]
    assert [button.custom_id for button in restarted.children] == [button.custom_id for button in view.children]
    assert [button.label for button in view.children] == ["Pode banir", "Não"]
    assert all(len(button.custom_id) <= 100 for button in view.children)
    handler.handle_interaction.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("approve", [True, False])
async def test_callback_delegates_decision_and_current_authorization_to_service(approve):
    handler = service()
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()))
    view = ActionRequestView(handler, [request(request_id="ban-44")])
    await view.children[0 if approve else 1].callback(interaction)
    handler.handle_interaction.assert_awaited_once_with(interaction, "ban-44", approve=approve)
    interaction.response.send_message.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_current_request_has_only_two_decisions_and_no_details_button():
    items = [request(request_id=f"ban-{index}") for index in range(4)]
    for index, item in enumerate(items):
        item["payload"]["target_id"] = 100 + index
    view = ActionRequestView(service(), items)
    assert len(view.children) == 2 and {button.row for button in view.children} == {0}
    assert [button.label for button in view.children] == ["Pode banir", "Não"]
    assert all("ban-0" in button.custom_id for button in view.children)
    assert render_action_requests(items) == "Posso banir <@100>?"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["created", "publishing", "pending"])
@pytest.mark.parametrize("action", ["ban_member", "kick_member", "timeout_member"])
async def test_active_staff_request_has_two_enabled_decisions(state, action):
    item = request(action, state=state)
    view = ActionRequestView(service(), [item])
    assert len(view.children) == 2 and all(not button.disabled for button in view.children)
    assert render_action_requests([item]).startswith("Posso ")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["created", "publishing", "pending", "blocked", "approved",
                                   "executing", "succeeded", "rejected", "expired", "uncertain", "failed"])
@pytest.mark.parametrize("action", ["join_voice", "move_voice", "leave_voice", "send_audio", "speak_voice"])
@pytest.mark.parametrize("ask_permission", [True, False])
async def test_navigation_and_audio_never_have_permission_cards_or_buttons(state, action, ask_permission):
    item = request(action, state=state, ask_permission=ask_permission)
    item["ask_permission"] = ask_permission
    view = ActionRequestView(service(), [item])
    assert view.children == [] and render_action_requests([item]) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["blocked", "approved", "executing", "processing", "completed",
                                   "rejected", "succeeded", "expired", "uncertain", "failed",
                                   "cancelled", "invalid"])
async def test_consumed_and_terminal_staff_requests_have_no_cards_or_buttons(state):
    item = request(state=state)
    assert ActionRequestView(service(), [item]).children == []
    assert render_action_requests([item]) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["ban_member", "kick_member", "purge_messages", "edit_channel"])
async def test_staff_cannot_skip_decision_by_changing_payload_permission_flag(action):
    item = request(action, ask_permission=False)
    item["ask_permission"] = False
    assert len(ActionRequestView(service(), [item]).children) == 2


@pytest.mark.asyncio
async def test_navigation_audio_and_blocked_steps_do_not_take_current_staff_card_slot():
    items = [request(request_id="future-ban", state="blocked"),
             request("send_audio", request_id="hidden-audio"),
             request("join_voice", request_id="auto-entry"),
             request("move_voice", request_id="auto-move"),
             request("leave_voice", request_id="auto-exit"),
             request("kick_member", request_id="current-kick"),
             request(request_id="next-ban")]
    view = ActionRequestView(service(), items)
    assert [button.custom_id for button in view.children] == [
        "chatbot:action:current-kick:approve", "chatbot:action:current-kick:reject",
    ]
    assert render_action_requests(items) == "Posso expulsar <@41> do servidor?"


@pytest.mark.asyncio
async def test_private_speech_status_audit_reason_and_requester_are_not_in_ui():
    item = request()
    item["payload"].update({"reason": PRIVATE_SPEECH, "requester_id": 999,
                           "delete_message_seconds": 0, "unknown": PRIVATE_SPEECH})
    item["public_result"] = PRIVATE_SPEECH
    item["status"] = PRIVATE_SPEECH
    view = ActionRequestView(service(), [item])
    assert render_action_requests([item]) == "Posso banir <@41>?"
    assert PRIVATE_SPEECH not in json.dumps(view.to_components())
    assert all(not hasattr(view, attribute) for attribute in ("requests", "payload", "text"))
    assert all(not hasattr(button, attribute) for button in view.children for attribute in ("payload", "text"))


@pytest.mark.asyncio
async def test_request_identity_that_cannot_fit_component_is_rejected():
    with pytest.raises(ValueError, match="Identificador"):
        ActionRequestView(service(), [request(request_id="x" * 100)])


@pytest.mark.parametrize("invalid_id", [None, True, -1, 0, "invalid", {"text": PRIVATE_SPEECH}])
def test_invalid_control_ids_use_generic_labels_without_private_data(invalid_id):
    item = request("assign_role")
    item["requester_id"] = invalid_id
    item["payload"].update({"target_id": invalid_id, "role_id": invalid_id})
    assert render_action_requests([item]) == "Posso adicionar o cargo informado ao usuário informado?"
    assert PRIVATE_SPEECH not in render_action_requests([item])


STAFF_CASES = [
    ("ban_member", "Posso banir <@41>?", "Pode banir"),
    ("timeout_member", "Posso silenciar <@41> por 1 minuto e 1 segundo?", "Pode silenciar"),
    ("untimeout_member", "Posso retirar o timeout de <@41>?", "Pode liberar"),
    ("kick_member", "Posso expulsar <@41> do servidor?", "Pode expulsar"),
    ("unban_member", "Posso desbanir <@41>?", "Pode desbanir"),
    ("purge_messages", "Posso apagar 2 mensagens de <#60>?\nMensagens: `111`, `112`", "Pode apagar"),
    ("assign_role", "Posso adicionar o cargo <@&70> a <@41>?", "Pode adicionar"),
    ("remove_role", "Posso remover o cargo <@&70> de <@41>?", "Pode remover"),
    ("change_nickname", 'Posso mudar o apelido de <@41> para "novo-apelido"?', "Pode alterar"),
    ("edit_channel", 'Posso alterar <#60>?\nNome: "nova-sala" · Tópico: "novo tópico" · Modo lento: 10 s', "Pode alterar"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected,label", STAFF_CASES)
async def test_staff_catalog_shows_only_natural_action_target_and_complete_critical_parameters(action, expected, label):
    item = request(action, ask_permission=False)
    item["payload"].update({"duration_seconds": 61, "role_id": 70, "nickname": "novo-apelido",
        "channel_id": 60, "channel_changes": {"name": "nova-sala", "topic": "novo tópico", "slowmode_delay": 10},
        "message_ids": [111, 112, 111], "nickname_before": PRIVATE_SPEECH,
        "channel_before": {"name": PRIVATE_SPEECH}, "reason": PRIVATE_SPEECH})
    before = deepcopy(item)
    view = ActionRequestView(service(), [item])
    assert render_action_requests([item]) == expected
    assert len(expected.splitlines()) <= 2
    assert [button.label for button in view.children] == [label, "Não"]
    assert all(not button.disabled for button in view.children)
    assert item == before  # Rendering does not change the exact approved payload.
    if action in {"ban_member", "kick_member", "purge_messages"}:
        assert view.children[0].style == discord.ButtonStyle.danger


def test_purge_keeps_all_twenty_five_pinned_ids_without_silently_truncating_invalid_list():
    item = request("purge_messages")
    identifiers = list(range(100, 125))
    item["payload"].update({"channel_id": 60, "message_ids": identifiers})
    rendered = render_action_requests([item])
    assert rendered == "Posso apagar 25 mensagens de <#60>?\nMensagens: " + ", ".join(f"`{value}`" for value in identifiers)
    item["payload"]["message_ids"] = list(range(100, 130))
    assert "`100`" not in render_action_requests([item])
    assert "25 mensagens" not in render_action_requests([item])
    item["payload"]["message_ids"] = {"text": PRIVATE_SPEECH}
    assert PRIVATE_SPEECH not in render_action_requests([item])


def test_proposed_channel_values_neutralize_mentions_and_never_show_unknown_fields_or_old_snapshot():
    item = request("edit_channel")
    item["payload"].update({"channel_id": 60, "channel_before": {"name": PRIVATE_SPEECH},
        "channel_changes": {"name": "**sala** <@999>", "topic": "@everyone <#998>",
                            "slowmode_delay": 0, "text": PRIVATE_SPEECH,
                            "permissions": {"text": PRIVATE_SPEECH}}})
    rendered = render_action_requests([item])
    assert rendered.startswith("Posso alterar <#60>?\nNome: ")
    assert "Tópico: " in rendered and "Modo lento: 0 s" in rendered
    assert all(value not in rendered for value in ("<@999>", "<#998>", "@everyone", "**sala**", PRIVATE_SPEECH, "permissions"))
    item["payload"]["channel_changes"] = {"name": {"text": PRIVATE_SPEECH}, "topic": None}
    rendered = render_action_requests([item])
    assert "(valor inválido)" in rendered and "(remover tópico)" in rendered and PRIVATE_SPEECH not in rendered


@pytest.mark.parametrize("value", [None, ""])
def test_nickname_removal_is_a_single_natural_line(value):
    item = request("change_nickname")
    item["payload"].update({"nickname_before": PRIVATE_SPEECH, "nickname": value})
    assert render_action_requests([item]) == "Posso remover o apelido de <@41>?"


def test_nickname_exact_spaces_and_literal_newlines_are_represented_without_new_ui_lines():
    item = request("change_nickname")
    item["payload"]["nickname"] = "  novo\napelido  "
    rendered = render_action_requests([item])
    assert '"  novo' in rendered and 'apelido  "' in rendered
    assert len(rendered.splitlines()) == 1
    assert "…" not in rendered


def test_channel_parameters_keep_full_proposed_values_and_fit_discord_without_before_summary():
    item = request("edit_channel")
    item["payload"].update({"channel_id": 12345678901234567890,
        "channel_before": {"name": PRIVATE_SPEECH, "topic": "*" * 1024},
        "channel_changes": {"name": "*" * 100, "topic": "*" * 500, "slowmode_delay": 21600}})
    rendered = render_action_requests([item])
    assert 'Tópico: "' + "\\*" * 500 + '"' in rendered
    assert 'Nome: "' + "\\*" * 100 + '"' in rendered
    assert len(rendered) <= 2000 and len(rendered.splitlines()) == 2
    assert "…" not in rendered and "resumo" not in rendered and PRIVATE_SPEECH not in rendered
    item["payload"]["channel_changes"]["topic"] = "*" * 501
    assert "(valor inválido: excede 500 caracteres)" in render_action_requests([item])


def test_unrepresentable_critical_values_fail_instead_of_being_silently_truncated():
    item = request("edit_channel")
    item["payload"]["channel_changes"] = {"topic": "\0" * 500}
    with pytest.raises(ValueError, match="parâmetros completos"):
        render_action_requests([item])


@pytest.mark.parametrize("seconds", [True, -1, 0, 29 * 86400, {"text": PRIVATE_SPEECH}])
def test_invalid_private_duration_is_not_reflected_into_card(seconds):
    item = request("timeout_member")
    item["payload"]["duration_seconds"] = seconds
    assert render_action_requests([item]) == "Posso silenciar <@41> pelo período informado?"


def test_timeout_keeps_full_exact_duration_instead_of_rounding():
    item = request("timeout_member")
    item["payload"]["duration_seconds"] = 86400 + 3600 + 60 + 1
    assert render_action_requests([item]) == "Posso silenciar <@41> por 1 dia e 1 hora e 1 minuto e 1 segundo?"
