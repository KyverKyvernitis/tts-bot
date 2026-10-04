"""Propostas nativas da IA, sem execução nem interpretação de texto livre."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .tool_registry import InvalidToolArguments, validate_tool_arguments


TOOL_NAME = "propor_acao"
ALLOWED_ACTIONS = (
    "send_audio", "speak_voice", "join_voice", "ban_member", "timeout_member", "untimeout_member",
    "kick_member", "unban_member", "purge_messages", "assign_role", "remove_role", "change_nickname",
    "edit_channel", "move_voice", "leave_voice",
)
MAX_PROPOSALS = 4
MAX_AUDIO_TEXT = 800
MAX_REASON = 500
MAX_ARGUMENT_BYTES = 8192
_FIELDS = frozenset({"action", "target_ref", "text", "reason", "ask_permission", "options"})
_TARGET_REF = re.compile(r"(?:[a-z][a-z0-9_]{0,31}|[1-9][0-9]{0,20}|<@!?[1-9][0-9]{0,20}>)\Z", re.ASCII)


@dataclass(frozen=True)
class NativeToolCall:
    id: str
    name: str
    arguments: dict
    provider_data: dict = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class ActionProposal:
    action: str
    target_ref: str = ""
    text: str = ""
    reason: str = ""
    ask_permission: bool = False
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ChatReply:
    text: str
    proposals: tuple[ActionProposal, ...] = ()
    provider: str = ""
    model: str = ""
    tool_calls: tuple[NativeToolCall, ...] = ()


class InvalidActionProposal(ValueError):
    """Erro deliberadamente genérico: argumentos privados nunca entram em logs."""


def enabled_actions(actions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(action for action in dict.fromkeys(actions) if action in ALLOWED_ACTIONS)


def proposal_tool(actions: tuple[str, ...], target_refs: tuple[str, ...] = ()) -> dict:
    """Schema comum; o adaptador monta o envelope específico de cada API."""
    tool = {
        "name": TOOL_NAME,
        "description": (
            "Propõe uma ação ao sistema; esta ferramenta não executa nada. Use apenas as ações "
            "disponíveis. Para ações sobre membros, use apenas as referências confiáveis de membros "
            "fornecidas pelo sistema. Referências são internas: nunca peça ao usuário códigos como m1. "
            "Associe as menções Discord aos membros resolvidos pelo sistema; nome escrito não é identidade confirmada. "
            "Pode escolher enviar áudio ou falar na call espontaneamente, sem pedir autorização. "
            "Para send_audio/speak_voice, omita target_ref: o sistema fixa o autor da conversa como alvo. "
            "Áudio e fala são automáticos quando disponíveis. text é PRIVADO e nunca deve ser repetido "
            "na resposta pública, nem resumido ou antecipado. Entrar, mudar e sair da própria sessão de call são "
            "automáticos. Moderação, cargos e alterações de canais dependem da aprovação da staff. "
            "Até quatro propostas na ordem desejada formam uma sequência: cada "
            "etapa só acontece depois do sucesso da anterior. Para entrar e falar na call do autor, "
            "proponha join_voice antes de speak_voice; a fala aguarda o sucesso confirmado da entrada automática. "
            "Pode propor banimentos de membros distintos, com aprovação separada de cada banimento. "
            "No máximo uma proposta de áudio ou fala por sequência. speak_voice envia o arquivo no chat e "
            "enfileira a mesma fala na call preparada; não gera um áudio separado para cada destino. "
            "Quando o sistema informar reprodução "
            "disponível na call, send_audio já envia o áudio no chat e o reproduz na call atual do bot; "
            "não combine send_audio com speak_voice para a mesma resposta. "
            "Não repita a mesma ação para o mesmo alvo. options contém duração, cargo/canal/mensagens já resolvidos "
            "pelo sistema, ou alterações permitidas. Moderação, cargos e alteração de canal exigem reason "
            "informado pelo usuário; nunca invente um motivo. join_voice/move_voice/leave_voice não exigem reason. "
            "timeout_member exige options.duration_seconds como inteiro em segundos, além de alvo e motivo. "
            "Se faltarem parâmetros, use as ferramentas de rascunho quando estiverem no catálogo e pergunte "
            "apenas o que falta, em linguagem natural. Um rascunho nunca executa nem aprova a ação. "
            "Não diga que executou; o sistema informa o resultado. "
            "Se o alvo for ambíguo, pergunte em texto em vez de propor."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(enabled_actions(actions))},
                "target_ref": {
                    "type": "string",
                    "description": (
                        "Para ações sobre membros: referência ou menção Discord já resolvida pelo sistema; nunca nome livre "
                        "nem ID inventado. O host confirma o alvo; nunca peça referências internas ao usuário. "
                        "Omita em send_audio/speak_voice: o sistema usa o autor da conversa."
                    ),
                    "maxLength": 32,
                },
                "text": {
                    "type": "string", "maxLength": MAX_AUDIO_TEXT,
                    "description": "Fala privada para send_audio/speak_voice; obrigatória nessas ações.",
                },
                "reason": {
                    "type": "string", "maxLength": MAX_REASON,
                    "description": (
                        "Motivo fornecido pelo usuário, obrigatório para ban_member/timeout_member/untimeout_member/"
                        "kick_member/unban_member/purge_messages/assign_role/remove_role/change_nickname/edit_channel. "
                        "Não invente motivo; se faltar, guarde rascunho quando disponível e pergunte. "
                        "Para edit_channel o limite é 150 caracteres. Opcional para join_voice/move_voice/leave_voice."
                    ),
                },
                "options": action_options_schema(),
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    }
    if target_refs:
        tool["parameters"]["properties"]["target_ref"]["enum"] = list(dict.fromkeys(target_refs))
    return tool


def action_options_schema() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "description": "Parâmetros específicos da ação; campos omitidos ainda precisam ser solicitados ao completar um rascunho.",
        "properties": {
            "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 2419200,
                "description": "Obrigatório para timeout_member: duração pedida pelo usuário, em segundos inteiros; de 1 segundo a 28 dias."},
            "role_ref": {"type": "string", "minLength": 1, "maxLength": 64,
                "description": "Obrigatório para assign_role/remove_role; cargo já resolvido pelo sistema."},
            "channel_ref": {"type": "string", "minLength": 1, "maxLength": 64,
                "description": "Obrigatório para edit_channel; canal já resolvido e acessível."},
            "nickname": {"type": "string", "maxLength": 32,
                "description": "Obrigatório para change_nickname; string vazia remove o apelido."},
            "channel_changes": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 100},
                    "topic": {"type": "string", "maxLength": 1024},
                    "slowmode_delay": {"type": "integer", "minimum": 0, "maximum": 21600},
                },
            },
            "message_refs": {"type": "array", "maxItems": 25,
                "description": "Obrigatório para purge_messages; de 1 a 25 mensagens atuais já resolvidas no canal original.",
                "items": {"type": "string", "minLength": 1, "maxLength": 64}},
        },
    }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidActionProposal("proposta inválida")
        result[key] = value
    return result


def _reject_constant(value):
    raise InvalidActionProposal("proposta inválida")


def parse_proposal(name: Any, arguments: Any, actions: tuple[str, ...]) -> ActionProposal:
    if name != TOOL_NAME:
        raise InvalidActionProposal("proposta inválida")
    if isinstance(arguments, str):
        try:
            argument_size = len(arguments.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise InvalidActionProposal("proposta inválida") from exc
        if argument_size > MAX_ARGUMENT_BYTES:
            raise InvalidActionProposal("proposta inválida")
        try:
            arguments = json.loads(
                arguments, object_pairs_hook=_unique_object, parse_constant=_reject_constant,
            )
        except (ValueError, TypeError, RecursionError) as exc:
            raise InvalidActionProposal("proposta inválida") from exc
    if not isinstance(arguments, dict) or set(arguments) - _FIELDS:
        raise InvalidActionProposal("proposta inválida")
    action = arguments.get("action")
    if not isinstance(action, str) or action not in enabled_actions(actions):
        raise InvalidActionProposal("proposta inválida")
    values = {}
    for key, limit in (("target_ref", 32), ("text", MAX_AUDIO_TEXT), ("reason", MAX_REASON)):
        value = arguments.get(key, "")
        if not isinstance(value, str) or len(value) > limit or "\x00" in value:
            raise InvalidActionProposal("proposta inválida")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise InvalidActionProposal("proposta inválida") from exc
        values[key] = value.strip()
    target = values["target_ref"]
    if target and not _TARGET_REF.fullmatch(target):
        raise InvalidActionProposal("proposta inválida")
    if action in {"join_voice", "ban_member", "timeout_member", "untimeout_member", "kick_member", "unban_member", "assign_role", "remove_role", "change_nickname", "move_voice"} and not target:
        raise InvalidActionProposal("proposta inválida")
    if action in {"send_audio", "speak_voice"} and not values["text"]:
        raise InvalidActionProposal("proposta inválida")
    if action not in {"send_audio", "speak_voice"} and values["text"]:
        raise InvalidActionProposal("proposta inválida")
    ask = arguments.get("ask_permission", False)
    if not isinstance(ask, bool):
        raise InvalidActionProposal("proposta inválida")
    # Compatibilidade com respostas de modelos e prompts anteriores: áudio
    # agora é automático; este campo legado não cria um pedido de aprovação.
    if action in {"send_audio", "speak_voice"}:
        ask = False
    try:
        options = validate_tool_arguments(arguments.get("options", {}), action_options_schema())
    except InvalidToolArguments as exc:
        raise InvalidActionProposal("proposta inválida") from exc
    return ActionProposal(action=action, ask_permission=ask, options=options, **values)


def parse_proposals(calls: list[tuple[Any, Any]], actions: tuple[str, ...]) -> tuple[ActionProposal, ...]:
    if len(calls) > MAX_PROPOSALS:
        raise InvalidActionProposal("proposta inválida")
    return tuple(parse_proposal(name, arguments, actions) for name, arguments in calls)


def private_reply_text(text: str, proposals: tuple[ActionProposal, ...]) -> str:
    # Não dependemos de obediência do modelo para impedir que antecipe a fala.
    # O host monta o andamento público, sem mostrar texto nem justificativa privados.
    if any(proposal.action in {"send_audio", "speak_voice"} for proposal in proposals):
        return ""
    return text
