# Tokens e reserva Qwen gratuita

O projeto mantém Groq como prioridade, Gemini como alternativa e prepara uma terceira reserva de texto: Qwen3-30B-A3B pela Cloudflare Workers AI. O modelo é fixo, `@cf/qwen/qwen3-30b-a3b-fp8`; esta integração não escolhe modelos pagos nem contrata planos. A reserva começa desativada, mesmo se já houver credenciais Cloudflare para imagens, e exige `CHATBOT_CLOUDFLARE_ENABLED=true`. A preferência já salva entre Groq e Gemini continua válida. Imagens seguem a cadeia de visão existente, pois esse Qwen aceita texto.

## Economia de tokens

O índice de capacidades e as instruções estáveis vêm antes dos dados que mudam em cada conversa. Isso favorece o cache por prefixo dos provedores; o cache depende de correspondência exata e não é garantido. No Groq, os dois modelos GPT-OSS utilizados suportam cache automático. Tokens em cache continuam fazendo parte da entrada reportada; não devem ser somados novamente ao total.

O modelo continua sabendo quais recursos existem, mas as declarações detalhadas de ferramentas podem ser carregadas por grupos conforme a necessidade. Consultas retornam resultados limitados, e o host preserva as referências reais e a autorização. Isso reduz texto repetido sem interpretar pedidos por uma lista fixa de frases.

As informações relevantes da memória podem ser recuperadas localmente antes da primeira geração. A base de conhecimento publicada pela staff fica separada dos fatos pessoais. Somente trechos relacionados à mensagem entram no contexto, dentro dos limites de tamanho e de acesso. Esses trechos são dados da conversa; não concedem permissões nem substituem as instruções e validações do host. A staff usa `/chatbot conhecimento` para salvar, listar ou remover entradas. O padrão restringe a informação ao canal atual; `publica:true` publica explicitamente para todo o servidor. Não publique informações privadas nesse modo.

As contagens mostram o turno completo, incluindo rodadas de ferramentas, tentativas de reparo e fallback que reportaram uso. Entrada, saída, cache e raciocínio são mostrados separadamente; cache e raciocínio são parcelas dos respectivos totais. Quando alguma tentativa não informa seu consumo, o resultado fica marcado como parcial. Sem contagem fornecida pelo provedor, o código não inventa zero nem estima tokens por caracteres.

Respostas já entregues não são repetidas para obter um fechamento do modelo. `preparar_resposta` permite compor a resposta junto de ajustes independentes; a entrega acontece depois dos resultados confirmados. Consultas cujo resultado ainda não foi visto exigem uma geração posterior, e falhas invalidam a resposta preparada. O GPT-OSS usa raciocínio baixo; o Qwen recebe `/no_think`, uma orientação documentada pelos autores, sem garantia de desligamento rígido na API Cloudflare. A participação espontânea reduz a frequência quando os circuitos locais apontam pressão de quota; menções e respostas diretas mantêm a prioridade. A atualização mantém a mesma síntese de áudio para o arquivo do chat e a reprodução na call, e preserva as verificações de efeitos, acesso e sessão.

Um lorebook no Mongo reduz o material enviado ao selecionar trechos. Ele não cria histórico infinito grátis. Cache também não aumenta limites de requisições ou elimina quotas diárias. O ganho real deve ser avaliado pelas contagens reportadas e pela qualidade da conversa.

## Preparar uma conta Cloudflare sem cobrança

No painel da Cloudflare, use **Workers Free** e não ative **Workers Paid** nem serviços de gateway com saldo ou cobrança. O Workers AI oferece 10.000 neurons por dia no plano Free; ao acabar a franquia desse plano, as requisições são bloqueadas. Neurons são uma unidade de consumo própria e não equivalem a uma quantidade fixa de tokens ou de conversas.

**O código não consegue confirmar o plano de cobrança da conta.** A mesma API pode cobrar uso excedente em uma conta Workers Paid. Para manter o requisito de não gastar dinheiro, a conta usada nesta reserva precisa permanecer no plano Free. Não é necessário migrar os provedores existentes nem colocar um cartão para esta configuração.

