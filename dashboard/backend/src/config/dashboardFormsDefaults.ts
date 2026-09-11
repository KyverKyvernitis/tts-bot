export function defaultFormsConfig() {
  return {
    enabled: false,
    form_channel_id: 0,
    responses_channel_id: 0,
    active_message_id: 0,
    active_c_trigger: { channel_id: 0, message_id: 0 },
    active_c_panel: { channel_id: 0, message_id: 0 },
    pending_reviews: [],
    panel: {
      title: "📝 Formulário de verificação",
      description: "Clique no botão abaixo pra preencher sua verificação.",
      button_label: "Preencher formulário",
      button_emoji: "📝",
      button_style: "primary",
      media_url: "",
      accent_color: "#5865F2",
    },
    modal: {
      title: "Nova verificação",
      fields: [
        { id: "field1", label: "Nome", placeholder: "Leonardo", response_label: "Nome", required: true, long: false, show_in_response: true, enabled: true, min_length: 0, max_length: 120 },
        { id: "field2", label: "Idade e pronome", placeholder: "17, ele/dele", response_label: "Idade e pronome", required: true, long: false, show_in_response: true, enabled: true, min_length: 0, max_length: 120 },
        { id: "field3", label: "Descrição", placeholder: "Conta um pouco sobre você...", response_label: "Descrição", required: true, long: true, show_in_response: true, enabled: true, min_length: 0, max_length: 1000 },
      ],
    },
    response: { title: "Nova Verificação", intro: "", footer: "Enviado por {user} • ID `{user_id}`", media_url: "", accent_color: "#5865F2" },
    approval: {
      enabled: false, role_id: 0, approve_label: "Aprovar", approve_emoji: "✅", approve_style: "success",
      reject_label: "Rejeitar", reject_emoji: "❌", reject_style: "danger",
      approve_dm: "✅ **Você foi aprovado em {guild}!**\nO cargo de aprovado foi aplicado, quando configurado pela staff.",
      reject_dm: "❌ **Você foi rejeitado em {guild}.**\nConfira as regras e tente novamente se a staff permitir.",
    },
  };
}
