"""Confirmações persistentes de ações; textos de fala ficam fora da interface.

Os componentes só encaminham o identificador do pedido. O serviço consulta o
estado persistido, verifica permissões atuais e decide se pode executar.
"""
from __future__ import annotations

from typing import Any, Protocol
import json

import discord


class _InteractionHandler(Protocol):
    async def handle_interaction(
        self, interaction: discord.Interaction, request_id: str, *, approve: bool,
    ) -> None: ...


_LABELS = {
    "ban_member": ("Pode banir", "Não"),
    "timeout_member": ("Pode silenciar", "Não"),
    "untimeout_member": ("Pode liberar", "Não"),
    "kick_member": ("Pode expulsar", "Não"),
    "unban_member": ("Pode desbanir", "Não"),
    "purge_messages": ("Pode apagar", "Não"),
    "assign_role": ("Pode adicionar", "Não"),
    "remove_role": ("Pode remover", "Não"),
    "change_nickname": ("Pode alterar", "Não"),
    "edit_channel": ("Pode alterar", "Não"),
}
_AUDIO_ACTIONS = {"send_audio", "speak_voice"}
_PENDING_STATES = {"created", "publishing", "pending"}
_MAX_REQUESTS = 1


def _requests_for_message(requests: list[dict]) -> list[dict]:
    return [request for request in requests if request.get("action") in _LABELS
            and request.get("state", "pending") in _PENDING_STATES][:_MAX_REQUESTS]


def _value(request: dict, field: str, default: Any = None) -> Any:
    payload = request.get("payload")
    if isinstance(payload, dict) and field in payload:
        return payload[field]
    return request.get(field, default)


def _needs_approval(request: dict) -> bool:
    ask_permission = request.get("ask_permission", _value(request, "ask_permission", True))
    return request["action"] not in _AUDIO_ACTIONS or ask_permission is not False


