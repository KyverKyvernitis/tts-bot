from __future__ import annotations

import os


FEEDBACK_FORUM_CHANNEL_ID = int(
    str(
        os.getenv("FEEDBACK_FORUM_CHANNEL_ID", "1529204142734573672")
        or "1529204142734573672"
    ).strip()
)

FEEDBACK_DOC_COLLECTION_SUFFIX = "feedbacks"
FEEDBACK_COUNTER_COLLECTION_SUFFIX = "feedback_counters"

STATUS_OPEN = "open"
STATUS_IN_REVIEW = "in_review"
STATUS_RESOLVING = "resolving"
STATUS_RESOLVED = "resolved"
OPEN_STATUSES = (STATUS_OPEN, STATUS_IN_REVIEW)

CATEGORY_OPTIONS = {
    "help": {
        "label": "Ajuda",
        "title": "AJUDA",
        "emoji": "🛠️",
        "description": "Peça ajuda sobre um membro, servidor ou recurso do bot.",
        "accent": 0x5865F2,
        "tag_aliases": ("ajuda", "help"),
    },
    "suggestion": {
        "label": "Sugestão",
        "title": "SUGESTÃO",
        "emoji": "💡",
        "description": "Envie uma ideia para melhorar o bot ou seus sistemas.",
        "accent": 0xFEE75C,
        "tag_aliases": ("sugestão", "sugestao", "suggestion"),
    },
    "bug": {
        "label": "Reportar bug",
        "title": "BUG",
        "emoji": "🐛",
        "description": "Relate um erro ou comportamento inesperado.",
        "accent": 0xED4245,
        "tag_aliases": ("bug", "reportar bug", "erro"),
    },
}

PROTOCOL_PREFIX = "FDB"
DESCRIPTION_MIN_LENGTH = 20
DESCRIPTION_MAX_LENGTH = 3000
MAX_OPEN_FEEDBACKS_PER_USER = 3
MAX_FORWARDED_ATTACHMENTS = 10

INTERNAL_OWNER_PREFIX = "//"
DM_MESSAGE_PREFIX = "_"
DM_STATUS_COMMAND = "_status"
DM_SWITCH_COMMAND = "_trocar"
