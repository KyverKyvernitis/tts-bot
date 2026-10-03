# Chatbot do próprio bot

O chatbot usa o nome e o avatar da conta do bot no Discord e envia respostas
nativas. A configuração por servidor controla ativação, canais permitidos e
respostas espontâneas. Não há escolha de personagem, imitação de usuários,
profiles ou identidade por webhook.

## Uso e configuração

| Ação | Comando ou interação |
| --- | --- |
| Conversar | Mencionar o bot no início da mensagem ou responder a uma mensagem do chatbot |
| Ativar e escolher canais | `/chatbot configurar` |
| Configurar respostas espontâneas | `/chatbot configurar` |
| Configurar áudios, calls e banimentos | `/chatbot configurar` → **Configurar ações** |
| Apagar sua memória | `/reset` |
| Apagar a memória do servidor | `/chatbot memoria` |
| Gerar uma imagem | `/imagem <prompt>` |
| Ver, editar ou transferir o prompt global | `/chatbotadmin master` |
| Apagar a memória de todos os servidores | `/chatbotadmin reset_global` |

`/chatbot configurar` e `/chatbot memoria` exigem Gerenciar servidor ou a
autorização equivalente validada pelo comando. O reset do servidor pede
confirmação. O reset global exige dono do bot ou operador autorizado e
confirmação. O gerenciamento do prompt global mantém a autorização do servidor
de configuração e as verificações de operador existentes.

Servidores novos começam com o chatbot desativado. Sem restrição de canais,
menções e replies podem funcionar nos canais acessíveis ao bot depois da
ativação. Escolher canais permitidos restringe onde o chatbot atende.
Threads e posts de fórum herdam a autorização do canal pai selecionado, mas
mantêm memória própria, isolada pelo ID de cada thread. A restrição de idade
herdada do canal pai e a allowlist existente continuam sendo consideradas no
isolamento da memória.
O comando mostra o estado atual e o botão **Editar configuração**, que abre o
formulário de ativação, canais e modo espontâneo. O botão **Configurar ações**
abre outro formulário: ações da IA, áudios, calls, banimentos e cargos de staff.
As quatro opções de ações começam ativadas, mas o chatbot de um servidor novo
continua desativado até a configuração. Os cargos são opcionais; escolher cargos
de staff não concede a permissão de banir. `/imagem` também respeita a
ativação e os canais permitidos. O chatbot atende nos servidores; mensagens
diretas não iniciam o fluxo de IA.

O modo espontâneo começa desligado. Ele exige uma lista explícita de canais e
uma chance de resposta entre 1% e 20%, com padrão de 5%. Quando há restrição de
canais permitidos, os canais espontâneos precisam pertencer a essa lista.
Cooldowns e limites da fila continuam controlando a frequência. O contexto
espontâneo usa a mensagem atual e a memória do chatbot, sem buscar automaticamente
o histórico do Discord.

O comportamento vem de um prompt global. Imagens, análise de anexos, transcrição,
fallback entre provedores e integração TTS continuam no fluxo do chatbot. Áudio
pode ser produzido espontaneamente ou a pedido, conforme os controles de ações.

O padrão de conversa usa português brasileiro informal, respostas curtas e
reconhecimento direto de correções. A orientação é ser mais livre com as palavras:
gírias, palavrões, sarcasmo e brincadeiras podem aparecer espontaneamente quando
cabem no contexto, sem exigir um pedido de xingamento ou um canal NSFW e sem
forçar insultos em toda resposta. O tom não depende de personagens ou
perfis. Instruções personalizadas do prompt mestre são preservadas: a atualização
substitui somente padrões antigos reconhecidos. A memória V3 existente não é
apagada para mudar o tom. Restrições do modelo escolhido continuam valendo;
a qualidade da conversa precisa ser verificada com as credenciais da instalação.

