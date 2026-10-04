# Ferramentas do chatbot

Esta atualização substitui a interpretação de pedidos de áudio e imagem por chamadas nativas de ferramentas. O modelo recebe, no início do prompt geral, um índice de suas capacidades e as instruções estáveis de identidade e linguagem. Depois vem o estado real da conversa. Os schemas detalhados das ferramentas são carregados conforme a necessidade, preservando o catálogo e as validações do host.

O catálogo é explícito: métodos internos dos cogs, terminal, credenciais, banco de dados, updater e administração global não são exportados automaticamente. Módulos desligados e recursos indisponíveis aparecem com o motivo correspondente. O bot sabe que sua conexão de voz não fornece escuta ao vivo.

## Conversa e áudio

- A IA pode consultar e salvar a preferência `auto`, `audio` ou `text` do próprio membro neste canal. Também pode consultar configurações de voz e idioma. As preferências ficam separadas do histórico; reset pessoal, do servidor ou global invalida e remove esses dados.
- Pedidos como “daqui para frente converse por áudio” são interpretados pelo modelo, que chama a ferramenta de preferência. O código não procura uma lista fixa de frases. O host aplica a preferência salva nas respostas seguintes, inclusive depois de reiniciar o bot.
- A ferramenta de formato pode escolher áudio somente neste turno, inclusive quando a preferência salva é texto. O host associa essa escolha ao pedido atual; ela preserva a preferência permanente, a voz e o idioma, e não pode ser forjada em argumentos de uma proposta de ação.
- A conversão para áudio usa a resposta confirmada citada ou a última resposta válida daquele membro no canal. O texto é preservado; áudio já enviado pode reutilizar o mesmo arquivo. Não se guarda áudio binário no Mongo.
- Áudio continua automático, sem cartão de aprovação e sem prévia do texto falado. `send_audio` e `speak_voice` usam uma única síntese: o arquivo é enviado no chat e os mesmos bytes são enfileirados na call quando o acesso e a sessão permitem. Uma fala após entrada ou mudança espera o sucesso da navegação. O envio confirmado permanece válido se a reprodução falhar; enfileirar não significa que o áudio já foi reproduzido. A atualização preserva os codecs, a velocidade e o tom da saída já configurada, e não altera os comandos TTS por prefixo.
- A transcrição funciona em anexos de áudio com o backend Whisper configurado. Imagens anexadas continuam passando pelo modelo de visão; a ferramenta de gerar imagens usa o serviço de geração existente.

O tom permite linguagem informal e palavrões conforme a conversa, sem frases de atendente e sem obrigar o bot a xingar. Configurações personalizadas do prompt global continuam respeitadas. Bloqueios aplicados pelos provedores não são removidos pelo código do projeto.

## Continuidade e indicador de digitação

Quando faltam argumentos de uma ação, a IA pode guardar um rascunho pelas ferramentas e retomá-lo nas próximas mensagens. O rascunho conserva o ID real do alvo, em vez de reutilizar um código temporário de outro turno. Ele pertence ao mesmo servidor, canal e membro, expira em dez minutos e é removido pelos resets de memória. Guardar o rascunho não concede aprovação nem executa a ação. O sistema consome a versão do rascunho uma única vez antes de criar o plano, para que ele não reapareça depois da publicação, da execução ou de um resultado incerto.

As falhas de preparo preservam o motivo público retornado pela ferramenta, em vez de substituir todos os casos por um aviso genérico. A duração de um timeout continua sendo um número inteiro de segundos, de 1 segundo a 28 dias, com motivo explícito.

O processamento usa o indicador nativo de digitação do Discord, renovado durante contexto, ferramentas, transcrição e síntese. Ele substitui as reações de carregamento. A renovação termina ao entregar, falhar ou cancelar; não permanece ativa enquanto uma ação espera a staff. O indicador pode levar alguns segundos para desaparecer no cliente do Discord.

## Identificação e resultados

Menções reais do Discord são associadas aos membros do servidor. As ferramentas resolvem nomes ambíguos, canais, cargos e mensagens acessíveis antes de propor efeitos; referências internas nunca devem ser pedidas ao usuário.

