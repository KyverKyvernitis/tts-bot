# Music Agent 0.3.83 — início mais rápido do `_play`

Esta versão continua as melhorias de áudio e arquivamento da 0.3.82. As músicas
permanentes permanecem no fórum Discord e os diretórios temporários continuam
disponíveis para download, divisão e envio. A reprodução usa buffers na RAM.

## Mudanças ativadas no fluxo atual

- O áudio dos arquivos v8, inclusive arquivos com uma única parte, começa a ser
  preparado enquanto ocorre a conexão de voz. A fonte preparada é adotada
  somente se faixa, canal, posição, efeitos e geração continuarem compatíveis.
  Preparação inicial, próxima faixa e próxima parte compartilham uma cota de
  preparação extra. Antes da adoção só a parte atual é preparada; o buffer inicial
  de cada estágio PCM guarda no máximo 500ms.
- Em links e playlists, `prepare_voice` antecipa o handshake enquanto os
  metadados são lidos. Uma lease de 45s protege conexões sem resposta HTTP.
  Cancelamento só desfaz a conexão criada por essa preparação. Sessões em uso,
  TTS e chamadas em outro canal são respeitadas. Pesquisas com seleção de
  resultado só entram na call depois da escolha.
- O lote inicial da playlist consulta o catálogo do fórum numa conexão SQLite
  e fora do loop de eventos. A primeira faixa reutiliza o payload já preparado.
  Cada faixa conhecida segue pelo arquivo; as demais usam a resolução normal.
- A aprendizagem de links/Spotify grava primeiro um journal SQLite durável e
  processa aliases depois da entrega do play. Eventos pendentes sobrevivem a
  restart e são retomados pelo coordenador. Não há limite total de músicas
  aprendidas; os limites de RAM e de lote controlam trabalho simultâneo.
- A conexão HTTP local do proxy é reutilizada. Os comandos seguem primeiro ao
  agente, sem snapshot completo de saúde a cada expiração do cache de 8s.
  Headers informam versão e prontidão. Reparos têm orçamento limitado e usam o
  mesmo `command_id`; resposta perdida de agente acessível não provoca restart.
  `MUSIC_AGENT_COMMAND_READY_TTL_SECONDS=0` preserva a auditoria completa antiga.
- Manifests grandes têm cache de metadados separado das assinaturas do CDN.
  Renovar uma assinatura não baixa o JSON outra vez. A revisão do post e os IDs
  dos anexos invalidam o cache; o cliente HTTP para manifests é reutilizado e
  fechado no shutdown. RAM limitada a 128 revisões/4MiB, sem áudio permanente.
- Durante o início do play, processos de arquivamento yt-dlp/FFmpeg em POSIX
  cedem CPU e rede, inclusive se já estiverem em andamento. Os grupos pertencem
  ao arquivador; pausas não consomem o timeout ativo do subprocesso. Fatias de
  trabalho evitam espera indefinida. Hashes e operações Ogg também cedem, e a
  leitura do upload ocorre no executor HTTP, mantendo o loop responsivo. Não
  se cancela um upload aceito nem se apaga staging de uma entrega pendente.
- O agente conserva conexão ociosa por até 10min com humanos e sai de call
  vazia após 2s. Configuração explícita antiga de idle é preservada; o exemplo
  de ambiente passa a indicar 600s. `_disconnect` continua sendo explícito.

Volume, estéreo, mistura de TTS e efeitos permanecem no pipeline de qualidade
da 0.3.82. Não foram aplicadas opções agressivas de FFmpeg que economizaram
apenas 3–5ms num ensaio local com resultados sobrepostos.

## Opção radical implementada

O controller aceita um Music Agent independente como executor de voz na VPS,
enviando `/command` diretamente. O Phone Worker continua atendendo metadados e
arquivo. Os modos `full`, `voice` e `archive` separam funções do agente; comandos
incompatíveis com o modo são recusados.

Ativação é opt-in por `MUSIC_AGENT_DIRECT_API_ENABLED`, endpoint e token privado.
O destino da guild continua vinculado ao executor que assumiu a sessão. Falhas
de rede não trocam executor automaticamente. Faça a troca com sessões
desconectadas e telefone no modo `archive` para evitar disputa pelo mesmo bot.

O [perfil Linux/systemd](../deploy/music-agent-vps/README.md) inclui unidade,
ambientes, dependências, instalação e retorno ao telefone. A API padrão é
loopback; acesso remoto usa HTTPS ou rede privada/Tailscale autenticada. Não foi
instalado ou ativado serviço em uma VPS externa nesta execução.

## Medição

As linhas `[music-start]` contêm JSON com o ID do comando e durações locais do
controller e do agente. O ponto final é o primeiro pacote musical transmitido
com sucesso no socket do Discord. Silêncio sintético, TTS isolado, pacote
descartado e callback de faixa antiga não contam.

Use `python3 scripts/benchmark-music-start.py antes.log --compare depois.log`
para comparar mediana/p95 e redução percentual. O script não compara relógios
de máquinas distintas nem soma fases que ocorrem em paralelo. Medição no cliente
Discord seria necessária para saber o instante exato do som ouvido.

Para um arquivo conhecido com call desconectada, a sobreposição troca a espera
aproximada `max(conexão, resolução) + preparação` por
`max(conexão, resolução + preparação)`. O ganho depende de qual etapa domina.
Nenhuma porcentagem de melhoria na call foi medida nesta sessão.

## Atualizar

Atualize controller e runtime do worker para 0.3.83 e reinicie os processos.
Use o ZIP completo ou o pacote cumulativo de alterações sobre o repositório
original. O pacote `desde-0.3.82` contém apenas esta segunda etapa, para quem já
aplicou a anterior. Não sobrescreva seus arquivos de ambiente/secrets com
exemplos: ajuste as variáveis desejadas.

O release automático do Phone Worker mantém 63 arquivos de runtime mais o
manifest (64 membros), compatível com o bootstrap instalado 1.0.0. O perfil VPS
e a ferramenta de benchmark ficam no checkout, fora desse release.

Arquivamento e aprendizagem continuam sem teto artificial de quantidade ou
duração. Limites reais do Discord, espaço temporário e disponibilidade de rede
continuam governando upload e progresso. Os limites de cache e concorrência
não limitam o acervo do fórum.
