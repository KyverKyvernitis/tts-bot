from __future__ import annotations

from copy import deepcopy
from typing import Any

TICKET_COMMAND_COOLDOWN = 10.0
TICKET_COMMAND_CLEANUP_DELAY = 6.0
EDITOR_TIMEOUT = 900.0
TRANSCRIPT_FETCH_LIMIT = 1000

KIND_PARTNERSHIP = "partnership"
KIND_REPORT = "report"
KIND_SUGGESTION = "suggestion"
KIND_OTHER = "other"
TICKET_KINDS = (KIND_PARTNERSHIP, KIND_REPORT, KIND_SUGGESTION, KIND_OTHER)

PUBLIC_OPTIONS: dict[str, dict[str, str]] = {
    KIND_PARTNERSHIP: {
        "label": "Parceria",
        "emoji": "🤝",
        "description": "Criar um ticket privado de parceria.",
    },
    KIND_REPORT: {
        "label": "Denúncia",
        "emoji": "👾",
        "description": "Enviar uma denúncia e abrir um ticket privado.",
    },
    KIND_SUGGESTION: {
        "label": "Sugestão",
        "emoji": "⚡",
        "description": "Enviar uma sugestão para o canal configurado.",
    },
    KIND_OTHER: {
        "label": "Outros",
        "emoji": "⚙️",
        "description": "Abrir um ticket para outros assuntos.",
    },
}

DEFAULT_REPORT_TYPES = [
    "Spam",
    "Flood",
    "Ofensa",
    "Assédio",
    "Golpe",
    "Divulgação indevida",
    "Conteúdo impróprio",
    "Raid",
    "Fake account",
    "Outro",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "panel": {
        "channel_id": 0,
        "message_id": 0,
        "title": "🎫 Atendimento",
        "description": "Escolha abaixo o tipo de atendimento.",
        "placeholder": "Escolha uma opção",
        "accent_color": "#5865F2",
        "image_url": "",
    },
    "channels": {
        "category_id": 0,
        "logs_channel_id": 0,
        "suggestions_channel_id": 0,
    },
    "roles": {
        "staff_role_id": 0,
        "partnership_staff_role_id": 0,
        "report_staff_role_id": 0,
        "other_staff_role_id": 0,
    },
    "enabled": {
        KIND_PARTNERSHIP: True,
        KIND_REPORT: True,
        KIND_SUGGESTION: True,
        KIND_OTHER: True,
    },
    "options": {
        "allow_multiple_open_tickets": False,
        "transcript_on_close": True,
        "use_server_webhook": False,
    },
    "permissions": {
        "everyone": {
            "view_channel": False,
            "send_messages": False,
            "read_message_history": False,
            "attach_files": False,
            "embed_links": False,
            "add_reactions": False,
        },
        "staff": {
            "view_channel": True,
            "send_messages": True,
            "read_message_history": True,
            "attach_files": True,
            "embed_links": True,
            "add_reactions": True,
            "manage_messages": True,
            "manage_channels": False,
        },
        "creator": {
            "view_channel": True,
            "send_messages": True,
            "read_message_history": True,
            "attach_files": True,
            "embed_links": True,
            "add_reactions": True,
            "mention_everyone": False,
        },
    },
    "texts": {
        "partnership_confirm": "Ao confirmar, criaremos um ticket privado para você conversar com a equipe responsável por parcerias.",
        "partnership_opening": "A equipe irá analisar sua solicitação. Envie aqui as informações da parceria.",
        "report_modal_notice": "Ao enviar este formulário, criaremos um ticket privado para você conversar com a equipe. Use esse atendimento apenas para denúncias reais.",
        "report_opening": "A equipe irá analisar a denúncia. Envie provas adicionais aqui, se necessário.",
        "other_opening": "Explique aqui o que você precisa e aguarde a equipe.",
        "close_notice": "Este ticket será fechado em alguns segundos.",
    },
    "report_types": list(DEFAULT_REPORT_TYPES),
    "next_ticket_number": 1,
    "active_tickets": [],
}


def default_ticket_config() -> dict[str, Any]:
    return deepcopy(DEFAULT_CONFIG)
