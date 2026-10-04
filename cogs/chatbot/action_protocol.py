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

    def __init__(self, message="proposta inválida", *, code="invalid_proposal", path="$"):
        super().__init__(message)
        self.code = code
        self.path = path


def enabled_actions(actions: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(action for action in dict.fromkeys(actions) if action in ALLOWED_ACTIONS)


def proposal_tool(actions: tuple[str, ...], target_refs: tuple[str, ...] = ()) -> dict:
    """Schema comum; o adaptador monta o envelope específico de cada API."""
    tool = {
        "name": TOOL_NAME,
        "description": (
            "Propõe uma ação ao sistema; esta ferramenta não executa nada. As ações disponíveis estão no enum action. "
            "Use referências confirmadas pelo sistema; nunca peça códigos internos ao usuário nem invente alvos. "
            "Nome escrito não confirma identidade; resolva menções ou pergunte se o alvo for ambíguo. "
            "Para send_audio/speak_voice, omita target_ref: o sistema fixa o autor da conversa. "
            "Áudio/fala e entrar/mudar/sair da própria call são automáticos quando disponíveis. "
            "Moderação, cargos e alterações de canais aguardam aprovação separada da staff. "
            "Até quatro propostas na ordem desejada formam uma sequência; só prossiga após sucesso da anterior "
            "e não repita ação/alvo. Para entrar e falar, proponha join_voice antes de speak_voice. "
            "No máximo uma proposta de áudio ou fala por sequência. Ambas enviam o arquivo no chat e a mesma fala "
            "na call disponível; speak_voice exige a call preparada; não combine send_audio com speak_voice. "
            "text é PRIVADO: nunca mostre, resuma ou antecipe a fala na resposta pública. "
            "Moderação, cargos e alteração de canal exigem reason informado pelo usuário; nunca invente um motivo. "
            "join_voice/move_voice/leave_voice não exigem reason. timeout_member exige alvo e "
            "options.duration_seconds em segundos inteiros. Se faltarem parâmetros, use as ferramentas de rascunho "
            "disponíveis e pergunte apenas o que falta. Um rascunho nunca executa nem aprova a ação. "
            "Só confirme execução depois do resultado real."
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
                        "Omita em send_audio/speak_voice (ou deixe vazio): o sistema usa o autor da conversa. "
                        "Em join_voice/move_voice, omitir ou deixar vazio escolhe a call do autor; "
                        "para outro membro, use a referência confirmada. Moderação exige alvo explícito."
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
        # Vazio equivale à omissão somente onde o protocolo permite. Alvos
        # privilegiados continuam obrigatórios na validação semântica abaixo.
        automatic_target = set(enabled_actions(actions)) & {"send_audio", "speak_voice", "join_voice", "move_voice"}
        references = ("", *target_refs) if automatic_target else target_refs
        tool["parameters"]["properties"]["target_ref"]["enum"] = list(dict.fromkeys(references))
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
                    "topic": {"type": "string", "maxLength": 500},
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
            raise InvalidActionProposal(code="duplicate_key")
        result[key] = value
    return result


def _reject_constant(value):
    raise InvalidActionProposal(code="invalid_constant")


def parse_proposal(name: Any, arguments: Any, actions: tuple[str, ...]) -> ActionProposal:
    if name != TOOL_NAME:
        raise InvalidActionProposal(code="tool_name")
    if isinstance(arguments, str):
        try:
            argument_size = len(arguments.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise InvalidActionProposal(code="serialization") from exc
        if argument_size > MAX_ARGUMENT_BYTES:
            raise InvalidActionProposal(code="argument_size")
        try:
            arguments = json.loads(
                arguments, object_pairs_hook=_unique_object, parse_constant=_reject_constant,
            )
        except InvalidActionProposal:
            raise
        except (ValueError, TypeError, RecursionError) as exc:
            raise InvalidActionProposal(code="invalid_json") from exc
    # A declaração nativa e o parser usam o mesmo contrato. O único campo
    # legado continua aceito, sem transformar áudio automático em aprovação.
    schema = proposal_tool(actions)["parameters"]
    schema["properties"]["ask_permission"] = {"type": "boolean"}
    try:
        arguments = validate_tool_arguments(arguments, schema)
    except InvalidToolArguments as exc:
        raise InvalidActionProposal(code=exc.code, path=exc.path) from exc
    action = arguments["action"]
    values = {}
    for key in ("target_ref", "text", "reason"):
        value = arguments.get(key, "")
        values[key] = value.strip()
    target = values["target_ref"]
    if target and not _TARGET_REF.fullmatch(target):
        raise InvalidActionProposal(code="target_reference", path="$/target_ref")
    if action in {"ban_member", "timeout_member", "untimeout_member", "kick_member", "unban_member", "assign_role", "remove_role", "change_nickname"} and not target:
        raise InvalidActionProposal(code="required", path="$/target_ref")
    if action in {"send_audio", "speak_voice"} and not values["text"]:
        raise InvalidActionProposal(code="required", path="$/text")
    if action not in {"send_audio", "speak_voice"} and values["text"]:
        raise InvalidActionProposal(code="forbidden_field", path="$/text")
    ask = arguments.get("ask_permission", False)
    # Compatibilidade com respostas de modelos e prompts anteriores: áudio
    # agora é automático; este campo legado não cria um pedido de aprovação.
    if action in {"send_audio", "speak_voice"}:
        ask = False
    options = arguments.get("options", {})
    return ActionProposal(action=action, ask_permission=ask, options=options, **values)


def parse_proposals(calls: list[tuple[Any, Any]], actions: tuple[str, ...]) -> tuple[ActionProposal, ...]:
    if len(calls) > MAX_PROPOSALS:
        raise InvalidActionProposal(code="max_proposals")
    return tuple(parse_proposal(name, arguments, actions) for name, arguments in calls)


def private_reply_text(text: str, proposals: tuple[ActionProposal, ...]) -> str:
    # Não dependemos de obediência do modelo para impedir que antecipe a fala.
    # O host monta o andamento público, sem mostrar texto nem justificativa privados.
    if any(proposal.action in {"send_audio", "speak_voice"} for proposal in proposals):
        return ""
    return text
