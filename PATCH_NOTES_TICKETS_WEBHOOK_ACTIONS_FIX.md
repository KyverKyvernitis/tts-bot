# Tickets — correção webhook ações

- Corrige o botão `🔒 Fechar` das mensagens **Ações do ticket** enviadas por webhook.
- Mantém a mensagem de ações usando webhook quando `Usar webhook do servidor` estiver ligado.
- Garante o import da `CloseConfirmView` usado no callback do botão fechar.
- Melhora a aplicação da foto do servidor no webhook visual, atualizando o avatar padrão do webhook quando possível e também enviando `avatar_url` por mensagem.
- Mantém fallback seguro para envio normal pelo bot se webhook/permissões falharem.
