function defaultTicketOption(id: string, label: string, emoji: string, description: string, flow: string, openingText: string) {
  return {
    id, builtin: true, enabled: true, label, emoji, description, flow,
    confirmation_text: id === "partnership" ? "Ao confirmar, criaremos um ticket privado para conversar com a equipe responsável." : "",
    opening_text: openingText,
    modal_title: id === "report" ? "Enviar denúncia" : id === "suggestion" ? "Enviar sugestão" : "Abrir ticket",
    modal_notice: id === "report" ? "Use esse atendimento apenas para denúncias reais." : "",
    subject_label: id === "report" ? "Usuário denunciado, se houver" : id === "suggestion" ? "Título da sugestão" : "Assunto",
    body_label: id === "suggestion" ? "Descrição da sugestão" : "Explique o atendimento",
    target_channel_id: 0,
    use_report_types: id === "report",
  };
}

export function defaultTicketsConfig() {
  const optionItems = {
    partnership: defaultTicketOption("partnership", "Parceria", "🤝", "Criar um ticket privado de parceria.", "confirm_ticket", "Envie aqui as informações da parceria."),
    report: defaultTicketOption("report", "Denúncia", "👾", "Enviar uma denúncia e abrir um ticket privado.", "modal_ticket", "Envie provas adicionais aqui, se necessário."),
    suggestion: defaultTicketOption("suggestion", "Sugestão", "⚡", "Enviar uma sugestão para o canal configurado.", "modal_channel", "Nova sugestão enviada para análise."),
    other: defaultTicketOption("other", "Outros", "⚙️", "Abrir um ticket para outros assuntos.", "modal_ticket", "Explique aqui o que você precisa e aguarde a equipe."),
  };
  return {
    feature_enabled: false,
    panel: { channel_id: 0, message_id: 0, title: "🎫 Atendimento", description: "Escolha abaixo o tipo de atendimento.", placeholder: "Escolha uma opção", accent_color: "#5865F2", image_url: "", side_image_url: "" },
    channels: { category_id: 0, logs_channel_id: 0, suggestions_channel_id: 0 },
    roles: { staff_role_id: 0, partnership_staff_role_id: 0, report_staff_role_id: 0, other_staff_role_id: 0 },
    enabled: { partnership: true, report: true, suggestion: true, other: true },
    options: { allow_multiple_open_tickets: false, transcript_on_close: true, use_server_webhook: false },
    permissions: {
      everyone: { view_channel: false, send_messages: false, read_message_history: false, attach_files: false, embed_links: false, add_reactions: false },
      staff: { view_channel: true, send_messages: true, read_message_history: true, attach_files: true, embed_links: true, add_reactions: true, manage_messages: true, manage_channels: false },
      creator: { view_channel: true, send_messages: true, read_message_history: true, attach_files: true, embed_links: true, add_reactions: true, mention_everyone: false },
    },
    texts: {
      partnership_confirm: "Ao confirmar, criaremos um ticket privado para você conversar com a equipe responsável por parcerias.",
      partnership_opening: "A equipe irá analisar sua solicitação. Envie aqui as informações da parceria.",
      report_modal_notice: "Ao enviar este formulário, criaremos um ticket privado para você conversar com a equipe.",
      report_opening: "A equipe irá analisar a denúncia. Envie provas adicionais aqui, se necessário.",
      other_opening: "Explique aqui o que você precisa e aguarde a equipe.",
      suggestion_published: "Nova sugestão enviada para análise.",
      close_notice: "Este ticket será fechado em alguns segundos.",
    },
    option_items: optionItems,
    report_types: ["Spam", "Flood", "Ofensa", "Assédio", "Golpe", "Divulgação indevida", "Conteúdo impróprio", "Raid", "Fake account", "Outro"],
    next_ticket_number: 1,
    next_custom_option_number: 1,
    active_tickets: [],
  };
}