As instruções de tom não incluem frases prontas para o modelo copiar. A mensagem
atual chega sem o prefixo artificial `[PEDIDO]`; o histórico mantém pares
completos de pergunta e resposta. O contexto coletivo entra em perguntas
explícitas sobre o canal/servidor ou no modo espontâneo. Um atraso no carregamento
do prompt mestre não descarta um histórico que já terminou de carregar.

Não existe uma etapa de tradução para inglês e retradução da resposta. A escolha
do modelo influencia a naturalidade em português: o padrão de texto agora
prioriza `openai/gpt-oss-120b`, com `openai/gpt-oss-20b` como fallback. Não foi
feita comparação real de fluência entre esses modelos e Gemini Flash. Os testes
locais validam o fluxo, não essa qualidade.

As recusas editoriais locais do chat de texto foram removidas: palavras adultas
ou grosseiras, por si só, não iniciam uma recusa automática. Os modelos e
provedores mantêm suas próprias regras, e as proteções da geração de imagens
continuam valendo. Os limites de anexos, filas e permissões são controles técnicos.

Nos logs em nível INFO, busque `chatbot: result=success`: o registro informa
`provider`, `model`, `mode`, `elapsed_ms` e `message_count`. Esse é o modelo que
realmente respondeu, inclusive após fallback; a primeira opção da lista pode
não ser a vencedora. Falhas incluem etapa, tipo, status e motivo de término
quando disponível. Os registros não incluem o texto privado da conversa.

O índice de mensagens registra quais respostas pertencem ao chatbot. Responder
a uma mensagem de música, jogos ou outra função do bot não inicia uma conversa
com a IA. O índice persiste para reconhecer replies após reinícios por até
14 dias a partir do envio, inclusive para imagens produzidas por `/imagem`.
Depois desse prazo, comece uma nova conversa mencionando o bot. Respostas
às mensagens dos personagens antigos não iniciam a conversa nova.

## Áudios, calls e banimentos

O bot pode propor recursos durante a conversa. Nesta versão, há quatro ações
estruturadas: `send_audio`, `speak_voice`, `join_voice` e `ban_member`, descritas
abaixo. Uma declaração na resposta textual, por si só, não executa essas ações. Ativação,
canais permitidos e configuração de ações continuam sendo conferidos antes da
execução. O bot informa o resultado obtido, sem anunciar sucesso antecipadamente.

| Recurso | Quem pode participar | Aprovação |
| --- | --- | --- |
| Enviar um áudio no chat | Qualquer membro na conversa | Pode ser automático; o bot também pode perguntar antes |
| Falar na call atual | Membro e bot na mesma call de voz | Pode ser automático; o bot também pode perguntar antes |
| Entrar em call | Bot desconectado e membro em uma call identificada no pedido | Sempre exige staff autorizada |
| Banir um membro | Alvo identificado neste servidor | Sempre exige aprovação com **Banir membros**, autorização e hierarquias válidas |

Quando o bot pergunta antes de enviar áudio ou falar, o pedido mostra somente a
ação e os botões **Pode mandar/Pode falar** e **Agora não**. A fala não aparece,
nem é resumida ou antecipada no texto. Somente o membro envolvido responde a
esse pedido. Um áudio automático dispensa esse clique.

Para aprovar entrada em call, valem o dono do servidor, **Gerenciar servidor**,
**Administrador** ou um dos cargos de staff configurados. O canal de voz fica
fixado no pedido: se o membro mudar de call, faça um novo pedido. Para banir,
o aprovador precisa de **Banir membros**; os cargos configurados restringem a
autorização, e a hierarquia do aprovador e do bot é conferida novamente antes
da execução. A IA não pode conceder essas permissões.

Os pedidos expiram em cinco minutos e seus botões ficam vinculados ao servidor,
canal e mensagem originais. O estado é persistente; dois cliques não executam a
mesma ação duas vezes. Pedidos pendentes ainda válidos podem reaparecer após
reiniciar. Uma execução com resultado incerto não é repetida automaticamente.