1. Abra [Workers AI na Cloudflare](https://dash.cloudflare.com/) e copie o **Account ID** da conta. É um identificador de 32 caracteres hexadecimais.
2. Crie um token pelo modelo de token **Workers AI**, restrito àquela conta. Em um token personalizado, conceda **Workers AI — Read** e **Workers AI — Edit** conforme o guia REST oficial.
3. Configure somente o ID e esse token na VPS. Não envie o token no Discord, no prompt ou em capturas de tela.

Na VPS, crie um arquivo separado, com acesso apenas ao root, e abra o editor:

```bash
sudo install -d -m 700 /etc/tts-bot
sudo touch /etc/tts-bot/cloudflare.env
sudo chmod 600 /etc/tts-bot/cloudflare.env
sudoedit /etc/tts-bot/cloudflare.env
```

No editor, salve estas três linhas, substituindo os textos entre `< >` pelos valores da sua conta:

```dotenv
CLOUDFLARE_ACCOUNT_ID=<seu Account ID de 32 caracteres>
CLOUDFLARE_API_TOKEN=<seu token Workers AI>
CHATBOT_CLOUDFLARE_ENABLED=true
```

Depois associe esse arquivo ao serviço existente e reinicie:

```bash
sudo install -d /etc/systemd/system/tts-bot.service.d
sudo tee /etc/systemd/system/tts-bot.service.d/30-cloudflare.conf >/dev/null <<'EOF'
[Service]
EnvironmentFile=/etc/tts-bot/cloudflare.env
EOF
sudo systemctl daemon-reload
sudo systemctl restart tts-bot.service
```

O systemd lê o arquivo antes de iniciar o processo como `ubuntu`; não é necessário tornar o token legível por outros usuários. Esse arquivo adicional preserva o arquivo de ambiente atual do bot. Sem ativação explícita e os dois valores válidos, a reserva permanece desativada e os provedores já configurados continuam disponíveis. O alias `CLOUDFLARE_API_KEY` é aceito por compatibilidade: prefira `CLOUDFLARE_API_TOKEN`. O orçamento local de 10.000 neurons é uma estimativa por processo, não uma leitura do saldo remoto nem uma garantia de cobrança: mantenha a conta no plano Free.

Use `/chatbot configurar` para conferir o estado: o painel informa se falta o ID ou o token, mostra a espera local de cada provedor e o consumo reportado do último turno. “Configurado” confirma a configuração local, não valida remotamente a conta ou a franquia. As credenciais não são editadas nem exibidas no Discord.

## Acompanhar a economia

```bash
sudo journalctl -u tts-bot.service --since "30 minutes ago" --no-pager -o cat \
  | grep -E 'chatbot: (configuration |provider=|skip |exhausted |result=success|usage |turn_usage |model_discovery|tool_repair|turno falhou)' \
  | tail -n 150
```

Os testes locais simulam os provedores, o banco e o Discord; não gastam a franquia. Naturalidade em português, interpretação de ferramentas e qualidade de RP precisam ser avaliadas no uso real. Nenhuma quantidade de respostas diárias é prometida: ela varia conforme o contexto, o raciocínio, o tamanho da saída e o número de chamadas por turno.

Fontes: [franquia Workers AI](https://developers.cloudflare.com/workers-ai/platform/pricing/), [modelo Qwen](https://developers.cloudflare.com/workers-ai/models/qwen3-30b-a3b-fp8/), [token e REST API](https://developers.cloudflare.com/workers-ai/get-started/rest-api/), [cache Groq](https://console.groq.com/docs/prompt-caching).

## Quarta reserva: Mistral Small 4

A quarta reserva de texto é opt-in e usa a API oficial da Mistral em `https://api.mistral.ai/v1/chat/completions`, com `mistral-small-latest`. Ela entra depois dos dois provedores principais (Groq/Gemini) e antes da reserva diária de neurons da Cloudflare. Assim, a preferência já salva entre Groq e Gemini não muda e o orçamento da Cloudflare fica mais protegido.

A reserva só é criada quando existem as duas configurações abaixo:

```dotenv
MISTRAL_API_KEY=<sua chave da Mistral>
CHATBOT_MISTRAL_ENABLED=true
```

O cliente envia `reasoning_effort=none` para o Mistral Small 4 e um `prompt_cache_key` derivado por hash apenas do prefixo de sistema estável. Ferramentas continuam usando function calling nativo e o circuito de falhas/fallback é o mesmo dos demais provedores. A chave nunca é mostrada no painel nem entra no estado passado ao modelo.

## Economia adicional desta rodada

- Conversas curtas deixam de reservar sempre o teto máximo de saída; o limite cresce conforme o tamanho do pedido e mantém tetos maiores para visão, ferramentas e propostas de ação.
- Conversa textual simples prefere GPT-OSS 20B antes do 120B e Gemini Flash Lite antes do Flash. Turnos com ferramentas, visão ou histórico nativo preservam a ordem forte.
- O histórico pessoal mantém sempre o último turno e escolhe turnos antigos por relevância local, com um orçamento normal menor e sem nova chamada de IA.
- O catálogo inicial de ferramentas caiu para no máximo 5 contratos e cerca de 4.800 caracteres; `carregar_ferramentas` continua disponível para buscar contratos adicionais.
- Recuperação automática de memória/conhecimento e resultados extensos de ferramentas usam limites menores; recibos de efeitos confirmados continuam preservados para impedir replay.
- A telemetria do turno agora separa uso bem-sucedido de tokens gastos em tentativas descartadas, mede cache/raciocínio, registra neurons reportados pela Cloudflare e mede apenas tamanhos de contexto, sem persistir o conteúdo do prompt.
