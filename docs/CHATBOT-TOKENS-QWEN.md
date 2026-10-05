# Tokens e reservas de IA econômicas

O projeto mantém Groq/Gemini como provedores principais e duas reservas independentes de texto: Mistral com Ministral 3B/8B/14B adaptativo e Qwen3-30B-A3B pela Cloudflare Workers AI. A preferência salva continua escolhendo apenas qual dos dois provedores principais vem primeiro; quando habilitadas, as reservas entram como Mistral e depois Cloudflare. O Qwen permanece fixo em `@cf/qwen/qwen3-30b-a3b-fp8`; a integração não escolhe automaticamente outro modelo da Cloudflare. Mistral e Cloudflare são opt-in e não são usados em participação espontânea. Imagens seguem a cadeia de visão existente.

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
  | grep -E 'chatbot: (configuration |provider=|skip |exhausted |result=success|usage |turn_usage |delivery_usage |model_discovery|tool_repair|turno falhou)' \
  | tail -n 150
```

Os testes locais simulam os provedores, o banco e o Discord; não gastam a franquia. Naturalidade em português, interpretação de ferramentas e qualidade de RP precisam ser avaliadas no uso real. Nenhuma quantidade de respostas diárias é prometida: ela varia conforme o contexto, o raciocínio, o tamanho da saída e o número de chamadas por turno.

Fontes: [franquia Workers AI](https://developers.cloudflare.com/workers-ai/platform/pricing/), [modelo Qwen](https://developers.cloudflare.com/workers-ai/models/qwen3-30b-a3b-fp8/), [token e REST API](https://developers.cloudflare.com/workers-ai/get-started/rest-api/), [cache Groq](https://console.groq.com/docs/prompt-caching).

## Quarta reserva: Mistral com Ministral adaptativo

A quarta reserva de texto é opt-in e usa a API oficial da Mistral em `https://api.mistral.ai/v1/chat/completions`. A cadeia padrão é `ministral-3b-latest`, `ministral-8b-latest` e `ministral-14b-latest`: 3B lidera turnos simples e fechamentos, 8B lidera conversa normal e 14B lidera turnos com ferramentas. Ela entra depois dos dois provedores principais (Groq/Gemini) e antes da reserva diária de neurons da Cloudflare. Assim, a preferência já salva entre Groq e Gemini não muda e o orçamento da Cloudflare fica mais protegido.

A reserva só é criada quando existem as duas configurações abaixo:

```dotenv
MISTRAL_API_KEY=<sua chave da Mistral>
CHATBOT_MISTRAL_ENABLED=true
```

Os Ministral 3B/8B/14B não recebem `reasoning_effort`, pois esses modelos reportam `reasoning=false` e rejeitam esse parâmetro. Se um `mistral-small-*` ou `mistral-medium-*` for explicitamente configurado, o cliente usa `reasoning_effort=none`. Todos mantêm um `prompt_cache_key` derivado por hash apenas das instruções estáveis, sem misturar estado dinâmico da conversa. Ferramentas continuam usando function calling nativo e o circuito de falhas/fallback é o mesmo dos demais provedores. A cadeia pode ser sobrescrita por `CHATBOT_MISTRAL_MODELS`; a chave nunca é mostrada no painel nem entra no estado passado ao modelo.

## Economia adicional desta rodada

- Conversas curtas deixam de reservar sempre o teto máximo de saída; o limite cresce conforme o tamanho do pedido e mantém tetos maiores para visão, ferramentas e propostas de ação.
- Conversa textual simples prefere GPT-OSS 20B antes do 120B e Gemini Flash Lite antes do Flash. Turnos com ferramentas, visão ou histórico nativo preservam a ordem forte.
- O histórico pessoal mantém sempre o último turno e escolhe turnos antigos por relevância local, com um orçamento normal menor e sem nova chamada de IA.
- O catálogo inicial de ferramentas caiu para no máximo 5 contratos e cerca de 4.800 caracteres; `carregar_ferramentas` continua disponível para buscar contratos adicionais.
- Recuperação automática de memória/conhecimento e resultados extensos de ferramentas usam limites menores; recibos de efeitos confirmados continuam preservados para impedir replay.
- A telemetria do turno agora separa uso bem-sucedido de tokens gastos em tentativas descartadas, mede cache/raciocínio, registra neurons reportados pela Cloudflare e mede apenas tamanhos de contexto, sem persistir o conteúdo do prompt.