A entrada em call é temporária e não é restaurada após reiniciar o bot. Para
entrar, o bot precisa estar desconectado: esta versão não muda uma sessão ativa
de outra call. Música ou TTS ocupado deixam as ações de call indisponíveis.
Para falar, o bot e o membro precisam continuar na mesma call. O bot não escuta nem
transcreve conversas ao vivo; anexos de áudio enviados no chat continuam tendo
seu fluxo próprio de transcrição.

Áudios anexados ao chat não são espelhados automaticamente na fila da call.
A fala em call usa somente `speak_voice`, com as verificações do adaptador de
voz. Se o provedor não aceitar ferramentas, um pedido de áudio ainda pode
receber um anexo no chat pelo fluxo existente, sem entrar na fila da call.

## Leitura de imagens

O chatbot pode analisar um anexo da mensagem atual ou da mensagem explicitamente
respondida. Para uma imagem enviada por outra pessoa, responda à mensagem com
uma menção ao bot no início: `@bot o que está escrito aqui?`. Isso não habilita
varredura do histórico do canal. As restrições de ativação e acesso continuam
valendo; uma resposta ao chatbot também inicia a conversa normalmente.

A imagem é baixada e preparada uma vez; as tentativas entre provedores recebem
os mesmos dados preparados. O processamento limita o tamanho e preserva a
legibilidade dos prints. Erros de download, formato, autenticação, modelo e
timeout são tratados separadamente. Uma falha ao baixar um anexo não é
interpretada como chave inválida do Gemini. O fallback usa apenas modelos de
visão configurados, dentro do tempo máximo do pedido.
Você pode reenviar o anexo respondendo ao aviso de falha do chatbot; esse aviso
é reconhecido como parte da conversa, mas a tentativa falha não fica na memória.

São aceitos JPEG, PNG, WebP e GIF, com até três imagens por mensagem e 20 MiB
por arquivo. Imagens muito grandes podem ser reduzidas; GIFs e WebPs animados
usam apenas o primeiro frame. PDFs e outros arquivos não são lidos como imagens.

As configurações abaixo são opcionais para operadores. Para aplicar esta
atualização, basta enviar o ZIP correto ao updater e reiniciar o bot. Não é
necessário editar `.env`; configurações explícitas existentes são preservadas.

| Variável opcional | Finalidade |
| --- | --- |
| `CHATBOT_GROQ_VISION_MODELS` | Lista ordenada de modelos Groq para ler imagens |
| `CHATBOT_GEMINI_VISION_MODELS` | Lista independente de modelos Gemini para ler imagens |
| `CHATBOT_GROQ_MODELS` | Modelos Groq para conversa de texto |
| `CHATBOT_GEMINI_MODELS` | Modelos Gemini para conversa de texto |
| `CHATBOT_TEXT_PROVIDER_ORDER` | Prioridade dos provedores de texto; padrão `groq,gemini` |

`CHATBOT_TEXT_PROVIDER_ORDER=gemini,groq` tenta Gemini primeiro em conversas de
texto, sem mudar a rota de imagens. Nomes desconhecidos e repetidos são ignorados;
os demais provedores configurados continuam disponíveis como fallback. O roteiro
de avaliação contém exemplos para comparar 20B, 120B e Flash.

