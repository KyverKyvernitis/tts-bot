"""Constantes da cog de formulários.

Convenções de match:
- Triggers comparados após `message.content.strip().lower()` — match exato.
  Substrings/palavras parciais NÃO disparam.
- Variantes "formulário" e "formulario" (sem acento) ambas aceitas.

Convenções de custom_id:
- O botão de submit do form é persistente e usa sufixo `:{guild_id}`.
- Os botões de aprovação/rejeição usam sufixo `:{guild_id}:{user_id}`.
"""
from __future__ import annotations


# ===== Triggers =====
TRIGGER_WORDS_FORM = frozenset({"form", "formulário", "formulario"})
TRIGGER_WORD_CUSTOMIZE = "c"


# ===== Custom IDs =====
# Form submit button (persistente; recebe :{guild_id} no fim)
CID_SUBMIT_PREFIX = "forms:submit"

# Response review buttons (recebem :{guild_id}:{applicant_id} no fim)
CID_REVIEW_APPROVE_PREFIX = "forms:review:approve"
CID_REVIEW_REJECT_PREFIX = "forms:review:reject"

# Customization panel buttons
CID_CUST_PANEL_BTN = "forms:cust:panel"
CID_CUST_MODAL_BTN = "forms:cust:modal"
CID_CUST_RESPONSE_BTN = "forms:cust:response"
CID_CUST_APPROVAL_TOGGLE_BTN = "forms:cust:approval_toggle"
CID_CUST_APPROVAL_EDIT_BTN = "forms:cust:approval_edit"
CID_CUST_DELETE_BTN = "forms:cust:delete"
CID_CUST_APPROVAL_ROLE_SELECT = "forms:cust:approval_role_select"
CID_CUST_BUTTON_STYLE_SELECT_PREFIX = "forms:cust:button_style"

# Setup view components
CID_SETUP_FORM_SELECT = "forms:setup:form_select"
CID_SETUP_RESP_SELECT = "forms:setup:resp_select"
CID_SETUP_CONFIRM_BTN = "forms:setup:confirm"


# ===== Timeouts (segundos) =====
CUSTOMIZATION_VIEW_TIMEOUT = 1800  # 30min; timeout não apaga a sessão.
SETUP_VIEW_TIMEOUT = 600


# ===== Limites do Discord =====
MODAL_TITLE_MAX = 45
TEXT_INPUT_LABEL_MAX = 45
TEXT_INPUT_PLACEHOLDER_MAX = 100
BUTTON_LABEL_MAX = 80
BUTTON_EMOJI_MAX = 32
MEDIA_URL_MAX = 400


# ===== Limites internos pros campos editáveis =====
PANEL_TITLE_MAX = 250
PANEL_DESCRIPTION_MAX = 1000
RESPONSE_TITLE_MAX = 250
RESPONSE_INTRO_MAX = 700
RESPONSE_FOOTER_MAX = 700
REVIEW_DM_MAX = 1000
FIELD_CONFIG_MAX = 160


# ===== Limites do modal de submissão =====
FIELD_VALUE_SHORT_MAX = 120
FIELD_VALUE_LONG_MAX = 1000


# ===== Valores default — sobrescritos via 'c' =====
DEFAULT_PANEL = {
    "title": "📝 Formulário de verificação",
    "description": "Clique no botão abaixo pra preencher sua verificação.",
    "button_label": "Preencher formulário",
    "button_emoji": "📝",
    "button_style": "primary",
    "media_url": "",
}

DEFAULT_MODAL = {
    "title": "Nova verificação",
    "field1_label": "Nome",
    "field1_placeholder": "Leonardo",
    "field1_required": True,
    "field2_label": "Idade e pronome",
    "field2_placeholder": "17, ele",
    "field2_required": True,
    "field3_label": "Descrição",
    "field3_placeholder": "Não sei",
    "field3_required": True,
}

# Placeholders aceitos no título/intro/footer e nas DMs:
#   {user} / {membro}      → menção do usuário
#   {user_name} / {nome_usuario} → display name do usuário
#   {user_id}              → ID do usuário
#   {guild} / {servidor}   → nome do servidor
#   {field1} / {nome}      → campo 1
#   {field2} / {idade}     → campo 2
#   {field3} / {descricao} / {motivo} → campo 3
DEFAULT_RESPONSE = {
    "title": "Nova Verificação",
    "intro": "",
    "footer": "Enviado por {user} • ID `{user_id}`",
    "media_url": "",
}

DEFAULT_APPROVAL = {
    "enabled": False,
    "role_id": 0,
    "approve_label": "Aprovar",
    "approve_emoji": "✅",
    "approve_style": "success",
    "reject_label": "Rejeitar",
    "reject_emoji": "❌",
    "reject_style": "danger",
    "approve_dm": "✅ **Você foi aprovado em {guild}!**\nO cargo de aprovado foi aplicado, quando configurado pela staff.",
    "reject_dm": "❌ **Você foi rejeitado em {guild}.**\nConfira as regras e tente novamente se a staff permitir.",
}
