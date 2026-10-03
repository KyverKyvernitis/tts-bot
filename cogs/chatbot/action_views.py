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
    "join_voice": ("Aprovar entrada", "Rejeitar"),
    "ban_member": ("Aprovar banimento", "Rejeitar"),
    "send_audio": ("Pode mandar", "Agora não"),
    "speak_voice": ("Pode falar", "Agora não"),
}
_AUDIO_ACTIONS = {"send_audio", "speak_voice"}
_PENDING_STATES = {"created", "pending"}
_MAX_REQUESTS = 2


def _requests_for_message(requests: list[dict]) -> list[dict]:
    return [request for request in requests if request.get("action") in _LABELS][:_MAX_REQUESTS]


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


def _safe_reason(reason: Any) -> str:
    # Não aceita valores estruturados: sua representação pode conter fala privada.
    if not isinstance(reason, str):
        return ""
    text = " ".join(reason.split())[:500]
    text = discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    # IDs citados no motivo não substituem os alvos fixados no pedido.
    return text.replace("<@", "<\u200b@").replace("<#", "<\u200b#")


def _request_description(request: dict) -> str:
    action = request["action"]
    requester = _user(request, "requester_id", "o solicitante")
    pending = str(request.get("state") or "pending") in _PENDING_STATES and _needs_approval(request)
    if action == "join_voice":
        if pending:
            target = _user(request, "target_id", "o usuário informado")
            return f"Pediu permissão: entrar na call de {target} ({_voice_channel(request)}). Pedido de {requester}."
        return f"Entrada em {_voice_channel(request)}, solicitada por {requester}."
    if action == "ban_member":
        target = _user(request, "target_id", "o usuário informado")
        description = (
            f"Pediu permissão: banir {target}. Pedido de {requester}."
            if pending else f"Banimento de {target}, solicitado por {requester}."
        )
        reason = _safe_reason(_value(request, "reason"))
        if reason:
            description += f" Motivo: {reason}"
        description += " Sem apagar o histórico de mensagens."
        return description
    if action == "send_audio":
        if pending:
            return f"Pediu permissão: enviar áudio para {requester}."
        return f"Áudio para {requester}."
    if pending:
        return f"Pediu permissão: falar na call ({_voice_channel(request)}). Pedido de {requester}."
    return f"Fala em {_voice_channel(request)}, solicitada por {requester}."


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
        f"{_request_description(request)}\n{_request_status(request)}"
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
            style=(discord.ButtonStyle.danger if action == "ban_member" else discord.ButtonStyle.success)
            if approve else discord.ButtonStyle.secondary,
            custom_id=custom_id, disabled=disabled, row=row,
        )
        self._service = service
        self._request_id = request_id
        self._approve = approve

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._service.handle_interaction(interaction, self._request_id, approve=self._approve)


class ActionRequestView(discord.ui.View):
    """No máximo dois pedidos e quatro botões, restauráveis após um restart."""
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
