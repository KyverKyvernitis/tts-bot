import type { DashboardFieldDefinition, DashboardFieldScope, DashboardTemplateSyntax, DashboardTemplateVariables } from "./dashboardTypes.js";

export const TTS_ENGINE_OPTIONS = [
  { value: "gtts", label: "gTTS" },
  { value: "edge", label: "Microsoft Edge" },
];
export const TTS_LANGUAGE_OPTIONS = [
  { value: "pt-BR", label: "Português — Brasil" },
  { value: "pt-PT", label: "Português — Portugal" },
  { value: "en-US", label: "Inglês — Estados Unidos" },
  { value: "en-GB", label: "Inglês — Reino Unido" },
  { value: "es-ES", label: "Espanhol — Espanha" },
  { value: "es-MX", label: "Espanhol — México" },
  { value: "fr-FR", label: "Francês — França" },
  { value: "de-DE", label: "Alemão — Alemanha" },
  { value: "it-IT", label: "Italiano — Itália" },
  { value: "ja-JP", label: "Japonês" },
  { value: "ko-KR", label: "Coreano" },
];
export const TTS_VOICE_OPTIONS = [
  { value: "pt-BR-FranciscaNeural", label: "Francisca — feminina" },
  { value: "pt-BR-AntonioNeural", label: "Antônio — masculina" },
  { value: "pt-BR-BrendaNeural", label: "Brenda — feminina" },
  { value: "pt-BR-DonatoNeural", label: "Donato — masculina" },
  { value: "pt-BR-ElzaNeural", label: "Elza — feminina" },
  { value: "pt-BR-FabioNeural", label: "Fábio — masculina" },
  { value: "pt-BR-GiovannaNeural", label: "Giovanna — feminina" },
  { value: "pt-BR-HumbertoNeural", label: "Humberto — masculina" },
  { value: "pt-BR-LeilaNeural", label: "Leila — feminina" },
  { value: "pt-BR-LeticiaNeural", label: "Letícia — feminina" },
  { value: "pt-BR-ManuelaNeural", label: "Manuela — feminina" },
  { value: "pt-BR-NicolauNeural", label: "Nicolau — masculina" },
  { value: "pt-BR-YaraNeural", label: "Yara — feminina" },
];
export const BUTTON_STYLE_OPTIONS = [
  { value: "primary", label: "Azul" },
  { value: "secondary", label: "Cinza" },
  { value: "success", label: "Verde" },
  { value: "danger", label: "Vermelho" },
];
export const TICKET_FLOW_OPTIONS = [
  { value: "confirm_ticket", label: "Confirmar e abrir ticket" },
  { value: "modal_ticket", label: "Formulário e ticket" },
  { value: "modal_channel", label: "Formulário para canal" },
  { value: "direct_ticket", label: "Abrir ticket direto" },
];
export const WELCOME_MODE_OPTIONS = [
  { value: "components_v2", label: "Components V2" },
  { value: "embed", label: "Embed" },
  { value: "normal", label: "Mensagem normal" },
];
export const WELCOME_STYLE_OPTIONS = [
  { value: "complete", label: "Completo" },
  { value: "simple", label: "Simples" },
  { value: "compact", label: "Compacto" },
];
export const WELCOME_COLOR_MODE_OPTIONS = [
  { value: "fixed", label: "Cor fixa" },
  { value: "member_avatar", label: "Combinar com avatar do membro" },
];
export const WELCOME_MEDIA_MODE_OPTIONS = [
  { value: "custom", label: "Link personalizado" },
  { value: "avatar_stars", label: "Estrelas pelo avatar" },
];
export const WELCOME_WEBHOOK_NAME_OPTIONS = [
  { value: "fixed", label: "Nome personalizado" },
  { value: "server", label: "Nome do servidor" },
  { value: "member", label: "Nome do membro" },
  { value: "inviter", label: "Nome de quem convidou" },
];
export const WELCOME_WEBHOOK_AVATAR_OPTIONS = [
  { value: "server", label: "Avatar do servidor" },
  { value: "member", label: "Avatar do membro" },
  { value: "inviter", label: "Avatar de quem convidou" },
  { value: "custom", label: "Avatar por link" },
];
export const WELCOME_EMBED_IMAGE_MODE_OPTIONS = [
  { value: "none", label: "Sem imagem" },
  { value: "member", label: "Avatar do membro" },
  { value: "inviter", label: "Avatar de quem convidou" },
  { value: "server", label: "Ícone do servidor" },
  { value: "bot", label: "Avatar do bot" },
  { value: "custom", label: "Link personalizado" },
];
export const WELCOME_EMBED_MAIN_IMAGE_MODE_OPTIONS = [
  ...WELCOME_EMBED_IMAGE_MODE_OPTIONS,
  { value: "avatar_stars", label: "Estrelas pelo avatar" },
];
export const BIRTHDAY_LEAP_MODE_OPTIONS = [
  { value: "feb28", label: "Avisar em 28/02" },
  { value: "mar01", label: "Avisar em 01/03" },
];