Cada consulta retorna um resultado estruturado ao modelo. Áudio enviado, fala enfileirada, operação aguardando aprovação, falha e resultado incerto têm significados distintos. Uma troca de provedor não autoriza repetir um efeito. Resultados incertos precisam ser verificados antes de uma nova tentativa.

Uma conversão já entregue pode encerrar a resposta sem gastar outra rodada de IA. Se somente o fechamento posterior falhar, o sistema preserva as entregas confirmadas; uma falha ao registrar metadados não provoca o reenvio de um arquivo. Depois das consultas, há uma rodada de fechamento dentro do mesmo prazo e sem novas execuções de ferramentas.

Falhas definidas antes de enviar o arquivo podem usar a resposta em texto quando a conversa ainda estiver acessível. Esse fallback não é uma prévia da fala. Envios incertos e falhas na call depois do anexo confirmado não geram outro arquivo. Se a etapa exigia fala na call e sua admissão falhou ou ficou incerta, as etapas dependentes param, preservando o anexo já enviado.

## Fallback e diagnóstico

Groq continua em primeiro por padrão, Gemini é a alternativa e Qwen3-30B-A3B pela Cloudflare é uma reserva adicional de texto quando configurada. A escolha salva entre Groq e Gemini é preservada; Cloudflare fica por último e não recebe imagens. As tentativas reservam parte do prazo para o próximo provedor. Um limite por modelo não bloqueia automaticamente os outros modelos; indisponibilidade comprovada da conta vale para todos. Os tempos de espera informados pela API são preservados, inclusive os dados estruturados do Gemini. Consulte [Tokens e Qwen](CHATBOT-TOKENS-QWEN.md) para configurar a reserva no plano gratuito e acompanhar o consumo.

Quando o Gemini informa que um modelo não está disponível, o roteador consulta o catálogo da API usando a chave já configurada e considera alternativas de conversa expostas naquele catálogo. A descoberta tem cache, limite de tamanho e prazo; não exige editar o ambiente. A presença no catálogo não garante quota disponível nem suporte a ferramentas: a requisição real continua verificando esses recursos. Um limite da conta não é contornado escolhendo outro nome de modelo.

Chamadas nativas rejeitadas registram a categoria da falha, o caminho do campo no schema e o nome da ferramenta quando ele pertence ao catálogo. Valores de argumentos, fala privada, nomes de ferramentas desconhecidas e respostas brutas continuam fora dos logs. O roteador pode pedir uma correção limitada ao modelo, dentro do mesmo prazo e antes de executar qualquer parte do lote. Não converte automaticamente tipos nem inventa alvos ou parâmetros ausentes. Entrar ou mudar para a call do próprio autor aceita o alvo omitido, usando o membro confirmado pelo Discord.

O índice de ferramentas no início do prompt é curto; descrições e schemas detalhados seguem nas declarações nativas, sem repetir instruções extensas a cada rodada. Os valores de `action`, como `join_voice`, são identificados como ações da ferramenta `propor_acao`. O contexto informa o estado local dos provedores e o atualiza entre rodadas; consultar esse estado não dispara uma nova chamada à API.

O diagnóstico registra provedor, modelo, duração, resultado e motivo de uma tentativa ou de sua ausência. Não registra chaves, prompts, respostas privadas ou o corpo bruto de erros. O painel de configuração mostra se a chave está configurada e se há modelos aguardando nova tentativa. “Configurado” indica configuração local, sem prometer disponibilidade da API.

As contagens de tokens reportadas pelos provedores aparecem nos logs e no diagnóstico, incluindo as parcelas de cache e raciocínio quando informadas. O painel pode mostrar o agregado do turno completo, com tentativas e rodadas de ferramentas. Uma contagem ausente permanece desconhecida, e totais incompletos são identificados como parciais. A exaustão preserva as causas das tentativas, em vez de mostrar apenas o último erro; um modelo ausente não encurta a espera de uma quota longa. O prazo de nova tentativa é uma previsão local, sem garantir recuperação do serviço. A atualização não aumenta as quotas contratadas.

