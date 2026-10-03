"""Propostas nativas da IA, sem execução nem interpretação de texto livre."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


TOOL_NAME = "propor_acao"
ALLOWED_ACTIONS = ("send_audio", "speak_voice", "join_voice", "ban_member")
MAX_PROPOSALS = 4
MAX_AUDIO_TEXT = 800
MAX_REASON = 500
MAX_ARGUMENT_BYTES = 8192
_FIELDS = frozenset({"action", "target_ref", "text", "reason", "ask_permission"})
_TARGET_REF = re.compile(r"[a-z][a-z0-9_]{0,31}\Z", re.ASCII)


@dataclass(frozen=True)
class ActionProposal:
    action: str
    target_ref: str = ""
    text: str = ""
    reason: str = ""
    ask_permission: bool = False


@dataclass(frozen=True)
class ChatReply:
    text: str
    proposals: tuple[ActionProposal, ...] = ()
    provider: str = ""
    model: str = ""


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
            "disponíveis. Para join_voice/ban_member, use apenas as referências confiáveis de membros "
            "fornecidas pelo sistema, como autor ou m1. "
            "Pode escolher enviar áudio ou falar na call espontaneamente, sem pedir autorização. "
            "Para send_audio/speak_voice, omita target_ref: o sistema fixa o autor da conversa como alvo. "
            "Áudio e fala são automáticos quando disponíveis. text é PRIVADO e nunca deve ser repetido "
            "na resposta pública, nem resumido ou antecipado. Entrar na call e banir sempre dependem "
            "da aprovação da staff. Até quatro propostas na ordem desejada formam uma sequência: cada "
            "etapa só acontece depois do sucesso da anterior. Para entrar e falar na call do autor, "
            "proponha join_voice antes de speak_voice; a fala aguarda a aprovação e o sucesso da entrada. "
            "Pode propor banimentos de membros distintos, com aprovação separada de cada banimento. "
            "No máximo uma proposta de áudio ou fala por sequência. Quando o sistema informar reprodução "
            "disponível na call, send_audio já envia o áudio no chat e o reproduz na call atual do bot; "
            "não combine send_audio com speak_voice para a mesma resposta. "
            "Não repita a mesma ação para o mesmo alvo. Não diga que executou; o sistema informa o resultado. "
            "Se o alvo for ambíguo, pergunte em texto em vez de propor."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(enabled_actions(actions))},
                "target_ref": {
                    "type": "string",
                    "description": (
                        "Somente para join_voice/ban_member: referência fornecida pelo sistema; nunca nome, ID "
                        "ou menção livre. Omita em send_audio/speak_voice: o sistema usa o autor da conversa."
                    ),
                    "maxLength": 32,
                },
                "text": {
                    "type": "string", "maxLength": MAX_AUDIO_TEXT,
                    "description": "Fala privada para send_audio/speak_voice; obrigatória nessas ações.",
                },
                "reason": {"type": "string", "maxLength": MAX_REASON},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    }
    if target_refs:
        tool["parameters"]["properties"]["target_ref"]["enum"] = list(dict.fromkeys(target_refs))
    return tool


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
    if action in {"join_voice", "ban_member"} and not target:
        raise InvalidActionProposal("proposta inválida")
    if action in {"send_audio", "speak_voice"} and not values["text"]:
        raise InvalidActionProposal("proposta inválida")
    if action in {"join_voice", "ban_member"} and values["text"]:
        raise InvalidActionProposal("proposta inválida")
    ask = arguments.get("ask_permission", False)
    if not isinstance(ask, bool):
        raise InvalidActionProposal("proposta inválida")
    # Compatibilidade com respostas de modelos e prompts anteriores: áudio
    # agora é automático; este campo legado não cria um pedido de aprovação.
    if action in {"send_audio", "speak_voice"}:
        ask = False
    return ActionProposal(action=action, ask_permission=ask, **values)


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