## Segunda rodada de eficiência

A parte estável do prompt de sistema agora fica antes do índice de capacidades e de qualquer estado dinâmico. O estado de disponibilidade dos provedores não é repetido para o modelo: fallback, circuit breaker, quota e reservas são decisões do host e continuam disponíveis no painel e nos logs. Isso aumenta a chance de reutilizar prefixos idênticos sem retirar do modelo informação necessária para responder ao usuário.

As reservas Mistral e Cloudflare são **protegidas para turnos solicitados**. Participação espontânea usa apenas Groq/Gemini; se não houver provedor primário utilizável, a fala espontânea nem é sorteada. Menções, replies e comandos continuam podendo cair para Mistral e depois Cloudflare.

Dentro de uma mesma rodada agentic, leituras idênticas são deduplicadas pelo host. Se o modelo repetir exatamente a mesma ferramenta de leitura com os mesmos argumentos, o resultado anterior é reutilizado e a consulta externa não é executada de novo. Se uma rodada inteira consistir apenas nessas repetições, o host encerra o loop de ferramentas e pede o fechamento sem ferramentas. Qualquer efeito confirmado ou de resultado incerto invalida esse cache de leitura para não reutilizar dados potencialmente obsoletos.

Os tetos normais de saída são adaptativos: mensagens mínimas reservam menos tokens, pedidos maiores crescem por faixas e ações ainda têm espaço superior. O teto é um limite, não uma meta de comprimento. Ferramentas recebem orçamento próprio para não truncar chamadas estruturadas.

A contabilidade da Cloudflare separa `measured_neurons` reportados pela API de `uncertain_reserved_neurons` de tentativas sem medição conclusiva. Uma requisição bem-sucedida com consumo reportado substitui a reserva estimada pelo valor real; falhas sem telemetria permanecem conservadoramente contabilizadas até a janela local expirar. O painel também expõe tokens descartados, uso por provedor e reutilização de leituras, para que otimizações futuras sejam baseadas em desperdício observado.

