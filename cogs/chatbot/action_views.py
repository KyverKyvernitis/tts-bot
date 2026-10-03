"""Confirmações persistentes de ações; textos de fala ficam fora da interface.

Os componentes só encaminham o identificador do pedido. O serviço consulta o
estado persistido, verifica permissões atuais e decide se pode executar.
"""
from __future__ import annotations

from typing import Any, Protocol

import discord


class _InteractionHandler(Protocol):
    async def handle_interaction(
        self, interaction: discord.Interaction, request_id: str, *, approve: bool,
    ) -> None: ...


_LABELS = {
    "join_voice": ("Pode entrar", "Agora não"),
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
    "move_voice": ("Pode mover", "Não"),
    "leave_voice": ("Pode sair", "Agora não"),
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


def _voice_channel(request: dict) -> str:
    identifier = _snowflake(_value(request, "voice_channel_id"))
    return f"<#{identifier}>" if identifier is not None else "o canal de voz informado"


def _safe_reason(reason: Any, *, limit: int = 500) -> str:
    # Não aceita valores estruturados: sua representação pode conter fala privada.
    if not isinstance(reason, str):
        return ""
    text = " ".join(reason.split())[:limit]
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    # IDs citados no motivo não substituem os alvos fixados no pedido.
    return text.replace("<@", "<\u200b@").replace("<#", "<\u200b#")


def _channel(request: dict) -> str:
    identifier = _snowflake(_value(request, "affected_channel_id") or _value(request, "channel_id"))
    return f"<#{identifier}>" if identifier is not None else "o canal informado"


def _role(request: dict) -> str:
    identifier = _snowflake(_value(request, "role_id"))
    return f"<@&{identifier}>" if identifier is not None else "o cargo informado"


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
    text = _safe_reason(value)
    return text or empty


def _previous_text(value: Any, *, limit: int, empty: str = "(vazio)") -> str:
    if not isinstance(value, str):
        return "(valor inválido)"
    text = _safe_reason(value[:limit]) or empty
    return text + ("… (resumo)" if len(value) > limit else "")


def _channel_changes(request: dict) -> str:
    changes = _value(request, "channel_changes")
    if not isinstance(changes, dict):
        return ""
    before = _value(request, "channel_before")
    before = before if isinstance(before, dict) else {}
    lines = []
    # Somente campos reconhecidos; valores desconhecidos não são representados.
    if "name" in changes:
        if "name" in before:
            lines.append(f"Nome atual: {_previous_text(before['name'], limit=32)}")
        lines.append(f"Nome novo: {_public_text(changes['name'], limit=100)}")
    if "topic" in changes:
        if "topic" in before:
            prior = before["topic"]
            lines.append("Tópico atual: " + ("(sem tópico)" if prior is None else _previous_text(prior, limit=40)))
        topic = changes["topic"]
        lines.append("Tópico novo: " + ("(remover tópico)" if topic is None else _public_text(topic, limit=500)))
    if "slowmode_delay" in changes:
        if "slowmode_delay" in before:
            prior = before["slowmode_delay"]
            prior_label = f"{prior} segundos" if isinstance(prior, int) and not isinstance(prior, bool) and 0 <= prior <= 21600 else "(valor inválido)"
            lines.append(f"Modo lento atual: {prior_label}")
        delay = changes["slowmode_delay"]
        value = f"{delay} segundos" if isinstance(delay, int) and not isinstance(delay, bool) and 0 <= delay <= 21600 else "(valor inválido)"
        lines.append(f"Modo lento novo: {value}")
    return "\n".join(lines)


def _message_ids(request: dict) -> list[int]:
    values = _value(request, "message_ids")
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(identifier for value in values[:25]
                              if (identifier := _snowflake(value)) is not None))


def _request_description(request: dict) -> str:
    action = request["action"]
    requester = _user(request, "requester_id", "o solicitante")
    if action == "join_voice":
        target = _user(request, "target_id", "o usuário informado")
        return f"Posso entrar na call de {target}?\nCanal: {_voice_channel(request)}.\nEstou conversando com {requester}."
    target = _user(request, "target_id", "o usuário informado")
    if action == "ban_member":
        description = f"Posso banir {target}?"
    elif action == "timeout_member":
        description = f"Posso silenciar {target} por {_duration(_value(request, 'duration_seconds'))}?"
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
            description += "\nMensagens fixadas: " + ", ".join(f"`{value}`" for value in identifiers)
    elif action == "assign_role":
        description = f"Posso adicionar o cargo {_role(request)} a {target}?"
    elif action == "remove_role":
        description = f"Posso remover o cargo {_role(request)} de {target}?"
    elif action == "change_nickname":
        nickname = _value(request, "nickname")
        label = "(remover apelido)" if nickname is None else _public_text(nickname, limit=32)
        description = f"Posso alterar o apelido de {target}?"
        payload = request.get("payload")
        if (isinstance(payload, dict) and "nickname_before" in payload) or "nickname_before" in request:
            prior = _value(request, "nickname_before")
            prior_label = "(sem apelido)" if prior is None else _public_text(prior, limit=32)
            description += f"\nApelido atual: {prior_label}"
        description += f"\nApelido novo: {label}"
    elif action == "edit_channel":
        description = f"Posso alterar {_channel(request)}?"
        changes = _channel_changes(request)
        if changes:
            description += "\n" + changes
    elif action == "move_voice":
        description = f"Posso mudar minha sessão de voz para a call de {target}?\nDestino: {_voice_channel(request)}."
        source = _snowflake(_value(request, "source_voice_channel_id"))
        if source is not None:
            description += f"\nOrigem: <#{source}>."
    elif action == "leave_voice":
        description = f"Posso sair da call {_voice_channel(request)}?"
    else:
        return ""
    description += f"\nEstou conversando com {requester}."
    reason = _safe_reason(_value(request, "reason"), limit=150 if action == "edit_channel" else 500)
    if reason:
        description += f"\nMotivo: {reason}"
    if action == "ban_member":
        description += "\nVou preservar o histórico de mensagens."
    return description


def _request_status(request: dict) -> str:
    state = str(request.get("state") or "pending")
    action = request["action"]
    if state in _PENDING_STATES:
        if not _needs_approval(request):
            return "Áudio: preparando..."
        return "Aguardando aprovação."
    if state in {"approved", "executing", "processing"}:
        return "Áudio: preparando..." if action in _AUDIO_ACTIONS else "Executando..."
    if state in {"completed", "succeeded"}:
        if action == "join_voice":
            return "Entrada concluída."
        if action == "ban_member":
            return "Banimento concluído."
        return "Áudio enviado." if action == "send_audio" else "Fala enviada ao canal de voz."
    if state == "rejected":
        return "Pedido rejeitado."
    if state == "expired":
        return "Pedido expirou. Faça um novo pedido."
    if state == "uncertain":
        return "Não consegui confirmar o resultado. Confira antes de repetir."
    if state in {"failed", "cancelled"}:
        return "Não consegui concluir esse pedido."
    return "Esse pedido não está disponível para aprovação."


def render_action_requests(requests: list[dict]) -> str:
    """Resumo público baseado nos campos de controle, sem transcrição da fala."""
    return "\n\n".join(
        _request_description(request)
        for request in _requests_for_message(requests)
    )


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