export const WELCOME_VARIABLE_HELP: Record<string, string> = {
  membro: "nome exibido do membro",
  membro_mencao: "menção do membro",
  usuario: "nome de usuário",
  usuario_id: "ID do membro",
  membro_id: "ID do membro",
  membro_avatar: "avatar do membro",
  servidor: "nome do servidor",
  servidor_id: "ID do servidor",
  servidor_icone: "ícone do servidor",
  contador: "quantidade atual de membros",
  criado_em: "data de criação da conta",
  criado_relativo: "há quanto tempo a conta foi criada",
  entrou_em: "horário da entrada no servidor",
  convite_codigo: "código do convite usado",
  convite_canal: "nome do canal do convite",
  convite_canal_mencao: "menção do canal do convite",
  convite_usos: "quantidade de usos do convite",
  convidador: "nome de quem convidou",
  convidador_mencao: "menção de quem convidou",
  convidador_avatar: "avatar de quem convidou",
  bot_avatar: "avatar do bot",
};
export const BIRTHDAY_VARIABLE_HELP: Record<string, string> = {
  usermention: "menciona o membro",
  userid: "ID do membro",
  username: "nome de usuário",
  userdisplayname: "nome exibido no servidor",
  usermessage: "mensagem enviada na thread",
  birthdayday: "dia do aniversário",
  birthdaymonth: "mês do aniversário",
  birthdayyear: "ano informado",
  birthdaydate: "data no formato dia/mês",
  birthdayage: "idade calculada",
  birthdaycount: "quantidade de aniversariantes",
  birthdaycalendarblock: "bloco do calendário",
  birthdaycalendar: "calendário completo",
  birthdaymentions: "menções dos aniversariantes",
  guildname: "nome do servidor",
  nowtimestamp: "timestamp atual",
  validexample: "exemplo de data válida",
};

