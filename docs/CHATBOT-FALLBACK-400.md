# Limite no principal e HTTP 400 nas alternativas

Um HTTP 429 significa limite de uso. Um HTTP 400 significa que a API rejeitou a requisição; sozinho não prova falta de cota, censura ou chave inválida. Nos logs apresentados, o Groq principal atingiu um limite e três alternativas retornaram 400. Os detalhes desses 400 foram descartados pela versão anterior, portanto não é possível confirmar retrospectivamente a causa específica.

Esta atualização corrige dois defeitos encontrados na revisão:

- Funções sem argumentos omitem `parameters` na declaração do Gemini. Esse campo é opcional na API; o host continua validando os argumentos com seu esquema original, sem aceitar campos extras.
- Quando um lote é interrompido, as chamadas restantes recebem um resultado `not_executed`. Um eventual fechamento recebe o histórico completo, sem executar essas chamadas ou inventar sucesso. Cancelar o turno continua interrompendo tudo.

O erro estruturado `tool_use_failed` do Groq passa pelo orçamento existente de um único reparo de chamada por turno. A geração rejeitada não é executada nem enviada de volta ao modelo. Erros de parâmetros, esquema ou histórico não usam esse reparo. Nenhuma confirmação, síntese de voz, configuração de áudio ou permissão de moderação muda nesta atualização.

Os novos diagnósticos de HTTP 400 usam apenas categorias fixas:

| Código | Significado |
| --- | --- |
| `api_tool_validation` | A API rejeitou o lote gerado pelo modelo. |
| `api_tool_schema` | Há uma indicação de erro na declaração da ferramenta. |
| `api_tool_history` | Há uma indicação de problema nas chamadas e respostas do histórico. |
| `api_context_limit` | A API informou que o contexto excede seu limite. |
| `api_parameter` | Há uma indicação de parâmetro rejeitado. |
| `api_invalid_argument` | Requisição inválida sem diagnóstico mais específico reconhecido. |

Essas categorias não expõem o corpo HTTP, a geração rejeitada, tokens de API ou texto privado de áudio. Quando as alternativas rejeitam a requisição e o principal tem limite, a causa de requisição inválida tem prioridade no erro final; as duas causas continuam registradas no diagnóstico do turno. Esperar a cota do principal não corrige um contrato inválido.

Depois de aplicar pelo updater, reproduza a conversa e filtre os logs:

```bash
sudo journalctl -u tts-bot.service --since "20 minutes ago" --no-pager -o cat \
  | grep -E 'chatbot: (configuration |provider=|skip |exhausted |tool_repair|turno falhou|result=success|turn_usage )' \
  | tail -n 150
```

Se o erro persistir, o campo `diagnostic_code` ajuda a distinguir a próxima correção. Esta atualização não aumenta cotas dos provedores. Qwen permanece desativado até a configuração explícita descrita em `CHATBOT-TOKENS-QWEN.md`.

Referência de contrato: [FunctionDeclaration na API Gemini](https://ai.google.dev/api/caching#FunctionDeclaration), cuja propriedade `parameters` é opcional. A verificação local usou também a [descrição oficial da API v1beta](https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta). Os testes simulam HTTP, Discord e banco; não fazem chamadas autenticadas nem gastam a franquia.