Para acompanhar os metadados na VPS:

```bash
sudo journalctl -u tts-bot.service --since "30 minutes ago" --no-pager -o cat \
  | grep -E 'chatbot: (configuration |provider=|skip |exhausted |result=success|usage |turn_usage |model_discovery|tool_repair|turno falhou)' \
  | tail -n 150
```

## Aprovações

Entrar, mudar ou sair da própria sessão de call é automático, sem aprovação da staff e sem motivo obrigatório. A IA escolhe pelas ferramentas conforme a conversa, inclusive para membros comuns. O estado de voz atual do autor e do bot, o nome e a identificação do canal e a indicação de estarem juntos são apresentados a cada rodada. As ações verificam novamente o destino, o acesso e a sessão antes do efeito, preservando as permissões Conectar/Falar do Discord e a posse dos outros recursos de voz.

Uma fala dependente só começa depois do sucesso confirmado da entrada ou mudança. Pedidos antigos de navegação ainda aguardando aprovação são cancelados e seus cartões removidos; a atualização não os transforma retroativamente em execuções automáticas.

Banimento, expulsão, timeout, remoção de timeout, desbanimento, mudanças de apelido, cargos, canais e exclusão de mensagens requerem uma aprovação específica por etapa. O membro comum pode solicitar, mas não recebe as permissões da pessoa que aprova. O bot e a staff precisam das permissões do Discord e da hierarquia correspondentes no momento do efeito.

As mensagens de aprovação usam a primeira pessoa, texto curto e somente dois botões de decisão, sem botão Detalhes. O cartão apresenta o alvo e os parâmetros efetivos relevantes, sem repetir a autoria ou os avisos de andamento. Aprovação, rejeição ou expiração retiram o cartão, sem a confirmação redundante “Aprovado. Vou executar a ação”. Falha, rejeição ou resultado incerto interrompem as etapas seguintes.

## Painel

Use `/chatbot configurar` para ajustar as opções no Discord:

- **Provedor de conversa:** Groq é a prioridade padrão desta atualização; Gemini permanece como fallback. A escolha explícita no painel pode alterar a ordem entre esses dois. Qwen/Cloudflare é a última reserva de texto quando o operador configura a conta e o token.
- **Disponibilidade:** o texto do painel mostra a configuração e a espera local de Groq, Gemini e Cloudflare, com previsão da próxima tentativa quando todos os modelos estão em espera. O consumo medido não inclui tentativas cujos provedores omitiram o uso.
- **Autorizar cargos e canais:** autorize os recursos em que o chatbot poderá propor alterações. A lista começa vazia; permissões e aprovações continuam obrigatórias. Cargos gerenciados, de staff ou capazes de conceder poderes administrativos ficam protegidos.
- **Configurar ações / Configurar áudios:** controles existentes de disponibilidade, staff e frequência de áudio.

A limpeza fixa no máximo 25 mensagens recentes antes da aprovação; nunca recalcula um intervalo de mensagens depois do clique. Alterações de canal limitam-se a nome, tópico e slowmode previamente apresentados. O timeout usa uma duração explícita de até 28 dias.

## Outros módulos

As ferramentas de música consultam a faixa e a fila e permitem pausar, retomar ou solicitar pular na mesma call, mantendo o serviço e a votação do módulo musical. Não criam outra sessão.

A consulta de tickets mostra somente o atendimento do solicitante e um painel que ele pode acessar. Abertura e fechamento continuam pelos formulários e botões do módulo de tickets; uma consulta não é apresentada como criação ou encerramento de atendimento.

## Validação e atualização

O ZIP incremental contém somente arquivos novos ou alterados nas pastas originais, sem uma pasta externa envolvendo o projeto. Sua aplicação é verificada com o extrator e o aplicador reais do updater, em uma cópia descartável. Os testes locais simulam provedores e operações do Discord; qualidade das respostas e disponibilidade dos serviços externos dependem dos modelos e do ambiente em produção.
