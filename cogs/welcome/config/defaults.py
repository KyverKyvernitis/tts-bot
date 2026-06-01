from __future__ import annotations

import re
from pathlib import Path

WELCOME_DOC_CONFIG = "welcome_config"
WELCOME_DOC_SENT = "welcome_sent_message"
WELCOME_DOC_EMOJI = "welcome_temp_emoji"
MAX_TEXT_DISPLAY = 3900
MAX_TEMPLATE_LENGTH = 1800
MAX_FOOTER_LENGTH = 300
MAX_AUTO_ROLES = 10
MAX_SPECIAL_RULES = 15
MAX_RULE_NAME = 80
MAX_WELCOME_VARIANTS = 3
MAX_VARIANT_NAME = 60
VAR_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")
HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
INVITE_CODE_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:discord\.gg/|discord\.com/invite/)?([A-Za-z0-9_-]{2,64})/?$", re.IGNORECASE)
CUSTOM_EMOJI_RE = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})>")
DEFAULT_DECORATIVE_EMOJI_LIMIT = 2
OWNER_GUILD_DECORATIVE_EMOJI_LIMIT = 4
MAX_DECORATIVE_EMOJIS = OWNER_GUILD_DECORATIVE_EMOJI_LIMIT
OWNER_PRESENCE_CACHE_SECONDS = 600.0
DISCORD_EMOJI_MAX_BYTES = 256 * 1024

STAR_SEPARATOR_ASSET = Path(__file__).resolve().parents[3] / "assets" / "welcome" / "star_separator.png"
STAR_SEPARATOR_FILENAME = "welcome-stars.png"

DEFAULT_ACCENT = "#5865F2"
DEFAULT_WEBHOOK_NAME = "Boas-vindas"
DEFAULT_PUBLIC = {
    "title": "Bem-vindo(a)!",
    "body": "Olá, {membro_mencao}. Seja bem-vindo(a) ao **{servidor}**.",
    "footer": "Você é o membro #{contador}.",
}
DEFAULT_DM = {
    "title": "Bem-vindo(a) ao {servidor}!",
    "body": "Que bom ter você por aqui, {membro}. Aproveite o servidor.",
    "footer": "",
}

DEFAULT_EMBED = {
    "content": "",
    "author_name": "",
    "author_icon_mode": "none",
    "author_icon_url": "",
    "author_url": "",
    "title": "",
    "title_url": "",
    "description": "",
    "color": "",
    "color_mode": "fixed",
    "thumbnail_mode": "none",
    "thumbnail_url": "",
    "image_mode": "custom",
    "image_url": "",
    "footer_text": "",
    "footer_icon_mode": "none",
    "footer_icon_url": "",
}

PRESETS: dict[str, dict[str, str]] = {
    "simple": {
        "label": "Simples",
        "emoji": "🌱",
        "title": "Bem-vindo(a)!",
        "body": "Olá, {membro_mencao}. Seja bem-vindo(a) ao **{servidor}**.",
        "footer": "Você é o membro #{contador}.",
    },
    "community": {
        "label": "Comunidade",
        "emoji": "✨",
        "title": "Bem-vindo(a) ao {servidor}!",
        "body": "Ei, {membro_mencao}! Entre, fique à vontade e aproveite o servidor.",
        "footer": "Membro #{contador}",
    },
    "gamer": {
        "label": "Gamer",
        "emoji": "🎮",
        "title": "Novo membro entrou na party",
        "body": "{membro_mencao} acabou de chegar no **{servidor}**.",
        "footer": "Agora somos {contador} membros.",
    },
    "compact": {
        "label": "Compacto",
        "emoji": "💫",
        "title": "Bem-vindo(a), {membro}!",
        "body": "Aproveite o **{servidor}**.",
        "footer": "",
    },
    "invite": {
        "label": "Com convite",
        "emoji": "🎁",
        "title": "Bem-vindo(a), {membro}!",
        "body": "{membro_mencao} chegou pelo convite de {convidador_mencao}.",
        "footer": "Convite: {convite_codigo}",
    },
}

VARIABLE_HELP: dict[str, str] = {
    "membro": "nome exibido do membro",
    "membro_mencao": "menção do membro",
    "usuario": "nome de usuário",
    "usuario_id": "ID do membro",
    "membro_id": "ID do membro",
    "membro_avatar": "avatar do membro",
    "servidor": "nome do servidor",
    "servidor_id": "ID do servidor",
    "servidor_icone": "ícone do servidor",
    "contador": "quantidade atual de membros",
    "criado_em": "data de criação da conta",
    "criado_relativo": "há quanto tempo a conta foi criada",
    "entrou_em": "horário da entrada no servidor",
    "convite_codigo": "código do convite usado",
    "convite": "mesmo valor de {convite_codigo}",
    "convite_canal": "nome do canal do convite",
    "convite_canal_mencao": "menção do canal do convite",
    "convite_usos": "quantidade de usos do convite",
    "convidador": "nome de quem convidou",
    "convidador_nome": "nome de quem convidou",
    "convidador_mencao": "menção de quem convidou",
    "convidador_avatar": "avatar de quem convidou",
    "bot_avatar": "avatar do bot",
    "convite_desconhecido": "texto curto quando o convite não for detectado",
}

STYLE_LABELS = {
    "complete": "Completo",
    "simple": "Simples",
    "compact": "Compacto",
}

RENDER_MODE_LABELS = {
    "components_v2": "Components V2",
    "embed": "Embed",
    "normal": "Mensagem normal",
}

RENDER_MODE_DESCRIPTIONS = {
    "components_v2": "Visual moderno com containers e texto V2",
    "embed": "Visual clássico com embed",
    "normal": "Mensagem leve em texto comum",
}

COLOR_MODE_LABELS = {
    "fixed": "Cor fixa",
    "member_avatar": "Combina com a foto do membro",
}

WEBHOOK_AVATAR_LABELS = {
    "server": "Avatar do servidor",
    "member": "Avatar do membro",
    "inviter": "Avatar de quem convidou",
    "custom": "Avatar por link",
}

EMBED_IMAGE_MODE_LABELS = {
    "none": "Sem imagem",
    "member": "Avatar do membro",
    "inviter": "Avatar de quem convidou",
    "server": "Ícone do servidor",
    "bot": "Avatar do bot",
    "custom": "Link personalizado",
}

EMBED_MAIN_IMAGE_MODE_LABELS = {
    **EMBED_IMAGE_MODE_LABELS,
    "avatar_stars": "Estrelas combinando com o membro",
}

MEDIA_MODE_LABELS = {
    "custom": "Link personalizado",
    "avatar_stars": "Estrelas combinando com o membro",
}

WEBHOOK_NAME_LABELS = {
    "fixed": "Nome personalizado",
    "server": "Nome do servidor",
    "member": "Nome do membro",
    "inviter": "Nome de quem convidou",
}

RULE_TYPE_LABELS = {
    "invite_code": "Convite específico",
    "inviter": "Quem convidou",
    "invite_channel": "Canal do convite",
}

RULE_PRIORITY = ("invite_code", "inviter", "invite_channel")