export function templateVariables(syntax: DashboardTemplateSyntax, keys: string[], labels: Record<string, string>): DashboardTemplateVariables {
  return { syntax, items: keys.map((key) => ({ key, label: labels[key] ?? key })) };
}
export const WELCOME_TEMPLATE_VARIABLES = templateVariables("curly", Object.keys(WELCOME_VARIABLE_HELP), WELCOME_VARIABLE_HELP);
export const FORM_VARIABLE_HELP: Record<string, string> = {
  user: "menção do membro",
  user_name: "nome exibido do membro",
  user_id: "ID do membro",
  guild: "nome do servidor",
  field1: "resposta da primeira pergunta",
  field2: "resposta da segunda pergunta",
  field3: "resposta da terceira pergunta",
  field4: "resposta da quarta pergunta",
  field5: "resposta da quinta pergunta",
};
export const FORM_TEMPLATE_VARIABLES = templateVariables("curly", Object.keys(FORM_VARIABLE_HELP), FORM_VARIABLE_HELP);
export const BIRTHDAY_REGISTER_VARIABLES = templateVariables("dollar_curly", [
  "usermention", "userid", "username", "userdisplayname", "birthdayday", "birthdaymonth", "birthdayyear",
  "birthdaydate", "birthdayage", "nowtimestamp", "usermessage", "validexample",
], BIRTHDAY_VARIABLE_HELP);
export const BIRTHDAY_ANNOUNCE_VARIABLES = templateVariables("dollar_curly", [
  "usermention", "userid", "username", "userdisplayname", "birthdayday", "birthdaymonth", "birthdayyear",
  "birthdaydate", "birthdayage", "birthdaycount", "birthdaymentions", "nowtimestamp", "guildname",
], BIRTHDAY_VARIABLE_HELP);
export const BIRTHDAY_CALENDAR_VARIABLES = templateVariables("dollar_curly", [
  "guildname", "birthdaycount", "birthdaycalendarblock", "birthdaycalendar", "nowtimestamp",
], BIRTHDAY_VARIABLE_HELP);

export function messageField(id: string, label: string, scope: DashboardFieldScope, path: string, group: string, maxLength = 1800): DashboardFieldDefinition {
  return { id, label, type: maxLength > 400 ? "textarea" : "text", scope, path, group, maxLength };
}

export function ticketOptionFields(id: string, label: string): DashboardFieldDefinition[] {
  const prefix = `tickets.option_items.${id}`;
  const fieldId = `tickets.option_items.${id}`;
  return [
    { id: `${fieldId}.label`, label: `${label}: nome`, type: "text", scope: "guild", path: `${prefix}.label`, maxLength: 80, group: "Fluxos" },
    { id: `${fieldId}.emoji`, label: `${label}: emoji`, type: "text", scope: "guild", path: `${prefix}.emoji`, maxLength: 32, group: "Fluxos" },
    { id: `${fieldId}.description`, label: `${label}: descrição`, type: "text", scope: "guild", path: `${prefix}.description`, maxLength: 100, group: "Fluxos" },
    { id: `${fieldId}.flow`, label: `${label}: comportamento`, type: "select", scope: "guild", path: `${prefix}.flow`, options: TICKET_FLOW_OPTIONS, group: "Fluxos" },
    { id: `${fieldId}.confirmation_text`, label: `${label}: confirmação`, type: "textarea", scope: "guild", path: `${prefix}.confirmation_text`, maxLength: 1200, group: "Fluxos" },
    { id: `${fieldId}.opening_text`, label: `${label}: mensagem de abertura`, type: "textarea", scope: "guild", path: `${prefix}.opening_text`, maxLength: 1800, group: "Fluxos" },
    { id: `${fieldId}.modal_title`, label: `${label}: título do formulário`, type: "text", scope: "guild", path: `${prefix}.modal_title`, maxLength: 45, group: "Fluxos" },
    { id: `${fieldId}.modal_notice`, label: `${label}: aviso do formulário`, type: "textarea", scope: "guild", path: `${prefix}.modal_notice`, maxLength: 1000, group: "Fluxos" },
    { id: `${fieldId}.subject_label`, label: `${label}: campo de assunto`, type: "text", scope: "guild", path: `${prefix}.subject_label`, maxLength: 45, group: "Fluxos" },
    { id: `${fieldId}.body_label`, label: `${label}: campo de descrição`, type: "text", scope: "guild", path: `${prefix}.body_label`, maxLength: 45, group: "Fluxos" },
    { id: `${fieldId}.target_channel_id`, label: `${label}: canal de destino`, type: "channel", scope: "guild", path: `${prefix}.target_channel_id`, group: "Fluxos" },
    { id: `${fieldId}.use_report_types`, label: `${label}: usar tipos de denúncia`, type: "boolean", scope: "guild", path: `${prefix}.use_report_types`, group: "Fluxos" },
  ];
}