Fontes adicionais: [Mistral Chat Completions](https://docs.mistral.ai/api/endpoint/chat), [Mistral reasoning](https://docs.mistral.ai/capabilities/reasoning/), [Mistral Small](https://docs.mistral.ai/getting-started/models/models_overview/).

## Terceira rodada de eficiência

O fechamento de um turno com ferramentas deixa de reenviar os schemas e o índice de capacidades quando novas chamadas já estão proibidas. O histórico nativo continua contendo cada `tool_call` e seu resultado confirmado, mas declarações que o modelo não pode mais usar são removidas da requisição final. O estado dinâmico do fechamento também omite a lista de ferramentas. Isso reduz principalmente tokens de entrada em turnos com consulta ou efeito sem alterar a autorização, que continua sendo responsabilidade do host.

Fechamentos sem novas ferramentas usam o perfil econômico do router: GPT-OSS 20B vem antes do 120B e Gemini Flash Lite vem antes do Flash. A margem de saída para sintetizar resultados de ferramentas é preservada mesmo sem os schemas; a economia vem do modelo e do contexto, não de truncar à força a resposta final. Se o modelo menor falhar, o fallback normal continua disponível.

Quando uma geração já produz texto junto de **apenas efeitos automáticos**, esse texto fica privado até todos os efeitos do lote confirmarem sucesso. Depois da confirmação, ele pode ser reutilizado como resposta final, eliminando a geração que antes servia apenas para reformular “concluído”. Isso nunca é aplicado a consultas de leitura, propostas privilegiadas, lotes parciais/incertos, falhas ou entregas que já ocorreram. `preparar_resposta` continua tendo prioridade quando o modelo o usa explicitamente.

A telemetria agora separa as etapas `direct`, `initial`, `tool_followup` e `closing`, incluindo uso reportado e tamanho do contexto por etapa. Depois da entrega real no Discord, o turno recebe também custo por resposta entregue: rodadas de modelo, tentativas, tokens e neurons quando o provedor os reporta. Turnos suprimidos ou sem entrega não recebem números “por resposta”. O painel `/chatbot configurar` mostra um resumo dessa eficiência sem expor prompt, resultados privados ou credenciais.

## Quarta rodada de eficiência

Os contratos nativos carregados deixaram de ser descritos novamente no índice textual de capacidades. Em cada rodada o índice enumera apenas funções ainda não cobertas pelos schemas enviados naquela requisição; o schema de `carregar_ferramentas` continua permitindo descobrir contratos adicionais. Depois do primeiro lote, candidatos escolhidos apenas pelo ranking local e nunca usados são descartados das rodadas seguintes. Ferramentas efetivamente chamadas e contratos carregados explicitamente pelo modelo permanecem para preservar o histórico nativo.

O estado dinâmico também não repete mais a lista de ferramentas carregadas nem os enums de ações já presentes nos schemas. Ele só acrescenta uma indisponibilidade quando o estado real de uma função carregada mudou. A autorização continua no host e o índice não avalia permissões nem availability.

Resultados extensos de ferramentas passam por um limite de fallback menor (`MAX_TOOL_RESULT_CHARS=4500`, `MAX_TOOL_RESULT_BYTES=9000`). Ferramentas devem paginar antes desse ponto; quando o fallback precisa truncar, ele mantém status e comprovantes necessários para impedir replay, sem copiar payload privado como prévia.

Recuperação local de lembretes pessoais e conhecimento publicado agora roda em paralelo, com um único prazo curto. Essas leituras não chamam modelo; o objetivo é preservar mais do deadline para a geração útil e reduzir turnos que precisariam cair em timeout/fallback.

A auditoria registra o tamanho do catálogo completo, schemas realmente carregados na primeira rodada, tamanho depois da poda e quantos candidatos especulativos foram removidos. `/chatbot configurar` mostra essa redução sem armazenar nomes, argumentos ou conteúdo das ferramentas.

## Quinta rodada de eficiência

O índice textual de capacidades deixa de repetir o catálogo completo em toda rodada. O schema de `carregar_ferramentas` já contém no enum `names` os nomes reais disponíveis; por isso o system prompt passa a descrever somente candidatos semanticamente relacionados à mensagem que não couberam no orçamento inicial de schemas. Em conversa comum, quando nenhum candidato ficou de fora, o índice vira apenas uma instrução curta de descoberta. O modo detalhado do `ToolRegistry` continua disponível para diagnóstico e compatibilidade.

Texto que acompanha uma resposta com `tool_calls` continua privado até o resultado real das ferramentas, mas agora não é reenviado como parte do histórico nativo das rodadas seguintes. IDs, nomes e argumentos das chamadas permanecem integrais, então OpenAI-compatible e Gemini continuam recebendo o pareamento exigido pela API. O objeto local da resposta mantém a prévia apenas para o caso já validado em que somente efeitos automáticos confirmados permitem reutilizá-la como fechamento. A auditoria mede somente quantos caracteres deixaram de ser reenviados; não converte isso em uma estimativa inventada de tokens.

O fechamento sem novas ferramentas recebe um estado operacional mínimo. Referências de membros/recursos, presença de voz, rascunhos e availability servem para decidir e executar chamadas, mas não podem alterar nada depois que o host proíbe novas tools. A rodada final conserva somente preferências necessárias de formato/idioma. Isso reduz especialmente o segundo prompt de pedidos com muitos alvos resolvidos.

A preferência da conversa deixa de ser relida do Mongo a cada rodada do mesmo turno. Ela já foi capturada antes do loop e o canal é serializado; quando `set_conversation_preferences` confirma uma mudança, o estado local é atualizado imediatamente. A economia principal aqui é de deadline/I/O, diminuindo a chance de um fechamento útil cair em timeout e gerar fallback desperdiçado.

O último turno pessoal também passa a respeitar o orçamento normal quando a mensagem atual é longa e lexicalmente independente do assunto anterior. Seguimentos curtos ou com termos em comum continuam preservando até o teto maior para manter continuidade. Uma troca clara de assunto ainda conserva até o orçamento normal de 2.800 caracteres, em vez de anexar automaticamente até 6.000 caracteres só porque a resposta anterior era grande.

## Sexta rodada de eficiência

O prefixo fixo do system prompt foi condensado sem retirar os contratos de confiança, privacidade, idioma, honestidade e confirmação de efeitos. Em turnos sem imagem, a mensagem redundante dizendo que nenhuma imagem foi recebida deixa de ser enviada: a ausência de blocos multimodais já representa esse estado. Quando há imagem, a diretiva visual também usa uma forma menor. Os delimitadores de contexto citado e de dados recuperados foram encurtados, mantendo explicitamente que esses blocos são dados e não instruções.

Lotes de ferramentas já concluídos deixam de reaparecer no wire format nativo em todas as gerações seguintes. Depois que o host executa e valida cada `tool_call`, ele conserva localmente nome, argumentos e resultado compacto, mas substitui no próximo prompt os pares `assistant(tool_calls)` + `tool(tool_call_id)` por um único envelope cumulativo `[FERRAMENTAS; ...]`. IDs de protocolo, tipos e wrappers específicos de OpenAI/Gemini não têm mais utilidade depois que aquele lote terminou. Isso reduz principalmente o crescimento linear de entrada quando um turno usa duas ou mais rodadas de consultas. A autorização e a execução continuam no host; o envelope compactado é explicitamente tratado como dado não confiável.

Fluxos puramente operacionais ganharam outro atalho de fechamento. Se um lote contém somente efeitos automáticos, não privilegiados, sem consultas anteriores, e todos os efeitos possuem recibos tipados confirmados pelo host, o bot pode responder diretamente com esses recibos quando a geração não trouxe uma prévia textual reutilizável. Assim, operações como controles simples deixam de gastar uma segunda geração apenas para parafrasear “feito”. Entregas já publicadas, propostas de staff, consultas, estados pendentes, falhas e resultados incertos não entram nesse atalho.

O teto para uma geração que ainda pode emitir ferramentas nativas caiu de 768 para 512 tokens. Propostas de ação continuam com o teto próprio maior e visão mantém seu limite separado. Participação espontânea recebe ainda um teto explícito de 160 tokens em texto/fechamento; esse cap nunca reduz a margem estrutural de uma chamada de ferramenta ou proposta de ação. Os envelopes internos de dados também deixam de influenciar a escolha do teto: o cálculo usa a última fala real do usuário, não o bloco de resultados que o host acrescentou depois.

A auditoria local ganhou `tool_protocol_compaction`, em caracteres, para mostrar quanto do protocolo concluído deixou de ser reenviado. Como nas rodadas anteriores, esse número não é convertido artificialmente em tokens: a economia faturada/contabilizada continua sendo avaliada pelos relatórios reais dos provedores.

## Sétima rodada de eficiência

Os resultados concluídos de ferramentas agora são preservados em **envelopes imutáveis por lote**, em vez de reescrever um único envelope cumulativo depois de cada rodada. Cada registro é serializado na forma curta `[tool,args,result]`. Quando uma nova consulta é executada, somente o protocolo nativo recém-criado (`assistant(tool_calls)` + respostas `tool`) é trocado pelo novo envelope; os envelopes anteriores permanecem byte a byte idênticos. Isso reduz wrappers/chaves repetidas e, principalmente, evita invalidar o prefixo já enviado aos provedores. Cache continua sendo uma otimização oportunista: somente `cached_tokens` reportados pelo próprio provedor contam como acerto real.

Uma leitura idêntica reutilizada no mesmo estado também deixou de reenviar `tool_call_id`, texto explicativo e o resultado completo. O resultado original continua presente no envelope imutável anterior; a repetição recebe somente um marcador curto `reused_read/same_result`. Efeitos ainda invalidam o cache local de leituras quando podem ter alterado o estado.

`preparar_resposta` continua no catálogo e pode ser carregado por `carregar_ferramentas`, mas deixou de ocupar o conjunto mínimo de schemas em **todo** turno com ferramentas. Os atalhos já existentes — prévia privada confirmada, recibo tipado de efeito e fechamento textual direto — cobrem o caminho comum sem esse contrato. O núcleo inicial conserva `carregar_ferramentas` e `select_response_format`; se um fluxo realmente precisar de `preparar_resposta`, o host continua validando e carregando o contrato normalmente.

O estado operacional também passa a respeitar os contratos efetivamente carregados. Em rodadas onde nenhum schema pode usar presença de voz, referências de membros/recursos ou rascunhos de ação, esses blocos não entram no prompt. Quando `propor_acao` ou ferramentas relacionadas a voz, música, fala, áudio ou rascunhos estão carregadas, os dados necessários continuam presentes. O corte é somente de contexto: autorização, resolução de alvos e guards permanecem no host.

A reserva Cloudflare ganhou conservação progressiva baseada no consumo **localmente contabilizado** do orçamento diário. Para respostas de texto puro, o teto passa a 320 tokens após 50%, 220 após 75% e 160 após 90% do orçamento local. Se uma resposta textual ainda não couber na reserva conservadora restante, o host pode reduzir o teto até o máximo que cabe, nunca abaixo de 96 tokens; se nem isso couber, a chamada é bloqueada antes do HTTP. Chamadas de ferramentas com `tool_choice=auto` nunca têm sua margem estrutural reduzida por essa regra, porque truncar JSON tende a provocar repair/fallback e gastar ainda mais. Fechamentos com `tool_choice=none` são tratados como texto puro. `/no_think` continua no system prompt do Qwen.

O painel `/chatbot configurar` mostra quando a reserva Cloudflare entrou em economia moderada, alta ou crítica e qual teto de texto está ativo. Esse estado é uma proteção por processo, não uma leitura da franquia remota nem uma confirmação do plano de cobrança.


### Fast path de smalltalk e último schema fixo removido

Mensagens sem nenhum termo lexical útil, sem afinidade atual ou recente com ferramentas, sem reply, anexo, ação ou rascunho pendente podem seguir diretamente para o modelo de texto. Esse atalho acontece antes da recuperação automática de memória/conhecimento e antes de enviar qualquer schema; o histórico normal da conversa continua presente. Não há uma lista de frases reconhecidas: qualquer termo lexical real ou afinidade com o catálogo desativa o atalho. A auditoria registra em caracteres os schemas que deixaram de ser enviados, sem estimar tokens que o provedor não reportou.

`select_response_format` também deixou o núcleo fixo. O único contrato necessariamente presente no fluxo normal é `carregar_ferramentas`; pedidos que mencionam texto, áudio, voz ou fala selecionam `select_response_format` pelo mesmo ranking local do catálogo. A preferência persistente de formato continua sendo aplicada pelo host e não precisa desse schema em cada turno. Isso reduz o prefixo pago de todas as conversas que não pedem uma troca de formato.