def _snowflake(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        identifier = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return identifier if identifier > 0 else None


def _user(request: dict, field: str, fallback: str) -> str:
    # A autoria pertence ao documento, não a dados da proposta.
    value = request.get(field) if field == "requester_id" else _value(request, field)
    identifier = _snowflake(value)
    return f"<@{identifier}>" if identifier is not None else fallback


def _channel(request: dict) -> str:
    identifier = _snowflake(_value(request, "affected_channel_id") or _value(request, "channel_id"))
    return f"<#{identifier}>" if identifier is not None else "o canal informado"


def _role(request: dict) -> str:
    identifier = _snowflake(_value(request, "role_id"))
    return f"<@&{identifier}>" if identifier is not None else "informado"


def _duration(seconds: Any) -> str:
    if not isinstance(seconds, int) or isinstance(seconds, bool) or not 1 <= seconds <= 28 * 86400:
        return "o período informado"
    parts = []
    for unit, singular, plural in ((86400, "dia", "dias"), (3600, "hora", "horas"),
                                    (60, "minuto", "minutos"), (1, "segundo", "segundos")):
        count, seconds = divmod(seconds, unit)
        if count:
            parts.append(f"{count} {singular if count == 1 else plural}")
    return " e ".join(parts)


def _public_text(value: Any, *, limit: int = 500, empty: str = "(vazio)") -> str:
    if not isinstance(value, str):
        return "(valor inválido)"
    if len(value) > limit:
        return f"(valor inválido: excede {limit} caracteres)"
    if not value:
        return empty
    # Aspas deixam claros espaços, quebras de linha e caracteres literais.
    # Não reduzimos o valor aplicado, nem o confundimos com menções/alvos.
    text = json.dumps(value, ensure_ascii=False)
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    return text.replace("<@", "<\u200b@").replace("<#", "<\u200b#")


def _channel_changes(request: dict) -> str:
    changes = _value(request, "channel_changes")
    if not isinstance(changes, dict):
        return ""
    lines = []
    # Somente campos reconhecidos; valores desconhecidos não são representados.
    if "name" in changes:
        lines.append(f"Nome: {_public_text(changes['name'], limit=100)}")
    if "topic" in changes:
        topic = changes["topic"]
        lines.append("Tópico: " + ("(remover tópico)" if topic is None else _public_text(topic, limit=500)))
    if "slowmode_delay" in changes:
        delay = changes["slowmode_delay"]
        value = f"{delay} s" if isinstance(delay, int) and not isinstance(delay, bool) and 0 <= delay <= 21600 else "(valor inválido)"
        lines.append(f"Modo lento: {value}")
    return " · ".join(lines)


def _message_ids(request: dict) -> list[int]:
    values = _value(request, "message_ids")
    if not isinstance(values, list) or not 1 <= len(values) <= 25:
        return []
    return list(dict.fromkeys(identifier for value in values
                              if (identifier := _snowflake(value)) is not None))


def _request_description(request: dict) -> str:
    action = request["action"]
    target = _user(request, "target_id", "o usuário informado")
    if action == "ban_member":
        description = f"Posso banir {target}?"
    elif action == "timeout_member":
        duration = _duration(_value(request, "duration_seconds"))
        period = "pelo período informado" if duration == "o período informado" else f"por {duration}"
        description = f"Posso silenciar {target} {period}?"
    elif action == "untimeout_member":
        description = f"Posso retirar o timeout de {target}?"
    elif action == "kick_member":
        description = f"Posso expulsar {target} do servidor?"
    elif action == "unban_member":
        description = f"Posso desbanir {target}?"
    elif action == "purge_messages":
        identifiers = _message_ids(request)
        description = f"Posso apagar {len(identifiers)} mensagens de {_channel(request)}?"
        if identifiers:
            description += "\nMensagens: " + ", ".join(f"`{value}`" for value in identifiers)
    elif action == "assign_role":
        recipient = f"a {target}" if target.startswith("<@") else "ao usuário informado"
        description = f"Posso adicionar o cargo {_role(request)} {recipient}?"
    elif action == "remove_role":
        member = f"de {target}" if target.startswith("<@") else "do usuário informado"
        description = f"Posso remover o cargo {_role(request)} {member}?"
    elif action == "change_nickname":
        nickname = _value(request, "nickname")
        description = (f"Posso remover o apelido de {target}?" if nickname in (None, "")
                       else f"Posso mudar o apelido de {target} para {_public_text(nickname, limit=32)}?")
    elif action == "edit_channel":
        description = f"Posso alterar {_channel(request)}?"
        changes = _channel_changes(request)
        if changes:
            description += "\n" + changes
    else:
        return ""
    return description


def render_action_requests(requests: list[dict]) -> str:
    """Resumo público baseado nos campos de controle, sem transcrição da fala."""
    rendered = "\n\n".join(
        _request_description(request)
        for request in _requests_for_message(requests)
    )
    if len(rendered) > 2000:
        raise ValueError("Os parâmetros completos do pedido não cabem na mensagem Discord.")
    return rendered


class _ActionDecisionButton(discord.ui.Button):
    def __init__(
        self, service: _InteractionHandler, request_id: str, *, action: str,
        approve: bool, disabled: bool, row: int,
    ):
        decision = "approve" if approve else "reject"
        custom_id = f"chatbot:action:{request_id}:{decision}"
        if not request_id or len(custom_id) > 100:
            raise ValueError("Identificador de pedido inválido para o componente Discord.")
        positive, negative = _LABELS[action]
        super().__init__(
            label=positive if approve else negative,
            style=(discord.ButtonStyle.danger if action in {"ban_member", "kick_member", "purge_messages"} else discord.ButtonStyle.success)
            if approve else discord.ButtonStyle.secondary,
            custom_id=custom_id, disabled=disabled, row=row,
        )
        self._service = service
        self._request_id = request_id
        self._approve = approve

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._service.handle_interaction(interaction, self._request_id, approve=self._approve)


class ActionRequestView(discord.ui.View):
    """Uma permissão por etapa, com dois botões e recuperação após restart."""
    def __init__(self, service: _InteractionHandler, requests: list[dict]):
        super().__init__(timeout=None)
        for row, request in enumerate(_requests_for_message(requests)):
            if not _needs_approval(request):
                continue
            request_id = str(request.get("request_id") or "")
            disabled = str(request.get("state") or "pending") not in _PENDING_STATES
            for approve in (True, False):
                self.add_item(_ActionDecisionButton(
                    service, request_id, action=request["action"],
                    approve=approve, disabled=disabled, row=row,
                ))
