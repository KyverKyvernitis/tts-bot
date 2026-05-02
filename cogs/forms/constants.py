"""Constantes da cog de formulários.

Convenções de match:
- Triggers comparados após `message.content.strip().lower()` — match exato.
  Substrings/palavras parciais NÃO disparam. Isso evita falso positivo em
  mensagens como "informação", "platform", "claro", etc.
- Variantes "formulário" e "formulario" (sem acento) ambas aceitas.

Convenções de custom_id:
- O botão de submit do form é persistente (sobrevive reboots) e usa sufixo
  `:{guild_id}` pra evitar colisão entre guilds — o discord.py registra
  views persistentes por custom_id como chave global.
- Os outros custom_ids são pra views não-persistentes (timeout > 0); colisão
  entre instâncias não é problema porque cada instância só vive enquanto a
  mensagem dela existe.
"""
from __future__ import annotations


# ===== Triggers =====
TRIGGER_WORDS_FORM = frozenset({"form", "formulário", "formulario"})
TRIGGER_WORD_CUSTOMIZE = "c"


# ===== Custom IDs =====
# Form submit button (persistente; recebe :{guild_id} no fim)
CID_SUBMIT_PREFIX = "forms:submit"

# Customization panel buttons
CID_CUST_PANEL_BTN = "forms:cust:panel"
CID_CUST_MODAL_BTN = "forms:cust:modal"
CID_CUST_RESPONSE_BTN = "forms:cust:response"
CID_CUST_DELETE_BTN = "forms:cust:delete"

# Setup view components
CID_SETUP_FORM_SELECT = "forms:setup:form_select"
CID_SETUP_RESP_SELECT = "forms:setup:resp_select"
CID_SETUP_CONFIRM_BTN = "forms:setup:confirm"


# ===== Timeouts (segundos) =====
CUSTOMIZATION_VIEW_TIMEOUT = 600  # 10min
SETUP_VIEW_TIMEOUT = 600


# ===== Limites do Discord (regras da plataforma) =====
MODAL_TITLE_MAX = 45
TEXT_INPUT_LABEL_MAX = 45
TEXT_INPUT_PLACEHOLDER_MAX = 100
BUTTON_LABEL_MAX = 80


# ===== Limites internos pros campos editáveis =====
PANEL_TITLE_MAX = 250
PANEL_DESCRIPTION_MAX = 1000
RESPONSE_HEADER_MAX = 500
RESPONSE_BODY_MAX = 2000


# ===== Limites do modal de submissão =====
AGE_PRONOUN_MAX = 50
DESCRIPTION_MAX = 1000


# ===== Valores default — sobrescritos via 'c' → modais de edição =====
DEFAULT_PANEL = {
    "title": "📝 Formulário de apresentação",
    "description": "Clique no botão abaixo pra preencher seu formulário.",
    "button_label": "Preencher formulário",
}

DEFAULT_MODAL = {
    "title": "Preencher formulário",
    "age_label": "Idade e pronome (ex: 18, ele)",
    "age_placeholder": "18, ele/dele",
    "desc_label": "Descrição",
    "desc_placeholder": "Conta um pouco sobre você...",
}

# Placeholders aceitos no template:
#   {user}            → menção do usuário (ex: <@123>)
#   {idade_pronome}   → conteúdo cru do field 1 do modal
#   {descricao}       → conteúdo cru do field 2 do modal
DEFAULT_RESPONSE = {
    "header": "**{user}** — `{idade_pronome}`",
    "body": "{descricao}",
}
