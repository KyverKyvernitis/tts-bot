from __future__ import annotations

import re

BIRTHDAY_THREAD_NAME = "🎂 Aniversários"
BIRTHDAY_DOC_CONFIG = "birthday_config"
BIRTHDAY_DOC_ENTRY = "birthday_entry"
BIRTHDAY_DOC_SENT = "birthday_sent"
DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\s*$")
VAR_RE = re.compile(r"\$\{([a-zA-Z0-9_]+)\}")
DEFAULT_TIMEZONE = "America/Sao_Paulo"
DEFAULT_DELETE_AFTER = 10
DEFAULT_REACTION = "✅"
MAX_TEXT_DISPLAY = 3900
MAX_CALENDAR_NAME_DISPLAY = 40
MAX_CALENDAR_DAY_LINE = 220
PT_MONTHS = {
    1: "Janeiro",
    2: "Fevereiro",
    3: "Março",
    4: "Abril",
    5: "Maio",
    6: "Junho",
    7: "Julho",
    8: "Agosto",
    9: "Setembro",
    10: "Outubro",
    11: "Novembro",
    12: "Dezembro",
}

DEFAULT_TEMPLATES: dict[str, str] = {
    "calendar": (
        "# 🎂 Aniversários"
        "${birthdaycalendarblock}"
    ),
    "saved": "Prontinho, ${usermention}. Seu aniversário foi salvo como **${birthdaydate}** 🎂",
    "updated": "Prontinho, ${usermention}. Atualizei seu aniversário para **${birthdaydate}** 🎂",
    "invalid": "Data inválida. Mande uma data no estilo **dia/mês**.",
    "announce_single": "🎂 Feliz aniversário, ${usermention}! Hoje é seu dia.",
    "announce_group": "🎂 Hoje temos ${birthdaycount} aniversariante(s)!\n\n${birthdaymentions}",
    "empty_calendar": "Nenhum aniversário cadastrado ainda.",
}

TEMPLATE_LABELS: dict[str, str] = {
    "calendar": "Mensagem do calendário",
    "announce_single": "Aviso individual",
    "announce_group": "Aviso agrupado",
}

TEMPLATE_DESCRIPTIONS: dict[str, str] = {
    "calendar": "Mensagem pública acima da thread. É aplicada no canal assim que salvar.",
    "announce_single": "Mensagem normal enviada no canal de avisos para uma pessoa. Pode mencionar o aniversariante.",
    "announce_group": "Mensagem normal enviada quando há mais de um aniversariante no dia. Pode mencionar os aniversariantes.",
}

VARIABLES_BY_TEMPLATE: dict[str, tuple[str, ...]] = {
    "calendar": (
        "guildname", "guildid", "guildmembercount", "birthdaycount", "birthdaycalendarblock", "birthdaycalendar",
        "birthdaycalendarcompact", "birthdaycalendarnext10", "birthdaycalendarnext20",
        "nextbirthdayname", "nextbirthdaydate", "nextbirthdaymention", "nowtimestamp",
        "nowdate", "nowtime", "announcechannel", "registerchannel",
    ),
    "saved": (
        "usermention", "userid", "username", "userdisplayname", "usernickname",
        "birthdayday", "birthdaymonth", "birthdayyear", "birthdaydate", "birthdaydatefull",
        "birthdayage", "birthdaytimestamp", "nowtimestamp",
    ),
    "updated": (
        "usermention", "userid", "username", "userdisplayname", "usernickname",
        "birthdayday", "birthdaymonth", "birthdayyear", "birthdaydate", "birthdaydatefull",
        "birthdayage", "birthdaytimestamp", "nowtimestamp",
    ),
    "invalid": (
        "usermention", "userid", "username", "userdisplayname", "usernickname",
        "usermessage", "validexample", "nowtimestamp",
    ),
    "announce_single": (
        "usermention", "userid", "username", "userdisplayname", "usernickname",
        "birthdayday", "birthdaymonth", "birthdayyear", "birthdaydate", "birthdaydatefull",
        "birthdayage", "birthdaytimestamp", "nowtimestamp", "guildname",
    ),
    "announce_group": (
        "birthdaycount", "birthdaymentions", "birthdaynames", "birthdaylist",
        "birthdaylistnumbered", "nowtimestamp", "guildname",
    ),
    "empty_calendar": ("guildname", "nowtimestamp"),
}

VARIABLE_HELP: dict[str, str] = {
    "usermention": "menciona o membro",
    "userid": "ID do membro",
    "username": "nome de usuário",
    "userdisplayname": "nome exibido no servidor",
    "usernickname": "apelido no servidor",
    "usermessage": "mensagem enviada na thread",
    "birthdayday": "dia do aniversário",
    "birthdaymonth": "mês do aniversário",
    "birthdayyear": "ano informado, se existir",
    "birthdaydate": "data no formato dia/mês",
    "birthdaydatefull": "data com ano quando existir",
    "birthdayage": "idade, se o ano foi informado",
    "birthdaytimestamp": "timestamp Discord do aniversário deste ano",
    "birthdaycount": "quantidade de aniversários cadastrados/no dia",
    "birthdaycalendarblock": "bloco público do calendário com resumo e meses, oculto quando não há aniversariantes",
    "birthdaycalendar": "calendário completo por mês e dia",
    "birthdaycalendarcompact": "calendário em linhas curtas",
    "birthdaycalendarnext10": "próximos 10 aniversários",
    "birthdaycalendarnext20": "próximos 20 aniversários",
    "birthdaymentions": "menções dos aniversariantes do dia",
    "birthdaynames": "nomes dos aniversariantes do dia",
    "birthdaylist": "lista com marcadores dos aniversariantes do dia",
    "birthdaylistnumbered": "lista numerada dos aniversariantes do dia",
    "nextbirthdayname": "nome do próximo aniversariante",
    "nextbirthdaydate": "data do próximo aniversário",
    "nextbirthdaymention": "menção do próximo aniversariante",
    "guildname": "nome do servidor",
    "guildid": "ID do servidor",
    "guildmembercount": "quantidade de membros do servidor",
    "announcechannel": "canal de avisos configurado",
    "registerchannel": "canal do calendário configurado",
    "nowtimestamp": "timestamp atual para usar em <t:${nowtimestamp}:R>",
    "nowdate": "data atual",
    "nowtime": "horário atual",
    "validexample": "exemplo de data válida",
}

VARIABLE_CATEGORIES: dict[str, tuple[str, ...]] = {
    "member": ("usermention", "userid", "username", "userdisplayname", "usernickname"),
    "birthday": ("birthdayday", "birthdaymonth", "birthdayyear", "birthdaydate", "birthdaydatefull", "birthdayage", "birthdaytimestamp"),
    "calendar": ("birthdaycount", "birthdaycalendarblock", "birthdaycalendar", "birthdaycalendarcompact", "birthdaycalendarnext10", "birthdaycalendarnext20", "nextbirthdayname", "nextbirthdaydate", "nextbirthdaymention"),
    "server": ("guildname", "guildid", "guildmembercount", "announcechannel", "registerchannel"),
    "time": ("nowtimestamp", "nowdate", "nowtime"),
    "invalid": ("usermessage", "validexample"),
}
