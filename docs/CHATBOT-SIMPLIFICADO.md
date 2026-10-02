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
também é herdada do canal pai e continua sujeita à allowlist existente.
O comando mostra o estado atual e o botão **Editar configuração**, que abre o
formulário de ativação, canais e modo espontâneo. `/imagem` também respeita a
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
é produzido quando solicitado, junto com a resposta em texto.

O índice de mensagens registra quais respostas pertencem ao chatbot. Responder
a uma mensagem de música, jogos ou outra função do bot não inicia uma conversa
com a IA. O índice persiste para reconhecer replies após reinícios por até
14 dias a partir do envio, inclusive para imagens produzidas por `/imagem`.
Depois desse prazo, comece uma nova conversa mencionando o bot. Respostas
às mensagens dos personagens antigos não iniciam a conversa nova.

## Aplicação da versão

Este ZIP incremental contém apenas os arquivos alterados e novos nos caminhos
originais, relativos à raiz do repositório. Envie o pacote ao updater do projeto.
O `update-manifest.json` incluído na raiz declara quatro operações `delete`:
`cogs/chatbot/persona.py`, `cogs/chatbot/profiles.py`, `cogs/chatbot/extrovert.py`
e `cogs/chatbot/webhooks.py`. O updater executa essas exclusões e aplica os
arquivos do pacote; o manifesto de controle não vira um arquivo do projeto.

Preserve uma cópia do código e do MongoDB anterior para recuperação. Como o ZIP
é parcial, não substitua a pasta inteira do projeto pelo conteúdo dele. Ao
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

A memória V3 começa vazia, sem misturar conversas de personagens antigos. Ela
não usa `profile_id` ou revisão de profile; conserva o isolamento de contexto e
as gerações que impedem respostas atrasadas de reintroduzir contexto apagado.
Uma resposta já em processamento pode terminar e ser enviada após o reset,
mas seu contexto antigo permanece invisível. As gerações de reset existentes
são preservadas na migração. O prompt antigo é arquivado e substituído pelo
prompt padrão do chatbot único, preservando o servidor responsável por sua
configuração.

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

198 testes e 52 subtests passaram com Python 3.12 e discord.py 2.7.1:

```sh
python -m pytest -q tests/test_chatbot*.py tests/test_tts_helpers.py tests/test_antibot.py
```

Discord, provedores e MongoDB foram simulados nos testes; nenhuma instância
de produção foi alterada. A validação cobre migração retomável, isolamento,
resets concorrentes, comandos e permissões, replies após reinício, threads,
imagens, áudio e cancelamento de tarefas. A compilação Python e o catálogo de
ajuda também foram verificados.