As listas usam IDs de modelo separados por vírgula. O padrão Groq de visão passa
de `qwen/qwen3.6-27b` para `qwen/qwen3.8-27b`, que é preview. O modelo anterior
foi retirado em 14/09/2026 para contas gratuitas e de desenvolvedor, conforme a
[documentação do Groq](https://console.groq.com/docs/deprecations). Valide acesso,
disponibilidade e custo na conta usada; variáveis de ambiente antigas continuam
prevalecendo sobre os padrões. Ler anexos e gerar imagens por `/imagem` são
operações diferentes, com configuração própria.

A [lista oficial de modelos Gemini](https://ai.google.dev/gemini-api/docs/models)
apresenta `gemini-3.8-flash` como estável. Conforme a
[página de descontinuações](https://ai.google.dev/gemini-api/docs/deprecations),
Gemini 2.5 Flash e Flash-Lite não foram descontinuados, mas o acesso está restrito
a contas que já os usaram. Uma chave nova pode não aceitar os padrões 2.5 deste
projeto. Confira acesso e custo na conta antes de configurar outro ID; versão
mais recente ou modelo maior não demonstram melhor português. A rota de visão
e a ordem de provedores continuam iguais nesta revisão de linguagem.

O fluxo não guarda os bytes do anexo na memória de conversa. Ao voltar a uma
imagem antiga, responda à mensagem que contém o anexo para fornecer a referência
visual exata. O roteiro de validação está em [CHATBOT-AVALIACAO.md](CHATBOT-AVALIACAO.md).

## Aplicação da versão

Os dois ZIPs contêm apenas arquivos alterados e novos nos caminhos originais,
relativos à raiz do repositório. Escolha um deles conforme o código instalado:

| Pacote | Base necessária | Exclusões |
| --- | --- | --- |
| `chatbot-acoes-atualizacao.zip` | Primeira simplificação já aplicada, ou qualquer atualização posterior do chatbot | Nenhuma; os módulos antigos já foram retirados |
| `chatbot-acoes-desde-original.zip` | Código do ZIP original, ainda com os módulos antigos | Quatro módulos, via `update-manifest.json` |

O pacote `atualizacao` reúne as mudanças posteriores à primeira simplificação
(`chatbot-simplificado-incremental.zip`), permitindo atualizar também quem já
aplicou os pacotes anteriores de conversa/visão.

Envie somente o pacote correspondente ao updater e reinicie o bot depois da
aplicação. O pacote `desde-original`
contém alterações acumuladas e declara quatro operações `delete`:
`cogs/chatbot/persona.py`, `cogs/chatbot/profiles.py`, `cogs/chatbot/extrovert.py`
e `cogs/chatbot/webhooks.py`. O updater exige que os arquivos declarados para
exclusão existam; esse pacote não deve ser aplicado depois da simplificação
anterior. O manifesto de controle não vira um arquivo do projeto.

Preserve uma cópia do código e do MongoDB anterior para recuperação. Como os ZIPs
são parciais, não substitua a pasta inteira do projeto pelo conteúdo deles. Ao
iniciar a versão atualizada, a preparação abaixo é executada automaticamente;
nenhuma migração foi executada em produção nesta entrega.

## Migração de dados

A inicialização executa a migração `chatbot-v3-single-bot` na coleção
`chatbot_data`. O marcador de tipo `chatbot_migration` registra `running` ou
`complete` e as contagens de documentos arquivados, removidos e configurações
atualizadas. Uma execução interrompida pode ser retomada; uma falha bloqueia a
inicialização do chatbot até a migração terminar.

A coleção `chatbot_legacy_v2` guarda cópias dos documentos antigos, com seus
`_id`: profiles, memórias V1/V2, mapas de mensagens por webhook, configurações
extrovert, registros de webhooks e snapshots das configurações de servidor e do
prompt global. A migração confere a cópia antes de retirar o documento legado
da coleção ativa. Os índices antigos são retirados depois da validação final.

O estado `enabled` dos servidores existentes é preservado. A restrição de
canais começa vazia, e respostas espontâneas começam desligadas. As configurações
antigas de personagens e extrovert ficam apenas no arquivo legado.
Instalações V1 sem documento de configuração recebem uma configuração baseada
na existência de um profile ativo arquivado. Uma configuração explícita de
servidor desativado sempre prevalece.

A memória V3 começa vazia na primeira migração de V1/V2, sem misturar conversas
de personagens antigos. Ela não usa `profile_id` ou revisão de profile;
conserva o isolamento de contexto e
as gerações que impedem respostas atrasadas de reintroduzir contexto apagado.
Uma resposta já em processamento pode terminar e ser enviada após o reset,
mas seu contexto antigo permanece invisível. As gerações de reset existentes
são preservadas na migração. O prompt antigo é arquivado e substituído pelo
prompt padrão do chatbot único, preservando o servidor responsável por sua
configuração.

Essa substituição diz respeito ao prompt legado de V1/V2. Em instalações já
migradas, a atualização de tom preserva prompts mestres personalizados e a
memória V3; apenas versões conhecidas do padrão recebem as novas instruções.

O runtime novo não lê o arquivo legado. Webhooks existentes no Discord não são
apagados pela migração; eles deixam de ser usados pelo chatbot.
Os comandos de reset limpam a memória do chatbot, sem apagar mensagens do
Discord. `/reset` continua disponível mesmo quando o chatbot está desativado.

## Atualização dos comandos no Discord

`/chatbot configurar` substitui `/chatbot profile`, `/chatbot persona` e
`/chatbot extrovert`. O grupo `/chatbot` permanece, junto com `/chatbot memoria`.

As permissões de sincronização existentes continuam valendo:

- `APP_COMMAND_SYNC_ENABLED=1` habilita a sincronização. Na ausência dessa
  variável, vale `SYNC_SLASH_COMMANDS`.
- `APP_COMMAND_SYNC_SCOPE=global` autoriza a publicação global. Sem um escopo
  explícito, vale `SYNC_GLOBAL_SLASH_COMMANDS`.
- `APP_COMMAND_SYNC_SCOPE=guild` sincroniza apenas os servidores configurados.
- `CLEAR_GLOBAL_COMMANDS` controla a limpeza de raízes globais removidas durante
  a sincronização por servidor; não permite apagar um grupo que ainda existe.

Para atualizar os subcomandos publicados globalmente e retirar as opções
antigas, execute uma inicialização com sincronização global habilitada. A
sincronização apenas por servidor preserva o grupo global existente, que pode
continuar mostrando seus subcomandos antigos. Ao habilitar o escopo global
posteriormente, o bot publica o manifesto mesmo que o código não tenha mudado
desde a sincronização por servidor. O Discord pode levar algum tempo para
refletir os comandos globais.

## Recuperação e retorno à versão anterior

Antes de aplicar em produção, mantenha backup do MongoDB e da versão anterior
do código. O arquivo legado preserva os documentos anteriores à migração, mas
não executa rollback automático.

Para voltar, interrompa o bot novo, restaure a versão anterior e os documentos
legados ou o backup correspondente. Restaure também os snapshots das
configurações e do prompt global; trocar somente o código não desfaz a migração.
Depois, sincronize os comandos de acordo com o escopo usado na instalação.
O histórico criado pela V3 não é convertido de volta para memória por profile.

Se precisar reaplicar a migração após uma restauração manual, confira o
marcador `chatbot-v3-single-bot`: um marcador `complete` sinaliza que a execução
já terminou. Não remova o arquivo legado ou o backup antes de validar a versão
que ficará em produção.

## Validação local

Execute a suíte local com as dependências do projeto:

```sh
python -m pytest -q tests/test_chatbot*.py tests/test_tts_helpers.py tests/test_antibot.py
```

Discord, provedores e MongoDB foram simulados nos testes; nenhuma instância
de produção foi alterada. A validação cobre migração retomável, isolamento,
resets concorrentes, comandos e permissões, replies após reinício, threads,
imagens, áudio, configuração de ações e cancelamento de tarefas. Esses testes verificam o fluxo e os
payloads; não comprovam a qualidade de respostas de modelos externos. Use o
roteiro [CHATBOT-AVALIACAO.md](CHATBOT-AVALIACAO.md) para avaliar comportamento,
precisão, leitura de prints, custo e demora com as credenciais da instalação.
